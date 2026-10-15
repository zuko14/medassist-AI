"""KK-P4-13: voice receptionist admin actions — knowledge, lexicon, learning review (VOICE-12..14).

VOICE-12: knowledge add / delete through POST/DELETE /admin/voice/knowledge, read back.
VOICE-13: lexicon add (doctor / test / department alias) through POST /admin/voice/lexicon; the target
          must be one of this clinic's own records (the catalogue the Learning form offers).
VOICE-14: learning suggestions listed (R0), approved / dismissed (R2) through the real endpoints.
Every write is VOICE_MANAGE (handler check) on a clinic with the AI receptionist on; the real routes
answer 403 without it. Answers and phrases that trip the clinical screen are never taught.

voice.settings_update was held for D-13; it is released with that fix (tests/test_assistant_voice_settings.py).
"""

import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, rules
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.voice.dates import IST
from tests.test_assistant_runs import CID, _user, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=IST)
CATALOGUE = {"specialty_synonym": ["Dermatology", "General Medicine"],
             "doctor_alias": ["Dr. Anitha Rao", "Dr. Kiran Kumar"], "test_alias": ["CBC", "Thyroid Profile"]}


def _sugg(sid, phrase, category="doctor_alias", proposed="Dr. Anitha Rao", safe=True, n=4):
    return {"id": sid, "patient_phrase": phrase, "category": category, "proposed_canonical": proposed,
            "evidence_count": n, "safety_screen_passed": safe, "status": "pending",
            "sample_utterances": ["my number is 9876543210"]}


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, action", [
    ("teach the receptionist an answer", "voice.knowledge_add"),
    ("receptionist ko sikhao", "voice.knowledge_add"),
    ("remove the receptionist answer about parking", "voice.knowledge_delete"),
    ('callers say "anitha madam" for Dr Anitha', "voice.lexicon_add"),
    ('remove the alias "anitha madam"', "voice.lexicon_delete"),
    ("what did the receptionist learn", "voice.learning_review"),
    ("receptionist ne kya seekha", "voice.learning_review"),
    ('approve the suggestion "skin doctor"', "voice.learning_approve"),
    ('dismiss the suggestion "skin doctor"', "voice.learning_dismiss"),
])
def test_routes(text, action):
    r = rules.route(text, now=NOW)
    assert r.action_id == action, r


def test_slots_come_from_the_text():
    r = rules.route('callers say "anitha madam" for Dr Anitha', now=NOW)
    assert (r.kind, r.params) == ("action", {"phrase": "anitha madam", "target": "dr anitha"}), r
    r = rules.route("remove the receptionist answer about parking", now=NOW)
    assert r.params == {"topic": "parking"}
    r = rules.route("teach the receptionist an answer", now=NOW)
    assert (r.kind, set(r.missing)) == ("clarify", {"question", "answer"})  # verbatim, only from the form


# ── API against a fake panel ─────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}


def _reset(knowledge=(), lexicon=(), suggestions=(), view=True, write_ok=True):
    S.clear()
    S.update(knowledge=[dict(x) for x in knowledge], lexicon=[dict(x) for x in lexicon],
             suggestions=[dict(x) for x in suggestions], view=view, write_ok=write_ok, writes=[])


def _view():
    if not S["view"]:
        raise HTTPException(status_code=403)


@fake.get("/admin/voice/knowledge")
async def _kb():
    _view()
    return {"entries": S["knowledge"], "summary": {}}


@fake.post("/admin/voice/knowledge")
async def _kb_add(request: Request):
    body = await request.json()
    S["writes"].append(("kb_add", body))
    row = {"id": f"k{len(S['knowledge']) + 1}", **body}
    if S["write_ok"]:
        S["knowledge"].append(row)
    return {"entry": row}


@fake.delete("/admin/voice/knowledge/{eid}")
async def _kb_del(eid: str):
    S["writes"].append(("kb_del", eid))
    if S["write_ok"]:
        S["knowledge"] = [x for x in S["knowledge"] if x["id"] != eid]
    return {"success": True}


