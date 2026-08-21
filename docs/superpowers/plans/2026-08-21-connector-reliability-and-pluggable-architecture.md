# Connector Reliability & Pluggable Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the diagnostic center's Report Connector (MocDoc) admin panel reliable in production — Test Connection stops failing on stale locks, Run History loads, dry-run results show real login/scrape evidence — and add the minimal groundwork (schema + registry endpoint) so a second HMIS connector never requires admin-frontend changes.

**Architecture:** Fix the root-cause cascade at its source (Dockerfile Chromium cache path → stale locks → RBAC gap) rather than patching each symptom. Add a real scheduled-polling worker service so connector runs stop being purely admin-triggered. Extend the existing `HospitalConnector` ABC / `CONNECTOR_REGISTRY` plugin pattern (already correct) into the admin UI via a `CONFIG_SCHEMA` class attribute and a schema-serving endpoint, so the frontend renders credential forms generically instead of hardcoding MocDoc field names.

**Tech Stack:** Python 3.11, FastAPI, Supabase (PostgREST), Playwright (Chromium), APScheduler, Render.com (Docker), vanilla JS admin panel (no build step, no JS test framework).

**Spec:** `docs/superpowers/specs/2026-08-21-connector-reliability-and-pluggable-architecture-design.md`

## Global Constraints

- Lock lease is 5 minutes (was 15) — `connectors/runner.py:acquire_connector_lock`.
- Dry-run sample is capped at the first 5 parsed rows, shape `{patient_name_masked, vam_id, report_name}` — no new scraping, reuses rows already parsed in memory.
- `CONFIG_SCHEMA` entries are `{key, label, type, placeholder, required}` — this exact key set, nothing more.
- Do NOT build a second real connector (Practo, Birlamedisoft, etc.) — schema/registry groundwork only, per explicit user instruction.
- Do NOT change the credential encryption scheme (Fernet) or multi-branch connector UX (migration 025) — out of scope.
- All new failure paths degrade to the existing generic error surfaces (`error_message` string, toast) — no new silent failures.
- The admin panel has no JS test framework (no `package.json`, no test runner) — frontend tasks are verified by manual browser check, not an automated test. Backend Python changes always get a `pytest` test.

---

### Task 1: Dockerfile — fix Playwright Chromium cache path

**Files:**
- Modify: `Dockerfile:21-24`
- Test: Create `tests/test_dockerfile_browser_path.py`

**Interfaces:**
- Produces: no code interface — this is a build-config regression guard other tasks don't depend on.

**Context:** `playwright install --with-deps chromium` runs as `root` (line 24) before `USER appuser` (line 35) is set. Playwright's default browser cache path is under `$HOME`, so root's install lands in `/root/.cache/ms-playwright`, invisible to `appuser` at runtime. Every cold boot silently re-downloads Chromium via the runtime fallback in `app/utils/browser_errors.py`, adding 1-3 minutes of non-determinism and raising the odds a Render redeploy kills a connector run mid-login (which leaves the stale lock fixed in Task 3-4).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dockerfile_browser_path.py
"""Regression guard for the Playwright Chromium cache-path bug: the browser
must be installed to a path that's the same for the root build-time user
and the appuser runtime user, or every cold boot re-downloads Chromium."""

from pathlib import Path

DOCKERFILE = Path(__file__).parent.parent / "Dockerfile"


def test_playwright_browsers_path_set_before_install_and_absolute():
    lines = DOCKERFILE.read_text().splitlines()

    env_line_idx = next(
        (i for i, l in enumerate(lines) if "PLAYWRIGHT_BROWSERS_PATH" in l and l.strip().startswith("ENV")),
        None,
    )
    install_line_idx = next(
        (i for i, l in enumerate(lines) if "playwright install" in l),
        None,
    )
    user_line_idx = next(
        (i for i, l in enumerate(lines) if l.strip().startswith("USER ")),
        None,
    )

    assert env_line_idx is not None, "Dockerfile must set ENV PLAYWRIGHT_BROWSERS_PATH"
    assert install_line_idx is not None, "Dockerfile must run playwright install"
    assert user_line_idx is not None, "Dockerfile must switch to a non-root USER"

    # Must be set before both the install (build-time/root) and the USER
    # switch (so appuser's runtime env matches what root installed to).
    assert env_line_idx < install_line_idx
    assert env_line_idx < user_line_idx

    path_value = lines[env_line_idx].split("PLAYWRIGHT_BROWSERS_PATH", 1)[1].strip().lstrip("=").strip()
    assert path_value.startswith("/"), "Path must be absolute, not $HOME-relative"
    assert "$HOME" not in path_value and "~" not in path_value
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dockerfile_browser_path.py -v`
Expected: FAIL — `assert env_line_idx is not None` (no `PLAYWRIGHT_BROWSERS_PATH` line exists yet)

- [ ] **Step 3: Fix the Dockerfile**

```dockerfile
# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright's default cache dir is under $HOME, which differs between the
# root user (build-time RUN) and appuser (runtime USER below). Pinning an
# absolute path here makes both resolve to the same directory, so the
# browser appuser installed at build time is the one it finds at runtime —
# no more per-boot Chromium re-download.
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright
RUN playwright install --with-deps chromium
```

Replace the current lines 22-24 in `Dockerfile` with the block above.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_dockerfile_browser_path.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add Dockerfile tests/test_dockerfile_browser_path.py
git commit -m "fix: pin PLAYWRIGHT_BROWSERS_PATH so appuser sees the build-time Chromium install"
```

---

### Task 2: render.yaml — add scheduled-polling worker service

**Files:**
- Modify: `render.yaml`
- Test: Create `tests/test_render_yaml.py`

**Interfaces:**
- Consumes: `connectors/runner.py`'s existing `main()` / `--all` flag (already implemented, untouched by this task).
- Produces: no code interface.

**Context:** `connectors/runner.py --all` (scheduled polling every 10 minutes, already fully implemented) is never invoked in production — `render.yaml` declares only one `web` service. This is the approved "Automatic Background Sync." Note for whoever applies this: pushing this change adds a second, separately-billed Render service — confirm with the user before deploying to production if that wasn't already understood as part of this change.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render_yaml.py
"""Regression guard: a worker service must run scheduled connector polling
in production, or every connector run stays purely admin-triggered."""

from pathlib import Path
import yaml

RENDER_YAML = Path(__file__).parent.parent / "render.yaml"


