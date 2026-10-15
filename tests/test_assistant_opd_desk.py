"""KK-P4-07: PB-06 register (+ consent card) and walk-in token; OPD arrive / recall / stage (KK-FR-060, 061).

REG-01  Duplicate candidates -> listed, nothing written; an existing patient gets a token via opd.walk_in.
REG-02  acknowledged_duplicates is sent only when the user said "new person".
REG-03  data_consent / whatsapp_opt_in come ONLY from the yes/no form; no sentence ever sets them.
        No data consent -> registration is refused before any write (register_patient requires it).
REG-04  The walk-in token is read back from the queue.
KK-FR-061  On a live-OPD clinic "Ravi has arrived" uses POST /admin/opd/appointments/{id}/arrive;
        elsewhere the legacy check-in (the two share triggers; availability breaks the tie).
Stage moves carry expected_from (the queue's CAS); a moved queue -> a new preview; "cancelled" is never
a Kriya stage move.
"""

import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, resolve, rules
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.voice.dates import IST
from tests.test_assistant_runs import CID, _user, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=IST)
TODAY = NOW.date().isoformat()
DOCTORS = [{"id": "d1", "name": "Dr. Srinivas Rao"}, {"id": "d2", "name": "Dr. Anitha"}]
OPD_LIVE = {"id": CID, "plan": "polyclinic", "account_type": "tenant", "opd_state": "READY",
            "features": {"kriya_ketu": True, "opd_enabled": True}}
NO_OPD = {"id": CID, "plan": "polyclinic", "account_type": "tenant", "features": {"kriya_ketu": True}}


# ── resolvers ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, name, phone", [
    ("register Ravi Kumar 9876543210", "Ravi Kumar", "9876543210"),   # the name keeps its casing
    ("register new patient Sita Devi, phone 98765 43210 for Dr Rao", "Sita Devi", "9876543210"),
    ("Ravi Kumar 9876543210 ni register cheyyi", "Ravi Kumar", "9876543210"),
])
def test_resolve_name_and_phone(text, name, phone):
    assert resolve.resolve_person_name(text) == name and resolve.resolve_phone(text) == phone


@pytest.mark.parametrize("text, expected", [
    ("recall token 5", "#5"), ("move token number 12 to billing", "#12"), ("send Ravi to billing", "ravi"),
])
def test_resolve_queue_target(text, expected):
    assert resolve.resolve_queue_target(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("send token 5 to billing", "billing"), ("mark token 5 done", "completed"), ("token 5 back to waiting", "waiting"),
    ("token 5 vitals", "vitals_pending"), ("cancel token 5", None),
])
def test_resolve_stage(text, expected):
    assert resolve.resolve_stage(text) == expected


def test_consent_is_never_read_from_text():
    r = rules.route("register Ravi Kumar 9876543210, he agrees to data storage and WhatsApp, consent yes",
                    doctors=DOCTORS, now=NOW)
    assert r.kind == "clarify" and set(r.missing) == {"data_consent", "whatsapp_opt_in"}
    assert "data_consent" not in r.params and "whatsapp_opt_in" not in r.params


# ── API against a fake panel ─────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}


def _reset(opd=True, dupes=(), patients=(), appts=(), queue=(), notified=True):
    S.clear()
    S.update(opd=opd, dupes=[dict(d) for d in dupes], patients=[dict(p) for p in patients],
             appts=[dict(a) for a in appts], queue=[dict(q) for q in queue], notified=notified, writes=[])


@fake.get("/admin/me")
async def _me():
    return {"branch_id": None, "opd_enabled": S["opd"], "opd_state": "READY" if S["opd"] else None}


@fake.get("/admin/doctors")
async def _doctors():
    return DOCTORS


@fake.post("/admin/opd/patients/duplicate-check")
async def _dupes(request: Request):
    body = await request.json()
    return [d for d in S["dupes"] if d.get("phone", "").endswith(body["phone"][-10:])]


@fake.post("/admin/opd/patients", status_code=201)
async def _register(request: Request):
    body = await request.json()
    S["writes"].append(("register", body))
    pid = f"p{len(S['patients']) + 1}"
    S["patients"].append({"patient_id": pid, "family_member_id": None, "name": body["name"], "mrn": f"MRN{pid}"})
    return {"patient_id": pid, "name": body["name"], "mrn": f"MRN{pid}"}