@fake.get("/admin/voice/lexicon")
async def _lex():
    _view()
    return {"entries": S["lexicon"]}


@fake.post("/admin/voice/lexicon")
async def _lex_add(request: Request):
    body = await request.json()
    S["writes"].append(("lex_add", body))
    row = {"id": f"l{len(S['lexicon']) + 1}", **body}
    if S["write_ok"]:
        S["lexicon"].append(row)
    return {"entry": row}


@fake.delete("/admin/voice/lexicon/{eid}")
async def _lex_del(eid: str):
    S["writes"].append(("lex_del", eid))
    if S["write_ok"]:
        S["lexicon"] = [x for x in S["lexicon"] if x["id"] != eid]
    return {"success": True}


@fake.get("/admin/voice/learning/suggestions")
async def _sugs(status: str = "pending"):
    _view()
    return {"suggestions": [s for s in S["suggestions"] if s["status"] == status], "catalogue": CATALOGUE}


@fake.post("/admin/voice/learning/suggestions/{sid}/{verb}")
async def _review(sid: str, verb: str, request: Request):
    body = await request.json() if verb == "approve" else None
    S["writes"].append((verb, sid, body))
    if S["write_ok"]:
        for s in S["suggestions"]:
            if s["id"] == sid:
                s["status"] = "approved" if verb == "approve" else "dismissed"
    return {"success": True}


client = TestClient(app)


@pytest.fixture
def va(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    _reset()
    clinic = {"id": CID, "plan": "polyclinic", "account_type": "tenant",
              "features": {"kriya_ketu": True, "ai_receptionist": True}}
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=clinic)):
        yield as_user


def _ask(text, page="voice"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": page}}).json()


def _answer(out, **answers):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "answer", "reply_to_run_id": out["run_id"],
        "slot_answers": answers, "ui_context": {"page": "voice"}}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm",
                       json={"plan_hash": out["plan_hash"], **extra}).json()


def _card(out):
    return out["cards"][0]


# VOICE-12 knowledge

def test_teach_an_answer_from_the_form_then_read_back(db, va):  # noqa: F811
    out = _answer(_ask("teach the receptionist an answer"),
                  question="Do you have parking?", answer="Yes, free parking in the basement.")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "free parking" in json.dumps(_card(out)) and S["writes"] == []
    done = _confirm(out)
    assert done["state"] == "COMPLETED_VERIFIED", done
    assert S["writes"] == [("kb_add", {"question": "Do you have parking?", "answer": "Yes, free parking in the basement."})]


def test_a_clinical_answer_is_never_taught(db, va):  # noqa: F811
    out = _answer(_ask("teach the receptionist an answer"),
                  question="What for fever?", answer="Take paracetamol 500 mg twice a day.")
    assert out["error_code"] == "nothing_to_do" and "medicine" in out["message"] and S["writes"] == []


def test_a_question_already_answered_is_refused(db, va):  # noqa: F811
    _reset(knowledge=[{"id": "k1", "question": "Do you have parking?", "answer": "Yes."}])
    out = _answer(_ask("teach the receptionist an answer"), question="do you have  parking?", answer="No.")
    assert out["error_code"] == "nothing_to_do" and "already" in out["message"] and S["writes"] == []


def test_a_write_the_read_back_cannot_see_is_unknown(db, va):  # noqa: F811
    _reset(write_ok=False)
    out = _answer(_ask("teach the receptionist an answer"), question="Do you have parking?", answer="Yes.")
    assert _confirm(out)["state"] == "UNKNOWN_OUTCOME" and len(S["writes"]) == 1


