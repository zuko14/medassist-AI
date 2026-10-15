"""KK-P4-14: PB-12 morning briefing and PB-13 end of day, on demand (R0, zero tokens).

BRF-01: each section is the user's own read of its page's endpoint; a section the route refuses (permission,
        feature off) is left out, and the rest of the briefing still shows.
BRF-02: no LLM call.
A section whose read fails on the server says so instead of disappearing. No patient name is shown.
The L4a scheduled briefing is not built: status.md §5 #17 (notifications are clinic-wide, owner decision).
"""

import asyncio
import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.assistant import executor, playbooks, rules
from app.assistant.executor import Call, CallerAuth, Step
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.voice.dates import IST
from tests.test_assistant_runs import CID, _user, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=IST)
TODAY, TOMORROW = "2026-10-15", "2026-10-16"


def _appt(doctor, time="10:00", token=None, status="confirmed", day=TODAY):
    return {"id": str(uuid.uuid4()), "doctor_name": doctor, "appointment_time": time, "appointment_date": day,
            "patient_name": "Secret Patient", "token_number": token, "status": status}


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, action", [
    ("good morning", "briefing.morning"), ("today's briefing", "briefing.morning"),
    ("ee roju emundi", "briefing.morning"), ("aaj kya hai", "briefing.morning"),
    ("wrap up the day", "briefing.eod"), ("end of day summary", "briefing.eod"),
    ("today's appointments", "appointments.search"),             # unchanged
])
def test_routes(text, action):
    assert rules.route(text, now=NOW).action_id == action


# ── API against a fake panel ─────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}


def _reset(appts=(), leaves=(), refuse=(), broken=()):
    S.clear()
    S.update(appts=[dict(a) for a in appts], leaves=[dict(x) for x in leaves], refuse=set(refuse),
             broken=set(broken), seen=[])


@fake.get("/admin/{rest:path}")
async def _serve(rest: str, request: Request):
    path, q = "/admin/" + rest, dict(request.query_params)
    S["seen"].append(path)
    if path in S["refuse"]:
        raise HTTPException(status_code=403)
    if path in S["broken"]:
        raise HTTPException(status_code=500)
    if path == "/admin/appointments":
        rows = [a for a in S["appts"] if q["date_from"] <= a["appointment_date"] <= q["date_to"]
                and a["status"] == q.get("status", a["status"])]
        return {"appointments": rows, "total": len(rows)}
    return {
        "/admin/leaves": S["leaves"],
        "/admin/opd/queue": {"doctors": [{"rows": [{"stage": "waiting"}, {"stage": "waiting"},
                                                   {"stage": "in_consultation"}]}]},
        "/admin/bookings/pending-review": {"bookings": [{"id": "b1"}]},
        "/admin/reports/deliveries": {"deliveries": [{"id": "r1"}, {"id": "r2"}]},
        "/admin/leads": {"rows": [], "total": 6},
        "/admin/voice/calls": {"calls": [], "total": 18},
        "/admin/notifications/unread-count": {"success": True, "unread_count": 4},
    }[path]


@fake.post("/admin/{rest:path}")
async def _no_writes(rest: str):
    S["seen"].append(("POST", rest))
    raise HTTPException(status_code=405)


client = TestClient(app)


@pytest.fixture
def br(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    _reset()
    clinic = {"id": CID, "plan": "polyclinic", "account_type": "tenant", "features": {"kriya_ketu": True}}
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=clinic)):
        yield as_user


def _ask(text):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": "dashboard"}}).json()


def _lines(out):
    return {c.get("page"): c["text"] for c in out["cards"][1:]}


