"""Kriya Ketu: "add a doctor" understood as said (session 52, field report from the owner's screenshots).

Reported: a spoken new-doctor profile ("Doctor name was Ramesh Kumar and his specialization is dermatologist ...")
opened "Change a doctor's specialization" with existing Dr. Suresh Kumar picked (a shared surname), and
"At the Dr. Ramesh Kumar, his fees is about 500 ... morning timings 9 to 10" opened "Change a doctor's hours".
"I mean, register a new doctor." then opened an empty "Add a doctor" form, losing everything already said.

Fixed (deterministic, zero-LLM):
- a name word preceded by a different first name never resolves to a doctor ("Ramesh Kumar" != Dr Suresh Kumar);
- "doctor name was X" is a new-doctor statement like "doctor name is X"; "named X" / "called X" read the name;
- a sentence routed to an edit-doctor action that dictates 3+ profile parts (fee / specialty / hours / days /
  branch), with no change verb, for nobody on the list, is "Add a doctor";
- "I mean, register a new doctor" carries the details said just above to the task named now;
- "fee 500, morning 10 to 12" never reads the fee as Rs 12; "10 o'clock to 12 o'clock" is a window;
  "the department dermatology" states the department; "Sunil" is not Sunday; "from Ameerpet branch" = ameerpet.
"""

import uuid
from unittest.mock import patch

import pytest

from app.assistant import resolve, rules
from app.routers import assistant as assistant_router
from tests.test_assistant_doctors import NOW, S, _answer, _ask, _confirm, _doc, _reset, client, docs  # noqa: F401
from tests.test_assistant_runs import as_user, db  # noqa: F401  (fixtures)

SURESH = {"id": "d-suresh", "name": "Dr. Suresh Kumar"}
ANITHA = {"id": "d-anitha", "name": "Dr. Anitha Rao"}
KIRAN = {"id": "d-kiran", "name": "Dr Kiran Reddy"}
DOCTORS = [SURESH, ANITHA, KIRAN]

# The owner's two utterances, as the transcript showed them.
SAID_1 = ("Doctor name was Ramesh Kumar and his specialization is dermatologist and also the department dermatology "
          "and his fees is 500 rupees and his morning slots are timings are 10 o'clock to 12 o'clock and evening "
          "slots are 5 o'clock to 8 o'clock.")
SAID_2 = ("At the Dr. Ramesh Kumar, his fees is about 500 and department is cardiology and he's from Ameerpet branch "
          "and also his morning timings are 9 to 10 o'clock and also the evening timings are 6 to 10 o'clock.")


# ── doctor names ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, ids", [
    ("Ramesh Kumar evening is now 5 to 8", []),                    # another first name: never Suresh
    (SAID_1, []), (SAID_2, []),
    ("Dr Kumar evening is now 5 to 8", ["d-suresh"]),              # the surname alone still finds him
    ("Suresh Kumar evening is now 5 to 8", ["d-suresh"]),
    ("change Kumar's fee to 600", ["d-suresh"]),                   # an instruction word before the name
    ("Monday Kumar is off", ["d-suresh"]),
    ("Dr Anitha's evening is now 5 to 8", ["d-anitha"]),
    ("Kumar Suresh evening 5 to 8", ["d-suresh"]),                 # either order of his own name
    ("Ramesh Kumar's booking is with Dr Kumar", ["d-suresh"]),     # one clean mention is enough
])
def test_a_different_first_name_never_resolves_to_a_look_alike(text, ids):
    assert [d["id"] for d in resolve.resolve_doctor(text, DOCTORS)] == ids


