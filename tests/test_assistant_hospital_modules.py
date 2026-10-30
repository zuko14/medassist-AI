"""Kriya Ketu over the Hospital OS modules (migrations 115-121): diagnostic worklist, LIS sign-off,
casualty / MLC, service tariffs, IPD receivables, credit notes, GSTR-1, certificates, consents, deposits.

Reads use the page's own endpoint and gate; every write is a hand-off a person completes on the page.
"""

from types import SimpleNamespace

import pytest

from app.assistant import policy, rules
from app.assistant.registry import REGISTRY

NEW_READS = ("orders.to_collect", "lis.pending_signoff", "emergency.board", "services.prices",
             "ipd_finance.receivables")
NEW_HANDOFFS = ("orders.collect", "lis.enter_results", "lis.sign", "emergency.mlc",
                "services.edit", "billing.gstr1", "documents.certificate",
                "documents.consent")
# voice-first (2026-10-30): confirmed writes with a read-back
VOICE_WRITES = ("emergency.register", "ipd_finance.deposit", "services.price", "ot.schedule", "services.create",
                "billing.credit_note", "ipd_finance.credit")
CLINIC = {"id": "c1", "plan": "hospital", "opd_enabled": True, "opd_state": "READY", "ipd_access": "ACTIVE",
          "features": {"kriya_ketu": True, "opd": True, "ipd": True}}


def _user(role="staff", perms=(), staff_role=None):
    return SimpleNamespace(role=role, permissions=list(perms), staff_role=staff_role, username="u",
                           branch_id=None, doctor_id=None)


def test_registered_with_the_right_tier():
    assert all(REGISTRY[a].risk == "R0" for a in NEW_READS)
    assert all(REGISTRY[a].risk == "H" for a in NEW_HANDOFFS)
    assert all(REGISTRY[a].risk in ("R2", "R3") and REGISTRY[a].verify for a in VOICE_WRITES)


@pytest.mark.parametrize("text, action", [
    ("show samples to collect", "orders.to_collect"),
    ("list pending investigations", "orders.to_collect"),
    ("enni sample collect cheyali", "orders.to_collect"),
    ("reports pending sign off", "lis.pending_signoff"),
    ("any critical results", "lis.pending_signoff"),
    ("who is in casualty", "emergency.board"),
    ("list casualty patients", "emergency.board"),
    ("emergency mein kaun hai", "emergency.board"),
    ("what is the price of x ray chest", "services.prices"),
    ("ipd outstanding", "ipd_finance.receivables"),
    ("how much is pending from tpa dues", "ipd_finance.receivables"),
    ("collect sample for ravi", "orders.collect"),
    ("enter results for cbc", "lis.enter_results"),
    ("sign the report for meena", "lis.sign"),
    ("rapid admit an accident case", "emergency.register"),
    ("register mlc for the accident patient", "emergency.mlc"),
    ("register emergency patient", "emergency.register"),
    ("add a new service for physiotherapy", "services.create"),
    ("issue a credit note on bill 104", "billing.credit_note"),
    ("download gstr1 for october", "billing.gstr1"),
    ("give a medical certificate to ravi", "documents.certificate"),
    ("take consent for surgery", "documents.consent"),
    ("take advance deposit from bed 12", "ipd_finance.deposit"),
])
def test_phrases_route_to_the_action(text, action):
    r = rules.route(text)
    assert (r.kind, r.action_id) in (("action", action), ("clarify", action)), (text, r)


def test_bulk_sign_off_is_never_offered():
    r = rules.route("sign all reports")
    assert r.action_id != "lis.sign"


@pytest.mark.parametrize("action, user, ok", [
    # role-implied grants count up front, exactly as the route's holder decides
    ("lis.pending_signoff", _user(staff_role="PATHOLOGIST"), True),
    ("lis.pending_signoff", _user(perms=["LAB_ENTRY"]), True),
    ("lis.pending_signoff", _user(perms=["OPD_FRONT_DESK"]), False),
    ("orders.to_collect", _user(perms=["IPD_NURSING"]), True),
    ("orders.to_collect", _user(perms=["LEADS_MANAGE"]), False),
    ("emergency.board", _user(perms=["OPD_FRONT_DESK"]), True),
    ("emergency.board", _user(perms=["LAB_ENTRY"]), False),
    ("services.prices", _user(perms=["OPD_BILLING"]), True),
    ("services.prices", _user(perms=["LAB_ENTRY"]), False),
    ("ipd_finance.receivables", _user(perms=["IPD_BILLING"]), True),
    ("ipd_finance.receivables", _user(perms=["IPD_NURSING"]), False),
    ("billing.credit_note", _user(staff_role="CASHIER"), True),
    ("billing.credit_note", _user(perms=["OPD_FRONT_DESK"]), False),
    ("documents.certificate", _user(perms=["OPD_CLINICAL"]), True),
    ("documents.certificate", _user(perms=["OPD_BILLING"]), False),
    ("billing.gstr1", _user(role="clinic_admin"), True),
])
def test_precheck_matches_the_route_holder(action, user, ok, monkeypatch):
    spec = REGISTRY[action]
    monkeypatch.setattr("app.assistant.registry.opd_enabled", lambda c: True)
    monkeypatch.setattr("app.assistant.registry.ipd_access", lambda c: "ACTIVE")
    assert policy.precheck(spec, user, CLINIC).allowed is ok


def test_modules_are_off_without_opd_or_ipd(monkeypatch):
    monkeypatch.setattr("app.assistant.registry.opd_enabled", lambda c: False)
    monkeypatch.setattr("app.assistant.registry.ipd_access", lambda c: None)
    bare = {"id": "c2", "features": {}}
    for a in ("orders.to_collect", "lis.pending_signoff", "emergency.board", "ipd_finance.receivables"):
        assert policy.precheck(REGISTRY[a], _user(role="clinic_admin"), bare).reason == "feature_disabled"


