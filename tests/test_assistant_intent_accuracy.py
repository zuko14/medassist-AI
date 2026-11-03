"""Kriya Ketu intent accuracy gate (session 53). Corpus: tests/ketu_corpus.py; report: scripts/assistant_intent_eval.py.

Every sentence is routed as /admin/assistant routes it (rules, then the router's tie step with every module on).
Zero tokens, deterministic. Two promises are enforced:
  * WRONG = 0 on every set: Kriya never picks a different task than the one asked for (asking or not
    understanding is allowed; a rule miss goes to the LLM fallback on the server);
  * accuracy floors, measured on 2026-11-01: DEV 100 %, DEV2 95 %, DEV3 100 %, HELDOUT 95.5 % (blind, before its one
    wrong action was fixed). The floors sit just under those numbers so a regression fails here.
"""

from datetime import datetime
from functools import lru_cache
from unittest.mock import AsyncMock, patch

import pytest

from app.assistant import llm, resolve, rules
from app.assistant.registry import REGISTRY, _trigger_key
from app.voice.dates import IST
from tests import ketu_corpus
from tests.test_assistant_runs import as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=IST)
FLOORS = {"DEV": 0.99, "DEV2": 0.92, "DEV3": 0.99, "DEV4": 1.0, "DEV5": 1.0, "DEV6": 1.0, "HELDOUT": 0.95}


def _outcome(text: str) -> str:
    docs = ketu_corpus.DOCTORS if rules.wants_doctors(text) else None
    r = rules.route(text, doctors=docs, now=NOW)
    if r.kind == "clarify" and r.candidate_actions:
        live = rules.resolve_tie(text, r.candidate_actions)
        if len(live) == 1:
            r = rules.fill(REGISTRY[live[0]], text, doctors=docs, now=NOW, question=rules.asks(text))
    if r.kind == "nav":
        return f"nav:{r.page}"
    if r.kind == "help":
        return "chat" if r.error_code in rules.SMALL_TALK else "help"
    if r.kind == "rejected":
        return f"reject:{r.error_code}"
    if r.kind == "clarify" and r.candidate_actions:
        return "ask:" + "|".join(r.candidate_actions)
    return r.action_id if r.kind in ("action", "clarify") else "none"


@lru_cache(maxsize=None)
def _score(name: str):
    correct, wrong = 0, []
    for expected, text in getattr(ketu_corpus, name):
        want = expected if isinstance(expected, tuple) else (expected,)
        got = _outcome(text)
        if got in want:
            correct += 1
        elif not (got == "none" or got.startswith("ask:")):
            wrong.append((text, want[0], got))
    return correct / len(getattr(ketu_corpus, name)), wrong


@pytest.mark.parametrize("name", sorted(FLOORS))
def test_never_a_wrong_action(name):
    _, wrong = _score(name)
    assert wrong == [], wrong


@pytest.mark.parametrize("name", sorted(FLOORS))
def test_accuracy_floor(name):
    accuracy, _ = _score(name)
    assert accuracy >= FLOORS[name], f"{name}: {accuracy:.1%} < {FLOORS[name]:.0%}"


def test_corpus_sizes_and_disjoint_heldout():
    sets = {n: [t for _, t in getattr(ketu_corpus, n)] for n in FLOORS}
    assert {n: len(v) for n, v in sets.items()} == {"DEV": 231, "DEV2": 82, "DEV3": 185, "DEV4": 24, "DEV5": 23, "DEV6": 24,
                                                  "HELDOUT": 132}
    tuned = set(sets["DEV"]) | set(sets["DEV2"]) | set(sets["DEV3"]) | set(sets["DEV4"]) | set(sets["DEV5"]) | set(sets["DEV6"])
    assert not set(sets["HELDOUT"]) & tuned  # the blind set shares no sentence with the tuning sets


# ── shorthand never changes the patient's text ───────────────────────────────

@pytest.mark.parametrize("text, out", [
    ("cancel ravi appt tmrw pls", "cancel ravi appointment tomorrow please"),
    ("message Ravi \"pls come tmrw\"", "message Ravi \"pls come tmrw\""),            # quoted: untouched
    ("tell Ravi on whatsapp: pls come tmrw", "tell Ravi on whatsapp: pls come tmrw"),  # after a colon: untouched
    ("Dr Anitha leave tmrw", "Dr Anitha leave tomorrow"),
    ("tmrwx appt's", "tmrwx appt's"),                                                # whole words only
])
def test_shorthand(text, out):
    assert resolve.expand_shorthand(text) == out


def test_a_messages_text_is_sent_as_typed():
    r = rules.route("message Ravi \"pls come tmrw at 10\"", now=NOW)
    assert r.action_id == "lead.message" and r.params.get("message") == "pls come tmrw at 10"


# ── absence is a leave only for a doctor on a stated day ─────────────────────

@pytest.mark.parametrize("text, leave", [
    ("kal Dr Kiran nahi aayenge", True), ("Dr Suresh Kumar will not come tomorrow", True),
    ("Dr Meera won't be available tomorrow", True), ("Dr Priya is on vacation from 10 to 12 November", True),
    ("Ravi will not come tomorrow", False),            # a patient: no doctor
    ("Dr Anitha is not available", False),            # no day said
])
def test_absence_as_leave(text, leave):
    assert (rules.route(text, doctors=ketu_corpus.DOCTORS, now=NOW).action_id == "doctor_leave.create") is leave


