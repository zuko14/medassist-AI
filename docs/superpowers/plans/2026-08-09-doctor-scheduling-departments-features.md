# Doctor Scheduling, Department List, Session-Timeout Fix & New Patient Features — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the false "session timed out" bug, let admins configure real per-doctor slot timings (auto-generated from start/end/duration) and pick from a full Indian-hospital department list, and ship four new patient-facing features (live OPD queue/token status, family/dependent profiles, post-discharge health check-in nudges, and a staff alert on emergency).

**Architecture:** All changes are additive to the existing FastAPI + Supabase + APScheduler + WhatsApp Cloud API stack. No existing runtime read-path (`get_available_slots`, intent whitelist, conversation state machine) is restructured — new columns/tables are added, new state-machine branches are added alongside existing ones, and the one true bug fix is a single-line change in the shared `update_state()` chokepoint.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, Supabase (Postgres), APScheduler, pytest + pytest-asyncio, vanilla JS admin panel (no build step).

## Global Constraints

- Every new DB-writing function must catch exceptions and log via the existing `logger` pattern (see `app/database.py`) — never let a raw Supabase exception bubble into the webhook handler.
- Every new admin API field is optional on `Update` models (`exclude_unset=True` pattern already used in `update_doctor`).
- Every new patient-facing string must be added to all three languages (`en`, `hi`, `te`) in `app/templates/whatsapp_templates.py`, matching the existing `MESSAGES` dict structure.
- Every new intent must be added to both `INTENT_KEYWORDS` (keyword fallback) and the LLM `allowed_intents` whitelist in `app/services/ai_engine.py` — never trust raw LLM output without whitelist validation (existing security pattern, must not regress).
- Migrations are additive only (`ALTER TABLE ... ADD COLUMN`, `CREATE TABLE`) — never drop or rename existing columns.
- `family_members` is a new, separate table keyed by `primary_patient_id` (FK to `patients.id`), NOT by phone — `patients.phone` carries a UNIQUE constraint, so family members must never be inserted as their own `patients` rows.
- No new pip dependencies — everything is buildable with stdlib `datetime` + the existing Supabase/FastAPI/APScheduler stack already installed.
- Before wiring any new admin endpoint, check whether existing appointment endpoints in `app/routers/admin.py` delegate to `app/services/analytics.py` rather than querying `supabase` directly — follow whichever pattern the neighboring code actually uses at execution time.

---

## Phase 0 — Bug fix: false "session timed out"

### Task 1: Clear stale booking-timeout timestamp on return to main menu

**Files:**
- Modify: `app/services/conversation.py:89-117` (`update_state` method)
- Test: `tests/test_conversation_session_timeout.py` (new)

**Interfaces:**
- Consumes: existing `ConversationManager.update_state(clinic, phone, new_state, new_context)` signature — unchanged.
- Produces: no new public interface; behavior change only (stale `booking_context_expires_at` no longer survives a return to `main_menu`).

- [ ] **Step 1: Write failing regression test**

