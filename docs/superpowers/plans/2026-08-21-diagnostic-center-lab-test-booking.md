# Diagnostic Center Lab Test Booking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let diagnostics-only clinics (zero doctors) offer WhatsApp patients a lab-test catalog to browse and book, with Razorpay payment collected at booking time, plus an admin panel page to manage the test catalog (CRUD + CSV import) and the collection window — while doctor-consultation clinics are completely unaffected.

**Architecture:** Extend the existing `appointments` table with a `booking_type` discriminator (`'consultation'` | `'lab_test'`) instead of forking a new booking table, so lab-test bookings inherit `PaymentService`'s already-hardened HMAC webhook verification, idempotent confirmation, amount-mismatch quarantine, and refund logic unchanged. A new `lab_tests` catalog table backs the admin CRUD + CSV import. Two new `ConversationState` entries (`browsing_lab_tests`, `confirming_collection_date`) route diagnostics-only clinics straight to test booking, bypassing the doctor/department flow entirely — this also closes off a second path into the `_show_department_list`/`_show_doctor_list` recursion crash fixed earlier today, since diagnostics-only clinics can no longer reach that flow at all.

**Tech Stack:** Python FastAPI, Supabase PostgreSQL, Meta WhatsApp Cloud API, Razorpay Payment Links, pytest + unittest.mock.

**Spec:** `docs/superpowers/specs/2026-08-21-diagnostic-center-lab-test-booking-design.md`

## Global Constraints

- Reuse `appointments` + `payment_events`; do not create a new booking table or a parallel payment pipeline. Only `lab_tests` (a catalog table) is new.
- Razorpay payment is collected **at booking time**, not deferred to walk-in ("pay at center" was explicitly rejected).
- v1 is one test per booking (no multi-test cart). No per-test time slots — one shared daily collection window per branch (or per clinic for single-location clinics).
- `doctor_name`/`appointment_time` stay `NULL` for `booking_type='lab_test'` rows. Do not make `doctor_name NOT NULL` or otherwise "fix" this — the existing partial unique index `idx_unique_active_slot` (migration 008) treats distinct NULLs as non-colliding, which is exactly the wanted behavior (many patients can share a collection date).
- New feature flag `lab_test_booking` is granted only to the `diagstream` and `polyclinic` plans in `app/services/tenant.py::PLAN_FEATURES`.
- New permission `LAB_TESTS_MANAGE` is granted by default to the `DIAGNOSTIC_OPERATOR` and `LAB_OPERATOR` role presets in `app/services/permissions.py`.
- CSV import columns: `name,sample_type,price_rupees,turnaround_hours,fasting_required,prep_instructions`. Rows are upserted by `(clinic_id, name)`. A malformed row is reported back individually — it never aborts the rest of the import.
- Phone numbers in logs stay masked; webhook/API responses never leak stack traces (existing codebase rule, `CLAUDE.md`).
- `available_days`/collection-window `days` values use the existing comma-separated weekday convention already used by `DoctorCreate.available_days` (e.g. `"Mon,Tue,Wed,Thu,Fri,Sat"`), not the spec's illustrative `"Mon-Sat"` range string.

---

## File Structure

**Create:**
- `migrations/038_lab_tests_table.sql` — new `lab_tests` catalog table
- `migrations/039_appointments_lab_test_booking.sql` — `appointments.booking_type`/`lab_test_id`/`lab_test_name`, relaxed `appointment_time`
- `migrations/040_lab_reports_matched_booking.sql` — `lab_reports.matched_booking_id`
- `tests/test_lab_test_booking_payment.py` — `booking_type='lab_test'` payment creation + fee resolution + notification copy
- `tests/test_lab_test_booking_conversation.py` — diagnostics-only routing + list/date-picker/booking flow
- `tests/test_lab_tests_admin.py` — Lab Tests CRUD + CSV import endpoint tests

**Modify:**
- `app/services/permissions.py` — add `LAB_TESTS_MANAGE`
- `app/services/tenant.py` — add `lab_test_booking` to `PLAN_FEATURES`
- `app/database.py` — add `get_lab_tests`, `get_lab_test_by_id`, `get_lab_collection_window`
- `app/services/payment.py` — `booking_type` branching in `create_booking_with_payment`, new `_get_lab_test_fee_paise`, `_notify_payment_confirmed` lab-test copy
- `app/routers/admin.py` — `LabTestCreate`/`LabTestUpdate` models, Lab Tests CRUD, CSV import, collection-window endpoints
- `app/services/conversation.py` — two new `ConversationState` entries, dispatch wiring, diagnostics-only routing in `_start_booking`, three new handler methods
- `admin/index.html` — Lab Tests nav item, page section + form, CRUD JS, CSV import UI, collection-window form, staff permission checkboxes, `loaders` map entry

---

### Task 1: Migration 038 — `lab_tests` catalog table

**Files:**
- Create: `migrations/038_lab_tests_table.sql`

**Interfaces:**
- Produces: table `lab_tests(id, clinic_id, branch_id, name, sample_type, prep_instructions, fasting_required, price_paise, turnaround_hours, is_active, created_at, updated_at)`, consumed by Task 6's `get_lab_tests`/`get_lab_test_by_id` and Task 8's admin CRUD.

- [ ] **Step 1: Write the migration**

```sql
-- Migration 038: Lab test catalog table
--
-- Diagnostic centers configure their test menu here (name, sample type,
-- price, turnaround, prep instructions). branch_id is nullable — a NULL
-- branch_id means the test is offered clinic-wide across all branches.

CREATE TABLE IF NOT EXISTS lab_tests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    branch_id UUID REFERENCES branches(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    sample_type TEXT,
    prep_instructions TEXT,
    fasting_required BOOLEAN NOT NULL DEFAULT false,
    price_paise INTEGER NOT NULL CHECK (price_paise > 0),
    turnaround_hours INTEGER,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lab_tests_clinic_active ON lab_tests(clinic_id, is_active);
CREATE INDEX IF NOT EXISTS idx_lab_tests_branch ON lab_tests(branch_id) WHERE branch_id IS NOT NULL;
```

- [ ] **Step 2: Verify syntax**

Run: `psql "$SUPABASE_DB_URL" -f migrations/038_lab_tests_table.sql` against a scratch/staging Supabase project (never production directly). Expected: `CREATE TABLE`, `CREATE INDEX` x2, no errors.

- [ ] **Step 3: Commit**

```bash
git add migrations/038_lab_tests_table.sql
git commit -m "feat(db): add lab_tests catalog table"
```

---

### Task 2: Migration 039 — `appointments` lab-test booking columns

**Files:**
- Create: `migrations/039_appointments_lab_test_booking.sql`

**Interfaces:**
- Consumes: `lab_tests(id)` from Task 1 (FK target).
- Produces: `appointments.booking_type` (`'consultation'`|`'lab_test'`), `appointments.lab_test_id`, `appointments.lab_test_name`, `appointments.appointment_time` now nullable — consumed by Task 7 (`payment.py`) and Task 12 (`conversation.py`).

- [ ] **Step 1: Write the migration**

```sql
-- Migration 039: Lab-test booking support on appointments
--
-- Diagnostic centers book lab tests, not doctor consultations. Rather than
-- forking a second booking table + payment pipeline, appointments gains a
-- booking_type discriminator plus nullable lab-test columns. Every existing
-- payment code path (create_booking_with_payment, process_payment_webhook,
-- expire_stale_bookings, refunds, admin confirm/reject/cancel, daily
-- reconciliation) keeps operating on this one table unchanged for
-- booking_type='consultation' rows.
--
-- IMPORTANT — do not "fix" this: lab_test bookings always have
-- doctor_name = NULL. The partial unique index idx_unique_active_slot
-- (migration 008) is defined as
--   UNIQUE (clinic_id, doctor_name, appointment_date, appointment_time)
--   WHERE status IN ('pending_payment', 'confirmed')
-- Postgres treats every NULL as distinct from every other NULL in a unique
-- index, so this constraint silently does not restrict lab_test rows —
-- which is exactly the desired behavior (many patients can share a
-- collection date). Making doctor_name NOT NULL would break lab-test
-- bookings; it is not a bug to be fixed.

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS booking_type TEXT NOT NULL DEFAULT 'consultation',
    ADD COLUMN IF NOT EXISTS lab_test_id UUID REFERENCES lab_tests(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS lab_test_name TEXT;

ALTER TABLE appointments
    DROP CONSTRAINT IF EXISTS appointments_booking_type_check;
ALTER TABLE appointments
    ADD CONSTRAINT appointments_booking_type_check
    CHECK (booking_type IN ('consultation', 'lab_test'));

-- appointment_time was NOT NULL — lab_test bookings only record a
-- collection date (appointment_date); the collection window itself is a
-- branch/clinic-level setting (branches.config / clinics.config), not
-- stored per-booking.
ALTER TABLE appointments
    ALTER COLUMN appointment_time DROP NOT NULL;

ALTER TABLE appointments
    DROP CONSTRAINT IF EXISTS appointments_time_required_for_consultation;
ALTER TABLE appointments
    ADD CONSTRAINT appointments_time_required_for_consultation
    CHECK (
        (booking_type = 'consultation' AND appointment_time IS NOT NULL)
        OR booking_type = 'lab_test'
    );

CREATE INDEX IF NOT EXISTS idx_appointments_booking_type ON appointments(clinic_id, booking_type);
```

- [ ] **Step 2: Verify syntax**

