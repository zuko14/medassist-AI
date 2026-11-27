"""Kriya Ketu router: phrasings from an unseen adversarial probe that routed to the wrong action. Each one is a
refusal or a how-to answer now; the medicine-advice screen still blocks real medicine questions."""
from datetime import datetime

import pytest

from app.assistant import rules
from app.voice.dates import IST

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=IST)
DOCS = [{"id": "d-kiran", "name": "Dr Kiran Reddy"}, {"id": "d-anitha", "name": "Dr. Anitha Rao"}]


def _r(text):
    return rules.route(text, doctors=DOCS, now=NOW)


@pytest.mark.parametrize("text", ["no need to refund him", "don't need to cancel Ravi's booking",
                                  "need not put Dr Kiran on leave"])
def test_no_need_to_is_a_refusal(text):
    assert (_r(text).kind, _r(text).error_code) == ("rejected", "negated")


@pytest.mark.parametrize("text", ["if I mark Dr Kiran on leave will patients get a message",
                                  "if we cancel tomorrow's bookings would patients be informed",
                                  "when I put Dr Anitha on leave does the patient get notified?"])
def test_if_i_consequence_questions_are_hypothetical(text):
    assert (_r(text).kind, _r(text).error_code) == ("rejected", "hypothetical")


@pytest.mark.parametrize("text", ["steps to create a new staff login", "where do I change the consultation fee",
                                  "where can I add a doctor leave"])
def test_how_to_phrasings_never_prepare_a_change(text):
    r = _r(text)
    assert r.kind in ("help", "rejected") and r.kind != "action", r


def test_a_refund_question_is_not_medicine_advice():
    assert _r("how can I give a refund to a patient").error_code != "medicine_advice"


@pytest.mark.parametrize("text", ["can I give paracetamol to a child", "can we give him augmentin instead"])
def test_real_medicine_questions_stay_blocked(text):
    assert _r(text).kind == "rejected"


@pytest.mark.parametrize("text,action", [("put Dr Kiran on leave tomorrow", "doctor_leave.create"),
                                         ("if Dr Kiran is free book Ravi", None)])
def test_ordinary_instructions_unchanged(text, action):
    r = _r(text)
    if action:
        assert r.action_id == action
    else:
        assert r.error_code != "hypothetical"   # a conditional instruction, not an "if I ... will" question
