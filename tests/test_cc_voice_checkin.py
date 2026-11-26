"""Voice care check-in (ADR-CC-16 as revised: spoken yes / no + read-back, no DTMF; VERIFICATION D3, D10).

The `care` packet gets the care script, never the lead greeting; reminder, lead and inbound greetings are
byte-identical to `main`; only the due items are asked; a spoken number is stored only after it was read back and
confirmed; a "no" to the identity question ends the call; a confirmed low reading gets `hypo_now` spoken in the
call; care calls have their own `care_daily_cap` and never use the lead / callback `daily_cap`.
"""

import asyncio
import importlib.util
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.voice import outbound
from app.voice import responses as R
from app.voice.dialog import DialogEngine, new_state
from tests.voice.test_dialog import TODAY, Conv, FakeTools
from tests.voice.test_reminder_calls import REM

ITEMS = [{"id": "i1", "kind": "adherence", "time": "morning"},
         {"id": "i2", "kind": "reading", "measure": "fbg", "measure_label": "fasting sugar", "hypo_below": 70}]
CARE = {"enrollment_id": "e1", "first_name": "Lakshmi", "items": ITEMS}


def _main_dialog():
    """app/voice/dialog.py as it is on main, loaded as a sibling module of the live package."""
    src = subprocess.run(["git", "show", "main:app/voice/dialog.py"], capture_output=True, text=True, check=True).stdout
    spec = importlib.util.spec_from_loader("app.voice._dialog_main", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "app.voice"
    sys.modules[spec.name] = mod
    exec(compile(src, "main:app/voice/dialog.py", "exec"), mod.__dict__)
    return mod


@pytest.mark.parametrize("kw", [{"reminder": REM}, {"outbound": True, "interest": "cardiology"}, {"outbound": True},
                                {}, {"outbound": False, "name": "Ravi"}])
def test_reminder_lead_and_inbound_greetings_are_byte_identical_to_main(kw):
    old = _main_dialog()
    clinic = {"hospital": "ABC Hospitals", "assistant": "Kriya", "emergency": "040-12345678", "hold_minutes": 10}
    for lang in ("te-IN", "hi-IN", "en-IN"):
        a_state, b_state = new_state(lang), old.new_state(lang)
        a = DialogEngine(FakeTools(), dict(clinic)).greeting(a_state, **kw)
        b = old.DialogEngine(FakeTools(), dict(clinic)).greeting(b_state, **kw)
        assert [(s.key, s.params) for s in a.says] == [(s.key, s.params) for s in b.says]
        assert a.control == b.control and a_state == b_state


def _conv(lang="en-IN", care=CARE):
    c = Conv(FakeTools(appts=[]), lang=lang)
    return c, c.text(c.engine.greeting(c.state, care=care))


def test_care_packet_gets_the_care_script_never_the_lead_greeting():
    c, en = _conv()
    assert en == "Namaste, this is Kriya, the automated care assistant of ABC Hospitals. Am I speaking with Lakshmi?"
    assert "enquired" not in en and c.state["expect"] == "care_identity"
    for lang in ("te-IN", "hi-IN"):
        _, t = _conv(lang)
        assert "Lakshmi" in t and "{" not in t
    for key in ("greeting_care", "care_ask_identity", "care_wrong_person", "care_ask_adherence", "care_ask_reading",
                "care_readback", "care_hypo_now", "care_thanks"):
        for lang in ("te-IN", "hi-IN", "en-IN"):
            out = R.realize(key, lang, TODAY, {}, hospital="H", assistant="K", name="L", when="morning",
                            measure="sugar", number="1 4 2", emergency="112")
            assert out and "{" not in out, (key, lang)


def test_confirmed_answers_only_due_items_and_hypo_spoken_in_the_call():
    c, _ = _conv()
    assert c.say("Yes") == "Did you take your morning medicines today? Please say yes or no."
    assert c.say("Yes") == "Please tell me your fasting sugar reading."
    assert c.say("sixty five") == "Please tell me your fasting sugar reading."       # not a digit string: asked again
    assert c.say("six five") == "I heard 6 5. Is that correct?"
    end = c.say("Yes")
    assert end.startswith("Your sugar is low. Please take three teaspoons of sugar") and end.endswith(
        "Your answers are with your care team.")
    assert c.control == "end"
    out = c.state["outcomes"][-1]
    assert out["outcome"] == "care_answered" and out["answers"] == [
        {"item": "i1", "kind": "adherence", "value": "taken"},
        {"item": "i2", "kind": "reading", "measure": "fbg", "value": "65", "source": "voice_speech_confirmed"}]


def test_unconfirmed_numbers_are_never_stored():
    c, _ = _conv(care={**CARE, "items": ITEMS[1:]})
    c.say("Yes")
    assert c.say("142") == "I heard 1 4 2. Is that correct?"
    assert c.say("No") == "Please tell me your fasting sugar reading."
    assert c.say("one four three") == "I heard 1 4 3. Is that correct?"
    c.say("No")                                                                    # second "no": nothing stored
    assert c.control == "end"
    out = c.state["outcomes"][-1]
    assert out["outcome"] == "care_partial" and out["answers"] == [{"item": "i2", "kind": "reading", "value": None}]


def test_bp_readback_and_wrong_person():
    c, _ = _conv(care={**CARE, "items": [{"id": "b", "kind": "reading", "measure": "bp", "measure_label": "BP"}]})
    c.say("Yes")
    assert c.say("140 by 90") == "I heard 1 4 0 by 9 0. Is that correct?"
    w, _ = _conv()
    assert w.say("No") == "Sorry to trouble you. Thank you."
    assert w.control == "end" and w.state["outcomes"] == [{"wf": "CARE", "outcome": "care_identity_failed"}]


@pytest.mark.parametrize("text,want", [("142", "142"), ("one four two", "142"), ("140/90", "140/90"),
                                       ("140 over 90", "140/90"), ("1420", None), ("hello", None), ("9", None)])
def test_care_number(text, want):
    assert DialogEngine.care_number(text) == want


def test_record_call_stores_only_confirmed_answers(monkeypatch):
    from app.services import cc, cc_voice
    stored, evaluated = [], []

    async def obs(cid, eid, measure, **kw):
        stored.append((measure, kw.get("code"), kw.get("value"), kw.get("value2"), kw["source"]))
        return {"id": str(uuid.uuid4())}

    async def sb(q):
        return SimpleNamespace(data=[{"id": "e1", "status": "active"}])
    monkeypatch.setattr(cc, "record_observation", obs)
    monkeypatch.setattr(cc, "evaluate_enrollment", lambda c, e: evaluated.append(e["id"]) or asyncio.sleep(0))
    monkeypatch.setattr(cc_voice, "sb", sb)
    monkeypatch.setattr("app.database.supabase", SimpleNamespace(table=lambda t: _Chain()))
    monkeypatch.setattr("app.database.sb", sb)
    state = {"outcomes": [{"wf": "CARE", "outcome": "care_partial", "answers": [
        {"item": "i1", "kind": "adherence", "value": "missed"},
        {"item": "i2", "kind": "reading", "measure": "bp", "value": "150/95"},
        {"item": "i3", "kind": "reading", "measure": "fbg", "value": None}]}]}
    call = {"id": "call1", "handoff_packet": {"care": {"enrollment_id": "e1"}}}
    n = asyncio.run(cc_voice.record_call({"id": "c1"}, call, state))
    assert n == 2 and stored == [("adherence", "missed", None, None, "voice_speech_confirmed"),
                                 ("bp", None, 150.0, 95.0, "voice_speech_confirmed")]
    assert evaluated == ["e1"]
    assert asyncio.run(cc_voice.record_call({"id": "c1"}, {"id": "x", "handoff_packet": {}}, state)) == 0


class _Chain:
    def __getattr__(self, name):
        return lambda *a, **k: self


# ── the daily caps (ADR-CC-16: care calls never use the lead / callback budget) ─────────────────────────────

class _Stop(Exception):
    pass


class _Noon(datetime):
    """11:00 IST: inside every calling window, so the caps (not the clock) decide the outcome at any time of day."""
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 25, 11, 0, tzinfo=tz or outbound.IST)


