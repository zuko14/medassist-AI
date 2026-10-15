"""Kriya Ketu: "register X" goes to X's own form, never the patient form.

Reported: "Register doctor Srinivas" opened the patient registration form
(phone + data consent + WhatsApp consent). patient.register owned the bare word
"register", so every "register <doctor / nurse / branch / test ...>" landed on it.
Strict: every phrase here must route exactly (the corpus allows 5 % misses).
"""

import pytest

from app.assistant import rules
from app.assistant.registry import REGISTRY

DOCTORS = [{"id": "d1", "name": "Dr. Meena Rao"}, {"id": "d2", "name": "Dr. Srinivas Reddy"}]


def _route(text):
    return rules.route(text, doctors=DOCTORS)


DOCTOR_CREATE = [
    "Register doctor Srinivas", "register a doctor", "register the doctor Anil", "register Dr Kiran Kumar",
    "Register dr. Srinivas", "please register doctor Anil", "register a new doctor", "add doctor Srinivas",
    "add a doctor", "create doctor Srinivas", "onboard doctor Srinivas", "new doctor Kiran",
    "doctor ni register cheyyi", "doctor ko register karo", "డాక్టర్ ని రిజిస్టర్ చేయండి", "डॉक्टर को रजिस्टर करो",
]


@pytest.mark.parametrize("text", DOCTOR_CREATE)
def test_doctor_registration_opens_the_doctor_form(text):
    r = _route(text)
    assert (r.kind, r.action_id) == ("clarify", "doctor.create"), (text, r)
    assert "phone" not in r.missing and "data_consent" not in r.missing


@pytest.mark.parametrize("text,name", [("Register doctor Srinivas", "Dr Srinivas"),
                                       ("register Dr Kiran Kumar", "Dr Kiran Kumar"),
                                       ("onboard doctor anil", "Dr Anil"),
                                       ("add doctor Srinivas", "Dr Srinivas")])
def test_doctor_name_is_read_from_the_sentence(text, name):
    assert _route(text).params.get("name") == name


@pytest.mark.parametrize("text,action", [
    ("register staff Priya", "staff.create"), ("register a nurse", "staff.create"),
    ("register nurse Priya", "staff.create"), ("add nurse Priya", "staff.create"),
    ("register receptionist", "staff.create"), ("register branch Kukatpally", "branch.manage"),
    ("register company Infosys", "corporate.manage"),
])
def test_other_registrations_hand_off_to_their_own_page(text, action):
    r = _route(text)
    assert (r.kind, r.action_id) == ("action", action), (text, r)


def test_a_nurse_login_prefills_the_nurse_role():
    assert _route("register nurse Priya").params.get("role") == "NURSE"


@pytest.mark.parametrize("text", ["register a lab test CBC", "register test thyroid"])
def test_lab_test_registration_opens_the_test_form(text):
    assert _route(text).action_id == "lab_test.create"


@pytest.mark.parametrize("text", ["register ward", "register bed 4", "doctor registration", "register a login",
                                  # any "register a/an/the (new) <thing>": no list needed
                                  "register a ventilator", "register an MRI machine", "register the new OT",
                                  "register new ambulance", "register ambulance"])
def test_other_objects_never_reach_the_patient_form(text):
    assert _route(text).action_id != "patient.register", text


def test_doctor_registration_alone_is_the_doctor_form():
    assert _route("doctor registration").action_id == "doctor.create"


def test_a_registration_number_edit_is_not_a_new_doctor():
    assert _route("update doctor registration number").action_id != "doctor.create"


@pytest.mark.parametrize("text", ["create doctor leave for Rao", "create leave for doctor Rao", "add dr rao leave tomorrow",
                                  "add doctor Rao leave on Friday", "apply leave for Dr Meena tomorrow"])
def test_a_doctor_leave_never_opens_the_new_doctor_form(text):
    assert _route(text).action_id == "doctor_leave.create", text


@pytest.mark.parametrize("text", [
    "register patient Ravi 9876543210", "register Ravi Kumar 9876543210", "new patient Ravi",
    "register Ravi with Dr Rao", "register patient for doctor Meena", "patient registration",
    "register karo Ravi", "Ravi ni register cheyyi", "register a new patient Ravi", "register new patient",
    "register another patient", "register one more patient", "register existing patient Ravi",
    "register the patient for IPD",
])
def test_patient_registration_still_works(text):
    r = _route(text)
    assert r.action_id == "patient.register", (text, r)


def test_the_llm_fallback_cannot_choose_the_patient_form_for_a_doctor():
    """rules.fill guards the LLM's pick the same way the trigger router does."""
    r = rules.fill(REGISTRY["patient.register"], "Register doctor Srinivas")
    assert r.kind == "none"


def test_a_clarify_reply_on_an_open_patient_form_is_not_vetoed():
    """Continuations pass `given`; the original sentence already routed to this form."""
    r = rules.fill(REGISTRY["patient.register"], "register Ravi 9876543210", given={"data_consent": "yes"})
    assert r.action_id == "patient.register"


@pytest.mark.parametrize("text,action", [
    ("admit Ravi to ward", "ipd.admit"), ("discharge Ravi", "ipd.discharge"),
    ("transfer Ravi to ICU", "ipd.transfer"), ("show inpatients", "ipd.inpatients"),
    ("pending discharges", "ipd.discharges"), ("discharge list", "ipd.discharges"),
    ("who is admitted", "ipd.inpatients"),
])
def test_ipd_requests_route(text, action):
    assert _route(text).action_id == action, text


@pytest.mark.parametrize("text", ["discharge all admitted patients", "discharge everyone in ward 2"])
def test_a_bulk_discharge_is_never_offered(text):
    assert _route(text).kind == "none", text


def test_ipd_hand_offs_never_execute():
    for aid in ("ipd.admit", "ipd.discharge", "ipd.transfer", "ipd.vitals"):
        assert REGISTRY[aid].risk == "H" and REGISTRY[aid].call is None, aid