```python
# tests/test_conversation_session_timeout.py
"""Regression test: stale booking_context_expires_at must not falsely
time out a fresh booking started after an old abandoned one."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_update_state_to_main_menu_clears_booking_expiry():
    manager = ConversationManager()
    clinic = {"id": "clinic-1"}

    # Simulate an old, already-expired stale value from a prior abandoned booking
    existing_session = {
        "state": "collecting_symptoms",
        "context": {},
        "booking_context_expires_at": "2020-01-01T00:00:00+00:00",
    }

    with patch(
        "app.database.get_conversation", new_callable=AsyncMock
    ) as mock_get_conv, patch("app.database.supabase") as mock_supabase:
        mock_get_conv.return_value = existing_session
        mock_table = mock_supabase.table.return_value
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = None

        await manager.update_state(clinic, "+919876543210", "main_menu", {})

        # Assert the update payload sent to Supabase clears the stale timestamp
        update_call_kwargs = mock_table.update.call_args[0][0]
        assert update_call_kwargs["booking_context_expires_at"] is None
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_conversation_session_timeout.py -v`
Expected: FAIL — `KeyError: 'booking_context_expires_at'` (the update payload doesn't include this key yet).

- [ ] **Step 3: Implement the fix**

In `app/services/conversation.py`, inside `update_state` (currently lines 89-117), change:

```python
        # Reset menu_shown to False if transitioning BACK to main_menu from another state
        if new_state == "main_menu" and session.get("state") != "main_menu":
            new_context["menu_shown"] = False

        merged = {**existing, **new_context}
        supabase.table("conversations").update(
            {
                "state": new_state,
                "context": merged,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("clinic_id", clinic["id"]).eq("phone", phone).execute()
```

to:

```python
        # Reset menu_shown to False if transitioning BACK to main_menu from another state
        if new_state == "main_menu" and session.get("state") != "main_menu":
            new_context["menu_shown"] = False

        merged = {**existing, **new_context}
        update_payload = {
            "state": new_state,
            "context": merged,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # Any return to main_menu must clear a stale mid-booking expiry timestamp —
        # otherwise a leftover value from an old abandoned booking falsely times out
        # the next booking attempt on its very first message.
        if new_state == "main_menu":
            update_payload["booking_context_expires_at"] = None

        supabase.table("conversations").update(update_payload).eq(
            "clinic_id", clinic["id"]
        ).eq("phone", phone).execute()
```

- [ ] **Step 4: Run test, verify it passes**

Run: `pytest tests/test_conversation_session_timeout.py -v`
Expected: PASS

- [ ] **Step 5: Run full existing conversation test suite to confirm no regression**

Run: `pytest tests/test_conversation_payment_mode.py tests/test_webhook.py -v`
Expected: PASS (no existing behavior depended on the old, buggy leftover value)

- [ ] **Step 6: Commit**

```bash
git add app/services/conversation.py tests/test_conversation_session_timeout.py
git commit -m "fix: clear stale booking_context_expires_at when returning to main_menu

Prevents a leftover timestamp from an abandoned booking falsely
timing out the very next booking attempt."
```

---

## Phase 1 — Doctor slot timings (auto-generated from start/end/duration)

### Task 2: `generate_slots()` helper

**Files:**
- Modify: `app/utils/helpers.py`
- Test: `tests/test_helpers_slots.py` (new)

**Interfaces:**
- Produces: `generate_slots(start: time, end: time, duration_minutes: int) -> list[str]` — used by Task 3 (admin API).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_helpers_slots.py
"""Tests for generate_slots() — pure slot-generation arithmetic."""

from datetime import time

import pytest

from app.utils.helpers import generate_slots


def test_generate_slots_exact_division():
    result = generate_slots(time(9, 0), time(11, 0), 30)
    assert result == ["09:00", "09:30", "10:00", "10:30"]


def test_generate_slots_with_remainder_is_truncated():
    # 09:00-10:15 in 20-min steps: 09:00, 09:20, 09:40, 10:00 (10:20 would exceed 10:15)
    result = generate_slots(time(9, 0), time(10, 15), 20)
    assert result == ["09:00", "09:20", "09:40", "10:00"]


def test_generate_slots_start_equal_end_returns_empty():
    assert generate_slots(time(9, 0), time(9, 0), 30) == []


def test_generate_slots_start_after_end_returns_empty():
    assert generate_slots(time(11, 0), time(9, 0), 30) == []


def test_generate_slots_zero_duration_returns_empty():
    assert generate_slots(time(9, 0), time(11, 0), 0) == []


def test_generate_slots_negative_duration_returns_empty():
    assert generate_slots(time(9, 0), time(11, 0), -10) == []
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_helpers_slots.py -v`
Expected: FAIL with `ImportError: cannot import name 'generate_slots'`

- [ ] **Step 3: Implement `generate_slots`**

Add to `app/utils/helpers.py` (append to end of file; the module already imports `datetime`/`timedelta`/`date` at the top — confirm those imports exist and add any missing ones alongside them rather than re-importing inline):

```python
from datetime import time as time_type  # avoid shadowing the `time` parameter name


def generate_slots(start: time_type, end: time_type, duration_minutes: int) -> list[str]:
    """Generate a list of "HH:MM" slot strings from start to end in fixed steps.

    Returns an empty list for any invalid input (start >= end, duration <= 0)
    rather than raising — callers decide whether that's an error worth
    surfacing (e.g. the admin API returns 422 for a truly invalid form
    submission, distinct from "this shift is intentionally empty").
    """
    if duration_minutes <= 0 or start >= end:
        return []

    slots = []
    current = datetime.combine(date.today(), start)
    end_dt = datetime.combine(date.today(), end)
    step = timedelta(minutes=duration_minutes)

    while current < end_dt:
        slots.append(current.strftime("%H:%M"))
        current += step

    return slots
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_helpers_slots.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/utils/helpers.py tests/test_helpers_slots.py
git commit -m "feat: add generate_slots() helper for doctor shift auto-generation"
```

---

### Task 3: Migration — doctor slot-config columns

**Files:**
- Create: `migrations/017_doctor_slot_config.sql`

- [ ] **Step 1: Write the migration**

```sql
-- Migration 017: Doctor slot-timing configuration columns
-- Run in Supabase SQL Editor
--
-- Adds start/end/duration columns so admins can configure real per-doctor
-- shift timings. morning_slots/evening_slots (existing JSONB columns) remain
-- the materialized list the booking engine reads — these new columns are
-- config only, used by the admin write path to regenerate that list.

ALTER TABLE doctors ADD COLUMN IF NOT EXISTS morning_start TIME NULL;
ALTER TABLE doctors ADD COLUMN IF NOT EXISTS morning_end TIME NULL;
ALTER TABLE doctors ADD COLUMN IF NOT EXISTS evening_start TIME NULL;
ALTER TABLE doctors ADD COLUMN IF NOT EXISTS evening_end TIME NULL;
ALTER TABLE doctors ADD COLUMN IF NOT EXISTS slot_duration_minutes INT NOT NULL DEFAULT 30;

-- Backfill existing doctors' config columns from their current materialized
-- slot arrays so the admin edit form has sensible values to pre-fill,
-- assuming the existing default 30-min cadence (matches the seed data in
-- migrations/001_initial_schema.sql).
UPDATE doctors
SET morning_start = '09:00', morning_end = '12:00'
WHERE morning_start IS NULL AND morning_slots IS NOT NULL AND jsonb_array_length(morning_slots) > 0;

UPDATE doctors
SET evening_start = '17:00', evening_end = '19:00'
WHERE evening_start IS NULL AND evening_slots IS NOT NULL AND jsonb_array_length(evening_slots) > 0;

-- Verify
SELECT id, name, morning_start, morning_end, evening_start, evening_end, slot_duration_minutes
FROM doctors ORDER BY created_at;
```

- [ ] **Step 2: Run the migration against the Supabase project (manual, via Supabase SQL Editor)**

No automated test — this is a schema migration. Verify by running the final `SELECT` and confirming every existing doctor row shows non-null `morning_start`/`morning_end` (or `evening_*`) matching their current materialized slots, and `slot_duration_minutes = 30`.

- [ ] **Step 3: Commit**

```bash
git add migrations/017_doctor_slot_config.sql
git commit -m "feat(db): add doctor slot-timing config columns (migration 017)"
```

---

### Task 4: Admin API — auto-generate slots from start/end/duration

**Files:**
- Modify: `app/routers/admin.py:295-314` (`DoctorCreate`, `DoctorUpdate` models), `app/routers/admin.py:450-495` (`create_doctor`, `update_doctor`)
- Test: `tests/test_doctor_slot_generation.py` (new)

**Interfaces:**
- Consumes: `generate_slots()` from Task 2 (`app/utils/helpers.py`).
- Produces: `DoctorCreate`/`DoctorUpdate` gain `morning_start`, `morning_end`, `evening_start`, `evening_end`, `slot_duration_minutes: int = 30` fields (all `Optional[time]` except duration).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_doctor_slot_generation.py
"""Tests for doctor slot auto-generation on create/update."""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from app.routers.admin import create_doctor, update_doctor, DoctorCreate, DoctorUpdate


@pytest.mark.asyncio
async def test_create_doctor_generates_morning_and_evening_slots():
    payload = DoctorCreate(
        name="Dr. Test",
        specialization="Cardiologist",
        department="Cardiology",
        morning_start="09:00",
        morning_end="11:00",
        evening_start="17:00",
        evening_end="18:00",
        slot_duration_minutes=30,
    )

    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1", "name": "Dr. Test"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        await create_doctor(payload, clinic_id="default", user=MagicMock())

    inserted = mock_sb.table.return_value.insert.call_args[0][0]
    assert inserted["morning_slots"] == ["09:00", "09:30", "10:00", "10:30"]
    assert inserted["evening_slots"] == ["17:00", "17:30"]


@pytest.mark.asyncio
async def test_create_doctor_rejects_end_before_start():
    payload = DoctorCreate(
        name="Dr. Test",
        specialization="Cardiologist",
        department="Cardiology",
        morning_start="11:00",
        morning_end="09:00",
    )

    with pytest.raises(HTTPException) as exc:
        await create_doctor(payload, clinic_id="default", user=MagicMock())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_doctor_regenerates_slots_when_timing_changed():
    payload = DoctorUpdate(morning_start="08:00", morning_end="09:00", slot_duration_minutes=15)

    mock_sb = MagicMock()
    mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        await update_doctor("doc-1", payload, clinic_id="default", user=MagicMock())

    updated = mock_sb.table.return_value.update.call_args[0][0]
    assert updated["morning_slots"] == ["08:00", "08:15", "08:30", "08:45"]
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_doctor_slot_generation.py -v`
Expected: FAIL — `DoctorCreate` has no field `morning_start`, etc.

- [ ] **Step 3: Extend the Pydantic models**

In `app/routers/admin.py`, replace the `DoctorCreate`/`DoctorUpdate` classes (lines 295-314) with:

```python
from datetime import time as time_type


class DoctorCreate(BaseModel):
    name: str
    specialization: str
    department: str
    available_days: str = "Mon,Tue,Wed,Thu,Fri"
    morning_slots: list[str] = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]
    evening_slots: list[str] = ["17:00", "17:30", "18:00", "18:30"]
    is_active: bool = True
    consultation_fee: int = 500
    morning_start: Optional[time_type] = None
    morning_end: Optional[time_type] = None
    evening_start: Optional[time_type] = None
    evening_end: Optional[time_type] = None
    slot_duration_minutes: int = 30


class DoctorUpdate(BaseModel):
    name: Optional[str] = None
    specialization: Optional[str] = None
    department: Optional[str] = None
    available_days: Optional[str] = None
    morning_slots: Optional[list[str]] = None
    evening_slots: Optional[list[str]] = None
    is_active: Optional[bool] = None
    consultation_fee: Optional[int] = None
    morning_start: Optional[time_type] = None
    morning_end: Optional[time_type] = None
    evening_start: Optional[time_type] = None
    evening_end: Optional[time_type] = None
    slot_duration_minutes: Optional[int] = None
```

- [ ] **Step 4: Add a shared slot-regeneration helper and wire it into `create_doctor`/`update_doctor`**

Add this module-level helper in `app/routers/admin.py`, just above `create_doctor` (around line 450):

```python
def _apply_slot_config(data: dict) -> dict:
    """Regenerate morning_slots/evening_slots from start/end/duration if provided.

    Mutates and returns `data` in place. Raises HTTPException(422) if a
    shift's end time isn't after its start time.
    """
    from app.utils.helpers import generate_slots

    duration = data.get("slot_duration_minutes") or 30

    morning_start = data.get("morning_start")
    morning_end = data.get("morning_end")
    if morning_start is not None and morning_end is not None:
        if morning_end <= morning_start:
            raise HTTPException(
                status_code=422, detail="morning_end must be after morning_start"
            )
        data["morning_slots"] = generate_slots(morning_start, morning_end, duration)

    evening_start = data.get("evening_start")
    evening_end = data.get("evening_end")
    if evening_start is not None and evening_end is not None:
        if evening_end <= evening_start:
            raise HTTPException(
                status_code=422, detail="evening_end must be after evening_start"
            )
        data["evening_slots"] = generate_slots(evening_start, evening_end, duration)

    return data
```

Then in `create_doctor` (around line 450-467), change:

```python
    try:
        doctor_data = doctor.dict()
        doctor_data["clinic_id"] = effective_clinic_id
        result = supabase.table("doctors").insert(doctor_data).execute()
```

to:

```python
    try:
        doctor_data = doctor.dict()
        doctor_data = _apply_slot_config(doctor_data)
        doctor_data["clinic_id"] = effective_clinic_id
        result = supabase.table("doctors").insert(doctor_data).execute()
```

And in `update_doctor` (around line 470-493), change:

```python
    try:
        update_data = doctor.dict(exclude_unset=True)
```

to:

```python
    try:
        update_data = doctor.dict(exclude_unset=True)
        update_data = _apply_slot_config(update_data)
```

Note: `time` objects serialize as `datetime.time`, which the Supabase client encodes to `"HH:MM:SS"` automatically — no extra serialization needed, consistent with how `date`/`datetime` fields are already handled elsewhere in this router (e.g. `LeaveCreate.leave_date`).

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_doctor_slot_generation.py -v`
Expected: PASS

- [ ] **Step 6: Run the full admin test suite to confirm no regression**

Run: `pytest tests/test_clinics.py tests/test_plan_features.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/routers/admin.py tests/test_doctor_slot_generation.py
git commit -m "feat(admin): auto-generate doctor morning/evening slots from start/end/duration"
```

---

### Task 5: Admin UI — slot-timing form fields, day checkboxes, and department datalist

**Files:**
- Modify: `admin/index.html:849-876` (doctor form HTML), `admin/index.html:1699-1751` (`submitDoctor`, `resetDoctorForm`, `editDoctor` JS)

- [ ] **Step 1: Replace the doctor form HTML**

In `admin/index.html`, replace lines 849-876 (the `docFormCard` block) with:

```html
            <div class="form-card" id="docFormCard">
                <h3 id="docFormTitle">➕ Add New Doctor</h3>
                <input type="hidden" id="f-docId">
                <div id="docMsg"></div>
                <div class="form-row">
                    <div class="field"><label>Full Name</label><input type="text" id="f-docName" placeholder="Dr. Full Name"></div>
                    <div class="field"><label>Specialization</label><input type="text" id="f-docSpec" placeholder="e.g. Cardiologist"></div>
                </div>
                <div class="form-row">
                    <div class="field">
                        <label>Department</label>
                        <input list="deptOptions" id="f-docDept" placeholder="Select or type a department">
                        <datalist id="deptOptions"></datalist>
                    </div>
                    <div class="field"><label>Consultation Fee (₹)</label><input type="number" id="f-docFee" value="500"></div>
                </div>
                <div class="form-row">
                    <div class="field"><label>Morning Start</label><input type="time" id="f-docMornStart" value="09:00"></div>
                    <div class="field"><label>Morning End</label><input type="time" id="f-docMornEnd" value="12:00"></div>
                </div>
                <div class="form-row">
                    <div class="field"><label>Evening Start</label><input type="time" id="f-docEveStart" value="17:00"></div>
                    <div class="field"><label>Evening End</label><input type="time" id="f-docEveEnd" value="19:00"></div>
                </div>
                <div class="form-row">
                    <div class="field">
                        <label>Slot Duration</label>
                        <select id="f-docDuration">
                            <option value="10">10 min</option>
                            <option value="15">15 min</option>
                            <option value="20">20 min</option>
                            <option value="30" selected>30 min</option>
                            <option value="45">45 min</option>
                            <option value="60">60 min</option>
                        </select>
                    </div>
                    <div class="field">
                        <label>Available Days</label>
                        <div id="f-docDays" style="display:flex; gap:8px; flex-wrap:wrap; margin-top:6px">
                            <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="doc-day-cb" value="Mon" checked>Mon</label>
                            <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="doc-day-cb" value="Tue" checked>Tue</label>
                            <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="doc-day-cb" value="Wed" checked>Wed</label>
                            <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="doc-day-cb" value="Thu" checked>Thu</label>
                            <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="doc-day-cb" value="Fri" checked>Fri</label>
                            <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="doc-day-cb" value="Sat">Sat</label>
                            <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="doc-day-cb" value="Sun">Sun</label>
                        </div>
                    </div>
                </div>
                <div id="docSlotPreview" style="color:var(--text3); font-size:0.8rem; margin:4px 0"></div>
                <button class="btn btn-accent" id="btn-docSubmit" onclick="submitDoctor()" style="margin-top:6px">Add Doctor</button>
                <button class="btn" id="btn-docCancel" onclick="resetDoctorForm()" style="margin-top:6px; display:none; background:var(--surface); border:1px solid var(--border)">Cancel</button>
            </div>
```

- [ ] **Step 2: Populate the department `<datalist>` and add a live slot-count preview**

In the `<script>` section, near the top-level constants (search for `let doctorCache`), add:

```javascript
const DEPARTMENT_OPTIONS = [
    'General Medicine', 'Cardiology', 'Cardiothoracic Surgery', 'Neurology', 'Neurosurgery',
    'Orthopedics', 'Gynecology & Obstetrics', 'Pediatrics', 'Dermatology', 'Ophthalmology',
    'ENT', 'Dental', 'Urology', 'Nephrology', 'Gastroenterology', 'Endocrinology',
    'Pulmonology', 'Oncology', 'Psychiatry', 'General Surgery', 'Plastic Surgery',
    'Radiology', 'Pathology', 'Anesthesiology', 'Emergency Medicine', 'Physiotherapy',
    'Dietetics & Nutrition', 'Ayurveda', 'Homeopathy', 'IVF & Fertility', 'Rheumatology',
    'Diabetology', 'Bariatric Surgery', 'Vascular Surgery', 'Andrology', 'Geriatrics',
    'Sports Medicine',
];

function populateDeptOptions() {
    document.getElementById('deptOptions').innerHTML =
        DEPARTMENT_OPTIONS.map(d => `<option value="${d}">`).join('');
}

function updateSlotPreview() {
    const toMinutes = (t) => { const [h, m] = t.split(':').map(Number); return h * 60 + m; };
    const countSlots = (start, end, dur) => {
        if (!start || !end || !dur) return 0;
        const diff = toMinutes(end) - toMinutes(start);
        return diff > 0 ? Math.floor(diff / dur) : 0;
    };
    const dur = parseInt(document.getElementById('f-docDuration').value) || 30;
    const morn = countSlots(document.getElementById('f-docMornStart').value, document.getElementById('f-docMornEnd').value, dur);
    const eve = countSlots(document.getElementById('f-docEveStart').value, document.getElementById('f-docEveEnd').value, dur);
    document.getElementById('docSlotPreview').textContent = `→ generates ${morn} morning + ${eve} evening slots`;
}

document.addEventListener('DOMContentLoaded', () => {
    populateDeptOptions();
    updateSlotPreview();
    ['f-docMornStart', 'f-docMornEnd', 'f-docEveStart', 'f-docEveEnd', 'f-docDuration'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', updateSlotPreview);
    });
});
```

- [ ] **Step 3: Update `submitDoctor()`, `resetDoctorForm()`, `editDoctor()`**

Replace the existing `submitDoctor` function (lines 1699-1723) with:

```javascript
async function submitDoctor() {
    const id = document.getElementById('f-docId').value;
    const name = document.getElementById('f-docName').value.trim();
    const spec = document.getElementById('f-docSpec').value.trim();
    if (!name || !spec) { msg('docMsg', 'Please fill in name and specialization', true); return; }
    try {
        const days = Array.from(document.querySelectorAll('.doc-day-cb:checked')).map(cb => cb.value).join(',');
        const payload = {
            name,
            specialization: spec,
            department: document.getElementById('f-docDept').value,
            consultation_fee: parseInt(document.getElementById('f-docFee').value) || 500,
            available_days: days || 'Mon,Tue,Wed,Thu,Fri',
            morning_start: document.getElementById('f-docMornStart').value || null,
            morning_end: document.getElementById('f-docMornEnd').value || null,
            evening_start: document.getElementById('f-docEveStart').value || null,
            evening_end: document.getElementById('f-docEveEnd').value || null,
            slot_duration_minutes: parseInt(document.getElementById('f-docDuration').value) || 30,
        };

        if (id) {
            await apiPut(`/admin/doctors/${id}`, payload);
            msg('docMsg', '✅ Doctor updated successfully!');
        } else {
            await apiPost('/admin/doctors', payload);
            msg('docMsg', '✅ Doctor added successfully!');
        }

        resetDoctorForm();
        loadDoctors();
    } catch (e) { msg('docMsg', id ? 'Failed to update doctor' : 'Failed to add doctor', true); }
}
```

Replace `resetDoctorForm` (lines 1725-1733) with:

```javascript
function resetDoctorForm() {
    document.getElementById('f-docId').value = '';
    document.getElementById('f-docName').value = '';
    document.getElementById('f-docSpec').value = '';
    document.getElementById('f-docDept').value = '';
    document.getElementById('f-docFee').value = '500';
    document.getElementById('f-docMornStart').value = '09:00';
    document.getElementById('f-docMornEnd').value = '12:00';
    document.getElementById('f-docEveStart').value = '17:00';
    document.getElementById('f-docEveEnd').value = '19:00';
    document.getElementById('f-docDuration').value = '30';
    document.querySelectorAll('.doc-day-cb').forEach(cb => {
        cb.checked = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].includes(cb.value);
    });
    updateSlotPreview();
    document.getElementById('docFormTitle').innerHTML = '➕ Add New Doctor';
    document.getElementById('btn-docSubmit').textContent = 'Add Doctor';
    document.getElementById('btn-docCancel').style.display = 'none';
}
```

Replace `window.editDoctor` (lines 1735-1751) with:

```javascript
window.editDoctor = function(id) {
    const d = doctorCache.find(x => x.id === id);
    if (!d) return;
    document.getElementById('f-docId').value = d.id;
    document.getElementById('f-docName').value = d.name;
    document.getElementById('f-docSpec').value = d.specialization || '';
    document.getElementById('f-docDept').value = d.department;
    document.getElementById('f-docFee').value = d.consultation_fee;
    document.getElementById('f-docMornStart').value = (d.morning_start || '09:00').slice(0, 5);
    document.getElementById('f-docMornEnd').value = (d.morning_end || '12:00').slice(0, 5);
    document.getElementById('f-docEveStart').value = (d.evening_start || '17:00').slice(0, 5);
    document.getElementById('f-docEveEnd').value = (d.evening_end || '19:00').slice(0, 5);
    document.getElementById('f-docDuration').value = d.slot_duration_minutes || 30;
    const activeDays = (d.available_days || 'Mon,Tue,Wed,Thu,Fri').split(',');
    document.querySelectorAll('.doc-day-cb').forEach(cb => { cb.checked = activeDays.includes(cb.value); });
    updateSlotPreview();

    document.getElementById('docFormTitle').innerHTML = '✏️ Edit Doctor Details';
    document.getElementById('btn-docSubmit').textContent = 'Update Doctor';
    document.getElementById('btn-docCancel').style.display = 'inline-block';

    document.getElementById('f-docName').focus();
    window.scrollTo({ top: document.getElementById('pg-doctors').offsetTop, behavior: 'smooth' });
};
```

- [ ] **Step 4: Manual verification in browser**

Start the admin panel locally (or against a dev deployment), open the Doctors page, add a doctor with Morning 09:00–11:00 / 20-min duration, confirm the preview shows "→ generates 6 morning + 0 evening slots", submit, and confirm the doctor list + edit form round-trip the values correctly. Also confirm the Department field offers the curated list via the datalist dropdown but still accepts free-text.

- [ ] **Step 5: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): doctor form gets slot-timing inputs, day checkboxes, and department datalist"
```

