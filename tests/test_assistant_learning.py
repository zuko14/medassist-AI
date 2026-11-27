"""Kriya Ketu learning (migration 159): confirmed LLM rescues -> admin-approved phrases -> zero-token routing.

The guarantees under test: a learned phrase is tried only when the built-in rules found nothing, after the clinical /
negation / hypothetical gates; only a current suggestion can be approved; suggestions never carry digits, doctor
names or refusal words; clinic admins only; one clinic's phrases never route another clinic's requests."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.assistant import learning, rules
from app.config import settings
from tests.test_ipd_api_e2e import _clinic, _doctor, _q, api  # noqa: F401  (fixture)

DOCS = [{"id": "d1", "name": "Dr. Suresh Kumar"}, {"id": "d2", "name": "Dr Kiran Reddy"},
        {"id": "d3", "name": "Dr. Anitha Rao"}]
DOC_WORDS = {"suresh", "kumar", "kiran", "reddy", "anitha", "rao"}
LEAVE = ["Dr Suresh wont come on Friday, block his schedule", "block his schedule for Dr Kiran tomorrow",
         "please block his schedule Dr Suresh 5th december"]
TAUGHT = [("block his schedule", "doctor_leave.create")]


def _rows(texts, action="doctor_leave.create"):
    return [{"input_text": t, "action_id": action} for t in texts]


# ── distill (pure) ──────────────────────────────────────────────────────────────

def test_three_confirmed_instructions_become_one_suggestion():
    assert learning.suggest(_rows(LEAVE), set(), DOC_WORDS) == [
        {"phrase": "block his schedule", "action_id": "doctor_leave.create", "evidence": 3}]


def test_below_the_evidence_threshold_nothing_is_suggested():
    assert learning.suggest(_rows(LEAVE[:2]), set(), DOC_WORDS) == []


def test_a_decided_phrase_is_never_suggested_again():
    assert learning.suggest(_rows(LEAVE), {("block his schedule", "doctor_leave.create")}, DOC_WORDS) == []


def test_suggestions_hold_no_digits_doctor_names_or_refusal_words():
    rows = _rows(["dont block 12 slots of Dr Suresh", "dont block 12 slots of Dr Suresh today",
                  "dont block 12 slots of Dr Suresh now"])
    for s in learning.suggest(rows, set(), DOC_WORDS):
        assert not any(c.isdigit() for c in s["phrase"]) and "suresh" not in s["phrase"]
        assert "dont" not in s["phrase"]


def test_a_phrase_the_rules_already_know_is_not_suggested():
    assert learning.suggest(_rows(["open leads now", "open leads please", "open leads quickly"], "nav.open"),
                            set(), DOC_WORDS) == []


def test_unknown_actions_are_ignored():
    assert learning.suggest(_rows(LEAVE, "not.an_action"), set(), DOC_WORDS) == []


# ── routing ──────────────────────────────────────────────────────────────────────

def test_a_learned_phrase_routes_a_new_sentence_with_its_slots():
    r = rules.route("Dr Anitha cannot come Monday, block his schedule", doctors=DOCS, learned=TAUGHT)
    assert (r.kind, r.action_id, r.params["doctor"]) == ("action", "doctor_leave.create", "Dr. Anitha Rao")


def test_without_learning_the_same_sentence_is_not_understood():
    assert rules.route("block his schedule for Dr Kiran tomorrow", doctors=DOCS).kind == "none"


def test_built_in_rules_always_win_over_a_learned_phrase():
    wrong = [("open leads", "doctor_leave.create")]
    assert rules.route("open leads", doctors=DOCS, learned=wrong).page == "leads"


@pytest.mark.parametrize("text,code", [("don't block his schedule for Dr Kiran", "negated"),
                                       ("what if I block his schedule for Dr Kiran", "hypothetical"),
                                       ("block his schedule, which medicine should he take", "clinical")])
def test_safety_gates_run_before_learned_phrases(text, code):
    r = rules.route(text, doctors=DOCS, learned=TAUGHT)
    assert r.kind == "rejected", r


def test_a_learned_action_the_user_may_not_run_is_never_offered():
    r = rules.route("block his schedule for Dr Kiran tomorrow", doctors=DOCS, learned=TAUGHT,
                    actions={"nav.open": rules.REGISTRY["nav.open"]})
    assert r.kind == "none"


# ── end to end on real Postgres ─────────────────────────────────────────────────

def _run(conn, clinic, text, action="doctor_leave.create", layer="llm", state="COMPLETED_VERIFIED"):
    _q(conn, """INSERT INTO assistant_runs (clinic_id, username, role, client_request_id, input_mode, input_text,
                route_layer, action_id, state) VALUES (%s, 'desk', 'staff', %s, 'text', %s, %s, %s, %s)
                RETURNING id""", clinic, str(uuid.uuid4()), text, layer, action, state)


@pytest.fixture
def ketu(api):  # noqa: F811
    with patch.object(settings, "assistant_enabled", True), patch.object(settings, "assistant_llm_enabled", False), \
         patch("app.routers.assistant._rate_limited", AsyncMock(return_value=False)):
        yield api


def _setup(api, name="Dr Kiran Reddy"):  # noqa: F811
    clinic = _clinic(api.conn)
    _q(api.conn, "UPDATE clinics SET features = COALESCE(features, '{}'::jsonb) || '{\"kriya_ketu\": true}' "
                 "WHERE id = %s RETURNING id", clinic)
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    doctor = _doctor(api.conn, clinic)
    _q(api.conn, "UPDATE doctors SET name = %s WHERE id = %s RETURNING id", name, doctor)
    return clinic, api.U("admin", "clinic_admin", clinic, str(uuid.uuid4()))


def test_full_loop_suggest_approve_route_forget(ketu):  # noqa: F811
    clinic, admin = _setup(ketu)
    for t in LEAVE:
        _run(ketu.conn, clinic, t)
    _run(ketu.conn, clinic, "block his schedule for Dr Kiran today", state="CANCELLED")   # not confirmed: no evidence
    _run(ketu.conn, clinic, "block his schedule Dr Kiran", layer="rules")                  # rules already knew it
    ketu.as_(admin)
    listed = ketu.call("GET", "/admin/assistant/learning", clinic).json()
    assert [(s["phrase"], s["action_id"], s["evidence"]) for s in listed["suggestions"]] == \
        [("block his schedule", "doctor_leave.create", 3)]

    body = {"phrase": "block his schedule", "action_id": "doctor_leave.create"}
    assert ketu.call("POST", "/admin/assistant/learning/approve", clinic,
                     {**body, "phrase": "open leads"}).status_code == 404          # not a suggestion: refused
    assert ketu.call("POST", "/admin/assistant/learning/approve", clinic, body).status_code == 200
    listed = ketu.call("GET", "/admin/assistant/learning", clinic).json()
    assert listed["suggestions"] == [] and [r["phrase"] for r in listed["learned"]] == ["block his schedule"]

    out = ketu.call("POST", "/admin/assistant/requests", clinic, {
        "client_request_id": str(uuid.uuid4()), "text": "Dr Kiran is travelling, block his schedule tomorrow",
        "ui_context": {"page": "dashboard"}}).json()
    assert out["action_id"] == "doctor_leave.create", out
    assert _q(ketu.conn, "SELECT route_layer FROM assistant_runs WHERE id = %s", out["run_id"]) == "rules"

    learned_id = listed["learned"][0]["id"]
    assert ketu.call("DELETE", f"/admin/assistant/learning/{learned_id}", clinic).status_code == 200
    assert ketu.call("GET", "/admin/assistant/learning", clinic).json()["learned"] == []


def test_dismissed_is_never_suggested_again(ketu):  # noqa: F811
    clinic, admin = _setup(ketu)
    for t in LEAVE:
        _run(ketu.conn, clinic, t)
    ketu.as_(admin)
    assert ketu.call("POST", "/admin/assistant/learning/dismiss", clinic,
                     {"phrase": "block his schedule", "action_id": "doctor_leave.create"}).status_code == 200
    listed = ketu.call("GET", "/admin/assistant/learning", clinic).json()
    assert listed["suggestions"] == [] and listed["learned"] == []


def test_admins_only_and_tenants_isolated(ketu):  # noqa: F811
    clinic, admin = _setup(ketu)
    other, other_admin = _setup(ketu)
    for t in LEAVE:
        _run(ketu.conn, clinic, t)
    ketu.as_(ketu.U("desk", "staff", clinic, str(uuid.uuid4())))
    assert ketu.call("GET", "/admin/assistant/learning", clinic).status_code == 403
    ketu.as_(admin)
    assert ketu.call("POST", "/admin/assistant/learning/approve", clinic,
                     {"phrase": "block his schedule", "action_id": "doctor_leave.create"}).status_code == 200
    ketu.as_(other_admin)
    assert ketu.call("GET", "/admin/assistant/learning", other).json() == \
        {"min_evidence": learning.MIN_EVIDENCE, "suggestions": [], "learned": []}
    assert ketu.call("GET", "/admin/assistant/learning", clinic).status_code == 403   # another clinic's list
