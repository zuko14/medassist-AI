"""Session 56: the receptionist offers only what THIS hospital's plan sells (app/voice/scope.py).

A diagnostic centre books tests and never offers a doctor; a clinic without lab booking never takes
a test booking; a hospital with both is unchanged. Unknown plans and failed reads keep everything
(FULL): plan scoping can never narrow a call by accident.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.voice.dialog import DialogEngine, new_state
from app.voice.scope import FULL, Scope, route, scope_for
from app.voice.session import CallSession
from app.voice.tools import CallContext

from .test_dialog import DOCS, SLOTS, Conv, FakeTools

DIAG = Scope(doctors=False, labs=True)
CLINIC_ONLY = Scope(doctors=True, labs=False)
NONE = Scope(doctors=False, labs=False)


class CatalogueTools(FakeTools):
    """lab_tests matches by the test's name, as the real catalogue search does."""

    async def lab_tests(self, query):
        self.calls.append(("lab_tests", query))
        return [{"id": "t1", "name": "CBC", "price_paise": 35000}] if "cbc" in (query or "").lower() else []


def conv(scope, lang="en-IN", **kw):
    c = Conv(CatalogueTools(slots=SLOTS, **kw), lang=lang)
    c.engine.clinic["scope"] = scope
    return c


# ---- the plan decides (mirrors WhatsApp's _is_diagnostics_only / help guide) ----

@pytest.mark.parametrize("plan, doctors, kind", [
    ("diagstream", None, "diagnostic"),
    ("diagbooking", DOCS, "diagnostic"),          # doctor rows never turn a diagnostic plan into a clinic
    ("soloclinic", DOCS, "clinic"),
    ("essential", DOCS, "clinic"),                # has lab REPORTS, not lab BOOKING
    ("polyclinic", DOCS, "hospital"),
    ("polyclinic", [], "diagnostic"),             # labs and no doctors at all: WhatsApp's diagnostics-only
    ("polyclinic", None, "hospital"),             # doctor read failed / unknown: never narrower
    ("enterprise", [], "diagnostic"),
    ("enterprise", DOCS, "hospital"),
    ("multispecialty", DOCS, "hospital"),
    ("womenchild", DOCS, "hospital"),
    # Specialty hospitals (session 57): consultations + treatments; IVF also runs its own tests.
    ("derma", DOCS, "clinic"), ("eye", DOCS, "clinic"), ("dental", DOCS, "clinic"), ("ivf", DOCS, "hospital"),
])
def test_scope_follows_the_plan(plan, doctors, kind):
    assert scope_for({"id": "c1", "plan": plan}, doctors).kind == kind


def test_owner_overrides_are_respected():
    clinic = {"id": "c1", "plan": "soloclinic", "features": {"lab_test_booking": True}}
    assert scope_for(clinic, DOCS).kind == "hospital"
    clinic = {"id": "c1", "plan": "polyclinic", "features": {"lab_test_booking": False}}
    assert scope_for(clinic, DOCS).kind == "clinic"


@pytest.mark.parametrize("clinic", [None, {}, {"id": "c1"}, {"id": "c1", "plan": "brand-new-plan"}])
def test_unknown_clinic_or_plan_keeps_everything(clinic):
    assert scope_for(clinic, []) == FULL


def test_route_table():
    assert route("BOOKING", DIAG) == "LAB_BOOKING"
    assert route("FEES", DIAG) == "LAB_PRICE"
    assert route("RESCHEDULE", DIAG) == "RESCHEDULE"      # acts on the caller's own booking only
    assert route("LAB_BOOKING", CLINIC_ONLY) == "NOT_OFFERED"
    assert route("LAB_PRICE", CLINIC_ONLY) == "NOT_OFFERED"
    assert route("BOOKING", NONE) == "NOT_OFFERED"
    for wf in ("BOOKING", "FEES", "LAB_BOOKING", "LAB_PRICE", "REPORT", "INFO", "HUMAN", "CANCEL"):
        assert route(wf, FULL) == wf                       # a hospital: unchanged


# ---- a diagnostic centre ----

def test_diagnostic_appointment_request_books_a_test_never_a_doctor():
    c = conv(DIAG)
    r = c.say("I want to book an appointment for tomorrow")
    assert "Which test" in r
    assert not [x for x in c.tools.calls if x[0] in ("find_doctors", "find_slots", "book")]
    r = c.say("CBC")
    assert "CBC" in r and "Shall I book it" in r
    c.say("yes")
    assert [x for x in c.tools.calls if x[0] == "book_lab"] == [("book_lab", "t1", "2026-10-07", "Ravi Kumar")]


def test_diagnostic_fees_question_asks_which_test_then_prices_it():
    c = conv(DIAG)
    c.tools.lab_tests = AsyncMock(side_effect=[[], [{"id": "t1", "name": "Thyroid Profile", "price_paise": 60000}]])
    r = c.say("what is the fee")
    assert "Which test" in r and "Dr." not in r
    r = c.say("thyroid")
    assert "Thyroid Profile" in r and "600 rupees" in r


def test_diagnostic_help_and_clarify_speak_of_tests():
    c2 = conv(DIAG)
    texts = [c2.say(u) for u in ("asdf qwer", "zxcv uiop", "mnbv lkjh")]
    joined = " ".join(texts)
    assert "book a test" in joined and "appointment" not in joined


def test_diagnostic_human_request_offers_tests_not_appointments():
    c = conv(DIAG)
    r = c.say("connect me to reception")
    assert "book your test" in r and "appointment" not in r


