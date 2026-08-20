# Diagnostic Center Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three confirmed production gaps in the already-shipped Diagnostic Center admin panel (commit `118a9fa`): an unreachable organization-profile page, an undeployed connector automation scheduler, and a defined-but-unenforced RBAC permission — plus two small operational conveniences.

**Architecture:** No new subsystems. Every change either removes a frontend gate, widens an existing FastAPI dependency, adds two thin endpoints that call the already-built `run_connector()`, or stands up a second Render process running code that already exists. No schema changes.

**Tech Stack:** FastAPI + Supabase (Python), vanilla JS admin SPA (`admin/index.html`), Render (Docker-based deploy), pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-diagnostic-center-product-refinement-audit.md` (this plan implements §6 Gaps 1–4, §21, §22, and the P0/P1/P2 items in §26). Deployment topology decided by user: **separate Render worker service** (§Risks/Unknowns item 1, now resolved).

## Global Constraints

- Do not touch any Clinic/Hospital/Polyclinic-visible nav item, endpoint, or behavior — every change here is either `diagstream`-only or purely additive to staff RBAC.
- `render.yaml` is deliberately gitignored (commit `27d2b6e` removed it in favor of dashboard-configured Render services) — do not recreate it. The worker-service task in this plan is a Render dashboard operation, not a committed file.
- No new dependencies, no new tables/columns — everything needed already exists in the codebase per the audit.
- Follow existing patterns exactly: `enforce_clinic_access()` / `enforce_branch_scope()` on every new endpoint, `require_permission(...)` (not `require_admin`) wherever staff delegation should apply, `_mask_connector()` on any connector data returned to the client.

---

### Task 1: Un-gate Hospital Profile for diagnostic centers

**Files:**
- Modify: `admin/index.html:800`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new — this only changes which plans see an existing, unmodified nav link and page.

- [ ] **Step 1: Remove the feature gate**

Change:
```html
<div class="nav-link" tabindex="0" data-page="profile" data-feature="booking" data-role="admin" onclick="go('profile',this)">
```
to:
```html
<div class="nav-link" tabindex="0" data-page="profile" data-role="admin" onclick="go('profile',this)">
```
(Drop `data-feature="booking"` only. Keep `data-role="admin"` — profile editing stays admin-only for every plan, unchanged.)

- [ ] **Step 2: Verify no other gate hides it**

Run: `grep -n 'data-page="profile"' admin/index.html`
Expected: exactly one match, the line from Step 1, with no `data-feature` attribute left on it.

- [ ] **Step 3: Manual check**

Since there is no JS test harness for `admin/index.html` in this repo, verify by inspection: `applyFeatureVisibility()` (`admin/index.html:1957`) hides any `[data-feature]` element whose feature isn't in `myFeatures`. With the attribute removed, this element is skipped by that `querySelectorAll('[data-feature]')` loop entirely and is visible unconditionally (subject only to the existing `data-role="admin"` check). Confirm by reading `applyFeatureVisibility()` — no live server needed for this step.

- [ ] **Step 4: Commit**

```bash
git add admin/index.html
git commit -m "fix(admin): un-gate Hospital Profile nav for diagnostic center plan

Profile held the only UI path to set org name/address/Maps link/emergency
phone, but was gated on the 'booking' feature which diagstream doesn't have —
leaving diagnostic centers with no way to configure fields their own lab
report messages render."
```

---

### Task 2: Enforce `CONNECTOR_MANAGE` on connector CRUD endpoints

**Files:**
- Modify: `app/routers/admin.py:2368` (`get_connectors`), `:2394` (`upsert_connector_credentials`), `:2497` (`toggle_connector`)
- Test: `tests/test_admin_connectors.py`

**Interfaces:**
- Consumes: `require_permission` from `app.services.permissions` (already imported in `admin.py` — confirm via `grep -n "from app.services.permissions import" app/routers/admin.py` before editing; add the import if `require_permission` isn't already in it).
- Produces: no signature change to any of the three functions — only the `Depends(...)` default for the `user` parameter changes.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_admin_connectors.py`:
```python
@pytest.mark.asyncio
async def test_get_connectors_allows_diagnostic_operator_staff():
    """A staff account with CONNECTOR_MANAGE (e.g. DIAGNOSTIC_OPERATOR role)
    must be able to list connectors — require_admin previously 403'd every
    staff account unconditionally, making the CONNECTOR_MANAGE grant dead."""
    staff = AdminUser(
        "diag_op", role="staff", clinic_id="clinic-3", user_id="user-3",
        permissions=["REPORTS_VIEW", "REPORTS_RESOLVE", "CONNECTOR_MANAGE"],
    )
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(data=[])

    with patch("app.routers.admin.supabase", mock_sb):
        result = await get_connectors(clinic_id="default", user=staff)

    assert result == {"connectors": []}


@pytest.mark.asyncio
async def test_get_connectors_rejects_staff_without_connector_manage():
    """A staff account without CONNECTOR_MANAGE must still be rejected."""
    staff = AdminUser(
        "front_desk", role="staff", clinic_id="clinic-3", user_id="user-4",
        permissions=["REPORTS_VIEW"],
    )
    with pytest.raises(HTTPException) as exc:
        await get_connectors(clinic_id="default", user=staff)
    assert exc.value.status_code == 403
```