@fake.get("/admin/opd/patients/search")
async def _search(q: str):
    return [p for p in S["patients"] if all(w in p["name"].lower() for w in q.lower().split())]


@fake.get("/admin/opd/patients/{pid}")
async def _patient(pid: str):
    p = next((p for p in S["patients"] if p["patient_id"] == pid), None)
    if not p:
        raise HTTPException(status_code=404)
    return {"id": pid, "name": p["name"], "mrn": p["mrn"]}


@fake.post("/admin/opd/walk-ins", status_code=201)
async def _walk_in(request: Request):
    body = await request.json()
    S["writes"].append(("walk-in", body))
    row = {"appointment_id": f"w{len(S['queue']) + 1}", "token_number": 9, "doctor_id": body["doctor_id"],
           "patient_name": next(p["name"] for p in S["patients"] if p["patient_id"] == body["patient_id"]),
           "stage": "waiting"}
    S["queue"].append(row)
    return row


@fake.get("/admin/opd/queue")
async def _queue(doctor_id: str = None, date: str = None):
    rows = [q for q in S["queue"] if not doctor_id or q["doctor_id"] == doctor_id]
    return {"doctors": [{"doctor_id": doctor_id, "rows": rows}], "etag": "x"}


@fake.get("/admin/appointments")
async def _appts(request: Request):
    p = request.query_params
    q = (p.get("q") or "").lower()
    rows = [a for a in S["appts"] if q in a["patient_name"].lower() and p["date_from"] <= a["appointment_date"] <= p["date_to"]
            and (not p.get("status") or a["status"] == p["status"])]
    return {"appointments": rows, "total": len(rows)}


@fake.post("/admin/opd/appointments/{aid}/arrive")
async def _arrive(aid: str):
    S["writes"].append(("arrive", aid))
    a = next(a for a in S["appts"] if a["id"] == aid)
    a["token_number"] = 4
    S["queue"].append({"appointment_id": aid, "token_number": 4, "doctor_id": "d1", "patient_name": a["patient_name"],
                       "stage": "vitals_pending"})
    return {"appointment_id": aid, "token_number": 4}


@fake.post("/admin/appointments/{aid}/check-in")
async def _legacy_check_in(aid: str):
    S["writes"].append(("check-in", aid))
    a = next(a for a in S["appts"] if a["id"] == aid)
    a["token_number"] = 3
    return a


@fake.post("/admin/opd/queue/{aid}/recall")
async def _recall(aid: str):
    S["writes"].append(("recall", aid))
    return {"notified": S["notified"]}


@fake.post("/admin/opd/queue/{aid}/stage")
async def _stage(aid: str, request: Request):
    body = await request.json()
    S["writes"].append(("stage", aid, body))
    row = next(q for q in S["queue"] if q["appointment_id"] == aid)
    if row["stage"] != body["expected_from"]:
        raise HTTPException(status_code=409)
    row["stage"] = body["to_stage"]
    return row


client = TestClient(app)


@pytest.fixture
def desk(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    _reset()
    holder = {"clinic": OPD_LIVE}
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(side_effect=lambda _id: holder["clinic"])):
        as_user["clinic"] = holder
        yield as_user


def _ask(text, page="opddesk"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": page}}).json()


def _answer(out, **answers):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "x", "reply_to_run_id": out["run_id"],
        "slot_answers": answers})


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"], **extra})


def _card(out):
    return out["cards"][0]


# PB-06 register

def test_register_asks_both_consent_questions_then_sends_only_the_answers(db, desk):  # noqa: F811
    out = _ask("register Ravi Kumar 9876543210 and send him WhatsApp updates")
    card = _card(out)
    assert out["state"] == "NEEDS_CLARIFICATION", out
    assert {f["name"]: f["kind"] for f in card["fields"]} == {"data_consent": "yesno", "whatsapp_opt_in": "yesno"}
    assert all(f.get("label") for f in card["fields"])
    prev = _answer(out, data_consent="yes", whatsapp_opt_in="no").json()
    assert prev["state"] == "NEEDS_CONFIRMATION" and S["writes"] == [], prev
    # input_text keeps what staff typed (KK-P1-01, 90-day purge); cards show only the last 4 digits.
    assert "9876543210" not in json.dumps(prev["cards"]) and "3210" in json.dumps(prev["cards"])
    done = _confirm(prev).json()
    assert done["state"] == "COMPLETED_VERIFIED", done
    (kind, body), = S["writes"]
    assert (body["data_consent"], body["whatsapp_opt_in"]) == (True, False)                     # REG-03
    assert body["name"] == "Ravi Kumar" and "acknowledged_duplicates" not in body               # REG-02
    assert "token" in json.dumps(done).lower()                                                   # next step named


