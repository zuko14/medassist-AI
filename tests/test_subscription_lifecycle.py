"""Subscription lifecycle: 30-day prepaid window, 5-day grace, backdated renewal.

These are pure-function tests on purpose. The lifecycle is computed from dates
on every read rather than flipped by a cron, so the whole contract is testable
without a database.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.subscription import (
    DEFAULT_GRACE_PERIOD_DAYS,
    STATUS_ACTIVE,
    STATUS_GRACE,
    STATUS_SUSPENDED,
    STATUS_TRIAL,
    SUBSCRIPTION_PERIOD_DAYS,
    automated_outbound_allowed,
    compute_subscription_state,
    ist_today,
    next_ist_midnight,
    renewal_window,
)

START = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def clinic(**over):
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "subscription_start_date": START.isoformat(),
        "subscription_end_date": (START + timedelta(days=30)).isoformat(),
        "grace_period_days": 5,
        "subscription_status": STATUS_ACTIVE,
        "daily_report_limit": 100,
    }
    base.update(over)
    return base


def at(days, hours=0):
    return START + timedelta(days=days, hours=hours)


# -- The three lifecycle windows --------------------------------------------


@pytest.mark.parametrize("day", [0, 1, 15, 29])
def test_active_for_the_full_30_days(day):
    state = compute_subscription_state(clinic(), now=at(day))
    assert state["status"] == STATUS_ACTIVE
    assert state["outbound_allowed"] is True
    assert state["banner"] is None


def test_day_30_exactly_is_the_boundary_into_grace():
    assert compute_subscription_state(clinic(), now=at(30))["status"] == STATUS_GRACE


@pytest.mark.parametrize("day,expected_grace_day", [(30, 1), (31, 2), (34, 5)])
def test_grace_period_counts_day_x_of_5(day, expected_grace_day):
    state = compute_subscription_state(clinic(), now=at(day, hours=1))
    assert state["status"] == STATUS_GRACE
    assert state["grace_day"] == expected_grace_day
    assert state["grace_days_left"] == DEFAULT_GRACE_PERIOD_DAYS - (expected_grace_day - 1)


def test_grace_period_still_sends():
    assert compute_subscription_state(clinic(), now=at(32))["outbound_allowed"] is True
    assert automated_outbound_allowed(clinic(), now=at(32)) is True


def test_grace_banner_is_the_exact_operator_wording():
    banner = compute_subscription_state(clinic(), now=at(31, hours=2))["banner"]
    assert "Your 30-day subscription has expired" in banner
    assert "5-day grace period (Day 2 of 5)" in banner
    assert "contact the administrator to renew" in banner.lower()


@pytest.mark.parametrize("day", [35, 36, 60])
def test_suspended_after_the_grace_period(day):
    state = compute_subscription_state(clinic(), now=at(day))
    assert state["status"] == STATUS_SUSPENDED
    assert state["outbound_allowed"] is False
    assert automated_outbound_allowed(clinic(), now=at(day)) is False


def test_a_shorter_grace_window_is_honoured_exactly():
    """grace_period_days is per-clinic; 3 days must mean 3, not the default 5."""
    c = clinic(grace_period_days=3)
    on_last_grace_day = compute_subscription_state(c, now=at(32, hours=12))
    assert on_last_grace_day["status"] == STATUS_GRACE
    assert on_last_grace_day["grace_day"] == 3
    assert on_last_grace_day["grace_days_left"] == 1
    assert compute_subscription_state(c, now=at(33, hours=1))["status"] == STATUS_SUSPENDED


def test_zero_grace_days_suspends_immediately_at_expiry():
    state = compute_subscription_state(clinic(grace_period_days=0), now=at(30, hours=1))
    assert state["status"] == STATUS_SUSPENDED


# -- Sticky suspension & trial ----------------------------------------------


def test_owner_suspension_outranks_the_dates():
    """An owner can kill a clinic mid-period; the dates must not resurrect it."""
    state = compute_subscription_state(
        clinic(subscription_status=STATUS_SUSPENDED), now=at(5)
    )
    assert state["status"] == STATUS_SUSPENDED
    assert state["outbound_allowed"] is False


def test_trial_reads_as_trial_inside_the_window_and_expires_normally():
    c = clinic(subscription_status=STATUS_TRIAL)
    assert compute_subscription_state(c, now=at(5))["status"] == STATUS_TRIAL
    assert compute_subscription_state(c, now=at(31))["status"] == STATUS_GRACE
    assert compute_subscription_state(c, now=at(40))["status"] == STATUS_SUSPENDED


def test_unconfigured_clinic_fails_open():
    """Pre-migration rows have no dates. Never silence a live hospital."""
    state = compute_subscription_state({"id": "x"}, now=at(0))
    assert state["unconfigured"] is True
    assert state["status"] == STATUS_ACTIVE
    assert state["outbound_allowed"] is True


def test_unconfigured_clinic_still_honours_an_explicit_suspension():
    state = compute_subscription_state(
        {"id": "x", "subscription_status": STATUS_SUSPENDED}, now=at(0)
    )
    assert state["outbound_allowed"] is False


def test_garbage_timestamp_is_treated_as_unconfigured_not_expired():
    state = compute_subscription_state(
        {"id": "x", "subscription_end_date": "not-a-date"}, now=at(0)
    )
    assert state["unconfigured"] is True
    assert state["outbound_allowed"] is True


def test_z_suffix_timestamps_parse():
    c = clinic(subscription_end_date="2026-10-01T00:00:00Z")
    state = compute_subscription_state(c, now=datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert state["status"] == STATUS_ACTIVE


def test_unknown_stored_status_falls_back_to_active_not_crash():
    state = compute_subscription_state(clinic(subscription_status="bogus"), now=at(5))
    assert state["status"] == STATUS_ACTIVE


# -- Renewal accounting: backdated grace deduction --------------------------


def test_renewal_backdates_to_the_previous_expiry():
    """Renewing on day 33 must not hand back 30 fresh days from day 33."""
    start, end = renewal_window(clinic(), now=at(33))
    assert start == START + timedelta(days=30)
    assert end == START + timedelta(days=60)
    # 3 grace days were consumed, so 27 usable days remain.
    assert (end - at(33)).days == 27


def test_renewal_during_the_active_period_stacks_rather_than_resetting():
    start, end = renewal_window(clinic(), now=at(10))
    assert start == START + timedelta(days=30)
    assert end == START + timedelta(days=60)


def test_renewal_always_grants_a_full_30_days_from_the_old_end():
    start, end = renewal_window(clinic(), now=at(31))
    assert (end - start).days == SUBSCRIPTION_PERIOD_DAYS


def test_very_late_renewal_does_not_hand_back_a_dead_window():
    """Backdating 6 months late would expire the moment it was granted."""
    now = at(200)
    start, end = renewal_window(clinic(), now=now)
    assert start == now
    assert end > now
    assert (end - start).days == SUBSCRIPTION_PERIOD_DAYS


def test_renewal_of_an_unconfigured_clinic_starts_now():
    now = at(3)
    start, end = renewal_window({"id": "x"}, now=now)
    assert start == now
    assert (end - start).days == SUBSCRIPTION_PERIOD_DAYS


def test_renewed_clinic_is_active_again_with_the_grace_days_deducted():
    now = at(33)
    start, end = renewal_window(clinic(), now=now)
    renewed = clinic(
        subscription_start_date=start.isoformat(),
        subscription_end_date=end.isoformat(),
        subscription_status=STATUS_ACTIVE,
    )
    state = compute_subscription_state(renewed, now=now)
    assert state["status"] == STATUS_ACTIVE
    assert state["outbound_allowed"] is True
    assert state["days_remaining"] == 27  # 30 paid, 3 grace days already burned


# -- Customer-safety contract -----------------------------------------------


def test_lifecycle_payload_carries_no_financial_field():
    """Same P0 boundary as /admin/messaging-usage: this reaches clinic admins."""
    forbidden = ("cost", "price", "paise", "inr", "markup", "margin", "revenue", "profit")
    for now in (at(5), at(32), at(40)):
        for key in compute_subscription_state(clinic(), now=now):
            assert not any(f in key.lower() for f in forbidden), key


# -- Asia/Kolkata day boundary ----------------------------------------------


def test_ist_day_rolls_over_at_1830_utc():
    assert ist_today(datetime(2026, 9, 1, 18, 29, tzinfo=timezone.utc)).isoformat() == "2026-09-01"
    assert ist_today(datetime(2026, 9, 1, 18, 30, tzinfo=timezone.utc)).isoformat() == "2026-09-02"


def test_next_ist_midnight_is_in_the_future_and_lands_on_1830_utc():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    nxt = next_ist_midnight(now)
    assert nxt > now
    assert (nxt.hour, nxt.minute) == (18, 30)