Note: these call `get_connectors(...)` directly (as every other test in this file does), so they exercise the function body, not FastAPI's `Depends` resolution. That's fine for confirming the function signature accepts a permission-bearing staff `AdminUser` — but the real gate being tested is the `Depends(...)` default itself, which Step 3 changes. Add one more test that calls the dependency factory directly to prove the wiring:
```python
from app.services.permissions import require_permission

@pytest.mark.asyncio
async def test_connector_manage_dependency_rejects_missing_permission():
    dep = require_permission("CONNECTOR_MANAGE")
    staff = AdminUser("x", role="staff", clinic_id="clinic-3", user_id="user-5", permissions=[])
    with pytest.raises(HTTPException) as exc:
        await dep(user=staff)
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Run tests to verify the dependency-wiring test passes today (it tests `permissions.py` directly, already correct) but note `get_connectors` itself is not yet reachable by `staff` role**

Run: `pytest tests/test_admin_connectors.py -v -k "connector_manage or diagnostic_operator"`
Expected: the two new endpoint-level tests are not yet meaningfully exercising the fix (they call the function directly, bypassing `Depends`), so they'll pass regardless — this task's real verification is Step 4's `require_admin` string search. Proceed to Step 3.

- [ ] **Step 3: Change the three `Depends` defaults**

In `app/routers/admin.py`, at each of the three locations, replace:
```python
user: AdminUser = Depends(require_admin),
```
with:
```python
user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
```
for `get_connectors` (`:2372`), `upsert_connector_credentials` (`:2399`), and `toggle_connector` (`:2501`). Add `require_permission` to the existing `from app.services.permissions import ...` line at the top of `admin.py` if it isn't already imported (check first — `require_permission` is used elsewhere in the file for the report endpoints, so it is almost certainly already imported).

- [ ] **Step 4: Verify no other connector endpoint was missed**

Run: `grep -n "Depends(require_admin)" app/routers/admin.py | grep -i connector`
Expected: no output (all three now use `require_permission`). Then run: `grep -n "Depends(require_admin)" app/routers/admin.py` and manually confirm every remaining match is a genuinely admin-only endpoint (profile save, audit logs, staff account creation, etc.) — not a connector one.

- [ ] **Step 5: Run the full connector test suite**

Run: `pytest tests/test_admin_connectors.py tests/test_permissions.py tests/test_diagnostic_feature_gating.py -v`
Expected: all pass, including the new tests from Step 1.

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin.py tests/test_admin_connectors.py
git commit -m "fix(rbac): enforce CONNECTOR_MANAGE on connector endpoints

CONNECTOR_MANAGE was defined and granted to the DIAGNOSTIC_OPERATOR role
preset but every connector CRUD endpoint still used require_admin, which
unconditionally 403s every staff account — making the grant unusable."
```

---

### Task 3: Add connector Test-Connection and Run-Now endpoints

**Files:**
- Modify: `app/routers/admin.py` (add two endpoints near the existing connector routes, after `toggle_connector`, before `get_connector_audit_log`)
- Test: `tests/test_admin_connectors.py`

**Interfaces:**
- Consumes: `connectors.runner.run_connector(clinic_id: str, connector_type: str = "mocdoc", dry_run: bool = False, limit: int = 0, vam_id_filter: str = "", branch_id: Optional[str] = None) -> dict` (already exists, returns the `summary` dict documented in `connectors/runner.py:254-262`).
- Produces: `POST /admin/connectors/{connector_id}/test -> {"success": bool, "result": dict}`, `POST /admin/connectors/{connector_id}/run-now -> {"success": bool, "result": dict}`. Both importable from `app.routers.admin` as `test_connector` and `run_connector_now` for tests.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_admin_connectors.py`:
```python
@pytest.mark.asyncio
async def test_test_connector_calls_dry_run():
    from app.routers.admin import test_connector

    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "conn-1", "clinic_id": "clinic-2", "branch_id": None, "connector_type": "mocdoc"}]
    )

    fake_summary = {"run_status": "dry_run", "reports_found": 3, "error_message": None}

    with patch("app.routers.admin.supabase", mock_sb), \
         patch("connectors.runner.run_connector", new_callable=AsyncMock, return_value=fake_summary) as mock_run:
        result = await test_connector(connector_id="conn-1", clinic_id="default", user=admin)

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["dry_run"] is True
    assert result["result"]["reports_found"] == 3


