# Diagnostic Admin Panel Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four production bugs in the diagnostic-center admin panel — Sunday collection hours can't differ from other days, real-world CSV exports are rejected by the bulk-import parser, the Lab Tests dashboard section is mislabeled, and the MocDoc report connector crashes on Render because the Chromium browser binary Playwright needs was never installed at deploy time.

**Architecture:** All four are independent, root-caused bugs in already-shipped code — no new subsystems. Each fix lands at the layer that owns the bug: the collection-window schema/UI gets an optional per-day override, the CSV importer gets a header-aliasing + decimal-price layer, the admin UI gets a copy change, and the deployment gets an explicit Docker build pin plus a shared error-translation helper so any future browser-launch misconfiguration fails with an actionable message instead of a raw Playwright stack trace.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, Supabase (Postgres, JSONB `config` columns — no migrations needed), vanilla JS admin panel (`admin/index.html`), Playwright (Chromium), Render.com (Docker build).

**Spec:** No separate spec document — this plan is scoped directly from four screenshots and a real CSV export in `issues/`, verified against the current codebase (`app/routers/admin.py`, `app/database.py`, `app/services/conversation.py`, `admin/index.html`, `connectors/mocdoc/worker.py`, `app/integrations/callmedex/browser/session.py`, `Dockerfile`, `railway.toml`). Root-cause findings are captured inline in each task below.

## Global Constraints

- No DB migrations — `lab_collection` window and all lab-test fields already live in existing JSONB/columns.
- Every changed branch gets a matching test, following the existing patterns in `tests/test_lab_tests_admin.py` (FastAPI `TestClient` + `unittest.mock.patch.object` on `admin_module.supabase`, `AsyncMock` for async helpers).
- Backward compatible: clinics that never set a Sunday override keep seeing exactly the same collection-window text as today.
- CSV import must never silently accept bad data — malformed rows are still reported per-row, not swallowed (existing "single bad row never aborts the whole import" contract is preserved).
- Phone masking / no-stack-traces-in-API-responses rules from `CLAUDE.md` are preserved throughout.

---

## File Structure

- `app/routers/admin.py` — `LabCollectionWindowUpdate` schema gains optional `sunday_start`/`sunday_end`; `import_lab_tests_csv` gains header-aliasing + decimal-price parsing.
- `app/database.py` — `format_collection_window()` new pure-function helper (Sunday-aware display string).
- `app/services/conversation.py` — `_handle_browsing_lab_tests` uses the new formatter instead of the flat `window.get('start')`/`window.get('end')` pair.
- `admin/index.html` — Sample Collection Window form gets two optional Sunday-hours inputs; "Lab Tests" nav label and page header renamed to "Lab Tests / Services".
- `app/utils/browser_errors.py` — new file: `friendly_browser_launch_error()`, a single shared translator used by both Playwright launch sites.
- `connectors/mocdoc/worker.py`, `app/integrations/callmedex/browser/session.py` — wrap `chromium.launch()` to raise the friendly error.
- `render.yaml` — new file: pins the Render service to build from the existing `Dockerfile` (which already runs `playwright install --with-deps chromium` correctly) instead of Render's native Python buildpack.
- Tests: `tests/test_lab_tests_admin.py` (extended), `tests/test_browser_errors.py` (new).

---

### Task 1: Sunday-specific collection hours — backend schema + persistence

**Root cause:** `LabCollectionWindowUpdate` (`app/routers/admin.py:725-735`) and `get_lab_collection_window()` (`app/database.py:236-257`) only ever store one flat `{start, end, days}` window. There is no way to give Sunday different hours than the rest of the week, even though the admin UI already lets an operator list `Sun` as an operating day (screenshot: `issues/WhatsApp Image 2026-08-21 at 3.23.20 PM.jpeg` shows `Mon,Tue,Wed,Thu,Fri,Sat,Sun` with a single 07:00-21:00 window applied to all of them).

**Files:**
- Modify: `app/routers/admin.py:725-735` (`LabCollectionWindowUpdate`), `app/routers/admin.py:1561-1596` (`update_lab_collection_window`)
- Test: `tests/test_lab_tests_admin.py`

**Interfaces:**
- Produces: `LabCollectionWindowUpdate.sunday_start: Optional[str] = None`, `.sunday_end: Optional[str] = None` — both validated as `HH:MM` when present, same regex as `start`/`end`.
- Produces: saved `window` dict optionally contains `sunday_start`/`sunday_end` keys (only when both are supplied) — this is the shape Task 2 reads.

