"""Diagnostic OS registration → bill → receipt end to end (P3: W1, W2, W12): real HTTP routes,
real Postgres (tests/ipd_pg_bridge.py).

Patient registry under the DX scope, referrers (dedupe), quote = registration,
visit with tubes and barcodes, idempotent Save, money hidden from a technician,
bill on the OPD ledger (appointment-less) equal to the patient shares, issue +
receipt through the unchanged OPD services, supplementary bill, cancel item before
and after issue, site scope for a branch-pinned login, wind-down closes new visits.
The `dxw` fixture is shared by the other P3 test files.
"""

import uuid

import pytest

from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _approve(conn, clinic, test, doctor):
    d = _q(conn, "SELECT dx_test_digest(%s, %s)", clinic, test)
    _q(conn, "SELECT dx_approve_test(%s, %s, %s, %s, 'Dr')", clinic, test, d, doctor)


@pytest.fixture
def dxw(api):  # noqa: F811
    c = api.conn
    clinic = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('Desk Lab', %s, "
                       "'diagstream', true) RETURNING id", "+9167" + uuid.uuid4().hex[:8]))
    branch = str(_q(c, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Main') RETURNING id", clinic))
    assert api.owner_toggle(clinic, "dx_enabled", True).status_code == 200      # provisions the site
    dept = str(_q(c, "INSERT INTO dx_departments (clinic_id, code, name, report_heading) VALUES (%s, 'BIO', 'Bio', "
                     "'BIO') RETURNING id", clinic))
    serum = str(_q(c, "INSERT INTO dx_containers (clinic_id, code, name, colour_hex, sample_type) VALUES (%s, '01', "
                      "'Plain', '#C0392B', 'serum') RETURNING id", clinic))
    edta = str(_q(c, "INSERT INTO dx_containers (clinic_id, code, name, colour_hex, sample_type) VALUES (%s, '07', "
                     "'EDTA', '#8E44AD', 'whole_blood') RETURNING id", clinic))
    doctor = str(_q(c, "INSERT INTO doctors (clinic_id, name, department, specialization, registration_number, "
                       "is_active) VALUES (%s, 'Dr. Rao', 'Lab', 'Path', 'R/1', true) RETURNING id", clinic))
    T = {}
    for key, name, price, cont in (("lipid", "Lipid Profile", 55000, serum), ("cbc", "CBC", 35000, edta),
                                   ("hba1c", "HbA1c", 45000, edta)):
        T[key] = str(_q(c, """INSERT INTO lab_tests (clinic_id, name, test_code, price_paise, department_id,
                              container_id, is_active) VALUES (%s, %s, %s, %s, %s, %s, true) RETURNING id""",
                        clinic, name, key.upper(), price, dept, cont))
        _q(c, "INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, parameter_code) VALUES (%s, %s, %s, %s)",
           clinic, T[key], name, key.upper())
        _approve(c, clinic, T[key], doctor)
    U, uid = api.U, lambda: str(uuid.uuid4())
    users = {"admin": U("admin", "clinic_admin", clinic, uid()),
             "desk": U("desk", "staff", clinic, uid(), ["DX_REGISTER", "DX_BILLING"], staff_role="FRONT_DESK"),
             "tech": U("tech", "staff", clinic, uid(), [], staff_role="LAB_OPERATOR"),
             "nobody": U("nurse", "staff", clinic, uid(), ["IPD_NURSING"], staff_role="NURSE")}
    patient = str(_q(c, "INSERT INTO patients (clinic_id, name, phone, gender, age_years, age_recorded_on) VALUES "
                        "(%s, 'Asha', %s, 'female', 34, CURRENT_DATE) RETURNING id", clinic,
                     "+9166" + uuid.uuid4().hex[:8]))
    return {"api": api, "c": c, "clinic": clinic, "branch": branch, "T": T, "u": users, "patient": patient,
            "serum": serum, "edta": edta, "dept": dept, "doctor": doctor}


def visit(w, items, user="desk", key=None, **extra):
    body = {"patient_id": w["patient"], "items": [{"lab_test_id": w["T"][i]} if isinstance(i, str) else i for i in items],
            "idempotency_key": key or uuid.uuid4().hex, **extra}
    return w["api"].as_(w["u"][user]).call("POST", "/admin/dx/visits", w["clinic"], body)


def test_registry_and_referrers(dxw):
    api, c = dxw["api"].as_(dxw["u"]["desk"]), dxw["clinic"]
    r = api.call("POST", "/admin/dx/patients", c, {"phone": "+919812345678", "name": "Ravi Kumar", "gender": "male",
                                                   "age_years": 40, "data_consent": True})
    assert r.status_code == 201, r.text
    assert api.call("GET", "/admin/dx/patients/search", c, q="Ravi").json()["patients"][0]["name"] == "Ravi Kumar"
    ref = api.call("POST", "/admin/dx/referrers", c, {"name": "Dr. N. Anuradha", "qualification": "MD DM (Neuro)"})
    assert ref.status_code == 201
    dup = api.call("POST", "/admin/dx/referrers", c, {"name": "DR N ANURADHA"})
    assert dup.status_code == 409 and dup.json()["detail"]["candidates"][0]["id"] == ref.json()["id"]
    assert api.call("GET", "/admin/dx/referrers", c, q="anuradha").json()["referrers"][0]["id"] == ref.json()["id"]
    # editing a referrer is an admin action
    assert api.call("PUT", f"/admin/dx/referrers/{ref.json()['id']}", c, {"report_copy": "whatsapp"}).status_code == 403
    dxw["api"].as_(dxw["u"]["admin"])
    assert api.call("PUT", f"/admin/dx/referrers/{ref.json()['id']}", c, {"report_copy": "whatsapp"}).status_code == 200


def test_walk_in_register_bill_issue_pay(dxw):
    api, c = dxw["api"], dxw["clinic"]
    ref = api.as_(dxw["u"]["desk"]).call("POST", "/admin/dx/referrers", c, {"name": "Dr Meena", "qualification": "MD"})
    q = api.call("POST", "/admin/dx/visits/quote", c, {"patient_id": dxw["patient"],
                                                       "items": [{"lab_test_id": dxw["T"]["lipid"]},
                                                                 {"lab_test_id": dxw["T"]["cbc"], "discount_paise": 3500}]})
    assert q.status_code == 200 and q.json()["total_paise"] == 55000 + 31500
    r = visit(dxw, ["lipid", {"lab_test_id": dxw["T"]["cbc"], "discount_paise": 3500}], key="save-click-0001",
              referrer_id=ref.json()["id"], priority="urgent", fasting_status="fasting")
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["visit"]["referrer_snapshot"]["name"] == "Dr Meena" and v["visit"]["priority"] == "urgent"
    assert sum(i["patient_share_paise"] for i in v["items"]) == q.json()["total_paise"]
    assert len(v["samples"]) == 2 and {s["container_code"] for s in v["samples"]} == {"01", "07"}
    # double click on Save: the same visit
    again = visit(dxw, ["lipid"], key="save-click-0001")
    assert again.status_code == 201 and again.json()["visit"]["id"] == v["visit"]["id"] and again.json()["replayed"]
    vid = v["visit"]["id"]
    bill = api.call("POST", f"/admin/dx/visits/{vid}/bill", c, {"issue": True})
    assert bill.status_code == 200, bill.text
    inv = bill.json()["invoices"][0]
    assert inv["status"] == "issued" and inv["appointment_id"] is None and inv["accession_id"] == vid
    assert inv["total_paise"] == 86500 and inv["invoice_number"].startswith("INV-")
    pay = api.call("POST", f"/admin/dx/visits/{vid}/receipts", c, {"mode": "cash", "amount_paise": 86500,
                                                                  "idempotency_key": "cash-0001-abcd"})
    assert pay.status_code == 201, pay.text
    assert api.call("POST", f"/admin/dx/visits/{vid}/receipts", c, {"mode": "cash", "amount_paise": 86500,
                                                                   "idempotency_key": "cash-0001-abcd"}).status_code == 201
    after = api.call("GET", f"/admin/dx/visits/{vid}", c).json()
    assert after["invoices"][0]["status"] == "paid" and after["invoices"][0]["paid_paise"] == 86500
    assert [e["kind"] for e in after["timeline"]] == ["registered", "billed"]
    # a test added after issue goes on a supplementary bill
    add = api.call("POST", f"/admin/dx/visits/{vid}/items", c, {"items": [{"lab_test_id": dxw["T"]["hba1c"]}],
                                                               "idempotency_key": "add-hba1c-0001"})
    assert add.status_code == 200 and len(add.json()["items"]) == 3
    sup = api.call("POST", f"/admin/dx/visits/{vid}/bill", c, {"issue": True}).json()
    assert len(sup["invoices"]) == 2 and sup["invoices"][1]["total_paise"] == 45000
    assert api.call("POST", f"/admin/dx/visits/{vid}/receipts", c, {"mode": "cash", "amount_paise": 45000,
                                                                   "idempotency_key": "cash-0002-abcd"}).status_code == 201
    assert api.call("POST", f"/admin/dx/visits/{vid}/receipts", c, {"mode": "cash", "amount_paise": 1,
                                                                   "idempotency_key": "cash-0003-abcd"}).status_code == 409


def test_errors_are_mapped_with_the_test_name(dxw):
    c = dxw["c"]
    _q(c, "UPDATE lab_tests SET approved_at = NULL, approval_digest = NULL, approved_by = NULL WHERE id = %s",
       dxw["T"]["cbc"])
    r = visit(dxw, ["cbc"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_test_not_approved"
    assert r.json()["detail"]["reason"] == "CBC"
    r = visit(dxw, [{"lab_test_id": dxw["T"]["lipid"], "discount_paise": 20000}])
    assert r.status_code == 403 and r.json()["detail"]["code"] == "dx_discount_over_limit"
    r = visit(dxw, [{"lab_test_id": dxw["T"]["lipid"], "discount_paise": 20000}], user="admin")
    assert r.status_code == 201                                       # an administrator may discount further
    assert visit(dxw, ["lipid"], user="nobody").status_code == 403
    r = visit(dxw, ["lipid"], billing_mode="b2b_credit")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_b2b_not_enabled"


def test_technician_sees_no_money_and_cancel_rules(dxw):
    api, c = dxw["api"], dxw["clinic"]
    v = visit(dxw, ["lipid", "cbc"]).json()
    vid = v["visit"]["id"]
    tech = api.as_(dxw["u"]["tech"]).call("GET", f"/admin/dx/visits/{vid}", c).json()
    assert "price_paise" not in tech["items"][0] and tech["invoices"] == [] and tech["samples"]
    assert api.call("POST", f"/admin/dx/visits/{vid}/bill", c, {"issue": False}).status_code == 403
    api.as_(dxw["u"]["desk"])
    api.call("POST", f"/admin/dx/visits/{vid}/bill", c, {"issue": False})
    lipid = next(i for i in v["items"] if i["item_name"] == "Lipid Profile")
    r = api.call("POST", f"/admin/dx/visits/{vid}/items/{lipid['id']}/cancel", c, {"reason": "patient declined"})
    assert r.status_code == 200
    detail = api.call("GET", f"/admin/dx/visits/{vid}", c).json()
    assert (detail["invoices"][0]["status"], detail["invoices"][0]["total_paise"]) == ("draft", 35000)
    assert api.call("POST", f"/admin/dx/visits/{vid}/bill", c, {"issue": True}).json()["invoices"][0]["total_paise"] == 35000
    cbc = next(i for i in v["items"] if i["item_name"] == "CBC")
    r = api.call("POST", f"/admin/dx/visits/{vid}/items/{cbc['id']}/cancel", c, {"reason": "changed mind"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_refund_first"
    r = api.call("POST", f"/admin/dx/visits/{vid}/cancel", c, {"reason": "wrong patient"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_refund_first"
    assert api.call("POST", f"/admin/dx/visits/{vid}/items/{uuid.uuid4()}/cancel", c,
                    {"reason": "nope"}).status_code == 404


def test_pinned_login_reaches_only_its_branch_and_another_tenant_nothing(dxw):
    api, c = dxw["api"], dxw["clinic"]
    other_branch = str(_q(dxw["c"], "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Kukatpally') RETURNING id", c))
    _q(dxw["c"], "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name, kind) VALUES (%s, %s, 'KPHB', "
                 "'Kukatpally', 'collection')", c, other_branch)
    v = visit(dxw, ["cbc"], branch_id=dxw["branch"]).json()
    pinned = api.U("kdesk", "staff", c, str(uuid.uuid4()), ["DX_REGISTER", "DX_READ"], staff_role="FRONT_DESK",
                   branch_id=other_branch)
    api.as_(pinned)
    assert api.call("GET", f"/admin/dx/visits/{v['visit']['id']}", c).status_code == 404
    assert api.call("GET", "/admin/dx/visits", c).json()["visits"] == []
    own = visit(dxw, ["cbc"], user="desk", branch_id=other_branch).json()        # collected at KPHB, processed at Main
    api.as_(pinned)
    assert [x["id"] for x in api.call("GET", "/admin/dx/visits", c).json()["visits"]] == [own["visit"]["id"]]
    assert api.call("POST", "/admin/dx/visits", c, {"patient_id": dxw["patient"], "branch_id": dxw["branch"],
                                                    "items": [{"lab_test_id": dxw["T"]["cbc"]}],
                                                    "idempotency_key": uuid.uuid4().hex}).status_code == 403
    stranger = api.U("x", "clinic_admin", str(uuid.uuid4()), str(uuid.uuid4()))
    assert api.as_(stranger).call("GET", f"/admin/dx/visits/{v['visit']['id']}", c).status_code in (403, 404)


def test_wind_down_closes_new_visits_but_not_existing_ones(dxw, monkeypatch):
    from app.services import dx as dx_service
    api, c = dxw["api"], dxw["clinic"]
    v = visit(dxw, ["cbc"]).json()

    async def _busy(clinic_id):
        return {"visits_in_process": 1, "samples_in_transit": 0, "unreleased_results": 0, "blocking": True}
    monkeypatch.setattr(dx_service, "deactivation_preview", _busy)
    assert api.owner_toggle(c, "dx_enabled", False, confirm=True).status_code == 200
    r = visit(dxw, ["cbc"])
    assert r.status_code == 409 and "switched off" in r.text
    api.as_(dxw["u"]["desk"])
    assert api.call("POST", f"/admin/dx/visits/{v['visit']['id']}/bill", c, {"issue": True}).status_code == 200
