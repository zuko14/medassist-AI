"""The Continuous Care WhatsApp hook (ADR-CC-09 revised, VERIFICATION D1/D2, X18; TEST-PLAN P4).

`cc:` ids reach the hook as BOTH template quick replies ('button') and in-window taps ('interactive'); other payloads
(dentrev:, rx_refill:, checkin_*) are never claimed. A typed number is stored only when all four conditions hold:
the whole message is a reading, the session is idle / main menu, this phone has an open reading prompt (≤ 12 h), and
the number fits that prompt's measure. Devanagari / Telugu digits are read; two family prompts → "whose reading";
any error inside returns False so the patient's message continues normally.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import cc_hooks
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
from tests.cc_world import care, patient, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _m():
    from app.services.whatsapp import whatsapp_service
    return SimpleNamespace(whatsapp=whatsapp_service)


def _clinic(care):
    invalidate_tenant_cache()
    return asyncio.run(get_clinic_by_id(care.clinic))


def _open(care, e, phone, kind="reading", measure="fbg", status="sent", hours_ago=0):
    return str(_q(care.conn, "INSERT INTO cc_prompts (clinic_id, enrollment_id, kind, measure_code, channel, phone, "
                             "status, sent_at, expires_at) VALUES (%s, %s, %s, %s, 'whatsapp_interactive', %s, %s, "
                             "now() - make_interval(hours => %s), now() + interval '6 hours') RETURNING id",
                  care.clinic, e["id"], kind, measure, phone, status, hours_ago))


def _typed(care, phone, text, state="main_menu", mid=None):
    return asyncio.run(cc_hooks.handle_typed(_m(), _clinic(care), phone, state, text, mid or "wamid." + uuid.uuid4().hex))


def _obs(care, e):
    return _q(care.conn, "SELECT coalesce(json_agg(json_build_array(measure_code, value_num, value_num2, source) "
                         "ORDER BY created_at), '[]') FROM cc_observations WHERE enrollment_id = %s", e["id"])


# ── wiring: one block outside the button branch ──────────────────────────────

@pytest.mark.parametrize("message_type", ["button", "interactive"])
def test_cc_taps_reach_the_hook_as_button_and_interactive(message_type):
    from app.services.conversation import ConversationManager
    clinic = {"id": "c1", "name": "H", "config": {}, "plan": "polyclinic", "account_type": "tenant",
              "features": {"opd_enabled": True, "care_enabled": True}}
    session = {"state": "selecting_slot", "context": {}, "last_processed_message_id": None,
               "booking_context_expires_at": None}
    tap = AsyncMock(return_value=True)
    m = ConversationManager()
    m.whatsapp = AsyncMock()
    tid = f"cc:adh:{uuid.uuid4()}:taken"
    with patch.object(cc_hooks, "handle_tap", tap), \
            patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=session)), \
            patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value={"id": "p", "language": "en"})), \
            patch("app.services.conversation.update_conversation", new=AsyncMock()) as upd, \
            patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="unknown")):
        asyncio.run(m._handle_message_locked(clinic=clinic, phone="+919800000001", message="Taken", message_type=message_type,
                                             message_id="wamid.T1", interactive_data={"id": tid, "type": "button"}))
    tap.assert_awaited_once()
    assert tap.await_args.args[2:4] == ("+919800000001", tid)
    m.whatsapp.send_text.assert_not_awaited()                    # claimed: nothing else answered
    assert not [c for c in upd.await_args_list if "state" in (c.args[2] if len(c.args) > 2 else {})]  # no state write


@pytest.mark.parametrize("payload", ["dentrev:abc:yes", "rx_refill:on:123", "checkin_better", "slot_10:00", ""])
def test_other_payloads_are_never_claimed(care, payload):  # noqa: F811
    assert asyncio.run(cc_hooks.handle_tap(_m(), _clinic(care), "+919800000001", payload, "w")) is False


def test_malformed_and_foreign_cc_taps_are_absorbed_without_effect(care):  # noqa: F811
    e, pid = care.enroll()
    p = _open(care, e, care.phone(pid), kind="adherence", measure=None)
    clinic = _clinic(care)
    assert asyncio.run(cc_hooks.handle_tap(_m(), clinic, care.phone(pid), "cc:adh:not-a-uuid:taken", "w1")) is True
    assert asyncio.run(cc_hooks.handle_tap(_m(), clinic, "+919811111111", f"cc:adh:{p}:taken", "w2")) is True
    assert _obs(care, e) == []                                    # a stranger's tap on this prompt stores nothing
    assert asyncio.run(cc_hooks.handle_tap(_m(), clinic, care.phone(pid), f"cc:adh:{p}:taken", "w3")) is True
    assert _obs(care, e) == [["adherence", None, None, "whatsapp_button"]]
    assert _q(care.conn, "SELECT status FROM cc_prompts WHERE id = %s", p) == "answered"


# ── the four-condition numeric claim ─────────────────────────────────────────

def test_reading_with_an_open_prompt_is_stored(care):  # noqa: F811
    e, pid = care.enroll()
    p = _open(care, e, care.phone(pid))
    assert _typed(care, care.phone(pid), " 142 ") is True
    assert _obs(care, e) == [["fbg", 142.0, None, "whatsapp_typed"]]
    assert _q(care.conn, "SELECT status FROM cc_prompts WHERE id = %s", p) == "answered"
    assert "142" not in care.wa.of("text")[-1].args[0] and "saved" in care.wa.of("text")[-1].args[0]


@pytest.mark.parametrize("text,state,prompt", [
    ("142", "main_menu", None),                       # no open prompt
    ("142", "selecting_slot", "fbg"),                 # mid-booking: "12" / "142" may be a slot or a choice
    ("12", "selecting_family_member", "fbg"),
    ("sugar 142 dizzy", "main_menu", "fbg"),          # not the whole message
    ("140/90", "main_menu", "fbg"),                   # BP shape for a glucose prompt
    ("142", "main_menu", "bp"),                       # glucose shape for a BP prompt
    ("142", "main_menu", "stale"),                    # prompt older than 12 h
    ("hello", "main_menu", "fbg"),
])
def test_not_claimed_unless_all_four_conditions_hold(care, text, state, prompt):  # noqa: F811
    e, pid = care.enroll()
    if prompt == "stale":
        _open(care, e, care.phone(pid), hours_ago=13)
    elif prompt:
        _open(care, e, care.phone(pid), measure=prompt)
    assert _typed(care, care.phone(pid), text, state) is False
    assert _obs(care, e) == []


def test_bp_reading(care):  # noqa: F811
    e, pid = care.enroll()
    _open(care, e, care.phone(pid), measure="bp")
    assert _typed(care, care.phone(pid), "140 / 90") is True
    assert _obs(care, e) == [["bp", 140.0, 90.0, "whatsapp_typed"]]


@pytest.mark.parametrize("text", ["१४२", "౧౪౨"])
def test_indic_digits(care, text):  # noqa: F811
    e, pid = care.enroll()
    _open(care, e, care.phone(pid))
    assert _typed(care, care.phone(pid), text) is True
    assert _obs(care, e) == [["fbg", 142.0, None, "whatsapp_typed"]]


def test_unreadable_number_after_send_reading_is_reasked_then_unparsed(care):  # noqa: F811
    e, pid = care.enroll()
    p = _open(care, e, care.phone(pid), status="awaiting_value")
    for i in range(2):
        assert _typed(care, care.phone(pid), "1420") is True
        assert "check it" in care.wa.of("text")[-1].args[0]
    assert _q(care.conn, "SELECT status FROM cc_prompts WHERE id = %s", p) == "unparsed" and _obs(care, e) == []


def test_implausible_number_is_reasked(care):  # noqa: F811
    e, pid = care.enroll()
    p = _open(care, e, care.phone(pid))
    assert _typed(care, care.phone(pid), "15") is True                       # fasting sugar 15: not plausible
    assert _obs(care, e) == [] and _q(care.conn, "SELECT status FROM cc_prompts WHERE id = %s", p) == "sent"


def test_two_family_prompts_ask_whose_reading(care):  # noqa: F811
    api, c = care.api, care.conn
    e1, holder = care.enroll()
    fm = str(_q(c, "INSERT INTO family_members (clinic_id, primary_phone, full_name) VALUES (%s, %s, 'Ravi Kumar') "
                   "RETURNING id", care.clinic, care.phone(holder)))
    api.as_(care.u.doctor)
    e2 = api.call("POST", "/admin/cc/enrollments", care.clinic, {"patient_id": holder, "family_member_id": fm,
                                                                   "protocol_code": "t2dm", "primary_doctor_id": care.doctor,
                                                                   "wa_optin_method": "in_person"}).json()
    api.as_(care.u.coord)
    api.call("POST", f"/admin/cc/enrollments/{e2['id']}/consent/counter", care.clinic,
             {"notice_version": "cc-notice-1", "guardian_relation": "son"})
    p1, p2 = _open(care, e1, care.phone(holder)), _open(care, e2, care.phone(holder))
    assert _typed(care, care.phone(holder), "142") is True
    q = care.wa.of("buttons")[-1]
    assert sorted(b["id"] for b in q.args[1]) == sorted([f"cc:who:{p1}", f"cc:who:{p2}"])
    assert {b["title"] for b in q.args[1]} == {"Lakshmi", "Ravi"} and _obs(care, e1) == _obs(care, e2) == []
    assert asyncio.run(cc_hooks.handle_tap(_m(), _clinic(care), care.phone(holder), f"cc:who:{p2}", "wamid.W1")) is True
    assert _obs(care, e2) == [["fbg", 142.0, None, "whatsapp_typed"]] and _obs(care, e1) == []


def test_any_error_returns_false_so_the_message_continues(care, monkeypatch):  # noqa: F811
    e, pid = care.enroll()
    _open(care, e, care.phone(pid))
    clinic = _clinic(care)

    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(cc_hooks, "sb", boom)
    assert asyncio.run(cc_hooks.handle_typed(_m(), clinic, care.phone(pid), "main_menu", "142", "w")) is False
    assert asyncio.run(cc_hooks.handle_tap(_m(), clinic, care.phone(pid), f"cc:adh:{uuid.uuid4()}:taken", "w")) is True


def test_duplicate_webhook_stores_one_observation(care):  # noqa: F811
    e, pid = care.enroll()
    _open(care, e, care.phone(pid))
    _typed(care, care.phone(pid), "142", mid="wamid.DUP")
    _open(care, e, care.phone(pid))
    _typed(care, care.phone(pid), "142", mid="wamid.DUP")
    assert len(_obs(care, e)) == 1


def test_phq9_flow_records_total_and_item9(care):  # noqa: F811
    from tests.cc_world import activate_pack
    activate_pack(care.api, care.clinic, care.u, "phq9", cards=())
    e, pid = care.enroll("phq9")
    p = _open(care, e, care.phone(pid), kind="phq9", measure="phq9")
    clinic, phone = _clinic(care), care.phone(pid)
    asyncio.run(cc_hooks.handle_tap(_m(), clinic, phone, f"cc:phq:{p}:start", "w0"))
    answers = [1, 2, 1, 0, 1, 2, 0, 1, 0]
    for n, v in enumerate(answers, start=1):
        asyncio.run(cc_hooks.handle_tap(_m(), _clinic(care), phone, f"cc:phq:{p}:q{n}:{v}", f"w{n}"))
    assert _q(care.conn, "SELECT value_num::int || ':' || value_code FROM cc_observations WHERE enrollment_id = %s",
              e["id"]) == f"{sum(answers)}:q9=0"
    assert len(care.wa.of("list")) == 9                               # one question per message, in-window lists
    assert _q(care.conn, "SELECT status FROM cc_prompts WHERE id = %s", p) == "answered"