---

## Phase 2 — Emergency fast-path: staff alert enhancement

### Task 6: Alert hospital staff on emergency keyword detection

**Files:**
- Modify: `app/config.py` (add setting), `app/services/conversation.py:2652-2677` (`_handle_emergency`)
- Test: `tests/test_emergency_staff_alert.py` (new)

**Interfaces:**
- Consumes: `settings.hospital_staff_alert_number` (new config field, empty string default = disabled).
- Produces: no new public interface — `_handle_emergency` signature unchanged.

- [ ] **Step 1: Write failing test**

```python
# tests/test_emergency_staff_alert.py
"""Tests for staff alert on emergency detection."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_emergency_alerts_staff_when_configured():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

    with patch("app.services.conversation.settings") as mock_settings, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send, patch(
        "app.services.conversation.log_analytics_event", new_callable=AsyncMock
    ), patch.object(manager, "update_state", new_callable=AsyncMock):
        mock_settings.hospital_staff_alert_number = "+919999999999"
        mock_settings.hospital_emergency_number = "108"
        mock_settings.hospital_maps_link = ""
        mock_settings.hospital_address = ""

        await manager._handle_emergency(clinic, "+918888888888", "en")

        # Two sends: one to the patient, one to staff
        assert mock_send.call_count == 2
        staff_call = mock_send.call_args_list[1]
        assert staff_call.args[1] == "+919999999999"


@pytest.mark.asyncio
async def test_emergency_skips_staff_alert_when_not_configured():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

    with patch("app.services.conversation.settings") as mock_settings, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send, patch(
        "app.services.conversation.log_analytics_event", new_callable=AsyncMock
    ), patch.object(manager, "update_state", new_callable=AsyncMock):
        mock_settings.hospital_staff_alert_number = ""
        mock_settings.hospital_emergency_number = "108"
        mock_settings.hospital_maps_link = ""
        mock_settings.hospital_address = ""

        await manager._handle_emergency(clinic, "+918888888888", "en")

        # Only one send: to the patient
        assert mock_send.call_count == 1
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_emergency_staff_alert.py -v`
Expected: FAIL — `send_text` called once in both cases (no staff alert exists yet).