def test_no_data_consent_means_no_registration(db, desk):  # noqa: F811
    out = _ask("register Ravi Kumar 9876543210")
    res = _answer(out, data_consent="no", whatsapp_opt_in="no").json()
    assert res["state"] == "REJECTED" and "consent" in res["message"].lower() and S["writes"] == []


def test_a_consent_answer_must_be_yes_or_no(db, desk):  # noqa: F811
    out = _ask("register Ravi Kumar 9876543210")
    assert _answer(out, data_consent="sure", whatsapp_opt_in="no").status_code == 422


def test_duplicates_are_listed_and_nothing_is_written(db, desk):  # noqa: F811
    _reset(dupes=[{"patient_id": "p9", "family_member_id": None, "mrn": "MRN9", "name": "Ravi K",
                   "phone": "919876543210", "match_reason": "same_phone"}])
    out = _answer(_ask("register Ravi Kumar 9876543210"), data_consent="yes", whatsapp_opt_in="yes").json()
    assert out["error_code"] == "nothing_to_do" and "MRN9" in out["message"] and "new person" in out["message"]
    assert S["writes"] == []                                                                     # REG-01


def test_new_person_acknowledges_exactly_the_candidates(db, desk):  # noqa: F811
    _reset(dupes=[{"patient_id": "p9", "family_member_id": None, "mrn": "MRN9", "name": "Ravi K",
                   "phone": "919876543210", "match_reason": "same_phone"}])
    out = _answer(_ask("register Ravi Kumar 9876543210 as a new person"), data_consent="yes", whatsapp_opt_in="yes").json()
    assert out["state"] == "NEEDS_CONFIRMATION", out
    _confirm(out)
    assert S["writes"][0][1]["acknowledged_duplicates"] == ["p9"]                                # REG-02


def test_staff_without_front_desk_are_refused(db, desk):  # noqa: F811
    desk["user"] = _user()
    out = _ask("register Ravi Kumar 9876543210")
    assert out["state"] == "REJECTED" and "Opd Front Desk" in out["message"]


# walk-in token (existing patient)

def test_walk_in_token_for_an_existing_patient_is_read_back(db, desk):  # noqa: F811
    _reset(patients=[{"patient_id": "p1", "family_member_id": None, "name": "Ravi Kumar", "mrn": "MRN1"}])
    out = _ask("token for Ravi Kumar with Dr Rao")
    assert out["state"] == "NEEDS_CONFIRMATION" and "Dr. Srinivas Rao" in json.dumps(_card(out)), out
    done = _confirm(out).json()
    assert done["state"] == "COMPLETED_VERIFIED" and "Token 9" in json.dumps(done)                # REG-04
    assert S["writes"] == [("walk-in", {"patient_id": "p1", "doctor_id": "d1", "visit_type": "new"})]


def test_walk_in_with_two_matching_patients_lists_them(db, desk):  # noqa: F811
    _reset(patients=[{"patient_id": "p1", "family_member_id": None, "name": "Ravi Kumar", "mrn": "MRN1"},
                     {"patient_id": "p2", "family_member_id": None, "name": "Ravi Teja", "mrn": "MRN2"}])
    out = _ask("token for Ravi with Dr Rao")
    assert out["error_code"] == "nothing_to_do" and "MRN1" in out["message"] and "MRN2" in out["message"]


def test_a_patient_already_in_todays_queue_is_not_given_a_second_token(db, desk):  # noqa: F811
    _reset(patients=[{"patient_id": "p1", "family_member_id": None, "name": "Ravi Kumar", "mrn": "MRN1"}],
           queue=[{"appointment_id": "a1", "token_number": 2, "doctor_id": "d1", "patient_name": "Ravi Kumar",
                   "stage": "waiting"}])
    out = _ask("token for Ravi Kumar with Dr Rao")
    assert out["error_code"] == "nothing_to_do" and "2" in out["message"] and S["writes"] == []


# arrive vs legacy check-in (KK-FR-061)

