"""KK-P4-03: report.resend (R2) + report.resend_failed (R3, PB-02), lead.message (R2), connector.run_now (R1).

- A report is named by patient name (and optionally words of the report name); anything but one
  match is refused with the matches listed. A report delivered in the last 24 h needs a tick
  (duplicate risk, PB-02). Read-back: the row is `sent` with a newer sent_at.
- "Resend failed reports" covers the failed rows of the last day, at most 20, typed count (R3).
- lead.message sends text the staff member wrote, verbatim (quotes or after a colon) — Kriya never
  writes patient-facing text. Do-not-contact and outside-24h contacts are refused before any write
  (KK-FR-054: call instead). The endpoint answers 200 only when Meta accepted the message, so the
  response is the evidence (ADR-004); no phone number is stored in assistant_runs.
- connector.run_now dispatches the one enabled connector (the user's branch when pinned); the
  endpoint's own "running"/"queued" answer is the evidence; the run's results land on the page.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, resolve, rules
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.voice.dates import IST
from tests.test_assistant_runs import _user, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=IST)
OLD = (NOW - timedelta(days=3)).astimezone(timezone.utc).isoformat()
RECENT = (NOW - timedelta(hours=2)).astimezone(timezone.utc).isoformat()
PHONE = "919876543210"


def _rep(i, name, report="CBC", state="failed", sent_at=OLD):
    return {"id": f"r{i}", "patient_name": name, "patient_phone": "91******3210", "report_name": report,
            "status": "sent" if state == "delivered" else state, "state": state, "sent_at": sent_at}


def _lead(name, phone=PHONE, window=True):
    return {"name": name, "phone": phone, "window_open": window,
            "window_closes_at": "2026-10-14T20:00:00+05:30" if window else None}


# ── resolvers ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("message Ravi: Doctor is available till 1 pm", "Doctor is available till 1 pm"),
    ('whatsapp Sita "Your report is ready"', "Your report is ready"),
    ("message Ravi that the doctor is late", None),        # never composed from an instruction
    ("message Ravi:   ", None),
])
def test_resolve_message(text, expected):
    assert resolve.resolve_message(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("message Ravi Kumar: hello", "ravi kumar"),
    ("Ravi ko message bhejo: kal aaiye", "ravi"),
    ("resend Ravi's CBC report", "ravi cbc"),
    ("Ravi report malli pampu", "ravi"),
])
def test_resolve_lead_and_report_targets(text, expected):
    assert resolve.resolve_lead(text) == expected


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, action", [
    ("resend Ravi's report", "report.resend"),
    ("resend failed reports", "report.resend_failed"),
    ("fail aina reports malli pampu", "report.resend_failed"),
    ("message Ravi: Doctor is in till 1", "lead.message"),
    ("run the connector now", "connector.run_now"),
    ("failed reports", "reports.queue"),                    # the read, unchanged
])
def test_routes(text, action):
    r = rules.route(text, now=NOW)
    assert r.action_id == action, r


@pytest.mark.parametrize("text", ['message Ravi "are you coming today?"', "message Ravi: can you come at 5?"])
def test_a_message_that_is_a_question_is_still_sent_not_answered_with_help(text):
    # D-35: the question words are inside the staff member's own message, not the instruction.
    r = rules.route(text, now=NOW)
    assert (r.kind, r.action_id) == ("action", "lead.message"), r


def test_a_question_about_messaging_still_gets_help():
    assert rules.route("can I message Ravi?", now=NOW).kind == "help"


def test_a_message_without_its_text_asks_for_it():
    r = rules.route("message Ravi that the doctor is late", now=NOW)
    assert (r.kind, r.action_id, r.missing) == ("clarify", "lead.message", ("message",))


# ── API against a fake panel ─────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}


def _reset(reps=(), leads=(), connectors=(), branch=None, send_ok=True):
    S.clear()
    S.update(reps=[dict(r) for r in reps], leads=[dict(x) for x in leads], connectors=[dict(c) for c in connectors],
             branch=branch, send_ok=send_ok, writes=[])


@fake.get("/admin/me")
async def _me():
    return {"username": "desk1", "branch_id": S["branch"]}


@fake.get("/admin/reports/deliveries")
async def _deliveries(days: int = 7, state: str = "all"):
    rows = [r for r in S["reps"] if state == "all" or r["state"] == state]
    return {"deliveries": rows, "total": len(rows)}


@fake.post("/admin/reports/{rid}/resend")
async def _resend(rid: str):
    S["writes"].append(("resend", rid))
    r = next((r for r in S["reps"] if r["id"] == rid), None)
    if r is None:
        raise HTTPException(status_code=404)
    if not S["send_ok"]:
        raise HTTPException(status_code=400, detail="another delivery in progress")
    r.update(status="sent", state="delivered", sent_at=datetime.now(timezone.utc).isoformat())
    return {"success": True}


@fake.get("/admin/leads")
async def _leads(q: str = None):
    rows = [x for x in S["leads"] if not q or q.split()[0] in x["name"].lower() or q in x["phone"]]
    return {"rows": rows, "total": len(rows)}


@fake.post("/admin/leads/message")
async def _message(request: Request):
    body = await request.json()
    S["writes"].append(("message", body["phone"], body["message"]))
    if not S["send_ok"]:
        raise HTTPException(status_code=502, detail="WhatsApp did not accept the message.")
    return {"success": True}


@fake.get("/admin/connectors")
async def _connectors():
    return {"connectors": S["connectors"]}


@fake.post("/admin/connectors/{cid}/run-now")
async def _run_now(cid: str):
    S["writes"].append(("run-now", cid))
    return {"success": True, "status": "queued", "dispatched_to": "worker"}


client = TestClient(app)


@pytest.fixture
def p43(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW):
        yield as_user


def _ask(text, page="diagreports"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": page}}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"], **extra})


def _card(out):
    return out["cards"][0]


# report.resend

def test_resend_one_report_and_read_the_newer_send_back(db, p43):  # noqa: F811
    _reset(reps=[_rep(1, "Ravi Kumar"), _rep(2, "Sita Devi")])
    out = _ask("resend Ravi's report")
    assert out["state"] == "NEEDS_CONFIRMATION" and _card(out)["requires_ack"] is False and S["writes"] == [], out
    done = _confirm(out).json()
    assert done["state"] == "COMPLETED_VERIFIED", done
    assert S["writes"] == [("resend", "r1")]


def test_two_reports_for_one_name_are_listed_not_guessed(db, p43):  # noqa: F811
    _reset(reps=[_rep(1, "Ravi Kumar", "CBC"), _rep(2, "Ravi Kumar", "Lipid profile")])
    out = _ask("resend Ravi's report")
    assert out["error_code"] == "nothing_to_do" and "CBC" in out["message"] and "Lipid" in out["message"]
    assert _ask("resend Ravi's lipid report")["state"] == "NEEDS_CONFIRMATION"


def test_a_report_delivered_in_the_last_day_needs_the_duplicate_tick(db, p43):  # noqa: F811
    _reset(reps=[_rep(1, "Ravi Kumar", state="delivered", sent_at=RECENT)])
    out = _ask("resend Ravi's report")
    assert _card(out)["requires_ack"] is True and "again" in _card(out)["ack_text"]
    assert _confirm(out).status_code == 422 and S["writes"] == []
    assert _confirm(out, ack_duplicate=True).json()["state"] == "COMPLETED_VERIFIED"


def test_a_refused_resend_is_failed_not_done(db, p43):  # noqa: F811
    _reset(reps=[_rep(1, "Ravi Kumar")], send_ok=False)
    out = _ask("resend Ravi's report")
    assert _confirm(out).json()["state"] == "FAILED"


def test_staff_without_reports_resolve_is_refused(db, p43):  # noqa: F811
    p43["user"] = _user()
    _reset(reps=[_rep(1, "Ravi Kumar")])
    out = _ask("resend Ravi's report")
    assert out["state"] == "REJECTED" and "Reports Resolve" in out["message"] and S["writes"] == []


# report.resend_failed (R3)

def test_resend_all_failed_needs_the_typed_count(db, p43):  # noqa: F811
    _reset(reps=[_rep(1, "Ravi Kumar"), _rep(2, "Sita Devi"), _rep(3, "Anu", state="delivered")])
    out = _ask("resend failed reports")
    card = _card(out)
    assert (card["type"], card["typed_count"]) == ("preview-typed", 2)
    assert _confirm(out, typed_count=1).status_code == 422 and S["writes"] == []
    done = _confirm(out, typed_count=2).json()
    assert done["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("resend", "r1"), ("resend", "r2")]


def test_more_than_20_failed_is_refused(db, p43):  # noqa: F811
    _reset(reps=[_rep(i, f"P{i}") for i in range(21)])
    assert _ask("resend failed reports")["error_code"] == "too_many"


def test_no_failed_reports_is_refused(db, p43):  # noqa: F811
    assert _ask("resend failed reports")["error_code"] == "nothing_to_do"


# lead.message

def test_message_a_lead_verbatim_inside_the_window(db, p43):  # noqa: F811
    _reset(leads=[_lead("Ravi Kumar"), _lead("Sita Devi", phone="919000000001")])
    out = _ask("message Ravi: Doctor is available till 1 pm", page="leads")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "Doctor is available till 1 pm" in json.dumps(_card(out))
    assert PHONE not in json.dumps(db.tables["assistant_runs"])            # no number stored
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("message", PHONE, "Doctor is available till 1 pm")]


def test_outside_the_24h_window_suggests_a_call(db, p43):  # noqa: F811
    _reset(leads=[_lead("Ravi Kumar", window=False)])
    out = _ask("message Ravi: hello", page="leads")
    assert out["error_code"] == "nothing_to_do" and "call" in out["message"].lower() and S["writes"] == []


def test_a_do_not_contact_lead_is_refused(db, p43):  # noqa: F811
    _reset(leads=[_lead("Ravi Kumar", phone="919•••••3210")])
    out = _ask("message Ravi: hello", page="leads")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_a_message_whatsapp_refused_is_never_shown_as_sent_nor_resent(db, p43):  # noqa: F811
    _reset(leads=[_lead("Ravi Kumar")], send_ok=False)
    out = _ask("message Ravi: hello", page="leads")
    # A 5xx on a write is uncertain to the executor (KK-P1-05): staff are told to check, never auto-resent.
    assert _confirm(out).json()["state"] == "UNKNOWN_OUTCOME" and len(S["writes"]) == 1


def test_the_message_can_be_typed_on_the_form(db, p43):  # noqa: F811
    _reset(leads=[_lead("Ravi Kumar")])
    out = _ask("message Ravi that the doctor is late", page="leads")
    reply = client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "x", "reply_to_run_id": out["run_id"],
        "slot_answers": {"message": "The doctor is 30 minutes late today."}}).json()
    assert reply["state"] == "NEEDS_CONFIRMATION" and "30 minutes late" in json.dumps(reply["cards"])


# connector.run_now

def test_run_the_one_enabled_connector(db, p43):  # noqa: F811
    _reset(connectors=[{"id": "c1", "connector_type": "mocdoc", "is_enabled": True, "branch_id": None,
                        "last_run_at": OLD}, {"id": "c2", "connector_type": "mocdoc", "is_enabled": False}])
    out = _ask("run the connector now", page="connectors")
    assert out["state"] == "NEEDS_CONFIRMATION" and any("minutes" in w for w in _card(out)["warnings"])
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("run-now", "c1")]


def test_several_enabled_connectors_point_to_the_page(db, p43):  # noqa: F811
    _reset(connectors=[{"id": "c1", "is_enabled": True, "branch_id": "b1"},
                       {"id": "c2", "is_enabled": True, "branch_id": "b2"}])
    out = _ask("run the connector now", page="connectors")
    assert out["error_code"] == "nothing_to_do" and "Report Connector" in out["message"] and S["writes"] == []


def test_a_pinned_user_runs_their_branch_connector(db, p43):  # noqa: F811
    _reset(connectors=[{"id": "c1", "is_enabled": True, "branch_id": "b1"},
                       {"id": "c2", "is_enabled": True, "branch_id": "b2"}], branch="b2")
    out = _ask("run the connector now", page="connectors")
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("run-now", "c2")]


# ── registry + corpus ────────────────────────────────────────────────────────

ACTIONS = ("report.resend", "report.resend_failed", "lead.message", "connector.run_now")


def test_registry_shape():
    want = {
        "report.resend": ("R2", "REPORTS_RESOLVE", ("POST", "/admin/reports/{report_id}/resend")),
        "report.resend_failed": ("R3", "REPORTS_RESOLVE", ("POST", "/admin/reports/{report_id}/resend")),
        "lead.message": ("R2", "LEADS_MANAGE", ("POST", "/admin/leads/message")),
        "connector.run_now": ("R1", "CONNECTOR_MANAGE", ("POST", "/admin/connectors/{connector_id}/run-now")),
    }
    for aid, (risk, perm, endpoint) in want.items():
        spec = REGISTRY[aid]
        assert (spec.risk, spec.permission, spec.perm_source, spec.endpoint) == (risk, perm, "dependency", endpoint)
    assert REGISTRY["report.resend_failed"].max_items == 20


@pytest.mark.parametrize("aid", ACTIONS)
def test_corpus_covers_each_action(aid):
    corpus = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                        .read_text(encoding="utf-8"))["phrases"]
    pos = [e for e in corpus if e["expect"] == f"action:{aid}"]
    assert len(pos) >= 5 and {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}
    assert len([e for e in corpus if e.get("near") == aid and e["expect"] != f"action:{aid}"]) >= 2
