# tests/test_plan_features.py
"""Tests for the flat ALL_FEATURES list derived from PLAN_FEATURES."""

from app.services.tenant import ALL_FEATURES, PLAN_FEATURES, has_feature


def test_all_features_excludes_wildcard_sentinel():
    assert "*" not in ALL_FEATURES


def test_all_features_is_sorted_and_deduplicated():
    assert ALL_FEATURES == sorted(set(ALL_FEATURES))


def test_all_features_contains_every_named_plan_feature():
    named = {f for feats in PLAN_FEATURES.values() for f in feats if f != "*"}
    assert set(ALL_FEATURES) == named


def test_soloclinic_features_subset_of_all_features():
    clinic = {"plan": "soloclinic"}
    resolved = [f for f in ALL_FEATURES if has_feature(clinic, f)]
    assert "booking" in resolved
    assert "lab_reports" not in resolved  # soloclinic doesn't have this feature


def test_diagstream_has_lab_reports_not_booking():
    clinic = {"plan": "diagstream"}
    resolved = [f for f in ALL_FEATURES if has_feature(clinic, f)]
    assert "lab_reports" in resolved
    assert "booking" not in resolved


# ─── Diagnostic Test Booking plan (diagbooking) ──────────────────────────────
# A diagnostic centre that ONLY takes lab-test bookings and payments over
# WhatsApp: inbound-driven, no connector, no report dispatch, no reminders.


def test_diagbooking_can_book_and_charge_for_lab_tests():
    clinic = {"plan": "diagbooking"}
    resolved = {f for f in ALL_FEATURES if has_feature(clinic, f)}
    assert "lab_test_booking" in resolved
    # A lab-test booking is priced from lab_tests.price_paise and paid through
    # Razorpay, so the payment feature is not optional for this plan.
    assert "payments_razorpay" in resolved


def test_diagbooking_sends_no_proactive_outbound():
    """No reminders and nothing to dispatch — every outbound message on this
    plan is a free in-session reply, which is the whole point of the tier."""
    clinic = {"plan": "diagbooking"}
    resolved = {f for f in ALL_FEATURES if has_feature(clinic, f)}
    for feature in ("reminders", "lab_reports", "diagnostic_reports", "ai_report_summary"):
        assert feature not in resolved, f"{feature} must not be on diagbooking"


def test_diagbooking_has_no_doctor_booking():
    clinic = {"plan": "diagbooking"}
    assert not has_feature(clinic, "booking")
    assert not has_feature(clinic, "roster_management")


def test_lab_test_booking_always_implies_razorpay_payments():
    """Regression guard. diagstream granted lab_test_booking without
    payments_razorpay, so those centres collected money through Razorpay but
    got a 403 from PUT /admin/settings/payment when entering their own keys."""
    for plan, features in PLAN_FEATURES.items():
        if "lab_test_booking" in features:
            assert "payments_razorpay" in features, (
                f"plan '{plan}' books paid lab tests but lacks payments_razorpay"
            )
