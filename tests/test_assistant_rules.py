"""KK-P1-06: Kriya Ketu deterministic router and resolvers (KK-FR-010..012, 015..017; RT-01..14).

The corpus (tests/fixtures/assistant_corpus.json, en / te-Latin / Telugu /
hi-Latin / Devanagari / code-mix) must route at >= 95 % with ZERO phrases routed
to a write, and no phrase may reach the LLM gateway (RT-12). Action phrases join
the corpus with their actions in P4 (KK-P4-01: the R0 reads); the trigger/slot
engine is also exercised here with synthetic specs.
"""

import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from app.assistant import resolve, rules
from app.assistant.registry import REGISTRY, ActionSpec, SlotSpec
from app.voice.dates import IST

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "assistant_corpus.json").read_text(encoding="utf-8"))
CORPUS, DOCTORS = _FIXTURE["phrases"], _FIXTURE["doctors"]


def _label(r) -> str:
    if r.kind in ("nav", "help"):
        return f"{r.kind}:{r.page}"
    if r.kind == "rejected":
        return r.error_code
    if r.kind == "action":
        return f"action:{r.action_id}"
    return r.kind


def _route(entry):
    return rules.route(entry["text"], visible_pages=entry.get("visible"), doctors=DOCTORS)


def test_corpus_is_big_and_multilingual():
    assert len(CORPUS) >= 200
    for name, rng in {"telugu": "ఀ-౿", "devanagari": "ऀ-ॿ"}.items():
        assert sum(bool(re.search(f"[{rng}]", e["text"])) for e in CORPUS) >= 10, name


def test_corpus_accuracy_and_zero_wrong_writes():
    misses = [(e["text"], e["expect"], _label(_route(e))) for e in CORPUS if _label(_route(e)) != e["expect"]]
    accuracy = 1 - len(misses) / len(CORPUS)
    assert accuracy >= 0.95, f"{accuracy:.1%} misses={misses}"
    # A phrase may reach a write only when that exact write is what it asks for (the preview still
    # needs a confirm); anything else routed to a write is a wrong write.
    wrong = [e["text"] for e in CORPUS if (r := _route(e)).kind == "action" and REGISTRY[r.action_id].risk != "R0"
             and e["expect"] != f"action:{r.action_id}"]
    assert wrong == []


@pytest.mark.parametrize("entry", [e for e in CORPUS if e["expect"] in ("negated", "clinical", "hypothetical")],
                         ids=lambda e: e["text"])
def test_safety_classes_are_exact(entry):
    """Negation, clinical and hypothetical phrases have no tolerance: every one must be caught."""
    assert _label(_route(entry)) == entry["expect"]


def test_no_corpus_phrase_reaches_the_llm():
    with patch("app.services.ai_gateway.call_ai_gateway", side_effect=AssertionError("LLM called")):
        for e in CORPUS:
            _route(e)


# ── engine with synthetic specs (P4 adds real ones) ───────────────────────────

def _noop(*a, **k):
    return None


LEAVE = ActionSpec(id="doctor_leave.create", module="leaves", summary="Add leave", risk="R2",
                   permission="DOCTOR_LEAVES_CREATE", perm_source="dependency", call=_noop, verify=_noop,
                   endpoint=("POST", "/admin/leaves"), page="leaves",
                   triggers=("on leave", "leave", "chutti", "selavu", "సెలవు", "छुट्टी"),
                   slots=(SlotSpec("doctor", "doctor"), SlotSpec("date", "date")))
LIST = ActionSpec(id="appointments.search", module="appointments", summary="Appointments", risk="R0",
                  call=_noop, endpoint=("GET", "/admin/appointments"), page="appointments",
                  triggers=("appointments", "bookings"), slots=(SlotSpec("date", "date", required=False),))
CHECKIN = ActionSpec(id="appointment.check_in", module="appointments", summary="Check in", risk="R1",
                     call=_noop, verify=_noop, endpoint=("POST", "/admin/appointments/{appointment_id}/check-in"),
                     page="appointments", triggers=("check in", "checkin", "arrived"))
DOCTORS = [{"id": "d1", "name": "Dr. Srinivas Rao"}, {"id": "d2", "name": "Dr. Anitha Reddy"},
           {"id": "d3", "name": "Dr. Srinivas Kumar"}]
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=IST)


