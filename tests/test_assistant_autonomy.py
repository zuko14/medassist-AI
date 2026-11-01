"""Session 54: Kriya Ketu does the task, end to end, through the real /admin/assistant API (a fake panel behind it).

* doctor.create from one spoken profile with no form: the name after "Name is …", one specialty said fills both
  fields, unsaid working days are the Doctors page default (marked in the preview);
* profile.update (PUT /admin/profile, admins only), holiday.delete, doctor_leave.move (add first, then remove),
  treatment.create, branch.doctor_unassign, voice.call_number.
Every write: preview first, nothing sent before Confirm, the exact body sent, a read-back after, and the refusals
(nothing to do / not allowed) write nothing.
"""

import ast
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, rules
from app.assistant.registry import DOCTOR_DEFAULT_DAYS, REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.routers.admin import AdminUser, DoctorCreate
from app.voice.dates import IST
from tests.test_assistant_runs import CID, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=IST)
B1, B2, D1, D2 = "b-kp", "b-mp", "d-suresh", "d-anitha"
CLINIC = {"id": CID, "plan": "dental", "account_type": "tenant",
          "features": {"kriya_ketu": True, "ai_receptionist": True}}
SAID = ("Register a new doctor. Name is Suresh Varma. His department is dermatologist and also his fees is ₹500 and "
        "also he serves at the Kukatpally branch and also his morning slot timings 9 to 11 o'clock and evening slot "
        "timings 6 o'clock to 7 o'clock.")

fake = FastAPI()
S: dict = {}


def _reset(**kw):
    S.clear()
    S.update(writes=[], me={"branch_id": None, "role": "clinic_admin", "permissions": []},
             doctors=[{"id": D1, "name": "Dr. Suresh Kumar"}, {"id": D2, "name": "Dr. Anitha Rao"}],
             profile={"name": "Aura Hospital", "hospital_address": "Ameerpet, Hyderabad", "hospital_maps_link": "",
                      "hospital_emergency_number": "9000000001", "hospital_staff_phone": "",
                      "followup_enabled": True, "followup_days": 3, "health_checkins_enabled": False},
             holidays=[{"holiday_date": "2026-12-25", "name": "Christmas"}],
             leaves=[{"id": "lv1", "doctor_name": "Dr. Anitha Rao", "leave_date": "2026-11-05", "leave_type": "full"}],
             treatments=[{"id": "t1", "name": "Botox", "category": "Anti-ageing", "price_from_rupees": 9000}],
             branches=[{"id": B1, "name": "Kukatpally", "is_active": True}, {"id": B2, "name": "Madhapur",
                                                                             "is_active": True}],
             assigned={B1: [D1, D2], B2: [D2]}, jobs=[])
    S.update(kw)


@fake.get("/admin/me")
async def _me():
    return S["me"]


@fake.get("/admin/doctors")
async def _doctors():
    return S["doctors"]


@fake.post("/admin/doctors")
async def _doctor_create(request: Request):
    body = await request.json()
    S["writes"].append(("POST /admin/doctors", body))
    S["doctors"].append({"id": "new", **body})
    return S["doctors"][-1]


@fake.get("/admin/profile")
async def _profile():
    return S["profile"]


@fake.put("/admin/profile")
async def _profile_put(request: Request):
    body = await request.json()
    S["writes"].append(("PUT /admin/profile", body))
    S["profile"].update(body)
    return S["profile"]


@fake.get("/admin/holidays")
async def _holidays():
    return S["holidays"]


@fake.delete("/admin/holidays/{day}")
async def _holiday_delete(day: str):
    S["writes"].append(("DELETE /admin/holidays", day))
    S["holidays"] = [h for h in S["holidays"] if h["holiday_date"] != day]
    return {"ok": True}


@fake.get("/admin/leaves")
async def _leaves(doctor: str = ""):
    return [r for r in S["leaves"] if not doctor or r["doctor_name"] == doctor]


@fake.post("/admin/leaves")
async def _leave_add(request: Request):
    body = await request.json()
    S["writes"].append(("POST /admin/leaves", body))
    S["leaves"].append({"id": f"lv{len(S['leaves']) + 1}", "doctor_name": body["doctor_name"],
                        "leave_date": body["leave_date"], "leave_type": body["leave_type"]})
    return S["leaves"][-1]