# ── the safety screens still hold ────────────────────────────────────────────

@pytest.mark.parametrize("text", ["Dr Rao is not on leave tomorrow", "don't cancel Ravi's appointment",
                                  "do not send the report", "Ravi ka appointment cancel mat karo",
                                  "please don't open the staff page", "don't call the leads today"])
def test_negations_are_still_refused(text):
    assert rules.route(text, doctors=ketu_corpus.DOCTORS, now=NOW).error_code == "negated"


@pytest.mark.parametrize("text, action", [
    ("Dr Meera is not on leave tomorrow, remove it", "doctor_leave.delete"),
    ("we don't do ESR anymore, stop the test", "lab_test.deactivate"),
])
def test_a_correction_with_its_own_verb_is_not_a_refusal(text, action):
    assert rules.route(text, doctors=ketu_corpus.DOCTORS, now=NOW).action_id == action


@pytest.mark.parametrize("text", ["cancel all appointments", "cancel today's appointments", "send the report to Ravi",
                                  "send messages remaining", "Not reached", "Patient agrees to WhatsApp messages"])
def test_deliberate_non_routes_stay_unrouted(text):
    assert rules.route(text, doctors=ketu_corpus.DOCTORS, now=NOW).kind == "none"


# ── tie step, patterns, triggers ─────────────────────────────────────────────

@pytest.mark.parametrize("text, cands, out", [
    ("change CBC price to 350", ["lab_test.price", "treatment.price"], ["lab_test.price"]),
    ("change botox price to 9000", ["lab_test.price", "treatment.price"], ["treatment.price"]),
    ("change Gold Plan price to 5000", ["lab_test.price", "treatment.price"], ["lab_test.price", "treatment.price"]),
    ("change CBC price to 350", ["treatment.price"], ["treatment.price"]),          # only what is switched on
    ("change the x-ray price to 400", ["lab_test.price", "services.price"], ["lab_test.price", "services.price"]),
    ("BP 120/80", ["opd.vitals", "ipd.vitals"], ["opd.vitals", "ipd.vitals"]),     # not a price tie: untouched
])
def test_resolve_tie(text, cands, out):
    assert rules.resolve_tie(text, cands) == out


def test_pattern_triggers_need_their_shape():
    assert rules._hits("dr kiran evenings 6 to 9", REGISTRY["doctor.schedule"].triggers[-1]) or any(
        rules._hits("dr kiran evenings 6 to 9", t) for t in REGISTRY["doctor.schedule"].triggers if t.startswith("re:"))
    assert not any(rules._hits("assign dr kiran to the madhapur branch mornings", t)
                   for t in REGISTRY["doctor.schedule"].triggers if t.startswith("re:"))
    assert rules._weight("re:anything") == rules._PATTERN_WEIGHT and rules._weight("cancel+leave") == 2


def test_an_added_trigger_never_duplicates_one_already_there():
    """ "check-in" is "check in" once normalised: added again it would count twice and outweigh other actions."""
    from dataclasses import replace
    from app.assistant import registry
    spec = replace(REGISTRY["appointment.check_in"], id="appointment.check_in")
    with patch.dict(registry.PHRASINGS, {"appointment.check_in": {"triggers": ("check-in", "Check In", "is here")}}):
        out = registry._phrased(replace(spec, triggers=("check in",), unless=(), keep=()))
    assert out.triggers == ("check in", "is here")
    assert _trigger_key("pre-auth+Approved") == _trigger_key("pre auth+approved")


# ── the LLM fallback sees how staff talk ─────────────────────────────────────

def test_llm_prompt_has_examples_and_language_rules():
    p = llm._system_prompt(["doctor_leave.create", "appointment.cancel", "doctor.schedule"], ["leaves"])
    assert '- doctor_leave.create: Add doctor leave (e.g. "on leave"; "is on leave")' in p
    assert '"cancel … appointment"' in p and "re:" not in p
    assert "Hinglish" in p and "nahi aayenge" in p and "help.howto" in p and "doctor.create" in p


@pytest.mark.asyncio
async def test_llm_is_told_the_open_page():
    answer = '{"kind": "unsupported", "action_id": null, "slots": {}, "question_for_user": null}'
    gw = AsyncMock(return_value={"choices": [{"message": {"content": answer}}], "usage": {"total_tokens": 10}})
    with patch.object(llm, "call_ai_gateway", gw), patch.object(llm.settings, "assistant_llm_enabled", True), \
         patch.object(llm, "_rate_limited", AsyncMock(return_value=False)):
        await llm.classify("please do the needful for the leads", clinic_id="c1", username="u",
                           action_ids=["leads.list"], ui_page="leads")
    assert gw.await_args.args[0][1]["content"].startswith("Open page: leads (WhatsApp Leads)\n")


# ── "how do I ..." about something Kriya itself does ─────────────────────────

def test_how_to_for_a_task_kriya_does_explains_it_and_changes_nothing(db, as_user):  # noqa: F811
    from tests.test_assistant_runs import _post
    out = _post("how do I cancel an appointment").json()
    assert (out["state"], out["action_id"]) == ("COMPLETED_VERIFIED", "help.howto"), out
    assert "Cancel a booking: I can do this for you" in out["message"] and "Appointments page" in out["message"]
    assert db.tables["assistant_runs"][0]["state"] == "COMPLETED_VERIFIED"  # an answer, never a preview or a write