- [ ] **Step 3: Add the config field**

In `app/config.py`, near the other `hospital_*` fields (around line 21-27), add:

```python
    hospital_staff_alert_number: str = ""  # optional — WhatsApp number reception monitors for emergency alerts; blank = disabled
```

- [ ] **Step 4: Implement the staff alert**

In `app/services/conversation.py`, `_handle_emergency` (currently lines 2652-2677), after the existing location-send block and before `await self.update_state(...)`, add:

```python
        # Alert hospital staff, if a staff alert number is configured for this clinic/platform
        staff_alert_number = get_clinic_contact(
            clinic, "staff_alert_number", settings.hospital_staff_alert_number
        )
        if staff_alert_number:
            from app.utils.security import mask_phone

            staff_msg = (
                f"🚨 Emergency keyword detected\n\n"
                f"Patient: {mask_phone(phone)}\n"
                f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"Please follow up if not already in contact."
            )
            await self.whatsapp.send_text(clinic, staff_alert_number, staff_msg)
```

Before finalizing, check `get_clinic_contact`'s implementation (likely `app/services/tenant.py`) — if it doesn't recognize `"staff_alert_number"` as a known lookup key yet, add it to whatever key table it uses there, following the exact same pattern as the existing `"emergency_number"` key.

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_emergency_staff_alert.py -v`
Expected: PASS

- [ ] **Step 6: Add `HOSPITAL_STAFF_ALERT_NUMBER` to `.env.example`**

Add a line next to the other `HOSPITAL_*` variables: `HOSPITAL_STAFF_ALERT_NUMBER=` with a comment `# Optional — WhatsApp number for emergency staff alerts`.

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/services/conversation.py tests/test_emergency_staff_alert.py .env.example
git commit -m "feat: alert hospital staff via WhatsApp when emergency keyword is detected"
```

---

## Phase 3 — Post-discharge health check-in

### Task 7: Migration — health check-in tracking columns

**Files:**
- Create: `migrations/018_health_checkin_columns.sql`

- [ ] **Step 1: Write the migration**

```sql
-- Migration 018: Post-discharge health check-in tracking
-- Run in Supabase SQL Editor
--
-- Separate from the existing `followup_sent` column, which drives a
-- same-day/next-day satisfaction survey. These two new flags drive a
-- distinct clinical safety check-in on day+3 and day+7 after the visit.

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS health_checkin_3d_sent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS health_checkin_7d_sent BOOLEAN NOT NULL DEFAULT false;

-- Verify
SELECT column_name FROM information_schema.columns
WHERE table_name = 'appointments' AND column_name LIKE 'health_checkin%';
```

- [ ] **Step 2: Run the migration against Supabase and verify the two columns exist**

- [ ] **Step 3: Commit**

```bash
git add migrations/018_health_checkin_columns.sql
git commit -m "feat(db): add health check-in tracking columns (migration 018)"
```

---

### Task 8: Multilingual health check-in messages

**Files:**
- Modify: `app/templates/whatsapp_templates.py` (`MESSAGES` dict, all 3 languages)

- [ ] **Step 1: Add the message keys**

In `app/templates/whatsapp_templates.py`, add to each language block in `MESSAGES` (`en` around line 191, `hi` around line 222, `te` around line 253 — add just before the closing `}` of each block):

English:
```python
        "health_checkin": "Hi {name}, checking in after your visit to Dr. {doctor}. How are you feeling?",
        "health_checkin_concern": "Sorry to hear that. Please call us at {phone} so we can help — don't wait if symptoms are serious.",
        "health_checkin_ok": "Great to hear! Take care, and reach out anytime if that changes.",
```

Hindi:
```python
        "health_checkin": "नमस्ते {name}, डॉ. {doctor} से आपकी मुलाकात के बाद जांच कर रहे हैं। आप कैसा महसूस कर रहे हैं?",
        "health_checkin_concern": "यह सुनकर खेद है। कृपया हमें {phone} पर कॉल करें ताकि हम मदद कर सकें — लक्षण गंभीर होने पर प्रतीक्षा न करें।",
        "health_checkin_ok": "यह सुनकर अच्छा लगा! ध्यान रखें, और कुछ बदलने पर कभी भी संपर्क करें।",
```

Telugu:
```python
        "health_checkin": "నమస్తే {name}, డాక్టర్ {doctor} వద్ద మీ సందర్శన తర్వాత తనిఖీ చేస్తున్నాము. మీరు ఎలా ఫీల్ అవుతున్నారు?",
        "health_checkin_concern": "అది వినడం బాధగా ఉంది. దయచేసి మాకు {phone} కు కాల్ చేయండి — లక్షణాలు తీవ్రంగా ఉంటే వేచి ఉండకండి.",
        "health_checkin_ok": "వినడం సంతోషంగా ఉంది! జాగ్రత్తగా ఉండండి, మార్పు ఉంటే ఎప్పుడైనా సంప్రదించండి.",
```

- [ ] **Step 2: Manual sanity check**

Run: `python -c "from app.templates.whatsapp_templates import get_message; print(get_message('health_checkin', 'en', name='Ravi', doctor='Rao'))"`
Expected: `Hi Ravi, checking in after your visit to Dr. Rao. How are you feeling?`

- [ ] **Step 3: Commit**

```bash
git add app/templates/whatsapp_templates.py
git commit -m "feat: add multilingual post-discharge health check-in messages"
```

---

### Task 9: Scheduler job — send day+3/day+7 health check-ins

**Files:**
- Modify: `app/services/scheduler.py`
- Test: `tests/test_scheduler_health_checkin.py` (new)

**Interfaces:**
- Consumes: `get_message("health_checkin", ...)` from Task 8.
- Produces: `SchedulerService.send_health_checkins()` — new method, registered as a daily cron job.

- [ ] **Step 1: Write failing test**

```python
# tests/test_scheduler_health_checkin.py
"""Tests for the post-discharge health check-in scheduler job."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.scheduler import SchedulerService


@pytest.mark.asyncio
async def test_send_health_checkins_sends_3day_and_marks_flag():
    service = SchedulerService()
    mock_appt = {
        "id": "appt-1",
        "clinic_id": "clinic-1",
        "patient_phone": "+919876543210",
        "patient_name": "Ravi Kumar",
        "doctor_name": "Dr. Rao",
        "status": "confirmed",
    }

    mock_sb = MagicMock()
    # First query (3-day) returns the appointment, second query (7-day) returns none
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.side_effect = [
        MagicMock(data=[mock_appt]),
        MagicMock(data=[]),
    ]
    mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = None

    with patch("app.services.scheduler.supabase", mock_sb), patch(
        "app.services.scheduler.get_clinic_by_id", new_callable=AsyncMock
    ) as mock_get_clinic, patch(
        "app.services.scheduler.whatsapp_service.send_interactive_buttons", new_callable=AsyncMock
    ) as mock_send:
        mock_get_clinic.return_value = {"id": "clinic-1", "name": "Test Hospital"}

        await service.send_health_checkins()

        mock_send.assert_called_once()
        sent_phone = mock_send.call_args[0][1]
        assert sent_phone == "+919876543210"

        # Confirm the 3-day flag was marked sent
        update_call_args = mock_sb.table.return_value.update.call_args[0][0]
        assert update_call_args == {"health_checkin_3d_sent": True}
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_scheduler_health_checkin.py -v`
Expected: FAIL — `AttributeError: 'SchedulerService' object has no attribute 'send_health_checkins'`

- [ ] **Step 3: Implement `send_health_checkins`**

In `app/services/scheduler.py`, add this method to `SchedulerService`, right after `send_followups` (after line 284):

```python
    async def send_health_checkins(self):
        """Send day+3 and day+7 post-discharge health check-ins.

        Distinct from send_followups (same-day satisfaction survey) —
        this is a clinical safety check, tracked via separate flags
        (health_checkin_3d_sent / health_checkin_7d_sent). Uses interactive
        buttons ("Feeling fine" / "Still have symptoms") so replies route
        through the intent system rather than free text.
        """
        for offset_days, flag_field in [(3, "health_checkin_3d_sent"), (7, "health_checkin_7d_sent")]:
            try:
                target_date = (datetime.now() - timedelta(days=offset_days)).strftime("%Y-%m-%d")

                appointments = (
                    supabase.table("appointments")
                    .select("*")
                    .eq("appointment_date", target_date)
                    .eq("status", "confirmed")
                    .eq(flag_field, False)
                    .execute()
                )

                for appt in appointments.data:
                    try:
                        clinic = await get_clinic_by_id(appt.get("clinic_id", "default"))
                        lang = "en"  # patient language isn't stored on appointments; default to English

                        from app.templates.whatsapp_templates import get_message

                        first_name = (appt.get("patient_name") or "there").split()[0]
                        text = get_message(
                            "health_checkin",
                            lang,
                            name=first_name,
                            doctor=appt.get("doctor_name", ""),
                        )

                        await whatsapp_service.send_interactive_buttons(
                            clinic,
                            appt["patient_phone"],
                            body=text,
                            buttons=[
                                {"id": "checkin_ok", "title": "Feeling fine"},
                                {"id": "checkin_concern", "title": "Still have symptoms"},
                            ],
                        )

                        supabase.table("appointments").update({flag_field: True}).eq(
                            "id", appt["id"]
                        ).execute()

                        logger.info(
                            f"Sent day+{offset_days} health check-in for appointment {appt['id']}"
                        )
                    except Exception as e:
                        logger.error(f"Error sending day+{offset_days} health check-in: {e}")

            except Exception as e:
                logger.error(f"Error in health check-in job (day+{offset_days}): {e}")
```

Then register it in `start()`, right after the `followups` job registration (after line 57):

```python
        # Post-discharge health check-ins (day+3, day+7 — runs daily at 10:30 AM)
        self.scheduler.add_job(
            self.send_health_checkins,
            CronTrigger(hour=10, minute=30),
            id="health_checkins",
            replace_existing=True,
        )
```

- [ ] **Step 4: Run test, verify it passes**

Run: `pytest tests/test_scheduler_health_checkin.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/scheduler.py tests/test_scheduler_health_checkin.py
git commit -m "feat: add day+3/day+7 post-discharge health check-in scheduler job"
```

---

### Task 10: Bot handling of "Still have symptoms" / "Feeling fine" replies

**Files:**
- Modify: `app/services/conversation.py` (button dispatch block near line 385-407, plus two new handler methods, plus ANY-state routing near line 557-560)
- Test: `tests/test_health_checkin_response.py` (new)

**Interfaces:**
- Consumes: `checkin_ok`/`checkin_concern` button IDs sent by Task 9's `send_interactive_buttons` call.
- Produces: `ConversationManager._handle_health_checkin_concern(clinic, phone, lang)`, `ConversationManager._handle_health_checkin_ok(clinic, phone, lang)` — new methods.

- [ ] **Step 1: Write failing test**

```python
# tests/test_health_checkin_response.py
"""Tests for patient response to the post-discharge health check-in."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_health_checkin_concern_sends_contact_and_logs_event():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

    with patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send, patch(
        "app.services.conversation.log_analytics_event", new_callable=AsyncMock
    ) as mock_log:
        await manager._handle_health_checkin_concern(clinic, "+919876543210", "en")

        mock_send.assert_called_once()
        mock_log.assert_called_once()
        assert mock_log.call_args[0][2] == "discharge_checkin_concern"