@fake.delete("/admin/leaves/{lid}")
async def _leave_delete(lid: str):
    S["writes"].append(("DELETE /admin/leaves", lid))
    S["leaves"] = [r for r in S["leaves"] if r["id"] != lid]
    return {"ok": True}


@fake.get("/admin/appointments")
async def _appts():
    return {"appointments": [], "total": 0}


@fake.get("/admin/treatments")
async def _treatments():
    return S["treatments"]


@fake.post("/admin/treatments")
async def _treatment_add(request: Request):
    body = await request.json()
    S["writes"].append(("POST /admin/treatments", body))
    S["treatments"].append({"id": "t-new", **body})
    return S["treatments"][-1]


@fake.get("/admin/branches")
async def _branches():
    return {"branches": S["branches"]}


@fake.get("/admin/branches/{bid}/doctors")
async def _branch_doctors(bid: str):
    return {"doctor_branches": [{"doctor_id": d, "branch_id": bid} for d in S["assigned"].get(bid, [])]}


@fake.delete("/admin/branches/{bid}/doctors/{did}")
async def _unassign(bid: str, did: str):
    S["writes"].append(("DELETE branch doctor", bid, did))
    S["assigned"][bid] = [d for d in S["assigned"].get(bid, []) if d != did]
    return {"ok": True}


@fake.get("/admin/voice/outbound")
async def _jobs():
    return {"jobs": S["jobs"]}


@fake.post("/admin/voice/outbound")
async def _call(request: Request):
    body = await request.json()
    S["writes"].append(("POST /admin/voice/outbound", body))
    S["jobs"].append({"id": "j1", "patient_phone": "+91" + body["phone"], "status": "queued"})
    return {"job": S["jobs"][-1]}


client = TestClient(app)


def _staff(*perms):
    return AdminUser(username="desk1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()), staff_role="FRONT_DESK",
                     permissions=list(perms))


@pytest.fixture
def ko(as_user):  # noqa: F811
    as_user["user"] = AdminUser(username="owner", role="clinic_admin", clinic_id=CID, user_id=str(uuid.uuid4()),
                                staff_role=None, permissions=[])
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=CLINIC)), \
         patch.object(playbooks, "_now", lambda: NOW), patch.object(rules, "_now", lambda: NOW):
        yield as_user


def _ask(text, page="dashboard"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "voice",
        "ui_context": {"page": page}}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm",
                       json={"plan_hash": out["plan_hash"], **extra}).json()


def _done(text, **extra):
    out = _ask(text)
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert S["writes"] == []  # nothing before Confirm
    done = _confirm(out, **extra)
    assert done["state"] == "COMPLETED_VERIFIED", done
    return out, done


# ── doctor.create: the owner's sentence, no form ─────────────────────────────

def test_the_owners_sentence_adds_the_doctor_without_a_form(db, ko):  # noqa: F811
    out, _ = _done(SAID)
    text = str(out["cards"])
    assert "Add Dr Suresh Varma" in text and "Dermatologist (Dermatology)" in text
    assert "you did not say them" in text  # the assumed days are said out loud in the preview
    (_, body), = S["writes"]
    assert body["name"] == "Dr Suresh Varma" and body["consultation_fee"] == 500
    assert (body["specialization"], body["department"]) == ("Dermatologist", "Dermatology")
    assert body["available_days"] == DOCTOR_DEFAULT_DAYS == "Mon,Tue,Wed,Thu,Fri"
    assert (body["morning_start"], body["morning_end"], body["evening_start"], body["evening_end"]) == \
        ("09:00", "11:00", "18:00", "19:00")
    assert body["branch_id"] == B1


def test_default_days_are_the_endpoints_own_default():
    assert DoctorCreate.model_fields["available_days"].default == DOCTOR_DEFAULT_DAYS


def test_said_days_and_both_specialty_fields_are_never_overridden():
    r = rules.route("add a new doctor Dr Kiran, specialization is dermatology, department is skin, ₹600, Mon-Sat "
                    "mornings 9 to 1", now=NOW)
    assert (r.params["specialization"], r.params["department"], r.params["days"]) == \
        ("Dermatology", "Skin", "Mon,Tue,Wed,Thu,Fri,Sat") and "assumed" not in r.params


