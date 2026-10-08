"""Callers' questions about the hospital (app/voice/knowledge.py + dialog._answer_question).

Includes a replay of production call CALL-20261008-EC8BEF (Aura Dental): the
caller asked about services and treatments while confirming a booking, was
not understood twice, and was transferred."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.voice import knowledge as K
from app.voice.nlu_rules import NluContext, gurmukhi_to_devanagari, is_question, understand

from .test_dialog import DOCS, NOW, SLOTS, Conv, FakeTools

TREATMENTS = ["Dental Check-up", "Scaling & Polishing", "Root Canal Treatment", "Tooth Filling"]
CTX = NluContext(doctors=DOCS, departments=["Cardiology", "Gynecology", "Dental"], now=NOW, treatments=TREATMENTS)


def _intents(text, expect=None):
    return understand(text, CTX, expect).intents


# ---- understanding ----

@pytest.mark.parametrize("text", [
    "ఎటువంటి సర్వీసెస్ మీరు ప్రొవైడ్ చేస్తారు?",            # production, turn 207
    "ఆవిడ ఏమేమి ట్రీట్మెంట్స్ ట్రీట్ చేస్తారు రూట్?",         # production, turn 214
    "What services do you provide?",
    "Do you do root canal treatment?",
    "root canal treatment entha?",
    "आप कौन कौन सी सेवाएं देते हैं?",
    "parking undha?",
])
def test_questions_about_the_hospital_are_knowledge_questions(text):
    assert "KNOWLEDGE_QUESTION" in _intents(text, expect="confirm")


@pytest.mark.parametrize("text, want", [
    ("Dental appointment jouttu", "BOOK_APPOINTMENT"),
    ("naaku doctor kavali", "BOOK_APPOINTMENT"),
    ("What are your timings?", "HOSPITAL_INFORMATION"),
    ("consultation fee entha?", "FEES"),
    ("CBC test price?", "LAB_TEST_SEARCH"),
    ("root canal treatment ki appointment kavali", "BOOK_APPOINTMENT"),
])
def test_existing_requests_are_not_taken_for_questions(text, want):
    got = _intents(text)
    assert want in got and "KNOWLEDGE_QUESTION" not in got, got


def test_gurmukhi_yes_from_stt_is_understood_as_yes():
    assert gurmukhi_to_devanagari("ਹਾਂ ਹਾਂ।") == "हां हां।"
    assert "AFFIRM" in _intents(gurmukhi_to_devanagari("ਹਾਂ ਹਾਂ।"), expect="confirm")
    assert "DENY" in _intents(gurmukhi_to_devanagari("ਨਹੀਂ"), expect="confirm")
    assert gurmukhi_to_devanagari("నమస్కారం hello") == "నమస్కారం hello"


def test_is_question():
    assert is_question("ఆవిడ ఏమేమి ట్రీట్మెంట్స్ చేస్తారు") and is_question("tell me about Dr. Meena")
    assert not is_question("Dr Meena kavali")


# ---- the replayed call ----

class KbTools(FakeTools):
    def __init__(self, answer="We offer Dental Check-up, Scaling & Polishing, Root Canal Treatment.", **kw):
        super().__init__(**kw)
        self.asked = []
        self._answer = answer

    async def answer_question(self, question, lang, entities, focus):
        self.asked.append((question, lang, focus))
        return {"text": self._answer, "source": "ai"} if self._answer else {"text": None, "source": "none"}


def _at_confirm(tools, lang="te-IN"):
    c = Conv(tools, lang=lang)
    # Where the production call stood: one slot offered, waiting for yes/no.
    c.state["task"] = {"wf": "BOOKING", "intent": "BOOK_APPOINTMENT",
                       "slots": {"department": "Cardiology", "date": "2026-10-07", "time_period": "MORNING",
                                 "selected": {**SLOTS[0], "doctor_name": "Dr. Meena Patel", "department": "Dental"}}}
    c.state["expect"] = "confirm"
    c.state["last_question"] = {"key": "present_one", "params": {"date": "2026-10-07", "time": "11:30",
                                                                 "doctor": "Dr. Meena Patel", "fee": 50000}}
    return c


def test_services_question_mid_confirmation_is_answered_then_the_booking_resumes():
    t = KbTools()
    c = _at_confirm(t)
    reply = c.say("ఎటువంటి సర్వీసెస్ మీరు ప్రొవైడ్ చేస్తారు?")
    assert "We offer Dental Check-up" in reply
    assert "Meena Patel" in reply and c.state["expect"] == "confirm"     # same booking question again
    assert c.state["failures"] == 0 and c.control == "continue"
    # "what treatments does SHE do": "she" is the doctor being booked.
    reply = c.say("ఆవిడ ఏమేమి ట్రీట్మెంట్స్ ట్రీట్ చేస్తారు రూట్?")
    assert t.asked[-1][2]["doctor_name"] == "Dr. Meena Patel"
    assert c.control == "continue" and c.state["expect"] == "confirm"
    # and the Gurmukhi "yes" now books.
    c.say(gurmukhi_to_devanagari("ਹਾਂ ਹਾਂ।"))
    assert c.state["outcomes"][-1]["outcome"] in ("booked", "payment_link_sent")


def test_three_questions_in_a_row_never_transfer():
    c = _at_confirm(KbTools(answer=None))
    for _ in range(4):
        reply = c.say("What services do you provide?")
        assert "ఆ వివరం ప్రస్తుతం నా దగ్గర లేదు" in reply
    assert c.control == "continue"


def test_question_with_no_task_offers_a_booking():
    c = Conv(KbTools(), lang="en-IN")
    reply = c.say("What services do you provide?")
    assert "We offer" in reply and c.state["expect"] == "offer"


def test_engine_without_the_tool_degrades_to_dont_know():
    c = _at_confirm(FakeTools(), lang="en-IN")
    reply = c.say("What services do you provide?")
    assert "don't have that detail" in reply and c.state["expect"] == "confirm"


# ---- the knowledge base ----

KB = {
    "facts": [
        {"id": "F1", "kind": "hospital", "text": "Hospital name: Aura Dental Hospital."},
        {"id": "F2", "kind": "doctor", "text": "Doctor: Dr. Meena Patel; department Dental; Dentist; qualifications "
                                               "BDS, MDS Oral Surgery; 6 years of experience; consultation fee Rs 500."},
        {"id": "F3", "kind": "treatment", "text": "Treatment: Root Canal Treatment; removes infected pulp; about 60 "
                                                  "minutes per visit; the doctor examines first, then explains the plan, "
                                                  "number of sittings and cost; price is told after the doctor's examination."},
        {"id": "F4", "kind": "qa", "text": "Q: Is there parking? A: Yes, free two-wheeler and car parking in the basement."},
    ],
    "doctors": [{"id": "d9", "name": "Dr. Meena Patel", "department": "Dental", "specialization": "Dentist",
                 "qualifications": "BDS, MDS Oral Surgery", "experience_years": 6}],
    "treatments": [{"id": "t1", "name": "Root Canal Treatment", "description": "Removes infected pulp.",
                    "concerns": "tooth pain, root canal, rct", "price_from_paise": 0},
                   {"id": "t2", "name": "Scaling & Polishing", "description": "Cleaning.", "price_from_paise": 80000}],
    "qa": [{"id": "q1", "question": "Is there parking available?", "answer": "Yes, free parking in the basement.",
            "language": None},
           {"id": "q2", "question": "Do you accept insurance?", "answer": "बीमा स्वीकार है।", "language": "hi"}],
    "departments": ["Dental"],
    "doctor_treatments": {},
}


@pytest.mark.parametrize("answer, ids, reason", [
    ("Dr. Meena Patel has 6 years of experience.", ["F2"], None),
    ("Root canal costs Rs 3000.", ["F3"], "number_not_in_facts:3000"),
    ("Dr. Rajesh does root canals.", ["F3"], "doctor_not_in_facts:rajesh"),
    ("Take an antibiotic before the visit.", ["F3"], "advice_word:antibiotic"),
    ("Root canal removes infected pulp.", [], "no_facts_cited"),
    ("Root canal removes infected pulp.", ["F99"], "no_facts_cited"),
    ("See https://x.y for details.", ["F1"], "url"),
    ("డాక్టర్ Meena గారు 6 సంవత్సరాల అనుభవం.", ["F2"], None),
])
def test_verifier(answer, ids, reason):
    assert K.verify(answer, KB, "question", ids) == reason


def test_clinic_qa_matches_only_clearly_and_in_its_language():
    assert K.match_qa("is parking available there?", KB, "en")["id"] == "q1"
    assert K.match_qa("what about insurance?", KB, "en") is None           # one shared word, hi-only entry
    assert K.match_qa("do you accept insurance please", KB, "te") is None  # language-pinned to hi


def test_template_answers_from_records():
    assert K.template_answer("root canal gurinchi cheppandi", KB, "en", {}, {}).startswith("Root Canal Treatment:")
    assert "examines first" in K.template_answer("rct?", KB, "en", {}, {})
    assert "Rs 800" in K.template_answer("scaling & polishing price", KB, "en", {}, {})
    doc = K.template_answer("what does she do", KB, "te", {}, {"doctor_name": "Dr. Meena Patel"})
    assert doc.startswith("డాక్టర్ Meena Patel గారు") and "Root Canal" not in doc   # no unlinked treatments
    assert K.template_answer("What services do you provide?", KB, "en", {}, {}).startswith("We offer Root Canal")
    assert K.template_answer("how is the weather", KB, "en", {}, {}) is None


def _llm(content):
    return AsyncMock(return_value={"choices": [{"message": {"content": json.dumps(content)}}],
                                   "usage": {"total_tokens": 900}})


def test_answer_prefers_clinic_qa_then_verified_ai_then_template():
    run = asyncio.run
    gw = _llm({"answer": "We have Dr. Meena Patel, a dentist with 6 years of experience.", "fact_ids": ["F2"]})
    with patch("app.services.ai_gateway.call_ai_gateway", new=gw), \
         patch("app.services.ai_gateway.calculate_cost_paise", return_value=12):
        qa = run(K.answer("Is there parking available?", KB, "en", "c1"))
        ai = run(K.answer("Who is your dentist?", KB, "en", "c1"))
    assert qa["source"] == "qa" and gw.await_count == 1
    assert ai["source"] == "ai" and ai["tokens"] == 900 and ai["cost_paise"] == 12
    bad = _llm({"answer": "Root canal is Rs 4500 in 2 sittings.", "fact_ids": ["F3"]})
    with patch("app.services.ai_gateway.call_ai_gateway", new=bad), \
         patch("app.services.ai_gateway.calculate_cost_paise", return_value=0):
        res = run(K.answer("root canal cost?", KB, "en", "c1"))
    assert res["source"] == "template" and res["reject"].startswith("number_not_in_facts")
    assert "4500" not in res["text"]
    with patch("app.services.ai_gateway.call_ai_gateway", new=AsyncMock(side_effect=TimeoutError())):
        res = run(K.answer("What is the meaning of life?", KB, "en", "c1"))
    assert res == {"text": None, "source": "none", "tokens": 0, "cost_paise": 0, "reject": "llm_unavailable"}


def test_facts_are_capped():
    big = {**KB, "facts": [{"id": f"F{i}", "kind": "qa", "text": "x" * 500} for i in range(100)]}
    assert len(K.facts_text(big)) <= K.MAX_FACTS_CHARS


# ---- session: "one moment" while answering, Gurmukhi never switches the language ----

from app.voice.session import CallSession  # noqa: E402

from .test_io_layer import ctx  # noqa: E402


def _session_patches():
    return [patch("app.database.get_doctors", new=AsyncMock(return_value=DOCS)),
            patch("app.voice.session.store.load_lexicon", new=AsyncMock(return_value=[])),
            patch("app.voice.session.store.update_call", new=AsyncMock()),
            patch("app.voice.session.store.add_event", new=AsyncMock()),
            patch("app.voice.session.store.get_call", new=AsyncMock(return_value={})),
            patch("app.voice.session.understand_llm", new=AsyncMock(side_effect=AssertionError("rules hear these")))]


@pytest.mark.asyncio
async def test_session_says_one_moment_only_for_questions():
    s = CallSession(ctx(lang="en-IN"), tools=KbTools(), now=NOW)
    s.state["lang"] = "en-IN"
    said = []

    async def interim(texts):
        said.append(texts)
    s.interim = interim
    ps = _session_patches()
    for p in ps:
        p.start()
    try:
        r = await s.handle("What services do you provide?")
        assert said == [["One moment, let me check."]] and "We offer" in " ".join(r.texts)
        await s.handle("Naaku cardiology doctor kavali")
        assert len(said) == 1                                   # a request gets no filler
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_gurmukhi_turns_never_switch_the_call_to_hindi():
    s = CallSession(ctx(), tools=KbTools(), now=NOW)
    ps = _session_patches()
    for p in ps:
        p.start()
    try:
        for _ in range(3):
            await s.handle("ਸੋ।")
        assert s.state["lang"] == "te-IN"
    finally:
        for p in ps:
            p.stop()


# ---- admin routes ----

from tests.voice.test_voice_routes import ADMIN, CID, VIEWER, client  # noqa: E402


def test_knowledge_routes_need_manage_to_write_and_are_tenant_scoped():
    c, p = client(VIEWER)
    try:
        assert c.post(f"/admin/voice/knowledge?clinic_id={CID}",
                      json={"question": "Is there parking?", "answer": "Yes."}).status_code == 403
        assert c.delete(f"/admin/voice/knowledge/x?clinic_id={CID}").status_code == 403
    finally:
        p.stop()
    c, p = client(ADMIN)
    try:
        sb = AsyncMock(return_value=type("R", (), {"data": []})())
        with patch("app.routers.voice_admin.sb", new=sb), \
             patch("app.routers.voice_admin.supabase") as fake:
            assert c.delete(f"/admin/voice/knowledge/e1?clinic_id={CID}").status_code == 404
        eqs = [call.args for call in fake.table.return_value.delete.return_value.eq.call_args_list]
        assert eqs[0] == ("clinic_id", CID)
        assert c.post(f"/admin/voice/knowledge?clinic_id={CID}",
                      json={"question": "Is there parking?", "answer": "Yes.", "language": "fr"}).status_code == 422
    finally:
        p.stop()


def test_ask_preview_blocks_medical_questions_before_any_lookup():
    c, p = client(VIEWER)
    try:
        with patch("app.voice.knowledge.load", new=AsyncMock(side_effect=AssertionError("must not load"))):
            r = c.post(f"/admin/voice/knowledge/ask?clinic_id={CID}",
                       json={"question": "I have chest pain, what should I do?", "language": "en"})
        assert r.status_code == 200 and r.json()["source"] == "safety"
        with patch("app.voice.knowledge.load", new=AsyncMock(return_value=KB)), \
             patch("app.voice.knowledge._grounded", new=AsyncMock(return_value=(None, 0, 0, "llm_unavailable"))):
            r = c.post(f"/admin/voice/knowledge/ask?clinic_id={CID}",
                       json={"question": "Is there parking available?", "language": "en"})
        assert r.json() == {"text": "Yes, free parking in the basement.", "source": "qa", "rejected": None}
    finally:
        p.stop()
