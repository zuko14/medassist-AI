"""Safety cards (ADR-CC-07, PROTOCOLS §4; TEST-PLAN P4): the card and an urgent case come in the SAME call as the
reading, even with the care quota exhausted, every template unapproved and the number's quality LOW — a safety
reply is an in-window answer to the patient's own message, never quota, never capped. Covered: hypo, very high,
BP crisis, PHQ-9 item 9 (WhatsApp, patient-facing) and ANC danger, OHSS, post-op eye (staff-entered → urgent case)."""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import cc, cc_hooks
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
from tests.cc_world import activate_pack, care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _m():
    from app.services.whatsapp import whatsapp_service
    return SimpleNamespace(whatsapp=whatsapp_service)


def _starve(care):
    """No quota, no approved template, quality LOW."""
    _q(care.conn, "UPDATE clinics SET config = coalesce(config, '{}'::jsonb) || '{\"dental_message_limits\": "
                  "{\"care\": 0}}'::jsonb, cc_settings = cc_settings || %s::jsonb WHERE id = %s",
       json.dumps({"wa_health": {"tier": "TIER_1K", "quality": "LOW"}, "templates": {}}), care.clinic)
    invalidate_tenant_cache()
    return asyncio.run(get_clinic_by_id(care.clinic))


def _prompt(care, e, phone, measure, kind="reading"):
    return str(_q(care.conn, "INSERT INTO cc_prompts (clinic_id, enrollment_id, kind, measure_code, channel, phone, "
                             "status, sent_at) VALUES (%s, %s, %s, %s, 'whatsapp_interactive', %s, 'sent', now()) "
                             "RETURNING id", care.clinic, e["id"], kind, measure, phone))


def _urgent(care, e):
    return _q(care.conn, "SELECT coalesce(string_agg(reason, ','), '') FROM cc_cases WHERE enrollment_id = %s "
                         "AND tier = 'urgent' AND status = 'open'", e["id"])


@pytest.mark.parametrize("value,card,reason", [("48", "hypo_now", "t2dm:hypo"),
                                                ("420", "high_sugar_symptoms", "t2dm:very_high")])
def test_glucose_safety_card_and_urgent_case_in_the_same_call(care, value, card, reason):  # noqa: F811
    e, pid = care.enroll()
    phone = care.phone(pid)
    _prompt(care, e, phone, "fbg")
    clinic = _starve(care)
    assert asyncio.run(cc_hooks.handle_typed(_m(), clinic, phone, "main_menu", value, "wamid." + uuid.uuid4().hex))
    texts = [s.args[0] for s in care.wa.of("text") if s.kw.get("_source") == "cc_safety"]
    assert len(texts) == 1 and care.wa.of("text")[-2].kw["_window_open"] is True
    if card == "hypo_now":
        assert "15 g" in texts[0] and "Do not drive" in texts[0]               # the Kriya default text
    else:
        assert texts[0] == "Care card text for high_sugar_symptoms, approved."   # the clinic-approved text
    assert reason in _urgent(care, e)
    assert not care.wa.of("template")
    assert _q(care.conn, "SELECT count(*) FROM clinic_message_quota_usage WHERE clinic_id = %s", care.clinic) == 0
    notes = _q(care.conn, "SELECT count(*) FROM admin_notifications WHERE clinic_id = %s AND title = '[Care] Urgent case'",
               care.clinic)
    assert notes == 1                                                          # coordinators alerted at once


def test_bp_crisis(care):  # noqa: F811
    activate_pack(care.api, care.clinic, care.u, "htn", cards=("bp_very_high_symptoms", "low_bp_dizzy", "visit_due"))
    e, pid = care.enroll("htn")
    phone = care.phone(pid)
    _prompt(care, e, phone, "bp")
    clinic = _starve(care)
    assert asyncio.run(cc_hooks.handle_typed(_m(), clinic, phone, "idle", "190/100", "wamid." + uuid.uuid4().hex))
    assert [s.args[0] for s in care.wa.of("text") if s.kw.get("_source") == "cc_safety"] == [
        "Care card text for bp_very_high_symptoms, approved."]
    assert "htn:crisis_range" in _urgent(care, e)


def test_phq9_item9_crisis_card(care):  # noqa: F811
    activate_pack(care.api, care.clinic, care.u, "phq9", cards=())
    e, pid = care.enroll("phq9")
    phone = care.phone(pid)
    p = _prompt(care, e, phone, "phq9", kind="phq9")
    clinic = _starve(care)
    asyncio.run(cc_hooks.handle_tap(_m(), clinic, phone, f"cc:phq:{p}:start", "w0"))
    for n in range(1, 10):
        asyncio.run(cc_hooks.handle_tap(_m(), clinic, phone, f"cc:phq:{p}:q{n}:{1 if n == 9 else 0}", f"w{n}"))
    cards = [s.args[0] for s in care.wa.of("text") if s.kw.get("_source") == "cc_safety"]
    assert len(cards) == 1 and "14416" in cards[0] and "112" in cards[0]
    assert "phq9:self_harm" in _urgent(care, e)


@pytest.mark.parametrize("pack,cards,facts,card,reason", [
    ("anc", ("anc_danger_signs",), {"edd": "2027-03-01", "dating_nt_scan_done": True}, "anc_danger_signs", "anc:anc_danger"),
    ("ivf_cycle", ("ohss_danger",), {}, "ohss_danger", "ivf_cycle:ohss_signs"),
    ("eye_post_cataract", ("eye_post_op_danger",), {"surgery_on": "2026-11-24"}, "eye_post_op_danger",
     "eye_post_cataract:eye_danger"),
])
def test_danger_symptom_is_urgent_with_its_card(care, pack, cards, facts, card, reason):  # noqa: F811
    activate_pack(care.api, care.clinic, care.u, pack, cards=cards)
    e, _ = care.enroll(pack, facts=facts)
    care.api.as_(care.u.coord)
    r = care.api.call("POST", f"/admin/cc/enrollments/{e['id']}/observations", care.clinic,
                      {"measure_code": "symptoms", "code": "danger"})
    assert r.status_code == 201, r.text
    ev = r.json()["evaluation"]
    assert ev["tier"] == "urgent" and ev["safety_card"] == card and ev["case_id"]
    assert reason in _urgent(care, e)


def test_every_pack_card_resolves_to_text_once_approved(care):  # noqa: F811
    invalidate_tenant_cache()
    clinic = asyncio.run(get_clinic_by_id(care.clinic))
    for key in ("crisis_support", "hypo_now"):                                 # Kriya defaults, every language
        for lang in ("en", "hi", "te"):
            assert asyncio.run(cc.card_text(clinic, key, lang))
    assert asyncio.run(cc.card_text(clinic, "ohss_danger", "en")) is None       # never invented
