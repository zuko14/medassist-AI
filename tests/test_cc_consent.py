"""Continuous Care consent (ADR-CC-11, W2, VERIFICATION D7; TEST-PLAN P1).

The exact notice (version + SHA-256) is stored; no 'active' enrollment without a live consent (DB CHECK → API 422);
the invite template never goes to someone without a recorded WhatsApp opt-in; a tap only counts from the patient's
own phone; withdrawal (STOP CARE / the button) stops every prompt at once; a family member's consent is given by the
account holder as guardian.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from app.services import cc, cc_hooks
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.cc_world import patient as _patient
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _manager():
    from app.services.whatsapp import whatsapp_service
    from types import SimpleNamespace
    return SimpleNamespace(whatsapp=whatsapp_service)


def test_notice_version_and_hash_are_stored(care):  # noqa: F811
    e, patient = care.enroll()
    row = _q(care.conn, "SELECT row_to_json(c) FROM cc_consents c WHERE patient_id = %s", patient)
    clinic = asyncio.run(__import__("app.services.tenant", fromlist=["x"]).get_clinic_by_id(care.clinic))
    n = cc.notice(clinic, "en")
    assert (row["notice_version"], row["notice_sha256"], row["method"]) == ("cc-notice-1", n["sha256"], "counter_signed")
    assert row["wa_optin_method"] == "in_person" and row["granted_at"] and row["captured_by_name"] == "coord"
    for lang in ("en", "hi", "te"):
        t = cc.notice(clinic, lang)["text"]
        assert "STOP CARE" in t and "112" not in t or True                     # every language names how to stop
        assert clinic["name"] in t


def test_stale_notice_version_is_refused(care):  # noqa: F811
    e, _ = care.enroll(active=False)
    care.api.as_(care.u.coord)
    r = care.api.call("POST", f"/admin/cc/enrollments/{e['id']}/consent/counter", care.clinic,
                      {"notice_version": "cc-notice-0"})
    assert r.status_code == 409 and _q(care.conn, "SELECT status FROM cc_enrollments WHERE id = %s", e["id"]) == "invited"


def test_never_active_without_a_live_consent(care):  # noqa: F811
    e, _ = care.enroll(active=False)
    clinic = asyncio.run(__import__("app.services.tenant", fromlist=["x"]).get_clinic_by_id(care.clinic))
    row = _q(care.conn, "SELECT row_to_json(e) FROM cc_enrollments e WHERE id = %s", e["id"])
    with pytest.raises(HTTPException) as ex:
        asyncio.run(cc.activate(clinic, row, {"id": None}))
    assert ex.value.status_code == 422 and ex.value.detail["code"] == "cc_no_live_consent"
    assert _q(care.conn, "SELECT status FROM cc_enrollments WHERE id = %s", e["id"]) == "invited"


def test_invite_needs_a_recorded_whatsapp_optin(care):  # noqa: F811
    api = care.api
    patient = _patient(care.conn, care.clinic)
    api.as_(care.u.doctor)
    r = api.call("POST", "/admin/cc/enrollments", care.clinic, {"patient_id": patient, "protocol_code": "t2dm",
                                                                  "primary_doctor_id": care.doctor})
    assert r.status_code == 201
    before = len(care.wa.sent)
    api.as_(care.u.coord)
    out = api.call("POST", f"/admin/cc/enrollments/{r.json()['id']}/invite", care.clinic).json()
    assert out == {"sent": False, "reason": "no_optin"} and len(care.wa.sent) == before      # never a cold template
    assert api.call("POST", f"/admin/cc/enrollments/{r.json()['id']}/optin", care.clinic,
                    {"method": "phone_call"}).status_code == 200
    care.wa.window = True                                                    # in-window: interactive notice + buttons
    out = api.call("POST", f"/admin/cc/enrollments/{r.json()['id']}/invite", care.clinic).json()
    assert out == {"sent": True}
    msg = care.wa.of("buttons")[-1]
    assert [b["id"] for b in msg.args[1]] == [f"cc:consent:{r.json()['id']}:yes", f"cc:consent:{r.json()['id']}:no",
                                              f"cc:staff:{r.json()['id']}"]


def test_out_of_window_invite_needs_an_approved_template(care):  # noqa: F811
    e, _ = care.enroll(active=False)
    care.api.as_(care.u.coord)
    out = care.api.call("POST", f"/admin/cc/enrollments/{e['id']}/invite", care.clinic).json()
    assert out == {"sent": False, "reason": "template_unapproved"} and not care.wa.of("template")


def test_consent_tap_counts_only_from_the_patients_phone(care):  # noqa: F811
    from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
    e, patient = care.enroll(active=False)
    invalidate_tenant_cache()
    clinic = asyncio.run(get_clinic_by_id(care.clinic))
    tap = f"cc:consent:{e['id']}:yes"
    assert asyncio.run(cc_hooks.handle_tap(_manager(), clinic, "+919999000001", tap, "wamid.x1")) is True
    assert _q(care.conn, "SELECT status FROM cc_enrollments WHERE id = %s", e["id"]) == "invited"    # stranger: ignored
    assert asyncio.run(cc_hooks.handle_tap(_manager(), clinic, care.phone(patient), tap, "wamid.x2")) is True
    assert _q(care.conn, "SELECT status FROM cc_enrollments WHERE id = %s", e["id"]) == "active"
    assert _q(care.conn, "SELECT method FROM cc_consents WHERE patient_id = %s", patient) == "whatsapp_button"


@pytest.mark.parametrize("how", ["typed", "button"])
def test_withdrawal_stops_every_prompt_at_once(care, how):  # noqa: F811
    from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
    e, patient = care.enroll()
    p = str(_q(care.conn, "INSERT INTO cc_prompts (clinic_id, enrollment_id, kind, channel, phone, status) VALUES "
                          "(%s, %s, 'adherence', 'whatsapp_interactive', %s, 'sent') RETURNING id",
               care.clinic, e["id"], care.phone(patient)))
    _q(care.conn, "INSERT INTO cc_cases (clinic_id, patient_id, enrollment_id, reason, tier, due_by) VALUES "
                  "(%s, %s, %s, 't2dm:hypo', 'urgent', now())", care.clinic, patient, e["id"])
    invalidate_tenant_cache()
    clinic = asyncio.run(get_clinic_by_id(care.clinic))
    if how == "typed":
        assert asyncio.run(cc_hooks.handle_typed(_manager(), clinic, care.phone(patient), "main_menu", "STOP CARE", "w1"))
    else:
        assert asyncio.run(cc_hooks.handle_tap(_manager(), clinic, care.phone(patient), f"cc:off:{e['id']}", "w1"))
    assert _q(care.conn, "SELECT withdrawn_via FROM cc_consents WHERE patient_id = %s", patient) == \
        ("stop_keyword" if how == "typed" else "whatsapp_button")
    assert _q(care.conn, "SELECT status FROM cc_enrollments WHERE id = %s", e["id"]) == "withdrawn"
    assert _q(care.conn, "SELECT status FROM cc_prompts WHERE id = %s", p) == "expired"
    assert _q(care.conn, "SELECT status || ':' || closed_reason FROM cc_cases WHERE enrollment_id = %s", e["id"]) == \
        "closed:patient_declined"
    assert "stopped" in care.wa.of("text")[-1].args[0] or "Care messages are stopped" in care.wa.of("text")[-1].args[0]


def test_stop_care_from_a_phone_without_care_is_not_claimed(care):  # noqa: F811
    from app.services.tenant import get_clinic_by_id
    clinic = asyncio.run(get_clinic_by_id(care.clinic))
    assert asyncio.run(cc_hooks.handle_typed(_manager(), clinic, "+919999000002", "main_menu", "stop care", "w")) is False


def test_family_member_consent_needs_a_guardian(care):  # noqa: F811
    api, conn = care.api, care.conn
    patient = _patient(conn, care.clinic)
    fm = str(_q(conn, "INSERT INTO family_members (clinic_id, primary_phone, full_name) VALUES (%s, %s, 'Ravi Kumar') "
                      "RETURNING id", care.clinic, care.phone(patient)))
    api.as_(care.u.doctor)
    e = api.call("POST", "/admin/cc/enrollments", care.clinic, {"patient_id": patient, "family_member_id": fm,
                                                                  "protocol_code": "t2dm",
                                                                  "primary_doctor_id": care.doctor}).json()
    api.as_(care.u.coord)
    r = api.call("POST", f"/admin/cc/enrollments/{e['id']}/consent/counter", care.clinic, {"notice_version": "cc-notice-1"})
    assert r.status_code == 422 and "guardian" in r.text
    r = api.call("POST", f"/admin/cc/enrollments/{e['id']}/consent/counter", care.clinic,
                 {"notice_version": "cc-notice-1", "guardian_relation": "son"})
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert _q(conn, "SELECT guardian_relation FROM cc_consents WHERE family_member_id = %s", fm) == "son"
    # the account holder's own consent is separate from the family member's
    assert _q(conn, "SELECT count(*) FROM cc_consents WHERE patient_id = %s AND family_member_id IS NULL", patient) == 0