def test_cards_show_the_page_rows():
    worklist = {"orders": [{"patient_name": "Ravi", "mrn": "M1", "item_name": "CBC", "urgency": "stat",
                            "status": "billed", "doctor_name": "Dr A"}], "total": 14}
    out = REGISTRY["orders.to_collect"].present([SimpleNamespace(data=worklist)], {})
    card = out["cards"][0]
    assert card["title"] == "Samples to collect (14)" and card["rows"][0][:4] == ["Ravi", "M1", "CBC", "Stat"]

    money = {"total_due_paise": 1250000, "ageing_paise": {"0-30": 1000000, "31-60": 250000, "61-90": 0, "90+": 0},
             "deposits_held_paise": 50000}
    rows = REGISTRY["ipd_finance.receivables"].present([SimpleNamespace(data=money)], {})["cards"][0]["rows"]
    assert ["Total due", "₹12,500"] in rows and ["Deposits held", "₹500"] in rows

    services = {"services": [{"code": "XR1", "name": "X Ray Chest PA", "category": "radiology",
                              "price_paise": 45000}, {"code": "US1", "name": "USG Abdomen",
                              "price_paise": 90000}, {"code": "OLD", "name": "X Ray Chest old",
                              "active": False}]}
    card = REGISTRY["services.prices"].present([SimpleNamespace(data=services)], {"q": "x ray chest"})["cards"][0]
    assert card["rows"] == [["XR1", "X Ray Chest PA", "Radiology", "₹450"]]


# ── Operation theatre (migration 122): one read, three hand-offs ─────────────

OT_HANDOFFS = ("ot.checklist", "ot.finish")  # booking is by voice now; the theatre records are not


def test_ot_actions_are_a_read_and_hand_offs_only():
    assert REGISTRY["ot.today"].risk == "R0" and REGISTRY["ot.today"].endpoint == ("GET", "/admin/ot/schedule")
    for a in OT_HANDOFFS:   # ADR-IPD-08: no executable clinical write
        spec = REGISTRY[a]
        assert spec.risk == "H" and spec.handoff.page == "ot" and not (spec.call or spec.endpoint)


@pytest.mark.parametrize("text, action", [
    ("surgeries today", "ot.today"),
    ("who is in ot", "ot.today"),
    ("ot lo evaru unnaru", "ot.today"),
    ("aaj ke operation", "ot.today"),
    ("book a surgery for ravi", "ot.schedule"),
    ("schedule surgery tomorrow 9 am", "ot.schedule"),
    ("fill the who checklist", "ot.checklist"),
    ("surgical safety checklist", "ot.checklist"),
    ("finish surgery for meena", "ot.finish"),
    ("write the post op note", "ot.finish"),
])
def test_ot_phrases_route_to_the_action(text, action):
    r = rules.route(text)
    assert (r.kind, r.action_id) in (("action", action), ("clarify", action)), (text, r)


def test_ot_surgery_consent_stays_a_document_and_bulk_finish_is_not_offered():
    assert rules.route("take consent for surgery").action_id == "documents.consent"
    assert rules.route("complete all surgeries").action_id != "ot.finish"
    assert rules.route("open operation theatre").kind == "nav"


@pytest.mark.parametrize("action, user, ok", [
    # pre-122 logins: admissions desk books, ward nurses tick the checklist, doctors (role) do everything
    ("ot.today", _user(perms=["IPD_NURSING"]), True),
    ("ot.today", _user(perms=["IPD_BILLING"]), False),
    ("ot.schedule", _user(perms=["IPD_ADMISSIONS"]), True),
    ("ot.schedule", _user(perms=["IPD_NURSING"]), False),
    ("ot.checklist", _user(perms=["IPD_NURSING"]), True),
    ("ot.checklist", _user(perms=["IPD_ADMISSIONS"]), False),
    ("ot.finish", _user(perms=["OPD_CLINICAL"], staff_role="DOCTOR"), True),
    ("ot.finish", _user(perms=["IPD_NURSING"], staff_role="NURSE"), False),
    ("ot.finish", _user(role="clinic_admin"), True),
])
def test_ot_precheck_matches_the_route_holder(action, user, ok, monkeypatch):
    monkeypatch.setattr("app.assistant.registry.ipd_access", lambda c: "ACTIVE")
    assert policy.precheck(REGISTRY[action], user, CLINIC).allowed is ok


def test_ot_is_off_without_ipd(monkeypatch):
    monkeypatch.setattr("app.assistant.registry.ipd_access", lambda c: None)
    for a in ("ot.today", *OT_HANDOFFS):
        assert policy.precheck(REGISTRY[a], _user(role="clinic_admin"), {"id": "c2"}).reason == "feature_disabled"


def test_ot_card_shows_the_theatre_list():
    day = {"surgeries": [{"scheduled_start": "2026-10-29T03:30:00+00:00", "theatre_name": "OT 1",
                          "patient_name": "Ravi", "mrn": "MRN-000001", "procedure_name": "Appendicectomy",
                          "surgeon_name": "Dr. Iyer", "status": "in_theatre"}]}
    card = REGISTRY["ot.today"].present([SimpleNamespace(data=day)], {})["cards"][0]
    assert card["title"] == "Theatre list today (1)"
    assert card["rows"][0] == ["29 Oct, 09:00", "OT 1", "Ravi", "MRN-000001", "Appendicectomy", "Dr. Iyer",
                               "In theatre"]