def _dispatch(monkeypatch, purpose, lead_calls_today, care_today, cc_state="ACTIVE"):
    deferred = []
    monkeypatch.setattr(outbound, "datetime", _Noon)

    class Q:
        def __init__(self, table):
            self.table, self.payload = table, None

        def update(self, payload):
            self.payload = payload
            return self

        def __getattr__(self, name):
            return lambda *a, **k: self

    async def sb(q):
        if q.table == "voice_outbound_jobs" and q.payload is None:
            return SimpleNamespace(data=[{"id": "j1", "clinic_id": "c1", "purpose": purpose, "attempts": 0,
                                          "patient_phone": "+919800000001", "context": {}}])
        if q.payload and "next_attempt_at" in q.payload:
            deferred.append(q.payload)
        return SimpleNamespace(data=[])

    async def in_flight(cid):
        raise _Stop()                                                    # reached the dial path: not deferred

    async def clinic_of(cid):
        return {"id": "c1", "cc_state": cc_state, "cc_settings": {"care_daily_cap": 30}}
    monkeypatch.setattr(outbound, "sb", sb)
    monkeypatch.setattr(outbound, "supabase", SimpleNamespace(table=Q))
    monkeypatch.setattr(outbound, "get_clinic_by_id", clinic_of)
    monkeypatch.setattr(outbound, "ai_receptionist_enabled", lambda c: True)
    monkeypatch.setattr(outbound.store, "voice_config", lambda c: {"outbound": {"daily_cap": 5, "concurrent": 1},
                                                                   "primary_language": "en-IN"})
    monkeypatch.setattr(outbound, "next_window_start", lambda w, t=None: t if t and t.hour == 23 else None)
    monkeypatch.setattr(outbound, "_today_count", lambda cid: asyncio.sleep(0, lead_calls_today))
    monkeypatch.setattr(outbound, "_care_today", lambda cid: asyncio.sleep(0, care_today))
    monkeypatch.setattr(outbound, "_in_flight", in_flight)
    try:
        asyncio.run(outbound.dispatch_due())
    except _Stop:
        return "dial"
    return "deferred" if deferred else "none"


def test_care_calls_have_their_own_cap(monkeypatch):
    assert _dispatch(monkeypatch, "care_checkin", lead_calls_today=99, care_today=0) == "dial"    # lead cap irrelevant
    assert _dispatch(monkeypatch, "care_checkin", lead_calls_today=0, care_today=30) == "deferred"


def test_lead_cap_excludes_care_calls_and_is_unchanged_without_care(monkeypatch):
    assert _dispatch(monkeypatch, "lead_followup", lead_calls_today=5, care_today=2) == "dial"     # 5 − 2 care < 5
    assert _dispatch(monkeypatch, "lead_followup", lead_calls_today=5, care_today=2, cc_state="OFF") == "deferred"
    assert _dispatch(monkeypatch, "callback_request", lead_calls_today=4, care_today=0, cc_state="OFF") == "dial"


def test_care_cap_setting():
    assert outbound._care_cap({}) == 30 and outbound._care_cap({"cc_settings": {"care_daily_cap": 12}}) == 12
    assert Path("app/voice/outbound.py").read_text(encoding="utf-8").count("CARE") >= 3
    assert datetime  # noqa: B018 (import used by the IST helpers above)
