"""Kriya Ketu greetings, thanks, "help" / "what can you do", and the "can't do that" fallback.

Found in a probe of unseen staff requests: "hi", "help", "what can you do" and "thanks" all ended at
"I can't do that yet. Try "open appointments" or "go to leads"." Now: a greeting or help request lists
what THIS user can actually do here (their permitted actions only); thanks gets a short reply; the
fallback names real examples. Deterministic, zero tokens; nothing is executed."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import assistant as assistant_router
from app.assistant.registry import REGISTRY
from tests.test_assistant_runs import _user, as_user, db  # noqa: F401  (fixtures)

client = TestClient(app)


def _ask(text):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "ui_context": {"page": "dashboard"}}).json()


def test_every_example_names_a_real_action():
    assert {a for a, _ in assistant_router.EXAMPLES} <= set(REGISTRY)


@pytest.mark.parametrize("text", ["hi", "Hello!", "hey kriya", "namaste", "help", "help please",
                                  "what can you do?", "who are you", "Hi Ketu, what can you do"])
def test_greeting_or_help_lists_what_this_user_can_do(db, as_user, text):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    out = _ask(text)
    assert (out["state"], out["action_id"]) == ("COMPLETED_VERIFIED", "help.howto"), out
    assert "show today's appointments" in out["message"] and "how do I" in out["message"], out
    assert out["cards"][0]["type"] == "help"


def test_examples_are_only_actions_the_user_may_run(db, as_user):  # noqa: F811
    as_user["user"] = _user()              # front-desk staff with no extra permissions
    msg = _ask("what can you do")["message"]
    assert "on leave" not in msg and "add a new doctor" not in msg, msg


@pytest.mark.parametrize("text", ["thanks", "Thank you!", "ok thanks", "dhanyavad"])
def test_thanks(db, as_user, text):  # noqa: F811
    out = _ask(text)
    assert (out["state"], out["message"]) == ("COMPLETED_VERIFIED", "You're welcome."), out


def test_a_real_request_is_not_small_talk(db, as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    assert _ask("good morning")["action_id"] == "briefing.morning"   # the existing briefing trigger
    assert _ask("hi, open leads")["action_id"] == "nav.open"            # the greeting is dropped
    assert _ask("Hello Kriya, show today's appointments")["action_id"] == "appointments.search"
    assert "Hi, I'm Kriya Ketu" not in _ask("help me add a leave")["message"]


def test_unsupported_request_names_real_examples(db, as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    out = _ask("do a cartwheel")
    assert out["state"] == "REJECTED" and out["message"].startswith("I can't do that yet."), out
    assert "show today's appointments" in out["message"], out