Run: `psql "$SUPABASE_DB_URL" -f migrations/039_appointments_lab_test_booking.sql` against the same scratch/staging project used in Task 1 (run after Task 1's migration, since this one FK-references `lab_tests`). Expected: `ALTER TABLE` x2, `CREATE INDEX`, no errors. Then sanity-check the existing doctor-booking constraint is untouched: `\d appointments` should still show `appointments_time_required_for_consultation` and the pre-existing `idx_unique_active_slot`.

- [ ] **Step 3: Commit**

```bash
git add migrations/039_appointments_lab_test_booking.sql
git commit -m "feat(db): add booking_type discriminator to appointments for lab-test bookings"
```

---

### Task 3: Migration 040 — `lab_reports.matched_booking_id`

**Files:**
- Create: `migrations/040_lab_reports_matched_booking.sql`

**Interfaces:**
- Consumes: `appointments(id)` (pre-existing).
- Produces: `lab_reports.matched_booking_id` — best-effort link from an incoming report to the lab-test booking it fulfills. Not consumed by any other task in this plan (report-arrival auto-linking is out of scope for this iteration's WhatsApp/admin flows); this column exists so a future iteration can populate it without another migration.

- [ ] **Step 1: Write the migration**

```sql
-- Migration 040: Link lab reports to the booking they fulfill
--
-- Best-effort link from an incoming report to the open lab-test booking it
-- fulfills. This is additive to PatientMatchService's existing safety gate,
-- not a replacement — delivery safety still depends solely on the existing
-- phone+name match logic in patient_match.py, unchanged.

ALTER TABLE lab_reports
    ADD COLUMN IF NOT EXISTS matched_booking_id UUID REFERENCES appointments(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_lab_reports_matched_booking ON lab_reports(matched_booking_id) WHERE matched_booking_id IS NOT NULL;
```

- [ ] **Step 2: Verify syntax**

Run: `psql "$SUPABASE_DB_URL" -f migrations/040_lab_reports_matched_booking.sql` against the scratch/staging project. Expected: `ALTER TABLE`, `CREATE INDEX`, no errors.

- [ ] **Step 3: Commit**

```bash
git add migrations/040_lab_reports_matched_booking.sql
git commit -m "feat(db): add lab_reports.matched_booking_id for future report-to-booking linking"
```

---

### Task 4: `LAB_TESTS_MANAGE` permission

**Files:**
- Modify: `app/services/permissions.py:13-28` (`PERMISSIONS`), `app/services/permissions.py:52-69` (`_DIAGNOSTIC_OPERATOR_GRANTS`, `ROLE_PRESETS["LAB_OPERATOR"]`)
- Test: `tests/test_lab_tests_admin.py` (new file; permission-resolution assertions live alongside the admin endpoint tests written in Task 8)

**Interfaces:**
- Produces: permission string `"LAB_TESTS_MANAGE"`, usable by `require_permission("LAB_TESTS_MANAGE")` in Task 8/9/10.

- [ ] **Step 1: Write the failing test**

Create `tests/test_lab_tests_admin.py`:

```python
"""Tests for Lab Tests admin CRUD, CSV import, and permission wiring."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("WABA_DISPLAY_NAME", "Test Hospital")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("HOSPITAL_NAME", "City Care Hospital")
os.environ.setdefault("HOSPITAL_EMERGENCY_NUMBER", "108")
os.environ.setdefault("HOSPITAL_PHONE", "+919876543210")
os.environ.setdefault("HOSPITAL_MAPS_LINK", "https://maps.google.com")
os.environ.setdefault("HOSPITAL_WEBSITE", "https://test.hospital.com")
os.environ.setdefault("HOSPITAL_PRIVACY_POLICY_URL", "https://test.hospital.com/privacy")
os.environ.setdefault("HOSPITAL_ADDRESS", "Test Address")
os.environ.setdefault("HOSPITAL_LANDMARK", "Test Landmark")
os.environ.setdefault("BOOKING_REF_PREFIX", "MC")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

mock_supabase = MagicMock()
mock_db_module = MagicMock()
mock_db_module.supabase = mock_supabase
sys.modules["app.database"] = mock_db_module


class TestLabTestsManagePermission:
    def test_permission_registered(self):
        from app.services.permissions import PERMISSIONS

        assert "LAB_TESTS_MANAGE" in PERMISSIONS

    def test_diagnostic_operator_granted_by_default(self):
        from app.services.permissions import ROLE_PRESETS

        assert "LAB_TESTS_MANAGE" in ROLE_PRESETS["DIAGNOSTIC_OPERATOR"]

    def test_lab_operator_granted_by_default(self):
        from app.services.permissions import ROLE_PRESETS

        assert "LAB_TESTS_MANAGE" in ROLE_PRESETS["LAB_OPERATOR"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_tests_admin.py -v`
Expected: FAIL — `"LAB_TESTS_MANAGE" in PERMISSIONS` is `False`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/permissions.py`, change the `PERMISSIONS` frozenset (lines 13-28):

```python
PERMISSIONS = frozenset({
    "DOCTORS_CREATE",
    "DOCTORS_UPDATE",
    "DOCTORS_DELETE",
    "DOCTOR_BRANCH_ASSIGN",
    "DOCTOR_LEAVES_CREATE",
    "DOCTOR_LEAVES_DELETE",
    "HOLIDAYS_CREATE",
    "HOLIDAYS_DELETE",
    "STAFF_VIEW",
    "STAFF_CREATE",
    "STAFF_UPDATE",
    "REPORTS_VIEW",
    "REPORTS_RESOLVE",
    "CONNECTOR_MANAGE",
    "LAB_TESTS_MANAGE",
})
```

Change `_DIAGNOSTIC_OPERATOR_GRANTS` (lines 52-56):

```python
_DIAGNOSTIC_OPERATOR_GRANTS = [
    "REPORTS_VIEW",
    "REPORTS_RESOLVE",
    "CONNECTOR_MANAGE",
    "LAB_TESTS_MANAGE",
]
```

Change `ROLE_PRESETS["LAB_OPERATOR"]` (line 63):

```python
    "LAB_OPERATOR": ["REPORTS_VIEW", "REPORTS_RESOLVE", "LAB_TESTS_MANAGE"],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_tests_admin.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/permissions.py tests/test_lab_tests_admin.py
git commit -m "feat(permissions): add LAB_TESTS_MANAGE permission for diagnostic/lab operators"
```

---

### Task 5: `lab_test_booking` feature flag

**Files:**
- Modify: `app/services/tenant.py:212-223` (`PLAN_FEATURES["diagstream"]`), `app/services/tenant.py:243-263` (`PLAN_FEATURES["polyclinic"]`)
- Test: `tests/test_lab_tests_admin.py` (append)

**Interfaces:**
- Produces: feature string `"lab_test_booking"`, checked via `has_feature(clinic, "lab_test_booking")` in Task 12.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lab_tests_admin.py`:

```python
class TestLabTestBookingFeatureFlag:
    def test_diagstream_has_lab_test_booking(self):
        from app.services.tenant import PLAN_FEATURES

        assert "lab_test_booking" in PLAN_FEATURES["diagstream"]

    def test_polyclinic_has_lab_test_booking(self):
        from app.services.tenant import PLAN_FEATURES

        assert "lab_test_booking" in PLAN_FEATURES["polyclinic"]

    def test_soloclinic_does_not_have_lab_test_booking(self):
        from app.services.tenant import PLAN_FEATURES

        assert "lab_test_booking" not in PLAN_FEATURES["soloclinic"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_tests_admin.py::TestLabTestBookingFeatureFlag -v`
Expected: FAIL — `"lab_test_booking"` not in `PLAN_FEATURES["diagstream"]`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/tenant.py`, update the plan comment block (lines 190-197):

```python
# ─── Plan Feature Registry ───────────────────────────────────────────────────
#
# Plans:
#   soloclinic  — Solo doctor / small clinic (booking + payments only)
#   diagstream  — Diagnostics / lab-only centres (lab reports + lab-test
#                 booking; no *doctor* booking)
#   essential   — Full-service hospital (everything except enterprise wildcard)
#   polyclinic  — Multi-branch hospital / polyclinic + diagnostics (essential + multi_branch)
#   enterprise  — Unlimited (all current + future features via wildcard)
```

Update `PLAN_FEATURES["diagstream"]` (lines 212-223):

```python
    "diagstream": {
        "multilingual",
        "emergency_escalation",
        "clinical_firewall",
        "compliance_dpdp",
        "compliance_nmc",
        "lab_reports",
        "diagnostic_reports",
        "ai_report_summary",
        "pii_sanitization",
        "multi_branch",  # Diagnostic centers can also run multiple branches
        "lab_test_booking",
    },
```

Update `PLAN_FEATURES["polyclinic"]` (lines 243-263):

```python
    "polyclinic": {
        "booking",
        "reminders",
        "multilingual",
        "emergency_escalation",
        "clinical_firewall",
        "admin_dashboard",
        "roster_management",
        "compliance_dpdp",
        "compliance_nmc",
        "lab_reports",
        "diagnostic_reports",
        "ai_report_summary",
        "pii_sanitization",
        "feedback",
        "analytics",
        "multi_department",
        "payments_razorpay",
        "staff_training",
        "multi_branch",  # Multi-branch support
        "lab_test_booking",
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_tests_admin.py::TestLabTestBookingFeatureFlag -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/tenant.py tests/test_lab_tests_admin.py
git commit -m "feat(tenant): grant lab_test_booking to diagstream and polyclinic plans"
```

---

### Task 6: `database.py` lab test helpers

**Files:**
- Modify: `app/database.py` (add new functions near `get_doctors`, `app/database.py:163-193`)
- Test: `tests/test_lab_tests_admin.py` (append)

**Interfaces:**
- Consumes: `lab_tests` table (Task 1), `branches.config`/`clinics.config` JSONB (pre-existing columns).
- Produces:
  - `async def get_lab_tests(clinic_id: str, branch_id: Optional[str] = None, active_only: bool = True) -> list`
  - `async def get_lab_test_by_id(clinic_id: str, lab_test_id: str) -> Optional[dict]`
  - `async def get_lab_collection_window(clinic: dict, branch_id: Optional[str] = None) -> dict` (returns `{"start": str, "end": str, "days": str}`)
  These three are consumed by Task 7 (fee lookup is separate, in `payment.py`), Task 12/13 (`conversation.py`), and Task 8 (`admin.py` reads `lab_tests` directly via `supabase`, not through these helpers, matching the existing Doctors admin pattern).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lab_tests_admin.py`:

```python
import asyncio


class TestGetLabTests:
    def test_get_lab_tests_filters_by_clinic_and_active(self):
        from app.database import get_lab_tests, supabase

        mock_result = MagicMock()
        mock_result.data = [
            {"id": "t1", "name": "CBC", "price_paise": 50000, "is_active": True}
        ]
        supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value = mock_result

        result = asyncio.get_event_loop().run_until_complete(
            get_lab_tests("clinic-1")
        )
        assert result == mock_result.data

    def test_get_lab_test_by_id_returns_none_when_missing(self):
        from app.database import get_lab_test_by_id, supabase

        mock_result = MagicMock()
        mock_result.data = []
        supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = mock_result

        result = asyncio.get_event_loop().run_until_complete(
            get_lab_test_by_id("clinic-1", "missing-id")
        )
        assert result is None

    def test_get_lab_collection_window_falls_back_to_clinic_config(self):
        from app.database import get_lab_collection_window

        clinic = {"config": {"lab_collection": {"start": "08:00", "end": "12:00", "days": "Mon,Wed,Fri"}}}
        result = asyncio.get_event_loop().run_until_complete(
            get_lab_collection_window(clinic, branch_id=None)
        )
        assert result == {"start": "08:00", "end": "12:00", "days": "Mon,Wed,Fri"}

    def test_get_lab_collection_window_returns_default_when_unset(self):
        from app.database import get_lab_collection_window

        clinic = {"config": {}}
        result = asyncio.get_event_loop().run_until_complete(
            get_lab_collection_window(clinic, branch_id=None)
        )
        assert result["start"] and result["end"] and result["days"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_tests_admin.py::TestGetLabTests -v`
Expected: FAIL — `ImportError: cannot import name 'get_lab_tests' from 'app.database'`.

- [ ] **Step 3: Write minimal implementation**

In `app/database.py`, add immediately after `get_doctors` (after line 192, before `get_doctors_at_branch` at line 195):

```python
async def get_lab_tests(
    clinic_id: str, branch_id: Optional[str] = None, active_only: bool = True
) -> list:
    """Get a clinic's lab test catalog, optionally filtered by branch.

    A test with branch_id=NULL is clinic-wide and is included regardless of
    the branch_id filter (mirrors the catalog's "unset = all branches" rule).
    """
    try:
        query = supabase.table("lab_tests").select("*").eq("clinic_id", clinic_id)
        if active_only:
            query = query.eq("is_active", True)
        result = query.order("name").execute()
        tests = result.data or []
        if branch_id:
            tests = [
                t for t in tests if not t.get("branch_id") or t["branch_id"] == branch_id
            ]
        return tests
    except Exception as e:
        logger.error(f"Error getting lab tests: {e}")
        return []


async def get_lab_test_by_id(clinic_id: str, lab_test_id: str) -> Optional[dict]:
    """Get a single active lab test by id, scoped to the clinic."""
    try:
        result = (
            supabase.table("lab_tests")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("id", lab_test_id)
            .eq("is_active", True)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error getting lab test {lab_test_id}: {e}")
        return None


async def get_lab_collection_window(clinic: dict, branch_id: Optional[str] = None) -> dict:
    """Resolve the sample collection window for a booking.

    Branch-level config takes priority; falls back to clinic-level config
    for single-location clinics; falls back to a hardcoded default if
    neither is configured.
    """
    default = {"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"}
    try:
        if branch_id:
            result = (
                supabase.table("branches").select("config").eq("id", branch_id).execute()
            )
            if result.data:
                window = (result.data[0].get("config") or {}).get("lab_collection")
                if window:
                    return window
        window = (clinic.get("config") or {}).get("lab_collection")
        return window or default
    except Exception as e:
        logger.error(f"Error getting lab collection window: {e}")
        return default
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_tests_admin.py::TestGetLabTests -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/database.py tests/test_lab_tests_admin.py
git commit -m "feat(db): add get_lab_tests/get_lab_test_by_id/get_lab_collection_window helpers"
```

---

### Task 7: `payment.py` — `booking_type` support

**Files:**
- Modify: `app/services/payment.py:98-244` (`create_booking_with_payment`), `app/services/payment.py:998-1016` (add `_get_lab_test_fee_paise` after `_get_doctor_fee_paise`), `app/services/payment.py:1260-1319` (`_notify_payment_confirmed`)
- Test: `tests/test_lab_test_booking_payment.py` (new file)

**Interfaces:**
- Consumes: `lab_tests.price_paise` (Task 1), `booking_type`/`lab_test_id`/`lab_test_name` columns on `appointments` (Task 2).
- Produces: `create_booking_with_payment(..., booking_type: str = "consultation", lab_test_id: Optional[str] = None, lab_test_name: Optional[str] = None)` — same return shape as today (`success`, `booking_id`, `booking_ref`, `razorpay_payment_link_id`, `payment_link`, `amount_paise`, `hold_expires_at`, `reason`). Consumed by Task 13 (`conversation.py`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_lab_test_booking_payment.py`:

```python
"""Tests for booking_type='lab_test' support in PaymentService."""

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("WABA_DISPLAY_NAME", "Test Hospital")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("HOSPITAL_NAME", "City Care Hospital")
os.environ.setdefault("HOSPITAL_EMERGENCY_NUMBER", "108")
os.environ.setdefault("HOSPITAL_PHONE", "+919876543210")
os.environ.setdefault("HOSPITAL_MAPS_LINK", "https://maps.google.com")
os.environ.setdefault("HOSPITAL_WEBSITE", "https://test.hospital.com")
os.environ.setdefault("HOSPITAL_PRIVACY_POLICY_URL", "https://test.hospital.com/privacy")
os.environ.setdefault("HOSPITAL_ADDRESS", "Test Address")
os.environ.setdefault("HOSPITAL_LANDMARK", "Test Landmark")
os.environ.setdefault("BOOKING_REF_PREFIX", "MC")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_key123")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "rzp_test_secret456")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret_789")
os.environ.setdefault("BOOKING_FEE_PAISE", "50000")
os.environ.setdefault("BOOKING_HOLD_MINUTES", "10")
os.environ.setdefault("REFUND_WINDOW_HOURS", "4")

mock_supabase = MagicMock()
mock_db_module = MagicMock()
mock_db_module.supabase = mock_supabase
sys.modules["app.database"] = mock_db_module


class TestLabTestBookingCreation:
    @pytest.mark.asyncio
    async def test_lab_test_booking_uses_lab_test_fee_not_doctor_fee(self):
        """booking_type='lab_test' must price from lab_tests.price_paise, not doctors.consultation_fee."""
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_booking = {"id": "lab-booking-uuid", "booking_ref": "MC-2026-9001"}
        mock_link = {"id": "plink_lab_test", "short_url": "https://rzp.io/i/labtest1"}

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_lab_test_fee_paise", new_callable=AsyncMock, return_value=80000
        ) as mock_fee, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ) as mock_doctor_fee, patch.object(
            service, "_create_payment_link", new_callable=AsyncMock, return_value=mock_link
        ), patch.object(service, "_log_payment_event"):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(data=[mock_booking])
            mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="Lab Test",
                doctor_name=None,
                appointment_date="2026-07-05",
                appointment_time=None,
                booking_type="lab_test",
                lab_test_id="test-uuid-1",
                lab_test_name="Complete Blood Count",
            )

        assert result["success"] is True
        assert result["amount_paise"] == 80000
        mock_fee.assert_called_once_with("test-clinic", "test-uuid-1")
        mock_doctor_fee.assert_not_called()

    @pytest.mark.asyncio
    async def test_lab_test_booking_data_includes_lab_test_fields(self):
        """The inserted row must carry booking_type/lab_test_id/lab_test_name."""
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_booking = {"id": "lab-booking-uuid-2", "booking_ref": "MC-2026-9002"}
        mock_link = {"id": "plink_lab_test2", "short_url": "https://rzp.io/i/labtest2"}

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_lab_test_fee_paise", new_callable=AsyncMock, return_value=30000
        ), patch.object(
            service, "_create_payment_link", new_callable=AsyncMock, return_value=mock_link
        ), patch.object(service, "_log_payment_event"):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(data=[mock_booking])
            mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

            await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="Lab Test",
                doctor_name=None,
                appointment_date="2026-07-05",
                appointment_time=None,
                booking_type="lab_test",
                lab_test_id="test-uuid-2",
                lab_test_name="Lipid Profile",
            )

            inserted = mock_table.insert.call_args[0][0]
            assert inserted["booking_type"] == "lab_test"
            assert inserted["lab_test_id"] == "test-uuid-2"
            assert inserted["lab_test_name"] == "Lipid Profile"
            assert inserted["doctor_name"] is None
            assert inserted["appointment_time"] is None

    @pytest.mark.asyncio
    async def test_consultation_booking_still_prices_from_doctor_fee(self):
        """Regression: omitting booking_type must keep pricing from the doctor's fee."""
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_booking = {"id": "consult-booking-uuid", "booking_ref": "MC-2026-9003"}
        mock_link = {"id": "plink_consult", "short_url": "https://rzp.io/i/consult1"}

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ) as mock_doctor_fee, patch.object(
            service, "_get_lab_test_fee_paise", new_callable=AsyncMock
        ) as mock_lab_fee, patch.object(
            service, "_create_payment_link", new_callable=AsyncMock, return_value=mock_link
        ), patch.object(service, "_log_payment_event"):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(data=[mock_booking])
            mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="General Medicine",
                doctor_name="Dr. Test",
                appointment_date="2026-07-05",
                appointment_time="10:00",
            )

        assert result["amount_paise"] == 50000
        mock_doctor_fee.assert_called_once()
        mock_lab_fee.assert_not_called()