@pytest.mark.parametrize("text, name", [
    ("Doctor name was Ramesh Kumar and his specialization is dermatologist", "Dr Ramesh Kumar"),
    ("the doctor's name was Priya Sharma, fee 500", "Dr Priya Sharma"),
    ("register a new doctor named Ramesh Kumar", "Dr Ramesh Kumar"),
    ("add a doctor called Srikanth for cardiology", "Dr Srikanth"),
    ("add new doctor named Dr. Priya", "Dr. Priya"),
    (SAID_2, "Dr Ramesh Kumar"),
    ("Add Dr Kiran Kumar, dermatologist, ₹600, Mon-Sat mornings 9 to 1", "Dr Kiran Kumar"),  # unchanged
    ("create the doctor srikanth cardiology department", "Dr Srikanth"),                     # unchanged
    ("doctor name is", None), ("add doctor", None),
])
def test_resolve_new_doctor_reads_the_spoken_forms(text, name):
    assert resolve.resolve_new_doctor(text) == name


# ── profile values ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, price", [
    ("add a new doctor Ramesh, fee 500, morning 10 to 12", ["amount", "500"]),   # was ["amount", "12"]
    ("Dr Rao morning 9 to 12, fee to 600", ["amount", "600"]),
    ("his fees is about 500 and department is cardiology", ["amount", "500"]),
    ("consultation fee: 700", ["amount", "700"]),
    ("change dr anitha consultation fee to 600", ["amount", "600"]),          # unchanged
    ("increase Dr Anitha's fee by 100", ["delta", "100"]),                    # unchanged
    ("fees is ₹100", ["amount", "100"]),                                      # unchanged
    ("morning 10 to 12", None), ("fee 10 to 12", None), ("fee 15 minutes", None),
])
def test_a_fee_is_never_read_from_the_hours(text, price):
    assert resolve.resolve_price(text) == price


@pytest.mark.parametrize("text, window", [
    ("10 o'clock to 12 o'clock", "10:00-12:00"), ("9 to 10 o'clock", "09:00-10:00"),
    ("10 oclock to 1 o clock", "10:00-13:00"), ("9 to 1", "09:00-13:00"),
])
def test_oclock_hours(text, window):
    assert resolve.resolve_time_window(text) == window


def test_spoken_session_hours():
    assert resolve.resolve_window_after(SAID_1, "morning") == "10:00-12:00"
    assert resolve.resolve_window_after(SAID_1, "evening") == "17:00-20:00"
    assert resolve.resolve_bare_windows("10 o'clock to 1 o'clock and 6 to 8") == {
        "morning": "10:00-13:00", "evening": "18:00-20:00"}


@pytest.mark.parametrize("text, field, value", [
    ("and also the department dermatology and his fees", "department", "Dermatology"),
    ("department of cardiology", "department", "Cardiology"),
    ("specialization dermatologist", "specialization", "Dermatologist"),
    ("department is cardiology and he's from Ameerpet", "department", "Cardiology"),
    ("the department of Dr Rao", "department", None),            # a mention, not a value
    ("Add Dr Kiran Kumar, dermatologist, ₹600", "specialization", None),
])
def test_resolve_stated_bare_specialty(text, field, value):
    assert resolve.resolve_stated(text, field) == value


@pytest.mark.parametrize("text, days", [
    ("add a new doctor Sunil Kumar mornings 9 to 1", None),      # was "Sun"
    ("Dr Satish, money matters", None),
    ("Mon-Sat", "Mon,Tue,Wed,Thu,Fri,Sat"), ("tues and thurs", "Tue,Thu"), ("mondays", "Mon"),
    ("monday to friday", "Mon,Tue,Wed,Thu,Fri"), ("Sunday", "Sun"),
])
def test_day_words_only(text, days):
    assert resolve.resolve_days(text) == days


def test_branch_from_a_spoken_profile():
    assert resolve.resolve_branch(SAID_2) == "ameerpet"
    assert resolve.resolve_branch("assign Dr Rao to Kukatpally branch mornings") == "kukatpally"  # unchanged


# ── routing ──────────────────────────────────────────────────────────────────

