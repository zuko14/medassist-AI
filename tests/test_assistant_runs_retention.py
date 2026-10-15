"""KK-P1-01: assistant_runs is tenant-owned and purged after 90 days (KK-NFR-RET-01, RET-01)."""

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import data_retention
from app.services.data_retention import DataRetentionService
from app.tenancy import TENANT_OWNED_TABLES

NOW = datetime(2026, 10, 14, 0, 30, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def test_assistant_runs_is_tenant_owned():
    assert "assistant_runs" in TENANT_OWNED_TABLES


@pytest.mark.asyncio
async def test_purge_deletes_only_rows_older_than_90_days():
    sb_client = MagicMock()
    query = sb_client.table.return_value.delete.return_value
    query.lt.return_value = "the-query"
    executed = AsyncMock(return_value=MagicMock(data=[{"id": 1}, {"id": 2}]))
    with patch.object(data_retention, "supabase", sb_client), \
         patch.object(data_retention, "sb", executed), \
         patch.object(data_retention, "datetime", _FrozenDatetime):
        count = await DataRetentionService().purge_assistant_runs()

    assert count == 2
    sb_client.table.assert_called_once_with("assistant_runs")
    query.lt.assert_called_once_with("created_at", (NOW - timedelta(days=90)).isoformat())
    executed.assert_awaited_once_with("the-query")


@pytest.mark.asyncio
async def test_purge_failure_is_logged_not_raised():
    with patch.object(data_retention, "supabase", MagicMock()), \
         patch.object(data_retention, "sb", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await DataRetentionService().purge_assistant_runs() == 0


def test_scheduler_runs_the_purge_daily_under_the_distributed_lock():
    from app.services import scheduler as sched_mod

    src = inspect.getsource(sched_mod)
    assert 'id="assistant_runs_purge"' in src
    job = inspect.getsource(sched_mod.SchedulerService.purge_assistant_runs)
    assert 'distributed_job_lock("purge_assistant_runs"' in job
    assert "data_retention_service.purge_assistant_runs()" in job
