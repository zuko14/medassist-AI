"""Specialty plans are registered everywhere, and the six existing plans are
byte-for-byte unchanged."""

import re
from pathlib import Path

import pytest

from app.services.tenant import (
    ALL_FEATURES,
    FEATURE_LABELS,
    PLAN_FEATURES,
    SPECIALTY_BY_PLAN,
    has_feature,
    specialty_enabled,
)

REPO = Path(__file__).resolve().parent.parent
SPECIALTY_PLANS = ["derma", "eye", "dental", "ivf"]

# Snapshot of the six production plans taken BEFORE this change. If this test
# fails, an existing tenant's features changed — that is a regression, not a
# snapshot to update.
EXISTING_PLAN_SNAPSHOT = {
    "soloclinic": {"booking", "reminders", "multilingual", "emergency_escalation", "clinical_firewall",
                   "admin_dashboard", "roster_management", "holiday_calendar", "compliance_dpdp",
                   "compliance_nmc", "payments_razorpay"},
    "diagstream": {"multilingual", "emergency_escalation", "clinical_firewall", "compliance_dpdp",
                   "compliance_nmc", "lab_reports", "diagnostic_reports", "ai_report_summary",
                   "pii_sanitization", "multi_branch", "lab_test_booking", "payments_razorpay",
                   "holiday_calendar"},
    "diagbooking": {"lab_test_booking", "payments_razorpay", "admin_dashboard", "holiday_calendar",
                    "multi_branch", "multilingual", "emergency_escalation", "clinical_firewall",
                    "compliance_dpdp", "compliance_nmc"},
    "essential": {"booking", "reminders", "multilingual", "emergency_escalation", "clinical_firewall",
                  "admin_dashboard", "roster_management", "holiday_calendar", "compliance_dpdp",
                  "compliance_nmc", "lab_reports", "ai_report_summary", "pii_sanitization", "feedback",
                  "analytics", "multi_department", "payments_razorpay", "staff_training"},
    "polyclinic": {"booking", "reminders", "multilingual", "emergency_escalation", "clinical_firewall",
                   "admin_dashboard", "roster_management", "holiday_calendar", "compliance_dpdp",
                   "compliance_nmc", "lab_reports", "diagnostic_reports", "ai_report_summary",
                   "pii_sanitization", "feedback", "analytics", "multi_department", "payments_razorpay",
                   "staff_training", "multi_branch", "lab_test_booking"},
    "enterprise": {"*"},
}


@pytest.mark.parametrize("plan", list(EXISTING_PLAN_SNAPSHOT))
def test_existing_plans_are_unchanged(plan):
    assert set(PLAN_FEATURES[plan]) == EXISTING_PLAN_SNAPSHOT[plan]


@pytest.mark.parametrize("plan", SPECIALTY_PLANS)
def test_specialty_plans_can_book_take_payments_and_run_branches(plan):
    clinic = {"plan": plan}
    for feature in ("booking", "payments_razorpay", "multi_branch", "reminders",
                    "specialty_treatments", "roster_management", "holiday_calendar",
                    "admin_dashboard", "clinical_firewall", "compliance_dpdp"):
        assert has_feature(clinic, feature), f"{plan} lacks {feature}"
    # Specialty plans do not dispatch lab reports.
    assert not has_feature(clinic, "lab_reports")


def test_only_ivf_books_lab_tests():
    assert has_feature({"plan": "ivf"}, "lab_test_booking")
    for plan in ("derma", "eye", "dental"):
        assert not has_feature({"plan": plan}, "lab_test_booking")


def test_specialty_by_plan_mapping():
    assert SPECIALTY_BY_PLAN == {
        "derma": "dermatology", "eye": "ophthalmology", "dental": "dental", "ivf": "fertility",
    }


@pytest.mark.parametrize("clinic, expected", [
    ({"plan": "derma"}, True),
    ({"plan": "ivf", "features": {}}, True),
    ({"plan": "eye", "features": {"specialty_treatments": False}}, False),
    # The enterprise wildcard must NOT switch the specialty flow on.
    ({"plan": "enterprise"}, False),
    ({"plan": "enterprise", "features": {"specialty_treatments": True}}, True),
    ({"plan": "polyclinic"}, False),
    ({"plan": "polyclinic", "features": {"specialty_treatments": True}}, True),
    ({"plan": "soloclinic", "features": {"specialty_treatments": "yes"}}, False),
    ({}, False),
    (None, False),
])
def test_specialty_enabled_truth_table(clinic, expected):
    assert specialty_enabled(clinic) is expected


def test_new_feature_has_a_label_and_is_listed():
    assert "specialty_treatments" in ALL_FEATURES
    assert FEATURE_LABELS["specialty_treatments"] == "Treatments & Procedures Catalog"
    assert set(FEATURE_LABELS) == set(ALL_FEATURES)


def test_treatments_permission_is_registered_but_in_no_preset():
    from app.services.permissions import PERMISSIONS, ROLE_PRESETS

    assert "TREATMENTS_MANAGE" in PERMISSIONS
    for role, grants in ROLE_PRESETS.items():
        assert "TREATMENTS_MANAGE" not in grants, role


def test_new_tables_are_tenant_owned():
    from app.tenancy import TENANT_OWNED_TABLES

    assert {"specialty_treatments", "treatment_doctors"} <= TENANT_OWNED_TABLES


@pytest.mark.parametrize("plan", SPECIALTY_PLANS)
def test_onboarding_and_update_validators_accept_the_plan(plan):
    from app.routers.clinics import CreateClinicRequest, UpdateClinicRequest

    req = CreateClinicRequest(
        name="Test Specialty", whatsapp_number="+919876543210", plan=plan,
        meta_phone_number_id="000000000000", meta_access_token="EAAG_test",
    )
    assert req.plan == plan
    assert UpdateClinicRequest(plan=plan).plan == plan


@pytest.mark.parametrize("plan", SPECIALTY_PLANS)
def test_platform_router_lists_the_plan(plan):
    src = (REPO / "app" / "routers" / "platform.py").read_text(encoding="utf-8")
    valid_block = src.split("valid_plans = {")[1].split("}")[0]
    assert f'"{plan}"' in valid_block
    by_plan_block = src.split("clinics_by_plan = {")[1].split("}")[0]
    assert f'"{plan}": 0' in by_plan_block


@pytest.mark.parametrize("plan", SPECIALTY_PLANS)
def test_message_accounting_fallback_lists_the_plan(plan):
    src = (REPO / "app" / "services" / "message_accounting.py").read_text(encoding="utf-8")
    assert re.search(rf'"{plan}": \{{"included_messages_month": 2500', src)


@pytest.mark.parametrize("plan", SPECIALTY_PLANS)
def test_platform_console_offers_and_styles_the_plan(plan):
    html = (REPO / "admin" / "platform.html").read_text(encoding="utf-8")
    assert f".badge-{plan} " in html
    create_block = html.split('id="ccPlan"')[1].split("</select>")[0]
    assert f'value="{plan}"' in create_block
    filter_block = html.split('id="planFilter"')[1].split("</select>")[0]
    assert f'value="{plan}"' in filter_block
