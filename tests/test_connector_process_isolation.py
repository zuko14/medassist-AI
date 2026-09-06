"""Chromium must never start inside the container that answers Meta webhooks.

KA-P0-A. render.yaml isolates connectors into a dedicated worker, but two
paths defeated that:

  1. settings.run_connectors_in_web defaulted to True, so a service created in
     the Render dashboard (where render.yaml never applies) polled connectors
     in the web process.
  2. The admin Test / Run now buttons called asyncio.ensure_future() on the
     connector unconditionally, spawning Playwright in the web process even
     when the flag was correctly False.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import connectors.runner as runner
from app.config import settings
from app.routers.admin import _dispatch_connector_run

@pytest.fixture(autouse=True)
def clean_task_tracker():
    """_connector_tasks is module-level state that leaks between tests.

    Without this the in-process test below leaves a "conn-1: running" entry
    that the status test then reads instead of exercising its own path — the
    same order-dependent pollution that makes the wider suite non-deterministic.
    """
    from app.routers import admin

    admin._connector_tasks.clear()
    yield
    admin._connector_tasks.clear()


CONNECTOR = {
    "id": "conn-1",
    "clinic_id": "clinic-A",
    "connector_type": "mocdoc",
    "branch_id": None,
    "config": {"poll_interval_minutes": 10, "username": "u"},
}


def test_default_keeps_polling_so_a_deploy_cannot_silently_stop_reports():
    """Opting out by DEFAULT would stop report delivery on a live client.

    This was briefly False. Checking production first showed Accumx
    Diagnostics' reports flow through the web process with no verified worker
    running, so the flip would have converted a latent OOM risk into a certain
    outage on the next deploy. The isolation win now comes from the admin
    handoff below plus an explicit opt-out, never from a silent default.
    """
    assert settings.model_fields["run_connectors_in_web"].default is True


@pytest.mark.asyncio
async def test_admin_run_is_handed_to_the_worker_not_run_in_web():
    mock_supabase = MagicMock()
    with patch("app.routers.admin.settings.run_connectors_in_web", False), \
         patch("app.routers.admin.supabase", mock_supabase), \
         patch("app.routers.admin.sb", new=AsyncMock()), \
         patch("app.routers.admin.spawn_background_task") as mock_spawn:
        result = await _dispatch_connector_run(CONNECTOR, "conn-1", dry_run=True)

    mock_spawn.assert_not_called()  # no Chromium in the webhook container
    assert result["dispatched_to"] == "worker"
    assert result["status"] == "queued"

    # The request is durable state the worker reads, so it must actually land.
    written = mock_supabase.table.return_value.update.call_args[0][0]["config"]
    assert written["run_requested_mode"] == "test"
    assert written["run_requested_at"]
    assert written["username"] == "u", "existing connector config was clobbered"


@pytest.mark.asyncio
async def test_admin_run_still_works_in_process_when_no_worker_is_provisioned():
    with patch("app.routers.admin.settings.run_connectors_in_web", True), \
         patch("app.routers.admin.sb", new=AsyncMock()), \
         patch("app.routers.admin.spawn_background_task") as mock_spawn, \
         patch("app.routers.admin._run_connector_background") as mock_bg:
        mock_bg.return_value = MagicMock()
        result = await _dispatch_connector_run(CONNECTOR, "conn-1", dry_run=False)

    mock_spawn.assert_called_once()
    assert result["dispatched_to"] == "web"


def _fake_lock(acquired=True):
    @asynccontextmanager
    async def _lock(*a, **k):
        yield acquired
    return _lock


@pytest.mark.asyncio
async def test_worker_runs_a_requested_connector_before_its_poll_interval():
    """An operator request must bypass poll_interval and clear itself first."""
    just_now = "2026-09-06T12:00:00+00:00"
    row = dict(CONNECTOR)
    row["last_run_at"] = just_now  # interval NOT elapsed
    row["config"] = {
        "poll_interval_minutes": 10,
        "run_requested_at": just_now,
        "run_requested_mode": "test",
    }

    select_result = MagicMock(data=[row])
    mock_sb = AsyncMock(return_value=select_result)

    with patch.object(runner, "distributed_job_lock", _fake_lock()), \
         patch.object(runner, "supabase", MagicMock()), \
         patch.object(runner, "sb", mock_sb), \
         patch.object(runner, "_ensure_subprocess_support", MagicMock()), \
         patch.object(runner, "run_connector", new=AsyncMock()) as mock_run, \
         patch.object(asyncio, "sleep", new=AsyncMock()):
        await runner.run_all_connectors()

    mock_run.assert_awaited_once()
    assert mock_run.await_args.kwargs["dry_run"] is True, "requested mode lost"
    assert mock_run.await_args.kwargs["clinic_id"] == "clinic-A"


@pytest.mark.asyncio
async def test_worker_skips_an_unrequested_connector_inside_its_poll_interval():
    """Regression guard: the request bypass must not disable interval skipping."""
    from datetime import datetime, timezone

    row = dict(CONNECTOR)
    row["last_run_at"] = datetime.now(timezone.utc).isoformat()
    row["config"] = {"poll_interval_minutes": 10}

    mock_sb = AsyncMock(return_value=MagicMock(data=[row]))

    with patch.object(runner, "distributed_job_lock", _fake_lock()), \
         patch.object(runner, "supabase", MagicMock()), \
         patch.object(runner, "sb", mock_sb), \
         patch.object(runner, "_ensure_subprocess_support", MagicMock()), \
         patch.object(runner, "run_connector", new=AsyncMock()) as mock_run, \
         patch.object(asyncio, "sleep", new=AsyncMock()):
        await runner.run_all_connectors()

    mock_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_reports_queued_while_the_worker_has_not_started_yet():
    """The gap between handing off and the worker picking up must not read as idle.

    _connector_tasks lives in the web process and locked_at is only written
    once the worker actually starts, so for up to a poll interval neither
    signal exists. Answering "idle" there looks like the button did nothing.
    """
    from app.routers.admin import test_connector_status

    queued = dict(CONNECTOR)
    queued["config"] = {"run_requested_at": "2026-09-06T12:00:00+00:00",
                        "run_requested_mode": "test"}

    with patch("app.routers.admin._load_connector_for_action",
               new=AsyncMock(return_value=queued)):
        result = await test_connector_status(
            connector_id="conn-1", clinic_id="clinic-A", user=MagicMock()
        )

    assert result["status"] == "running", "queued handoff must not read as idle"
    assert result["mode"] == "test"


@pytest.mark.asyncio
async def test_a_worker_run_that_finished_is_reported_done_not_idle():
    """The web process must read the outcome of a run it did not execute.

    _connector_tasks is per-process, so when the dedicated worker runs the
    connector the polling browser never sees 'done' in web memory. Returning
    'idle' there made the UI poll to its 300s ceiling and report 'timed out'
    on every SUCCESSFUL run — the operator would conclude the connector is
    broken while it is working. connector_audit_log is the durable record.
    """
    from datetime import datetime, timezone
    from app.routers.admin import test_connector_status

    fresh = datetime.now(timezone.utc).isoformat()
    audit_row = {
        "run_status": "success",
        "reports_found": 4,
        "reports_uploaded": 4,
        "reports_failed": 0,
        "error_message": None,
        "created_at": fresh,
    }

    idle = dict(CONNECTOR)
    idle["config"] = {}
    idle["locked_at"] = None

    mock_supabase = MagicMock()
    with patch("app.routers.admin._load_connector_for_action",
               new=AsyncMock(return_value=idle)), \
         patch("app.routers.admin.supabase", mock_supabase), \
         patch("app.routers.admin.sb",
               new=AsyncMock(return_value=MagicMock(data=[audit_row]))):
        result = await test_connector_status(
            connector_id="conn-1", clinic_id="clinic-A", user=MagicMock()
        )

    assert result["status"] == "done", "a finished worker run must not read as idle"
    assert result["success"] is True
    assert result["result"]["reports_uploaded"] == 4


@pytest.mark.asyncio
async def test_a_stale_audit_row_is_not_replayed_as_a_fresh_result():
    """Guard the window: an old run must not answer a new request."""
    from app.routers.admin import test_connector_status

    stale_row = {
        "run_status": "success",
        "reports_found": 1,
        "reports_uploaded": 1,
        "reports_failed": 0,
        "error_message": None,
        "created_at": "2026-09-01T00:00:00+00:00",  # days old
    }

    idle = dict(CONNECTOR)
    idle["config"] = {}
    idle["locked_at"] = None

    with patch("app.routers.admin._load_connector_for_action",
               new=AsyncMock(return_value=idle)), \
         patch("app.routers.admin.supabase", MagicMock()), \
         patch("app.routers.admin.sb",
               new=AsyncMock(return_value=MagicMock(data=[stale_row]))):
        result = await test_connector_status(
            connector_id="conn-1", clinic_id="clinic-A", user=MagicMock()
        )

    assert result["status"] == "idle"


@pytest.mark.asyncio
async def test_a_naive_audit_timestamp_does_not_500_the_polling_endpoint():
    """The browser polls this every 5s; an unparseable timestamp must degrade, not raise."""
    from datetime import datetime

    from app.routers.admin import test_connector_status

    naive = {
        "run_status": "success",
        "reports_found": 1,
        "reports_uploaded": 1,
        "reports_failed": 0,
        "error_message": None,
        "created_at": datetime.now().replace(microsecond=0).isoformat(),  # no tzinfo
    }
    idle = dict(CONNECTOR)
    idle["config"] = {}
    idle["locked_at"] = None

    with patch("app.routers.admin._load_connector_for_action",
               new=AsyncMock(return_value=idle)), \
         patch("app.routers.admin.supabase", MagicMock()), \
         patch("app.routers.admin.sb",
               new=AsyncMock(return_value=MagicMock(data=[naive]))):
        result = await test_connector_status(
            connector_id="conn-1", clinic_id="clinic-A", user=MagicMock()
        )

    assert result["status"] in ("done", "idle")  # never an exception