def test_remove_an_answer_by_a_word_of_its_question(db, va):  # noqa: F811
    _reset(knowledge=[{"id": "k1", "question": "Do you have parking?", "answer": "Yes."},
                      {"id": "k2", "question": "Is there a pharmacy?", "answer": "Yes."}])
    out = _ask("remove the receptionist answer about parking")
    assert out["state"] == "NEEDS_CONFIRMATION" and "parking" in json.dumps(_card(out)), out
    assert _confirm(out)["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("kb_del", "k1")] and [x["id"] for x in S["knowledge"]] == ["k2"]


def test_remove_with_two_matches_lists_them(db, va):  # noqa: F811
    _reset(knowledge=[{"id": "k1", "question": "Is parking free?", "answer": "Yes."},
                      {"id": "k2", "question": "Where is parking?", "answer": "Basement."}])
    out = _ask("remove the receptionist answer about parking")
    assert out["error_code"] == "nothing_to_do" and "Where is parking?" in out["message"] and S["writes"] == []


# VOICE-13 lexicon

def test_alias_for_a_doctor_uses_the_catalogue_spelling(db, va):  # noqa: F811
    out = _ask('callers say "anitha madam" for Dr Anitha')
    assert out["state"] == "NEEDS_CONFIRMATION" and "Dr. Anitha Rao" in json.dumps(_card(out)), out
    assert _confirm(out)["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("lex_add", {"kind": "doctor_alias", "phrase": "anitha madam", "canonical": "Dr. Anitha Rao"})]


def test_alias_for_a_department_and_a_test(db, va):  # noqa: F811
    out = _ask('patients say "skin doctor" for dermatology')
    assert _confirm(out)["state"] == "COMPLETED_VERIFIED"
    out = _ask('callers say "sugar test" for thyroid profile')
    _confirm(out)
    assert [w[1]["kind"] for w in S["writes"]] == ["specialty_synonym", "test_alias"]


def test_alias_for_something_the_clinic_does_not_have_is_refused(db, va):  # noqa: F811
    out = _ask('callers say "heart doctor" for cardiology')
    assert out["error_code"] == "nothing_to_do" and "cardiology" in out["message"].lower() and S["writes"] == []


def test_an_existing_alias_is_refused(db, va):  # noqa: F811
    _reset(lexicon=[{"id": "l1", "kind": "doctor_alias", "phrase": "Anitha Madam", "canonical": "Dr. Anitha Rao"}])
    out = _ask('callers say "anitha madam" for Dr Anitha')
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_remove_an_alias(db, va):  # noqa: F811
    _reset(lexicon=[{"id": "l1", "kind": "doctor_alias", "phrase": "anitha madam", "canonical": "Dr. Anitha Rao"}])
    out = _ask('remove the alias "Anitha Madam"')
    assert _confirm(out)["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("lex_del", "l1")]


# VOICE-14 learning

def test_learning_list_shows_phrases_never_the_callers_words(db, va):  # noqa: F811
    _reset(suggestions=[_sugg("s1", "anitha madam"), _sugg("s2", "skin doctor", "specialty_synonym", "Dermatology")])
    out = _ask("what did the receptionist learn")
    assert out["state"] == "COMPLETED_VERIFIED" and out["action_id"] == "voice.learning_review", out
    text = json.dumps(out)
    assert "skin doctor" in text and "9876543210" not in text and "9876543210" not in json.dumps(db.tables["assistant_runs"])


def test_approve_a_suggestion_as_suggested(db, va):  # noqa: F811
    _reset(suggestions=[_sugg("s1", "skin doctor", "specialty_synonym", "Dermatology")])
    out = _ask('approve the suggestion "skin doctor"')
    assert out["state"] == "NEEDS_CONFIRMATION" and "Dermatology" in json.dumps(_card(out)), out
    assert _confirm(out)["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("approve", "s1", {})]


def test_a_faq_suggestion_needs_its_answer_written_on_the_page(db, va):  # noqa: F811
    _reset(suggestions=[_sugg("s1", "do you have parking", "knowledge_faq", "")])
    out = _ask('approve the suggestion "do you have parking"')
    assert out["error_code"] == "nothing_to_do" and "answer" in out["message"] and S["writes"] == []


def test_a_suggestion_blocked_by_the_safety_screen_can_only_be_dismissed(db, va):  # noqa: F811
    _reset(suggestions=[_sugg("s1", "sugar tablet", safe=False)])
    out = _ask('approve the suggestion "sugar tablet"')
    assert out["error_code"] == "nothing_to_do" and "dismiss" in out["message"] and S["writes"] == []
    done = _confirm(_ask('dismiss the suggestion "sugar tablet"'))
    assert done["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("dismiss", "s1", None)]


def test_a_suggestion_reviewed_meanwhile_is_not_approved_twice(db, va):  # noqa: F811
    _reset(suggestions=[_sugg("s1", "skin doctor", "specialty_synonym", "Dermatology")])
    out = _ask('approve the suggestion "skin doctor"')
    S["suggestions"][0]["status"] = "dismissed"
    again = _confirm(out)
    assert again["state"] == "REJECTED" and S["writes"] == []


# permissions and gates

def test_staff_without_voice_manage_are_refused(db, va):  # noqa: F811
    va["user"] = _user()
    out = _ask('callers say "anitha madam" for Dr Anitha')
    assert out["state"] == "REJECTED" and "Voice Manage" in out["message"] and S["writes"] == []


def test_voice_manage_without_voice_view_is_told_why(db, va):  # noqa: F811
    va["user"] = _user()
    va["user"].permissions = ["VOICE_MANAGE"]
    _reset(view=False)
    out = _ask('callers say "anitha madam" for Dr Anitha')
    assert out["error_code"] == "nothing_to_do" and "Voice View" in out["message"] and S["writes"] == []


def test_settings_update_is_released_with_the_d13_fix():
    # Held while D-13 was open; registered once saves wrote only the changed key (tests/test_assistant_voice_settings.py).
    assert REGISTRY["voice.settings_update"].endpoint == ("PUT", "/admin/voice/settings")


@pytest.mark.parametrize("method, path, body", [
    ("post", "/admin/voice/knowledge", {"question": "Parking?", "answer": "Yes."}),
    ("delete", "/admin/voice/knowledge/x", None),
    ("post", "/admin/voice/lexicon", {"kind": "doctor_alias", "phrase": "a", "canonical": "b"}),
    ("delete", "/admin/voice/lexicon/x", None),
    ("post", "/admin/voice/learning/suggestions/x/approve", {}),
    ("post", "/admin/voice/learning/suggestions/x/dismiss", None),
])
def test_the_real_routes_refuse_without_voice_manage(method, path, body):
    from tests.voice.test_voice_routes import CID as VCID, VIEWER, client as voice_client
    c, p = voice_client(VIEWER)
    try:
        kw = {"json": body} if body is not None else {}
        assert getattr(c, method)(f"{path}?clinic_id={VCID}", **kw).status_code == 403
    finally:
        p.stop()


# ── registry + corpus ────────────────────────────────────────────────────────

ACTIONS = ("voice.knowledge_add", "voice.knowledge_delete", "voice.lexicon_add", "voice.lexicon_delete",
           "voice.learning_review", "voice.learning_approve", "voice.learning_dismiss")


@pytest.mark.parametrize("aid", ACTIONS)
def test_registry_shape(aid):
    spec = REGISTRY[aid]
    assert (spec.permission, spec.perm_source, spec.page) == (
        "VOICE_VIEW" if aid == "voice.learning_review" else "VOICE_MANAGE", "handler", "voice")
    assert spec.risk == ("R0" if aid == "voice.learning_review" else "R2")


@pytest.mark.parametrize("aid", ACTIONS)
def test_corpus_covers_each_action(aid):
    corpus = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                        .read_text(encoding="utf-8"))["phrases"]
    # knowledge_add always asks (its question and answer come only from the form): tagged clarify entries.
    pos = [e for e in corpus if e["expect"] == f"action:{aid}" or e.get("action") == aid]
    assert len(pos) >= 5 and {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}, aid
    assert len([e for e in corpus if e.get("near") == aid and e["expect"] != f"action:{aid}"]) >= 2, aid