@pytest.mark.asyncio
async def test_run_connector_now_calls_run_connector_not_dry_run():
    from app.routers.admin import run_connector_now

    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "conn-1", "clinic_id": "clinic-2", "branch_id": None, "connector_type": "mocdoc"}]
    )

    fake_summary = {"run_status": "success", "reports_uploaded": 2, "error_message": None}

    with patch("app.routers.admin.supabase", mock_sb), \
         patch("connectors.runner.run_connector", new_callable=AsyncMock, return_value=fake_summary) as mock_run:
        result = await run_connector_now(connector_id="conn-1", clinic_id="default", user=admin)

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["dry_run"] is False
    assert result["result"]["reports_uploaded"] == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_admin_connectors.py -v -k "test_connector or run_connector_now"`
Expected: `ImportError` — `test_connector` / `run_connector_now` don't exist yet in `app.routers.admin`.

- [ ] **Step 3: Implement the two endpoints**

Add to `app/routers/admin.py`, after `toggle_connector`:
```python
async def _load_connector_for_action(connector_id: str, user: "AdminUser", clinic_id: str) -> dict:
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    row = (
        supabase.table("integration_connectors")
        .select("*")
        .eq("id", connector_id)
        .execute()
    )
    if not row.data:
        raise HTTPException(status_code=404, detail="Connector not found")
    connector = row.data[0]
    enforce_clinic_access(user, connector["clinic_id"])
    enforce_branch_scope(user, connector.get("branch_id"))
    return connector