@pytest.mark.asyncio
async def test_health_checkin_ok_sends_acknowledgement():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

    with patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock) as mock_send:
        await manager._handle_health_checkin_ok(clinic, "+919876543210", "en")
        mock_send.assert_called_once()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_health_checkin_response.py -v`
Expected: FAIL — `AttributeError`

- [ ] **Step 3: Implement the handlers**

In `app/services/conversation.py`, add the new handler methods near `_handle_emergency` (after line 2677):

```python
    async def _handle_health_checkin_concern(
        self, clinic: dict, phone: str, lang: str
    ) -> None:
        """Patient reported ongoing symptoms in a post-discharge check-in."""
        await self.whatsapp.send_text(
            clinic, phone, get_message("health_checkin_concern", lang, phone=clinic["whatsapp_number"])
        )
        await log_analytics_event(clinic["id"], phone, "discharge_checkin_concern")

    async def _handle_health_checkin_ok(self, clinic: dict, phone: str, lang: str) -> None:
        """Patient confirmed they're feeling fine in a post-discharge check-in."""
        await self.whatsapp.send_text(clinic, phone, get_message("health_checkin_ok", lang))
```

- [ ] **Step 4: Wire the button IDs into intent parsing and ANY-state routing**

In the interactive-button parsing block in `app/services/conversation.py` (near line 385-388, alongside `suggest_yes`/`suggest_no`), add:

```python
            elif button_id == "checkin_concern":
                intent = "health_checkin_concern"
            elif button_id == "checkin_ok":
                intent = "health_checkin_ok"
```

Route both intents from ANY state, alongside the existing `emergency`/`opt_out` checks (near line 557-560):

```python
        if intent == "health_checkin_concern":
            await self._handle_health_checkin_concern(clinic, phone, lang)
            return

        if intent == "health_checkin_ok":
            await self._handle_health_checkin_ok(clinic, phone, lang)
            return
```

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_health_checkin_response.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/conversation.py tests/test_health_checkin_response.py
git commit -m "feat: handle patient replies to post-discharge health check-in"
```

---

## Phase 4 — Live OPD queue/token status

### Task 11: Migration — queue/token columns

**Files:**
- Create: `migrations/019_appointment_queue_tokens.sql`

- [ ] **Step 1: Write the migration**

```sql
-- Migration 019: Live OPD queue/token status
-- Run in Supabase SQL Editor

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS token_number INT NULL;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS queue_status VARCHAR(20) NULL
    CHECK (queue_status IN ('waiting', 'in_consultation', 'done'));

-- Index to keep "next token for doctor+date" and "currently serving" lookups fast
CREATE INDEX IF NOT EXISTS idx_appointments_queue
    ON appointments (clinic_id, doctor_name, appointment_date, token_number);

-- Verify
SELECT column_name FROM information_schema.columns
WHERE table_name = 'appointments' AND column_name IN ('token_number', 'queue_status');
```

- [ ] **Step 2: Run against Supabase, verify columns + index exist**

- [ ] **Step 3: Commit**

```bash
git add migrations/019_appointment_queue_tokens.sql
git commit -m "feat(db): add live OPD queue/token columns (migration 019)"
```

---

### Task 12: Database helpers — check-in, call-next, queue lookup

**Files:**
- Modify: `app/database.py`
- Test: `tests/test_queue_database.py` (new)

**Interfaces:**
- Produces: `check_in_appointment(clinic_id, appointment_id) -> Optional[dict]`, `call_next_patient(clinic_id, doctor_name, date_str) -> Optional[dict]`, `get_patient_queue_status(clinic_id, phone, date_str) -> Optional[dict]` — used by Task 13 (admin API) and Task 14 (bot intent).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_queue_database.py
"""Tests for queue/token database helpers."""

import pytest
from unittest.mock import MagicMock, patch

from app.database import check_in_appointment, call_next_patient, get_patient_queue_status


@pytest.mark.asyncio
async def test_check_in_appointment_assigns_next_token():
    mock_sb = MagicMock()
    # MAX(token_number) query returns existing max of 3
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"token_number": 3}]
    )
    mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "appt-1", "token_number": 4, "queue_status": "waiting"}]
    )

    with patch("app.database.supabase", mock_sb):
        result = await check_in_appointment("clinic-1", "appt-1")

    assert result["token_number"] == 4
    assert result["queue_status"] == "waiting"


@pytest.mark.asyncio
async def test_check_in_appointment_first_token_of_day_is_1():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[]
    )
    mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "appt-1", "token_number": 1, "queue_status": "waiting"}]
    )

    with patch("app.database.supabase", mock_sb):
        result = await check_in_appointment("clinic-1", "appt-1")

    assert result["token_number"] == 1


@pytest.mark.asyncio
async def test_get_patient_queue_status_not_checked_in():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "appt-1", "token_number": None, "doctor_name": "Dr. Rao"}]
    )

    with patch("app.database.supabase", mock_sb):
        result = await get_patient_queue_status("clinic-1", "+919876543210", "2026-08-09")

    assert result["checked_in"] is False
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_queue_database.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement the helpers**

Add to `app/database.py`, after `get_patient_appointments` (find the end of that function first):

