"""Scripted conversations through the real NLU rules + dialog engine, with an
in-memory FakeTools. Covers the spec's acceptance demos at the text level."""

import asyncio
from datetime import date, datetime

from app.voice.dates import IST
from app.voice.dialog import DialogEngine, new_state
from app.voice.lexicon import SPECIALTIES
from app.voice.nlu_rules import NluContext, understand
from app.voice import responses as R

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=IST)
TODAY = date(2026, 10, 6)
DOCS = [
    {"id": "d1", "name": "Dr. Srinivas Rao", "department": "Cardiology", "fee_paise": 80000},
    {"id": "d2", "name": "Dr. Anil Varma", "department": "Cardiology", "fee_paise": 60000},
    {"id": "d3", "name": "Dr. Lakshmi Prasanna", "department": "Gynecology", "fee_paise": 50000},
]


class FakeTools:
    def __init__(self, **kw):
        self.calls = []
        self.profile = kw.get("profile", {"name": "Ravi Kumar"})
        self.slots = kw.get("slots")            # None => system down
        self.book_result = kw.get("book_result", {"status": "confirmed", "verified": True,
                                                  "booking_ref": "KR-7Q2X", "whatsapp_sent": True})
        self.book_results = kw.get("book_results")
        self.appts = kw.get("appts", [])
        self.reps = kw.get("reports", [])
        self.handoff_mode = kw.get("handoff_mode", "transfer")
        self.cancel_result = kw.get("cancel_result", {"status": "cancelled", "verified": True})

    async def find_doctors(self, department, doctor_ids):
        self.calls.append(("find_doctors", department, doctor_ids))
        if doctor_ids:
            return [d for d in DOCS if d["id"] in doctor_ids]
        return [d for d in DOCS if d["department"] == department]

    async def find_slots(self, doctors, date_, period, clock):
        self.calls.append(("find_slots", date_, period, clock))
        if self.slots is None:
            return None
        return [o for o in self.slots if o["date"] == date_
                and (period is None or o["period"] == period) and o["doctor_id"] in {d["id"] for d in doctors}]

    async def next_available(self, doctors, after, period):
        self.calls.append(("next_available", after, period))
        ids = {d["id"] for d in doctors}
        return [o for o in (self.slots or []) if o["date"] > after and o["doctor_id"] in ids][:2]

    async def caller_profile(self):
        return self.profile

    async def book(self, option, name, relation):
        self.calls.append(("book", option["doctor_id"], option["date"], option["time"], name, relation))
        if self.book_results:
            return self.book_results.pop(0)
        return self.book_result

    async def upcoming_appointments(self):
        return self.appts

    async def cancel(self, appt):
        self.calls.append(("cancel", appt["id"]))
        return self.cancel_result

    async def reports(self):
        return self.reps

    async def resend_report(self, r):
        self.calls.append(("resend_report", r["id"]))
        return {"status": "sent"}

    async def doctor_fees(self, department, doctor_ids):
        return [d for d in DOCS if (doctor_ids and d["id"] in doctor_ids) or d["department"] == department]

    async def lab_tests(self, query):
        self.calls.append(("lab_tests", query))
        return [{"id": "t1", "name": "CBC", "price_paise": 35000}] if query else []

    async def book_lab(self, test, date_, name):
        self.calls.append(("book_lab", test["id"], date_, name))
        return {"status": "payment_pending", "verified": True, "link_sent": True, "amount_paise": 35000,
                "hold_minutes": 10}

    async def info(self, topic):
        return "We are open 9 AM to 9 PM." if topic == "hours" else None

    async def queue_status(self):
        return None

    async def create_callback(self, reason):
        self.calls.append(("callback", reason))
        return {"ok": True}

    async def handoff(self, reason):
        self.calls.append(("handoff", reason))
        return {"mode": self.handoff_mode}


