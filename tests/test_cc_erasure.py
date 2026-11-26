"""DPDP erasure with Continuous Care (VERIFICATION D6, CC-61; TEST-PLAN P4).

Erasing an enrolled patient — or the account holder of an enrolled family member — stops every prompt, removes the
person's CC rows, withdraws the consent ('erasure') and succeeds with no FK error; the doctor's signed activation and
other patients' rows are untouched. A clinic without CC makes no cc_* call (tests/test_cc_zero_impact.py).
"""

import asyncio

from app.services.data_retention import DataRetentionService
from tests.cc_world import care, patient, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _seed(care, e):
    c, cid = care.conn, care.clinic
    pid = _q(c, "SELECT patient_id FROM cc_enrollments WHERE id = %s", e["id"])
    phone = care.phone(pid)
    _q(c, "INSERT INTO cc_prompts (clinic_id, enrollment_id, kind, channel, phone, status) VALUES "
          "(%s, %s, 'adherence', 'whatsapp_interactive', %s, 'sent')", cid, e["id"], phone)
    _q(c, "INSERT INTO cc_observations (clinic_id, enrollment_id, measure_code, value_num, observed_at, source) "
          "VALUES (%s, %s, 'fbg', 142, now(), 'whatsapp_typed')", cid, e["id"])
    case = _q(c, "INSERT INTO cc_cases (clinic_id, patient_id, enrollment_id, reason, tier, due_by) VALUES "
                 "(%s, %s, %s, 't2dm:hypo', 'urgent', now()) RETURNING id", cid, pid, e["id"])
    _q(c, "INSERT INTO cc_case_events (clinic_id, case_id, kind) VALUES (%s, %s, 'opened')", cid, case)
    _q(c, "INSERT INTO cc_actions (clinic_id, case_id, action_code, decided_by_name, decider_role) VALUES "
          "(%s, %s, 'order_lab', 'Dr', 'doctor')", cid, case)
    return pid, phone


def _count(care, pid):
    return {t: _q(care.conn, f"SELECT count(*) FROM {t} x WHERE clinic_id = %s AND " + (
        "patient_id = %s" if t in ("cc_enrollments", "cc_cases") else
        "enrollment_id IN (SELECT id FROM cc_enrollments WHERE patient_id = %s)"), care.clinic, pid)
        for t in ("cc_enrollments", "cc_cases", "cc_prompts", "cc_observations", "cc_care_plans")}


def test_erasure_of_an_enrolled_patient(care):  # noqa: F811
    e, _ = care.enroll()
    other, _ = care.enroll()
    pid, phone = _seed(care, e)
    assert _count(care, pid)["cc_observations"] == 1 and _count(care, pid)["cc_care_plans"] == 1
    out = asyncio.run(DataRetentionService().anonymize_clinical_records(care.clinic, phone))
    assert out["care_rows_purged"]["enrollments"] == 1 and out["care_rows_purged"]["observations"] == 1
    assert not [x for x in out.get("errors") or [] if "cc_" in str(x)]
    assert set(_count(care, pid).values()) == {0}
    assert _q(care.conn, "SELECT withdrawn_via FROM cc_consents WHERE patient_id = %s", pid) == "erasure"
    assert _q(care.conn, "SELECT status FROM cc_enrollments WHERE id = %s", other["id"]) == "active"   # others untouched
    assert _q(care.conn, "SELECT status FROM cc_protocol_activations WHERE id = %s", care.activation["id"]) == "active"


def test_erasure_of_an_account_holder_with_an_enrolled_family_member(care):  # noqa: F811
    api, c = care.api, care.conn
    holder = patient(c, care.clinic)
    fm = str(_q(c, "INSERT INTO family_members (clinic_id, primary_phone, full_name) VALUES (%s, %s, 'Ravi Kumar') "
                   "RETURNING id", care.clinic, care.phone(holder)))
    api.as_(care.u.doctor)
    e = api.call("POST", "/admin/cc/enrollments", care.clinic, {"patient_id": holder, "family_member_id": fm,
                                                                  "protocol_code": "t2dm", "primary_doctor_id": care.doctor,
                                                                  "wa_optin_method": "in_person"}).json()
    api.as_(care.u.coord)
    assert api.call("POST", f"/admin/cc/enrollments/{e['id']}/consent/counter", care.clinic,
                    {"notice_version": "cc-notice-1", "guardian_relation": "son"}).status_code == 200
    _seed(care, e)
    out = asyncio.run(DataRetentionService().anonymize_clinical_records(care.clinic, care.phone(holder)))
    assert out["care_rows_purged"]["enrollments"] == 1
    assert _q(c, "SELECT count(*) FROM cc_enrollments WHERE family_member_id = %s", fm) == 0
    assert _q(c, "SELECT count(*) FROM cc_prompts WHERE enrollment_id = %s", e["id"]) == 0
    assert _q(c, "SELECT withdrawn_via FROM cc_consents WHERE family_member_id = %s", fm) == "erasure"
    # the family member row itself was removed or redacted by the existing step (no FK error from CC)
    left = _q(c, "SELECT max(full_name) FROM family_members WHERE id = %s", fm)
    assert left is None or left.startswith("[REDACTED")


def test_erasure_after_care_was_switched_off_still_purges(care):  # noqa: F811
    e, _ = care.enroll()
    pid, phone = _seed(care, e)
    assert care.api.owner_toggle(care.clinic, "care_enabled", False, confirm=True).status_code == 200
    _q(care.conn, "UPDATE clinics SET cc_state = 'OFF' WHERE id = %s", care.clinic)
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    out = asyncio.run(DataRetentionService().anonymize_clinical_records(care.clinic, phone))
    assert out["care_rows_purged"]["enrollments"] == 1 and set(_count(care, pid).values()) == {0}
