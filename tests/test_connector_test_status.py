# tests/test_connector_test_status.py
"""Regression guard: GET /connectors/{id}/test-status must not report a bare
'idle' when the in-memory task dict was wiped by a server restart mid-run.
It should fall back to the DB advisory lock (locked_at) written at run-start
by connectors.runner.acquire_connector_lock, so an interrupted run is
reported as 'running' (still within the lock lease) or 'error' (lease
expired — the run died) instead of silently vanishing."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.routers.admin import AdminUser, _connector_tasks
from app.routers.admin import test_connector_status as get_test_status


def _mock_supabase(connector_row):
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[connector_row])
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[connector_row])
    mock_sb.table.return_value = mock_table
    return mock_sb


def _connector_row(locked_at=None):
    return {
        "id": "conn-1",
        "clinic_id": "clinic-1",
        "branch_id": None,
        "connector_type": "mocdoc",
        "locked_at": locked_at,
    }


@pytest.mark.asyncio
async def test_status_idle_when_no_task_and_no_lock():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    _connector_tasks.pop("conn-1", None)
    mock_sb = _mock_supabase(_connector_row(locked_at=None))

    with patch("app.routers.admin.supabase", mock_sb):
        result = await get_test_status(connector_id="conn-1", clinic_id="default", user=admin)

    assert result["status"] == "idle"


@pytest.mark.asyncio
async def test_status_running_when_lock_fresh_after_restart():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    _connector_tasks.pop("conn-1", None)
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    mock_sb = _mock_supabase(_connector_row(locked_at=fresh))

    with patch("app.routers.admin.supabase", mock_sb):
        result = await get_test_status(connector_id="conn-1", clinic_id="default", user=admin)

    assert result["status"] == "running"


@pytest.mark.asyncio
async def test_status_error_when_lock_expired_after_crash():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    _connector_tasks.pop("conn-1", None)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    mock_sb = _mock_supabase(_connector_row(locked_at=stale))

    with patch("app.routers.admin.supabase", mock_sb):
        result = await get_test_status(connector_id="conn-1", clinic_id="default", user=admin)

    assert result["status"] == "error"
    assert "interrupted" in result["result"]["error_message"].lower()
