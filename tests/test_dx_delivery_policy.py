"""DELIVERY-POLICY §3 decision table, rule by rule, on real PostgreSQL (dx_delivery_decision) — plus rule 9
(plan capability), which the single sender checks after the claim.

Each rule refuses with its stable code; the first refusing rule wins; an override lifts rule 7 only.
"""

import asyncio
import json
import uuid

import pytest

from tests.test_migration_dx_m3 import _err, _q, lab  # noqa: F401  (fixture)
from tests.test_migration_dx_m6 import m6  # noqa: F401  (fixture)
from tests.test_migration_dx_m7 import (  # noqa: F401  (fixture)
    _account, _bill_paid, _decide, _deliveries, _guc, _owner, _release, _rendered, _set_visit, _signed, m7)


@pytest.fixture
def pol(m7):  # noqa: F811
    _owner(m7, "dx")
    _signed(m7)
    m7["rel"] = _release(m7)
    return m7


def test_rule_1_superseded_release(pol):
    _signed(pol, "Lipid Profile", "180")
    _release(pol)
    assert _decide(pol, pol["rel"]["id"], "patient") == (False, "release_superseded")
    assert _decide(pol, str(uuid.uuid4()), "patient") == (False, "release_not_found")


def test_rule_2_delivery_owner(pol):
    _owner(pol, "connector")
    for t in ("patient", "print_patient", "share_link", "partner_portal", "account"):
        assert _decide(pol, pol["rel"]["id"], t) == (False, "owner_connector")
    _owner(pol, "shadow")
    for t in ("patient", "referrer", "account", "share_link", "partner_portal"):
        assert _decide(pol, pol["rel"]["id"], t) == (False, "held_shadow")
    assert _decide(pol, pol["rel"]["id"], "print_patient")[1] != "held_shadow"     # staff print with watermark


def test_rule_3_payer_routed(pol):
    acc = _account(pol, name="Health Assure TPA", kind="insurance_tpa", report_delivery="none")
    _set_visit(pol, b2b_account_id=acc, billing_mode="internal")
    for t in ("patient", "print_patient", "share_link", "referrer"):
        assert _decide(pol, pol["rel"]["id"], t) == (False, "payer_routed")
    # the CHECK on accounts never lets a payer default to the patient without a recorded authorisation
    assert "dx_b2b_payer_routed" in _err(pol["c"], "UPDATE dx_b2b_accounts SET report_delivery = 'patient' WHERE id = %s",
                                         acc)


def test_rule_4_account_rule(pol):
    acc = _account(pol, name="City Hospital", kind="hospital", report_delivery="account", desk_whatsapp="+919800000333")
    _set_visit(pol, b2b_account_id=acc, billing_mode="internal")
    assert _decide(pol, pol["rel"]["id"], "patient") == (False, "account_rule")
    assert _decide(pol, pol["rel"]["id"], "account") == (True, None)
    _q(pol["c"], "UPDATE dx_b2b_accounts SET report_delivery = 'patient' WHERE id = %s", acc)
    assert _decide(pol, pol["rel"]["id"], "account") == (False, "account_rule")
    _q(pol["c"], "UPDATE dx_b2b_accounts SET report_delivery = 'none' WHERE id = %s", acc)
    assert _decide(pol, pol["rel"]["id"], "account") == (False, "account_rule")


def test_rule_5_patient_phone(pol):
    member = str(_q(pol["c"], """INSERT INTO family_members (clinic_id, primary_phone, full_name, relationship, gender,
                                 age_years, age_recorded_on) SELECT clinic_id, phone, 'Ravi (B2B)', 'other', 'male', 40, CURRENT_DATE FROM patients
                                 WHERE id = %s RETURNING id""", pol["patient"]))
    acc = _account(pol, name="Nursing Home", kind="hospital", report_delivery="both")
    _q(pol["c"], "UPDATE family_members SET dx_b2b_account_id = %s WHERE id = %s", acc, member)
    _set_visit(pol, b2b_account_id=acc, billing_mode="internal", family_member_id=member)
    assert _decide(pol, pol["rel"]["id"], "patient") == (False, "no_patient_phone")


def test_rule_6_referrer_consent(pol):
    ref = str(_q(pol["c"], "INSERT INTO dx_referrers (clinic_id, kind, name, report_copy) VALUES (%s, 'doctor', 'Dr. X', "
                           "'whatsapp') RETURNING id", pol["clinic"]))
    _set_visit(pol, referrer_id=ref, delivery_prefs=json.dumps({"consent_at": "2026-11-15T10:00:00+05:30"}))
    assert _decide(pol, pol["rel"]["id"], "referrer") == (False, "no_referrer_consent")    # no phone
    _q(pol["c"], "UPDATE dx_referrers SET phone = '+919811111112' WHERE id = %s", ref)
    assert _decide(pol, pol["rel"]["id"], "referrer") == (False, "held_dues")              # rule 7 next
    _q(pol["c"], "UPDATE dx_referrers SET report_copy = 'none' WHERE id = %s", ref)
    assert _decide(pol, pol["rel"]["id"], "referrer") == (False, "no_referrer_consent")


