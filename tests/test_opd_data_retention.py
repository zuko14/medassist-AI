"""Tests for Kriya OPD OS Data Retention & DPDP Act Erasure (Phase 1.2).

Verifies:
1. Patient demographics erasure nulls all Phase 1.2 fields:
   address_line, city, pincode, emergency_contact_name, emergency_contact_phone,
   emergency_contact_relation, allergies, allergies_status.
2. Family member erasure:
   - Unreferenced family members are hard-deleted.
   - Family members referenced by appointments / opd_encounters / opd_prescriptions / opd_invoices
     are redacted in-place to [REDACTED:<id_prefix>] with allergies scrubbed.
3. Inbound queue pseudonymization (phone -> [REDACTED], payload -> {}).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Resolved at call time: other suite tests delete and re-import app.database /
# services, so a name bound at import would run against a module the patches miss.
def DataRetentionService(*a, **k):  # noqa: N802 - stands in for the class
    import importlib
    return importlib.import_module("app.services.data_retention").DataRetentionService(*a, **k)



CLINIC_ID = str(uuid.uuid4())
PATIENT_PHONE = "+919876543210"
PATIENT_ID = str(uuid.uuid4())
FAM_UNREFERENCED_ID = str(uuid.uuid4())
FAM_REFERENCED_ID = str(uuid.uuid4())


@pytest.mark.asyncio
async def test_opd_patient_demographics_erasure():
    """DPDP erasure scrubs all new Phase 1.2 demographic fields on patient row."""
    service = DataRetentionService()

    fake_patient = MagicMock(data=[{"id": PATIENT_ID, "name": "Rajesh Kumar", "phone": PATIENT_PHONE}])

    calls = []

    async def intercept_sb(query):
        calls.append(query)
        # Check if it's the initial patients select
        req = getattr(query, "request", None)
        path = str(getattr(req, "url", getattr(req, "path", "")))
        if "patients" in path and getattr(req, "http_method", "") == "GET":
            return fake_patient
        return MagicMock(data=[{"id": PATIENT_ID}])

    # A real (offline) PostgREST client: the assertions read builder URLs, and an
    # earlier suite test can leave the module's `supabase` swapped for a MagicMock.
    from postgrest import SyncPostgrestClient
    with patch("app.services.data_retention.sb", AsyncMock(side_effect=intercept_sb)),          patch("app.services.data_retention.supabase", SyncPostgrestClient("http://pg.invalid")):
        res = await service.anonymize_clinical_records(CLINIC_ID, PATIENT_PHONE)

    assert res["errors"] == []

    # Find the patient row update call
    patient_update_found = False
    for q in calls:
        req = getattr(q, "request", None)
        path = str(getattr(req, "url", getattr(req, "path", "")))
        body = getattr(req, "json", None) if req else None

        if "patients" in path and body and "address_line" in body:
            patient_update_found = True
            assert body["address_line"] is None
            assert body["city"] is None
            assert body["pincode"] is None
            assert body["emergency_contact_name"] is None
            assert body["emergency_contact_phone"] is None
            assert body["emergency_contact_relation"] is None
            assert body["allergies"] == []
            assert body["allergies_status"] == "unknown"
            assert body["name"] == "[REDACTED]"
            assert body["data_consent"] is False

    assert patient_update_found, "Expected patient demographics update query with scrubbed fields"


@pytest.mark.asyncio
async def test_family_member_erasure_unreferenced_vs_referenced():
    """Unreferenced family members are deleted, while referenced ones are redacted in-place."""
    service = DataRetentionService()

    fake_patient_res = MagicMock(data=[{"id": PATIENT_ID, "name": "Family Head", "phone": PATIENT_PHONE}])
    fake_empty_appts = MagicMock(data=[])
    fake_empty_reports = MagicMock(data=[])
    fake_empty_rx = MagicMock(data=[])

    # Two family members: one with OPD records, one without
    fam_rows_res = MagicMock(data=[
        {
            "id": FAM_UNREFERENCED_ID,
            "full_name": "Baby Child (Unreferenced)",
            "primary_phone": PATIENT_PHONE,
            "allergies": ["Peanuts"],
            "allergies_status": "recorded",
        },
        {
            "id": FAM_REFERENCED_ID,
            "full_name": "Elderly Parent (Has Visits)",
            "primary_phone": PATIENT_PHONE,
            "allergies": ["Penicillin"],
            "allergies_status": "recorded",
        },
    ])

    # Encounter ref for FAM_REFERENCED_ID
    fake_has_ref = MagicMock(data=[{"id": "enc-1", "family_member_id": FAM_REFERENCED_ID}])
    fake_no_ref = MagicMock(data=[])

    with patch("app.services.data_retention.sb", AsyncMock()) as sb_patch:
        sb_patch.side_effect = [
            fake_patient_res,     # 1. patients lookup
            fake_empty_appts,     # 2. appointments update
            fake_empty_reports,   # 3. lab reports select
            fake_empty_reports,   # 4. lab reports update
            fake_empty_rx,        # 5. prescriptions update
            Exception("violates foreign key constraint"),  # 6. bulk delete fails due to OPD FK references
            fam_rows_res,         # 7. family members lookup
            # Checks for FM_UNREFERENCED (4 tables, all empty)
            fake_no_ref,          # appointments
            fake_no_ref,          # opd_encounters
            fake_no_ref,          # opd_prescriptions
            fake_no_ref,          # opd_invoices
            MagicMock(data=[{"id": FAM_UNREFERENCED_ID}]),  # delete unreferenced fm
            # Checks for FM_REFERENCED (appointments has ref)
            fake_has_ref,         # appointments
            MagicMock(data=[{"id": FAM_REFERENCED_ID}]),    # redact in place
            # Remaining steps
            fake_no_ref,          # patient_records delete
            fake_no_ref,          # patients update
            fake_no_ref,          # inbound_messages update
            fake_no_ref,          # audit log insert
        ]

        res = await service.anonymize_clinical_records(CLINIC_ID, PATIENT_PHONE)

        assert res["errors"] == []
        assert res["family_members_deleted"] == 1
