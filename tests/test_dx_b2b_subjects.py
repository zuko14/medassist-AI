"""B2B accounts and subjects without their own phone (ADR-DX-20, ADR-DX-21, AUDIT A2/A6).

Payer-routed accounts (insurance / TPA, corporate) default to account delivery and
can copy the patient only with a recorded authorisation; a hospital's patient who
has no phone (or the hospital's shared desk number) becomes a dependant of the
account's holder patient, never listed in that desk phone's WhatsApp family picker.
"""

import asyncio
import uuid

from app.database import get_family_members
from tests.test_dx_visits_e2e import dxw, visit  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def test_payer_routed_accounts(dxw):  # noqa: F811
    api, c = dxw["api"].as_(dxw["u"]["admin"]), dxw["clinic"]
    tpa = api.call("POST", "/admin/dx/b2b-accounts", c, {"name": "Medi Assist TPA", "kind": "insurance_tpa",
                                                        "desk_whatsapp": "+919800000001"})
    assert tpa.status_code == 201 and tpa.json()["report_delivery"] == "account"
    bad = api.call("POST", "/admin/dx/b2b-accounts", c, {"name": "Corp X", "kind": "corporate", "report_delivery": "patient"})
    assert bad.status_code == 422 and "payer" in bad.text
    ok = api.call("POST", "/admin/dx/b2b-accounts", c, {"name": "Corp Y", "kind": "corporate", "report_delivery": "both",
                                                       "patient_copy_reason": "Employer consent forms collected 2026"})
    assert ok.status_code == 201 and ok.json()["patient_copy_authorised_at"]
    # revoking the authorisation while the patient still gets a copy is refused
    r = api.call("PUT", f"/admin/dx/b2b-accounts/{ok.json()['id']}", c, {"patient_copy_reason": None})
    assert r.status_code == 422
    r = api.call("PUT", f"/admin/dx/b2b-accounts/{ok.json()['id']}", c, {"patient_copy_reason": None,
                                                                         "report_delivery": "account"})
    assert r.status_code == 200 and r.json()["patient_copy_authorised_at"] is None
    hosp = api.call("POST", "/admin/dx/b2b-accounts", c, {"name": "Naidu Hospital", "kind": "hospital", "code": "NAIDU"})
    assert hosp.json()["report_delivery"] == "patient"
    assert api.call("POST", "/admin/dx/b2b-accounts", c, {"name": "Dup", "kind": "hospital", "code": "NAIDU"}).status_code == 409
    api.as_(dxw["u"]["desk"])
    assert api.call("POST", "/admin/dx/b2b-accounts", c, {"name": "Zed Hospital", "kind": "hospital"}).status_code == 403


def test_b2b_subject_without_phone_is_a_hidden_dependant(dxw):  # noqa: F811
    api, c = dxw["api"].as_(dxw["u"]["admin"]), dxw["clinic"]
    desk = "+919440989440"
    acc = api.call("POST", "/admin/dx/b2b-accounts", c, {"name": "Naidu Hospital", "kind": "hospital",
                                                        "desk_whatsapp": desk}).json()
    api.as_(dxw["u"]["desk"])
    a = api.call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/subjects", c, {"name": "K Ramaiah", "gender": "male",
                                                                            "age_years": 61})
    b = api.call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/subjects", c, {"name": "P Lakshmi", "phone": desk,
                                                                            "gender": "female"})
    assert a.status_code == 201 and b.status_code == 201, (a.text, b.text)
    assert a.json()["patient_id"] == b.json()["patient_id"]                         # one holder patient
    assert a.json()["mrn"]
    again = api.call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/subjects", c, {"name": "K Ramaiah"})
    assert again.json()["family_member_id"] == a.json()["family_member_id"]          # same subject reused
    own = api.call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/subjects", c, {"name": "Own Phone",
                                                                              "phone": "+919812300000"})
    assert own.status_code == 422 and "own phone" in own.text
    holder = _q(dxw["c"], "SELECT holder_patient_id::text FROM dx_b2b_accounts WHERE id = %s", acc["id"])
    assert holder == a.json()["patient_id"]
    # the desk phone's WhatsApp chat lists none of the hospital's patients
    _q(dxw["c"], "INSERT INTO family_members (clinic_id, primary_phone, full_name, relationship) VALUES (%s, %s, "
                 "'Desk clerk child', 'child')", c, desk)
    names = [m["full_name"] for m in asyncio.run(get_family_members(c, desk))]
    assert names == ["Desk clerk child"]
    # the subject is registered on the account
    r = api.call("POST", "/admin/dx/visits", c, {"patient_id": a.json()["patient_id"],
                                                 "family_member_id": a.json()["family_member_id"],
                                                 "b2b_account_id": acc["id"], "source": "b2b", "patient_type": "b2b",
                                                 "external_ref": "IP-2231",
                                                 "items": [{"lab_test_id": dxw["T"]["cbc"]}],
                                                 "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 201, r.text
    snap = r.json()["visit"]
    assert snap["patient_snapshot"]["name"] == "K Ramaiah" and snap["b2b_account_id"] == acc["id"]


def test_account_without_a_phone_cannot_hold_subjects(dxw):  # noqa: F811
    api, c = dxw["api"].as_(dxw["u"]["admin"]), dxw["clinic"]
    acc = api.call("POST", "/admin/dx/b2b-accounts", c, {"name": "No Phone Clinic", "kind": "clinic"}).json()
    r = api.as_(dxw["u"]["desk"]).call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/subjects", c, {"name": "X Y"})
    assert r.status_code == 422
    _q(dxw["c"], "UPDATE dx_b2b_accounts SET status = 'on_hold' WHERE id = %s", acc["id"])
    r = visit(dxw, ["cbc"], b2b_account_id=acc["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_b2b_on_hold"
