"""Session 56: what a person says about the CALL itself, on the calls Kriya places.

"Don't call me" / "wrong number" end the call and the number is never dialled again; "busy / driving /
call later" ends it politely and staff call back; "I'll be late" on a reminder is acknowledged; "who is
this?" is answered on any call. None of them is ever a misunderstanding, and "call mat karo" is never a yes.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.voice import outbound
from app.voice.nlu_rules import NluContext, understand

from .test_dialog import SLOTS, Conv, FakeTools

CTX = NluContext(doctors=[], departments=["Cardiology"])
APPT = {"id": "a1", "date": "2026-10-06", "time": "17:30", "doctor_name": "Dr. Ramesh Rao", "doctor_id": "d1",
        "patient_name": "Lakshmi"}


class NotingTools(FakeTools):
    async def note_for_staff(self, text):
        self.calls.append(("note", text))
        return {"ok": True}


def lead(lang="en-IN"):
    c = Conv(NotingTools(slots=SLOTS), lang=lang)
    c.engine.greeting(c.state, outbound=True, interest="cardiology")
    return c


def reminder(lang="en-IN"):
    c = Conv(NotingTools(slots=SLOTS), lang=lang)
    c.engine.greeting(c.state, reminder=APPT)
    return c


def acts(text, expect=None):
    return understand(text, CTX, expect).intents


# ---- understanding ----

@pytest.mark.parametrize("text", ["mujhe call mat karo", "call mat karo", "मुझे कॉल मत करो", "మళ్ళీ కాల్ చేయకండి",
                                  "don't call me again", "please stop calling me", "remove my number"])
def test_do_not_call_is_never_a_yes(text):
    i = acts(text, "offer")
    assert "DO_NOT_CALL" in i and "AFFIRM" not in i


@pytest.mark.parametrize("text", ["mat karo", "బుక్ చేయకండి", "don't book it", "मत कीजिए"])
def test_a_negated_do_word_is_never_a_yes(text):
    i = acts(text, "confirm")
    assert "AFFIRM" not in i and "DENY" in i


@pytest.mark.parametrize("text", ["don't call me now, call me later", "abhi call mat karo baad mein karo",
                                  "ఇప్పుడు కాల్ చేయకండి, తర్వాత చేయండి", "don't call me now I'm driving"])
def test_not_now_is_busy_never_do_not_call(text):
    i = acts(text, "offer")
    assert "DO_NOT_CALL" not in i and "BUSY" in i


@pytest.mark.parametrize("text, act", [
    ("wrong number", "WRONG_NUMBER"), ("రాంగ్ నంబర్", "WRONG_NUMBER"), ("galat number hai", "WRONG_NUMBER"),
    ("I'm driving", "BUSY"), ("in a meeting, call me later", "BUSY"), ("ఇప్పుడు బిజీగా ఉన్నాను", "BUSY"),
    ("call me in the evening", "BUSY"), ("who is this?", "WHO_IS_THIS"), ("ఎవరు మాట్లాడేది", "WHO_IS_THIS"),
    ("kaun bol raha hai", "WHO_IS_THIS"),
])
def test_call_acts_are_read(text, act):
    assert act in acts(text, "offer")


def test_who_is_the_doctor_is_not_who_is_this():
    assert "WHO_IS_THIS" not in acts("who is the doctor", None)
    assert "WHO_IS_THIS" not in acts("aap kaun se doctor ke paas bhejoge", None)


def test_late_means_running_late_only_in_its_own_shapes():
    """Session 57: "I'll be late" counts on any call; a loose "late" ("late evening slot") never does."""
    assert "RUNNING_LATE" in acts("I'll be 15 minutes late", "reminder")
    assert "RUNNING_LATE" in acts("I'll be 15 minutes late", None)
    assert "RUNNING_LATE" in acts("late", "reminder")                      # on a reminder, the bare word
    for text in ("do you have a late evening slot", "late", "is the doctor late today"):
        assert "RUNNING_LATE" not in acts(text, None), text


@pytest.mark.parametrize("text, expected", [("yes", ["AFFIRM"]), ("haan book kar do", None), ("ఓకే", ["AFFIRM"]),
                                            ("no", ["DENY"]), ("cardiology tomorrow", None)])
def test_ordinary_replies_unchanged(text, expected):
    i = acts(text, "offer")
    assert not set(i) & {"DO_NOT_CALL", "WRONG_NUMBER", "BUSY", "WHO_IS_THIS", "RUNNING_LATE"}
    if expected:
        assert i == expected


# ---- lead calls ----

@pytest.mark.parametrize("text, outcome, said", [
    ("mujhe call mat karo", "do_not_call", "won't call you again"),
    ("wrong number", "wrong_number", "wrong number"),
])
def test_lead_never_again_ends_the_call_and_tells_staff(text, outcome, said):
    c = lead()
    r = c.say(text)
    assert said in r and c.control == "end"
    assert c.state["outcomes"][-1] == {"wf": "CALL", "outcome": outcome}
    assert any(x[0] == "note" for x in c.tools.calls)
    assert not [x for x in c.tools.calls if x[0] in ("find_slots", "book", "handoff")]


def test_lead_busy_ends_politely_and_staff_call_back():
    c = lead()
    r = c.say("I'm driving, call me later")
    assert "sorry to disturb" in r and c.control == "end"
    assert c.state["outcomes"][-1]["outcome"] == "call_later"
    assert ("note", "Busy when called: please call back at a better time.") in c.tools.calls
    assert not [x for x in c.tools.calls if x[0] == "callback"]          # one note, not two


def test_lead_who_is_this_is_answered_and_the_offer_repeated():
    c = lead()
    r = c.say("who is this?")
    assert "This is Kriya from ABC Hospitals" in r and "book an appointment" in r
    assert c.control == "continue" and c.state["failures"] == 0 and c.state["expect"] == "offer"
    r = c.say("yes")
    assert "Which doctor or department" in r                    # the offer still stood: straight into booking


def test_lead_who_is_this_in_telugu():
    c = lead("te-IN")
    r = c.say("ఎవరు మాట్లాడేది")
    assert "ABC Hospitals నుండి Kriya మాట్లాడుతున్నాను" in r


# ---- reminder calls ----

def test_reminder_running_late_is_acknowledged_and_reception_told():
    c = reminder()
    r = c.say("I'll be 15 minutes late")
    assert "as soon as you can" in r and c.control == "end"
    assert c.state["outcomes"][-1] == {"wf": "REMINDER", "outcome": "reminder_running_late"}
    assert any(x[0] == "note" and "late" in x[1] for x in c.tools.calls)


def test_reminder_busy_reminds_to_come_on_time_without_a_callback_note():
    c = reminder()
    r = c.say("I'm busy right now")
    assert "come on time" in r and c.control == "end"
    assert not [x for x in c.tools.calls if x[0] in ("note", "callback")]


def test_reminder_do_not_call():
    c = reminder("hi-IN")
    r = c.say("mujhe call mat karo")
    assert "कॉल नहीं करेंगे" in r and c.control == "end"


def test_reminder_yes_and_no_unchanged():
    c = reminder()
    c.say("yes I will come")
    assert c.state["outcomes"][-1]["outcome"] == "reminder_confirmed"
    c = reminder()
    r = c.say("no I can't come")
    assert "move your appointment" in r


# ---- inbound calls: the person called US ----

def test_inbound_busy_is_still_a_callback_request_and_wrong_number_not_acted_on():
    c = Conv(NotingTools(slots=SLOTS), lang="en-IN")
    r = c.say("I'm busy, call me back")
    assert "call you back" in r and ("callback", "CALLBACK_REQUEST") in c.tools.calls
    c = Conv(NotingTools(slots=SLOTS), lang="en-IN")
    c.say("wrong number")
    assert not [x for x in c.tools.calls if x[0] == "note"]
    assert not any(o.get("outcome") == "wrong_number" for o in c.state["outcomes"])


def test_inbound_who_is_this():
    c = Conv(NotingTools(slots=SLOTS), lang="en-IN")
    r = c.say("who is this")
    assert "This is Kriya from ABC Hospitals" in r and "how can I help" in r


# ---- never dialled again ----

def _res(data):
    return SimpleNamespace(data=data)


@pytest.mark.asyncio
async def test_eligible_refuses_a_number_that_said_never_again():
    sb = AsyncMock(side_effect=[_res([{"id": "p1", "opted_in": True}]), _res([{"caller_phone": "+919000000001"}])])
    with patch.object(outbound, "sb", new=sb), patch.object(outbound, "supabase", new=MagicMock()) as fake:
        assert await outbound._eligible("c1", "+919000000001", attested=True) == "do_not_contact"
    q = fake.table.return_value.select.return_value.eq.return_value
    q.eq.assert_called_with("direction", "outbound")
    q.eq.return_value.in_.assert_called_with("outcome", ["do_not_call", "wrong_number"])


@pytest.mark.asyncio
async def test_eligible_unchanged_for_everyone_else():
    sb = AsyncMock(side_effect=[_res([{"id": "p1", "opted_in": True}]), _res([])])
    with patch.object(outbound, "sb", new=sb), patch.object(outbound, "supabase", new=MagicMock()):
        assert await outbound._eligible("c1", "+919000000001") is None
    sb = AsyncMock(side_effect=[_res([{"id": "p1", "opted_in": False}])])           # opt-out: no extra read
    with patch.object(outbound, "sb", new=sb), patch.object(outbound, "supabase", new=MagicMock()):
        assert await outbound._eligible("c1", "+919000000001") == "do_not_contact"


@pytest.mark.asyncio
async def test_bulk_import_skips_a_number_that_said_never_again():
    sb = AsyncMock(side_effect=[_res([{"phone": "+919000000001", "opted_in": True},
                                      {"phone": "+919000000002", "opted_in": True}]),
                                _res([]), _res([{"caller_phone": "+919000000002"}]), _res([{"id": "j"}])])
    with patch.object(outbound, "ai_receptionist_enabled", return_value=True), \
         patch.object(outbound, "sb", new=sb), patch.object(outbound, "supabase", new=MagicMock()):
        r = await outbound.enqueue_bulk({"id": "c1"}, [{"phone": "9000000001"}, {"phone": "9000000002"}],
                                        {"source": "camp"}, "admin1")
    assert r["queued"] == 1 and r["skipped"] == [{"phone": "+919000000002", "reason": "Opted out — never called"}]


def test_reminder_busy_that_day_and_cant_come_is_offered_a_new_time():
    """Session 57 audit: "busy" must not swallow "can't come" on a reminder (the call used to just end)."""
    c = reminder()
    r = c.say("no I'm busy that day, I can't come")
    assert "move your appointment" in r and c.control == "continue"
    assert c.state["outcomes"][-1]["outcome"] == "reminder_cant_come"