class TestNotifyPaymentConfirmedLabTestCopy:
    @pytest.mark.asyncio
    async def test_lab_test_booking_gets_test_copy_not_doctor_copy(self):
        from app.services.payment import PaymentService

        service = PaymentService()
        booking = {
            "clinic_id": "test-clinic",
            "patient_phone": "+919876543210",
            "booking_ref": "MC-2026-9001",
            "booking_type": "lab_test",
            "lab_test_name": "Complete Blood Count",
            "appointment_date": "2026-07-05",
            "amount_paise": 80000,
            "branch_id": None,
        }

        with patch("app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock) as mock_send, patch(
            "app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value={"name": "Accumax Diagnostics", "config": {}}
        ):
            await service._notify_payment_confirmed(booking)

        sent_text = mock_send.call_args[0][2]
        assert "Complete Blood Count" in sent_text
        assert "Doctor" not in sent_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_test_booking_payment.py -v`
Expected: FAIL — `create_booking_with_payment()` raises `TypeError: unexpected keyword argument 'booking_type'`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/payment.py`, change the `create_booking_with_payment` signature (lines 98-113):

```python
    async def create_booking_with_payment(
        self,
        clinic_id: str,
        patient_phone: str,
        patient_name: str,
        department: str,
        doctor_name: Optional[str],
        appointment_date: str,
        appointment_time: Optional[str],
        symptoms: str = "",
        patient_id: Optional[str] = None,
        clinic: Optional[dict] = None,
        branch_id: Optional[str] = None,
        branch_name: Optional[str] = None,
        deposit_percent: int = 100,
        booking_type: str = "consultation",
        lab_test_id: Optional[str] = None,
        lab_test_name: Optional[str] = None,
    ) -> dict:
```

Replace the fee-resolution block (lines 132-135):

```python
        # ── Determine fee based on booking type ──
        if booking_type == "lab_test":
            amount_paise = await self._get_lab_test_fee_paise(clinic_id, lab_test_id)
        else:
            amount_paise = await self._get_doctor_fee_paise(clinic_id, doctor_name)
        if deposit_percent < 100:
            amount_paise = round(amount_paise * deposit_percent / 100)
```

Replace `booking_data` (lines 151-165):

```python
        booking_data = {
            "clinic_id": clinic_id,
            "patient_id": patient_id,
            "patient_phone": patient_phone,
            "patient_name": patient_name,
            "department": department,
            "doctor_name": doctor_name,
            "appointment_date": appointment_date,
            "appointment_time": appointment_time,
            "symptoms": symptoms,
            "status": "pending_payment",
            "amount_paise": amount_paise,
            "hold_expires_at": hold_expires_at,
            "booking_ref": booking_ref,
            "booking_type": booking_type,
        }
        if booking_type == "lab_test":
            booking_data["lab_test_id"] = lab_test_id
            booking_data["lab_test_name"] = lab_test_name
```

Add `_get_lab_test_fee_paise` immediately after `_get_doctor_fee_paise` (after line 1016, before `_create_razorpay_order`):

```python
    async def _get_lab_test_fee_paise(self, clinic_id: str, lab_test_id: str) -> int:
        """Get a lab test's price in paise directly from the catalog."""
        try:
            result = (
                supabase.table("lab_tests")
                .select("price_paise")
                .eq("clinic_id", clinic_id)
                .eq("id", lab_test_id)
                .execute()
            )
            if result.data and result.data[0].get("price_paise"):
                return int(result.data[0]["price_paise"])
        except Exception as e:
            logger.error(f"Error fetching lab test fee: {e}")

        return settings.booking_fee_paise
```

In `_notify_payment_confirmed` (lines 1260-1312), replace the `msg = (...)` assignment (lines 1280-1291) with a `booking_type` branch:

```python
            amount_rupees = booking.get("amount_paise", 0) / 100

            if booking.get("booking_type") == "lab_test":
                msg = (
                    f"✅ *Payment Confirmed — Test Booked!*\n\n"
                    f"📋 *Booking Ref:* {booking.get('booking_ref', 'N/A')}\n"
                    f"🧪 *Test:* {booking.get('lab_test_name', 'N/A')}\n"
                    f"📅 *Collection Date:* {date_display}\n"
                    f"💰 *Paid:* ₹{amount_rupees:.0f}\n\n"
                    f"📌 Please arrive during our sample collection hours with a valid ID.\n\n"
                    f"_Cancellation with full refund available up to {settings.refund_window_hours} hours before your collection date._"
                )
            else:
                msg = (
                    f"✅ *Payment Confirmed — Appointment Booked!*\n\n"
                    f"📋 *Booking Ref:* {booking.get('booking_ref', 'N/A')}\n"
                    f"👨‍⚕️ *Doctor:* {booking.get('doctor_name', 'N/A')}\n"
                    f"🏥 *Department:* {booking.get('department', 'N/A')}\n"
                    f"📅 *Date:* {date_display}\n"
                    f"🕐 *Time:* {booking.get('appointment_time', 'N/A')}\n"
                    f"💰 *Paid:* ₹{amount_rupees:.0f}\n\n"
                    f"📌 Please arrive 15 minutes early with any relevant medical records.\n\n"
                    f"_Cancellation with full refund available up to {settings.refund_window_hours} hours before your appointment. "
                    f"No-show bookings are non-refundable._"
                )
```