def slot(doc, d, t, period):
    fee = next(x["fee_paise"] for x in DOCS if x["id"] == doc)
    name = next(x["name"] for x in DOCS if x["id"] == doc)
    return {"doctor_id": doc, "doctor_name": name, "date": d, "time": t, "period": period, "fee_paise": fee}


SLOTS = [slot("d1", "2026-10-07", "10:30", "MORNING"), slot("d2", "2026-10-07", "11:00", "MORNING"),
         slot("d1", "2026-10-07", "17:00", "EVENING"), slot("d1", "2026-10-09", "10:00", "MORNING")]


class Conv:
    def __init__(self, tools, lang="te-IN"):
        self.tools = tools
        self.engine = DialogEngine(tools, {"hospital": "ABC Hospitals", "assistant": "Kriya",
                                           "emergency": "040-12345678", "hold_minutes": 10})
        self.state = new_state(lang)
        self.ctx = NluContext(doctors=DOCS, departments=["Cardiology", "Gynecology"], now=NOW)
        self.control = "continue"

    def text(self, out):
        labels = {k: v["label"] for k, v in SPECIALTIES.items()}
        return " ".join(R.realize(s.key, self.state["lang"], TODAY, labels, **s.params) for s in out.says)

    def say(self, utterance):
        nlu = understand(utterance, self.ctx, self.state.get("expect"))
        out = asyncio.run(self.engine.turn(self.state, nlu, utterance))
        self.control = out.control
        return self.text(out)


def test_spec_95_telugu_booking_known_caller():
    t = FakeTools(slots=SLOTS[:1] + SLOTS[2:])
    c = Conv(t)
    r1 = c.say("Naaku repu heart doctor appointment kavali")
    assert "ఉదయం కావాలా" in r1                                  # one question: time period
    r2 = c.say("Morning")
    assert "Srinivas Rao" in r2 and "ఉదయం 10:30" in r2 and "800 రూపాయలు" in r2
    r3 = c.say("Yes book it.")
    assert "బుక్ అయింది" in r3 and "K R 7 Q 2 X" in r3 and "WhatsApp" in r3
    assert [x for x in t.calls if x[0] == "book"] == [("book", "d1", "2026-10-07", "10:30", "Ravi Kumar", "SELF")]
    assert c.state["outcomes"][-1] == {"wf": "BOOKING", "outcome": "booked"}
    assert "ఇంకా ఏమైనా" in r3


def test_payment_link_flow_says_held_not_booked():
    t = FakeTools(slots=SLOTS[:1], book_result={"status": "payment_pending", "verified": True, "link_sent": True,
                                                "amount_paise": 80000, "hold_minutes": 10})
    c = Conv(t, lang="en-IN")
    c.say("I need a cardiologist tomorrow morning.")
    r = c.say("yes")
    assert "held this slot" in r and "payment link on WhatsApp" in r and "800 rupees" in r
    assert "is booked" not in r


def test_unverified_booking_is_never_announced_as_booked():
    t = FakeTools(slots=SLOTS[:1], book_result={"status": "confirmed", "verified": False})
    c = Conv(t, lang="en-IN")
    c.say("I need a cardiologist tomorrow morning.")
    r = c.say("yes")
    assert "couldn't confirm" in r and "is booked" not in r
    assert ("callback", "booking_unverified") in t.calls


def test_hms_down_no_false_confirmation():
    c = Conv(FakeTools(slots=None), lang="en-IN")
    r = c.say("I need a cardiologist tomorrow morning.")
    assert "temporarily unavailable" in r and "is booked" not in r


def test_correction_no_evening_replans():
    t = FakeTools(slots=SLOTS)
    c = Conv(t, lang="en-IN")
    r = c.say("I need a cardiologist tomorrow morning.")
    assert "two options" in r
    r = c.say("No, evening.")
    assert "5 PM" in r and "Shall I book it" in r
    assert ("find_slots", "2026-10-07", "EVENING", None) in t.calls