def test_render_yaml_has_connector_polling_worker():
    config = yaml.safe_load(RENDER_YAML.read_text())
    services = config.get("services", [])

    worker = next((s for s in services if s.get("type") == "worker"), None)
    assert worker is not None, "render.yaml must declare a worker service for scheduled connector polling"
    assert worker.get("dockerCommand") == "python -m connectors.runner --all"
    assert worker.get("dockerfilePath") == "./Dockerfile"

    web = next((s for s in services if s.get("type") == "web"), None)
    assert web is not None, "existing web service must still be declared"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_render_yaml.py -v`
Expected: FAIL — `assert worker is not None`

- [ ] **Step 3: Add the worker service**

```yaml
services:
  - type: web
    name: mediassist-ai
    env: docker
    dockerfilePath: ./Dockerfile
    healthCheckPath: /health
    autoDeploy: true

  - type: worker
    name: mediassist-connector-worker
    env: docker
    dockerfilePath: ./Dockerfile
    dockerCommand: python -m connectors.runner --all
    autoDeploy: true
```

Replace the full contents of `render.yaml` with the block above.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_render_yaml.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add render.yaml tests/test_render_yaml.py
git commit -m "feat: add Render worker service for scheduled connector polling"
```

---

### Task 3: Shorten lock lease to 5 minutes and surface remaining TTL

**Files:**
- Modify: `connectors/runner.py:72-115` (`acquire_connector_lock`, `release_connector_lock`), `connectors/runner.py:329-335` (lock-fail branch in `run_connector`)
- Test: Create `tests/test_connector_runner.py`

**Interfaces:**
- Produces: `acquire_connector_lock(connector_id: str, worker_id: str = "worker-1") -> tuple[bool, int]` — `(acquired, remaining_minutes)`; `remaining_minutes` is `0` when acquired, else the lock's remaining TTL in whole minutes (rounded up, minimum 1). `release_connector_lock(connector_id: str) -> None` — unchanged return type, now also untracks the connector from the module-level `_locks_held_by_this_process` set (consumed by Task 4).
- Consumes: nothing new.

