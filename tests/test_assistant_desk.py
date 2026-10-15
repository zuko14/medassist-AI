"""KK-P4-05: front-desk writes (action-registry-spec §4.3, §4.4, §4.11).

appointment.check_in (R1), appointment.cancel (R2), booking.confirm / booking.reject (R2),
opd.call_next (R1), notifications.mark_all_read (R1). holiday.create is held for D-27
(hospital_holidays keyed on the date alone, so clinics collide).

- A booking is named by its ref, a phone fragment or the patient's name; it is looked up with
  the page's own list endpoint, and anything but exactly one match is a plain refusal listing
  the matches (never a guess, never a write).
- Every write goes through its existing /admin endpoint and is read back. Statuses checked against
  the real service: cancel ends `cancelled`, or `refunded` when the refund went through
  (payment.initiate_refund); reject ends `cancelled`; confirm ends `confirmed`; check-in sets
  token_number (database.check_in_appointment).
"""

import json
import uuid
from datetime import datetime, timedelta
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
TODAY, TOMORROW = NOW.date().isoformat(), (NOW.date() + timedelta(days=1)).isoformat()
REF1, REF2 = "MC-2026-ABCD2345", "MC-2026-WXYZ6789"
DOCTORS = [{"id": "d1", "name": "Dr. Srinivas Rao"}, {"id": "d2", "name": "Dr. Anitha"}]


def _appt(i, ref, name, day=TODAY, status="confirmed", token=None, phone="919876543210"):
    return {"id": f"a{i}", "booking_ref": ref, "patient_name": name, "patient_phone": phone,
            "doctor_name": "Dr. Srinivas Rao", "appointment_date": day, "appointment_time": "10:00",
            "status": status, "token_number": token, "amount_paise": 50000}


# ── resolver ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("check in MC-2026-ABCD2345", REF1),
    ("cancel booking mc-2026-abcd2345 please", REF1),
    ("check in 98765 43210", "98765"),
    ("check in Ravi Kumar", "ravi kumar"),
    ("cancel Ravi's appointment", "ravi"),
    ("Ravi ka check in karo", "ravi"),
    ("Sita vachharu check in cheyyi", "sita"),
    ("check in the patient", None),
    ("cancel today's appointment", None),
])
def test_resolve_booking(text, expected):
    assert resolve.resolve_booking(text) == expected


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, action, params", [
    ("check in MC-2026-ABCD2345", "appointment.check_in", {"who": REF1}),
    ("Ravi has arrived, check in", "appointment.check_in", {"who": "ravi"}),
    ("cancel Ravi's appointment", "appointment.cancel", {"who": "ravi"}),
    ("booking raddu cheyyi MC-2026-ABCD2345", "appointment.cancel", {"who": REF1}),
    ("confirm booking MC-2026-ABCD2345", "booking.confirm", {"who": REF1}),
    ("reject booking for Ravi", "booking.reject", {"who": "ravi"}),
    ("call next patient for Dr Rao", "opd.call_next", {"doctor": "Dr. Srinivas Rao"}),
    ("mark all notifications as read", "notifications.mark_all_read", {}),
])
def test_desk_actions_route(text, action, params):
    r = rules.route(text, doctors=DOCTORS, now=NOW)
    assert (r.kind, r.action_id, r.params) == ("action", action, params), r


@pytest.mark.parametrize("text, kind", [
    ("don't cancel Ravi's appointment", "rejected"), ("what if we cancel Ravi's booking", "rejected"),
    ("check in the patient", "clarify"), ("next token", "action"),   # the queue read, never call-next
])
def test_desk_safety(text, kind):
    r = rules.route(text, doctors=DOCTORS, now=NOW)
    assert r.kind == kind
    if text == "next token":
        assert r.action_id == "opd.queue"


# ── through the API, against a fake panel with state ─────────────────────────

fake = FastAPI()
S: dict = {}


def _reset(appts=(), queue=(), notes=(), refund=False, write_ok=True):
    S.clear()
    S.update(appts=[dict(a) for a in appts], queue=[dict(q) for q in queue], notes=[dict(n) for n in notes],
             refund=refund, write_ok=write_ok, writes=[])


