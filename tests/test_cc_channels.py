"""Continuous Care channels (CHANNELS.md, ADR-CC-10 / ADR-CC-17; TEST-PLAN P4).

Window open → interactive card (no quota); closed → the approved utility template, which reserves one unit of quota
kind 'care'; unapproved template → nothing sent + failure code; quota exhausted → skip; Meta error → quota released;
STOP / no WhatsApp opt-in → never a message; the card window; the messaging budget (tier share cap, MEDIUM halves,
LOW stops). Reply-button titles fit Meta's 20 characters in every language (U10).
"""

import asyncio
import json
from datetime import date, datetime, timedelta

import pytest

from app.services import cc_channels as ch
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

IST = ch.IST


def test_button_titles_fit_meta_limits():
    for key, langs in ch.BUTTON_TITLES.items():
        assert set(langs) == {"en", "hi", "te"}, key
        for lang, title in langs.items():
            assert 1 <= len(title) <= 20, (key, lang, title, len(title))
    for name, tpl in ch.TEMPLATES.items():
        assert len(tpl["buttons"]) <= 3 and all(len(b) <= 20 for b in tpl["buttons"]), name
        assert tpl["body"].count("{{") == len(tpl["example"]), name
        assert ch.meta_payload(name)["category"] == "UTILITY"


def test_template_variables_are_sanitised():
    assert ch.safe_name("Lakshmi Devi") == "Lakshmi"
    assert ch.safe_name("<b>Ravi</b>*{{1}}") == "bRavib1"
    assert ch.safe_name(None) == "there" and ch.safe_name("") == "there" and ch.safe_name("x" * 90) == "x" * 40


def test_due_times():
    d = date(2026, 11, 25)                                             # a Wednesday
    adh = {"prompt_mode": "adherence", "schedule": {"times": ["08:00", "20:00"]}, "starts_on": "2026-11-20",
           "ends_on": "2026-11-30"}
    assert [t.strftime("%H:%M") for t in ch.due_times(adh, d)] == ["08:00", "20:00"]
    assert ch.due_times(adh, date(2026, 12, 1)) == [] and ch.due_times(adh, date(2026, 11, 19)) == []
    every3 = {"prompt_mode": "reading", "schedule": {"every_days": 3, "time": "07:30"}, "starts_on": "2026-11-22"}
    assert len(ch.due_times(every3, d)) == 1 and ch.due_times(every3, d + timedelta(days=1)) == []
    weekly = {"prompt_mode": "reading", "schedule": {"weekly": ["mon", "thu"], "time": "08:30"}, "starts_on": "2026-11-01"}
    assert ch.due_times(weekly, d) == [] and len(ch.due_times(weekly, date(2026, 11, 26))) == 1
    assert ch.due_times({**adh, "prompt_mode": "display_only"}, d) == []
    assert ch.due_times(adh, d)[0].utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize("health,share,want", [
    ({}, 40, None), ({"tier": "TIER_1K"}, 40, 400), ({"tier": "TIER_1K", "quality": "GREEN"}, 40, 400),
    ({"tier": "TIER_1K", "quality": "MEDIUM"}, 40, 200), ({"tier": "TIER_1K", "quality": "YELLOW"}, 60, 300),
    ({"tier": "TIER_1K", "quality": "LOW"}, 40, 0), ({"tier": "TIER_250", "quality": "RED"}, 60, 0),
    ({"tier": "TIER_50"}, 10, 5)])
def test_template_budget(health, share, want):
    assert ch.template_budget({"cc_settings": {"wa_health": health, "care_tier_share": share}}) == want


# ── dispatch on real Postgres ────────────────────────────────────────────────

def _clinic(care, **settings):
    if settings:
        cur = _q(care.conn, "SELECT cc_settings FROM clinics WHERE id = %s", care.clinic)
        _q(care.conn, "UPDATE clinics SET cc_settings = %s::jsonb WHERE id = %s", json.dumps({**cur, **settings}),
           care.clinic)
    invalidate_tenant_cache()
    return asyncio.run(get_clinic_by_id(care.clinic))


def _run(care, clinic, hhmm="07:35"):
    """Materialise today's items and dispatch at `hhmm` IST today."""
    from app.utils.helpers import today_ist
    day = today_ist()
    asyncio.run(ch.materialize(clinic, day))
    h, m = map(int, hhmm.split(":"))
    return asyncio.run(ch.dispatch(clinic, datetime(day.year, day.month, day.day, h, m, tzinfo=IST)))


def _prompt(care, e):
    return _q(care.conn, "SELECT row_to_json(p) FROM cc_prompts p WHERE enrollment_id = %s AND kind <> 'consent_invite' "
                         "ORDER BY created_at DESC LIMIT 1", e["id"])


def _used(care):
    return _q(care.conn, "SELECT coalesce(sum(used), 0) FROM clinic_message_quota_usage WHERE clinic_id = %s "
                         "AND kind = 'care'", care.clinic)


APPROVED = {n: "APPROVED" for n in ch.TEMPLATES}


def test_in_window_card_is_interactive_and_free(care):  # noqa: F811
    e, patient = care.enroll()
    care.wa.window = True
    out = _run(care, _clinic(care))
    assert out["sent"] == 1
    p = _prompt(care, e)
    assert (p["channel"], p["status"], p["kind"], p["measure_code"], p["quota_reserved"]) == (
        "whatsapp_interactive", "sent", "reading", "fbg", False)
    msg = care.wa.of("buttons")[-1]
    assert msg.phone == care.phone(patient) and [b["id"] for b in msg.args[1]] == [f"cc:read:{p['id']}",
                                                                                   f"cc:skip:{p['id']}"]
    assert "STOP CARE" in msg.args[0] and _used(care) == 0
    assert _run(care, _clinic(care))["sent"] == 0                       # idempotent: the item was prompted once