def test_the_reported_profiles_add_a_doctor():
    one = rules.route(SAID_1, doctors=DOCTORS, now=NOW)
    assert (one.kind, one.action_id, one.missing) == ("clarify", "doctor.create", ("days",)), one
    assert one.params == {"name": "Dr Ramesh Kumar", "specialization": "Dermatologist", "department": "Dermatology",
                          "fee": ["amount", "500"], "morning": "10:00-12:00", "evening": "17:00-20:00"}
    two = rules.route(SAID_2, doctors=DOCTORS, now=NOW)
    assert (two.kind, two.action_id) == ("clarify", "doctor.create"), two
    assert two.params == {"name": "Dr Ramesh Kumar", "department": "Cardiology", "fee": ["amount", "500"],
                          "morning": "09:00-10:00", "evening": "18:00-22:00", "branch": "ameerpet"}
    assert set(two.missing) == {"specialization", "days"}


@pytest.mark.parametrize("text, action, doctor", [
    ("Dr Kumar evening is now 5 to 8", "doctor.schedule", "Dr. Suresh Kumar"),
    ("change Dr Suresh Kumar's consultation fee to 600", "doctor.fee", "Dr. Suresh Kumar"),
    ("change Dr Anitha's specialization to cosmetology", "doctor.details", "Dr. Anitha Rao"),
    # an existing doctor's several details stay an edit of that doctor
    ("Dr Anitha fees 500, department cardiology, morning 9 to 1", None, "Dr. Anitha Rao"),
    # a change verb keeps it an edit even for a name not on the list (the picker asks who)
    ("change Dr Ramesh's fee to 500, department cardiology and morning 9 to 1", None, None),
])
def test_edits_of_existing_doctors_stay_edits(text, action, doctor):
    r = rules.route(text, doctors=DOCTORS, now=NOW)
    assert r.action_id != "doctor.create", r
    if action:
        assert r.action_id == action, r
        assert r.params.get("doctor") == doctor, r
    else:  # several edits match: Kriya asks which (as before); never "Add a doctor"
        assert r.kind == "clarify" and set(r.candidate_actions) <= rules.DOCTOR_EDITS, r


def test_two_profile_parts_are_not_enough_to_add_a_doctor():
    r = rules.route("Dr Ramesh morning timings 9 to 12 and evening timings 5 to 8, Mon to Sat", doctors=DOCTORS, now=NOW)
    assert r.action_id == "doctor.schedule" and "doctor" in r.missing, r   # hours + days: a picker, not a new doctor


def test_unknown_doctor_list_never_switches():
    assert not rules.prefer_new_doctor(["doctor.schedule"], SAID_2, None)
    assert rules.prefer_new_doctor(["doctor.schedule"], SAID_2, DOCTORS)
    assert not rules.prefer_new_doctor(["doctor.schedule", "leave.create"], SAID_2, DOCTORS)


@pytest.mark.parametrize("text, yes", [
    ("I mean, register a new doctor.", True), ("No, I meant add a new doctor", True),
    ("sorry, I mean add a doctor", True), ("Kriya, I meant register a doctor", True),
    ("it's a new doctor", True), ("not that, add a doctor", True),
    ("register a new doctor", False), ("what I mean is nothing", False), ("add a doctor I mean", False),
])
def test_is_correction(text, yes):
    assert rules.is_correction(text) is yes


# ── through the API ──────────────────────────────────────────────────────────

def test_reported_profile_end_to_end_adds_ramesh_never_touches_suresh(db, docs):  # noqa: F811
    _reset(doctors=[_doc(id="d-suresh", name="Dr. Suresh Kumar")])
    out = _ask(SAID_1)
    assert (out["state"], out["action_id"]) == ("NEEDS_CLARIFICATION", "doctor.create"), out
    assert out["message"].startswith("Add a doctor")
    assert out["cards"][0]["missing"] == ["days"]
    out = _answer(out, days="Mon-Sat")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "Add Dr Ramesh Kumar" in str(out["cards"]) and S["writes"] == []
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, body), = S["writes"]
    assert body["name"] == "Dr Ramesh Kumar" and body["consultation_fee"] == 500
    assert (body["specialization"], body["department"]) == ("Dermatologist", "Dermatology")
    assert (body["morning_start"], body["morning_end"], body["evening_start"], body["evening_end"]) == \
        ("10:00", "12:00", "17:00", "20:00")
    assert body["available_days"] == "Mon,Tue,Wed,Thu,Fri,Sat"