def _r(text, **kw):
    return rules.route(text, actions={s.id: s for s in (LEAVE, LIST, CHECKIN)}, doctors=DOCTORS, now=NOW, **kw)


def test_unique_trigger_with_all_slots_is_an_action():
    r = _r("Dr Anitha is on leave tomorrow")
    assert (r.kind, r.action_id) == ("action", "doctor_leave.create")
    assert r.params == {"doctor": "Dr. Anitha Reddy", "date": "2026-10-15"}


def test_missing_slot_is_a_clarify_form_without_llm():
    r = _r("Dr Anitha is on leave")
    assert (r.kind, r.action_id, r.missing) == ("clarify", "doctor_leave.create", ("date",))


def test_ambiguous_doctor_is_never_auto_picked():
    r = _r("Dr Srinivas is on leave tomorrow")
    assert r.kind == "clarify" and r.missing == ("doctor",)
    assert {c["id"] for c in r.candidates} == {"d1", "d3"}


def test_optional_slot_may_be_absent():
    r = _r("list appointments")  # "show appointments" alone is navigation (a page name + nav words)
    assert (r.kind, r.action_id, r.params) == ("action", "appointments.search", {})


def test_tie_is_broken_by_the_current_page_else_clarified():
    tie = _r("leave appointments")
    assert tie.kind == "clarify" and set(tie.candidate_actions) == {"doctor_leave.create", "appointments.search"}
    assert _r("leave appointments", ui_page="leaves").action_id == "doctor_leave.create"


def test_question_about_a_write_is_help_not_a_write():
    assert _r("can you check in Ravi?").kind == "help"


def test_question_about_a_read_is_answered():
    assert _r("which appointments today?").action_id == "appointments.search"


def test_negated_write_never_routes():
    assert _r("Dr Anitha leave vaddu").error_code == "negated"


def test_hypothetical_write_never_routes():
    assert _r("what if Dr Anitha is on leave tomorrow").error_code == "hypothetical"


def test_nav_never_wins_over_extra_words():
    assert _r("open leaves for Dr Anitha tomorrow").kind != "nav"


def test_an_action_outside_the_permitted_set_is_invisible():
    r = rules.route("Dr Anitha is on leave tomorrow", actions={}, doctors=DOCTORS, now=NOW)
    assert r.kind == "none"


# ── resolvers ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("tomorrow", date(2026, 10, 15)), ("repu", date(2026, 10, 15)), ("today", date(2026, 10, 14)),
    ("ellundi", date(2026, 10, 16)), ("no date here", None),
])
def test_resolve_date(text, expected):
    assert resolve.resolve_date(text, now=NOW) == expected


@pytest.mark.parametrize("text, expected", [
    ("raise prices 10%", ("percent", Decimal("10"))), ("by 12.5 percent", ("percent", Decimal("12.5"))),
    ("10 శాతం", ("percent", Decimal("10"))), ("5 प्रतिशत", ("percent", Decimal("5"))),
    ("set CBC to ₹450", ("amount", Decimal("450"))), ("rs. 1,200", ("amount", Decimal("1200"))),
    ("Rs 300", ("amount", Decimal("300"))), ("500 rupees", ("amount", Decimal("500"))),
    ("₹0", None), ("0%", None), ("-5%", None), ("no number", None), ("price 450", None),
])
def test_resolve_amount(text, expected):
    assert resolve.resolve_amount(text) == expected


def test_resolve_doctor_returns_all_matches_on_ambiguity():
    assert [d["id"] for d in resolve.resolve_doctor("Dr Anitha", DOCTORS)] == ["d2"]
    assert {d["id"] for d in resolve.resolve_doctor("Srinivas", DOCTORS)} == {"d1", "d3"}
    assert resolve.resolve_doctor("nobody", DOCTORS) == []


def test_pages_mentioned_prefers_the_longest_name():
    assert rules.pages_mentioned("open payment settings") == ["paysettings"]
    assert rules.pages_mentioned("treatment plans") == ["dentalplans"]
    assert rules.pages_mentioned("home visits") == ["homecollect"]