def test_closed_window_needs_an_approved_template(care):  # noqa: F811
    e, _ = care.enroll()
    assert _run(care, _clinic(care))["skipped"] == 1
    p = _prompt(care, e)
    assert (p["channel"], p["status"], p["failure_code"]) == ("whatsapp_template", "failed", "template_unapproved")
    assert not care.wa.of("template") and _used(care) == 0


def test_template_reserves_care_quota_and_meta_error_releases_it(care):  # noqa: F811
    e, _ = care.enroll()
    clinic = _clinic(care, templates=APPROVED)
    assert _run(care, clinic)["sent"] == 1
    p = _prompt(care, e)
    assert (p["template_name"], p["status"], p["quota_reserved"]) == ("cc_reading_request_v1", "sent", True)
    assert p["wa_message_id"].startswith("wamid.") and _used(care) == 1
    tpl = care.wa.of("template")[-1]
    assert tpl.args[0] == "cc_reading_request_v1" and tpl.kw["language"] == "en"
    payloads = [c["parameters"][0]["payload"] for c in tpl.kw["components"] if c["type"] == "button"]
    assert payloads == [f"cc:read:{p['id']}", f"cc:skip:{p['id']}"]
    # dental quotas untouched: the care unit is its own kind
    assert _q(care.conn, "SELECT count(*) FROM clinic_message_quota_usage WHERE clinic_id = %s AND kind <> 'care'",
              care.clinic) == 0
    e2, _ = care.enroll()                                               # (its consent invite used one unit)
    before = _used(care)
    care.wa.fail = True
    assert _run(care, _clinic(care))["failed"] == 1
    p2 = _prompt(care, e2)
    assert (p2["status"], p2["failure_code"], p2["quota_reserved"]) == ("failed", "meta_error", False)
    assert _used(care) == before                                        # the failed unit was released


def test_quota_exhausted_skips(care):  # noqa: F811
    e, _ = care.enroll()
    _q(care.conn, "UPDATE clinics SET config = coalesce(config, '{}'::jsonb) || '{\"dental_message_limits\": "
                  "{\"care\": 0}}'::jsonb WHERE id = %s", care.clinic)
    assert _run(care, _clinic(care, templates=APPROVED))["skipped"] == 1
    assert _prompt(care, e)["failure_code"] == "quota" and not care.wa.of("template")


@pytest.mark.parametrize("health,sent_before,code", [
    ({"tier": "TIER_50"}, 5, "budget"),                                # 10 % of 50 = 5 a day, already used
    ({"tier": "TIER_1K", "quality": "LOW"}, 0, "budget"),               # LOW stops care templates
])
def test_messaging_budget(care, health, sent_before, code):  # noqa: F811
    e, patient = care.enroll()
    for _ in range(sent_before):
        _q(care.conn, "INSERT INTO cc_prompts (clinic_id, enrollment_id, kind, channel, phone, status, sent_at) VALUES "
                      "(%s, %s, 'adherence', 'whatsapp_template', %s, 'sent', now())",
           care.clinic, e["id"], care.phone(patient))
    clinic = _clinic(care, templates=APPROVED, wa_health=health, care_tier_share=10)
    assert _run(care, clinic)["skipped"] == 1
    assert _prompt(care, e)["failure_code"] == code and _used(care) == 0


def test_medium_quality_halves_the_share(care):  # noqa: F811
    e, patient = care.enroll()
    for _ in range(5):                                                  # 20 % of 50 = 10, halved at MEDIUM = 5
        _q(care.conn, "INSERT INTO cc_prompts (clinic_id, enrollment_id, kind, channel, phone, status, sent_at) VALUES "
                      "(%s, %s, 'adherence', 'whatsapp_template', %s, 'sent', now())",
           care.clinic, e["id"], care.phone(patient))
    clinic = _clinic(care, templates=APPROVED, wa_health={"tier": "TIER_50", "quality": "MEDIUM"}, care_tier_share=20)
    assert ch.template_budget(clinic) == 5 and _run(care, clinic)["skipped"] == 1
    clinic = _clinic(care, wa_health={"tier": "TIER_50", "quality": "GREEN"})
    assert ch.template_budget(clinic) == 10


@pytest.mark.parametrize("why", ["stop", "no_optin"])
def test_never_messages_stop_or_no_optin(care, why):  # noqa: F811
    e, patient = care.enroll()
    care.wa.window = True
    if why == "stop":
        _q(care.conn, "UPDATE patients SET opted_in = false, opted_out_at = now() WHERE id = %s", patient)
    else:
        _q(care.conn, "UPDATE cc_consents SET wa_optin_at = NULL, wa_optin_method = NULL WHERE patient_id = %s", patient)
    assert _run(care, _clinic(care))["skipped"] == 1
    assert _prompt(care, e)["failure_code"] == ("opted_out" if why == "stop" else "no_optin")
    assert not care.wa.sent[1:] or all(s.kind != "buttons" or "cc:read" not in str(s.args) for s in care.wa.sent)


def test_counter_registered_patient_without_stop_is_reachable(care):  # noqa: F811
    """opted_in False is the column default for a patient who never chatted — not a STOP."""
    e, patient = care.enroll()
    assert _q(care.conn, "SELECT coalesce(opted_in, false) FROM patients WHERE id = %s", patient) is False
    care.wa.window = True
    assert _run(care, _clinic(care))["sent"] == 1


def test_nothing_outside_the_card_window(care):  # noqa: F811
    care.enroll()
    care.wa.window = True
    assert _run(care, _clinic(care), "22:30") == {"sent": 0, "skipped": 0, "failed": 0}
    assert not care.wa.of("buttons")[1:]