def test_a_profile_without_any_specialty_still_asks_for_it():
    r = rules.route("add a new doctor Dr Kiran, ₹600, mornings 9 to 1", now=NOW)
    assert r.kind == "clarify" and set(r.missing) == {"specialization", "department"}


# ── profile.update ───────────────────────────────────────────────────────────

def test_profile_address_and_numbers(db, ko):  # noqa: F811
    out, _ = _done("change the hospital address to 12-3, Road No 5, Banjara Hills, Hyderabad")
    assert "Ameerpet, Hyderabad" in str(out["cards"]) and "Banjara Hills" in str(out["cards"])  # old -> new
    assert S["writes"] == [("PUT /admin/profile", {"hospital_address": "12-3, Road No 5, Banjara Hills, Hyderabad"})]
    S["writes"].clear()
    out, _ = _done("update the hospital emergency number to 98480 12345")
    assert "check every digit" in str(out["cards"])
    assert S["writes"] == [("PUT /admin/profile", {"hospital_emergency_number": "9848012345"})]


def test_profile_followups(db, ko):  # noqa: F811
    _done("turn off patient follow ups")
    assert S["writes"] == [("PUT /admin/profile", {"followup_enabled": False})]


def test_profile_same_value_is_nothing_to_do(db, ko):  # noqa: F811
    out = _ask("rename the hospital to Aura Hospital")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_profile_is_admin_only(db, ko):  # noqa: F811
    ko["user"] = _staff("DOCTORS_UPDATE", "HOLIDAYS_DELETE")
    out = _ask("change the hospital address to Banjara Hills, Hyderabad")
    assert out["state"] == "REJECTED" and S["writes"] == []


def test_profile_never_touches_whatsapp_routing():
    from app.assistant import resolve
    assert set(resolve._PROFILE) | {"followup_enabled", "followup_days", "health_checkins_enabled"} <= \
        set(playbooks._PROFILE_LABELS)
    assert not {"phone_number_id", "is_sandbox"} & set(playbooks._PROFILE_LABELS)


# ── holiday.delete ───────────────────────────────────────────────────────────

def test_holiday_removed(db, ko):  # noqa: F811
    out, _ = _done("remove the holiday on 25 December")
    assert "Christmas" in out["message"]
    assert S["writes"] == [("DELETE /admin/holidays", "2026-12-25")] and S["holidays"] == []


def test_not_a_holiday_is_nothing_to_do(db, ko):  # noqa: F811
    assert _ask("remove the holiday on 26 December")["error_code"] == "nothing_to_do" and S["writes"] == []


# ── doctor_leave.move ────────────────────────────────────────────────────────

def test_leave_moved_add_first_then_remove(db, ko):  # noqa: F811
    _done("move Dr Anitha's leave from 5 November to 7 November")
    assert [w[0] for w in S["writes"]] == ["POST /admin/leaves", "DELETE /admin/leaves"]  # never a gap
    assert S["writes"][0][1]["leave_date"] == "2026-11-07" and S["writes"][1][1] == "lv1"
    assert [r["leave_date"] for r in S["leaves"]] == ["2026-11-07"]


def test_moving_needs_the_remove_permission_up_front(db, ko):  # noqa: F811
    ko["user"] = _staff("DOCTOR_LEAVES_CREATE")
    S["me"] = {"branch_id": None, "role": "staff", "permissions": ["DOCTOR_LEAVES_CREATE"]}
    out = _ask("move Dr Anitha's leave from 5 November to 7 November")
    assert out["error_code"] == "nothing_to_do" and "cannot remove leaves" in out["message"] and S["writes"] == []


def test_moving_a_leave_that_is_not_there(db, ko):  # noqa: F811
    assert _ask("move Dr Anitha's leave from 6 November to 7 November")["error_code"] == "nothing_to_do"


# ── treatment.create ─────────────────────────────────────────────────────────

def test_treatment_added(db, ko):  # noqa: F811
    _done("add a new treatment Hair PRP under Hair & Scalp for 4000")
    assert S["writes"] == [("POST /admin/treatments", {"name": "Hair PRP", "category": "Hair & Scalp",
                                                       "price_from_rupees": 4000})]


def test_treatment_category_is_asked_never_invented(db, ko):  # noqa: F811
    out = _ask("add treatment \"Carbon Laser Peel\" for 3500")
    assert out["state"] == "NEEDS_CLARIFICATION" and out["cards"][0]["missing"] == ["category"]
    assert S["writes"] == []