def test_two_options_then_name_for_mother_then_confirm():
    t = FakeTools(slots=SLOTS, profile={"name": None})
    c = Conv(t, lang="en-IN")
    c.say("My mother needs a heart doctor tomorrow morning")
    r = c.say("second one")
    assert "patient's name" in r
    r = c.say("Lakshmi Devi")
    assert "Lakshmi Devi with Dr. Anil Varma" in r and "600 rupees" in r
    c.say("yes")
    assert t.calls[-1] == ("book", "d2", "2026-10-07", "11:00", "Lakshmi Devi", "MOTHER")


def test_slot_taken_reoffers_once():
    t = FakeTools(slots=SLOTS[:1] + SLOTS[2:], book_results=[{"status": "slot_taken"}])
    c = Conv(t, lang="en-IN")
    c.say("I need a cardiologist tomorrow morning.")
    r = c.say("yes")
    assert "just taken" in r


def test_multi_intent_booking_then_report():
    t = FakeTools(slots=SLOTS[:1], reports=[{"id": "r1", "test_name": "CBC", "status": "ready"}])
    c = Conv(t, lang="en-IN")
    c.say("Book a cardiology appointment tomorrow morning and send my report on WhatsApp.")
    r = c.say("yes")
    assert "is booked" in r and "your other request" in r and "sent the report" in r
    assert ("resend_report", "r1") in t.calls


def test_cancel_with_refund_from_two_appointments():
    appts = [{"id": "a1", "doctor_name": "Dr. Srinivas Rao", "date": "2026-10-07", "time": "10:30",
              "status": "confirmed", "booking_ref": "KR-1", "doctor_id": "d1"},
             {"id": "a2", "doctor_name": "Dr. Anil Varma", "date": "2026-10-09", "time": "11:00",
              "status": "confirmed", "booking_ref": "KR-2", "doctor_id": "d2"}]
    t = FakeTools(appts=appts, cancel_result={"status": "refunded", "verified": True})
    c = Conv(t, lang="en-IN")
    r = c.say("I want to cancel my appointment")
    assert "You have 2 appointments" in r
    r = c.say("second one")
    assert "Shall I cancel your appointment with Dr. Anil Varma" in r
    r = c.say("yes")
    assert "refund has been initiated" in r and ("cancel", "a2") in t.calls


def test_cancel_unverified_never_claims_cancelled():
    appts = [{"id": "a1", "doctor_name": "Dr. Srinivas Rao", "date": "2026-10-07", "time": "10:30",
              "status": "confirmed", "doctor_id": "d1"}]
    t = FakeTools(appts=appts, cancel_result={"status": "cancelled", "verified": False})
    c = Conv(t, lang="en-IN")
    c.say("cancel my appointment")
    r = c.say("yes")
    assert "couldn't cancel" in r


def test_human_request_is_offered_self_service_once_then_transfers():
    t = FakeTools()
    c = Conv(t, lang="en-IN")
    r = c.say("I want to talk to the receptionist.")
    assert "book your appointment myself" in r and c.control == "continue"
    assert not [x for x in t.calls if x[0] == "handoff"]
    r = c.say("receptionist please")                      # asked twice: always honoured
    assert "connecting you" in r and c.control == "transfer"
    assert ("handoff", "caller_requested_human") in t.calls


def test_human_offer_accepted_transfers_and_declined_keeps_helping():
    t = FakeTools()
    c = Conv(t, lang="en-IN")
    c.say("connect me to reception")
    assert "connecting you" in c.say("yes") and c.control == "transfer"
    c2 = Conv(FakeTools(), lang="en-IN")
    c2.say("connect me to reception")
    assert "how can i help" in c2.say("no").lower() and c2.control == "continue"


def test_human_request_mid_booking_transfers_immediately():
    c = Conv(FakeTools(), lang="en-IN")
    c.say("I need a heart doctor tomorrow")
    r = c.say("just connect me to the receptionist")
    assert "connecting you" in r and c.control == "transfer"


