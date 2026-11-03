"""Session 57: a patient who booked on WhatsApp phones the hospital. The receptionist reads THEIR booking
(this clinic, their number) and handles what a front-desk person would: details, "is it confirmed?", cancel,
change the time or the doctor, payment trouble, the refund, running late, the wait, a complaint.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.voice.nlu_rules import NluContext, understand
from app.voice.tools import CallContext, KriyaTools

from .test_dialog import SLOTS, Conv, FakeTools

APPT = {"id": "a1", "booking_ref": "KR-7Q2X", "status": "confirmed", "paid": False, "doctor_id": "d3",
        "doctor_name": "Dr. Lakshmi Prasanna", "booking_type": "consultation", "lab_test_name": None,
        "date": "2026-10-07", "time": "10:30"}
CTX = NluContext(doctors=[], departments=["Cardiology", "Gynecology"])


class Tools(FakeTools):
    def __init__(self, refunds=None, **kw):
        super().__init__(slots=SLOTS, **kw)
        self._refunds = refunds

    async def refunds(self):
        self.calls.append(("refunds",))
        return self._refunds

    async def note_for_staff(self, text):
        self.calls.append(("note", text))
        return {"ok": True}


def caller(lang="en-IN", appt=APPT, **kw):
    return Conv(Tools(appts=[appt] if appt else [], **kw), lang=lang)


def intents(text, expect=None):
    return understand(text, CTX, expect).intents


# ---- understanding ----

@pytest.mark.parametrize("text", ["I want to change my appointment time", "can I come tomorrow evening instead",
                                  "move my appointment to Friday", "I booked the wrong doctor",
                                  "అపాయింట్మెంట్ టైమ్ మార్చాలి", "अपॉइंटमेंट का समय बदलना है", "mera appointment badal do"])
def test_changing_the_booking_is_a_reschedule(text):
    i = intents(text)
    assert "RESCHEDULE_APPOINTMENT" in i and "APPOINTMENT_STATUS" not in i and "HOSPITAL_INFORMATION" not in i


@pytest.mark.parametrize("text, expect", [("another day", "option"), ("change the doctor", "option"),
                                          ("instead", "confirm"), ("change the time", "date")])
def test_mid_booking_answers_are_never_a_reschedule(text, expect):
    assert "RESCHEDULE_APPOINTMENT" not in intents(text, expect)


@pytest.mark.parametrize("text", ["when will I get my refund", "I cancelled my appointment, where is my refund",
                                  "రీఫండ్ ఎప్పుడు వస్తుంది", "रिफंड कब आएगा", "paisa wapas kab milega"])
def test_refund_questions(text):
    i = intents(text)
    assert "REFUND_STATUS" in i and "LOCATION" not in i and "APPOINTMENT_STATUS" not in i


def test_cancel_and_refund_is_one_cancel():
    i = intents("cancel my appointment and refund my money")
    assert "CANCEL_APPOINTMENT" in i and "REFUND_STATUS" not in i


@pytest.mark.parametrize("text", ["I will be late for my appointment", "I'm running 10 minutes late",
                                  "నేను కొంచెం లేట్ అవుతుంది", "मैं थोड़ा लेट हो जाऊँगा", "late ho jayega"])
def test_running_late_on_any_call(text):
    i = intents(text)
    assert "RUNNING_LATE" in i and "APPOINTMENT_STATUS" not in i and "BOOK_APPOINTMENT" not in i


@pytest.mark.parametrize("text", ["how long will I have to wait", "ఎంతసేపు ఆగాలి", "कितना इंतज़ार करना पड़ेगा"])
def test_waiting_is_the_queue_not_fees(text):
    i = intents(text)
    assert "QUEUE_STATUS" in i and "FEES" not in i


@pytest.mark.parametrize("text", ["the doctor did not come", "డాక్టర్ గారు రాలేదు", "डॉक्टर नहीं आए",
                                  "the receptionist was rude"])
def test_complaints(text):
    i = intents(text)
    assert "COMPLAINT" in i and "DOCTOR_INFORMATION" not in i


def test_report_did_not_come_is_a_report_question():
    i = intents("my report did not come")
    assert "COMPLAINT" not in i and "REPORT_STATUS" in i


# ---- conversations ----

def test_details_and_is_it_confirmed():
    for text in ("what time is my appointment", "is my appointment confirmed"):
        r = caller().say(text)
        assert "Dr. Lakshmi Prasanna tomorrow at 10:30 AM" in r and "It is confirmed" in r


def test_pending_review_is_never_called_confirmed():
    r = caller(appt={**APPT, "status": "pending_review"}).say("is my appointment confirmed")
    assert "It is confirmed" not in r and "checking it" in r


def test_change_time_moves_the_same_doctors_booking_and_cancels_the_old_one_after():
    c = caller(appt={**APPT, "doctor_id": "d1", "doctor_name": "Dr. Srinivas Rao"})
    assert "Which day" in c.say("I want to change my appointment time")
    c.say("tomorrow morning")
    r = c.say("yes")
    assert "old appointment" in r
    calls = [x for x in c.tools.calls if x[0] in ("book", "cancel")]
    assert calls == [("book", "d1", "2026-10-07", "10:30", "Ravi Kumar", "SELF"), ("cancel", "a1")]  # new first


def test_wrong_doctor_moves_to_the_named_department():
    c = caller()
    c.say("I booked the wrong doctor, change it to cardiology")
    c.say("tomorrow")
    c.say("morning")
    c.say("the first one")
    c.say("yes")
    calls = [x for x in c.tools.calls if x[0] in ("book", "cancel")]
    assert calls == [("book", "d1", "2026-10-07", "10:30", "Ravi Kumar", "SELF"), ("cancel", "a1")]


def test_payment_trouble_on_an_open_payment_goes_to_staff():
    c = caller(appt={**APPT, "status": "pending_payment"})
    r = c.say("I paid but did not get confirmation")
    assert "payment is still pending" in r and "fresh payment link" in r
    assert ("callback", "payment_help") in c.tools.calls


def test_payment_question_on_a_paid_booking():
    r = caller(appt={**APPT, "paid": True}).say("payment status please")
    assert "It is confirmed" in r and "payment has been received" in r


@pytest.mark.parametrize("rows, says, callback", [
    ([{"id": "a9", "status": "refunded", "doctor_name": "Dr. Anil Varma", "date": "2026-10-03", "time": "11:00"}],
     "refund for your appointment with Dr. Anil Varma", None),
    ([{"id": "a9", "status": "cancelled", "doctor_name": "Dr. Anil Varma", "date": "2026-10-03", "time": "11:00"}],
     "isn't processed yet", "refund_not_processed"),
    ([], "can't see the refund details", "refund_status"),
    (None, "can't see the refund details", "refund_status"),          # lookup failed: never "nothing is due"
])
def test_refund(rows, says, callback):
    c = caller(refunds=rows)
    r = c.say("when will I get my refund")
    assert says in r
    cbs = [x for x in c.tools.calls if x[0] == "callback"]
    assert cbs == ([("callback", callback)] if callback else [])


def test_running_late_inbound_tells_reception_and_keeps_helping():
    c = caller()
    r = c.say("I will be late for my appointment")
    assert "as soon as you can" in r and c.control == "continue" and c.state["expect"] == "anything_else"
    assert any(x[0] == "note" for x in c.tools.calls)


def test_complaint_is_noted_for_staff():
    c = caller()
    r = c.say("the doctor did not come")
    assert "call you back" in r and any(x[0] == "callback" for x in c.tools.calls)


@pytest.mark.parametrize("lang, text, says", [
    ("te-IN", "నా అపాయింట్మెంట్ కన్ఫర్మ్ అయిందా", "కన్ఫర్మ్ అయింది"),
    ("hi-IN", "मेरा अपॉइंटमेंट कब है", "यह कन्फर्म है"),
    ("te-IN", "రీఫండ్ ఎప్పుడు వస్తుంది", "రీఫండ్"),
    ("hi-IN", "मैं थोड़ा लेट हो जाऊँगा", "रिसेप्शन"),
])
def test_telugu_and_hindi(lang, text, says):
    assert says in caller(lang).say(text)


# ---- the refund read is this clinic's and this caller's only ----

@pytest.mark.asyncio
async def test_refund_lookup_is_scoped_to_clinic_and_caller_and_skips_unpaid():
    q = MagicMock()
    for m in ("table", "select", "eq", "in_", "gte", "order", "limit"):
        getattr(q, m).return_value = q
    rows = [{"id": "a1", "status": "refunded", "payment_id": "pay_1", "appointment_date": "2026-10-03",
             "appointment_time": "11:00:00", "doctor_name": "Dr. X"},
            {"id": "a2", "status": "cancelled", "payment_id": None, "appointment_date": "2026-10-04"}]
    ctx = CallContext(call_id="c", call_ref="CALL-1", clinic={"id": "clinic-A"}, branch_id=None,
                      caller_phone="+919876543210")
    with patch("app.voice.tools.supabase", new=q), \
         patch("app.voice.tools.sb", new=AsyncMock(return_value=SimpleNamespace(data=rows))), \
         patch("app.voice.tools.store.add_event", new=AsyncMock()):
        out = await KriyaTools(ctx).refunds()
    assert [r["id"] for r in out] == ["a1"]                                # an unpaid cancel has no refund
    eqs = [c.args for c in q.eq.call_args_list]
    assert ("clinic_id", "clinic-A") in eqs and ("patient_phone", "+919876543210") in eqs