```python
async def check_in_appointment(clinic_id: str, appointment_id: str) -> Optional[dict]:
    """Assign the next sequential token number for this appointment's doctor+date."""
    try:
        appt_result = (
            supabase.table("appointments")
            .select("doctor_name, appointment_date")
            .eq("clinic_id", clinic_id)
            .eq("id", appointment_id)
            .execute()
        )
        if not appt_result.data:
            return None
        doctor_name = appt_result.data[0]["doctor_name"]
        appointment_date = appt_result.data[0]["appointment_date"]

        max_result = (
            supabase.table("appointments")
            .select("token_number")
            .eq("clinic_id", clinic_id)
            .eq("doctor_name", doctor_name)
            .eq("appointment_date", appointment_date)
            .order("token_number", desc=True)
            .limit(1)
            .execute()
        )
        current_max = max_result.data[0]["token_number"] if max_result.data and max_result.data[0]["token_number"] else 0
        next_token = current_max + 1

        result = (
            supabase.table("appointments")
            .update({"token_number": next_token, "queue_status": "waiting"})
            .eq("clinic_id", clinic_id)
            .eq("id", appointment_id)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error checking in appointment: {e}")
        return None


async def call_next_patient(clinic_id: str, doctor_name: str, date_str: str) -> Optional[dict]:
    """Mark the current in_consultation patient done, and the next waiting patient in_consultation."""
    try:
        supabase.table("appointments").update({"queue_status": "done"}).eq(
            "clinic_id", clinic_id
        ).eq("doctor_name", doctor_name).eq("appointment_date", date_str).eq(
            "queue_status", "in_consultation"
        ).execute()

        next_result = (
            supabase.table("appointments")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("doctor_name", doctor_name)
            .eq("appointment_date", date_str)
            .eq("queue_status", "waiting")
            .order("token_number")
            .limit(1)
            .execute()
        )
        if not next_result.data:
            return None

        next_appt = next_result.data[0]
        supabase.table("appointments").update({"queue_status": "in_consultation"}).eq(
            "clinic_id", clinic_id
        ).eq("id", next_appt["id"]).execute()
        return next_appt
    except Exception as e:
        logger.error(f"Error calling next patient: {e}")
        return None


async def get_patient_queue_status(clinic_id: str, phone: str, date_str: str) -> Optional[dict]:
    """Look up a patient's queue position for today's appointment."""
    try:
        result = (
            supabase.table("appointments")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("patient_phone", phone)
            .eq("appointment_date", date_str)
            .eq("status", "confirmed")
            .execute()
        )
        if not result.data:
            return None

        appt = result.data[0]
        if not appt.get("token_number"):
            return {"checked_in": False, "doctor_name": appt.get("doctor_name")}

        serving_result = (
            supabase.table("appointments")
            .select("token_number")
            .eq("clinic_id", clinic_id)
            .eq("doctor_name", appt["doctor_name"])
            .eq("appointment_date", date_str)
            .in_("queue_status", ["waiting", "in_consultation"])
            .order("token_number")
            .limit(1)
            .execute()
        )
        currently_serving = (
            serving_result.data[0]["token_number"] if serving_result.data else appt["token_number"]
        )
        patients_ahead = max(0, appt["token_number"] - currently_serving)

        return {
            "checked_in": True,
            "token_number": appt["token_number"],
            "currently_serving": currently_serving,
            "patients_ahead": patients_ahead,
            "doctor_name": appt["doctor_name"],
        }
    except Exception as e:
        logger.error(f"Error getting patient queue status: {e}")
        return None
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_queue_database.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/database.py tests/test_queue_database.py
git commit -m "feat(db): add check-in, call-next, and queue-status lookup helpers"
```

---

### Task 13: Admin API + UI — Check In / Call Next

**Files:**
- Modify: `app/routers/admin.py` (new endpoints, near the existing `/appointments/*` routes at line 406-421), `admin/index.html:1625-1660` (appointments table)

**Interfaces:**
- Consumes: `check_in_appointment`, `call_next_patient` from Task 12.
- Produces: `POST /admin/appointments/{appointment_id}/check-in`, `POST /admin/doctors/{doctor_name}/queue/call-next?date=YYYY-MM-DD`.

- [ ] **Step 1: Add the admin endpoints**

In `app/routers/admin.py`, near the existing appointments routes (after line 421), add:

```python
@router.post("/appointments/{appointment_id}/check-in")
async def check_in_appointment_endpoint(
    appointment_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)
):
    """Check in a patient for today's queue — assigns the next token number."""
    from app.database import check_in_appointment

    effective_clinic_id = clinic_id if user.role == "platform_owner" else user.clinic_id
    result = await check_in_appointment(effective_clinic_id, appointment_id)
    if not result:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return result


@router.post("/doctors/{doctor_name}/queue/call-next")
async def call_next_patient_endpoint(
    doctor_name: str,
    date: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Mark the current patient done and call the next waiting patient."""
    from app.database import call_next_patient

    effective_clinic_id = clinic_id if user.role == "platform_owner" else user.clinic_id
    result = await call_next_patient(effective_clinic_id, doctor_name, date)
    if not result:
        return {"message": "No more waiting patients"}
    return result
```

Before finalizing, check whether the neighboring `/appointments/recent` and `/appointments/upcoming` endpoints (lines 406-421) call `supabase` directly or delegate to `app/services/analytics.py` — match whichever pattern they actually use, and confirm the exact spelling of `user.role`/`user.clinic_id` (or whatever the real `AdminUser`/dependency object exposes) against the live code before wiring `effective_clinic_id`.

- [ ] **Step 2: Add "Check In" button to today's appointments**

In `admin/index.html`, update the appointments table row template (lines 1638-1657) to add a Check In action for today's confirmed appointments:

```javascript
            <tbody>${data.map(a => `<tr>
                <td style="color:var(--accent2);font-weight:600;font-family:monospace">${a.booking_ref || '—'}</td>
                <td>${a.patient_name || a.patient_phone || '—'}</td>
                <td>${a.doctor_name || '—'}</td>
                <td>${a.department || '—'}</td>
                <td>${a.appointment_date || '—'}</td>
                <td>${a.appointment_time || '—'}</td>
                <td>${badge(a.status)}${a.token_number ? ` · Token #${a.token_number}` : ''}</td>
                <td>
                    ${a.status === 'confirmed' && a.appointment_date === new Date().toISOString().slice(0, 10) && !a.token_number ?
                        `<button style="background:var(--green-bg,#dcfce7);color:var(--green,#16a34a);border:none;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;margin-right:4px" onclick="checkInAppt('${a.id}')">Check In</button>` : ''}
                    ${a.status === 'confirmed' ?
                        `<button style="background:#fee2e2;color:#dc2626;border:none;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;" onclick="cancelAppt('${a.id}')">Cancel</button>` : '—'}
                </td>
            </tr>`).join('')}</tbody>
```

Add the `checkInAppt` function right after `loadAppointments` (after line 1660):

```javascript
window.checkInAppt = async function(id) {
    try {
        await apiPost(`/admin/appointments/${id}/check-in`, {});
        toast('Patient checked in');
        loadAppointments();
    } catch (e) { toast('Failed to check in patient', true); }
};
```

- [ ] **Step 3: Manual verification**

Book a test appointment for today, load the Appointments admin page, confirm the "Check In" button appears only for today's confirmed, not-yet-checked-in appointment, click it, confirm the row now shows "Token #1" and the button disappears.

- [ ] **Step 4: Commit**

```bash
git add app/routers/admin.py admin/index.html
git commit -m "feat(admin): add Check In / Call Next queue management endpoints and UI"
```

---

### Task 14: Bot intent — "my token" / "queue status"

**Files:**
- Modify: `app/services/ai_engine.py` (`INTENT_KEYWORDS`, LLM prompt list, `allowed_intents`), `app/services/conversation.py` (routing + new handler), `app/templates/whatsapp_templates.py` (messages)
- Test: `tests/test_queue_status_intent.py` (new)

**Interfaces:**
- Consumes: `get_patient_queue_status` from Task 12.
- Produces: `ConversationManager._handle_queue_status(clinic, phone, lang)` — new method; `"queue_status"` added as a recognized intent end-to-end.

- [ ] **Step 1: Write failing test**

```python
# tests/test_queue_status_intent.py
"""Tests for the 'my token'/queue status bot intent."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.ai_engine import keyword_intent_fallback
from app.services.conversation import ConversationManager


def test_keyword_fallback_detects_queue_status():
    assert keyword_intent_fallback("what is my token number") == "queue_status"
    assert keyword_intent_fallback("queue status please") == "queue_status"


@pytest.mark.asyncio
async def test_handle_queue_status_checked_in():
    manager = ConversationManager()
    clinic = {"id": "clinic-1"}

    with patch(
        "app.services.conversation.get_patient_queue_status", new_callable=AsyncMock
    ) as mock_lookup, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send:
        mock_lookup.return_value = {
            "checked_in": True,
            "token_number": 14,
            "currently_serving": 9,
            "patients_ahead": 5,
            "doctor_name": "Dr. Rao",
        }

        await manager._handle_queue_status(clinic, "+919876543210", "en")

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][2]
        assert "14" in sent_text
        assert "5" in sent_text


@pytest.mark.asyncio
async def test_handle_queue_status_not_checked_in():
    manager = ConversationManager()
    clinic = {"id": "clinic-1"}

    with patch(
        "app.services.conversation.get_patient_queue_status", new_callable=AsyncMock
    ) as mock_lookup, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send:
        mock_lookup.return_value = {"checked_in": False, "doctor_name": "Dr. Rao"}

        await manager._handle_queue_status(clinic, "+919876543210", "en")

        sent_text = mock_send.call_args[0][2]
        assert "check" in sent_text.lower()


@pytest.mark.asyncio
async def test_handle_queue_status_no_appointment_today():
    manager = ConversationManager()
    clinic = {"id": "clinic-1"}

    with patch(
        "app.services.conversation.get_patient_queue_status", new_callable=AsyncMock
    ) as mock_lookup, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send:
        mock_lookup.return_value = None

        await manager._handle_queue_status(clinic, "+919876543210", "en")

        sent_text = mock_send.call_args[0][2]
        assert "no" in sent_text.lower() or "don't have" in sent_text.lower()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_queue_status_intent.py -v`
Expected: FAIL — keyword fallback doesn't return `queue_status`; `_handle_queue_status` doesn't exist.

- [ ] **Step 3: Add the intent keywords**

In `app/services/ai_engine.py`, add a new entry to `INTENT_KEYWORDS` (after the `"doctor_availability"` entry, around line 64):

```python
    "queue_status": [
        "my token",
        "token number",
        "queue status",
        "queue position",
        "my turn",
        "how long wait",
        "मेरा टोकन",
        "क्यू स्टेटस",
        "నా టోకెన్",
        "క్యూ స్థితి",
    ],
```

