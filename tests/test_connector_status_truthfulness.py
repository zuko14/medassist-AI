"""The connector status the operator sees must match what actually polls.

Production report: the dashboard read "Report Connector: Disabled · Last run:
13:02:42" — a disabled connector with a two-minute-old timestamp. The runs were
"Test Connection" dry runs, which stamped last_run_at as if they were real polls.
"""

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers.admin import AdminUser, get_diagnostic_stats


def _user():
    return AdminUser(username="op", role="admin", clinic_id="c1", branch_id=None)


def _connector(**over):
    # Relative to now: a fixed timestamp would age past the stale window and
    # turn this fixture "stalled" on some future run of the suite.
    just_ran = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    row = {
        "id": "conn-1",
        "branch_id": None,
        "connector_type": "mocdoc",
        "is_enabled": True,
        "last_run_at": just_ran,
        "last_success_at": None,
        "last_error": None,
        "updated_at": just_ran,
        "locked_at": None,
        "config": {"poll_interval_minutes": 5},
    }
    row.update(over)
    return row


async def _stats(connector_rows):
    def fake_table(name):
        tbl = MagicMock()
        for m in ("select", "eq", "in_", "gte", "lte", "order", "limit"):
            getattr(tbl, m).return_value = tbl
        tbl.execute.return_value = MagicMock(
            data=connector_rows if name == "integration_connectors" else []
        )
        return tbl

    with patch("app.routers.admin.supabase") as sb, patch(
        "app.routers.admin.enforce_clinic_access", return_value="c1"
    ):
        sb.table.side_effect = fake_table
        return await get_diagnostic_stats(clinic_id="c1", user=_user())


@pytest.mark.asyncio
async def test_disabled_connector_is_reported_disabled():
    out = await _stats([_connector(is_enabled=False)])
    assert out["connector"]["health"] == "disabled"


@pytest.mark.asyncio
async def test_disabled_connector_advertises_no_next_run():
    """A disabled connector is never polled, so promising a next run is a lie."""
    out = await _stats([_connector(is_enabled=False)])
    assert out["connector"]["next_run_at"] is None


@pytest.mark.asyncio
async def test_disabled_sibling_is_counted_not_hidden():
    """The headline stays on the live connector, but the off one still shows.

    Inverting this to "worst health wins" would resurrect an older bug where a
    decommissioned branch pinned the dashboard to its stale Chromium error.
    """
    out = await _stats(
        [
            _connector(id="a", branch_id="b1", updated_at="2099-01-01T00:00:00+00:00"),
            _connector(id="b", branch_id="b2", is_enabled=False),
        ]
    )
    conn = out["connector"]
    assert conn["id"] == "a"
    assert conn["connector_count"] == 2
    assert conn["unhealthy_count"] == 1


@pytest.mark.asyncio
async def test_single_healthy_connector_still_reports_healthy():
    out = await _stats([_connector()])
    assert out["connector"]["health"] == "healthy"
    assert out["connector"]["connector_count"] == 1
    assert out["connector"]["unhealthy_count"] == 0


@pytest.mark.asyncio
async def test_no_phantom_consecutive_failures_field():
    """The column does not exist; reporting a hardcoded 0 invents good news."""
    out = await _stats([_connector()])
    assert "consecutive_failures" not in out["connector"]