def test_rule_7_dues_and_override(pol):
    for t in ("patient", "print_patient", "share_link", "partner_portal"):
        assert _decide(pol, pol["rel"]["id"], t) in ((False, "held_dues"), (False, "account_rule"))
    assert _decide(pol, pol["rel"]["id"], "patient") == (False, "held_dues")
    _bill_paid(pol)
    assert _decide(pol, pol["rel"]["id"], "patient") == (True, None)
    # an expired override does not count; a live one lifts rule 7 only
    _guc(pol, "INSERT INTO dx_field_collections (clinic_id, accession_id, amount_paise, mode, collected_by, "
              "collected_by_name, collected_at, idempotency_key) VALUES (%s, %s, 100, 'cash', %s, 'phleb', now(), %s)",
         pol["clinic"], pol["visit"]["visit"]["id"], pol["tech"], uuid.uuid4().hex)
    assert _decide(pol, pol["rel"]["id"], "patient") == (False, "held_dues")
    assert "dx_delivery_overrides_expiry" in _err(pol["c"], "SELECT dx_create_delivery_override(%s, %s, %s, now() - "
                                                            "interval '1 hour', %s, 'admin')", pol["clinic"],
                                                  pol["visit"]["visit"]["id"], "long enough reason", pol["path"])
    _q(pol["c"], "SELECT dx_create_delivery_override(%s, %s, 'doorstep cash verified by phone', NULL, %s, 'admin')",
       pol["clinic"], pol["visit"]["visit"]["id"], pol["path"])
    assert _decide(pol, pol["rel"]["id"], "patient") == (True, None)
    _owner(pol, "connector")
    assert _decide(pol, pol["rel"]["id"], "patient") == (False, "owner_connector")


def test_rule_8_account_copy_of_b2b_visit_is_not_dues(pol):
    acc = _account(pol, name="City Hospital", kind="hospital", report_delivery="both", desk_whatsapp="+919800000444")
    _set_visit(pol, b2b_account_id=acc, billing_mode="b2b_credit")
    assert _decide(pol, pol["rel"]["id"], "account") == (True, None)
    _set_visit(pol, billing_mode="patient")          # a patient-billed visit: the account copy waits too
    assert _decide(pol, pol["rel"]["id"], "account") == (False, "held_dues")


def test_rule_9_plan_without_whatsapp_reports(pol, monkeypatch):
    """Checked by dx_reports.send_delivery after the claim: the row is blocked, nothing reaches the pipeline."""
    from app.services import dx_reports
    from app.services.lab_reports import LabReportService
    _bill_paid(pol)
    assert _q(pol["c"], "SELECT dx_reevaluate_deliveries(%s, %s)", pol["clinic"], pol["visit"]["visit"]["id"]) == []
    _rendered(pol, pol["rel"])
    did = _deliveries(pol, pol["rel"]["id"])["patient"]["id"]
    _q(pol["c"], "UPDATE clinics SET features = features || '{\"lab_reports\": false}' WHERE id = %s", pol["clinic"])
    calls = []

    async def no_send(self, **kw):
        calls.append(kw)
    monkeypatch.setattr(LabReportService, "upload_and_send", no_send)
    from tests.ipd_pg_bridge import PgBridge
    from tests.conftest_db import _PG_URI
    bridge = PgBridge(_PG_URI).install(monkeypatch)
    try:
        out = asyncio.run(dx_reports.send_delivery(pol["clinic"], did))
    finally:
        bridge.close()
    assert out["status"] == "blocked" and out["reason"] == "plan_no_delivery" and calls == []


def test_rule_10_allowed_and_status_mapping(pol):
    _bill_paid(pol)
    assert _decide(pol, pol["rel"]["id"], "patient") == (True, None)
    assert _q(pol["c"], "SELECT dx_delivery_status_for(true, NULL)") == "queued"
    assert _q(pol["c"], "SELECT dx_delivery_status_for(false, 'held_dues')") == "held_dues"
    assert _q(pol["c"], "SELECT dx_delivery_status_for(false, 'held_shadow')") == "held_shadow"
    assert _q(pol["c"], "SELECT dx_delivery_status_for(false, 'payer_routed')") == "blocked"
    assert "dx_target_invalid" in _err(pol["c"], "SELECT * FROM dx_delivery_decision(%s, %s, 'email')", pol["clinic"],
                                       pol["rel"]["id"])