def test_after_hours_handoff_becomes_callback():
    c = Conv(FakeTools(handoff_mode="callback"), lang="en-IN")
    c.say("connect me to a person")
    r = c.say("yes")
    assert "call you back" in r and c.control == "end"


def test_production_hello_calls_are_answered_not_transferred():
    """CALL-20261007-5B274E: 'హలో.', 'హలో.', then mis-heard words -> transferred in 5 s."""
    c = Conv(FakeTools())
    assert "వింటున్నాను" in c.say("హలో.")
    c.say("హలో.")
    assert c.control == "continue" and c.state.get("misses_total", 0) == 0
    r = c.say("కార్డియోలజీ డిపార్ట్మెంట్లో అపాయింట్మెంట్ ఉంటాదా?")
    assert c.control == "continue" and "రోజు" in r          # booking started: asks the day


def test_production_okay_and_aa_confirm_the_booking():
    """CALL-20261006-0883EB: 'ఓకే.' / 'ఆ.' to 'బుక్ చేయమంటారా?' were not understood -> transferred."""
    t = FakeTools(slots=SLOTS[:1])
    c = Conv(t)
    c.say("Naaku repu heart doctor appointment kavali morning")
    assert "బుక్ అయింది" in c.say("ఓకే.")
    t2 = FakeTools(slots=SLOTS[:1])
    c2 = Conv(t2)
    c2.say("Naaku repu heart doctor appointment kavali morning")
    assert "బుక్ అయింది" in c2.say("ఆ.")


def test_production_other_information_asks_which_topic():
    """CALL-20261006-81F8B1: Kriya's own menu option 'వేరే సమాచారం కావాలి' was not understood twice."""
    t = FakeTools()
    c = Conv(t)
    r = c.say("వేరే సమాచారం కావాలి.")
    assert "టైమింగ్స్" in r and c.state["expect"] == "info_topic"


def test_production_free_slots_question_starts_booking():
    """CALL-20261006-A40DAD: 'డాక్టర్ స్లాట్స్ ఖాళీ ఉన్నాయా?' -> not understood -> transferred."""
    c = Conv(FakeTools())
    r = c.say("డాక్టర్ స్లాట్స్ ఖాళీ ఉన్నాయా?")
    assert "ఏ డాక్టర్" in r and c.control == "continue"


def test_opening_date_alone_starts_booking():
    c = Conv(FakeTools())
    r = c.say("ఈరోజు.")
    assert "ఏ డాక్టర్" in r and c.state["task"]["slots"].get("date")


def test_real_gibberish_still_escalates_after_three_misses():
    c = Conv(FakeTools())
    for u in ("ఏం చేస్తున్నావ్?", "ఏం చేస్తున్నావ్?", "ఏం చేస్తున్నావ్?"):
        c.say(u)
    assert c.control == "transfer"


def test_three_misunderstandings_escalate():
    t = FakeTools()
    c = Conv(t, lang="en-IN")
    c.say("hmm umm")                  # a filler is answered patiently, not counted
    assert c.control == "continue" and c.state.get("misses_total", 0) == 0
    c.say("blah")
    c.say("zzz")
    c.say("qwerty")
    assert c.control == "transfer" and ("handoff", "repeated_misunderstanding") in t.calls


def test_unknown_caller_cannot_get_others_report():
    c = Conv(FakeTools(reports=[]), lang="en-IN")
    r = c.say("send my friend's report to whatsapp")
    assert "don't see any recent reports on this number" in r