def _match(a, q):
    q = (q or "").lower()
    return not q or q in (a["booking_ref"] or "").lower() or q in a["patient_name"].lower() or q in a["patient_phone"]


@fake.get("/admin/appointments")
async def _appointments(request: Request):
    p = request.query_params
    rows = [a for a in S["appts"] if _match(a, p.get("q"))
            and (not p.get("date_from") or p["date_from"] <= a["appointment_date"] <= p["date_to"])
            and (not p.get("status") or a["status"] == p["status"])]
    return {"appointments": rows, "total": len(rows)}


@fake.get("/admin/bookings/pending-review")
async def _pending():
    return {"bookings": [a for a in S["appts"] if a["status"] == "pending_review"]}


def _find(aid):
    a = next((a for a in S["appts"] if a["id"] == aid), None)
    if a is None:
        raise HTTPException(status_code=404)
    return a


@fake.post("/admin/appointments/{aid}/check-in")
async def _check_in(aid: str):
    S["writes"].append(("check-in", aid))
    a = _find(aid)
    if S["write_ok"]:
        a["token_number"] = 7
    return a


@fake.delete("/admin/appointments/{aid}")
async def _cancel(aid: str):
    S["writes"].append(("cancel", aid))
    a = _find(aid)
    if a["status"] != "confirmed":
        return {"success": False, "message": "can_only_cancel_confirmed"}
    if S["write_ok"]:
        a["status"] = "refunded" if S["refund"] else "cancelled"
    return {"success": True}


@fake.post("/admin/bookings/{aid}/confirm")
async def _confirm_b(aid: str):
    S["writes"].append(("confirm", aid))
    _find(aid)["status"] = "confirmed"
    return {"success": True}


@fake.post("/admin/bookings/{aid}/reject")
async def _reject_b(aid: str):
    S["writes"].append(("reject", aid))
    _find(aid)["status"] = "cancelled"
    return {"success": True}


@fake.get("/admin/doctors")
async def _doctors():
    return DOCTORS


@fake.get("/admin/opd/queue")
async def _queue(doctor_id: str = None, date: str = None):
    rows = [q for q in S["queue"] if not doctor_id or q["doctor_id"] == doctor_id]
    return {"doctors": [{"doctor_id": doctor_id, "rows": rows}], "etag": "x"}


@fake.post("/admin/opd/queue/call-next")
async def _call_next(request: Request):
    body = await request.json()
    S["writes"].append(("call-next", body["doctor_id"]))
    rows = [q for q in S["queue"] if q["doctor_id"] == body["doctor_id"]]
    out = {"called": None, "finished": None}
    for q in rows:
        if q["stage"] == "in_consultation":
            q["stage"] = "billing"
            out["finished"] = q
    waiting = sorted((q for q in rows if q["stage"] == "waiting"), key=lambda q: q["token_number"])
    if waiting:
        waiting[0]["stage"] = "in_consultation"
        out["called"] = waiting[0]
    return out


@fake.get("/admin/notifications")
async def _notes(unread_only: bool = False, limit: int = 50):
    return {"success": True, "notifications": [n for n in S["notes"] if not unread_only or not n["is_read"]][:limit]}


@fake.post("/admin/notifications/mark-all-read")
async def _mark_all():
    S["writes"].append(("mark-all-read", None))
    for n in S["notes"]:
        n["is_read"] = True
    return {"success": True, "updated_count": len(S["notes"])}


client = TestClient(app)


@pytest.fixture
def desk(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    _reset()
    # OPD off by default: on a live-OPD clinic a check-in is the OPD arrive (KK-P4-07, tested there).
    # The call-next tests switch it on.
    opd = {"on": False}
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW), \
         patch("app.assistant.registry.opd_enabled", lambda c: opd["on"]), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value={
             "id": CID, "plan": "polyclinic", "opd_state": "READY", "features": {"kriya_ketu": True}})):
        as_user["opd"] = opd
        yield as_user


def _ask(text, page="appointments"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": page}}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"], **extra})


def _card(out):
    return out["cards"][0]


# check-in

