"""DX-24: DPDP erasure redacts Diagnostic OS visits in place (real Postgres, real service).

The visit, its tests, tubes, orders and bill lines stay (NMC clinical retention, GST
records); the name / DOB snapshot, external reference and delivery preferences go;
another patient's visit is untouched.
"""

import asyncio
import uuid

from app.services.data_retention import DataRetentionService
from tests.test_dx_visits_e2e import dxw, visit  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def test_erasure_redacts_dx_visits_and_keeps_the_record(dxw):  # noqa: F811
    c = dxw["c"]
    mine = visit(dxw, ["cbc"], external_ref="IP-778", delivery_prefs={"patient": True}).json()
    dxw["api"].as_(dxw["u"]["desk"]).call("POST", f"/admin/dx/visits/{mine['visit']['id']}/bill", dxw["clinic"],
                                          {"issue": True})
    other = str(_q(c, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Ravi', %s) RETURNING id",
                   dxw["clinic"], "+9164" + uuid.uuid4().hex[:8]))
    theirs = visit({**dxw, "patient": other}, ["cbc"]).json()
    phone = _q(c, "SELECT phone FROM patients WHERE id = %s", dxw["patient"])
    out = asyncio.run(DataRetentionService().anonymize_clinical_records(dxw["clinic"], phone))
    assert out["dx_visits_redacted"] == 1 and not [e for e in out["errors"] if "dx" in e]
    row = _q(c, "SELECT to_jsonb(a) FROM dx_accessions a WHERE id = %s", mine["visit"]["id"])
    assert row["patient_snapshot"]["name"] == "[REDACTED]" and "date_of_birth" not in row["patient_snapshot"]
    assert row["patient_snapshot"]["gender"] == "female" and row["patient_snapshot"]["redacted_at"]
    assert row["external_ref"] is None and row["delivery_prefs"] == {}
    for table, col in (("dx_accession_items", "accession_id"), ("dx_samples", "accession_id"),
                       ("opd_orders", "accession_id"), ("opd_invoices", "accession_id")):
        assert _q(c, f"SELECT count(*) FROM {table} WHERE {col} = %s", mine["visit"]["id"]) >= 1, table
    assert _q(c, "SELECT patient_snapshot ->> 'name' FROM dx_accessions WHERE id = %s", theirs["visit"]["id"]) == "Ravi"
    # running it again changes nothing more
    again = asyncio.run(DataRetentionService().anonymize_clinical_records(dxw["clinic"], phone))
    assert again.get("dx_visits_redacted", 0) == 0
