"""KK-P4-06: PB-03 leads -> receptionist calls, PB-04 lead funnel, PB-05 automatic lead calls on/off.

WA-08: only `hot` / `open` leads are read, and a do-not-contact (masked) number is never sent.
WA-09: `consent_attested` is never true: these are the clinic's own WhatsApp contacts.
WA-10 (KK-P4-06a): the preview's dry run gives the exact list and skip reasons; nothing is queued
       until confirm, and then only the dry run's `would_queue` phones.
WA-11: the funnel's numbers equal the source rows.
WA-12: PB-05 PUTs the whole settings object it read with one field changed; a concurrent edit
       between preview and confirm means a new preview.
Words naming an interest must match a known interest label, else refusal: an ignored filter
would widen who gets called. No phone number is stored in assistant_runs.
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
P1, P2, P3 = "+919000000001", "+919000000002", "+919000000003"


def _lead(name, phone, interests=("Dermatology",)):
    return {"name": name, "phone": phone, "interests": list(interests), "stage": "interested", "window_open": False}


SETTINGS = {"primary_language": "te-IN", "assistant_name": "Kriya", "speaker": None, "pace": 1.0,
            "reception_hours": {"start": "09:00", "end": "18:00", "days": "Mon-Sat"}, "emergency_number": None,
            "monthly_budget_paise": 100000,
            "outbound": {"auto_leads": False, "window_start": "10:00", "window_end": "19:00", "max_attempts": 2,
                         "daily_cap": 30, "concurrent": 1, "pitch": {"te": "", "hi": "", "en": "We have a pharmacy."}}}


# ── resolvers / routing ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("turn on automatic lead calls", "on"), ("stop auto lead calls", "off"), ("auto lead calls chalu karo", "on"),
    ("auto lead calls band karo", "off"), ("auto lead calls", None),
])
def test_resolve_on_off(text, expected):
    assert resolve.resolve_on_off(text) == expected


@pytest.mark.parametrize("text, params", [
    ("call today's hot leads", {"segment": "hot", "days": 1}),
    ("call the open leads from this week", {"segment": "open", "days": 7}),
    ("call all dermatology leads from this week", {"segment": "open", "days": 7, "interest": "dermatology"}),
    ("leads ki call cheyyi", {"segment": "open", "days": 7}),
])
def test_call_leads_routes_with_its_filters(text, params):
    r = rules.route(text, now=NOW)
    assert (r.kind, r.action_id) == ("action", "leads.call"), r
    assert r.params == params


@pytest.mark.parametrize("text, action", [
    ("how did the lead calls go", "leads.funnel"), ("lead conversion report", "leads.funnel"),
    ("turn on automatic lead calls", "voice.auto_leads"), ("new leads", "leads.list"),
])
def test_routes(text, action):
    assert rules.route(text, now=NOW).action_id == action


# ── API against a fake panel ─────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}


def _reset(leads=(), jobs=(), settings=None, skip=(), leads_forbidden=False, queue_ok=True):
    S.clear()
    S.update(leads=[dict(x) for x in leads], jobs=[dict(j) for j in jobs], settings=json.loads(json.dumps(settings or SETTINGS)),
             skip=dict(skip), leads_forbidden=leads_forbidden, queue_ok=queue_ok, writes=[], lead_queries=[])


@fake.get("/admin/leads")
async def _leads(request: Request):
    if S["leads_forbidden"]:
        raise HTTPException(status_code=403)
    q = dict(request.query_params)
    S["lead_queries"].append(q)
    rows = [x for x in S["leads"] if not q.get("interest") or q["interest"] in x["interests"]]
    if q.get("segment") == "booked":
        rows = [x for x in S["leads"] if x.get("stage") == "booked"]
    return {"rows": rows, "total": len(rows), "interest_labels": ["Dermatology", "Dental", "Lab test"]}


@fake.post("/admin/voice/outbound/bulk")
async def _bulk(request: Request):
    body = await request.json()
    phones = [c["phone"] for c in body["contacts"]]
    skipped = [{"phone": p, "reason": S["skip"][p]} for p in phones if p in S["skip"]]
    would = [p for p in phones if p not in S["skip"]]
    if body.get("dry_run"):
        return {"queued": 0, "would_queue": would, "skipped": skipped, "truncated": 0, "dry_run": True}
    S["writes"].append(("bulk", body))
    if S["queue_ok"]:
        S["jobs"] += [{"patient_phone": p, "status": "queued", "attempts": 0} for p in would]
    return {"queued": len(would), "skipped": skipped, "truncated": 0}


@fake.get("/admin/voice/outbound")
async def _jobs():
    return {"jobs": S["jobs"]}


@fake.get("/admin/voice/settings")
async def _get_settings():
    return {"settings": S["settings"], "budget_paise": 100000}


@fake.put("/admin/voice/settings")
async def _put_settings(request: Request):
    body = await request.json()
    S["writes"].append(("settings", body))
    S["settings"] = body
    return {"success": True}


client = TestClient(app)


@pytest.fixture
def lc(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    _reset()
    # The real gate (tenant.ai_receptionist_enabled): an explicit owner opt-in on a tenant account.
    clinic = {"id": CID, "plan": "polyclinic", "account_type": "tenant",
              "features": {"kriya_ketu": True, "ai_receptionist": True}}
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=clinic)):
        yield as_user


def _ask(text):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": "leads"}}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"], **extra})


def _card(out):
    return out["cards"][0]


# PB-03

def test_call_leads_previews_the_exact_list_then_queues_only_those(db, lc):  # noqa: F811
    _reset(leads=[_lead("Ravi", P1), _lead("Sita", P2), _lead("Anu", P3)], skip={P3: "Called in the last 7 days"})
    out = _ask("call the open leads from this week")
    card = _card(out)
    assert (out["state"], card["type"], card["typed_count"]) == ("NEEDS_CONFIRMATION", "preview-typed", 2), out
    text = json.dumps(card, ensure_ascii=False)
    assert "Called in the last 7 days" in text and "10:00" in text and "19:00" in text
    assert S["writes"] == [] and all(q["segment"] in ("hot", "open") for q in S["lead_queries"])   # WA-08
    for p in (P1, P2, P3):
        assert p not in json.dumps(db.tables["assistant_runs"])                                     # no phones stored
    done = _confirm(out, typed_count=2).json()
    assert done["state"] == "COMPLETED_VERIFIED", done
    (kind, body), = S["writes"]
    assert [c["phone"] for c in body["contacts"]] == [P1, P2]                                      # WA-10
    assert body["consent_attested"] is False and "dry_run" not in body                             # WA-09
    assert body["contacts"][0]["interest"] == "Dermatology"


def test_a_do_not_contact_number_is_never_sent(db, lc):  # noqa: F811
    _reset(leads=[_lead("Ravi", P1), _lead("Dnc", "+919•••••0009")])
    out = _ask("call the open leads from this week")
    assert _card(out)["typed_count"] == 1
    _confirm(out, typed_count=1)
    assert [c["phone"] for c in S["writes"][0][1]["contacts"]] == [P1]


def test_an_unknown_interest_is_refused_never_widened(db, lc):  # noqa: F811
    _reset(leads=[_lead("Ravi", P1)])
    out = _ask("call all cardiology leads from this week")
    assert out["error_code"] == "nothing_to_do" and "Dermatology" in out["message"] and S["writes"] == []


def test_a_known_interest_filters_the_read(db, lc):  # noqa: F811
    _reset(leads=[_lead("Ravi", P1), _lead("Sita", P2, ("Dental",))])
    out = _ask("call all dermatology leads from this week")
    assert _card(out)["typed_count"] == 1 and S["lead_queries"][-1]["interest"] == "Dermatology"


def test_no_one_to_call_is_refused_with_the_reasons(db, lc):  # noqa: F811
    _reset(leads=[_lead("Ravi", P1)], skip={P1: "Opted out — never called"})
    out = _ask("call the open leads from this week")
    assert out["error_code"] == "nothing_to_do" and "Opted out" in out["message"] and S["writes"] == []


def test_more_than_100_leads_must_be_narrowed(db, lc):  # noqa: F811
    _reset(leads=[_lead(f"P{i}", f"+91900000{i:04d}") for i in range(101)])
    assert _ask("call the open leads from this week")["error_code"] == "too_many"


def test_without_leads_permission_the_refusal_says_so(db, lc):  # noqa: F811
    _reset(leads_forbidden=True)
    out = _ask("call the open leads from this week")
    assert out["error_code"] == "nothing_to_do" and "Leads Manage" in out["message"]


def test_a_queue_the_read_back_cannot_see_is_unknown(db, lc):  # noqa: F811
    _reset(leads=[_lead("Ravi", P1)], queue_ok=False)
    out = _ask("call the open leads from this week")
    assert _confirm(out, typed_count=1).json()["state"] == "UNKNOWN_OUTCOME" and len(S["writes"]) == 1


def test_the_dry_run_is_the_only_post_a_preview_may_send():
    from app.assistant.executor import Call
    from app.assistant.registry import read_only_post
    assert read_only_post(Call("POST", "/admin/voice/outbound/bulk", json={"contacts": [], "dry_run": True}))
    assert not read_only_post(Call("POST", "/admin/voice/outbound/bulk", json={"contacts": []}))
    assert not read_only_post(Call("POST", "/admin/leads/message", json={"dry_run": True}))


# PB-04

def test_funnel_numbers_equal_the_source_rows(db, lc):  # noqa: F811
    _reset(jobs=[{"patient_phone": P1, "status": "queued"}, {"patient_phone": P2, "status": "dialing"},
                 {"patient_phone": P3, "status": "completed"}, {"patient_phone": "+919000000004", "status": "completed"},
                 {"patient_phone": "+919000000005", "status": "failed"}],
           leads=[dict(_lead("Ravi", P1), stage="booked")])
    out = _ask("how did the lead calls go")
    assert out["state"] == "COMPLETED_VERIFIED" and out["action_id"] == "leads.funnel", out
    items = dict(_card(out)["rows"])   # present.metrics: a table of [label, value]
    assert (items["Waiting to be called"], items["Calling now"], items["Calls finished"], items["Not reached"]) \
        == ("1", "1", "2", "1")
    assert items["Leads who booked (30 days)"] == "1" and S["writes"] == []                      # WA-11


# PB-05

def test_auto_leads_puts_the_whole_settings_with_one_field_changed(db, lc):  # noqa: F811
    out = _ask("turn on automatic lead calls")
    assert out["state"] == "NEEDS_CONFIRMATION" and "30" in json.dumps(_card(out)), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (kind, body), = S["writes"]
    want = json.loads(json.dumps(SETTINGS))
    want["outbound"]["auto_leads"] = True
    assert body == want                                                                          # WA-12


def test_auto_leads_already_in_that_state_is_refused(db, lc):  # noqa: F811
    assert _ask("turn off automatic lead calls")["error_code"] == "nothing_to_do" and S["writes"] == []


def test_a_settings_edit_after_the_preview_means_a_new_preview(db, lc):  # noqa: F811
    out = _ask("turn on automatic lead calls")
    S["settings"]["outbound"]["daily_cap"] = 50
    again = _confirm(out).json()
    assert (again["state"], again["error_code"]) == ("NEEDS_CONFIRMATION", "plan_changed") and S["writes"] == []


def test_staff_without_voice_manage_are_refused(db, lc):  # noqa: F811
    lc["user"] = _user()
    out = _ask("turn on automatic lead calls")
    assert out["state"] == "REJECTED" and "Voice Manage" in out["message"]


# ── registry + corpus ────────────────────────────────────────────────────────

def test_registry_shape():
    assert (REGISTRY["leads.call"].risk, REGISTRY["leads.call"].endpoint) == ("R3", ("POST", "/admin/voice/outbound/bulk"))
    assert REGISTRY["leads.call"].max_items == 100
    assert (REGISTRY["leads.funnel"].risk, REGISTRY["voice.auto_leads"].risk) == ("R0", "R2")
    for aid in ("leads.call", "voice.auto_leads"):
        assert (REGISTRY[aid].permission, REGISTRY[aid].perm_source) == ("VOICE_MANAGE", "handler")


@pytest.mark.parametrize("aid", ["leads.call", "leads.funnel", "voice.auto_leads"])
def test_corpus_covers_each_action(aid):
    corpus = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                        .read_text(encoding="utf-8"))["phrases"]
    pos = [e for e in corpus if e["expect"] == f"action:{aid}"]
    assert len(pos) >= 5 and {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}
    assert len([e for e in corpus if e.get("near") == aid and e["expect"] != f"action:{aid}"]) >= 2
