"""Patients (MI-04): the legacy list lands in patient_records through the 088 importer (quota, dedupe), front-desk
search shows legacy hits, "Use this record" links, the phone match queue is staff-confirmed and never auto-merges
(shared family phones), consent is never set, DPDP erasure removes linked legacy rows."""

import uuid

from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)
from tests.test_mi_api_helpers import FX, mi_clinic, run


def _patient(api, clinic, name, phone):
    return str(_q(api.conn, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, %s, %s) RETURNING id",
                  clinic, name, phone))


def test_patient_list_import_search_link_and_undo(api):  # noqa: F811
    clinic = mi_clinic(api)
    dry, a = run(api, clinic, "patients", (FX / "mocdoc_patients.csv").read_bytes(), preset="mocdoc")
    assert dry["rows_ok"] == 4 and a.status_code == 200 and a.json()["written"] == 4
    assert _q(api.conn, "SELECT count(*) FROM patient_records WHERE clinic_id = %s", clinic) == 4
    # the importer never touches the live registry or anyone's consent
    assert _q(api.conn, "SELECT count(*) FROM patients WHERE clinic_id = %s", clinic) == 0
    hits = api.call("GET", "/admin/mi/legacy-search", clinic, q="lakshmi").json()["records"]
    assert len(hits) == 1 and hits[0]["external_id"] == "MR-1001" and hits[0]["linked_patient_id"] is None
    assert api.call("GET", "/admin/mi/legacy-search", clinic, q="9848022338").json()["records"][0]["id"] == hits[0]["id"]
    assert api.call("GET", "/admin/mi/legacy-search", clinic, q="MR-1002").json()["records"][0]["full_name"] == "రవి కుమార్"
    assert api.call("GET", "/admin/mi/legacy-search", clinic, q="ab").status_code == 422
    live = _patient(api, clinic, "Lakshmi Devi", "+919848022338")
    r = api.call("POST", "/admin/mi/links", clinic, {"patient_record_id": hits[0]["id"], "patient_id": live})
    assert r.status_code == 201 and r.json()["legacy_mrn"] == "MR-1001" and r.json()["source"] == "front_desk"
    assert api.call("POST", "/admin/mi/links", clinic, {"patient_record_id": hits[0]["id"], "patient_id": live}).status_code == 409
    assert _q(api.conn, "SELECT opted_in IS NOT TRUE FROM patients WHERE id = %s", live)       # consent untouched
    # a linked list is in use: undo refused, nothing removed
    assert api.call("DELETE", f"/admin/mi/batches/{a.json()['batch_id']}", clinic).status_code == 409
    assert _q(api.conn, "SELECT count(*) FROM patient_records WHERE clinic_id = %s", clinic) == 4


def test_unlinked_list_undo_removes_the_088_batch(api):  # noqa: F811
    clinic = mi_clinic(api)
    _, a = run(api, clinic, "patients", "Name,Phone\nAsha,9000000001\n")
    assert api.call("DELETE", f"/admin/mi/batches/{a.json()['batch_id']}", clinic).status_code == 200
    assert _q(api.conn, "SELECT count(*) FROM patient_records WHERE clinic_id = %s", clinic) == 0


def test_phone_match_queue_never_auto_merges(api):  # noqa: F811
    clinic = mi_clinic(api)
    run(api, clinic, "patients", "Patient ID,Name,Phone\nF-1,Ramu,9000000777\nF-2,Sita,9000000777\n")
    p = _patient(api, clinic, "Ramu K", "+919000000777")                 # one family phone, two legacy people
    assert api.call("POST", "/admin/mi/matches/refresh", clinic).json()["added"] == 2
    assert api.call("POST", "/admin/mi/matches/refresh", clinic).json()["added"] == 0           # idempotent
    m = api.call("GET", "/admin/mi/matches", clinic).json()["matches"]
    assert len(m) == 2 and _q(api.conn, "SELECT count(*) FROM mi_patient_links WHERE clinic_id = %s", clinic) == 0
    assert all("XXXX" in x["patient"]["phone"] for x in m)                # masked
    ramu = next(x for x in m if x["record"]["full_name"] == "Ramu")
    sita = next(x for x in m if x["record"]["full_name"] == "Sita")
    assert api.call("POST", f"/admin/mi/matches/{ramu['id']}/confirm", clinic).status_code == 200
    assert api.call("POST", f"/admin/mi/matches/{sita['id']}/reject", clinic).status_code == 200
    assert api.call("POST", f"/admin/mi/matches/{sita['id']}/confirm", clinic).status_code == 409
    links = _q(api.conn, "SELECT json_agg(source) FROM mi_patient_links WHERE clinic_id = %s AND patient_id = %s", clinic, p)
    assert links == ["match_queue"]
    assert api.call("GET", "/admin/mi/matches", clinic).json()["matches"] == []


def test_front_desk_can_search_and_link_but_not_import(api):  # noqa: F811
    clinic = mi_clinic(api)
    run(api, clinic, "patients", "Name,Phone\nAsha,9000000001\n")
    api.as_(api.U("desk", "staff", clinic, str(uuid.uuid4()), ["OPD_FRONT_DESK"], staff_role="FRONT_DESK"))
    assert api.call("GET", "/admin/mi/legacy-search", clinic, q="asha").status_code == 200
    assert api.call("GET", "/admin/mi/batches", clinic).status_code == 403
    api.as_(api.U("doc", "staff", clinic, str(uuid.uuid4()), ["OPD_CLINICAL"], staff_role="DOCTOR"))
    assert api.call("GET", "/admin/mi/legacy-search", clinic, q="asha").status_code == 403


def test_dpdp_erasure_removes_linked_legacy_rows(api, monkeypatch):  # noqa: F811
    import asyncio
    from app.services import data_retention
    clinic = mi_clinic(api)
    run(api, clinic, "patients", "Patient ID,Name,Phone\nE-1,Old Name,9111111111\n")
    rec = _q(api.conn, "SELECT id FROM patient_records WHERE clinic_id = %s", clinic)
    live = _patient(api, clinic, "New Name", "+919222222222")             # a different phone than the legacy row
    api.call("POST", "/admin/mi/links", clinic, {"patient_record_id": str(rec), "patient_id": live})
    svc = next(v for v in vars(data_retention).values() if hasattr(v, "anonymize_clinical_records")
               and not isinstance(v, type))
    asyncio.run(svc.anonymize_clinical_records(clinic, "+919222222222"))
    assert _q(api.conn, "SELECT count(*) FROM patient_records WHERE clinic_id = %s", clinic) == 0
    assert _q(api.conn, "SELECT count(*) FROM mi_patient_links WHERE clinic_id = %s", clinic) == 0