def test_morning_briefing_has_every_section_the_user_can_read(db, br):  # noqa: F811
    _reset(appts=[_appt("Dr. Rao"), _appt("Dr. Rao", "11:00"), _appt("Dr. Anitha")],
           leaves=[{"doctor_name": "Dr. Srinivas", "leave_date": TODAY, "leave_type": "full"},
                   {"doctor_name": "Dr. Old", "leave_date": "2026-10-01", "leave_type": "full"}])
    out = _ask("good morning")
    assert (out["state"], out["action_id"]) == ("COMPLETED_VERIFIED", "briefing.morning"), out
    assert "15 Oct" in out["cards"][0]["text"]
    lines = _lines(out)
    assert "3" in lines["appointments"] and "Dr. Rao 2" in lines["appointments"]
    assert "Dr. Srinivas" in lines["leaves"] and "Dr. Old" not in lines["leaves"]
    assert "2 waiting" in lines["opdqueue"] and "1" in lines["payments"]
    assert "2" in lines["diagreports"] and "6" in lines["leads"] and "18" in lines["voice"]
    assert "Secret Patient" not in json.dumps(out) + json.dumps(db.tables["assistant_runs"])
    assert not any(isinstance(x, tuple) for x in S["seen"])                       # reads only


def test_sections_the_route_refuses_are_left_out(db, br):  # noqa: F811
    # BRF-01: a front desk without LEADS_MANAGE / REPORTS_VIEW, clinic without OPD or the receptionist.
    _reset(appts=[_appt("Dr. Rao")],
           refuse={"/admin/leads", "/admin/reports/deliveries", "/admin/opd/queue", "/admin/voice/calls"})
    out = _ask("good morning")
    assert out["state"] == "COMPLETED_VERIFIED", out
    assert set(_lines(out)) == {"appointments", "leaves", "payments", None}
    assert "lead" not in json.dumps(out["cards"]).lower()


def test_a_section_the_server_could_not_read_says_so(db, br):  # noqa: F811
    _reset(broken={"/admin/bookings/pending-review"})
    out = _ask("good morning")
    assert out["state"] == "COMPLETED_VERIFIED" and "couldn't" in _lines(out)["payments"].lower()


def test_end_of_day(db, br):  # noqa: F811
    _reset(appts=[_appt("Dr. Rao", token=3), _appt("Dr. Rao"), _appt("Dr. Rao", status="pending_payment"),
                  _appt("Dr. Anitha", "09:30", day=TOMORROW), _appt("Dr. Rao", "11:00", day=TOMORROW)],
           leaves=[{"doctor_name": "Dr. Srinivas", "leave_date": TOMORROW, "leave_type": "half_morning"}])
    out = _ask("wrap up the day")
    assert (out["state"], out["action_id"]) == ("COMPLETED_VERIFIED", "briefing.eod"), out
    text = " | ".join(c["text"] for c in out["cards"])
    assert "1 confirmed booking" in text and "1 booking" in text                      # not arrived, payment hold
    assert "Dr. Srinivas" in text and "morning" in text and "09:30" in text and "2 bookings" in text
    assert "Secret Patient" not in text


def test_briefings_make_no_llm_call(db, br):  # noqa: F811
    # BRF-02
    with patch("app.services.ai_gateway.call_ai_gateway", side_effect=AssertionError("LLM called")):
        assert _ask("good morning")["state"] == "COMPLETED_VERIFIED"
        assert _ask("wrap up the day")["state"] == "COMPLETED_VERIFIED"


# ── executor: optional reads ─────────────────────────────────────────────────

def test_a_write_step_can_never_be_optional():
    run = {"id": "r", "clinic_id": CID, "username": "u", "state": "EXECUTING"}
    with pytest.raises(ValueError):
        asyncio.run(executor.execute(run, [Step("w", Call("POST", "/admin/x"), optional=True)],
                                     CallerAuth(None, None), app=fake))


def test_registry_shape():
    for aid in ("briefing.morning", "briefing.eod"):
        spec = REGISTRY[aid]
        assert (spec.risk, spec.permission, spec.perm_source) == ("R0", None, "none")
        assert all(s.optional and s.call.method == "GET" for s in spec.call({}))


@pytest.mark.parametrize("aid", ["briefing.morning", "briefing.eod"])
def test_corpus_covers_each_action(aid):
    corpus = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                        .read_text(encoding="utf-8"))["phrases"]
    pos = [e for e in corpus if e["expect"] == f"action:{aid}"]
    assert len(pos) >= 5 and {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}, aid
    assert len([e for e in corpus if e.get("near") == aid and e["expect"] != f"action:{aid}"]) >= 2, aid
