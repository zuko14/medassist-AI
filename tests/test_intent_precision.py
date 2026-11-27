"""Keyword fast-paths answer from ANY state before the model, so a false hit is costly: an opt-out unsubscribes the
patient, an emergency replaces the answer with the emergency card. ASCII keywords now match whole words only (the
voice rule), emergencies also match plurals, and voice shares the emergency matcher."""
from unittest.mock import patch

import pytest

from app.services import ai_engine
from app.services.ai_engine import contains_emergency, is_opt_out, is_queue_status, keyword_intent_fallback
from app.voice.safety import screen

CLINIC = {"id": "c", "plan": "multispecialty", "features": {}}

NOT_FAST_PATH = [  # names typed at "patient name?" and sentences that only contain a trigger substring
    "Christopher", "Arjun Turner", "Stephen Stopford", "my pain has not stopped since yesterday",
    "headache stopped but fever continues", "what are the benefits of the full body checkup",
    "my son is studying, can we come after 5", "I accidentally booked twice, please cancel one",
    "is there a return policy for the payment", "what is the status of my blood report",
    "I am waiting for my report since 2 days", "can I bring my mother along, she is waiting at home",
]
FAST_PATH = [
    ("stop", "opt_out"), ("STOP", "opt_out"), ("unsubscribe", "opt_out"), ("please stop messaging me", "opt_out"),
    ("ఆపు", "opt_out"), ("my token number", "queue_status"), ("how many patients ahead of me", "queue_status"),
    ("queue status", "queue_status"), ("is my turn near", "queue_status"), ("how long is the waiting", "queue_status"),
    ("status", "queue_status"), ("టోకెన్ ఎంత", "queue_status"), ("my father is unconscious", "emergency"),
    ("accident on highway, bleeding a lot", "emergency"), ("he is having fits", "emergency"),
    ("my wife's water broke", "emergency"), ("child having convulsions", "emergency"),
    ("she is having seizures", "emergency"), ("delete my data", "data_deletion_request"),
]


async def _route(message):
    reached = {"llm": False}

    async def llm(*a, **k):
        reached["llm"] = True
        raise RuntimeError("model not called in this test")

    with patch.object(ai_engine, "call_openrouter_with_backoff", llm):
        got = await ai_engine.detect_intent(message, CLINIC)
    return "LLM" if reached["llm"] else got


@pytest.mark.asyncio
@pytest.mark.parametrize("message", NOT_FAST_PATH)
async def test_trigger_substrings_reach_the_model(message):
    assert await _route(message) == "LLM"


@pytest.mark.asyncio
@pytest.mark.parametrize("message,intent", FAST_PATH)
async def test_real_requests_still_take_the_fast_path(message, intent):
    assert await _route(message) == intent


@pytest.mark.parametrize("message", ["Christopher", "my pain has not stopped", "Arjun Turner", "Hitesh",
                                     "what are the benefits of this package"])
def test_offline_fallback_is_never_opt_out_queue_or_emergency(message):
    assert keyword_intent_fallback(message, CLINIC) not in ("opt_out", "queue_status", "emergency", "greeting")


def test_predicates():
    assert is_opt_out("Stop.") and is_opt_out("pls stop") and not is_opt_out("dont stop the medicine")
    assert is_queue_status("how many people ahead") and not is_queue_status("go ahead and book")
    assert contains_emergency("seizures") and not contains_emergency("benefits")


@pytest.mark.parametrize("text", ["child having convulsions", "he had two seizures", "she is having seizures",
                                  "he is convulsing", "multiple strokes"])
def test_voice_screen_catches_plural_emergencies(text):
    assert screen(text) == "emergency"


@pytest.mark.parametrize("text", ["what are the benefits", "my son is studying", "i accidentally pressed one"])
def test_voice_screen_ignores_substrings(text):
    assert screen(text) is None


@pytest.mark.parametrize("message,intent", [
    ("cancel my appointment", "cancel_appointment"), ("please cancel tomorrow's booking", "cancel_appointment"),
    ("appointment cancel karo", "cancel_appointment"), ("stop booking", "cancel_appointment"),
    ("reschedule my appointment to friday", "reschedule_appointment"), ("postpone my visit", "reschedule_appointment"),
    ("can I change my booking to evening", "reschedule_appointment"), ("remove my information", "data_deletion_request"),
    # old meanings kept
    ("review my report", "view_reports"), ("book a full body checkup", "book_appointment"),
    ("I want to book an appointment", "book_appointment"), ("send my blood test report", "view_reports"),
])
def test_offline_fallback_prefers_the_specific_verb(message, intent):
    """With the model down (or a message flagged as injection), "cancel my appointment" used to start a booking and
    "reschedule" read as doctor availability ("schedule")."""
    assert keyword_intent_fallback(message, CLINIC) == intent