- [ ] **Step 1: Write the failing test**

```python
class TestLabCollectionWindowSundayOverride:
    def test_persists_sunday_override_when_both_fields_given(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

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
                json={
                    "start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
                    "sunday_start": "09:00", "sunday_end": "13:00",
                },
            )

        assert resp.status_code == 200
        saved = resp.json()["lab_collection"]
        assert saved["sunday_start"] == "09:00"
        assert saved["sunday_end"] == "13:00"

    def test_omits_sunday_override_when_not_given(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

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
                json={"start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"},
            )

        assert resp.status_code == 200
        saved = resp.json()["lab_collection"]
        assert "sunday_start" not in saved
        assert "sunday_end" not in saved

    def test_rejects_bad_sunday_time_format(self):
        from app.routers.admin import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        client = TestClient(app)
        resp = client.put(
            "/admin/lab-collection-window",
            json={"start": "07:00", "end": "21:00", "days": "Sun", "sunday_start": "9am", "sunday_end": "13:00"},
        )
        assert resp.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_tests_admin.py -k SundayOverride -v`
Expected: FAIL — `sunday_start`/`sunday_end` are not accepted fields / not persisted (extra keys silently dropped by Pydantic, or KeyError on `saved["sunday_start"]`).

- [ ] **Step 3: Implement**

Replace `app/routers/admin.py:725-735`:

```python
class LabCollectionWindowUpdate(BaseModel):
    start: str
    end: str
    days: str = "Mon,Tue,Wed,Thu,Fri,Sat"
    sunday_start: Optional[str] = None
    sunday_end: Optional[str] = None

    @field_validator("start", "end", "sunday_start", "sunday_end")
    @classmethod
    def validate_time_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("Time must be in HH:MM format")
        return v
```

In `update_lab_collection_window` (`app/routers/admin.py:1571`), replace:

```python
    window = {"start": payload.start, "end": payload.end, "days": payload.days}
```

with:

```python
    window = {"start": payload.start, "end": payload.end, "days": payload.days}
    if payload.sunday_start and payload.sunday_end:
        window["sunday_start"] = payload.sunday_start
        window["sunday_end"] = payload.sunday_end
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_tests_admin.py -k SundayOverride -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/routers/admin.py tests/test_lab_tests_admin.py
git commit -m "feat(lab-tests): allow optional Sunday-specific collection hours"
```

---

### Task 2: Sunday-aware collection-window display text

**Root cause:** `app/services/conversation.py:3565` shows patients a single flat `window.get('start')} - {window.get('end')` string regardless of which day they end up picking — so even after Task 1 lets an operator configure different Sunday hours, the WhatsApp message would still lie about them.

**Files:**
- Modify: `app/database.py` (add `format_collection_window`, right after `get_lab_collection_window` at line 257)
- Modify: `app/services/conversation.py:3509`, `:3565`
- Test: `tests/test_lab_tests_admin.py`

**Interfaces:**
- Consumes: the `window` dict shape from Task 1 (`start`, `end`, `days`, optional `sunday_start`, `sunday_end`).
- Produces: `format_collection_window(window: dict) -> str` — pure function, no I/O.

- [ ] **Step 1: Write the failing test**

```python
class TestFormatCollectionWindow:
    def test_no_sunday_override_returns_flat_range(self):
        from app.database import format_collection_window

        window = {"start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"}
        assert format_collection_window(window) == "07:00 - 21:00"

    def test_sunday_override_appended_when_sunday_is_an_operating_day(self):
        from app.database import format_collection_window

        window = {
            "start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
            "sunday_start": "09:00", "sunday_end": "13:00",
        }
        assert format_collection_window(window) == "07:00 - 21:00 (Sun: 09:00 - 13:00)"

    def test_sunday_override_ignored_when_sunday_not_an_operating_day(self):
        from app.database import format_collection_window

        window = {
            "start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat",
            "sunday_start": "09:00", "sunday_end": "13:00",
        }
        assert format_collection_window(window) == "07:00 - 21:00"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_tests_admin.py -k FormatCollectionWindow -v`
Expected: FAIL with `ImportError: cannot import name 'format_collection_window'`

- [ ] **Step 3: Implement**