def test_existing_treatment_is_refused(db, ko):  # noqa: F811
    assert _ask("add treatment Botox under Anti-ageing for 9500")["error_code"] == "nothing_to_do"


# ── branch.doctor_unassign ───────────────────────────────────────────────────

def test_doctor_taken_off_a_branch(db, ko):  # noqa: F811
    _done("remove Dr Anitha from the Madhapur branch")
    assert S["writes"] == [("DELETE branch doctor", B2, D2)] and S["assigned"][B2] == []


def test_not_assigned_is_nothing_to_do(db, ko):  # noqa: F811
    assert _ask("remove Dr Suresh Kumar from the Madhapur branch")["error_code"] == "nothing_to_do"


# ── voice.call_number ────────────────────────────────────────────────────────

def test_receptionist_calls_one_number(db, ko):  # noqa: F811
    _done("call 9876543210 about knee pain")
    assert S["writes"] == [("POST /admin/voice/outbound", {"phone": "9876543210", "purpose": "lead_followup",
                                                           "interest": "knee pain"})]


def test_a_number_already_queued_is_not_called_twice(db, ko):  # noqa: F811
    S["jobs"] = [{"id": "j0", "patient_phone": "+919876543210", "status": "queued"}]
    assert _ask("call 9876543210 about knee pain")["error_code"] == "nothing_to_do" and S["writes"] == []


@pytest.mark.parametrize("text, action", [
    ("call next patient for Dr Anitha", "opd.call_next"), ("recall token 12", "opd.recall"),
    ("call the new leads", "leads.call"), ("show the call log", "voice.calls"),
    ("hospital holiday on 1 November", "holiday.create"), ("mark Dr Anitha on leave tomorrow", "doctor_leave.create"),
    ("assign Dr Anitha to Kukatpally branch", "branch.doctor_assign"), ("move token 7 to billing", "opd.stage"),
    ("set the receptionist emergency number to 98480 12345", "voice.settings_update"),
])
def test_neighbours_keep_their_sentences(text, action):
    docs = [{"id": D2, "name": "Dr. Anitha Rao"}]
    assert rules.route(text, doctors=docs, now=NOW).action_id == action


def test_every_new_action_is_an_existing_endpoint_with_a_preview():
    for aid in ("profile.update", "holiday.delete", "doctor_leave.move", "treatment.create", "branch.doctor_unassign",
                "voice.call_number"):
        spec = REGISTRY[aid]
        assert spec.risk == "R2" and spec.preview and spec.endpoint, aid


def test_no_duplicate_keys_in_the_registry_tables():
    """A repeated key in a dict literal silently drops the earlier entry (and its phrasings)."""
    src = (Path(__file__).resolve().parents[1] / "app" / "assistant" / "registry.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            assert len(keys) == len(set(keys)), {k for k in keys if keys.count(k) > 1}


# ── a spoken answer to the one open text field ───────────────────────────────

def test_spoken_category_completes_the_treatment(db, ko):  # noqa: F811
    first = _ask("add treatment \"Carbon Laser Peel\" for 3500")
    assert first["cards"][0]["missing"] == ["category"]
    out = _ask("skin and laser")
    assert (out["state"], out["action_id"], out["route_layer"]) == ("NEEDS_CONFIRMATION", "treatment.create", "form"), out
    assert _confirm(out)["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("POST /admin/treatments", {"name": "Carbon Laser Peel", "category": "Skin and laser",
                                                       "price_from_rupees": 3500})]


@pytest.mark.parametrize("reply", ["thanks", "what categories are there?", "cancel it", "don't add it",
                                   "this is a very long reply that is surely not a category name"])
def test_chatter_is_never_taken_as_the_answer(db, ko, reply):  # noqa: F811
    _ask("add treatment \"Carbon Laser Peel\" for 3500")
    out = _ask(reply)
    assert out.get("route_layer") != "form" and S["writes"] == []


def test_two_open_fields_are_never_guessed_from_one_reply():
    spec = REGISTRY["doctor.details"]
    assert rules.free_reply(spec, ["specialization", "department"], "dermatology") == {}
    assert rules.free_reply(REGISTRY["treatment.create"], ["category"], "Hair & Scalp") == {"category": "Hair & Scalp"}