def test_i_mean_carries_the_details_over_and_closes_the_wrong_form(db, docs):  # noqa: F811
    _reset(doctors=[_doc(id="d-suresh", name="Dr. Suresh Kumar")])
    wrong = _ask("Dr Ramesh timings 9 to 1")                     # understood as "Change a doctor's hours"
    assert (wrong["state"], wrong["action_id"]) == ("NEEDS_CLARIFICATION", "doctor.schedule"), wrong
    out = _ask("I mean, register a new doctor.")
    assert (out["state"], out["action_id"]) == ("NEEDS_CLARIFICATION", "doctor.create"), out
    missing = out["cards"][0]["missing"]
    assert "name" not in missing and "morning" not in missing and "evening" not in missing, out
    old = client.get("/admin/assistant/runs?limit=5").json()
    old = next(r for r in old if r["run_id"] == wrong["run_id"])
    assert (old["state"], old["error_code"]) == ("CANCELLED", "corrected")
    out = _answer(out, specialization="Dermatology", department="Skin", fee="600", days="Mon-Sat")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, body), = S["writes"]
    assert body["name"] == "Dr Ramesh" and (body["morning_start"], body["morning_end"]) == ("09:00", "13:00")


def test_i_mean_replaces_an_unconfirmed_preview(db, docs):  # noqa: F811
    wrong = _ask("Dr Anitha's evening is now 6 to 8")
    assert wrong["state"] == "NEEDS_CONFIRMATION", wrong
    out = _ask("No, I meant change Dr Anitha's consultation fee to 600")
    assert out["action_id"] == "doctor.fee", out
    runs = {r["run_id"]: r for r in client.get("/admin/assistant/runs?limit=5").json()}
    # the fee sentence has every detail itself, so nothing is carried and the preview is left as it was
    assert runs[wrong["run_id"]]["state"] == "NEEDS_CONFIRMATION"
    assert S["writes"] == []


def test_a_correction_without_an_open_run_routes_as_usual(db, docs):  # noqa: F811
    out = _ask("I mean, register a new doctor.")
    assert (out["state"], out["action_id"]) == ("NEEDS_CLARIFICATION", "doctor.create"), out
    assert "name" in out["cards"][0]["missing"]


def test_a_correction_never_carries_across_modules(db, docs):  # noqa: F811
    first = _ask("hospital holiday on 1 November")
    out = _ask("I mean, register a new doctor.")
    assert out["action_id"] == "doctor.create" and "name" in out["cards"][0]["missing"], out
    runs = {r["run_id"]: r for r in client.get("/admin/assistant/runs?limit=5").json()}
    assert runs[first["run_id"]].get("error_code") != "corrected"


def test_a_correction_never_touches_a_confirmed_run(db, docs):  # noqa: F811
    done = _ask("Dr Anitha's evening is now 6 to 8")
    assert _confirm(done).json()["state"] == "COMPLETED_VERIFIED"
    out = _ask("I mean, register a new doctor.")
    assert "name" in out["cards"][0]["missing"], out             # nothing carried from a finished run
    runs = {r["run_id"]: r for r in client.get("/admin/assistant/runs?limit=5").json()}
    assert runs[done["run_id"]]["state"] == "COMPLETED_VERIFIED"


def test_llm_fallback_uses_the_same_new_doctor_rule():
    from app.assistant import llm
    intent = llm.Intent.model_validate({"kind": "command", "action_id": "doctor.schedule", "question_for_user": None,
                                        "slots": {"doctor_text": "Dr Ramesh Kumar"}})
    body = assistant_router.RequestIn(client_request_id=uuid.uuid4(), text=SAID_2)
    with patch.object(rules, "_now", lambda: NOW):
        r = assistant_router._from_llm(intent, body, None, DOCTORS)
    assert (r.kind, r.action_id) == ("clarify", "doctor.create"), r