Add to `app/database.py` immediately after `get_lab_collection_window` (after line 257):

```python
def format_collection_window(window: dict) -> str:
    """Human-readable collection-window text for the WhatsApp booking flow.

    Appends a Sunday-specific note only when the clinic both operates on
    Sunday and has configured different Sunday hours — otherwise identical
    to the flat start-end string clinics have always seen.
    """
    base = f"{window.get('start', '07:00')} - {window.get('end', '11:00')}"
    sunday_start = window.get("sunday_start")
    sunday_end = window.get("sunday_end")
    if sunday_start and sunday_end:
        days = {d.strip() for d in window.get("days", "").split(",") if d.strip()}
        if "Sun" in days:
            return f"{base} (Sun: {sunday_start} - {sunday_end})"
    return base
```

In `app/services/conversation.py:3509`, extend the existing local import:

```python
        from app.database import get_lab_test_by_id, get_lab_collection_window, format_collection_window
```

Replace `app/services/conversation.py:3565`:

```python
            f"🏠 Collection window: {window.get('start', '07:00')} - {window.get('end', '11:00')}"
```

with:

```python
            f"🏠 Collection window: {format_collection_window(window)}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_tests_admin.py -k FormatCollectionWindow -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/database.py app/services/conversation.py tests/test_lab_tests_admin.py
git commit -m "feat(lab-tests): show Sunday-specific hours in booking confirmation"
```

---

### Task 3: Sunday-hours fields in the admin UI

**Files:**
- Modify: `admin/index.html:1247-1256` (Sample Collection Window form), `admin/index.html:3307-3324` (`submitCollectionWindow`)

**Interfaces:**
- Consumes: `PUT /admin/lab-collection-window` accepting optional `sunday_start`/`sunday_end` from Task 1.

- [ ] **Step 1: Manual verification plan (no automated test — static HTML/JS form)**

After Step 2, load the admin panel locally, open Lab Tests, and confirm: leaving the two new fields blank and saving still works (matches `test_omits_sunday_override_when_not_given`); filling both and saving round-trips correctly on reload.

- [ ] **Step 2: Implement**

Replace `admin/index.html:1247-1256`:

```html
                    <form id="collection-window-form" onsubmit="submitCollectionWindow(event)">
                        <div class="form-row">
                            <div class="field"><label>Start Time</label><input type="time" id="cw-start" required value="07:00"></div>
                            <div class="field"><label>End Time</label><input type="time" id="cw-end" required value="11:00"></div>
                            <div class="field"><label>Operating Days (comma-separated)</label><input type="text" id="cw-days" required value="Mon,Tue,Wed,Thu,Fri,Sat,Sun" placeholder="Mon,Tue,Wed,Thu,Fri,Sat,Sun"></div>
                        </div>
                        <div class="form-row" style="margin-top:8px;">
                            <div class="field"><label>Sunday Start (optional override)</label><input type="time" id="cw-sunday-start"></div>
                            <div class="field"><label>Sunday End (optional override)</label><input type="time" id="cw-sunday-end"></div>
                        </div>
                        <p style="color:var(--text3); font-size:0.78rem; margin-top:4px;">Leave the Sunday fields blank if Sunday uses the same hours as your other operating days.</p>
                        <div style="margin-top:12px;">
                            <button type="submit" class="btn btn-accent">Save Window</button>
                        </div>
                    </form>
```

Replace `submitCollectionWindow` (`admin/index.html:3307-3324`):

```javascript
async function submitCollectionWindow(e) {
    if (e && e.preventDefault) e.preventDefault();
    const start = document.getElementById('cw-start').value;
    const end = document.getElementById('cw-end').value;
    const days = document.getElementById('cw-days').value.trim();
    const sundayStart = document.getElementById('cw-sunday-start').value;
    const sundayEnd = document.getElementById('cw-sunday-end').value;

    if (!start || !end || !days) {
        toast('Please fill all collection window fields', true);
        return;
    }

    const payload = { start, end, days };
    if (sundayStart && sundayEnd) {
        payload.sunday_start = sundayStart;
        payload.sunday_end = sundayEnd;
    }

    try {
        await apiPut('/admin/lab-collection-window', payload);
        toast('Sample collection window saved');
    } catch (e) {
        toast(e.message || 'Failed to save collection window', true);
    }
}
```