Add `queue_status` to the LLM prompt's intent list (line ~527-529):

```python
book_appointment, cancel_appointment, reschedule_appointment, view_services,
doctor_availability, emergency, opt_out, data_deletion_request, human_escalation,
followup_booking, greeting, queue_status, or unknown.
```

And to the `allowed_intents` whitelist (line ~546-559):

```python
        allowed_intents = {
            "book_appointment",
            "cancel_appointment",
            "reschedule_appointment",
            "view_services",
            "doctor_availability",
            "emergency",
            "opt_out",
            "data_deletion_request",
            "human_escalation",
            "followup_booking",
            "greeting",
            "queue_status",
            "unknown",
        }
```

- [ ] **Step 4: Add the message templates**

In `app/templates/whatsapp_templates.py`, add to each language's `MESSAGES` block (alongside the health-checkin keys added in Task 8):

English:
```python
        "queue_status_waiting": "Token #{token} — now serving #{serving}. About {ahead} patient(s) ahead of you (~{wait} min estimated wait).",
        "queue_status_not_checked_in": "You haven't checked in at reception yet for your appointment with {doctor}. Please check in when you arrive.",
        "queue_status_none": "I don't see a confirmed appointment for you today. If you think this is a mistake, please call us.",
```

Hindi:
```python
        "queue_status_waiting": "टोकन #{token} — अभी #{serving} की सेवा हो रही है। आपसे पहले लगभग {ahead} मरीज़ हैं (~{wait} मिनट अनुमानित प्रतीक्षा)।",
        "queue_status_not_checked_in": "आपने {doctor} के साथ अपने अपॉइंटमेंट के लिए अभी तक रिसेप्शन पर चेक इन नहीं किया है। पहुंचने पर कृपया चेक इन करें।",
        "queue_status_none": "मुझे आज आपके लिए कोई पुष्ट अपॉइंटमेंट नहीं दिख रहा। अगर यह गलती है, तो कृपया हमें कॉल करें।",
```

Telugu:
```python
        "queue_status_waiting": "టోకెన్ #{token} — ఇప్పుడు #{serving} సేవలో ఉంది. మీ ముందు దాదాపు {ahead} మంది రోగులు ఉన్నారు (~{wait} నిమిషాల అంచనా వేచి ఉండే సమయం).",
        "queue_status_not_checked_in": "మీరు డాక్టర్ {doctor} తో మీ అపాయింట్‌మెంట్ కోసం ఇంకా రిసెప్షన్‌లో చెక్ ఇన్ చేయలేదు. దయచేసి వచ్చినప్పుడు చెక్ ఇన్ చేయండి.",
        "queue_status_none": "ఈరోజు మీకు నిర్ధారించబడిన అపాయింట్‌మెంట్ కనిపించడం లేదు. ఇది పొరపాటు అని మీరు భావిస్తే, దయచేసి మాకు కాల్ చేయండి.",
```

- [ ] **Step 5: Implement `_handle_queue_status` and wire routing**

In `app/services/conversation.py`, add the import at the top (alongside other `app.database` imports):

```python
from app.database import get_patient_queue_status
```

Add the handler method near `_handle_emergency`:

```python
    async def _handle_queue_status(self, clinic: dict, phone: str, lang: str) -> None:
        """Reply with the patient's live OPD queue/token position, if checked in today."""
        from datetime import date as date_type

        status = await get_patient_queue_status(
            clinic["id"], phone, date_type.today().strftime("%Y-%m-%d")
        )

        if status is None:
            await self.whatsapp.send_text(clinic, phone, get_message("queue_status_none", lang))
            return

        if not status["checked_in"]:
            await self.whatsapp.send_text(
                clinic,
                phone,
                get_message(
                    "queue_status_not_checked_in", lang, doctor=status.get("doctor_name", "")
                ),
            )
            return

        avg_consult_minutes = 5  # ponytail: fixed estimate, make per-doctor configurable if this proves inaccurate in practice
        estimated_wait = status["patients_ahead"] * avg_consult_minutes

        await self.whatsapp.send_text(
            clinic,
            phone,
            get_message(
                "queue_status_waiting",
                lang,
                token=status["token_number"],
                serving=status["currently_serving"],
                ahead=status["patients_ahead"],
                wait=estimated_wait,
            ),
        )
```

Wire the intent routing from ANY state, alongside the existing emergency/opt-out checks (near line 557-560 in the dispatcher):

```python
        if intent == "queue_status":
            await self._handle_queue_status(clinic, phone, lang)
            return
```

- [ ] **Step 6: Run tests, verify they pass**

Run: `pytest tests/test_queue_status_intent.py -v`
Expected: PASS

- [ ] **Step 7: Run the full ai_engine + conversation test suites to confirm no regression**

Run: `pytest tests/test_ai_engine.py tests/test_conversation_payment_mode.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/services/ai_engine.py app/services/conversation.py app/templates/whatsapp_templates.py tests/test_queue_status_intent.py
git commit -m "feat: add 'my token'/queue status bot intent for live OPD queue lookup"
```

---

## Phase 5 — Family/dependent profiles

### Task 15: Migration — `family_members` table

**Files:**
- Create: `migrations/020_family_members.sql`

- [ ] **Step 1: Write the migration**

```sql
-- Migration 020: Family/dependent profiles
-- Run in Supabase SQL Editor
--
-- Separate table, NOT rows in `patients` — patients.phone carries a UNIQUE
-- constraint, so dependents who share the primary patient's WhatsApp number
-- cannot be represented as their own patients row. family_members is a
-- convenience lookup keyed by primary_patient_id, purely to pre-fill
-- appointments.patient_name during booking.

CREATE TABLE IF NOT EXISTS family_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    primary_patient_id UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    relation VARCHAR(50),
    age INT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_family_members_primary_patient
    ON family_members (clinic_id, primary_patient_id);

-- Verify
SELECT table_name FROM information_schema.tables WHERE table_name = 'family_members';
```

Confirm `clinics.id` and `patients.id` are both `UUID` (matches every other multi-tenant table's `clinic_id UUID REFERENCES clinics(id)` pattern already in this codebase, e.g. `migrations/003_multi_tenant.sql`) before running — adjust column types if the live schema differs.

- [ ] **Step 2: Run against Supabase, verify table + index exist**

- [ ] **Step 3: Commit**

```bash
git add migrations/020_family_members.sql
git commit -m "feat(db): add family_members table for dependent profiles (migration 020)"
```

---

### Task 16: Database helpers — list and add family members

**Files:**
- Modify: `app/database.py`
- Test: `tests/test_family_members_database.py` (new)

**Interfaces:**
- Produces: `get_family_members(clinic_id, primary_patient_id) -> list[dict]`, `add_family_member(clinic_id, primary_patient_id, name, relation=None, age=None) -> dict`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_family_members_database.py
"""Tests for family_members database helpers."""

import pytest
from unittest.mock import MagicMock, patch

from app.database import get_family_members, add_family_member


@pytest.mark.asyncio
async def test_get_family_members_returns_list():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "fam-1", "name": "Priya Kumar", "relation": "spouse"}]
    )

    with patch("app.database.supabase", mock_sb):
        result = await get_family_members("clinic-1", "patient-1")

    assert len(result) == 1
    assert result[0]["name"] == "Priya Kumar"


@pytest.mark.asyncio
async def test_add_family_member_inserts_and_returns_row():
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "fam-2", "name": "Arjun Kumar", "relation": "child"}]
    )

    with patch("app.database.supabase", mock_sb):
        result = await add_family_member("clinic-1", "patient-1", "Arjun Kumar", relation="child")

    assert result["name"] == "Arjun Kumar"
    inserted = mock_sb.table.return_value.insert.call_args[0][0]
    assert inserted["primary_patient_id"] == "patient-1"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_family_members_database.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement the helpers**

Add to `app/database.py`, after the queue helpers added in Task 12:

```python
async def get_family_members(clinic_id: str, primary_patient_id: str) -> list[dict]:
    """Get saved family/dependent profiles for a patient."""
    try:
        result = (
            supabase.table("family_members")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("primary_patient_id", primary_patient_id)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting family members: {e}")
        return []


async def add_family_member(
    clinic_id: str,
    primary_patient_id: str,
    name: str,
    relation: Optional[str] = None,
    age: Optional[int] = None,
) -> dict:
    """Save a new family/dependent profile."""
    try:
        data = {
            "clinic_id": clinic_id,
            "primary_patient_id": primary_patient_id,
            "name": name,
            "relation": relation,
            "age": age,
        }
        result = supabase.table("family_members").insert(data).execute()
        return result.data[0]
    except Exception as e:
        logger.error(f"Error adding family member: {e}")
        raise
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_family_members_database.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/database.py tests/test_family_members_database.py
git commit -m "feat(db): add get_family_members/add_family_member helpers"
```

---

### Task 17: Bot flow — select saved family member or add new

**Files:**
- Modify: `app/services/conversation.py` (button dispatch, state dispatch, `for_family` handler, new handler methods, `_handle_collecting_name`)
- Test: `tests/test_family_member_booking_flow.py` (new)