def test_diagnostic_clinical_question_offers_the_doctors_test():
    c = conv(DIAG)
    out = c.engine.clinical(c.state)
    text = c.text(out)
    assert "test your doctor advised" in text and "doctor's appointment" not in text
    assert c.state["offer"] == {"wf": "LAB_BOOKING", "slots": {}}
    r = c.say("yes")
    assert "Which test" in r


def test_diagnostic_lead_call_offers_a_test():
    c = conv(DIAG)
    out = c.engine.greeting(c.state, outbound=True, interest="thyroid test",
                            slots={"test": {"id": "t9", "name": "Thyroid Profile", "price_paise": 60000}})
    text = c.text(out)
    assert "book a test" in text and "appointment" not in text
    assert c.state["offer"]["wf"] == "LAB_BOOKING" and c.state["offer"]["slots"]["test"]["id"] == "t9"
    r = c.say("yes")
    assert "Which day" in r                                  # the test is known: straight to the day


def test_diagnostic_test_reschedule_is_not_called_paid():
    appt = {"id": "a1", "booking_ref": "KR-1", "status": "confirmed", "paid": False, "doctor_id": None,
            "booking_type": "lab_test", "lab_test_name": "CBC", "date": "2026-10-08", "time": None}
    c = conv(DIAG, appts=[appt])
    r = c.say("I want to reschedule")
    assert "already paid" not in r and "change the date" in r


# ---- a clinic without lab booking ----

def test_clinic_without_labs_never_books_a_test():
    c = conv(CLINIC_ONLY)
    r = c.say("I want to book a CBC test tomorrow")
    assert "don't take test bookings" in r and "book an appointment" in r
    assert not [x for x in c.tools.calls if x[0] in ("lab_tests", "book_lab")]
    r = c.say("yes")
    assert "doctor or department" in r


def test_nothing_offered_goes_to_staff_honestly():
    c = conv(NONE)
    r = c.say("book an appointment tomorrow")
    assert "can't arrange that" in r and "reception" in r
    assert "tests and scans" not in r                       # nothing is claimed that this plan does not do
    out = c.engine.clinical(new_state("en-IN"))
    assert "Shall I book" not in c.text(out)


# ---- a hospital (both): exactly as before ----

def test_hospital_unchanged_doctor_booking_and_lab_booking():
    c = conv(FULL)
    r = c.say("I need a cardiologist tomorrow morning.")
    assert "two options" in r
    c2 = conv(FULL)
    r = c2.say("I want to book a CBC test")
    assert "Which day" in r and c2.state["task"]["slots"]["test"]["name"] == "CBC"
    assert c2.engine._offer_say({"wf": "LAB_BOOKING", "slots": {"test": {"name": "CBC"}}}).key == "offer_lab_booking"


def test_engine_without_scope_is_full():
    e = DialogEngine(FakeTools(), {"hospital": "X"})
    assert e.scope == FULL and e._k("help_menu") == "help_menu"


# ---- the session decides the scope once, from the clinic row ----

def _ctx(clinic):
    return CallContext(call_id="call-1", call_ref="CALL-1", clinic=clinic, branch_id=None,
                       caller_phone="+919876543210", reception_number=None)


def _patches(doctors):
    return [patch("app.voice.session.store.update_call", new=AsyncMock()),
            patch("app.voice.session.store.add_event", new=AsyncMock()),
            patch("app.voice.session.store.get_call", new=AsyncMock(return_value={})),
            patch("app.voice.session.store.load_lexicon", new=AsyncMock(return_value=[])),
            patch("app.database.get_doctors", new=doctors)]


@pytest.mark.asyncio
@pytest.mark.parametrize("plan, doctors, kind", [
    ("diagstream", AsyncMock(return_value=[]), "diagnostic"),
    ("polyclinic", AsyncMock(return_value=[]), "diagnostic"),
    ("polyclinic", AsyncMock(return_value=DOCS), "hospital"),
    ("polyclinic", AsyncMock(side_effect=RuntimeError("db down")), "hospital"),   # failure -> FULL
])
async def test_session_scope_from_clinic_row(plan, doctors, kind):
    s = CallSession(_ctx({"id": "c1", "name": "X Labs", "plan": plan}), tools=FakeTools(slots=SLOTS))
    ps = _patches(doctors)
    for p in ps:
        p.start()
    try:
        await s.start()
        await s.handle("hello")
    finally:
        for p in ps:
            p.stop()
    assert s.engine.scope.kind == kind


@pytest.mark.asyncio
async def test_session_diagnostic_lead_prefills_the_matching_test():
    s = CallSession(_ctx({"id": "c1", "name": "X Labs", "plan": "diagbooking"}), tools=CatalogueTools())
    s._scope = DIAG
    name, slots = await s._lead_context("CBC")
    assert slots == {"test": {"id": "t1", "name": "CBC", "price_paise": 35000}}


@pytest.mark.parametrize("scope", [FULL, DIAG])
def test_naming_the_test_keeps_the_booking_and_the_day(scope):
    """'Which test?' -> 'CBC' used to restart as a price question and lose 'tomorrow'."""
    c = conv(scope)
    r = c.say("I want to book a test tomorrow")
    assert "Which test" in r
    r = c.say("CBC")
    assert "CBC for Ravi Kumar, tomorrow" in r and "Shall I book it" in r
    c.say("yes")
    assert [x for x in c.tools.calls if x[0] == "book_lab"] == [("book_lab", "t1", "2026-10-07", "Ravi Kumar")]
