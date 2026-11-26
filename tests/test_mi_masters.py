"""Masters (MI-03) through the real routes on real Postgres: doctors (+ weekly schedule template), services
(update with the old value kept for undo), packages (never re-priced), payers (merge by name). Idempotent re-apply,
duplicate reporting, atomic undo, undo refused once referenced."""

import pytest

from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)
from tests.test_mi_api_helpers import FX, mi_clinic, run, upload


def test_doctors_mocdoc_apply_idempotent_and_undo(api):  # noqa: F811
    clinic = mi_clinic(api)
    raw = (FX / "mocdoc_doctors.csv").read_bytes()
    dry, a = run(api, clinic, "doctors", raw, preset="mocdoc")
    assert (dry["rows_ok"], dry["rows_error"], dry["rows_duplicate"], dry["existing"]) == (2, 1, 1, 0)
    assert a.status_code == 200 and a.json()["written"] == 2
    doc = _q(api.conn, "SELECT row_to_json(d) FROM doctors d WHERE clinic_id = %s AND name = 'Dr. Ramesh Rao'", clinic)
    assert (doc["available_days"], doc["consultation_fee"], doc["registration_number"]) == (
        "Mon,Tue,Wed,Thu,Fri,Sat", 800, "TSMC/2010/1234")
    # the same file again: the dry run says so and apply changes nothing
    dry2, a2 = run(api, clinic, "doctors", raw, preset="mocdoc")
    assert dry2["already_applied"] is True and dry2["existing"] == 2
    assert a2.json() == {"already_applied": True, "batch_id": a.json()["batch_id"], "written": 0}
    # a different file with the same doctors: duplicates by registration number / name are never added twice
    _, a3 = run(api, clinic, "doctors", "Doctor,Department,Reg No\nDr. Ramesh Rao,Cardiology,TSMC/2010/1234\n")
    assert a3.json()["written"] == 0 and a3.json()["skipped_existing"] == 1
    assert _q(api.conn, "SELECT count(*) FROM doctors WHERE clinic_id = %s", clinic) == 2
    # undo removes exactly what the batch created
    r = api.call("DELETE", f"/admin/mi/batches/{a.json()['batch_id']}", clinic)
    assert r.status_code == 200 and r.json()["deleted"] == 2
    assert _q(api.conn, "SELECT count(*) FROM doctors WHERE clinic_id = %s", clinic) == 0
    assert api.call("DELETE", f"/admin/mi/batches/{a.json()['batch_id']}", clinic).status_code == 409
    assert api.call("POST", f"/admin/mi/doctors/apply/{dry['id']}", clinic).status_code == 409   # applied/undone


def test_undo_refused_once_an_imported_doctor_is_used_and_nothing_changes(api):  # noqa: F811
    clinic = mi_clinic(api)
    _, a = run(api, clinic, "doctors", "Doctor,Department\nDr. Busy,Medicine\nDr. Idle,Medicine\n")
    doc = _q(api.conn, "SELECT id FROM doctors WHERE clinic_id = %s AND name = 'Dr. Busy'", clinic)
    pat = _q(api.conn, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'P', '+919000000123') RETURNING id", clinic)
    appt = _q(api.conn, """INSERT INTO appointments (clinic_id, patient_id, doctor_id, doctor_name, patient_phone, department,
                           appointment_date, appointment_time, status) VALUES (%s, %s, %s, 'Dr. Busy', '+919000000123',
                           'Medicine', CURRENT_DATE, '10:00', 'confirmed') RETURNING id""", clinic, pat, doc)
    _q(api.conn, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id) VALUES (%s, %s, %s, %s)",
       clinic, appt, pat, doc)                                          # the doctor is now on a clinical record
    r = api.call("DELETE", f"/admin/mi/batches/{a.json()['batch_id']}", clinic)
    assert r.status_code == 409 and "in use" in r.json()["detail"]
    assert _q(api.conn, "SELECT count(*) FROM doctors WHERE clinic_id = %s", clinic) == 2        # atomic: both stay
    assert _q(api.conn, "SELECT status FROM mi_batches WHERE id = %s", a.json()["batch_id"]) == "applied"


def test_services_update_keeps_old_value_and_undo_restores(api):  # noqa: F811
    clinic = mi_clinic(api)
    _q(api.conn, "INSERT INTO billable_services (clinic_id, service_code, name, category, standard_price_paise) "
                 "VALUES (%s, 'ECG', 'ECG', 'diagnostic', 30000)", clinic)
    dry, a = run(api, clinic, "services", (FX / "practo_services.csv").read_bytes(), preset="practo_insta")
    assert dry["existing"] == 1 and dry["rows_error"] == 1
    assert a.json()["written"] == 2 and a.json()["updated"] == 1
    assert _q(api.conn, "SELECT standard_price_paise FROM billable_services WHERE clinic_id = %s AND service_code = 'ECG'",
              clinic) == 35000
    api.call("DELETE", f"/admin/mi/batches/{a.json()['batch_id']}", clinic)
    assert _q(api.conn, "SELECT standard_price_paise || ':' || name FROM billable_services WHERE clinic_id = %s "
                        "AND service_code = 'ECG'", clinic) == "30000:ECG"
    assert _q(api.conn, "SELECT count(*) FROM billable_services WHERE clinic_id = %s", clinic) == 1


def test_packages_need_a_price_and_are_never_repriced(api):  # noqa: F811
    clinic = mi_clinic(api)
    csv = "Code,Name,Price,Includes,Inclusions\nNORMAL_DEL,Normal delivery,25000,\"bed, nursing\",2 days ward\nX,No price,,,\n"
    dry, a = run(api, clinic, "packages", csv)
    assert dry["rows_ok"] == 1 and {e["column"] for e in dry["errors"]} >= {"code", "price_paise"}
    assert a.json()["written"] == 1
    _, a2 = run(api, clinic, "packages", "Code,Name,Price\nNORMAL_DEL,Normal delivery,99000\n")
    assert a2.json()["skipped_existing"] == 1
    assert _q(api.conn, "SELECT price_paise FROM ipd_packages WHERE clinic_id = %s", clinic) == 2500000


def test_payers_merge_by_name(api):  # noqa: F811
    clinic = mi_clinic(api)
    _, a = run(api, clinic, "payers", "Payer,Type,Code\nStar Health,Insurance,STAR\nMedi Assist,TPA,MA01\nPM-JAY,Govt,\n")
    assert a.json()["written"] == 3
    _, a2 = run(api, clinic, "payers", "Payer,Type\nstar  health,insurance\n")
    assert a2.json()["skipped_existing"] == 1
    assert _q(api.conn, "SELECT payer_type FROM insurance_payers WHERE clinic_id = %s AND name = 'PM-JAY'", clinic) \
        == "government_scheme"


def test_kind_mismatch_and_tenant_scope(api):  # noqa: F811
    a, b = mi_clinic(api), mi_clinic(api)
    api.as_(api.U("admin", "clinic_admin", a, __import__("uuid").uuid4().hex))
    dry = upload(api, a, "payers", "Payer,Type\nA,TPA\n").json()
    assert api.call("POST", f"/admin/mi/doctors/apply/{dry['id']}", a).status_code == 404
    api.as_(api.U("admin", "clinic_admin", b, __import__("uuid").uuid4().hex))
    assert api.call("POST", f"/admin/mi/payers/apply/{dry['id']}", b).status_code == 404
    assert api.call("GET", f"/admin/mi/batches/{dry['id']}", b).status_code == 404