**Interfaces:**
- Consumes: `get_family_members`, `add_family_member` from Task 16; `send_interactive_list(clinic, phone, body, button_text, sections, header=None)` (existing, `app/services/whatsapp.py:174`).
- Produces: two new conversation states, `selecting_family_member` and `confirming_save_family_member`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_family_member_booking_flow.py
"""Tests for the family-member selection/save booking flow."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_for_family_with_saved_members_shows_list():
    manager = ConversationManager()
    clinic = {"id": "clinic-1"}

    with patch(
        "app.services.conversation.get_family_members", new_callable=AsyncMock
    ) as mock_get, patch.object(
        manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as mock_send_list, patch.object(
        manager, "update_state", new_callable=AsyncMock
    ) as mock_update_state, patch(
        "app.services.conversation.get_patient_by_phone", new_callable=AsyncMock
    ) as mock_get_patient:
        mock_get_patient.return_value = {"id": "patient-1"}
        mock_get.return_value = [{"id": "fam-1", "name": "Priya Kumar", "relation": "spouse"}]

        await manager._handle_for_family_selected(clinic, "+919876543210", {}, "en")

        mock_send_list.assert_called_once()
        assert mock_update_state.call_args[0][2] == "selecting_family_member"


@pytest.mark.asyncio
async def test_for_family_with_no_saved_members_asks_name():
    manager = ConversationManager()
    clinic = {"id": "clinic-1"}

    with patch(
        "app.services.conversation.get_family_members", new_callable=AsyncMock
    ) as mock_get, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send_text, patch.object(
        manager, "update_state", new_callable=AsyncMock
    ) as mock_update_state, patch(
        "app.services.conversation.get_patient_by_phone", new_callable=AsyncMock
    ) as mock_get_patient:
        mock_get_patient.return_value = {"id": "patient-1"}
        mock_get.return_value = []

        await manager._handle_for_family_selected(clinic, "+919876543210", {}, "en")

        mock_send_text.assert_called_once()
        assert mock_update_state.call_args[0][2] == "collecting_name"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_family_member_booking_flow.py -v`
Expected: FAIL — `AttributeError: 'ConversationManager' object has no attribute '_handle_for_family_selected'`

- [ ] **Step 3: Add imports**

In `app/services/conversation.py`, add to the `app.database` import block:

```python
from app.database import get_family_members, add_family_member
```

- [ ] **Step 4: Extract the "For Family" branch into its own method**

Replace the existing inline `"family"`/`"for_family"` button handling (lines 333-341):

```python
            elif button_id in ["family", "for_family"]:
                lang = await get_lang(clinic, phone)
                ctx = session.get("context", {}) or {}
                ctx["for_self"] = False
                await update_conversation(clinic["id"], phone, {"context": ctx})
                await self.whatsapp.send_text(
                    clinic, phone, get_message("ask_name", lang)
                )
                await self.update_state(clinic, phone, "collecting_name", context)
                return
```

with:

```python
            elif button_id in ["family", "for_family"]:
                lang = await get_lang(clinic, phone)
                ctx = session.get("context", {}) or {}
                ctx["for_self"] = False
                await self._handle_for_family_selected(clinic, phone, ctx, lang)
                return
```

Add the new method near `_handle_collecting_name`:

```python
    async def _handle_for_family_selected(
        self, clinic: dict, phone: str, context: dict, lang: str
    ) -> None:
        """Patient chose 'For Family' — show saved dependents, or ask for a name if none saved."""
        patient = await get_patient_by_phone(clinic["id"], phone)
        members = await get_family_members(clinic["id"], patient["id"]) if patient else []

        if members:
            rows = [
                {"id": f"fam_{m['id']}", "title": m["name"][:24], "description": m.get("relation", "")[:72]}
                for m in members
            ]
            rows.append({"id": "fam_add_new", "title": "+ Add New Person", "description": ""})

            body = {
                "en": "Who is this appointment for?",
                "hi": "यह अपॉइंटमेंट किसके लिए है?",
                "te": "ఈ అపాయింట్‌మెంట్ ఎవరి కోసం?",
            }.get(lang, "Who is this appointment for?")

            await self.whatsapp.send_interactive_list(
                clinic, phone, body=body, button_text="Select", sections=[{"title": "Family", "rows": rows}]
            )
            await self.update_state(clinic, phone, "selecting_family_member", context)
        else:
            await self.whatsapp.send_text(clinic, phone, get_message("ask_name", lang))
            await self.update_state(clinic, phone, "collecting_name", context)
```

- [ ] **Step 5: Add the `selecting_family_member` state handler**

Add button parsing for `fam_*` IDs in the interactive-button block (near the other `startswith` checks, after the `slot_`/`date_` handling around line 366):

```python
            elif button_id == "fam_add_new":
                intent = "family_add_new"
            elif button_id.startswith("fam_"):
                intent = "select_family_member"
                message = button_id.replace("fam_", "")
```

Add the state dispatch entry (in the big `if state == ...` chain, alongside `selecting_department` around line 610-613):

```python
        elif state == "selecting_family_member":
            await self._handle_selecting_family_member(
                clinic, phone, message, intent, context, lang
            )
```

Add the handler method:

```python
    async def _handle_selecting_family_member(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
    ) -> None:
        """Patient picked a saved family member, or chose to add a new one."""
        if intent == "family_add_new":
            await self.whatsapp.send_text(clinic, phone, get_message("ask_name", lang))
            await self.update_state(clinic, phone, "collecting_name", context)
            return

        # message holds the family_members.id (button_id with "fam_" stripped)
        patient = await get_patient_by_phone(clinic["id"], phone)
        members = await get_family_members(clinic["id"], patient["id"]) if patient else []
        selected = next((m for m in members if m["id"] == message), None)

        if not selected:
            await self.whatsapp.send_text(clinic, phone, get_message("invalid_input", lang))
            return

        context["booking_name"] = selected["name"]
        await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
        await self.update_state(clinic, phone, "collecting_symptoms", context)
```

- [ ] **Step 6: Add the save-new-member prompt after typed-name entry**

In `_handle_collecting_name` (lines 1334-1343), change:

```python
        name = result
        context["booking_name"] = name

        # Save to patient record if for self
        if context.get("for_self", True):
            await update_patient(clinic["id"], phone, {"name": name})

        # Move to symptoms
        await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
        await self.update_state(clinic, phone, "collecting_symptoms", context)
```

to:

```python
        name = result
        context["booking_name"] = name

        # Save to patient record if for self
        if context.get("for_self", True):
            await update_patient(clinic["id"], phone, {"name": name})
            await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
            await self.update_state(clinic, phone, "collecting_symptoms", context)
            return

        # For a freshly-typed family member name, offer to save it for next time
        save_prompt = {
            "en": f"Save {name} for future bookings?",
            "hi": f"भविष्य की बुकिंग के लिए {name} को सहेजें?",
            "te": f"భవిష్యత్తు బుకింగ్‌ల కోసం {name}ని సేవ్ చేయాలా?",
        }.get(lang, f"Save {name} for future bookings?")

        await self.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=save_prompt,
            buttons=[
                {"id": "save_family_yes", "title": "Yes"},
                {"id": "save_family_no", "title": "No"},
            ],
        )
        await self.update_state(clinic, phone, "confirming_save_family_member", context)
```

- [ ] **Step 7: Add the `confirming_save_family_member` state handler**

Add button parsing (alongside the `fam_*` handling from Step 5):

```python
            elif button_id == "save_family_yes":
                intent = "save_family_yes"
            elif button_id == "save_family_no":
                intent = "save_family_no"
```

Add the state dispatch entry:

```python
        elif state == "confirming_save_family_member":
            await self._handle_confirming_save_family_member(
                clinic, phone, intent, context, patient, lang
            )
```

Add the handler method:

```python
    async def _handle_confirming_save_family_member(
        self,
        clinic: dict,
        phone: str,
        intent: str,
        context: dict,
        patient: dict,
        lang: str,
    ) -> None:
        """Save (or skip saving) the family member just typed in, then continue booking."""
        if intent == "save_family_yes" and patient:
            await add_family_member(clinic["id"], patient["id"], context["booking_name"])

        await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
        await self.update_state(clinic, phone, "collecting_symptoms", context)
```

- [ ] **Step 8: Run tests, verify they pass**

Run: `pytest tests/test_family_member_booking_flow.py -v`
Expected: PASS

- [ ] **Step 9: Run the full conversation-related test suite to confirm no regression**

Run: `pytest tests/test_conversation_payment_mode.py tests/test_webhook.py tests/test_appointment.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add app/services/conversation.py tests/test_family_member_booking_flow.py
git commit -m "feat: let patients select saved family members or save new ones during booking"
```

---

## Self-Review Summary

- **Spec coverage:** All 4 problem areas from the design doc are covered — bug fix (Task 1), doctor slot timings (Tasks 2-5), department list (Task 5), and all 4 selected new features: emergency staff alert (Task 6), post-discharge check-ins (Tasks 7-10), OPD queue (Tasks 11-14), family profiles (Tasks 15-17).
- **Type consistency:** `generate_slots(start: time, end: time, duration_minutes: int) -> list[str]` (Task 2) is the exact signature consumed in Task 4. `check_in_appointment`/`call_next_patient`/`get_patient_queue_status` (Task 12) exact names/signatures are consumed by Tasks 13 and 14. `get_family_members`/`add_family_member` (Task 16) exact names/signatures are consumed by Task 17.
- **No placeholders:** every step has runnable code; the one deliberate simplification (fixed 5-min average consult time for wait estimates) is marked with a `ponytail:` comment naming the upgrade path.
- **Data-integrity constraint respected:** `family_members` is a standalone table keyed by `primary_patient_id`, not a `patients` row, because `patients.phone` is UNIQUE (confirmed against live schema before writing Task 15).
- **Operational dependencies flagged, not silently assumed:** queue accuracy depends on reception discipline using Check In/Call Next in order (Task 13/14) — called out in the plan and the original spec, not hidden.
