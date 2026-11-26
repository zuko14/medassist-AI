"""The `pharmacy` plan registry and eligibility (RXR-ADR-1/2/9/11; VERIFICATION D13 / D14).

The plan has no booking, reminders or lab features and is not a specialty plan; Pharmacy OS becomes eligible for it
without OPD while every one of the 12 existing plans keeps exactly its old answer; the voice receptionist's scope for
a pharmacy offers no doctor or test booking; the WhatsApp menu offers no booking.
"""

import pytest

from app.services import rx_retail
from app.services.tenant import (HYBRID_SPECIALTY_PLANS, PHARMACY_PLANS, PLAN_FEATURES, SPECIALTY_BY_PLAN, has_feature,
                                 opd_enabled, rx_access, rx_eligible)
from app.voice import responses as R
from app.voice.scope import route, scope_for

OLD_PLANS = ("soloclinic", "diagstream", "diagbooking", "essential", "polyclinic", "enterprise", "derma", "eye", "dental",
             "ivf", "multispecialty", "womenchild")


def _c(plan, opd=False, rx=None, **kw):
    feats = {"opd_enabled": True} if opd else {}
    if rx is not None:
        feats["pharmacy_enabled"] = rx
    return {"plan": plan, "account_type": "tenant", "features": feats, **kw}


def test_plan_features():
    f = PLAN_FEATURES["pharmacy"]
    assert not f & {"booking", "reminders", "lab_reports", "lab_test_booking", "diagnostic_reports", "specialty_treatments"}
    assert {"multilingual", "clinical_firewall", "compliance_dpdp", "multi_branch"} <= f
    assert "pharmacy" not in SPECIALTY_BY_PLAN and "pharmacy" not in HYBRID_SPECIALTY_PLANS
    assert PHARMACY_PLANS == {"pharmacy"}


@pytest.mark.parametrize("plan", OLD_PLANS)
def test_existing_plans_keep_their_answer(plan):
    # exactly the pre-150 rule: eligible only with OPD on (which itself needs the plan's booking feature)
    for opd in (False, True):
        assert rx_eligible(_c(plan, opd)) == opd_enabled(_c(plan, opd)), (plan, opd)


def test_pharmacy_is_eligible_without_opd_and_still_needs_the_opt_in():
    assert rx_eligible(_c("pharmacy")) is True and opd_enabled(_c("pharmacy", opd=True)) is False
    assert rx_access(_c("pharmacy")) is None                                  # never on without the owner's toggle
    assert rx_access(_c("pharmacy", rx=True)) == "ACTIVE"
    assert rx_eligible({**_c("pharmacy"), "account_type": "corporate_partner"}) is False
    assert has_feature(_c("pharmacy"), "booking") is False


def test_voice_scope_offers_no_booking():
    s = scope_for(_c("pharmacy", rx=True), doctors=[])
    assert (s.kind, s.doctors, s.labs) == ("pharmacy", False, False)
    assert route("BOOKING", s) == "NOT_OFFERED" and route("LAB_BOOKING", s) == "NOT_OFFERED"
    assert route("FEES", s) == "NOT_OFFERED" and route("PHARMACY_REFILL", s) == "PHARMACY_REFILL"
    for plan in OLD_PLANS:                                                     # unchanged for every other plan
        assert scope_for(_c(plan), doctors=None).kind != "pharmacy"
    for lang in ("en", "hi", "te"):
        text = R.T["help_menu_pharmacy"][lang].lower()
        assert "appointment" not in text and "doctor" not in text and "test" not in text and "अपॉइंटमेंट" not in text


def test_whatsapp_menu_offers_no_booking():
    for lang in ("en", "hi", "te"):
        rows = rx_retail.menu_rows(lang)
        ids = [r["id"] for r in rows]
        assert ids == ["rx_menu_refill", "rx_menu_send", "rx_menu_stock", "rx_menu_hours", "rx_menu_staff", "menu_emergency"]
        assert not [i for i in ids if "book" in i or "doctor" in i or "lab" in i]
        assert all(len(r["title"]) <= 24 for r in rows)