(The `date_display` computation above this block and the branch-address-append logic below it are unchanged and apply to both booking types.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_test_booking_payment.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full existing payment suite to confirm no regression**

Run: `pytest tests/test_payment.py -v`
Expected: PASS (all pre-existing tests, unchanged — `doctor_name`/`appointment_time` remain required-by-keyword so every existing call site is unaffected)

- [ ] **Step 6: Commit**

```bash
git add app/services/payment.py tests/test_lab_test_booking_payment.py
git commit -m "feat(payment): support booking_type='lab_test' in create_booking_with_payment"
```

---

### Task 8: `admin.py` — Lab Tests CRUD endpoints

**Files:**
- Modify: `app/routers/admin.py` (add `LabTestCreate`/`LabTestUpdate` models near other `*Create`/`*Update` models, add endpoints near the Doctors CRUD block, i.e. after `app/routers/admin.py:1226` where `delete_doctor` starts)
- Test: `tests/test_lab_tests_admin.py` (append)

**Interfaces:**
- Consumes: `lab_tests` table (Task 1), `AdminUser`, `require_permission("LAB_TESTS_MANAGE")` (Task 4), `resolve_clinic_id_for_write`, `enforce_clinic_access`, `enforce_branch_scope`, `log_admin_action`, `_friendly_db_error` (all pre-existing in `admin.py`).
- Produces: `GET/POST /admin/lab-tests`, `PUT/DELETE /admin/lab-tests/{test_id}` — consumed by Task 15/16 (`admin/index.html`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lab_tests_admin.py`:

```python
from fastapi.testclient import TestClient


def _make_admin_user(permissions=None):
    from app.routers.admin import AdminUser

    user = AdminUser("staff-user")
    user.username = "labstaff"
    user.role = "staff"
    user.clinic_id = "clinic-1"
    user.user_id = "user-1"
    user.permissions = permissions or ["LAB_TESTS_MANAGE"]
    user.branch_id = None
    return user


class TestLabTestsCrudEndpoints:
    def test_create_lab_test_computes_price_paise_from_rupees(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.services.permissions import require_permission
        app.dependency_overrides[require_permission("LAB_TESTS_MANAGE")] = fake_user

        mock_new_test = {"id": "new-test-id", "name": "CBC", "price_paise": 50000}
        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[mock_new_test]
            )
            client = TestClient(app)
            resp = client.post(
                "/admin/lab-tests",
                json={"name": "CBC", "price_rupees": 500},
            )

        assert resp.status_code == 200
        insert_call = mock_sb.table.return_value.insert.call_args[0][0]
        assert insert_call["price_paise"] == 50000
        assert "price_rupees" not in insert_call
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_tests_admin.py::TestLabTestsCrudEndpoints -v`
Expected: FAIL — `404 Not Found` (no `/admin/lab-tests` route registered yet).

- [ ] **Step 3: Write minimal implementation**

In `app/routers/admin.py`, add the Pydantic models near the other `*Create`/`*Update` models (locate the existing `DoctorCreate`/`DoctorUpdate` class definitions and add these immediately after `DoctorUpdate`):

```python
class LabTestCreate(BaseModel):
    name: str
    sample_type: Optional[str] = None
    prep_instructions: Optional[str] = None
    fasting_required: bool = False
    price_rupees: int
    turnaround_hours: Optional[int] = None
    is_active: bool = True
    branch_id: Optional[str] = None

    @field_validator("price_rupees")
    @classmethod
    def validate_price(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("price_rupees must be greater than 0")
        return v


class LabTestUpdate(BaseModel):
    name: Optional[str] = None
    sample_type: Optional[str] = None
    prep_instructions: Optional[str] = None
    fasting_required: Optional[bool] = None
    price_rupees: Optional[int] = None
    turnaround_hours: Optional[int] = None
    is_active: Optional[bool] = None
    branch_id: Optional[str] = None
```

Add the endpoints after `delete_doctor` (i.e., after the Doctors CRUD block ends, before the Leaves endpoints begin):

```python
@router.get("/lab-tests")
async def get_lab_tests_admin(
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Get the clinic's lab test catalog."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("lab_tests").select("*")
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        if branch_id:
            query = query.eq("branch_id", branch_id)
        result = query.order("name").execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error fetching lab tests for clinic_id={effective_clinic_id}: {e}")
        raise HTTPException(status_code=500, detail=_friendly_db_error(e, "Failed to fetch lab tests"))


@router.post("/lab-tests")
async def create_lab_test(
    test: LabTestCreate,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("LAB_TESTS_MANAGE")),
):
    """Create a new lab test catalog entry."""
    effective_clinic_id = None
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)

        if test.branch_id:
            enforce_branch_scope(user, test.branch_id)
            branch_check = (
                supabase.table("branches")
                .select("id")
                .eq("id", test.branch_id)
                .eq("clinic_id", effective_clinic_id)
                .execute()
            )
            if not branch_check.data:
                raise HTTPException(
                    status_code=400, detail="Selected branch does not belong to your clinic."
                )

        try:
            test_data = test.model_dump(exclude={"price_rupees"})
        except AttributeError:
            test_data = test.dict(exclude={"price_rupees"})
        test_data["price_paise"] = test.price_rupees * 100
        test_data["clinic_id"] = effective_clinic_id

        result = supabase.table("lab_tests").insert(test_data).execute()
        new_test = result.data[0]

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="create_lab_test",
            resource_type="lab_test",
            resource_id=new_test["id"],
            details={"name": new_test.get("name")},
            ip_address=client_ip,
        )
        return new_test
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error creating lab test for clinic_id={effective_clinic_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail=_friendly_db_error(e, "Failed to create lab test")
        )


@router.put("/lab-tests/{test_id}")
async def update_lab_test(
    test_id: str,
    test: LabTestUpdate,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("LAB_TESTS_MANAGE")),
):
    """Update an existing lab test catalog entry."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        if test.branch_id:
            enforce_branch_scope(user, test.branch_id)

        try:
            update_data = test.model_dump(exclude_unset=True, exclude={"price_rupees"})
        except AttributeError:
            update_data = test.dict(exclude_unset=True, exclude={"price_rupees"})
        if test.price_rupees is not None:
            update_data["price_paise"] = test.price_rupees * 100
        if not update_data:
            return {"message": "No fields to update"}

        query = supabase.table("lab_tests").update(update_data)
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        result = query.eq("id", test_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Lab test not found")

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="update_lab_test",
            resource_type="lab_test",
            resource_id=test_id,
            details={"updated_fields": list(update_data.keys())},
            ip_address=client_ip,
        )
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating lab test {test_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=_friendly_db_error(e, "Failed to update lab test")
        )


@router.delete("/lab-tests/{test_id}")
async def delete_lab_test(
    test_id: str,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("LAB_TESTS_MANAGE")),
):
    """Delete a lab test catalog entry."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("lab_tests").delete()
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        result = query.eq("id", test_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Lab test not found")

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="delete_lab_test",
            resource_type="lab_test",
            resource_id=test_id,
            details={},
            ip_address=client_ip,
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting lab test {test_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=_friendly_db_error(e, "Failed to delete lab test")
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_tests_admin.py::TestLabTestsCrudEndpoints -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/routers/admin.py tests/test_lab_tests_admin.py
git commit -m "feat(admin): add Lab Tests CRUD endpoints"
```

---

### Task 9: `admin.py` — CSV import endpoint

**Files:**
- Modify: `app/routers/admin.py` (add endpoint immediately after `delete_lab_test` from Task 8)
- Test: `tests/test_lab_tests_admin.py` (append)

**Interfaces:**
- Consumes: `lab_tests` table (Task 1), `LAB_TESTS_MANAGE` permission (Task 4).
- Produces: `POST /admin/lab-tests/import-csv` returning `{"created": int, "updated": int, "errors": list[str]}` — consumed by Task 16 (`admin/index.html`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lab_tests_admin.py`:

```python
import io


class TestLabTestsCsvImport:
    def test_valid_rows_are_created(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.services.permissions import require_permission
        app.dependency_overrides[require_permission("LAB_TESTS_MANAGE")] = fake_user

        csv_content = (
            "name,sample_type,price_rupees,turnaround_hours,fasting_required,prep_instructions\n"
            "CBC,Blood,500,24,false,None required\n"
            "Fasting Sugar,Blood,300,12,true,8 hour fast required\n"
        )

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )
            mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "new-id"}]
            )
            client = TestClient(app)
            resp = client.post(
                "/admin/lab-tests/import-csv",
                files={"file": ("tests.csv", io.BytesIO(csv_content.encode()), "text/csv")},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] == 2
        assert body["errors"] == []

    def test_malformed_row_is_reported_without_aborting_import(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.services.permissions import require_permission
        app.dependency_overrides[require_permission("LAB_TESTS_MANAGE")] = fake_user

        csv_content = (
            "name,sample_type,price_rupees,turnaround_hours,fasting_required,prep_instructions\n"
            "CBC,Blood,not_a_number,24,false,None required\n"
            "Lipid Profile,Blood,400,24,true,12 hour fast\n"
        )

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )
            mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "new-id"}]
            )
            client = TestClient(app)
            resp = client.post(
                "/admin/lab-tests/import-csv",
                files={"file": ("tests.csv", io.BytesIO(csv_content.encode()), "text/csv")},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] == 1
        assert len(body["errors"]) == 1
        assert "Row 2" in body["errors"][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_tests_admin.py::TestLabTestsCsvImport -v`
Expected: FAIL — `404 Not Found` (no `/admin/lab-tests/import-csv` route yet).

- [ ] **Step 3: Write minimal implementation**

In `app/routers/admin.py`, add `import csv` and `import io` to the top-level imports (alongside `import asyncio`, `import logging`, etc. at lines 3-8):

```python
import asyncio
import csv
import io
import logging
import re
import secrets
```

Add the endpoint after `delete_lab_test`:

```python
@router.post("/lab-tests/import-csv")
async def import_lab_tests_csv(
    file: UploadFile = File(...),
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("LAB_TESTS_MANAGE")),
):
    """Bulk-import lab tests from a CSV file.

    Expected columns: name,sample_type,price_rupees,turnaround_hours,
    fasting_required,prep_instructions. Each row is upserted by
    (clinic_id, name). Malformed rows are reported individually — a single
    bad row never aborts the whole import.
    """
    effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded CSV")

    reader = csv.DictReader(io.StringIO(text))
    required_cols = {"name", "price_rupees"}
    if not reader.fieldnames or not required_cols.issubset(set(reader.fieldnames)):
        raise HTTPException(
            status_code=400, detail="CSV must include at least 'name' and 'price_rupees' columns"
        )

    created, updated, errors = 0, 0, []
    for i, row in enumerate(reader, start=2):  # header is row 1
        name = (row.get("name") or "").strip()
        price_raw = (row.get("price_rupees") or "").strip()
        if not name:
            errors.append(f"Row {i}: missing name")
            continue
        try:
            price_rupees = int(price_raw)
            if price_rupees <= 0:
                raise ValueError
        except ValueError:
            errors.append(f"Row {i} ('{name}'): price_rupees must be a positive whole number")
            continue

        turnaround_raw = (row.get("turnaround_hours") or "").strip()
        test_data = {
            "clinic_id": effective_clinic_id,
            "name": name,
            "price_paise": price_rupees * 100,
            "sample_type": (row.get("sample_type") or "").strip() or None,
            "turnaround_hours": int(turnaround_raw) if turnaround_raw.isdigit() else None,
            "fasting_required": (row.get("fasting_required") or "").strip().lower() in ("true", "1", "yes"),
            "prep_instructions": (row.get("prep_instructions") or "").strip() or None,
        }

        try:
            existing = (
                supabase.table("lab_tests")
                .select("id")
                .eq("clinic_id", effective_clinic_id)
                .eq("name", name)
                .execute()
            )
            if existing.data:
                supabase.table("lab_tests").update(test_data).eq("id", existing.data[0]["id"]).execute()
                updated += 1
            else:
                supabase.table("lab_tests").insert(test_data).execute()
                created += 1
        except Exception as e:
            errors.append(f"Row {i} ('{name}'): {_friendly_db_error(e, 'save failed')}")

    await log_admin_action(
        user=user,
        action="import_lab_tests_csv",
        resource_type="lab_test",
        resource_id=None,
        details={"created": created, "updated": updated, "errors": len(errors)},
        ip_address="unknown",
    )
    return {"created": created, "updated": updated, "errors": errors}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_tests_admin.py::TestLabTestsCsvImport -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/routers/admin.py tests/test_lab_tests_admin.py
git commit -m "feat(admin): add CSV bulk-import endpoint for lab test catalog"
```

---

### Task 10: `admin.py` — collection window endpoint

**Files:**
- Modify: `app/routers/admin.py` (add endpoint after `import_lab_tests_csv` from Task 9)
- Test: `tests/test_lab_tests_admin.py` (append)

**Interfaces:**
- Consumes: `branches.config`/`clinics.config` JSONB (pre-existing), `LAB_TESTS_MANAGE` permission (Task 4).
- Produces: `PUT /admin/lab-collection-window?branch_id=<optional>` — consumed by Task 16 (`admin/index.html`). Consumed at read time by Task 6's `get_lab_collection_window`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lab_tests_admin.py`:

```python
class TestLabCollectionWindowEndpoint:
    def test_sets_clinic_level_window_when_no_branch_id(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.services.permissions import require_permission
        app.dependency_overrides[require_permission("LAB_TESTS_MANAGE")] = fake_user

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ):
            mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"config": {}}]
            )
            mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"id": "clinic-1"}]
            )
            client = TestClient(app)
            resp = client.put(
                "/admin/lab-collection-window",
                json={"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"},
            )

        assert resp.status_code == 200
        assert resp.json()["lab_collection"]["start"] == "07:00"

    def test_rejects_bad_time_format(self):
        from app.routers.admin import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.services.permissions import require_permission
        app.dependency_overrides[require_permission("LAB_TESTS_MANAGE")] = fake_user

        client = TestClient(app)
        resp = client.put(
            "/admin/lab-collection-window",
            json={"start": "7am", "end": "11:00", "days": "Mon,Tue"},
        )
        assert resp.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_tests_admin.py::TestLabCollectionWindowEndpoint -v`
Expected: FAIL — `404 Not Found` (no `/admin/lab-collection-window` route yet).

- [ ] **Step 3: Write minimal implementation**

In `app/routers/admin.py`, add the model near `LabTestCreate`/`LabTestUpdate`:

```python
class LabCollectionWindowUpdate(BaseModel):
    start: str
    end: str
    days: str = "Mon,Tue,Wed,Thu,Fri,Sat"

    @field_validator("start", "end")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("Time must be in HH:MM format")
        return v
```

Add the endpoint after `import_lab_tests_csv`:

```python
@router.put("/lab-collection-window")
async def update_lab_collection_window(
    payload: LabCollectionWindowUpdate,
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    user: AdminUser = Depends(require_permission("LAB_TESTS_MANAGE")),
):
    """Set the daily sample collection window for a branch, or the clinic
    itself for single-location clinics (branch_id omitted)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    window = {"start": payload.start, "end": payload.end, "days": payload.days}

    try:
        if branch_id:
            enforce_branch_scope(user, branch_id)
            branch_result = (
                supabase.table("branches")
                .select("config")
                .eq("id", branch_id)
                .eq("clinic_id", effective_clinic_id)
                .execute()
            )
            if not branch_result.data:
                raise HTTPException(status_code=404, detail="Branch not found")
            config = branch_result.data[0].get("config") or {}
            config["lab_collection"] = window
            supabase.table("branches").update({"config": config}).eq("id", branch_id).execute()
        else:
            clinic_result = supabase.table("clinics").select("config").eq("id", effective_clinic_id).execute()
            if not clinic_result.data:
                raise HTTPException(status_code=404, detail="Clinic not found")
            config = clinic_result.data[0].get("config") or {}
            config["lab_collection"] = window
            supabase.table("clinics").update({"config": config}).eq("id", effective_clinic_id).execute()

        return {"success": True, "lab_collection": window}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating lab collection window: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=_friendly_db_error(e, "Failed to update collection window")
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_tests_admin.py::TestLabCollectionWindowEndpoint -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full admin test suite to confirm no regression, then commit**

Run: `pytest tests/test_lab_tests_admin.py -v`
Expected: PASS (all tests from Tasks 4/5/6/8/9/10)

```bash
git add app/routers/admin.py tests/test_lab_tests_admin.py
git commit -m "feat(admin): add lab sample collection window endpoint"
```

---

### Task 11: `conversation.py` — new states + dispatch wiring + diagnostics-only routing

**Files:**
- Modify: `app/services/conversation.py:79-82` (`ConversationState`), `app/services/conversation.py:662-669` (dispatch chain), `app/services/conversation.py:1085-1131` (`_start_booking`)
- Test: `tests/test_lab_test_booking_conversation.py` (new file)

**Interfaces:**
- Consumes: `has_feature(clinic, "lab_test_booking")` (Task 5), `get_doctors` (pre-existing, already imported in `conversation.py`).
- Produces: `ConversationState.BROWSING_LAB_TESTS = "browsing_lab_tests"`, `ConversationState.CONFIRMING_COLLECTION_DATE = "confirming_collection_date"`; routing behavior consumed by Task 12/13's handler methods (added in this same file, next tasks).

- [ ] **Step 1: Write the failing test**

Create `tests/test_lab_test_booking_conversation.py`:

```python
"""Tests for diagnostics-only routing into the lab-test booking flow."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.conversation import ConversationManager, ConversationState


class TestDiagnosticsOnlyRouting:
    def test_new_conversation_states_exist(self):
        assert ConversationState.BROWSING_LAB_TESTS == "browsing_lab_tests"
        assert ConversationState.CONFIRMING_COLLECTION_DATE == "confirming_collection_date"

    @pytest.mark.asyncio
    async def test_diagnostics_only_clinic_enters_lab_test_flow_not_department_list(self):
        """Regression: a diagnostics-only clinic (lab_test_booking feature, zero
        doctors) must never reach _show_department_list — this is the second,
        independent layer of protection against the department/doctor-list
        recursion crash."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"language": "en", "name": "Test Patient"}

        with patch(
            "app.services.tenant.has_feature", return_value=True
        ), patch(
            "app.services.conversation.get_doctors", new_callable=AsyncMock, return_value=[]
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_lab_tests, patch.object(
            manager, "_show_department_list", new_callable=AsyncMock
        ) as mock_show_dept_list, patch(
            "app.services.conversation.get_clinic_branches", new_callable=AsyncMock, return_value=[]
        ), patch(
            "app.services.conversation.has_branches", return_value=False
        ):
            await manager._start_booking(clinic, "+919876543210", patient, "en")

        mock_show_lab_tests.assert_called_once()
        mock_show_dept_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_clinic_with_doctors_never_enters_lab_test_flow(self):
        """A clinic with doctors (even if lab_test_booking is somehow enabled)
        keeps using the normal doctor-booking flow untouched."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"language": "en", "name": "Test Patient"}

        with patch(
            "app.services.tenant.has_feature", return_value=True
        ), patch(
            "app.services.conversation.get_doctors",
            new_callable=AsyncMock,
            return_value=[{"id": "doc-1", "name": "Dr. Test"}],
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_lab_tests, patch.object(
            manager, "_continue_booking_after_branch", new_callable=AsyncMock
        ) as mock_continue, patch(
            "app.services.conversation.get_clinic_branches", new_callable=AsyncMock, return_value=[]
        ), patch(
            "app.services.conversation.has_branches", return_value=False
        ):
            await manager._start_booking(clinic, "+919876543210", patient, "en")

        mock_show_lab_tests.assert_not_called()
        mock_continue.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_test_booking_conversation.py -v`
Expected: FAIL — `AttributeError: BROWSING_LAB_TESTS` (state doesn't exist yet), and `_show_lab_test_list` doesn't exist as a mockable attribute either (patch.object will fail with `AttributeError`).

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, update `ConversationState` (lines 79-82):

```python
    SELECTING_SLOT = "selecting_slot"
    CONFIRMING_BOOKING = "confirming_booking"
    BROWSING_LAB_TESTS = "browsing_lab_tests"
    CONFIRMING_COLLECTION_DATE = "confirming_collection_date"
    AWAITING_PAYMENT = "awaiting_payment"
    MANAGING_APPOINTMENT = "managing_appointment"
```

Update the dispatch chain (lines 662-669), inserting the two new branches between `confirming_booking` and `awaiting_payment`:

```python
        elif state == "confirming_booking":
            await self._handle_confirming_booking(
                clinic, phone, message, intent, context, patient, lang
            )
        elif state == "browsing_lab_tests":
            await self._handle_browsing_lab_tests(
                clinic, phone, message, intent, context, lang, interactive_data
            )
        elif state == "confirming_collection_date":
            await self._handle_confirming_collection_date(
                clinic, phone, message, intent, context, patient, lang, interactive_data
            )
        elif state == "awaiting_payment":
            await self._handle_awaiting_payment(
                clinic, phone, message, context, patient, lang
            )
```

Update `_start_booking` (lines 1085-1096), inserting the diagnostics-only check right after the language guard:

```python
    async def _start_booking(
        self, clinic: dict, phone: str, patient: Optional[dict], lang: str
    ) -> None:
        """Start the booking flow — with optional branch selection for multi-branch clinics."""
        patient = patient or {}

        # Guard: Language must be set before proceeding
        if not patient.get("language"):
            await self._send_language_selection(clinic, phone)
            await self.update_state(clinic, phone, "selecting_language")
            return

        # ── Diagnostics-Only Routing ─────────────────────────────────────────
        # A clinic on the lab_test_booking feature with zero active doctors
        # never enters the doctor/department flow at all — this is checked
        # live (not just plan-assumed), since a polyclinic can have both
        # doctors and lab tests. This also closes a second path into the
        # _show_department_list/_show_doctor_list recursion crash fixed
        # separately today, since diagnostics-only clinics can no longer
        # reach that flow to begin with.
        from app.services.tenant import has_feature

        if has_feature(clinic, "lab_test_booking"):
            doctors = await get_doctors(clinic["id"])
            if not doctors:
                await self._show_lab_test_list(clinic, phone, {}, lang)
                return
        # ── End Diagnostics-Only Routing ─────────────────────────────────────

        # ── Multi-Branch Check ──────────────────────────────────────────────
```

(The rest of `_start_booking`, from the multi-branch check onward, is unchanged.)

- [ ] **Step 4: Add stub methods so the test's `patch.object` calls succeed**

`patch.object(manager, "_show_lab_test_list", ...)` requires the attribute to exist on the class first. Add temporary stub methods at the end of the class (they are replaced with real implementations in Task 12/13 — this step only exists to make Task 11's test independently runnable; Task 12/13 overwrite these same method bodies):

```python
    async def _show_lab_test_list(
        self, clinic: dict, phone: str, context: dict, lang: str
    ) -> None:
        raise NotImplementedError  # implemented in Task 12

    async def _handle_browsing_lab_tests(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        raise NotImplementedError  # implemented in Task 12

    async def _handle_confirming_collection_date(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        patient: dict,
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        raise NotImplementedError  # implemented in Task 13
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_lab_test_booking_conversation.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Run the existing department-selection regression suite to confirm no interference**

Run: `pytest tests/test_department_selection.py -v`
Expected: PASS (all 3 pre-existing tests — `has_feature` is patched per-test there via `"app.services.tenant.has_feature"`, unaffected by this change)

- [ ] **Step 7: Commit**

```bash
git add app/services/conversation.py tests/test_lab_test_booking_conversation.py
git commit -m "feat(conversation): add diagnostics-only routing into lab-test booking states"
```

---

### Task 12: `conversation.py` — `_show_lab_test_list` + `_handle_browsing_lab_tests`

**Files:**
- Modify: `app/services/conversation.py` (replace the two `NotImplementedError` stubs from Task 11, Step 4)
- Test: `tests/test_lab_test_booking_conversation.py` (append)

**Interfaces:**
- Consumes: `get_lab_tests`, `get_lab_test_by_id`, `get_lab_collection_window` (Task 6), `send_interactive_list`, `send_interactive_buttons` (pre-existing, `app/services/whatsapp.py:271`, `:208`).
- Produces: replaces the two stub methods with working implementations; `_next_collection_dates(self, days_config: str, count: int = 3) -> list[str]` helper method, consumed only within this file by `_handle_browsing_lab_tests`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lab_test_booking_conversation.py`:

```python
class TestShowLabTestList:
    @pytest.mark.asyncio
    async def test_sends_list_of_active_tests(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {}

        fake_tests = [
            {"id": "t1", "name": "CBC", "price_paise": 50000, "sample_type": "Blood"},
            {"id": "t2", "name": "Lipid Profile", "price_paise": 40000, "sample_type": "Blood"},
        ]

        with patch(
            "app.database.get_lab_tests", new_callable=AsyncMock, return_value=fake_tests
        ), patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ) as mock_update_state:
            await manager._show_lab_test_list(clinic, "+919876543210", context, "en")

        mock_send_list.assert_called_once()
        sections = mock_send_list.call_args.kwargs["sections"]
        row_ids = [r["id"] for r in sections[0]["rows"]]
        assert row_ids == ["labtest_t1", "labtest_t2"]
        mock_update_state.assert_called_once_with(clinic, "+919876543210", "browsing_lab_tests", context)

    @pytest.mark.asyncio
    async def test_no_active_tests_falls_back_to_main_menu(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {}

        with patch(
            "app.database.get_lab_tests", new_callable=AsyncMock, return_value=[]
        ), patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ) as mock_send_text, patch.object(
            manager, "_send_main_menu", new_callable=AsyncMock
        ) as mock_main_menu:
            await manager._show_lab_test_list(clinic, "+919876543210", context, "en")

        mock_send_text.assert_called_once()
        mock_main_menu.assert_called_once()


class TestHandleBrowsingLabTests:
    @pytest.mark.asyncio
    async def test_selecting_test_offers_collection_dates(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {}
        fake_test = {
            "id": "t1",
            "name": "CBC",
            "price_paise": 50000,
            "sample_type": "Blood",
            "fasting_required": False,
            "prep_instructions": None,
            "turnaround_hours": 24,
        }
        fake_window = {"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"}

        with patch(
            "app.database.get_lab_test_by_id", new_callable=AsyncMock, return_value=fake_test
        ), patch(
            "app.database.get_lab_collection_window", new_callable=AsyncMock, return_value=fake_window
        ), patch.object(
            manager.whatsapp, "send_interactive_buttons", new_callable=AsyncMock
        ) as mock_send_buttons, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ) as mock_update_state:
            await manager._handle_browsing_lab_tests(
                clinic, "+919876543210", "", "select_lab_test", context, "en",
                interactive_data={"id": "labtest_t1"},
            )

        mock_send_buttons.assert_called_once()
        buttons = mock_send_buttons.call_args.kwargs["buttons"]
        assert len(buttons) == 3
        assert all(b["id"].startswith("labdate_") for b in buttons)
        assert context["lab_test_id"] == "t1"
        assert context["lab_test_name"] == "CBC"
        mock_update_state.assert_called_once_with(
            clinic, "+919876543210", "confirming_collection_date", context
        )

    @pytest.mark.asyncio
    async def test_deactivated_test_reprompts_list(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {}

        with patch(
            "app.database.get_lab_test_by_id", new_callable=AsyncMock, return_value=None
        ), patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_list:
            await manager._handle_browsing_lab_tests(
                clinic, "+919876543210", "", "select_lab_test", context, "en",
                interactive_data={"id": "labtest_deleted-id"},
            )

        mock_show_list.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_test_booking_conversation.py::TestShowLabTestList tests/test_lab_test_booking_conversation.py::TestHandleBrowsingLabTests -v`
Expected: FAIL — `NotImplementedError` (stub bodies from Task 11).

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, replace the three-stub block from Task 11 Step 4. First replace `_show_lab_test_list` and `_handle_browsing_lab_tests`; leave `_handle_confirming_collection_date` as the `NotImplementedError` stub for now (Task 13 replaces it):

```python
    async def _show_lab_test_list(
        self, clinic: dict, phone: str, context: dict, lang: str
    ) -> None:
        """Show the clinic's active lab test catalog as a WhatsApp list."""
        from app.database import get_lab_tests

        tests = await get_lab_tests(clinic["id"], branch_id=context.get("branch_id"))
        if not tests:
            no_tests_msg = {
                "en": "Sorry, no lab tests are available for booking right now. Please call us directly.",
                "hi": "क्षमा करें, अभी कोई लैब टेस्ट बुकिंग के लिए उपलब्ध नहीं है। कृपया सीधे हमें कॉल करें।",
                "te": "క్షమించండి, ప్రస్తుతం బుకింగ్ కోసం ల్యాబ్ టెస్ట్‌లు అందుబాటులో లేవు. దయచేసి నేరుగా మాకు కాల్ చేయండి.",
            }.get(
                lang,
                "Sorry, no lab tests are available for booking right now. Please call us directly.",
            )
            await self.whatsapp.send_text(clinic, phone, no_tests_msg)
            await self._send_main_menu(clinic, phone, lang)
            return

        rows = []
        for t in tests[:10]:
            price_rupees = t["price_paise"] // 100
            desc = f"₹{price_rupees}"
            if t.get("sample_type"):
                desc += f" • {t['sample_type']}"
            rows.append(
                {
                    "id": f"labtest_{t['id']}",
                    "title": t["name"][:24],
                    "description": desc[:72],
                }
            )

        body_msg = {
            "en": "Please select the test you'd like to book:",
            "hi": "कृपया वह टेस्ट चुनें जिसे आप बुक करना चाहते हैं:",
            "te": "మీరు బుక్ చేయాలనుకుంటున్న టెస్ట్‌ను ఎంచుకోండి:",
        }.get(lang, "Please select the test you'd like to book:")

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body=body_msg,
            button_text="Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి"),
            sections=[{"rows": rows}],
        )
        await self.update_state(clinic, phone, "browsing_lab_tests", context)

    def _next_collection_dates(self, days_config: str, count: int = 3) -> list[str]:
        """Return up to `count` upcoming YYYY-MM-DD dates matching days_config
        (comma-separated weekday abbreviations, e.g. 'Mon,Tue,Wed,Thu,Fri,Sat')."""
        weekday_abbr = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        allowed = {d.strip() for d in days_config.split(",") if d.strip()}
        dates = []
        cursor = datetime.now(timezone.utc)
        for _ in range(14):
            if weekday_abbr[cursor.weekday()] in allowed:
                dates.append(cursor.strftime("%Y-%m-%d"))
                if len(dates) == count:
                    break
            cursor += timedelta(days=1)
        return dates

    async def _handle_browsing_lab_tests(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Handle a lab test selection from the list and offer collection dates."""
        from app.database import get_lab_test_by_id, get_lab_collection_window

        selected_id = None
        if interactive_data and interactive_data.get("id", "").startswith("labtest_"):
            selected_id = interactive_data["id"][len("labtest_"):]

        if not selected_id:
            await self.whatsapp.send_text(
                clinic, phone, "Please select a test from the list above."
            )
            return

        test = await get_lab_test_by_id(clinic["id"], selected_id)
        if not test:
            await self.whatsapp.send_text(
                clinic, phone, "That test is no longer available. Please choose another."
            )
            await self._show_lab_test_list(clinic, phone, context, lang)
            return

        price_rupees = test["price_paise"] // 100
        detail_lines = [f"🧪 *{test['name']}*", f"💰 Price: ₹{price_rupees}"]
        if test.get("sample_type"):
            detail_lines.append(f"🩸 Sample: {test['sample_type']}")
        if test.get("fasting_required"):
            detail_lines.append("⚠️ Fasting required before this test")
        if test.get("prep_instructions"):
            detail_lines.append(f"📋 Prep: {test['prep_instructions']}")
        if test.get("turnaround_hours"):
            detail_lines.append(f"⏱️ Report in ~{test['turnaround_hours']} hours")

        window = await get_lab_collection_window(clinic, context.get("branch_id"))
        detail_lines.append(f"\n🕐 Sample collection hours: {window['start']} – {window['end']}")

        candidate_dates = self._next_collection_dates(
            window.get("days", "Mon,Tue,Wed,Thu,Fri,Sat")
        )
        if not candidate_dates:
            await self.whatsapp.send_text(
                clinic,
                phone,
                "\n".join(detail_lines)
                + "\n\nNo collection days are configured. Please call us to book.",
            )
            await self._send_main_menu(clinic, phone, lang)
            return

        detail_lines.append("\nPlease choose a collection date:")

        buttons = []
        for d in candidate_dates:
            display = datetime.strptime(d, "%Y-%m-%d").strftime("%d %b")
            buttons.append({"id": f"labdate_{d}", "title": display})

        await self.whatsapp.send_interactive_buttons(
            clinic, phone, body="\n".join(detail_lines), buttons=buttons
        )

        context["lab_test_id"] = test["id"]
        context["lab_test_name"] = test["name"]
        await self.update_state(clinic, phone, "confirming_collection_date", context)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_test_booking_conversation.py::TestShowLabTestList tests/test_lab_test_booking_conversation.py::TestHandleBrowsingLabTests -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/conversation.py tests/test_lab_test_booking_conversation.py
git commit -m "feat(conversation): implement lab test list and date-picker handlers"
```

---

### Task 13: `conversation.py` — `_handle_confirming_collection_date`

**Files:**
- Modify: `app/services/conversation.py` (replace the remaining `NotImplementedError` stub from Task 11, Step 4)
- Test: `tests/test_lab_test_booking_conversation.py` (append)

**Interfaces:**
- Consumes: `payment_service.create_booking_with_payment(..., booking_type="lab_test", lab_test_id=..., lab_test_name=...)` (Task 7).
- Produces: transitions to the pre-existing `awaiting_payment` state, unmodified — the flow terminates here; `_handle_awaiting_payment` (lines 2726-2818, untouched) takes over.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lab_test_booking_conversation.py`:

```python
class TestHandleConfirmingCollectionDate:
    @pytest.mark.asyncio
    async def test_selecting_date_creates_payment_gated_booking(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"id": "patient-1", "name": "Test Patient"}
        context = {"lab_test_id": "t1", "lab_test_name": "CBC", "branch_id": None, "branch_name": None}

        fake_result = {
            "success": True,
            "booking_id": "booking-1",
            "booking_ref": "MC-2026-1000",
            "payment_link": "https://rzp.io/i/xyz",
            "amount_paise": 50000,
            "hold_expires_at": "2026-08-21T13:00:00Z",
        }

        with patch(
            "app.services.payment.payment_service.create_booking_with_payment",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as mock_create_booking, patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ) as mock_send_text, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ) as mock_update_state:
            await manager._handle_confirming_collection_date(
                clinic, "+919876543210", "", "select_date", context, patient, "en",
                interactive_data={"id": "labdate_2026-08-24"},
            )

        mock_create_booking.assert_called_once()
        call_kwargs = mock_create_booking.call_args.kwargs
        assert call_kwargs["booking_type"] == "lab_test"
        assert call_kwargs["lab_test_id"] == "t1"
        assert call_kwargs["lab_test_name"] == "CBC"
        assert call_kwargs["doctor_name"] is None
        assert call_kwargs["appointment_time"] is None
        assert call_kwargs["appointment_date"] == "2026-08-24"

        sent_text = mock_send_text.call_args[0][2]
        assert fake_result["payment_link"] in sent_text
        mock_update_state.assert_called_once_with(
            clinic, "+919876543210", "awaiting_payment", context
        )
        assert context["booking_id"] == "booking-1"
        assert context["booking_ref"] == "MC-2026-1000"

    @pytest.mark.asyncio
    async def test_no_date_selected_reprompts(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"id": "patient-1", "name": "Test Patient"}
        context = {"lab_test_id": "t1", "lab_test_name": "CBC"}

        with patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ) as mock_send_text:
            await manager._handle_confirming_collection_date(
                clinic, "+919876543210", "not a date", "unknown", context, patient, "en",
                interactive_data=None,
            )

        mock_send_text.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_test_booking_conversation.py::TestHandleConfirmingCollectionDate -v`
Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, replace the final `NotImplementedError` stub:

```python
    async def _handle_confirming_collection_date(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        patient: dict,
        lang: str,
        interactive_data: Optional[dict] = None,
    ) -> None:
        """Handle the collection date tap and create the payment-gated booking."""
        selected_date = None
        if interactive_data and interactive_data.get("id", "").startswith("labdate_"):
            selected_date = interactive_data["id"][len("labdate_"):]

        if not selected_date:
            await self.whatsapp.send_text(
                clinic, phone, "Please pick a collection date from the buttons above."
            )
            return

        from app.services.payment import payment_service

        result = await payment_service.create_booking_with_payment(
            clinic_id=clinic["id"],
            patient_phone=phone,
            patient_name=patient.get("name") or "Patient",
            department="Lab Test",
            doctor_name=None,
            appointment_date=selected_date,
            appointment_time=None,
            patient_id=patient.get("id"),
            clinic=clinic,
            branch_id=context.get("branch_id"),
            branch_name=context.get("branch_name"),
            booking_type="lab_test",
            lab_test_id=context["lab_test_id"],
            lab_test_name=context["lab_test_name"],
        )

        if not result["success"]:
            await self.whatsapp.send_text(
                clinic,
                phone,
                "Sorry, we couldn't process your booking right now. Please try again.",
            )
            await self.update_state(clinic, phone, "main_menu")
            await self._send_main_menu(clinic, phone, lang)
            return

        amount_rupees = result["amount_paise"] / 100
        date_display = datetime.strptime(selected_date, "%Y-%m-%d").strftime("%d %b %Y")

        payment_msg = (
            f"💳 *Payment Required to Confirm Booking*\n\n"
            f"🧪 Test: {context['lab_test_name']}\n"
            f"📅 Collection Date: {date_display}\n"
            f"💰 Amount: ₹{amount_rupees:.0f}\n\n"
            f"⏱️ *This booking is held for {settings.booking_hold_minutes} minutes.* Pay before it expires.\n\n"
            f"👉 Click below to pay securely via Razorpay:\n"
            f"{result['payment_link']}"
        )
        await self.whatsapp.send_text(clinic, phone, payment_msg)

        context["booking_id"] = result["booking_id"]
        context["booking_ref"] = result["booking_ref"]
        await self.update_state(clinic, phone, "awaiting_payment", context)
```

Verify `settings` is already imported at module level in `conversation.py` (grep for `from app.config import settings`); if it is not present, add `from app.config import settings` to the existing import block at the top of the file.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_test_booking_conversation.py -v`
Expected: PASS (all tests in the file — routing, list, date-picker, booking creation)

- [ ] **Step 5: Run the full conversation + payment test suites to confirm no regression**

Run: `pytest tests/test_department_selection.py tests/test_conversation_payment_mode.py tests/test_family_member_booking_flow.py tests/test_payment.py -v`
Expected: PASS (all pre-existing tests)

- [ ] **Step 6: Commit**

```bash
git add app/services/conversation.py tests/test_lab_test_booking_conversation.py
git commit -m "feat(conversation): create payment-gated lab-test booking on date selection"
```

---

### Task 14: `admin/index.html` — Lab Tests nav item + page section + form

**Files:**
- Modify: `admin/index.html:806` (nav-link block, add after the Doctors nav-link), `admin/index.html:1020-1034` (add a new `pg-labtests` section after the `pg-doctors` section closes)

**Interfaces:**
- Produces: DOM elements `#pg-labtests`, `#labTestList`, `#labTestCount`, `#labTestFormCard`, `#labTestFormTitle`, `#f-labTestId`, `#f-labTestName`, `#f-labTestSampleType`, `#f-labTestPrice`, `#f-labTestTurnaround`, `#f-labTestFasting`, `#f-labTestPrep`, `#f-labTestActive`, `#labTestMsg`, `#btn-labTestSubmit`, `#btn-labTestCancel` — consumed by Task 15's JS.

- [ ] **Step 1: Add the nav-link**

In `admin/index.html`, immediately after the Doctors nav-link (line 806-808):

```html
            <div class="nav-link" tabindex="0" data-page="doctors" data-feature="booking" onclick="go('doctors',this)">
                <span class="ico">...</span> Doctors
            </div>
            <div class="nav-link" tabindex="0" data-page="labtests" data-feature="lab_test_booking" onclick="go('labtests',this)">
                <span class="ico">🧪</span> Lab Tests
            </div>
```

(Only the second `<div class="nav-link" ...data-page="labtests"...>` block is new; the Doctors block above it is shown for placement context and is unchanged.)

- [ ] **Step 2: Add the page section**

Immediately after the `pg-doctors` section's closing `</div>` (the section that starts at line 1020 — find where it closes, right before the next `<div id="pg-...` section begins), insert:

```html
        <div id="pg-labtests" class="sec">
            <div class="page-head">
                <h2>Lab Tests</h2>
                <p>Manage your diagnostic center's test catalog and sample collection window</p>
            </div>
            <div class="card">
                <div class="card-head">
                    <h3>Test Catalog</h3>
                    <span id="labTestCount" style="color:var(--text3); font-size:0.8rem;"></span>
                </div>
                <div class="tbl-wrap" id="labTestList">
                    <div class="loader"><div class="spin"></div>Loading lab tests...</div>
                </div>
            </div>
            <div class="card">
                <div class="card-head"><h3>📁 Bulk Import (CSV)</h3></div>
                <p style="color:var(--text3); font-size:0.85rem; margin-bottom:10px">
                    Columns: name, sample_type, price_rupees, turnaround_hours, fasting_required, prep_instructions
                </p>
                <input type="file" id="f-labTestCsv" accept=".csv">
                <button class="btn" onclick="submitLabTestCsv()" style="margin-top:8px">Import CSV</button>
                <div id="labTestCsvMsg" style="margin-top:8px"></div>
            </div>
            <div class="card">
                <div class="card-head"><h3>🕐 Sample Collection Window</h3></div>
                <div class="form-row">
                    <div class="field"><label>Start Time</label><input type="time" id="f-collectStart" value="07:00"></div>
                    <div class="field"><label>End Time</label><input type="time" id="f-collectEnd" value="11:00"></div>
                </div>
                <div class="field">
                    <label>Collection Days</label>
                    <div id="f-collectDays" style="display:flex; gap:8px; flex-wrap:wrap; margin-top:6px">
                        <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="collect-day-cb" value="Mon" checked>Mon</label>
                        <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="collect-day-cb" value="Tue" checked>Tue</label>
                        <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="collect-day-cb" value="Wed" checked>Wed</label>
                        <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="collect-day-cb" value="Thu" checked>Thu</label>
                        <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="collect-day-cb" value="Fri" checked>Fri</label>
                        <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="collect-day-cb" value="Sat" checked>Sat</label>
                        <label style="display:flex; align-items:center; gap:4px; font-size:0.8rem"><input type="checkbox" class="collect-day-cb" value="Sun">Sun</label>
                    </div>
                </div>
                <button class="btn" onclick="submitCollectionWindow()" style="margin-top:8px">Save Window</button>
                <div id="collectWindowMsg" style="margin-top:8px"></div>
            </div>
            <div class="form-card" id="labTestFormCard">
                <h3 id="labTestFormTitle">➕ Add New Lab Test</h3>
                <input type="hidden" id="f-labTestId">
                <div id="labTestMsg"></div>
                <div class="form-row">
                    <div class="field"><label>Test Name</label><input type="text" id="f-labTestName" placeholder="e.g. Complete Blood Count"></div>
                    <div class="field"><label>Sample Type</label><input type="text" id="f-labTestSampleType" placeholder="e.g. Blood"></div>
                </div>
                <div class="form-row">
                    <div class="field"><label>Price (₹)</label><input type="number" id="f-labTestPrice" value="500"></div>
                    <div class="field"><label>Turnaround (hours)</label><input type="number" id="f-labTestTurnaround" placeholder="e.g. 24"></div>
                </div>
                <div class="field">
                    <label><input type="checkbox" id="f-labTestFasting"> Fasting required</label>
                </div>
                <div class="field">
                    <label>Prep Instructions</label>
                    <textarea id="f-labTestPrep" placeholder="e.g. 8-12 hour fast required"></textarea>
                </div>
                <div class="field">
                    <label><input type="checkbox" id="f-labTestActive" checked> Active</label>
                </div>
                <div class="form-row">
                    <button class="btn" id="btn-labTestSubmit" onclick="submitLabTest()">Add Test</button>
                    <button class="btn" id="btn-labTestCancel" style="display:none; background:var(--surface)" onclick="resetLabTestForm()">Cancel Edit</button>
                </div>
            </div>
        </div>
```

- [ ] **Step 3: Manual verification**

Open `admin/index.html` in a browser against a running backend with a `diagstream`-plan clinic logged in (or temporarily grant `lab_test_booking` via per-clinic feature override). Confirm the "Lab Tests" nav item appears and clicking it shows the new page shell with loading spinners (JS wiring happens in Task 15, so the lists will not populate yet — that's expected at this step).

- [ ] **Step 4: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): add Lab Tests page markup"
```

---

### Task 15: `admin/index.html` — Lab Tests CRUD JS + `loaders` map entry

**Files:**
- Modify: `admin/index.html:2117` (`loaders` map), add new JS functions near `loadDoctors`/`submitDoctor`/`editDoctor`/`delDoctor` (`admin/index.html:2489-2677`)

**Interfaces:**
- Consumes: `#labTestList`, `#labTestCount`, `#f-labTestId`, `#f-labTestName`, `#f-labTestSampleType`, `#f-labTestPrice`, `#f-labTestTurnaround`, `#f-labTestFasting`, `#f-labTestPrep`, `#f-labTestActive` (Task 14 markup), `api()`/`apiPost()`/`apiPut()`/`apiDel()`, `esc()`, `badge()`, `emptyState()`, `loading()`, `hasPermission()`, `confirmDialog()`, `toast()`, `msg()` (all pre-existing helpers).
- Produces: `loadLabTests()` (registered in the `loaders` map, key `labtests`), `submitLabTest()`, `resetLabTestForm()`, `window.editLabTest`, `window.delLabTest`.

- [ ] **Step 1: Add the `loaders` map entry**

In `admin/index.html`, update line 2117:

```javascript
    const loaders = { dashboard: loadDashboard, profile: loadProfile, appointments: loadAppointments, doctors: loadDoctors, leaves: loadLeaves, holidays: loadHolidays, patients: loadPatients, diagreports: loadDiagnosticQueuePage, labreports: loadLabReports, prescriptions: loadPrescriptions, payments: loadPayments, paysettings: loadPaymentSettings, branches: loadBranches, connectors: loadConnectorsPage, staff: loadStaff, labtests: loadLabTests };
```

- [ ] **Step 2: Add the CRUD JS functions**

Add immediately after `window.delDoctor` (after line 2677, before the `// ═══════ LEAVES ═══════` comment):

```javascript
// ═══════ LAB TESTS ═══════
let labTestCache = [];

async function loadLabTests() {
    const el = document.getElementById('labTestList');
    el.innerHTML = loading();
    try {
        labTestCache = await api('/admin/lab-tests') || [];
        document.getElementById('labTestCount').textContent = `${labTestCache.length} test${labTestCache.length !== 1 ? 's' : ''}`;
        if (!labTestCache.length) { el.innerHTML = emptyState('flask', 'No lab tests added yet'); return; }
        el.innerHTML = `<table>
            <thead><tr><th>Name</th><th>Sample</th><th>Price</th><th>Turnaround</th><th>Status</th><th>Actions</th></tr></thead>
            <tbody>${labTestCache.map(t => `<tr>
                <td style="font-weight:600; color:var(--text-strong)">${esc(t.name)}</td>
                <td>${esc(t.sample_type) || '—'}</td>
                <td>₹${Math.round(t.price_paise / 100)}</td>
                <td style="color:var(--text2); font-size:0.8rem">${t.turnaround_hours ? t.turnaround_hours + 'h' : '—'}</td>
                <td>${t.is_active ? badge('active') : badge('inactive')}</td>
                <td>
                    ${hasPermission('LAB_TESTS_MANAGE') ? `<button class="btn" style="padding:4px 8px; font-size:0.8rem; background:var(--surface); min-width:auto" onclick="editLabTest('${t.id}')">✏️</button>` : ''}
                    ${hasPermission('LAB_TESTS_MANAGE') ? `<button class="btn" style="padding:4px 8px; font-size:0.8rem; background:var(--red-bg); color:var(--red); min-width:auto" onclick="delLabTest('${t.id}')">🗑️</button>` : ''}
                </td>
            </tr>`).join('')}</tbody>
        </table>`;
    } catch (e) { el.innerHTML = emptyState('warning', 'Failed to load lab tests'); }
}

async function submitLabTest() {
    const id = document.getElementById('f-labTestId').value;
    const name = document.getElementById('f-labTestName').value.trim();
    const price = parseInt(document.getElementById('f-labTestPrice').value);
    if (!name || !price || price <= 0) { msg('labTestMsg', 'Please fill in a name and a valid price', true); return; }

    try {
        const payload = {
            name,
            sample_type: document.getElementById('f-labTestSampleType').value.trim() || null,
            price_rupees: price,
            turnaround_hours: parseInt(document.getElementById('f-labTestTurnaround').value) || null,
            fasting_required: document.getElementById('f-labTestFasting').checked,
            prep_instructions: document.getElementById('f-labTestPrep').value.trim() || null,
            is_active: document.getElementById('f-labTestActive').checked,
        };

        if (id) {
            await apiPut(`/admin/lab-tests/${id}`, payload);
            msg('labTestMsg', '✅ Lab test updated successfully!');
        } else {
            await apiPost('/admin/lab-tests', payload);
            msg('labTestMsg', '✅ Lab test added successfully!');
        }

        resetLabTestForm();
        loadLabTests();
    } catch (e) { msg('labTestMsg', e.message || (id ? 'Failed to update lab test' : 'Failed to add lab test'), true); }
}

function resetLabTestForm() {
    document.getElementById('f-labTestId').value = '';
    document.getElementById('f-labTestName').value = '';
    document.getElementById('f-labTestSampleType').value = '';
    document.getElementById('f-labTestPrice').value = '500';
    document.getElementById('f-labTestTurnaround').value = '';
    document.getElementById('f-labTestFasting').checked = false;
    document.getElementById('f-labTestPrep').value = '';
    document.getElementById('f-labTestActive').checked = true;
    document.getElementById('labTestFormTitle').innerHTML = '➕ Add New Lab Test';
    document.getElementById('btn-labTestSubmit').textContent = 'Add Test';
    document.getElementById('btn-labTestCancel').style.display = 'none';
}

window.editLabTest = function(id) {
    const t = labTestCache.find(x => x.id === id);
    if (!t) return;
    document.getElementById('f-labTestId').value = t.id;
    document.getElementById('f-labTestName').value = t.name;
    document.getElementById('f-labTestSampleType').value = t.sample_type || '';
    document.getElementById('f-labTestPrice').value = Math.round(t.price_paise / 100);
    document.getElementById('f-labTestTurnaround').value = t.turnaround_hours || '';
    document.getElementById('f-labTestFasting').checked = Boolean(t.fasting_required);
    document.getElementById('f-labTestPrep').value = t.prep_instructions || '';
    document.getElementById('f-labTestActive').checked = Boolean(t.is_active);

    document.getElementById('labTestFormTitle').innerHTML = '✏️ Edit Lab Test';
    document.getElementById('btn-labTestSubmit').textContent = 'Update Test';
    document.getElementById('btn-labTestCancel').style.display = 'inline-block';

    document.getElementById('f-labTestName').focus();
    window.scrollTo({ top: document.getElementById('pg-labtests').offsetTop, behavior: 'smooth' });
};

window.delLabTest = async function(id) {
    const ok = await confirmDialog('Are you sure you want to delete this lab test? This action cannot be undone.', { okText: 'Delete', danger: true });
    if (!ok) return;
    try {
        await apiDel(`/admin/lab-tests/${id}`);
        loadLabTests();
    } catch (e) { toast('Failed to delete lab test.', true); }
};
```

- [ ] **Step 2: Manual verification**

With a `diagstream`-plan clinic logged into the admin panel, navigate to Lab Tests, add a test, confirm it appears in the table with the correct price, edit it, confirm the form pre-fills, delete it, confirm it disappears and `loadLabTests()` re-renders the empty state.

- [ ] **Step 3: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): wire up Lab Tests CRUD"
```

---

### Task 16: `admin/index.html` — CSV import + collection window JS

**Files:**
- Modify: `admin/index.html` (add JS functions near the Lab Tests CRUD functions added in Task 15)

**Interfaces:**
- Consumes: `#f-labTestCsv`, `#labTestCsvMsg`, `#f-collectStart`, `#f-collectEnd`, `.collect-day-cb`, `#collectWindowMsg` (Task 14 markup), `POST /admin/lab-tests/import-csv`, `PUT /admin/lab-collection-window` (Task 9/10).
- Produces: `submitLabTestCsv()`, `submitCollectionWindow()`.

- [ ] **Step 1: Add the JS functions**

Add immediately after `window.delLabTest` from Task 15:

```javascript
async function submitLabTestCsv() {
    const fileInput = document.getElementById('f-labTestCsv');
    const file = fileInput.files[0];
    if (!file) { msg('labTestCsvMsg', 'Please choose a CSV file first', true); return; }

    try {
        const formData = new FormData();
        formData.append('file', file);
        const resp = await fetch('/admin/lab-tests/import-csv', {
            method: 'POST',
            headers: { 'Authorization': auth },
            body: formData,
        });
        if (!resp.ok) throw new Error((await resp.json()).detail || 'Import failed');
        const result = await resp.json();

        let summary = `✅ Imported: ${result.created} created, ${result.updated} updated`;
        if (result.errors.length) {
            summary += `<br>⚠️ ${result.errors.length} row(s) skipped:<br>` + result.errors.map(e => esc(e)).join('<br>');
        }
        document.getElementById('labTestCsvMsg').innerHTML = summary;
        fileInput.value = '';
        loadLabTests();
    } catch (e) { msg('labTestCsvMsg', e.message || 'Import failed', true); }
}

async function submitCollectionWindow() {
    try {
        const days = Array.from(document.querySelectorAll('.collect-day-cb:checked')).map(cb => cb.value).join(',');
        if (!days) { msg('collectWindowMsg', 'Please select at least one collection day', true); return; }
        const payload = {
            start: document.getElementById('f-collectStart').value,
            end: document.getElementById('f-collectEnd').value,
            days,
        };
        await apiPut('/admin/lab-collection-window', payload);
        msg('collectWindowMsg', '✅ Collection window saved!');
    } catch (e) { msg('collectWindowMsg', e.message || 'Failed to save collection window', true); }
}
```

- [ ] **Step 2: Manual verification**

With a `diagstream`-plan clinic logged in, upload a CSV with one valid row and one row with a non-numeric price; confirm the summary shows `1 created` and the malformed row's error message. Then set a collection window and save; confirm the success message appears and, after reloading the page, the saved values persist (fetch `/admin/lab-tests` or check via a booking flow that `get_lab_collection_window` returns the saved values).

- [ ] **Step 3: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): add CSV import and collection window UI for lab tests"
```

---

### Task 17: `admin/index.html` — staff permission checkboxes for `LAB_TESTS_MANAGE`

**Files:**
- Modify: `admin/index.html:1473-1483` (add-staff permission checkboxes), `admin/index.html:1739-1749` (edit-staff permission checkboxes)

**Interfaces:**
- Consumes: `.staff-perm-cb`/`.edit-staff-perm-cb` collection logic (pre-existing, `admin/index.html:3536`, `:3584`) — no JS changes needed, only new checkbox markup, since the existing collectors already read every checked box by CSS class regardless of which permission value it carries.

- [ ] **Step 1: Add the checkbox to the add-staff form**

In `admin/index.html`, after line 1483 (`STAFF_UPDATE` checkbox in the add-staff form):

```html
                        <label><input type="checkbox" class="staff-perm-cb" value="STAFF_UPDATE"> Update Staff</label>
                        <label><input type="checkbox" class="staff-perm-cb" value="LAB_TESTS_MANAGE"> Manage Lab Tests</label>
```

(Only the second line is new.)

- [ ] **Step 2: Add the checkbox to the edit-staff form**

After line 1749 (`STAFF_UPDATE` checkbox in the edit-staff form):

```html
                <label><input type="checkbox" class="edit-staff-perm-cb" value="STAFF_UPDATE"> Update Staff</label>
                <label><input type="checkbox" class="edit-staff-perm-cb" value="LAB_TESTS_MANAGE"> Manage Lab Tests</label>
```

(Only the second line is new.)

- [ ] **Step 3: Manual verification**

Log in as a `clinic_admin` on a `diagstream`-plan clinic, go to Staff, add a new staff member with the `DIAGNOSTIC_OPERATOR` role preset, confirm "Manage Lab Tests" is pre-checked (per Task 4's `_DIAGNOSTIC_OPERATOR_GRANTS`); then create a `CUSTOM_ROLE` staff member and manually check only "Manage Lab Tests", save, and confirm that staff member's login can reach the Lab Tests admin page while other gated pages stay hidden.

- [ ] **Step 4: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): expose LAB_TESTS_MANAGE in staff permission forms"
```

---

### Task 18: Rollout — Accumax Diagnostics production data fix

**Files:** None (Supabase data change, not code)

**Interfaces:** None — this is an operational step, listed here so it isn't lost after the code ships.

- [ ] **Step 1: Grant the feature and correct the plan for the clinic that triggered this work**

Once Tasks 1-17 are deployed to production, run against the production Supabase project (not the scratch/staging project used for migration verification):

```sql
-- Verify current state first
SELECT id, name, plan, features FROM clinics WHERE name ILIKE '%Accumax%';

-- Ensure the diagstream plan (or an explicit per-clinic override) grants lab_test_booking
UPDATE clinics
SET features = COALESCE(features, '{}'::jsonb) || '{"lab_test_booking": true}'::jsonb
WHERE name ILIKE '%Accumax%';
```

- [ ] **Step 2: Add at least one real lab test via the admin panel**

Log into Accumax Diagnostics' admin panel, add its actual test catalog (manually or via CSV import), and set its real sample collection window — until this is done, patients messaging the WhatsApp number will see the "no lab tests are available" fallback message from Task 12, which is safe but not useful.

- [ ] **Step 3: Manually verify the WhatsApp flow end-to-end against production**

Send "Book Appointment" (or the clinic's equivalent trigger phrase) from a test WhatsApp number to Accumax Diagnostics' number. Confirm: the lab test list appears (not a department/doctor list), selecting a test shows prep instructions and date buttons, selecting a date sends a real Razorpay payment link, and completing a real ₹1 test payment (temporarily set one test's price low for this check, then restore it) confirms the booking and sends the lab-test confirmation copy from Task 7.

This step has no automated test — it is the final human check that the whole chain (migrations → payment → WhatsApp → admin) works against the real production database and real Razorpay/WhatsApp credentials, which none of the mocked unit tests in Tasks 1-17 can verify.

---

## Self-Review

**1. Spec coverage:**
- "reuse `appointments` + `payment_events`, not a new booking table" → Tasks 2, 7 (no new booking table; `payment_events` untouched, already generic).
- NULL `doctor_name` semantics documented, not fixed → Task 2's migration comment.
- `lab_tests` catalog table → Task 1.
- Branch/clinic `config.lab_collection` → Task 6 (read), Task 10 (write) — no migration, matches spec ("that column and pattern already exists").
- `lab_reports.matched_booking_id` → Task 3.
- Conversation flow (`browsing_lab_tests → confirming_collection_date → awaiting_payment`) → Tasks 11, 12, 13 (spec's illustrative `selecting_lab_test` middle state is folded into `_handle_browsing_lab_tests`'s response handling, not a separate persisted state — documented in the plan header's Architecture line).
- Routing decision (diagnostics-only → lab test flow, never department list) → Task 11.
- `_notify_payment_confirmed` `booking_type` branch → Task 7.
- Admin "Lab Tests" nav item, feature+permission gated → Tasks 14, 4, 5.
- CSV bulk import, per-row error reporting → Task 9 (backend), Task 16 (UI).
- Collection window admin form → Task 10 (backend), Task 14/16 (UI).
- `lab_test_booking` granted to `diagstream`/`polyclinic` only → Task 5.
- Testing plan (routing test, payment/webhook/notification test, NULL non-interference, CSV import test) → Tasks 11 (routing), 7 (payment/notification), 2 Step 2 (NULL semantics — verified manually against real Postgres per the spec's own requirement that this needs real index semantics, not a mock), 9 (CSV import).
- Explicitly out of scope items (per-test slots, home collection, multi-test cart, doctor consultations at diagnostic centers) → not built anywhere in this plan; confirmed absent.
- Rollout note for Accumax Diagnostics' production plan/feature correction → Task 18.

**2. Placeholder scan:** No "TBD"/"TODO"/"add appropriate error handling" found. Every step has runnable code or an exact SQL/manual verification command.

**3. Type consistency:** `create_booking_with_payment`'s new parameters (`booking_type`, `lab_test_id`, `lab_test_name`) match across Task 7 (definition) and Task 13 (call site — all three passed by keyword, matching names exactly). `get_lab_tests`/`get_lab_test_by_id`/`get_lab_collection_window` signatures match across Task 6 (definition) and Tasks 12 (call sites — `get_lab_tests(clinic["id"], branch_id=...)`, `get_lab_test_by_id(clinic["id"], selected_id)`, `get_lab_collection_window(clinic, context.get("branch_id"))`). `ConversationState.BROWSING_LAB_TESTS`/`CONFIRMING_COLLECTION_DATE` string values (`"browsing_lab_tests"`/`"confirming_collection_date"`) match the dispatch-chain string comparisons in Task 11 and the `update_state()` calls in Task 12/13. `LAB_TESTS_MANAGE` string matches across Task 4 (`PERMISSIONS`/`ROLE_PRESETS`), Task 8/9/10 (`require_permission("LAB_TESTS_MANAGE")`), and Task 17 (checkbox `value="LAB_TESTS_MANAGE"`).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-21-diagnostic-center-lab-test-booking.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