**Context:** The 15-minute lease is why a single killed run blocks every subsequent Test Connection attempt for up to 15 minutes — the literal `Connector is currently locked by another worker` error the user reported. `acquire_connector_lock`/`release_connector_lock` have exactly one caller each, both in `run_connector` (confirmed via repo-wide search) — safe to change the return signature.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_connector_runner.py
"""Tests for connectors/runner.py's distributed advisory lock: acquire,
deny-with-remaining-TTL, expiry, and release."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_supabase_with_locked_at(locked_at_iso):
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "conn-1", "locked_at": locked_at_iso}]
    )
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    return mock_sb, mock_table


@pytest.mark.asyncio
async def test_acquire_lock_granted_when_no_existing_lock():
    from connectors.runner import acquire_connector_lock

    mock_sb, mock_table = _mock_supabase_with_locked_at(None)
    with patch("connectors.runner.supabase", mock_sb):
        acquired, remaining = await acquire_connector_lock("conn-1")

    assert acquired is True
    assert remaining == 0
    mock_table.update.assert_called_once()


@pytest.mark.asyncio
async def test_acquire_lock_denied_with_remaining_ttl_when_recently_locked():
    from connectors.runner import acquire_connector_lock

    locked_two_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    mock_sb, mock_table = _mock_supabase_with_locked_at(locked_two_min_ago)
    with patch("connectors.runner.supabase", mock_sb):
        acquired, remaining = await acquire_connector_lock("conn-1")

    assert acquired is False
    assert remaining == 3  # 5-minute lease minus ~2 elapsed, rounded up
    mock_table.update.assert_not_called()


@pytest.mark.asyncio
async def test_acquire_lock_granted_after_ttl_expires():
    from connectors.runner import acquire_connector_lock

    locked_six_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    mock_sb, mock_table = _mock_supabase_with_locked_at(locked_six_min_ago)
    with patch("connectors.runner.supabase", mock_sb):
        acquired, remaining = await acquire_connector_lock("conn-1")

    assert acquired is True
    assert remaining == 0


@pytest.mark.asyncio
async def test_release_connector_lock_clears_fields_and_untracks():
    from connectors.runner import acquire_connector_lock, release_connector_lock, _locks_held_by_this_process

    mock_sb, mock_table = _mock_supabase_with_locked_at(None)
    with patch("connectors.runner.supabase", mock_sb):
        await acquire_connector_lock("conn-1")
        assert "conn-1" in _locks_held_by_this_process

        await release_connector_lock("conn-1")

    update_call = mock_table.update.call_args_list[-1][0][0]
    assert update_call == {"locked_at": None, "locked_by": None}
    assert "conn-1" not in _locks_held_by_this_process
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_connector_runner.py -v`
Expected: FAIL — `acquire_connector_lock` currently returns a plain `bool`, so `acquired, remaining = await acquire_connector_lock(...)` raises `TypeError: cannot unpack non-iterable bool object`

- [ ] **Step 3: Implement the lease shortening and TTL return value**

Add `import math` to the existing import block at the top of `connectors/runner.py` (alongside the existing `import time` etc.).

Replace `connectors/runner.py:72-115` with:

```python
LOCK_LEASE = timedelta(minutes=5)

# Connector IDs this process currently holds the advisory lock for.
# Drained on graceful shutdown (see release_all_locks_held) so a killed
# process doesn't leave a stale lock blocking the next Test Connection.
_locks_held_by_this_process: set[str] = set()


async def acquire_connector_lock(connector_id: str, worker_id: str = "worker-1") -> tuple[bool, int]:
    """Acquire distributed advisory lock on connector record (5 min lease).

    Returns (acquired, remaining_minutes). remaining_minutes is 0 when
    acquired; otherwise it's the lock's remaining TTL rounded up to the
    nearest minute (minimum 1), for surfacing "retry in ~Nm" to the admin UI.
    """
    try:
        res = (
            supabase.table("integration_connectors")
            .select("id, locked_at")
            .eq("id", connector_id)
            .execute()
        )
        if not res.data:
            return False, 0
        row = res.data[0]
        locked_at = row.get("locked_at")
        if locked_at:
            try:
                dt = datetime.fromisoformat(locked_at.replace("Z", "+00:00"))
                elapsed = datetime.now(timezone.utc) - dt
                if elapsed < LOCK_LEASE:
                    remaining = max(1, math.ceil((LOCK_LEASE - elapsed).total_seconds() / 60))
                    logger.warning(
                        f"Connector {connector_id} is locked by another process (locked_at={locked_at})"
                    )
                    return False, remaining
            except Exception:
                pass

        now_str = datetime.now(timezone.utc).isoformat()
        supabase.table("integration_connectors").update({
            "locked_at": now_str,
            "locked_by": worker_id,
        }).eq("id", connector_id).execute()
        _locks_held_by_this_process.add(connector_id)
        return True, 0
    except Exception as e:
        logger.warning(f"Could not acquire lock for connector {connector_id} (proceeding): {e}")
        return True, 0


async def release_connector_lock(connector_id: str) -> None:
    """Release distributed advisory lock on connector record."""
    try:
        supabase.table("integration_connectors").update({
            "locked_at": None,
            "locked_by": None,
        }).eq("id", connector_id).execute()
    except Exception as e:
        logger.warning(f"Could not release lock for connector {connector_id}: {e}")
    finally:
        _locks_held_by_this_process.discard(connector_id)


async def release_all_locks_held() -> None:
    """Release every connector lock this process currently holds.

    Called on graceful shutdown (FastAPI lifespan, SIGTERM in scheduled
    mode) so a killed process doesn't leave a stale lock blocking the
    next Test Connection for the full lease.
    """
    for connector_id in list(_locks_held_by_this_process):
        await release_connector_lock(connector_id)
```

Then update the call site at `connectors/runner.py:329-335` (inside `run_connector`) from:

```python
        connector_id = connector_row.get("id")
        if connector_id:
            locked = await acquire_connector_lock(connector_id)
            if not locked:
                summary["run_status"] = "locked"
                summary["error_message"] = "Connector is currently locked by another worker"
                return summary
```

to:

```python
        connector_id = connector_row.get("id")
        if connector_id:
            locked, remaining = await acquire_connector_lock(connector_id)
            if not locked:
                summary["run_status"] = "locked"
                summary["error_message"] = f"Connector is busy — retry in ~{remaining}m"
                return summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_connector_runner.py -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add connectors/runner.py tests/test_connector_runner.py
git commit -m "fix: shorten connector lock lease to 5 min and surface remaining TTL in error message"
```

---

### Task 4: Release locks on graceful shutdown (FastAPI + scheduled-mode SIGTERM)

**Files:**
- Modify: `app/main.py:117-120` (lifespan shutdown), `connectors/runner.py:655-693` (`start_scheduled_mode`)
- Test: Modify `tests/test_connector_runner.py`

**Interfaces:**
- Consumes: `release_all_locks_held()` from Task 3.

**Context:** Task 3 shortens the lease and adds `release_all_locks_held()`, but nothing calls it yet. Wiring it into both processes that can hold a lock — the web service (admin-triggered Test Connection / Run Now) and the new worker service (scheduled polling) — means a graceful redeploy no longer leaves a stale lock at all, not just a shorter one.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_connector_runner.py`:

```python
def test_lifespan_shutdown_calls_release_all_locks_held():
    """Regression guard: FastAPI's shutdown path must release any connector
    lock this process holds, or a killed web worker leaves a stale lock."""
    import inspect
    from app import main

    source = inspect.getsource(main.lifespan)
    shutdown_section = source.split("# Shutdown", 1)[1]
    assert "release_all_locks_held" in shutdown_section


def test_scheduled_mode_releases_locks_on_sigterm():
    """Regression guard: the connector worker's scheduled mode must release
    its locks on SIGTERM (the signal Render sends on redeploy/stop), not
    just on KeyboardInterrupt."""
    import inspect
    from connectors import runner

    source = inspect.getsource(runner.start_scheduled_mode)
    assert "signal.signal(signal.SIGTERM" in source
    assert "release_all_locks_held" in source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_connector_runner.py -v -k "shutdown or sigterm"`
Expected: FAIL — neither `release_all_locks_held` nor the SIGTERM handler exist in these functions yet

- [ ] **Step 3: Wire the shutdown release into `app/main.py`**

Replace `app/main.py:117-120`:

```python
    # Shutdown
    logger.info("Shutting down MediAssist AI...")
    await callmedex_container.queue_engine.shutdown()
    scheduler_service.shutdown()
```

with:

```python
    # Shutdown
    logger.info("Shutting down MediAssist AI...")
    await callmedex_container.queue_engine.shutdown()
    scheduler_service.shutdown()
    from connectors.runner import release_all_locks_held
    await release_all_locks_held()
```

- [ ] **Step 4: Wire a SIGTERM handler into `connectors/runner.py`'s scheduled mode**

Add `import signal` to the top-level imports of `connectors/runner.py` (alongside `import time`).

Replace `connectors/runner.py:655-693` (`start_scheduled_mode`):

```python
def start_scheduled_mode():
    """Start APScheduler with polling and cleanup jobs."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = AsyncIOScheduler()

    # Poll every 10 minutes
    scheduler.add_job(
        run_all_connectors,
        IntervalTrigger(minutes=10),
        id="poll_connectors",
        replace_existing=True,
    )

    # Storage cleanup daily at 2 AM
    scheduler.add_job(
        cleanup_expired_storage,
        CronTrigger(hour=2, minute=0),
        id="cleanup_storage",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Connector runner started in scheduled mode. "
        "Polling every 10 minutes. Storage cleanup daily at 2 AM."
    )

    # Run immediately on startup
    loop = asyncio.get_event_loop()
    loop.create_task(run_all_connectors())

    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down connector runner...")
        scheduler.shutdown()
