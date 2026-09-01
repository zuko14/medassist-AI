"""Fixed-tier daily report limits: thresholds, counters, and the dispatch gate."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.subscription import (
    DAILY_REPORT_LIMIT_TIERS,
    STATUS_SUSPENDED,
    limit_state,
    record_outbound_usage,
    report_dispatch_allowed,
)

CLINIC_ID = "11111111-1111-1111-1111-111111111111"


# -- Tier definition ---------------------------------------------------------


def test_the_tiers_are_exactly_the_five_plus_unlimited():
    assert DAILY_REPORT_LIMIT_TIERS == (0, 50, 100, 200, 300, 500)


def test_api_and_database_agree_on_the_tiers():
    """A tier the API accepts but the CHECK constraint rejects is a 500."""
    sql = open("migrations/068_subscription_lifecycle_and_daily_limits.sql", encoding="utf-8").read()
    for tier in DAILY_REPORT_LIMIT_TIERS:
        assert str(tier) in sql.split("CHECK (daily_report_limit IN (")[1].split(")")[0]


# -- Threshold behaviour -----------------------------------------------------


@pytest.mark.parametrize("used,level", [
    (0, "ok"), (39, "ok"), (40, "warning"), (49, "warning"),
    (50, "blocked"), (51, "blocked"),
])
def test_80_percent_warns_and_100_percent_blocks(used, level):
    assert limit_state(50, used)["level"] == level


def test_warning_fires_exactly_at_80_percent():
    assert limit_state(100, 79)["level"] == "ok"
    assert limit_state(100, 80)["level"] == "warning"
    assert limit_state(100, 99)["level"] == "warning"
    assert limit_state(100, 100)["level"] == "blocked"


def test_remaining_and_percent_are_reported_for_the_badge():
    state = limit_state(200, 160)
    assert state["percent"] == 80
    assert state["remaining"] == 40
    assert state["level"] == "warning"


def test_zero_means_unlimited_and_never_blocks():
    state = limit_state(0, 10_000)
    assert state["is_unlimited"] is True
    assert state["level"] == "unlimited"
    assert state["remaining"] is None


def test_overshoot_does_not_produce_negative_remaining():
    state = limit_state(50, 500)
    assert state["remaining"] == 0
    assert state["level"] == "blocked"


def test_missing_or_null_limit_is_treated_as_unlimited_not_zero():
    assert limit_state(None, 5)["is_unlimited"] is True


# -- The dispatch gate -------------------------------------------------------


def _clinic(limit=50, status="active"):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {
        "id": CLINIC_ID,
        "daily_report_limit": limit,
        "subscription_start_date": (now - timedelta(days=1)).isoformat(),
        "subscription_end_date": (now + timedelta(days=29)).isoformat(),
        "grace_period_days": 5,
        "subscription_status": status,
    }


@pytest.mark.asyncio
async def test_dispatch_allowed_below_the_limit():
    with patch("app.services.subscription.get_daily_usage", new_callable=AsyncMock) as usage:
        usage.return_value = {"reports_delivered_count": 10}
        allowed, reason = await report_dispatch_allowed(_clinic(limit=50))
    assert allowed is True
    assert reason == ""


@pytest.mark.asyncio
async def test_dispatch_blocked_at_the_limit():
    with patch("app.services.subscription.get_daily_usage", new_callable=AsyncMock) as usage:
        usage.return_value = {"reports_delivered_count": 50}
        allowed, reason = await report_dispatch_allowed(_clinic(limit=50))
    assert allowed is False
    assert reason == "daily_limit_reached"


@pytest.mark.asyncio
async def test_unlimited_never_queries_usage_at_all():
    with patch("app.services.subscription.get_daily_usage", new_callable=AsyncMock) as usage:
        allowed, reason = await report_dispatch_allowed(_clinic(limit=0))
    assert (allowed, reason) == (True, "")
    usage.assert_not_called()


@pytest.mark.asyncio
async def test_suspension_blocks_before_the_limit_is_even_checked():
    with patch("app.services.subscription.get_daily_usage", new_callable=AsyncMock) as usage:
        allowed, reason = await report_dispatch_allowed(_clinic(limit=500, status=STATUS_SUSPENDED))
    assert allowed is False
    assert reason == "suspended"
    usage.assert_not_called()


@pytest.mark.asyncio
async def test_a_usage_read_failure_fails_open():
    """A dropped Supabase connection must not stop a hospital's reports."""
    with patch("app.database.sb", side_effect=RuntimeError("db down")):
        allowed, reason = await report_dispatch_allowed(_clinic(limit=50))
    assert allowed is True


# -- Atomic counter increment ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("classification,counter", [
    ("LAB_REPORT", "p_reports"),
    ("PRESCRIPTION", "p_prescriptions"),
    ("APPOINTMENT_REMINDER", "p_reminders"),
    ("FOLLOW_UP", "p_followups"),
])
async def test_each_class_advances_its_own_counter(classification, counter):
    with patch("app.database.supabase") as mock_sb, \
         patch("app.database.sb", new_callable=AsyncMock) as mock_exec:
        mock_sb.rpc.return_value = MagicMock()
        await record_outbound_usage(CLINIC_ID, classification)

    params = mock_sb.rpc.call_args[0][1]
    assert mock_sb.rpc.call_args[0][0] == "increment_clinic_daily_usage"
    assert params[counter] == 1
    assert params["p_total"] == 1
    assert mock_exec.await_count == 1
    for other in ("p_reports", "p_prescriptions", "p_reminders", "p_followups"):
        if other != counter:
            assert params[other] == 0


@pytest.mark.asyncio
async def test_an_unclassified_send_still_counts_toward_total_outbound():
    with patch("app.database.supabase") as mock_sb, \
         patch("app.database.sb", new_callable=AsyncMock):
        mock_sb.rpc.return_value = MagicMock()
        await record_outbound_usage(CLINIC_ID, "OTHER")

    params = mock_sb.rpc.call_args[0][1]
    assert params["p_total"] == 1
    assert params["p_reports"] == 0


@pytest.mark.asyncio
async def test_the_sentinel_clinic_scope_is_never_counted():
    """'default' is not a tenant. Counting it would corrupt a real clinic row."""
    with patch("app.database.supabase") as mock_sb:
        await record_outbound_usage("default", "LAB_REPORT")
        await record_outbound_usage(None, "LAB_REPORT")
    mock_sb.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_a_counter_failure_never_raises():
    """This runs inside the send path. It must not be able to break delivery."""
    with patch("app.database.supabase") as mock_sb, \
         patch("app.database.sb", side_effect=RuntimeError("rpc missing")):
        mock_sb.rpc.return_value = MagicMock()
        await record_outbound_usage(CLINIC_ID, "LAB_REPORT")  # must not raise