APPT = {"id": "a1", "booking_ref": "MC-2026-ABCD2345", "patient_name": "Ravi Kumar", "patient_phone": "919876543210",
        "doctor_name": "Dr. Srinivas Rao", "appointment_date": TODAY, "appointment_time": "10:00",
        "status": "confirmed", "token_number": None}


def test_on_live_opd_arrival_uses_the_opd_arrive_endpoint(db, desk):  # noqa: F811
    _reset(appts=[APPT])
    out = _ask("Ravi has arrived, check in")
    assert out["action_id"] == "opd.arrive", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("arrive", "a1")]


def test_without_opd_the_legacy_check_in_is_used(db, desk):  # noqa: F811
    desk["clinic"]["clinic"] = NO_OPD
    _reset(opd=False, appts=[APPT])
    out = _ask("Ravi has arrived, check in")
    assert out["action_id"] == "appointment.check_in", out
    _confirm(out)
    assert S["writes"] == [("check-in", "a1")]


# recall + stage

QUEUE = [{"appointment_id": "q1", "token_number": 5, "doctor_id": "d1", "patient_name": "Ravi Kumar",
          "stage": "in_consultation"}]


def test_recall_by_token(db, desk):  # noqa: F811
    _reset(queue=QUEUE)
    out = _ask("recall token 5", page="opdqueue")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("recall", "q1")]


def test_a_recall_whatsapp_did_not_send_is_not_shown_as_done(db, desk):  # noqa: F811
    _reset(queue=QUEUE, notified=False)
    out = _ask("recall token 5", page="opdqueue")
    assert _confirm(out).json()["state"] != "COMPLETED_VERIFIED"


def test_stage_move_carries_the_current_stage(db, desk):  # noqa: F811
    _reset(queue=QUEUE)
    out = _ask("send token 5 to billing", page="opdqueue")
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("stage", "q1", {"to_stage": "billing", "expected_from": "in_consultation"})]


def test_stage_after_the_queue_moved_needs_a_new_preview(db, desk):  # noqa: F811
    _reset(queue=QUEUE)
    out = _ask("send token 5 to billing", page="opdqueue")
    S["queue"][0]["stage"] = "completed"
    again = _confirm(out).json()
    assert again["state"] in ("NEEDS_CONFIRMATION", "REJECTED") and S["writes"] == []


def test_cancelling_a_queue_entry_is_not_a_kriya_stage_move(db, desk):  # noqa: F811
    _reset(queue=QUEUE)
    out = _ask("cancel token 5", page="opdqueue")
    assert out["state"] != "NEEDS_CONFIRMATION" and S["writes"] == []


# ── registry + corpus ────────────────────────────────────────────────────────

ACTIONS = ("patient.register", "opd.walk_in", "opd.arrive", "opd.recall", "opd.stage")
CORPUS_ACTIONS = ("patient.register", "opd.walk_in", "opd.recall", "opd.stage")  # opd.arrive: check-in's words


def test_registry_shape():
    want = {"patient.register": ("R2", ("POST", "/admin/opd/patients")),
            "opd.walk_in": ("R2", ("POST", "/admin/opd/walk-ins")),
            "opd.arrive": ("R1", ("POST", "/admin/opd/appointments/{appointment_id}/arrive")),
            "opd.recall": ("R1", ("POST", "/admin/opd/queue/{appointment_id}/recall")),
            "opd.stage": ("R1", ("POST", "/admin/opd/queue/{appointment_id}/stage"))}
    for aid, (risk, endpoint) in want.items():
        spec = REGISTRY[aid]
        assert (spec.risk, spec.endpoint, spec.perm_source) == (risk, endpoint, "handler"), aid
    from app.assistant.registry import VARIANTS
    assert VARIANTS["appointment.check_in"] == "opd.arrive" and REGISTRY["opd.arrive"].triggers == ()


@pytest.mark.parametrize("aid", CORPUS_ACTIONS)
def test_corpus_covers_each_action(aid):
    corpus = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                        .read_text(encoding="utf-8"))["phrases"]
    # patient.register always asks (consent comes only from the form): its phrases are tagged clarify entries.
    pos = [e for e in corpus if e["expect"] == f"action:{aid}" or e.get("action") == aid]
    assert len(pos) >= 5 and {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}
    for e in pos:
        assert rules.route(e["text"]).action_id == aid, e["text"]
    assert len([e for e in corpus if e.get("near") == aid and e["expect"] != f"action:{aid}"]) >= 2
