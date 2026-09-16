"""The `multispecialty` plan (migration 078): a general hospital that ALSO runs
a treatments catalogue — Aayush-style facilities with emergency, cardiac,
maternity, dialysis, lab and radiology plus skin / eye / dental treatments.

The distinction that carries this plan: it is specialty-ENABLED but it is NOT
in SPECIALTY_BY_PLAN. That is what keeps the departments row in the patient
menu next to the treatments rows. If a later change puts "multispecialty" into
SPECIALTY_BY_PLAN, `Our Services` disappears for a hospital that has fifteen
departments — these tests fail first.

Every existing plan must be byte-for-byte unchanged; test_specialty_plans.py
holds the six-plan snapshot and this file re-checks the four specialty plans.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import specialty_flow
from app.services.conversation import ConversationManager
from app.services.tenant import (
    ALL_FEATURES,
    HYBRID_SPECIALTY_PLANS,
    PLAN_FEATURES,
    SPECIALTY_BY_PLAN,
    has_feature,
    specialty_enabled,
)

REPO = Path(__file__).resolve().parent.parent
PLAN = "multispecialty"
PHONE = "+919000000077"


# ── plan registry ────────────────────────────────────────────────────────────

def test_plan_carries_every_feature_without_the_enterprise_wildcard():
    """Enterprise-equivalent, but enumerated: a "*" plan can never be trimmed
    per tenant from the owner console, and it makes specialty_enabled() lie."""
    assert PLAN_FEATURES[PLAN] == set(ALL_FEATURES)
    assert "*" not in PLAN_FEATURES[PLAN]


def test_plan_is_polyclinic_plus_the_catalogue():
    assert PLAN_FEATURES[PLAN] == set(PLAN_FEATURES["polyclinic"]) | {"specialty_treatments"}


@pytest.mark.parametrize("feature", [
    # the general hospital half
    "booking", "multi_department", "multi_branch", "roster_management",
    "holiday_calendar", "reminders", "payments_razorpay", "analytics", "feedback",
    "staff_training", "admin_dashboard", "clinical_firewall", "compliance_dpdp",
    # the diagnostics half — the Aayush board lists laboratory, x-ray, CT
    "lab_reports", "diagnostic_reports", "ai_report_summary", "lab_test_booking",
    # the specialty half
    "specialty_treatments",
])
def test_both_halves_are_switched_on(feature):
    assert has_feature({"plan": PLAN}, feature)


def test_the_four_single_specialty_plans_are_unchanged():
    """Adding a hybrid plan must not leak features into derma/eye/dental/ivf."""
    for plan in ("derma", "eye", "dental"):
        assert "lab_reports" not in PLAN_FEATURES[plan]
        assert "lab_test_booking" not in PLAN_FEATURES[plan]
        assert "multi_department" not in PLAN_FEATURES[plan]
    assert "lab_test_booking" in PLAN_FEATURES["ivf"]
    assert "lab_reports" not in PLAN_FEATURES["ivf"]


# ── the hybrid gate ──────────────────────────────────────────────────────────

def test_hybrid_plan_is_not_a_single_specialty():
    """SPECIALTY_BY_PLAN means "this facility IS one specialty". A hospital with
    fifteen departments is not, and is_specialty_plan() is what suppresses the
    departments row in the patient menu."""
    assert PLAN not in SPECIALTY_BY_PLAN
    assert SPECIALTY_BY_PLAN == {
        "derma": "dermatology", "eye": "ophthalmology", "dental": "dental", "ivf": "fertility",
    }
    assert HYBRID_SPECIALTY_PLANS == frozenset({PLAN})
    assert not specialty_flow.is_specialty_plan({"plan": PLAN})


@pytest.mark.parametrize("clinic, expected", [
    ({"plan": PLAN}, True),
    ({"plan": PLAN, "features": {}}, True),
    ({"plan": PLAN, "features": None}, True),
    # The owner can still switch the catalogue off for one tenant.
    ({"plan": PLAN, "features": {"specialty_treatments": False}}, False),
    ({"plan": PLAN, "features": {"specialty_treatments": "no"}}, True),
    # …and no other plan changed.
    ({"plan": "enterprise"}, False),
    ({"plan": "polyclinic"}, False),
    ({"plan": "derma"}, True),
])
def test_specialty_enabled_truth_table(clinic, expected):
    assert specialty_enabled(clinic) is expected


# ── patient menu: both halves, inside Meta's 10-row list limit ───────────────

def _manager():
    m = ConversationManager()
    m.whatsapp = MagicMock()
    m.whatsapp.send_interactive_list = AsyncMock()
    return m


def _menu_ids(m):
    call = m.whatsapp.send_interactive_list.await_args
    return [r["id"] for s in call.kwargs["sections"] for r in s["rows"]]


@pytest.mark.asyncio
async def test_menu_keeps_departments_and_lab_tests_alongside_treatments():
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=True)):
        await m._send_main_menu({"id": "c1", "plan": PLAN}, PHONE, "en")
    assert _menu_ids(m) == [
        "menu_treatments", "menu_concern", "menu_book", "menu_services",
        "menu_doctors", "menu_lab_tests", "menu_emergency", "menu_human",
    ]


@pytest.mark.asyncio
async def test_menu_fits_the_whatsapp_list_limit():
    """Meta allows 10 rows per list and there is no search box in the sheet —
    an eleventh row is silently dropped, not scrolled to."""
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=True)):
        await m._send_main_menu({"id": "c1", "plan": PLAN}, PHONE, "en")
    rows = [r for s in m.whatsapp.send_interactive_list.await_args.kwargs["sections"] for r in s["rows"]]
    assert len(rows) <= 10
    for row in rows:
        assert len(row["title"]) <= 24
        assert len(row.get("description") or "") <= 72


@pytest.mark.asyncio
async def test_menu_without_a_published_treatment_is_an_ordinary_hospital_menu():
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=False)):
        await m._send_main_menu({"id": "c1", "plan": PLAN}, PHONE, "en")
    assert _menu_ids(m) == ["menu_book", "menu_services", "menu_doctors", "menu_lab_tests",
                            "menu_emergency", "menu_human"]


# ── starter treatments: the hybrid clinic names the list ─────────────────────

def test_starter_request_model_validates_the_specialty():
    from pydantic import ValidationError

    from app.routers.admin import TreatmentStarterRequest
    from app.services.specialty_catalog import STARTER_TREATMENTS

    assert TreatmentStarterRequest().specialty is None
    for specialty in STARTER_TREATMENTS:
        assert TreatmentStarterRequest(specialty=specialty).specialty == specialty
    with pytest.raises(ValidationError):
        TreatmentStarterRequest(specialty="cardiology")


def test_starter_endpoint_requires_a_specialty_for_a_hybrid_plan():
    """A derma clinic's list is decided by its plan. A multi-specialty hospital
    has no single list, so it has to say which one — never a silent default."""
    from fastapi import HTTPException

    from app.routers.admin import _starter_specialty

    assert _starter_specialty({"plan": "derma"}, "dental") == "dermatology"
    assert _starter_specialty({"plan": "ivf"}, None) == "fertility"
    assert _starter_specialty({"plan": PLAN}, "dental") == "dental"
    assert _starter_specialty({"plan": PLAN}, "dermatology") == "dermatology"

    with pytest.raises(HTTPException) as missing:
        _starter_specialty({"plan": PLAN}, None)
    assert missing.value.status_code == 400

    # An override-enabled general clinic is in the same boat.
    with pytest.raises(HTTPException) as override:
        _starter_specialty({"plan": "polyclinic", "features": {"specialty_treatments": True}}, None)
    assert override.value.status_code == 400


# ── registry sweep: every list that validates or displays a plan ─────────────

def test_onboarding_and_update_validators_accept_the_plan():
    from app.routers.clinics import CreateClinicRequest, UpdateClinicRequest

    req = CreateClinicRequest(
        name="Aayush Hospitals", whatsapp_number="+919876543210", plan=PLAN,
        meta_phone_number_id="000000000000", meta_access_token="EAAG_test",
    )
    assert req.plan == PLAN
    assert UpdateClinicRequest(plan=PLAN).plan == PLAN


def test_platform_router_lists_the_plan():
    src = (REPO / "app" / "routers" / "platform.py").read_text(encoding="utf-8")
    assert f'"{PLAN}"' in src.split("valid_plans = {")[1].split("}")[0]
    assert f'"{PLAN}": 0' in src.split("clinics_by_plan = {")[1].split("}")[0]


def test_message_accounting_fallback_lists_the_plan():
    src = (REPO / "app" / "services" / "message_accounting.py").read_text(encoding="utf-8")
    assert re.search(rf'"{PLAN}": \{{"included_messages_month": 5000', src)


def test_platform_console_offers_styles_and_filters_the_plan():
    html = (REPO / "admin" / "platform.html").read_text(encoding="utf-8")
    assert f".badge-{PLAN} " in html
    assert f'value="{PLAN}"' in html.split('id="ccPlan"')[1].split("</select>")[0]
    assert f'value="{PLAN}"' in html.split('id="planFilter"')[1].split("</select>")[0]
    # This plan DOES dispatch reports, unlike the four specialty plans.
    assert f"'{PLAN}'" in re.search(r"const PLANS_WITH_LAB_REPORTS = \[([^\]]+)\]", html).group(1)


def test_the_hospital_panel_url_serves_the_admin_panel():
    src = (REPO / "app" / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/hospital-panel")' in src
    block = src.split('@app.get("/derma-panel")')[1].split("async def")[0]
    assert "/hospital-panel" in block


def test_migration_078_widens_both_plan_constraints():
    sql = (REPO / "migrations" / "078_multispecialty_plan.sql").read_text(encoding="utf-8")
    assert sql.count(f"'{PLAN}'") >= 4  # guard + clinics CHECK + plan_tiers CHECK + tier row
    assert "plan_tiers" in sql and "ON CONFLICT" in sql
    assert "ALTER TABLE appointments" not in sql  # no schema change: the catalogue already mixes
    assert (REPO / "migrations" / "rollback" / "078_down.sql").exists()


def test_admin_panel_lets_a_hybrid_clinic_choose_the_starter_list():
    html = (REPO / "admin" / "index.html").read_text(encoding="utf-8")
    assert 'id="trtStarterSpecialty"' in html
    block = html.split('id="trtStarterSpecialty"')[1].split("</select>")[0]
    for specialty in ("dermatology", "ophthalmology", "dental", "fertility"):
        assert f'value="{specialty}"' in block