```

with:

```python
def start_scheduled_mode():
    """Start APScheduler with polling and cleanup jobs."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    def _handle_sigterm(signum, frame):
        # Render sends SIGTERM on redeploy/stop. Route it through the same
        # shutdown path as Ctrl-C so in-flight locks get released below.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    scheduler = AsyncIOScheduler()

    # Poll every 10 minutes
    scheduler.add_job(
        run_all_connectors,
        IntervalTrigger(minutes=10),
        id="poll_connectors",
        replace_existing=True,
    )

    # Storage cleanup daily at 2 AM
    scheduler.add_job(
        cleanup_expired_storage,
        CronTrigger(hour=2, minute=0),
        id="cleanup_storage",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Connector runner started in scheduled mode. "
        "Polling every 10 minutes. Storage cleanup daily at 2 AM."
    )

    # Run immediately on startup
    loop = asyncio.get_event_loop()
    loop.create_task(run_all_connectors())

    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down connector runner...")
        scheduler.shutdown()
        loop.run_until_complete(release_all_locks_held())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_connector_runner.py -v`
Expected: PASS (all 6 tests from Tasks 3-4)

- [ ] **Step 6: Commit**

```bash
git add app/main.py connectors/runner.py tests/test_connector_runner.py
git commit -m "fix: release connector locks on graceful shutdown (FastAPI lifespan + worker SIGTERM)"
```

---

### Task 5: RBAC fix — audit-log endpoint must accept CONNECTOR_MANAGE staff

**Files:**
- Modify: `app/routers/admin.py:3085-3089`
- Test: Modify `tests/test_admin_connectors.py`

**Interfaces:**
- Consumes: `require_permission` from `app/services/permissions.py` (already imported at `app/routers/admin.py:42`).

**Context:** `GET /admin/connectors/{id}/audit-log` uses `require_admin`, which rejects `role == "staff"` unconditionally, while every sibling `/admin/connectors/*` endpoint uses `require_permission("CONNECTOR_MANAGE")`. A `DIAGNOSTIC_OPERATOR` staff account — the role this whole panel is built for — holds `CONNECTOR_MANAGE` but gets 403'd loading run history. This is the direct cause of the reported "Failed to load run history."

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_admin_connectors.py`:

```python
def test_audit_log_uses_connector_manage_permission_not_require_admin():
    """Regression guard: the audit-log route's dependency must match every
    other /admin/connectors/* endpoint (require_permission), not
    require_admin, which 403s every staff account unconditionally."""
    import inspect
    from app.routers.admin import get_connector_audit_log

    source = inspect.getsource(get_connector_audit_log)
    assert "require_admin" not in source
    assert 'require_permission("CONNECTOR_MANAGE")' in source


@pytest.mark.asyncio
async def test_audit_log_allows_diagnostic_operator_staff():
    """A staff account with CONNECTOR_MANAGE (e.g. DIAGNOSTIC_OPERATOR role)
    must be able to load run history — this was 403ing before the fix."""
    from app.routers.admin import get_connector_audit_log

    staff = AdminUser(
        "diag_op", role="staff", clinic_id="clinic-3", user_id="user-3",
        permissions=["REPORTS_VIEW", "REPORTS_RESOLVE", "CONNECTOR_MANAGE"],
    )
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
        data={"clinic_id": "clinic-3", "connector_type": "mocdoc", "branch_id": None}
    )
    mock_table.select.return_value.eq.return_value.eq.return_value.is_.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"id": "log-1", "run_status": "success", "reports_found": 2}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        result = await get_connector_audit_log(connector_id="conn-1", limit=20, user=staff)

    assert result["audit_log"][0]["id"] == "log-1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_admin_connectors.py -v -k audit_log_uses_connector_manage or audit_log_allows_diagnostic_operator`
Expected: FAIL — `"require_admin" not in source` fails because the endpoint still uses `require_admin`

- [ ] **Step 3: Fix the dependency**

In `app/routers/admin.py`, replace `3085-3089`:

```python
@router.get("/connectors/{connector_id}/audit-log")
async def get_connector_audit_log(
    connector_id: str,
    limit: int = 20,
    user: AdminUser = Depends(require_admin),
):
```

with:

```python
@router.get("/connectors/{connector_id}/audit-log")
async def get_connector_audit_log(
    connector_id: str,
    limit: int = 20,
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_admin_connectors.py -v`
Expected: PASS (full file, including the pre-existing `test_audit_log_cross_tenant_forbidden`, unaffected by this change)

- [ ] **Step 5: Commit**

```bash
git add app/routers/admin.py tests/test_admin_connectors.py
git commit -m "fix: audit-log endpoint requires CONNECTOR_MANAGE permission, not admin role"
```

---

### Task 6: Richer dry-run results — masked sample rows in the run summary

**Files:**
- Modify: `connectors/runner.py:375-382` (dry-run block), `connectors/runner.py:511-520` (audit-log insert in the `finally` block)
- Test: Modify `tests/test_connector_runner.py`

**Interfaces:**
- Produces: `run_connector(..., dry_run=True)`'s returned summary dict gains a `sample` key: `list[{"patient_name_masked": str, "vam_id": str | None, "report_name": str}]`, capped at 5 entries. This key is present only in the dict `run_connector` returns to its caller — it is NOT persisted to `connector_audit_log` (that table has no `sample` column; sending it would make every dry-run audit-log insert silently fail, which would regress Run History).
- Consumes: `ReportMetadata` fields (`patient_name`, `vam_id`, `report_name`) from `connectors/base.py`, already returned by `fetch_new_reports()`.

**Context:** Today dry-run only returns a count — nothing proves the login actually reached the reports page and parsed real rows, which is exactly what the user asked for ("logging in and checking the lab reports and list the available patients"). The sample data is already sitting in memory in `reports` before `dry_run`'s early return discards it — no new scraping needed.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_connector_runner.py`:

```python
@pytest.mark.asyncio
async def test_run_connector_dry_run_includes_masked_sample_excluded_from_audit_log():
    from connectors.runner import run_connector, CONNECTOR_REGISTRY, _mask_sample_name
    from connectors.base import ReportMetadata

    class _FakeConnector:
        def __init__(self, **kwargs):
            pass

        async def authenticate(self):
            return True

        async def fetch_new_reports(self):
            return [
                ReportMetadata(
                    patient_name=f"Patient {i}",
                    patient_phone="+919999999999",
                    report_name=f"CBC Report {i}",
                    report_type="lab",
                    external_report_id=f"ext-{i}",
                    vam_id=f"VAM-{i}",
                )
                for i in range(7)
            ]

        async def cleanup(self):
            pass

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.eq.return_value.is_.return_value.single.return_value.execute.return_value = MagicMock(
        data={
            "id": "conn-1",
            "clinic_id": "clinic-2",
            "is_enabled": True,
            "config": {"username": "labadmin", "password": "plaintext-dev-only"},
        }
    )

    with patch("connectors.runner.supabase", mock_sb), \
         patch.dict(CONNECTOR_REGISTRY, {"mocdoc": _FakeConnector}), \
         patch("connectors.runner.acquire_connector_lock", new_callable=AsyncMock, return_value=(True, 0)), \
         patch("connectors.runner.release_connector_lock", new_callable=AsyncMock):
        result = await run_connector(clinic_id="clinic-2", dry_run=True)

    assert result["run_status"] == "dry_run"
    assert len(result["sample"]) == 5
    assert result["sample"][0]["patient_name_masked"] == _mask_sample_name("Patient 0")
    assert result["sample"][0]["vam_id"] == "VAM-0"
    assert result["sample"][0]["report_name"] == "CBC Report 0"

    insert_payload = mock_table.insert.call_args[0][0]
    assert "sample" not in insert_payload
    assert insert_payload["run_status"] == "dry_run"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_connector_runner.py -v -k dry_run_includes_masked_sample`
Expected: FAIL — `ImportError: cannot import name '_mask_sample_name'` (doesn't exist yet), and `result["sample"]` is a `KeyError`

- [ ] **Step 3: Implement the masking helper, the sample field, and the audit-log exclusion**

Add this helper near `_scope_by_branch` in `connectors/runner.py` (just below it):

```python
def _mask_sample_name(name: str) -> str:
    """Mask a patient name for the dry-run sample: keep each word's first
    letter (e.g. 'John Smith' -> 'J•••• S••••'), never expose the full name."""
    if not name:
        return ""
    return " ".join(w[:1] + "•" * max(0, len(w) - 1) for w in name.split())
```

Replace the dry-run block at `connectors/runner.py:375-382`:

```python
        if dry_run:
            logger.info("=== DRY RUN MODE — No downloads or uploads ===")
            logger.info(f"=== DRY RUN RESULTS: {len(reports)} reports found ===")
            for r in reports:
                logger.info(f"  → {r}")
            summary["run_status"] = "dry_run"
            await connector.cleanup()
            return summary
```

with:

```python
        if dry_run:
            logger.info("=== DRY RUN MODE — No downloads or uploads ===")
            logger.info(f"=== DRY RUN RESULTS: {len(reports)} reports found ===")
            for r in reports:
                logger.info(f"  → {r}")
            summary["run_status"] = "dry_run"
            summary["sample"] = [
                {
                    "patient_name_masked": _mask_sample_name(r.patient_name),
                    "vam_id": r.vam_id,
                    "report_name": r.report_name,
                }
                for r in reports[:5]
            ]
            await connector.cleanup()
            return summary
```

Replace the audit-log insert at `connectors/runner.py:511-520` (inside the `finally` block):

```python
        # Save audit log
        try:
            supabase.table("connector_audit_log").insert({
                "clinic_id": clinic_id,
                "connector_type": connector_type,
                "branch_id": branch_id,
                **summary,
            }).execute()
        except Exception as e:
            logger.error(f"Failed to save audit log: {e}")
```

with:

```python
        # Save audit log. "sample" is excluded — connector_audit_log has no
        # such column, and sending it would make every dry-run insert fail.
        try:
            audit_row = {k: v for k, v in summary.items() if k != "sample"}
            supabase.table("connector_audit_log").insert({
                "clinic_id": clinic_id,
                "connector_type": connector_type,
                "branch_id": branch_id,
                **audit_row,
            }).execute()
        except Exception as e:
            logger.error(f"Failed to save audit log: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_connector_runner.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add connectors/runner.py tests/test_connector_runner.py
git commit -m "feat: dry-run results include masked sample rows without breaking audit-log inserts"
```

---

### Task 7: Admin UI — richer Test Connection result card

**Files:**
- Modify: `admin/index.html:1610-1613` (HTML), `admin/index.html:4228-4251` (`testConnector()`)

**Interfaces:**
- Consumes: `sample` field on the `/admin/connectors/{id}/test-status` response's `result` object, produced by Task 6.

**Context:** Today `testConnector()` shows only a toast with a report count. The user explicitly asked to see "logging in and checking the lab reports and list the available patients" — this renders the `sample` rows from Task 6 as a small table under the buttons, in addition to the existing toast.

- [ ] **Step 1: Add a result container to the HTML**

In `admin/index.html`, replace line 1612-1613:

```html
                <button class="btn btn-ghost" onclick="runConnectorNow()" id="btn-connRunNow">▶️ Run Now</button>
            </div>
```

with:

```html
                <button class="btn btn-ghost" onclick="runConnectorNow()" id="btn-connRunNow">▶️ Run Now</button>
                <div id="connTestResult"></div>
            </div>
```

- [ ] **Step 2: Render the result card in `testConnector()`**

Replace `admin/index.html:4228-4251`:

```javascript
async function testConnector() {
    const id = _connectorId;
    if (!id) { toast('Save credentials first, then test.', true); return; }
    const btn = document.getElementById('btn-connTest');
    btn.disabled = true; btn.textContent = '⏳ Starting test...';
    try {
        await apiPost(`/admin/connectors/${id}/test`, {});
        btn.textContent = '⏳ Testing (polling...)';
        const result = await _pollConnectorStatus(id);
        if (result.status === 'done') {
            const r = result.result || {};
            toast(result.success ? `✅ Login OK — ${r.reports_found || 0} report(s) found` : `⚠️ ${r.error_message || 'Test failed'}`, !result.success);
        } else if (result.status === 'error') {
            toast(`⚠️ ${(result.result || {}).error_message || 'Test failed'}`, true);
        } else {
            toast('⚠️ Test timed out — check logs for details', true);
        }
    } catch (e) {
        toast('Error: ' + e.message, true);
    } finally {
        btn.disabled = false; btn.textContent = '🔍 Test Connection';
        loadConnectorsPage();
    }
}
```

with:

```javascript
function _renderConnectorTestResult(r, success) {
    const statusLine = success
        ? `<strong style="color:#2e7d32">✅ Login successful</strong> — ${r.reports_found || 0} report(s) found on the portal`
        : `<strong style="color:#c62828">⚠️ ${esc(r.error_message || 'Test failed')}</strong>`;
    const sample = r.sample || [];
    let sampleHtml = '';
    if (sample.length > 0) {
        sampleHtml = '<table style="margin-top:8px;width:100%"><thead><tr><th>Patient</th><th>VAM ID</th><th>Report</th></tr></thead><tbody>' +
            sample.map(s => `<tr><td>${esc(s.patient_name_masked || '')}</td><td>${esc(s.vam_id || '—')}</td><td>${esc(s.report_name || '')}</td></tr>`).join('') +
            '</tbody></table>';
    }
    return `<div class="card" style="margin-top:12px;padding:12px">${statusLine}${sampleHtml}</div>`;
}

async function testConnector() {
    const id = _connectorId;
    if (!id) { toast('Save credentials first, then test.', true); return; }
    const btn = document.getElementById('btn-connTest');
    const resultEl = document.getElementById('connTestResult');
    resultEl.innerHTML = '';
    btn.disabled = true; btn.textContent = '⏳ Starting test...';
    try {
        await apiPost(`/admin/connectors/${id}/test`, {});
        btn.textContent = '⏳ Testing (polling...)';
        const result = await _pollConnectorStatus(id);
        if (result.status === 'done') {
            const r = result.result || {};
            toast(result.success ? `✅ Login OK — ${r.reports_found || 0} report(s) found` : `⚠️ ${r.error_message || 'Test failed'}`, !result.success);
            resultEl.innerHTML = _renderConnectorTestResult(r, result.success);
        } else if (result.status === 'error') {
            const r = (result.result || {});
            toast(`⚠️ ${r.error_message || 'Test failed'}`, true);
            resultEl.innerHTML = _renderConnectorTestResult(r, false);
        } else {
            toast('⚠️ Test timed out — check logs for details', true);
        }
    } catch (e) {
        toast('Error: ' + e.message, true);
    } finally {
        btn.disabled = false; btn.textContent = '🔍 Test Connection';
        loadConnectorsPage();
    }
}
```

- [ ] **Step 3: Manually verify in the browser**

No JS test framework exists in this project (no `package.json`). Verify manually:

1. Run the app locally (`uvicorn app.main:app --reload`), open `/admin-panel`, go to Report Connector.
2. Click Test Connection against a clinic with valid MocDoc credentials (or a mocked/dev connector).
3. Confirm: the toast still appears, AND a card appears below the buttons showing "Login successful — N report(s) found" plus a table of up to 5 masked patient rows.
4. Trigger a failing test (bad credentials) and confirm the card shows the red error line with no table.

- [ ] **Step 4: Commit**

```bash
git add admin/index.html
git commit -m "feat: show masked sample rows and login status in Test Connection result card"
```

---

### Task 8: `CONFIG_SCHEMA` on `HospitalConnector` and `MocDocConnector`

**Files:**
- Modify: `connectors/base.py:49-56` (class docstring area), `connectors/mocdoc/worker.py:89-98`
- Test: Create `tests/test_connector_registry.py`

**Interfaces:**
- Produces: `HospitalConnector.CONFIG_SCHEMA: list[dict]` (empty default on the ABC — every entry shaped `{key, label, type, placeholder, required}`). `MocDocConnector.CONFIG_SCHEMA` is the 4-entry list for `username`, `password`, `clinic_slug`, `base_url` (the fields `ConnectorCredentialsUpdate` and the admin form already use — confirmed exact field names in `app/routers/admin.py:820-828`).

**Context:** This is the groundwork step of Design E: no second connector is being built (per explicit user instruction), only the mechanism that lets the admin UI render a type-appropriate credential form instead of hardcoding "MocDoc Credentials" labels.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connector_registry.py
"""Tests for the CONFIG_SCHEMA groundwork that lets the admin UI render a
credential form per connector type instead of hardcoding MocDoc fields."""

REQUIRED_SCHEMA_KEYS = {"key", "label", "type", "placeholder", "required"}


def test_hospital_connector_base_has_empty_default_schema():
    from connectors.base import HospitalConnector

    assert HospitalConnector.CONFIG_SCHEMA == []


def test_mocdoc_connector_config_schema_has_required_fields():
    from connectors.mocdoc.worker import MocDocConnector

    keys = [f["key"] for f in MocDocConnector.CONFIG_SCHEMA]
    assert keys == ["username", "password", "clinic_slug", "base_url"]
    for field in MocDocConnector.CONFIG_SCHEMA:
        assert REQUIRED_SCHEMA_KEYS <= set(field.keys())

    password_field = next(f for f in MocDocConnector.CONFIG_SCHEMA if f["key"] == "password")
    assert password_field["type"] == "password"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_connector_registry.py -v`
Expected: FAIL — `AttributeError: type object 'HospitalConnector' has no attribute 'CONFIG_SCHEMA'`

- [ ] **Step 3: Add `CONFIG_SCHEMA` to the base class**

In `connectors/base.py`, inside the `HospitalConnector` class body, immediately after the class docstring (`connectors/base.py:55`, right before `def __init__`), add:

```python
    # Ordered list of {key, label, type, placeholder, required} describing
    # the credential fields this connector type needs. The admin panel
    # fetches this via GET /admin/connectors/types to render a form without
    # hardcoding any connector's field names. Empty by default — every
    # concrete subclass overrides it.
    CONFIG_SCHEMA: list[dict] = []
```

- [ ] **Step 4: Add `CONFIG_SCHEMA` to `MocDocConnector`**

In `connectors/mocdoc/worker.py`, inside the `MocDocConnector` class body, immediately after the class docstring (`connectors/mocdoc/worker.py:97`, right before `def __init__`), add:

```python
    CONFIG_SCHEMA = [
        {"key": "username", "label": "Username", "type": "text", "placeholder": "MocDoc login ID", "required": True},
        {"key": "password", "label": "Password", "type": "password", "placeholder": "Leave blank to keep existing", "required": True},
        {"key": "clinic_slug", "label": "Clinic Slug", "type": "text", "placeholder": "e.g. visakha-multispeciality-clinics", "required": False},
        {"key": "base_url", "label": "Base URL", "type": "text", "placeholder": "https://mocdoc.com", "required": False},
    ]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_connector_registry.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add connectors/base.py connectors/mocdoc/worker.py tests/test_connector_registry.py
git commit -m "feat: add CONFIG_SCHEMA to HospitalConnector/MocDocConnector for admin-UI form generation"
```

---

### Task 9: New endpoint `GET /admin/connectors/types`

**Files:**
- Modify: `app/routers/admin.py` (add after `get_connectors`, i.e. after line 2768, before the `ConnectorToggle` class)
- Test: Modify `tests/test_admin_connectors.py`

**Interfaces:**
- Consumes: `CONNECTOR_REGISTRY` from `connectors/runner.py` (local import, matching the existing lazy-import pattern already used for `run_connector` at `app/routers/admin.py:2975`), `CONFIG_SCHEMA` from Task 8.
- Produces: `GET /admin/connectors/types` → `{"types": [{"type": str, "display_name": str, "schema": list[dict]}]}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_admin_connectors.py`:

```python
@pytest.mark.asyncio
async def test_get_connector_types_returns_mocdoc_schema():
    from app.routers.admin import get_connector_types

    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    result = await get_connector_types(user=admin)

    types_by_key = {t["type"]: t for t in result["types"]}
    assert "mocdoc" in types_by_key
    assert types_by_key["mocdoc"]["display_name"] == "MocDoc"
    schema_keys = [f["key"] for f in types_by_key["mocdoc"]["schema"]]
    assert schema_keys == ["username", "password", "clinic_slug", "base_url"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_connectors.py -v -k get_connector_types`
Expected: FAIL — `ImportError: cannot import name 'get_connector_types'`

- [ ] **Step 3: Implement the endpoint**

In `app/routers/admin.py`, immediately after the `get_connectors` function ends (after line 2768, before `class ConnectorToggle(BaseModel):`), add:

```python
@router.get("/connectors/types")
async def get_connector_types(
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
    """List every registered connector type and its credential schema, so
    the admin panel can render a type-appropriate form instead of
    hardcoding MocDoc-specific field names."""
    from connectors.runner import CONNECTOR_REGISTRY

    display_names = {"mocdoc": "MocDoc"}
    return {
        "types": [
            {
                "type": connector_type,
                "display_name": display_names.get(connector_type, connector_type.title()),
                "schema": getattr(connector_cls, "CONFIG_SCHEMA", []),
            }
            for connector_type, connector_cls in CONNECTOR_REGISTRY.items()
        ]
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_admin_connectors.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add app/routers/admin.py tests/test_admin_connectors.py
git commit -m "feat: add GET /admin/connectors/types endpoint for schema-driven admin UI"
```

---

### Task 10: Admin UI — connector-type selector and schema-driven credential form

**Files:**
- Modify: `admin/index.html:1592-1613` (HTML), `admin/index.html:4129-4226` (JS: `loadConnectorsPage`, `loadConnectorCredentials`, `saveConnectorCredentials`, plus two new functions)

**Interfaces:**
- Consumes: `GET /admin/connectors/types` from Task 9.

**Context:** This is the final Design-E deliverable: the form renders from the fetched schema instead of hardcoded MocDoc field IDs. Only one connector type exists today (`mocdoc`), so behavior is unchanged for the current user — but adding a second connector later requires zero admin-frontend changes, exactly one new `HospitalConnector` subclass + one `CONNECTOR_REGISTRY` entry, per the approved design.

- [ ] **Step 1: Replace the hardcoded credentials form HTML**

In `admin/index.html`, replace lines 1592-1613:

```html
            <div class="form-card">
                <h3>🔌 MocDoc Credentials</h3>
                <div id="connectorMsg"></div>
                <div class="form-row">
                    <div class="field"><label>Username</label><input type="text" id="f-connUsername" placeholder="MocDoc login ID"></div>
                    <div class="field"><label>Password</label><input type="password" id="f-connPassword" placeholder="Leave blank to keep existing"></div>
                </div>
                <div class="form-row">
                    <div class="field"><label>Clinic Slug</label><input type="text" id="f-connSlug" placeholder="e.g. visakha-multispeciality-clinics"></div>
                    <div class="field"><label>Base URL</label><input type="text" id="f-connBaseUrl" placeholder="https://mocdoc.com"></div>
                </div>
                <div class="form-row">
                    <div class="field"><label>Admin Alert Phone</label><input type="text" id="f-connAlertPhone" placeholder="+91XXXXXXXXXX"></div>
                    <div class="field"><label>Poll Interval (minutes)</label><input type="number" id="f-connPollMinutes" min="1" value="10"></div>
                </div>
                <div class="form-row">
                    <div class="field"><label><input type="checkbox" id="f-connEnabled"> Connector Enabled</label></div>
                </div>
                <button class="btn btn-accent" onclick="saveConnectorCredentials()">Save Credentials</button>
                <button class="btn btn-ghost" onclick="testConnector()" id="btn-connTest">🔍 Test Connection</button>
                <button class="btn btn-ghost" onclick="runConnectorNow()" id="btn-connRunNow">▶️ Run Now</button>
                <div id="connTestResult"></div>
            </div>
```

with:

```html
            <div class="form-card">
                <h3>🔌 Connector Credentials</h3>
                <div id="connectorMsg"></div>
                <div class="form-row">
                    <div class="field"><label>Connector Type</label><select id="f-connType" onchange="loadConnectorsPage()"></select></div>
                </div>
                <div class="form-row" id="connFieldsContainer"></div>
                <div class="form-row">
                    <div class="field"><label>Admin Alert Phone</label><input type="text" id="f-connAlertPhone" placeholder="+91XXXXXXXXXX"></div>
                    <div class="field"><label>Poll Interval (minutes)</label><input type="number" id="f-connPollMinutes" min="1" value="10"></div>
                </div>
                <div class="form-row">
                    <div class="field"><label><input type="checkbox" id="f-connEnabled"> Connector Enabled</label></div>
                </div>
                <button class="btn btn-accent" onclick="saveConnectorCredentials()">Save Credentials</button>
                <button class="btn btn-ghost" onclick="testConnector()" id="btn-connTest">🔍 Test Connection</button>
                <button class="btn btn-ghost" onclick="runConnectorNow()" id="btn-connRunNow">▶️ Run Now</button>
                <div id="connTestResult"></div>
            </div>
```

(Note: this supersedes the `<div id="connTestResult"></div>` line added in Task 7 — it now lives inside this rewritten block.)

- [ ] **Step 2: Replace the JS: add type-loading, generic field rendering, generic save**

In `admin/index.html`, replace lines 4129-4226 (from `let _connectorId = null;` through the end of `saveConnectorCredentials()`):

```javascript
let _connectorId = null;
let _connectorBranchesLoaded = false;
let _connectorTypes = [];

function _connBranchParam() {
    const branchId = document.getElementById('f-connectorBranch').value;
    return branchId ? '&branch_id=' + branchId : '';
}

async function loadConnectorsPage() {
    const showBranchPicker = myFeatures === null || myFeatures.includes('multi_branch');
    document.getElementById('connectorBranchCard').style.display = showBranchPicker ? 'block' : 'none';

    if (showBranchPicker && !_connectorBranchesLoaded) {
        try {
            const data = await api('/admin/branches');
            const select = document.getElementById('f-connectorBranch');
            (data.branches || []).forEach(b => {
                const opt = document.createElement('option');
                opt.value = b.id;
                opt.textContent = b.name;
                select.appendChild(opt);
            });
        } catch (e) {}
        _connectorBranchesLoaded = true;
    }

    if (_connectorTypes.length === 0) {
        await loadConnectorTypes();
    }
    await loadConnectorCredentials();
    await loadConnectorAuditLog();
    await loadFailedReports();
}

async function loadConnectorTypes() {
    try {
        const data = await api('/admin/connectors/types');
        _connectorTypes = data.types || [];
        const select = document.getElementById('f-connType');
        const previousValue = select.value;
        select.innerHTML = '';
        _connectorTypes.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.type;
            opt.textContent = t.display_name;
            select.appendChild(opt);
        });
        if (previousValue) select.value = previousValue;
    } catch (e) {
        _connectorTypes = [];
    }
}

function _currentSchema() {
    const type = document.getElementById('f-connType').value || 'mocdoc';
    const entry = _connectorTypes.find(t => t.type === type);
    return (entry && entry.schema) || [];
}

function _renderConnFields(cfg) {
    const container = document.getElementById('connFieldsContainer');
    const schema = _currentSchema();
    container.innerHTML = schema.map(f => {
        const isSecret = f.type === 'password';
        let placeholder = f.placeholder;
        if (isSecret && cfg.password_set) placeholder = 'Saved — leave blank to keep existing';
        if (f.key === 'username' && cfg.username_masked) placeholder = 'Saved: ' + cfg.username_masked;
        const value = (!isSecret && f.key !== 'username') ? (cfg[f.key] || '') : '';
        return `<div class="field"><label>${esc(f.label)}</label>` +
            `<input type="${isSecret ? 'password' : 'text'}" id="f-connField-${f.key}" placeholder="${esc(placeholder)}" value="${esc(value)}"></div>`;
    }).join('');
}

async function loadConnectorCredentials() {
    try {
        const data = await api('/admin/connectors?clinic_id=default' + _connBranchParam());
        const conn = (data.connectors || [])[0];
        _connectorId = conn ? conn.id : null;
        const cfg = (conn && conn.config) || {};

        if (conn && conn.connector_type) {
            document.getElementById('f-connType').value = conn.connector_type;
        }
        _renderConnFields(cfg);

        document.getElementById('f-connAlertPhone').value = cfg.admin_alert_phone || '';
        document.getElementById('f-connPollMinutes').value = cfg.poll_interval_minutes || 10;
        document.getElementById('f-connEnabled').checked = conn ? !!conn.is_enabled : false;

        const statusEl = document.getElementById('connectorStatus');
        if (conn) {
            const lastRun = conn.last_run_at ? new Date(conn.last_run_at).toLocaleString() : 'never';
            const lastOk = conn.last_success_at ? new Date(conn.last_success_at).toLocaleString() : 'never';
            statusEl.textContent = `Last run: ${lastRun} · Last success: ${lastOk}` +
                (conn.last_error ? ` · Last error: ${conn.last_error}` : '');
        } else {
            statusEl.textContent = 'No connector configured yet';
        }
    } catch (e) {
        msg('connectorMsg', 'Error loading connector: ' + e.message, true);
    }
}

async function saveConnectorCredentials() {
    const branchId = document.getElementById('f-connectorBranch').value;
    const connectorType = document.getElementById('f-connType').value || 'mocdoc';
    const body = {
        connector_type: connectorType,
        branch_id: branchId || null,
        admin_alert_phone: document.getElementById('f-connAlertPhone').value.trim() || null,
        poll_interval_minutes: parseInt(document.getElementById('f-connPollMinutes').value, 10) || null,
        is_enabled: document.getElementById('f-connEnabled').checked,
    };
    _currentSchema().forEach(f => {
        const el = document.getElementById('f-connField-' + f.key);
        if (!el) return;
        const val = el.value.trim();
        if (f.type === 'password') {
            if (val) body.password = val;
        } else if (f.key === 'username') {
            if (val) body.username = val;
        } else {
            body[f.key] = val || null;
        }
    });

    const btn = document.querySelector('#pg-connectors .btn-accent');
    const origText = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }

    try {
        const res = await apiPut('/admin/connectors?clinic_id=default', body);
        toast('✅ Connector credentials saved!');
        // Small delay so the user sees the toast before the page refreshes
        setTimeout(() => loadConnectorsPage(), 500);
    } catch (e) {
        const errMsg = e.message || 'Unknown error';
        toast('❌ Save failed: ' + errMsg, true);
        msg('connectorMsg', 'Error: ' + errMsg, true);
        console.error('saveConnectorCredentials failed:', e);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = origText; }
    }
}
```

- [ ] **Step 3: Manually verify in the browser**

No JS test framework exists in this project. Verify manually:

1. Run the app locally, open `/admin-panel` → Report Connector.
2. Confirm the "Connector Type" dropdown shows "MocDoc" (the only registered type) and the 4 fields (Username, Password, Clinic Slug, Base URL) render below it with the same labels/placeholders as before.
3. Confirm existing saved credentials still show masked placeholders (`Saved: la••••••`, `Saved — leave blank to keep existing`) exactly as before the refactor.
4. Save credentials with a blank password and confirm the existing password is preserved (check via Test Connection still succeeding).
5. Confirm Test Connection and Run Now still work end-to-end (ties together Tasks 3-10).

- [ ] **Step 4: Commit**

```bash
git add admin/index.html
git commit -m "feat: render connector credential form from schema instead of hardcoded MocDoc fields"
```

---

## Self-Review

**Spec coverage:**
- Design A (deployment fix) → Tasks 1-2 (Dockerfile `PLAYWRIGHT_BROWSERS_PATH`, `render.yaml` worker service). Covered.
- Design B (lock reliability) → Task 3 (5-min lease, remaining-TTL error message) and Task 4 (graceful shutdown release, FastAPI + SIGTERM). Covered.
- Design C (RBAC fix) → Task 5. Covered.
- Design D (richer dry-run results) → Task 6 (backend `sample` field, audit-log-safe) and Task 7 (frontend result card). Covered.
- Design E (pluggable connector UI groundwork) → Task 8 (`CONFIG_SCHEMA`), Task 9 (`/admin/connectors/types`), Task 10 (schema-driven form). Covered. No second connector built, per explicit instruction.
- Design F (testing) → every backend task carries its own `pytest` file/additions (`test_dockerfile_browser_path.py`, `test_render_yaml.py`, `test_connector_runner.py`, `test_admin_connectors.py` additions, `test_connector_registry.py`); frontend tasks (7, 10) use manual browser verification since no JS test framework exists in this repo. Covered.
- "Error handling" section (all new failure paths degrade to existing generic surfaces) → verified explicitly in Task 6 (the `sample` exclusion from the audit-log insert prevents a new silent Run-History failure mode) and Task 3 (lock-fail path still returns the existing `error_message` string shape, just reworded).
- "Explicitly out of scope" → no task touches Fernet encryption, multi-branch UX, or builds a second real connector.

**Placeholder scan:** No "TBD"/"TODO" markers. Every code block is complete, runnable code with exact file paths and line numbers as read from the current repo state.

**Type consistency:** `acquire_connector_lock` return type `tuple[bool, int]` is used identically in Task 3 (definition) and its only caller inside `run_connector` (same task). `release_all_locks_held()` (defined Task 3) is called identically in Task 4's two call sites. `CONFIG_SCHEMA` shape `{key, label, type, placeholder, required}` (defined Task 8) is consumed identically by Task 9's endpoint (`getattr(connector_cls, "CONFIG_SCHEMA", [])`) and Task 10's frontend (`f.key`, `f.label`, `f.type`, `f.placeholder`). `sample` field shape `{patient_name_masked, vam_id, report_name}` (defined Task 6) is consumed identically by Task 7's `_renderConnectorTestResult`.