def test_reschedule_paid_goes_to_staff_unpaid_rebooks():
    paid = [{"id": "a1", "doctor_name": "Dr. Srinivas Rao", "date": "2026-10-07", "time": "10:30",
             "status": "confirmed", "paid": True, "doctor_id": "d1"}]
    c = Conv(FakeTools(appts=paid), lang="en-IN")
    r = c.say("I want to reschedule my appointment")
    assert "already paid" in r and c.control == "transfer"

    unpaid = [dict(paid[0], paid=False)]
    t = FakeTools(appts=unpaid, slots=SLOTS)
    c = Conv(t, lang="en-IN")
    r = c.say("I want to reschedule my appointment to friday morning")
    assert "Shall I book it" in r                      # Fri 10:00 single option
    r = c.say("yes")
    assert "is booked" in r and "cancelled your old appointment" in r and ("cancel", "a1") in t.calls


def test_fees_then_offer_booking():
    t = FakeTools(slots=SLOTS)
    c = Conv(t, lang="en-IN")
    r = c.say("cardiology doctor fees entha")
    assert "800 rupees" in r and "Would you like me to book" in r
    r = c.say("yes")
    assert "Which day" in r


def test_hindi_lab_booking():
    t = FakeTools()
    c = Conv(t, lang="hi-IN")
    r = c.say("मुझे कल ब्लड टेस्ट बुक करना है")
    assert "CBC" in r and "कल" in r and "350 रुपये" in r
    r = c.say("हाँ")
    assert "पेमेंट लिंक" in r
    assert t.calls[-1] == ("book_lab", "t1", "2026-10-07", "Ravi Kumar")


def test_language_switch_and_repeat():
    c = Conv(FakeTools(slots=SLOTS), lang="te-IN")
    c.say("repu heart doctor kavali")
    r = c.say("Speak in English")
    assert c.state["lang"] == "en-IN" and "speak in English" in r and "Morning or evening" in r
    r2 = c.say("repeat please")
    assert r2 == r


def test_idempotent_after_booking():
    t = FakeTools(slots=SLOTS[:1])
    c = Conv(t, lang="en-IN")
    c.say("I need a cardiologist tomorrow morning.")
    c.say("yes")
    c.say("yes")
    assert len([x for x in t.calls if x[0] == "book"]) == 1


def test_goodbye_after_anything_else():
    c = Conv(FakeTools(), lang="en-IN")
    c.say("what are your timings")
    r = c.say("no thanks")
    assert "Thank you for calling" in r and c.control == "end"


def test_ask_if_ai_is_truthful():
    c = Conv(FakeTools(), lang="te-IN")
    r = c.say("are you a robot?")
    assert "ఆటోమేటెడ్ అసిస్టెంట్" in r


def test_emergency_and_clinical():
    t = FakeTools()
    c = Conv(t, lang="te-IN")
    out = asyncio.run(c.engine.emergency(c.state))
    txt = c.text(out)
    assert "108" in txt and "040-12345678" in txt and out.control == "transfer"
    out = c.engine.clinical(new_state("en-IN"))
    assert out.says[0].key == "clinical_refusal"


def test_every_template_renders_in_every_language():
    sample = {"hospital": "H", "assistant": "Kriya", "date": "2026-10-09", "time": "10:30", "doctor": "Dr. X",
              "d1": "Dr. A", "d2": "Dr. B", "t1": "10:00", "t2": "17:00", "fee": 50000, "amount": 50000,
              "price": 30000, "ref": "KR-1", "hold": 10, "test": "CBC", "patient": "Ravi", "specialty": "ENT",
              "text": "x", "token": "5", "ahead": 2, "interest": "cardiology", "emergency": "108",
              "whatsapp_sent": True, "name": "Ravi",
              "appt": {"doctor_name": "Dr. X", "date": "2026-10-09", "time": "10:30", "status": "pending_payment"},
              "appts": [{"doctor_name": "Dr. X", "date": "2026-10-09", "time": "10:30"}]}
    labels = {k: v["label"] for k, v in SPECIALTIES.items()}
    for key in R.T:
        for lang in ("te-IN", "hi-IN", "en-IN", "ta-IN"):
            out = R.realize(key, lang, TODAY, labels, **sample)
            assert out and "{" not in out, (key, lang, out)