- [ ] **Step 3: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): add optional Sunday collection-hours override"
```

---

### Task 4: CSV import — accept real-world column headers

**Root cause:** `import_lab_tests_csv` (`app/routers/admin.py:1500-1505`) requires the literal lowercase column names `name` and `price_rupees`. The user's actual export, `issues/ACCUMAX CHATBOT.csv`, uses `TEST NAME,PRICE IN RUPEES` — a completely reasonable human-authored CSV that gets rejected outright with *"CSV must include at least 'name' and 'price_rupees' columns"* (screenshot: `issues/WhatsApp Image 2026-08-21 at 3.17.42 PM.jpeg`).

**Files:**
- Modify: `app/routers/admin.py:1480-1559` (`import_lab_tests_csv` and a new module-level alias table above it)
- Test: `tests/test_lab_tests_admin.py`

**Interfaces:**
- Produces: `_CSV_HEADER_ALIASES: dict[str, set[str]]` and `_normalize_csv_headers(fieldnames: list[str]) -> dict[str, str]` (raw header → canonical field name), both module-private to `admin.py`.

- [ ] **Step 1: Write the failing test**

```python
    def test_accepts_real_world_aliased_headers(self):
        """Matches the actual ACCUMAX CHATBOT.csv export format: 'TEST NAME,PRICE IN RUPEES'."""
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        csv_content = (
            "TEST NAME,PRICE IN RUPEES\n"
            "BRUCELLOSIS IGG/IGM (EACH),4430\n"
            "HSV PCR,6450\n"
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

    def test_still_rejects_csv_missing_a_price_column_entirely(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        csv_content = "TEST NAME,NOTES\nCBC,routine\n"

        with patch.object(admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"):
            client = TestClient(app)
            resp = client.post(
                "/admin/lab-tests/import-csv",
                files={"file": ("tests.csv", io.BytesIO(csv_content.encode()), "text/csv")},
            )

        assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lab_tests_admin.py -k "aliased_headers" -v`
Expected: FAIL with 400 (`CSV must include at least 'name' and 'price_rupees' columns`) instead of 200.

- [ ] **Step 3: Implement**

Add above `import_lab_tests_csv` (before line 1480 in `app/routers/admin.py`):

```python
_CSV_HEADER_ALIASES: dict[str, set[str]] = {
    "name": {"name", "test name", "testname", "test_name"},
    "price_rupees": {
        "price_rupees", "price", "price in rupees", "price(rs)", "price (rs)",
        "mrp", "rate", "amount",
    },
    "sample_type": {"sample_type", "sample type", "specimen", "specimen type"},
    "turnaround_hours": {
        "turnaround_hours", "turnaround hours", "turnaround (hours)", "tat", "tat (hours)",
    },
    "fasting_required": {"fasting_required", "fasting", "fasting required"},
    "prep_instructions": {"prep_instructions", "preparation", "prep instructions", "instructions"},
}


def _normalize_csv_headers(fieldnames: list[str]) -> dict[str, str]:
    """Map each raw CSV header to its canonical field name, case/whitespace-insensitively.

    Headers that don't match any known alias are left unmapped (ignored on
    each row) rather than rejected — a stray "Notes" column shouldn't block
    an otherwise-valid import.
    """
    header_map: dict[str, str] = {}
    for raw in fieldnames:
        norm = raw.strip().lower()
        for canonical, aliases in _CSV_HEADER_ALIASES.items():
            if norm in aliases:
                header_map[raw] = canonical
                break
    return header_map
```

Replace `app/routers/admin.py:1500-1531`:

```python
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
```

with:

```python
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV file has no header row")

    header_map = _normalize_csv_headers(reader.fieldnames)
    canonical_present = set(header_map.values())
    if not {"name", "price_rupees"}.issubset(canonical_present):
        raise HTTPException(
            status_code=400,
            detail=(
                "CSV must include a test-name column (e.g. 'name' or 'Test Name') "
                "and a price column (e.g. 'price_rupees' or 'Price in Rupees')"
            ),
        )

    created, updated, errors = 0, 0, []
    for i, raw_row in enumerate(reader, start=2):  # header is row 1
        row = {header_map.get(k, k): v for k, v in raw_row.items() if k is not None}
        name = (row.get("name") or "").strip()
        price_raw = (row.get("price_rupees") or "").strip().replace(",", "").replace("₹", "")
        if not name:
            errors.append(f"Row {i}: missing name")
            continue
        try:
            price_rupees_val = float(price_raw)
            if price_rupees_val <= 0:
                raise ValueError
        except ValueError:
            errors.append(f"Row {i} ('{name}'): price_rupees must be a positive number")
            continue

        turnaround_raw = (row.get("turnaround_hours") or "").strip()
        test_data = {
            "clinic_id": effective_clinic_id,
            "name": name,
            "price_paise": int(round(price_rupees_val * 100)),
            "sample_type": (row.get("sample_type") or "").strip() or None,
            "turnaround_hours": int(turnaround_raw) if turnaround_raw.isdigit() else None,
            "fasting_required": (row.get("fasting_required") or "").strip().lower() in ("true", "1", "yes"),
            "prep_instructions": (row.get("prep_instructions") or "").strip() or None,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lab_tests_admin.py -k "CsvImport or aliased_headers or missing_a_price" -v`
Expected: PASS (all CSV import tests, old and new)

- [ ] **Step 5: Commit**

```bash
git add app/routers/admin.py tests/test_lab_tests_admin.py
git commit -m "fix(lab-tests): accept real-world CSV column headers on bulk import"
```

---

### Task 5: CSV import — accept decimal rupee prices

**Root cause:** The real ACCUMAX file has fractional prices (e.g. `PROTEIN - PLEURAL FLUID,3601.2`, `ANTI PARIETAL CELLS ANTIBODY,2110.8`, `HELICOBACTER PYLORI ANTIGEN, RAPID STOO,4797.6`). Task 4's `float(price_raw)` already parses these correctly (the old code used `int(price_raw)`, which would `ValueError` on any decimal price) — this task adds the regression test that proves it, since it's a distinct real-data bug from the header-naming one and deserves its own explicit coverage.

**Files:**
- Test: `tests/test_lab_tests_admin.py`

**Interfaces:**
- Consumes: `test_data["price_paise"]` computed in Task 4 as `int(round(price_rupees_val * 100))`.

- [ ] **Step 1: Write the test**

```python
    def test_accepts_decimal_rupee_prices(self):
        """Real ACCUMAX export rows like 'PROTEIN - PLEURAL FLUID,3601.2'."""
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        csv_content = "TEST NAME,PRICE IN RUPEES\nPROTEIN - PLEURAL FLUID,3601.2\n"

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
        assert resp.json()["created"] == 1
        insert_call = mock_sb.table.return_value.insert.call_args[0][0]
        assert insert_call["price_paise"] == 360120
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_lab_tests_admin.py -k decimal_rupee -v`
Expected: PASS (implementation already lands as part of Task 4's replacement block — this step just proves it holds for the exact real-data shape).

- [ ] **Step 3: Commit**

```bash
git add tests/test_lab_tests_admin.py
git commit -m "test(lab-tests): cover decimal rupee prices in CSV import"
```

---

### Task 6: Rename "Lab Tests" to "Lab Tests / Services" in the admin dashboard

**Root cause:** Screenshot `issues/WhatsApp Image 2026-08-21 at 3.23.20 PM.jpeg` shows the sidebar nav item and page reading just "Lab Tests" — the user wants it labeled "Lab Tests / Services" since the catalog covers general diagnostic services too, not only lab tests narrowly.

**Files:**
- Modify: `admin/index.html:825` (sidebar nav label), `admin/index.html:1236` (page `<h2>`)

- [ ] **Step 1: Implement**

`admin/index.html:825` — change the trailing nav text from `Lab Tests` to `Lab Tests / Services` (keep the existing `<span class="ico">...</span>` icon markup untouched, only the text node after it changes).

`admin/index.html:1236` — replace:

```html
                <h2>🧪 Lab Test Catalog</h2>
```

with:

```html
                <h2>🧪 Lab Tests / Services Catalog</h2>
```

- [ ] **Step 2: Manual verification**

Load the admin panel, confirm the sidebar reads "Lab Tests / Services" and the page header reads "🧪 Lab Tests / Services Catalog"; confirm no other code references the old nav label text for routing (routing uses `go('lab-tests', ...)` ids, not label text, so this is a pure copy change).

- [ ] **Step 3: Commit**

```bash
git add admin/index.html
git commit -m "copy(admin-ui): rename Lab Tests section to Lab Tests / Services"
```

---

### Task 7: Actionable error when the Chromium browser binary is missing

**Root cause:** `connectors/mocdoc/worker.py:138` and `app/integrations/callmedex/browser/session.py:52` both call `playwright.chromium.launch()` with no handling for a missing browser binary. When it's missing, Playwright raises `Executable doesn't exist at /opt/render/.cache/ms-playwright/chromium_headless_shell-...` — a message meant for a developer's terminal, not for whoever reads the WhatsApp admin alert (`⚠️ MocDoc Connector Crashed`, per the user's report this turn). This task adds a shared translator so the *next* time either browser fails to launch for any reason (missing binary, OOM, sandbox issue), the alert is actionable. Task 8 fixes the actual root cause (Render not installing the browser at build time) for this specific incident.

**Files:**
- Create: `app/utils/browser_errors.py`
- Modify: `connectors/mocdoc/worker.py:133-145` (`_init_browser`)
- Modify: `app/integrations/callmedex/browser/session.py` (around line 52)
- Test: `tests/test_browser_errors.py`

**Interfaces:**
- Produces: `friendly_browser_launch_error(exc: Exception) -> str`, pure function, no I/O.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for translating opaque Playwright browser-launch failures into
actionable operator-facing messages."""
import pytest


class TestFriendlyBrowserLaunchError:
    def test_translates_missing_executable_error(self):
        from app.utils.browser_errors import friendly_browser_launch_error

        exc = Exception(
            "Executable doesn't exist at /opt/render/.cache/ms-playwright/"
            "chromium_headless_shell-1148/chrome-linux/headless_shell\n"
            "Looks like Playwright was just installed or updated."
        )
        msg = friendly_browser_launch_error(exc)
        assert "playwright install" in msg.lower()
        assert "render.yaml" in msg.lower() or "dockerfile" in msg.lower()

    def test_passes_through_unrelated_errors_unchanged(self):
        from app.utils.browser_errors import friendly_browser_launch_error

        exc = Exception("net::ERR_CONNECTION_REFUSED")
        assert friendly_browser_launch_error(exc) == "net::ERR_CONNECTION_REFUSED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_browser_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.utils.browser_errors'`

- [ ] **Step 3: Implement**

Create `app/utils/browser_errors.py`:

```python
"""Translates opaque Playwright browser-launch failures into an actionable
message, so on-call staff reading a WhatsApp admin alert don't have to
parse a raw Playwright stack trace to know what to do next."""


def friendly_browser_launch_error(exc: Exception) -> str:
    text = str(exc)
    if "Executable doesn't exist" in text or "playwright install" in text.lower():
        return (
            "Chromium browser is not installed on this server — the deploy did not "
            "run 'playwright install --with-deps chromium' at build time. Check that "
            "the Render service builds from the Dockerfile (see render.yaml, env: docker), "
            "then redeploy."
        )
    return text
```

In `connectors/mocdoc/worker.py`, replace lines 137-138:

```python
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
```

with:

```python
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
```

and wrap the existing `launch(...)` call's closing so the full block becomes:

```python
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
        except Exception as e:
            from app.utils.browser_errors import friendly_browser_launch_error
            raise RuntimeError(friendly_browser_launch_error(e)) from e
```

In `app/integrations/callmedex/browser/session.py`, wrap the existing `browser = await playwright_obj.chromium.launch(headless=headless)` (line 52) the same way:

```python
            try:
                browser = await playwright_obj.chromium.launch(headless=headless)
            except Exception as e:
                from app.utils.browser_errors import friendly_browser_launch_error
                raise RuntimeError(friendly_browser_launch_error(e)) from e
```

(Match existing indentation at that call site — read the surrounding function before editing to preserve it exactly.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_browser_errors.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/utils/browser_errors.py connectors/mocdoc/worker.py app/integrations/callmedex/browser/session.py tests/test_browser_errors.py
git commit -m "fix(connectors): surface actionable error when Chromium binary is missing"
```

---

### Task 8: Pin the Render build to Docker so Playwright's Chromium install actually runs

**Root cause:** `Dockerfile:24` already correctly runs `RUN playwright install --with-deps chromium`. But the crash path in the error the user reported — `/opt/render/.cache/ms-playwright/chromium_headless_shell` — is Render's *native* Python buildpack cache directory, not a Docker image path. There is no `render.yaml` in this repo (confirmed absent by directory listing), and the only build config present, `railway.toml`, is Railway-specific and has no effect on Render. This means the live Render service is almost certainly configured as a native Python web service (`pip install -r requirements.txt` + `uvicorn ...`), which never runs the Dockerfile's `playwright install` step at all — so the browser binary is simply never present, every single deploy, not intermittently.

**Files:**
- Create: `render.yaml`

**Interfaces:** None — deployment config only.

- [ ] **Step 1: Implement**

Create `render.yaml` at the repo root:

```yaml
services:
  - type: web
    name: mediassist-ai
    env: docker
    dockerfilePath: ./Dockerfile
    healthCheckPath: /health
    autoDeploy: true
```

This does not touch environment variables (those stay exactly as configured in the Render dashboard) — it only pins the build mechanism to the Dockerfile that already installs Chromium + its OS-level dependencies correctly.

- [ ] **Step 2: Required manual action (cannot be automated from this repo)**

`render.yaml` alone does not retroactively change an already-created Render service's build settings unless that service is managed as a Render "Blueprint". Do ONE of the following in the Render dashboard for the live `mediassist-ai` (or equivalently named) service:

- **Option A (recommended, no env-var risk):** Service → Settings → Build & Deploy → change Environment/Runtime to **Docker**, set Dockerfile Path to `./Dockerfile`, save, then trigger **Manual Deploy → Clear build cache & deploy**.
- **Option B:** Reconnect the repo as a Render Blueprint so `render.yaml` is adopted directly (only do this if comfortable confirming existing env vars survive the reconnect — verify in a staging environment first if available).

After redeploying, confirm in the Render build logs that a line resembling `Downloading Chromium ... playwright install --with-deps chromium` appears, and that the next MocDoc connector poll on Accumax Diagnostics completes without the `⚠️ MocDoc Connector Crashed` alert.

- [ ] **Step 3: Commit**

```bash
git add render.yaml
git commit -m "fix(deploy): pin Render build to Dockerfile so Playwright Chromium installs"
```

---

### Task 9: Full regression + manual verification

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: all prior tests plus every test added in Tasks 1, 2, 4, 5, 7 pass; zero regressions.

- [ ] **Step 2: Manual admin-panel verification (cannot be automated — requires live credentials)**

- Lab Tests / Services page: set a Sunday override (e.g. 09:00-13:00) alongside a 07:00-21:00 weekday window, save, reload the page, confirm both persist.
- Upload the real `issues/ACCUMAX CHATBOT.csv` (1397 rows) via Import CSV and confirm it reports `created: 1397, errors: []` (or a small number of `errors` only for genuinely blank-name rows, if any exist) instead of the header-rejection message.
- Confirm the sidebar and page header read "Lab Tests / Services".

- [ ] **Step 3: Manual WhatsApp verification**

- Book a lab test at Accumax Diagnostics for a Sunday collection date and confirm the WhatsApp message shows the Sunday-specific window text (e.g. `07:00 - 21:00 (Sun: 09:00 - 13:00)`).
- After Task 8's Render redeploy, confirm the next MocDoc connector poll for Accumax Diagnostics completes without crashing (check Report Automation / Report Connector status in the admin dashboard, and confirm no new `⚠️ MocDoc Connector Crashed` WhatsApp alert arrives).

---

## Self-Review

**Issue coverage:**
- Sunday collection hours → Tasks 1, 2, 3, part of Task 9's manual verification.
- CSV import rejecting the real ACCUMAX export → Tasks 4, 5.
- "Lab Tests" → "Lab Tests / Services" dashboard label → Task 6.
- MocDoc Playwright/Chromium crash on Render → Tasks 7 (defense-in-depth error message), 8 (actual root-cause fix).

**Placeholder scan:** No "TBD"/"implement later" — every step has literal code or an explicit manual-action checklist where automation isn't possible (Render dashboard settings, live CSV upload, live WhatsApp booking).

**Type/signature consistency:** `format_collection_window(window: dict) -> str` defined in Task 2, consumed identically in Task 2's own conversation.py edit. `friendly_browser_launch_error(exc: Exception) -> str` defined in Task 7, consumed identically at both call sites in the same task. `_normalize_csv_headers(fieldnames: list[str]) -> dict[str, str]` defined and consumed within Task 4 only. No cross-task signature drift.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-21-diagnostic-admin-panel-fixes.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