@router.post("/connectors/{connector_id}/test")
async def test_connector(
    connector_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
    """Dry-run the connector: authenticate and parse only, no downloads or sends."""
    connector = await _load_connector_for_action(connector_id, user, clinic_id)

    from connectors.runner import run_connector

    try:
        result = await run_connector(
            clinic_id=connector["clinic_id"],
            connector_type=connector.get("connector_type", "mocdoc"),
            dry_run=True,
            branch_id=connector.get("branch_id"),
        )
    except Exception as e:
        logger.error(f"Connector test run failed for {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Test run failed: {str(e)}")

    return {"success": result.get("run_status") in ("dry_run", "success"), "result": result}


@router.post("/connectors/{connector_id}/run-now")
async def run_connector_now(
    connector_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
    """Trigger one full connector poll cycle immediately, outside the scheduler."""
    connector = await _load_connector_for_action(connector_id, user, clinic_id)

    from connectors.runner import run_connector as run_connector_cycle

    try:
        result = await run_connector_cycle(
            clinic_id=connector["clinic_id"],
            connector_type=connector.get("connector_type", "mocdoc"),
            dry_run=False,
            branch_id=connector.get("branch_id"),
        )
    except Exception as e:
        logger.error(f"Manual connector run failed for {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Run failed: {str(e)}")

    return {"success": result.get("run_status") in ("success", "partial"), "result": result}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_admin_connectors.py -v -k "test_connector or run_connector_now"`
Expected: PASS.

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `pytest tests/ -v`
Expected: all existing tests still pass; only new tests added.

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin.py tests/test_admin_connectors.py
git commit -m "feat(connectors): add on-demand Test Connection and Run Now endpoints

Reuses the existing run_connector(dry_run=...) function — no new
automation logic, just exposes what already worked from the CLI."
```

---

### Task 4: Surface next-run timing on the diagnostic stats endpoint

**Files:**
- Modify: `app/routers/admin.py:2858-2872` (`get_diagnostic_stats`, `connector_info` block)
- Test: `tests/test_diagnostic_admin_queue.py`

**Interfaces:**
- Consumes: `poll_interval_minutes` already present in `integration_connectors.config` (JSONB); default to `10` if absent (matches `run_connector.py`'s scheduler interval).
- Produces: `connector_info` dict gains two new keys: `"poll_interval_minutes": int`, `"next_run_at": Optional[str]` (ISO timestamp, `None` if `last_run_at` is null).

- [ ] **Step 1: Write the failing test**

Extend `test_get_diagnostic_stats` in `tests/test_diagnostic_admin_queue.py` — add to the mocked `integration_connectors` row:
```python
"config": {"poll_interval_minutes": 10},
```
and add assertions after the existing ones:
```python
    assert stats["connector"]["poll_interval_minutes"] == 10
    assert stats["connector"]["next_run_at"] is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_diagnostic_admin_queue.py -v -k test_get_diagnostic_stats`
Expected: `KeyError` or `AssertionError` — the new keys don't exist yet.

- [ ] **Step 3: Implement**

In `get_diagnostic_stats`, inside the `if connectors:` block (`admin.py:2859-2872`), add:
```python
            poll_minutes = (c.get("config") or {}).get("poll_interval_minutes", 10)
            last_run_at = c.get("last_run_at")
            next_run_at = None
            if last_run_at:
                try:
                    dt = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
                    next_run_at = (dt + timedelta(minutes=poll_minutes)).isoformat()
                except Exception:
                    next_run_at = None
            connector_info = {
                "id": c.get("id"),
                "connector_type": c.get("connector_type"),
                "is_enabled": is_enabled,
                "last_run_at": last_run_at,
                "last_success_at": c.get("last_success_at"),
                "last_error": last_error,
                "health": health,
                "poll_interval_minutes": poll_minutes,
                "next_run_at": next_run_at,
            }
```
(Replaces the existing `connector_info = {...}` block — same fields plus the two new ones. `timedelta` is already imported in `admin.py` per the existing `retention_cutoff` line.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_diagnostic_admin_queue.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routers/admin.py tests/test_diagnostic_admin_queue.py
git commit -m "feat(diagnostic-dashboard): surface next scheduled connector run time"
```

---

### Task 5: Frontend — Test Connection / Run Now buttons and Next Run display

**Files:**
- Modify: `admin/index.html` (`pg-connectors` card ~line 1527, `diagStatusStrip` ~line 949, `loadDiagnosticDashboard()` ~line 3959-3985)

**Interfaces:**
- Consumes: `POST /admin/connectors/{id}/test`, `POST /admin/connectors/{id}/run-now` (Task 3), `stats.connector.next_run_at` / `stats.connector.poll_interval_minutes` (Task 4).
- Produces: nothing consumed elsewhere.

- [ ] **Step 1: Add buttons to the connector credentials card**

In `admin/index.html`, after the "Save Credentials" button (~line 1527):
```html
                <button class="btn btn-accent" onclick="saveConnectorCredentials()">Save Credentials</button>
                <button class="btn btn-ghost" onclick="testConnector()" id="btn-connTest">🔍 Test Connection</button>
                <button class="btn btn-ghost" onclick="runConnectorNow()" id="btn-connRunNow">▶️ Run Now</button>
```
Add the two handler functions near `saveConnectorCredentials()` (same script section):
```javascript
async function testConnector() {
    const id = _currentConnectorId; // set by loadConnectorsPage() when the row is loaded
    if (!id) { toast('Save credentials first, then test.', true); return; }
    const btn = document.getElementById('btn-connTest');
    btn.disabled = true; btn.textContent = 'Testing...';
    try {
        const res = await apiPost(`/admin/connectors/${id}/test`, {});
        const r = res.result || {};
        toast(res.success ? `✅ Login OK — ${r.reports_found || 0} report(s) found` : `⚠️ ${r.error_message || 'Test failed'}`, !res.success);
    } catch (e) {
        toast('Error: ' + e.message, true);
    } finally {
        btn.disabled = false; btn.textContent = '🔍 Test Connection';
    }
}

async function runConnectorNow() {
    const id = _currentConnectorId;
    if (!id) { toast('Save credentials first.', true); return; }
    const ok = await confirmDialog('Run the connector now instead of waiting for the next scheduled poll?', { okText: 'Run Now' });
    if (!ok) return;
    const btn = document.getElementById('btn-connRunNow');
    btn.disabled = true; btn.textContent = 'Running...';
    try {
        const res = await apiPost(`/admin/connectors/${id}/run-now`, {});
        const r = res.result || {};
        toast(res.success ? `✅ Run complete — ${r.reports_uploaded || 0} uploaded, ${r.reports_failed || 0} failed` : `⚠️ ${r.error_message || 'Run failed'}`, !res.success);
        loadConnectorsPage();
    } catch (e) {
        toast('Error: ' + e.message, true);
    } finally {
        btn.disabled = false; btn.textContent = '▶️ Run Now';
    }
}
```
Note: `_currentConnectorId` must already be tracked by the existing `loadConnectorsPage()` function (it populates the credentials form when a connector row exists) — locate where it sets `f-connUsername` etc. and set `_currentConnectorId = data.connectors[0].id` (or per-branch equivalent) at the same point. If no such tracking variable currently exists, add `let _currentConnectorId = null;` near the top of the connectors-page script block and set it there.

- [ ] **Step 2: Add "Next run" to the dashboard status strip**

In `loadDiagnosticDashboard()` (`admin/index.html`, inside the `if (conn) {` block after the `statusSub.textContent = ...` line):
```javascript
            if (conn.next_run_at) {
                const nextRun = new Date(conn.next_run_at);
                const mins = Math.max(0, Math.round((nextRun - new Date()) / 60000));
                statusSub.textContent += ` · Next run in ~${mins}m`;
            }
```

- [ ] **Step 3: Manual verification**

Run: `grep -n "testConnector\|runConnectorNow\|_currentConnectorId\|next_run_at" admin/index.html`
Expected: matches for the new function definitions, the two button `onclick` handlers, and the dashboard status-strip addition — no orphaned references (every `onclick="testConnector()"` etc. must have a matching function definition in the same file).

- [ ] **Step 4: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): add Test Connection / Run Now buttons and next-run display"
```

---

### Task 6: Deploy the connector runner as a separate Render worker service

**This is a Render dashboard operation, not a code change** — `render.yaml` stays gitignored per commit `27d2b6e`'s deliberate decision to configure Render through its dashboard rather than a committed blueprint. Nothing in this task touches git.

- [ ] **Step 1: Create the second Render service**

In the Render dashboard, on the same project as the existing `kriya-ai` web service:
- New → Background Worker (not Web Service — no HTTP port needed, avoids the health-check requirement a Web Service imposes).
- Same GitHub repo, same branch, same `Dockerfile` (Render will reuse the existing Docker build — no new Dockerfile needed since `CMD` is overridden per-service, not baked in).
- **Start Command override:** `python -m connectors.runner --all`
- **Environment variables:** copy every env var from the existing web service (`WHATSAPP_TOKEN`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `CONNECTOR_ENCRYPTION_KEY`, `GROQ_API_KEY`, etc.) — the runner process needs the same Supabase/WhatsApp/encryption credentials as the web process. `MEDASSIST_URL` should point at the web service's public Render URL (not `localhost`), since `run_connector()` calls `settings.integration_secret`-authenticated endpoints on the live web service (`connectors/runner.py:317-321`).
- Plan tier: starter is sufficient — Playwright/Chromium runs intermittently (every 10 min), not continuously.

- [ ] **Step 2: Verify it started correctly**

In the new service's Render logs, confirm the line: `"Connector runner started in scheduled mode. Polling every 10 minutes."` (from `connectors/runner.py:678-681`), and that the very first `run_all_connectors()` invocation (fired immediately on startup per `runner.py:684-685`) completes without an unhandled exception in the logs.

- [ ] **Step 3: Verify against the database**

For any clinic with an enabled `mocdoc` connector, confirm `integration_connectors.last_run_at` updates roughly every 10 minutes (query via Supabase dashboard or `psql`). This is the concrete signal that Gap 2 from the audit is closed — automation is now actually running, not just built.

- [ ] **Step 4: No commit needed** — this task is infrastructure-only.

---

## Self-Review

**Spec coverage:** Task 1 → §6 Gap 1 / §21.1. Task 2 → §6 Gap 3 / §22.2. Task 3 → §6 Gap 4 / §11 / §22.1. Task 4 → §8 / §21.3 / §22.3. Task 5 → §21.2/§21.3. Task 6 → §6 Gap 2 / §22.4 / Risks item 1. All P0/P1/P2 items from the audit's §26 are covered; P3 items are explicitly excluded per the audit, not silently dropped.

**Placeholder scan:** No TBDs. Every step has runnable code or an exact grep/manual-check command.

**Type consistency:** `run_connector(...)` signature used in Tasks 3 and 6 matches `connectors/runner.py:230-237` exactly (`clinic_id`, `connector_type`, `dry_run`, `limit`, `vam_id_filter`, `branch_id`). `AdminUser(username, role=..., clinic_id=..., user_id=..., permissions=...)` constructor usage in new tests matches the pattern already used throughout `tests/test_diagnostic_admin_queue.py` and `tests/test_admin_connectors.py`.

**Task independence:** Tasks 1, 2, 4, 6 are fully independent of each other. Task 3 depends on nothing new. Task 5 depends on Task 3 (endpoint URLs) and Task 4 (`next_run_at` field) — do Task 5 last.
