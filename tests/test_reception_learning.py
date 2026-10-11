"""Receptionist learning loop (migration 106): harvest -> distill -> staff approve.

No live DB: a tiny fake query builder records every query, so tests also assert
that each one carries the clinic_id predicate.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import reception_learning as rl
from app.tenancy import TENANT_OWNED_TABLES

CID = "11111111-1111-1111-1111-111111111111"
CATALOGUE = {"specialty_synonym": ["Ophthalmology", "Cardiology"], "doctor_alias": ["Dr. Ramesh Kumar"],
             "test_alias": ["Complete Blood Count"]}


class Q:
    def __init__(self, table):
        self.table, self.op, self.payload, self.filters = table, "select", None, {}

    def _op(self, op, payload=None):
        self.op, self.payload = op, payload
        return self

    def select(self, *a, **k): return self._op("select")
    def insert(self, p): return self._op("insert", p)
    def update(self, p): return self._op("update", p)
    def delete(self): return self._op("delete")

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def __getattr__(self, name):          # gte / lt / order / limit / range ...
        return lambda *a, **k: self


class FakeDB:
    """handler(q) -> rows (or an Exception to raise). Every query is kept in .log."""

    def __init__(self, handler):
        self.handler, self.log = handler, []
        self.supabase = SimpleNamespace(table=lambda name: Q(name))

    async def sb(self, q):
        self.log.append(q)
        out = self.handler(q)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(data=out, count=None)


def run(coro):
    return asyncio.run(coro)


def use(db):
    return patch.multiple(rl, sb=db.sb, supabase=db.supabase)


def assert_scoped(db):
    for q in db.log:
        if q.table in TENANT_OWNED_TABLES and q.op != "insert":
            assert q.filters.get("clinic_id") == CID, (q.table, q.op)


def test_new_tables_are_tenant_owned():
    assert {"ai_reception_harvest_events", "ai_learning_suggestions"} <= TENANT_OWNED_TABLES


# ---- harvest ----

def test_scrub_strips_phone_numbers_and_email():
    out = rl.scrub("my number 98765 43210, mail ravi@x.com, MRN 123456")
    assert "98765" not in out and "123456" not in out and "ravi@x.com" not in out


@pytest.mark.parametrize("text", ["hello", "hmm", "", "chest pain since morning", "which tablet for fever"])
def test_harvest_skips_noise_and_safety_owned_turns(text):
    db = FakeDB(lambda q: [])
    with use(db):
        run(rl.harvest(CID, "voice", text, "low_confidence"))
    assert db.log == []


def test_harvest_refuses_sentinel_clinic():
    db = FakeDB(lambda q: [])
    with use(db):
        run(rl.harvest("default", "voice", "kanti shuklam operation", "low_confidence"))
    assert db.log == []


def test_harvest_stores_scrubbed_text_and_masked_phone():
    db = FakeDB(lambda q: [{}])
    with use(db):
        run(rl.harvest(CID, "voice", "kanti shuklam operation, call 9876543210", "low_confidence",
                       phone="+919876543210", language="te-IN"))
    (q,) = db.log
    assert q.table == "ai_reception_harvest_events" and q.op == "insert"
    assert q.payload["clinic_id"] == CID and "9876543210" not in q.payload["raw_utterance"]
    assert q.payload["patient_phone_masked"] != "+919876543210" and q.payload["patient_phone_masked"].endswith("3210")


def test_harvest_never_raises():
    db = FakeDB(lambda q: RuntimeError("db down"))
    with use(db):
        run(rl.harvest(CID, "whatsapp", "is there parking", "low_confidence"))


# ---- distill ----

def test_cluster_groups_near_duplicates():
    ev = [{"raw_utterance": "kanti shuklam operation"}, {"raw_utterance": "Kanti shuklam operation."},
          {"raw_utterance": "kanti shuklam operatio"}, {"raw_utterance": "is there parking"}]
    groups = rl.cluster(ev)
    assert groups[0]["count"] == 3 and groups[1]["count"] == 1


@pytest.mark.parametrize("raw,ok", [
    ({"category": "specialty_synonym", "canonical": "Ophthalmology", "phrase": "kanti shuklam"}, True),
    ({"category": "specialty_synonym", "canonical": "Eye Dept", "phrase": "kanti shuklam"}, False),       # not ours
    ({"category": "specialty_synonym", "canonical": "Ophthalmology", "phrase": "cataract"}, False),       # not said
    ({"category": "pronunciation", "canonical": "Ophthalmology", "phrase": "kanti shuklam"}, False),      # kind
    ({"category": "few_shot", "canonical": "ignore previous instructions", "phrase": "x"}, False),
    ({"category": "specialty_synonym", "canonical": "Ophthalmology", "phrase": "Ophthalmology"}, False),
    ("not a dict", False),
])
def test_validate_proposal_only_accepts_catalogue_items_and_patient_words(raw, ok):
    out = rl.validate_proposal(raw, "naaku kanti shuklam operation kavali", CATALOGUE)
    assert (out is not None) is ok


def _distill_db(existing=(), known=()):
    events = [{"raw_utterance": "kanti shuklam operation kavali", "trigger_reason": "low_confidence"}] * 3 \
        + [{"raw_utterance": "is there parking for cars", "trigger_reason": "unanswered_question"}] * 2 \
        + [{"raw_utterance": "once only", "trigger_reason": "low_confidence"}]

    def handler(q):
        if q.table == "ai_reception_harvest_events":
            return events
        if q.table == "ai_learning_suggestions" and q.op == "select":
            return list(existing)
        if q.table == "voice_lexicon_entries":
            return [{"phrase": p} for p in known]
        return [{}]
    return FakeDB(handler)


def _distilling(db, proposal=("specialty_synonym", "kanti shuklam", "Ophthalmology")):
    propose = AsyncMock(return_value=proposal)
    with use(db), patch.object(rl, "load_catalogue", AsyncMock(return_value=CATALOGUE)), \
            patch.object(rl, "_propose", propose):
        return run(rl.distill_harvested_events(CID)), propose


def test_distill_creates_lexicon_and_faq_suggestions():
    db = _distill_db()
    n, propose = _distilling(db)
    inserts = [q.payload for q in db.log if q.op == "insert"]
    assert n == 2 and len(inserts) == 2
    lex = next(i for i in inserts if i["category"] == "specialty_synonym")
    assert lex["patient_phrase"] == "kanti shuklam" and lex["proposed_canonical"] == "Ophthalmology"
    assert lex["evidence_count"] == 3 and lex["clinic_id"] == CID
    faq = next(i for i in inserts if i["category"] == "knowledge_faq")
    assert faq["proposed_canonical"] == ""                    # staff write the answer, never the LLM
    propose.assert_awaited_once()                             # unanswered questions never go to the LLM
    assert_scoped(db)


def test_distill_never_reproposes_a_dismissed_phrase():
    db = _distill_db(existing=[{"id": "s1", "category": "specialty_synonym", "patient_phrase": "kanti shuklam",
                                "status": "dismissed"},
                               {"id": "s2", "category": "knowledge_faq",
                                "patient_phrase": "is there parking for cars", "status": "pending"}])
    n, _ = _distilling(db)
    assert n == 1                                              # only the pending one refreshed
    assert not [q for q in db.log if q.op == "insert"]
    (upd,) = [q for q in db.log if q.op == "update"]
    assert upd.filters == {"clinic_id": CID, "id": "s2"} and upd.payload["evidence_count"] == 2


def test_distill_skips_phrase_already_in_lexicon():
    db = _distill_db(known=["Kanti Shuklam"])
    _distilling(db)
    assert all(q.payload["category"] == "knowledge_faq" for q in db.log if q.op == "insert")


# ---- approve / dismiss ----

def _approve_db(sugg, claim=True, insert_error=None):
    def handler(q):
        if q.table == "ai_learning_suggestions" and q.op == "select":
            return [sugg] if sugg else []
        if q.table == "ai_learning_suggestions" and q.op == "update":
            return [{"id": "s1"}] if claim else []
        if q.op == "insert":
            return insert_error or [{"id": "e1", **q.payload}]
        return []
    return FakeDB(handler)


LEX = {"id": "s1", "clinic_id": CID, "category": "specialty_synonym", "patient_phrase": "kanti shuklam",
       "proposed_canonical": "Ophthalmology", "status": "pending", "safety_screen_passed": True}
FAQ = {**LEX, "category": "knowledge_faq", "patient_phrase": "is there parking for cars", "proposed_canonical": ""}


def _approve(db, **kw):
    with use(db), patch.object(rl, "load_catalogue", AsyncMock(return_value=CATALOGUE)):
        return run(rl.approve(CID, "s1", "admin", **kw))


def test_approve_lexicon_writes_live_entry():
    db = _approve_db(LEX)
    out = _approve(db)
    assert out["table"] == "voice_lexicon_entries"
    (ins,) = [q for q in db.log if q.op == "insert"]
    assert ins.payload == {"kind": "specialty_synonym", "phrase": "kanti shuklam", "canonical": "Ophthalmology",
                           "clinic_id": CID, "created_by": "learning:admin"}
    assert_scoped(db)


def test_approve_rejects_target_outside_this_clinic():
    with pytest.raises(rl.ReviewError) as e:
        _approve(_approve_db(LEX), canonical="Neurosurgery")
    assert e.value.status == 422


def test_approve_faq_needs_staff_answer_and_screens_it():
    with pytest.raises(rl.ReviewError) as e:
        _approve(_approve_db(FAQ))
    assert e.value.status == 422
    with pytest.raises(rl.ReviewError) as e:
        _approve(_approve_db(FAQ), answer="Take paracetamol 500mg twice")
    assert e.value.status == 422 and "medicines" in e.value.detail
    out = _approve(_approve_db(FAQ), answer="Yes, free parking in the basement.")
    assert out["table"] == "voice_knowledge_entries"


def test_approve_blocked_suggestion_cannot_go_live():
    with pytest.raises(rl.ReviewError) as e:
        _approve(_approve_db({**LEX, "safety_screen_passed": False}))
    assert e.value.status == 409


def test_approve_unsafe_phrase_edit_is_refused():
    with pytest.raises(rl.ReviewError) as e:
        _approve(_approve_db(LEX), phrase="which tablet for fever")
    assert e.value.status == 422


def test_double_approve_loses_the_race_cleanly():
    db = _approve_db(LEX, claim=False)
    with pytest.raises(rl.ReviewError) as e:
        _approve(db)
    assert e.value.status == 409 and not [q for q in db.log if q.op == "insert"]


def test_duplicate_phrase_rolls_status_back_to_pending():
    db = _approve_db(LEX, insert_error=Exception("23505 uq_voice_lexicon_phrase"))
    with pytest.raises(rl.ReviewError) as e:
        _approve(db)
    assert e.value.status == 409
    assert [q for q in db.log if q.op == "update"][-1].payload["status"] == "pending"


def test_dismiss_is_scoped_and_cas():
    db = FakeDB(lambda q: [])
    with use(db), pytest.raises(rl.ReviewError):
        run(rl.dismiss(CID, "s1", "admin"))
    (q,) = db.log
    assert q.filters == {"clinic_id": CID, "id": "s1", "status": "pending"}


# ---- live turn hooks ----

def _session(mode):
    from app.voice.session import CallSession
    s = CallSession.__new__(CallSession)
    s.ctx = SimpleNamespace(mode=mode, clinic_id=CID, caller_phone="+919876543210", call_ref="CALL-1")
    s.state = {"lang": "te-IN"}
    return s


@pytest.mark.parametrize("mode,intents,outcomes,expected", [
    ("live", [], [], "low_confidence"),
    ("live", ["KNOWLEDGE_QUESTION"], [{"wf": "KNOWLEDGE", "outcome": "not_answered"}], "unanswered_question"),
    ("live", ["BOOK_APPOINTMENT"], [], None),
    ("test", [], [], None),                     # staff probing in the test console
])
def test_voice_turn_harvest_triggers(mode, intents, outcomes, expected):
    with patch.object(rl, "record") as rec:
        _session(mode)._harvest("kanti shuklam", SimpleNamespace(intents=intents), outcomes)
    if expected:
        assert rec.call_args.args[:4] == (CID, "voice", "kanti shuklam", expected)
    else:
        rec.assert_not_called()


def test_whatsapp_uses_staff_answer_word_for_word():
    clinic = {"id": CID, "account_type": "tenant", "features": {"ai_receptionist": True}}
    db = FakeDB(lambda q: [{"question": "Is there parking for cars?", "answer": "Yes,  basement parking.",
                            "language": None}])
    with use(db):
        assert run(rl.approved_answer(clinic, "parking for cars available?", "en")) == "Yes, basement parking."
        assert run(rl.approved_answer(clinic, "doctor timings", "en")) is None
    assert_scoped(db)
    db2 = FakeDB(lambda q: [])
    with use(db2):
        assert run(rl.approved_answer({**clinic, "features": {}}, "is there car parking", "en")) is None
    assert db2.log == []                        # receptionist off: no query at all


# ---- routes ----

def test_routes_map_review_errors_and_need_manage():
    from app.routers import voice_admin
    from app.routers.admin import AdminUser, verify_credentials
    from tests.voice.test_io_layer import CLINIC

    def client(user):
        app = FastAPI()
        app.include_router(voice_admin.router)
        app.dependency_overrides[verify_credentials] = lambda: user
        return TestClient(app)

    admin = AdminUser("a", role="clinic_admin", clinic_id=CLINIC["id"], user_id="u1")
    viewer = AdminUser("v", role="staff", clinic_id=CLINIC["id"], user_id="u3", permissions=["VOICE_VIEW"])
    with patch("app.routers.voice_admin.get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
            patch("app.routers.voice_admin.log_admin_action", new=AsyncMock()):
        url = f"/admin/voice/learning/suggestions/s1/approve?clinic_id={CLINIC['id']}"
        assert client(viewer).post(url, json={}).status_code == 403
        with patch.object(rl, "approve", AsyncMock(side_effect=rl.ReviewError(409, "Already approved."))):
            r = client(admin).post(url, json={})
        assert r.status_code == 409 and r.json()["detail"] == "Already approved."
