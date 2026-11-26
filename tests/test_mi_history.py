"""Legacy history (MI-05): imported visits and lab values are read-only, labelled 'Imported record (from …)', never a
signed Kriya encounter / prescription; the diagnosis map is deterministic; rows unknown to the imported list are
reported, not guessed; re-import never duplicates; dues; chronic list → CC candidates only with CC on."""

import uuid

from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)
from tests.test_mi_api_helpers import FX, mi_clinic, run, upload


def _world(api):
    clinic = mi_clinic(api)
    run(api, clinic, "patients", (FX / "mocdoc_patients.csv").read_bytes(), preset="mocdoc")
    rec = str(_q(api.conn, "SELECT id FROM patient_records WHERE clinic_id = %s AND external_id = 'MR-1001'", clinic))
    live = str(_q(api.conn, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Lakshmi Devi', '+919848022338') "
                            "RETURNING id", clinic))
    assert api.call("POST", "/admin/mi/links", clinic, {"patient_record_id": rec, "patient_id": live}).status_code == 201
    return clinic, live


def test_history_and_labs_are_read_only_labelled_and_idempotent(api):  # noqa: F811
    clinic, live = _world(api)
    assert api.call("PUT", "/admin/mi/diagnosis-map", clinic,
                    {"legacy_text": "Type 2 Diabetes Mellitus", "icd10_code": "e11.9"}).status_code == 200
    dry, a = run(api, clinic, "history", (FX / "practo_history.csv").read_bytes(), preset="practo_insta")
    assert dry["rows_ok"] == 2 and dry["rows_error"] == 2
    probs = " ".join(e["problem"] for e in dry["errors"])
    assert "import the patient list first" in probs and "not a date" in probs
    assert a.json()["written"] == 2
    _, labs = run(api, clinic, "lab_values", (FX / "mocdoc_labs.csv").read_bytes(), preset="mocdoc")
    assert labs.json()["written"] == 3
    card = api.call("GET", f"/admin/mi/patients/{live}/legacy", clinic).json()
    assert card["linked"] and len(card["encounters"]) == 2 and len(card["lab_values"]) == 3
    assert all(e["legacy"] and e["label"].startswith("Imported record (from Practo Insta, ") for e in card["encounters"])
    dm = next(e for e in card["encounters"] if e["diagnosis_text"].startswith("Type 2"))
    assert dm["icd10_code"] == "E11.9"                                # filled from the clinic's map at dry run
    assert {x["flag"] for x in card["lab_values"]} == {"high", None}
    # never a signed Kriya record, never a prescription the firewall or protocol engine could read as a decision
    assert _q(api.conn, "SELECT count(*) FROM opd_encounters WHERE clinic_id = %s", clinic) == 0
    assert _q(api.conn, "SELECT count(*) FROM opd_prescriptions WHERE clinic_id = %s", clinic) == 0
    # a second file with the same visits (another export date) adds nothing
    _, again = run(api, clinic, "history", (FX / "practo_history.csv").read_bytes().replace(b"Medicines", b"Medicines "),
                   preset="practo_insta")
    assert again.json()["written"] == 0 and again.json()["skipped_existing"] == 2


def test_diagnosis_map_backfills_deterministically(api):  # noqa: F811
    clinic, _ = _world(api)
    run(api, clinic, "history", "Patient ID,Visit Date,Diagnosis\nMR-1001,01/01/2026,  essential   HYPERTENSION \n")
    api.call("PUT", "/admin/mi/diagnosis-map", clinic, {"legacy_text": "Essential hypertension", "icd10_code": "I10"})
    assert _q(api.conn, "SELECT icd10_code FROM mi_legacy_encounters WHERE clinic_id = %s", clinic) == "I10"
    assert api.call("PUT", "/admin/mi/diagnosis-map", clinic, {"legacy_text": "x", "icd10_code": "BAD"}).status_code == 422
    assert len(api.call("GET", "/admin/mi/diagnosis-map", clinic).json()["map"]) == 1


def test_dues_shown_and_closed_only_through_an_issued_bill(api):  # noqa: F811
    clinic, live = _world(api)
    _, a = run(api, clinic, "dues", "Patient ID,Balance,Date,Bill No\nMR-1001,\"1,250.00\",01/03/2026,OLD-77\n")
    card = api.call("GET", f"/admin/mi/patients/{live}/legacy", clinic).json()
    assert card["open_dues_paise"] == 125000
    due = card["dues"][0]["id"]
    assert api.call("POST", f"/admin/mi/dues/{due}/close", clinic, {"status": "collected"}).status_code == 422
    assert api.call("POST", f"/admin/mi/dues/{due}/close", clinic,
                    {"status": "collected", "invoice_id": str(uuid.uuid4())}).status_code == 422
    assert api.call("POST", f"/admin/mi/dues/{due}/close", clinic, {"status": "written_off"}).status_code == 200
    # a closed due is in use: its batch cannot be undone
    assert api.call("DELETE", f"/admin/mi/batches/{a.json()['batch_id']}", clinic).status_code == 409


def test_chronic_list_needs_continuous_care_and_never_enrolls(api):  # noqa: F811
    clinic, _ = _world(api)
    csv = "Patient ID,Condition,ICD\nMR-1001,Type 2 diabetes,E11.9\n"
    dry = upload(api, clinic, "chronic_list", csv).json()
    r = api.call("POST", f"/admin/mi/chronic_list/apply/{dry['id']}", clinic)
    assert r.status_code == 409 and "Continuous Care" in r.json()["detail"]
    assert _q(api.conn, "SELECT status FROM mi_batches WHERE id = %s", dry["id"]) == "dry_run"
    assert api.owner_toggle(clinic, "care_enabled", True).status_code == 200
    r = api.call("POST", f"/admin/mi/chronic_list/apply/{dry['id']}", clinic)
    assert r.status_code == 200 and r.json()["written"] == 1
    cands = api.call("GET", "/admin/mi/chronic-candidates", clinic).json()["candidates"]
    assert len(cands) == 1 and cands[0]["status"] == "new" and cands[0]["icd10_code"] == "E11.9"


def test_inpatient_cutover_is_a_report_only(api):  # noqa: F811
    clinic, _ = _world(api)
    dry = upload(api, clinic, "inpatients_cutover", "Patient ID,Ward,Bed,Admitted On\nMR-1001,Female Ward,F-12,20/11/2026\n")
    assert dry.status_code == 200 and dry.json()["report"][0]["bed"] == "F-12"
    r = api.call("POST", f"/admin/mi/inpatients_cutover/apply/{dry.json()['id']}", clinic)
    assert r.status_code == 422 and "admit each patient" in r.json()["detail"]