def test_check_in_by_ref_previews_then_writes_once_and_reads_the_token_back(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar"), _appt(2, REF2, "Sita Devi")])
    out = _ask("check in MC-2026-ABCD2345")
    assert out["state"] == "NEEDS_CONFIRMATION" and S["writes"] == [], out
    assert "Ravi Kumar" in json.dumps(_card(out)) and "919876543210" not in json.dumps(db.tables["assistant_runs"])
    done = _confirm(out).json()
    assert done["state"] == "COMPLETED_VERIFIED", done
    assert S["writes"] == [("check-in", "a1")] and "Token 7" in json.dumps(done)


def test_check_in_by_name_with_two_matches_lists_them_and_writes_nothing(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar"), _appt(2, REF2, "Ravi Teja")])
    out = _ask("check in Ravi")
    assert (out["state"], out["error_code"]) == ("REJECTED", "nothing_to_do")
    assert REF1 in out["message"] and REF2 in out["message"] and S["writes"] == []


def test_check_in_only_looks_at_today(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar", day=TOMORROW)])
    out = _ask("check in Ravi")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_already_checked_in_is_refused_with_its_token(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar", token=4)])
    out = _ask("check in Ravi")
    assert out["error_code"] == "nothing_to_do" and "4" in out["message"] and S["writes"] == []


def test_a_check_in_the_read_back_cannot_see_is_unknown(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar")], write_ok=False)
    out = _ask("check in Ravi")
    assert _confirm(out).json()["state"] == "UNKNOWN_OUTCOME" and len(S["writes"]) == 1


# cancel

@pytest.mark.parametrize("refund, final", [(False, "cancelled"), (True, "refunded")])
def test_cancel_verifies_cancelled_or_refunded(db, desk, refund, final):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar", day=TOMORROW)], refund=refund)
    out = _ask("cancel Ravi's appointment")
    card = _card(out)
    assert out["state"] == "NEEDS_CONFIRMATION" and any("refund" in w for w in card["warnings"])
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["appts"][0]["status"] == final and S["writes"] == [("cancel", "a1")]


def test_cancel_ignores_bookings_that_are_not_confirmed(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar", status="cancelled")])
    assert _ask("cancel Ravi's appointment")["error_code"] == "nothing_to_do" and S["writes"] == []


def test_a_booking_changed_since_the_preview_is_never_cancelled(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar")])
    out = _ask("cancel Ravi's appointment")
    S["appts"][0]["status"] = "pending_review"     # moved under us: the confirm re-reads and finds nothing
    again = _confirm(out).json()
    assert again["state"] == "REJECTED" and S["writes"] == [] and S["appts"][0]["status"] == "pending_review"


# booking confirm / reject

def test_confirm_a_pending_booking(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar", status="pending_review")])
    out = _ask("confirm booking MC-2026-ABCD2345", page="payments")
    assert any("payment" in w.lower() for w in _card(out)["warnings"])
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("confirm", "a1")]


def test_reject_a_pending_booking(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar", status="pending_review")])
    out = _ask("reject booking for Ravi", page="payments")
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED" and S["appts"][0]["status"] == "cancelled"


def test_confirm_with_no_pending_booking_is_refused(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar")])
    assert _ask("confirm booking MC-2026-ABCD2345")["error_code"] == "nothing_to_do" and S["writes"] == []


# call next

QUEUE = [{"appointment_id": "q1", "doctor_id": "d1", "token_number": 3, "patient_name": "Ravi", "stage": "in_consultation"},
         {"appointment_id": "q2", "doctor_id": "d1", "token_number": 5, "patient_name": "Sita", "stage": "waiting"},
         {"appointment_id": "q3", "doctor_id": "d1", "token_number": 4, "patient_name": "Anu", "stage": "waiting"}]


def test_call_next_names_who_finishes_and_who_is_called(db, desk):  # noqa: F811
    desk["opd"]["on"] = True
    _reset(queue=QUEUE)
    out = _ask("call next patient for Dr Rao", page="opdqueue")
    text = json.dumps(_card(out))
    assert "Token 3" in text and "Token 4" in text and S["writes"] == []
    done = _confirm(out).json()
    assert done["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("call-next", "d1")]


def test_call_next_with_an_empty_queue_is_refused(db, desk):  # noqa: F811
    desk["opd"]["on"] = True
    assert _ask("call next patient for Dr Rao")["error_code"] == "nothing_to_do" and S["writes"] == []


def test_call_next_after_the_queue_moved_needs_a_new_preview(db, desk):  # noqa: F811
    desk["opd"]["on"] = True
    _reset(queue=QUEUE)
    out = _ask("call next patient for Dr Rao")
    S["queue"][2]["stage"] = "in_consultation"     # the desk already called token 4
    again = _confirm(out).json()
    assert (again["state"], again["error_code"]) == ("NEEDS_CONFIRMATION", "plan_changed") and S["writes"] == []


def test_call_next_needs_live_opd(db, desk):  # noqa: F811
    with patch("app.assistant.registry.opd_enabled", lambda c: False):
        out = _ask("call next patient for Dr Rao")
    assert out["state"] == "REJECTED" and S["writes"] == []


# notifications

def test_mark_all_read(db, desk):  # noqa: F811
    _reset(notes=[{"id": "n1", "is_read": False, "title": "x"}, {"id": "n2", "is_read": True, "title": "y"}])
    out = _ask("mark all notifications as read")
    assert "1 unread" in json.dumps(_card(out))
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("mark-all-read", None)]


def test_nothing_unread_is_refused(db, desk):  # noqa: F811
    assert _ask("mark all notifications as read")["error_code"] == "nothing_to_do" and S["writes"] == []


# clarify: a missing booking is asked for, and the typed answer goes through the same resolver

def test_missing_booking_is_asked_for_then_answered(db, desk):  # noqa: F811
    _reset(appts=[_appt(1, REF1, "Ravi Kumar")])
    out = _ask("check in the patient")
    assert out["state"] == "NEEDS_CLARIFICATION" and out["cards"][0]["fields"] == [{"name": "who", "kind": "booking"}]
    reply = client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "check in the patient", "reply_to_run_id": out["run_id"],
        "slot_answers": {"who": "MC-2026-ABCD2345"}}).json()
    assert reply["state"] == "NEEDS_CONFIRMATION", reply
    bad = client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "x", "reply_to_run_id": _ask("check in the patient")["run_id"],
        "slot_answers": {"who": "?"}})
    assert bad.status_code == 422


def test_staff_can_use_desk_actions_without_a_permission(db, desk):  # noqa: F811
    desk["user"] = _user()
    _reset(appts=[_appt(1, REF1, "Ravi Kumar")])
    assert _ask("check in Ravi")["state"] == "NEEDS_CONFIRMATION"


# ── registry + corpus ────────────────────────────────────────────────────────

DESK = ("appointment.check_in", "appointment.cancel", "booking.confirm", "booking.reject", "opd.call_next",
        "notifications.mark_all_read")


def test_registry_shape():
    want = {
        "appointment.check_in": ("R1", "none", ("POST", "/admin/appointments/{appointment_id}/check-in")),
        "appointment.cancel": ("R2", "none", ("DELETE", "/admin/appointments/{appointment_id}")),
        "booking.confirm": ("R2", "none", ("POST", "/admin/bookings/{booking_id}/confirm")),
        "booking.reject": ("R2", "none", ("POST", "/admin/bookings/{booking_id}/reject")),
        "opd.call_next": ("R1", "handler", ("POST", "/admin/opd/queue/call-next")),
        "notifications.mark_all_read": ("R1", "none", ("POST", "/admin/notifications/mark-all-read")),
    }
    for aid, (risk, src, endpoint) in want.items():
        spec = REGISTRY[aid]
        assert (spec.risk, spec.perm_source, spec.endpoint) == (risk, src, endpoint), aid
    assert REGISTRY["opd.call_next"].permission == "OPD_FRONT_DESK" and "OPD_CLINICAL" in REGISTRY["opd.call_next"].also
    assert REGISTRY["holiday.create"].risk == "R3"     # released after D-27 (tests/test_assistant_holiday.py)


@pytest.mark.parametrize("aid", DESK)
def test_corpus_covers_each_desk_action(aid):
    corpus = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                        .read_text(encoding="utf-8"))["phrases"]
    pos = [e for e in corpus if e["expect"] == f"action:{aid}"]
    assert len(pos) >= 5 and {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}
    assert len([e for e in corpus if e.get("near") == aid and e["expect"] != f"action:{aid}"]) >= 2
