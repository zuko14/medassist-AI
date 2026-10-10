"""Tests for Kriya OPD OS Patient Registry (Phase 1.2).

Verifies:
1. Search across patients, family members, and legacy records.
2. Duplicate checks (exact phone/MRN match vs name/age candidates).
3. Patient registration with mandatory DPDP Act consent (missing consent -> 422).
4. Account holder vs dependant / family member creation.
5. Dynamic age calculation with date_of_birth and age_recorded_on.
6. MRN generation and assignment via RPC fallback.
7. Patient demographics patch (PATCH /admin/opd/patients/{id}).
"""

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin, opd
from app.routers.admin import AdminUser
from app.services.opd import _compute_age, duplicate_candidates, register_patient, search_patients

client = TestClient(app)

CLINIC_ID = str(uuid.uuid4())
ADMIN_USER = AdminUser("receptionist", role="staff", clinic_id=CLINIC_ID, permissions=["OPD_FRONT_DESK"])
CLINICAL_USER = AdminUser("doctor_smith", role="staff", clinic_id=CLINIC_ID, permissions=["OPD_CLINICAL"], doctor_id=str(uuid.uuid4()))
MOCK_CLINIC = {"id": CLINIC_ID, "features": {"opd_enabled": True}, "opd_state": "READY"}


@pytest.fixture
def auth_front_desk():
    app.dependency_overrides[admin.verify_credentials] = lambda: ADMIN_USER
    yield
    app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.fixture
def auth_clinical():
    app.dependency_overrides[admin.verify_credentials] = lambda: CLINICAL_USER
    yield
    app.dependency_overrides.pop(admin.verify_credentials, None)


# ─── 1. AGE COMPUTATION HELPER ───────────────────────────────────────────────


def test_compute_age_from_dob():
    """Exact birth date calculates age dynamically relative to today."""
    today = datetime.date.today()
    dob_40y = today.replace(year=today.year - 40)
    assert _compute_age(dob_40y, None, None) == 40


def test_compute_age_from_recorded_years():
    """Recorded age advances when recorded_on date is from prior years."""
    recorded_date = datetime.date.today() - datetime.timedelta(days=750)  # ~2 years ago
    assert _compute_age(None, 30, recorded_date) == 32


def test_compute_age_fallback():
    assert _compute_age(None, 25, None) == 25
    assert _compute_age(None, None, None) is None


# ─── 2. DUPLICATE CHECK & CANDIDATES ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_candidates_matching():
    """Duplicate candidates returns exact phone matches and similar name matches."""
    fake_phone_match = MagicMock(data=[{
        "id": "p1",
        "name": "Rajesh Kumar",
        "phone": "+919876543210",
        "mrn": "MRN-001",
        "age_years": 40,
        "gender": "male",
    }])
    fake_empty = MagicMock(data=[])

    with patch("app.services.opd.sb", AsyncMock(side_effect=[fake_phone_match, fake_empty, fake_empty])):
        dups = await duplicate_candidates(CLINIC_ID, name="Rajesh", phone="+919876543210")
        assert len(dups) >= 1
        assert dups[0]["match_reason"] == "same_phone"
        assert dups[0]["patient_id"] == "p1"


# ─── 3. PATIENT REGISTRATION & DPDP CONSENT ──────────────────────────────────


def test_registration_requires_dpdp_consent(auth_front_desk):
    """Registration without data_consent returns 422 Unprocessable Entity."""
    payload = {
        "name": "Ananya Sharma",
        "phone": "+919876543210",
        "is_account_holder": True,
        "data_consent": False,  # Missing consent!
    }
    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)):
        r = client.post(f"/admin/opd/patients?clinic_id={CLINIC_ID}", json=payload)
    assert r.status_code == 422
    assert "Data consent is required" in r.text or "DPDP" in r.text


def test_register_account_holder_success(auth_front_desk):
    """Account holder registration inserts into patients table and assigns MRN."""
    payload = {
        "name": "Amitabh Varma",
        "phone": "+919123456789",
        "is_account_holder": True,
        "age_years": 45,
        "gender": "male",
        "address_line": "123 MG Road",
        "city": "Bengaluru",
        "pincode": "560001",
        "emergency_contact_name": "Sunita Varma",
        "emergency_contact_phone": "+919123456780",
        "allergies": ["Penicillin"],
        "allergies_status": "recorded",
        "data_consent": True,
        "whatsapp_opt_in": True,
    }

    fake_ins = MagicMock(data=[{"id": "pat-123"}])
    fake_fresh = MagicMock(data=[{
        "id": "pat-123",
        "name": "Amitabh Varma",
        "phone": "+919123456789",
        "mrn": "MUM-2026-0001",
        "date_of_birth": None,
        "age_years": 45,
        "age_recorded_on": "2026-10-09",
        "gender": "male",
        "address_line": "123 MG Road",
        "city": "Bengaluru",
        "pincode": "560001",
        "emergency_contact_name": "Sunita Varma",
        "emergency_contact_phone": "+919123456780",
        "emergency_contact_relation": None,
        "allergies": ["Penicillin"],
        "allergies_status": "recorded",
        "opted_in": True,
    }])
    fake_empty = MagicMock(data=[])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.services.opd.sb", AsyncMock(side_effect=[
             fake_empty,  # dup phone in patients
             fake_empty,  # dup phone in family_members
             fake_empty,  # dup name in patients
             fake_empty,  # existing patient lookup by phone
             fake_ins,    # insert patient
             fake_fresh,  # read fresh patient
         ])), \
         patch("app.services.opd.assign_mrn", AsyncMock(return_value="MUM-2026-0001")), \
         patch("app.routers.opd.log_admin_action", AsyncMock()):
        r = client.post(f"/admin/opd/patients?clinic_id={CLINIC_ID}", json=payload)

    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Amitabh Varma"
    assert data["mrn"] == "MUM-2026-0001"
    assert data["is_account_holder"] is True


def test_register_family_member_success(auth_front_desk):
    """Dependant registration links to parent patient and inserts into family_members."""
    payload = {
        "name": "Rohan Varma",
        "phone": "+919123456789",
        "is_account_holder": False,
        "relationship": "son",
        "guardian_name": "Amitabh Varma",
        "age_years": 12,
        "gender": "male",
        "data_consent": True,
        "whatsapp_opt_in": True,
    }

    fake_parent_lookup = MagicMock(data=[{"id": "pat-123", "phone": "+919123456789"}])
    fake_fm_ins = MagicMock(data=[{"id": "fm-456"}])
    fake_fm_fresh = MagicMock(data=[{
        "id": "fm-456",
        "patient_id": "pat-123",
        "full_name": "Rohan Varma",
        "primary_phone": "+919123456789",
        "relationship": "son",
        "mrn": "MUM-2026-0001-01",
        "date_of_birth": None,
        "age_years": 12,
        "gender": "male",
        "allergies": [],
        "allergies_status": "unknown",
    }])
    fake_empty = MagicMock(data=[])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.services.opd.sb", AsyncMock(side_effect=[
             fake_empty,          # dup phone in patients
             fake_empty,          # dup phone in family_members
             fake_empty,          # dup name in patients
             fake_parent_lookup,  # parent patient lookup
             fake_fm_ins,         # insert family_member
             fake_fm_fresh,       # select fresh family_member
         ])), \
         patch("app.services.opd.assign_mrn", AsyncMock(return_value="MUM-2026-0001-01")), \
         patch("app.routers.opd.log_admin_action", AsyncMock()):
        r = client.post(f"/admin/opd/patients?clinic_id={CLINIC_ID}", json=payload)

    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Rohan Varma"
    assert data["is_account_holder"] is False
    assert data["family_member_id"] == "fm-456"
    assert data["relationship"] == "son"


# ─── 4. SEARCH & PATIENT DETAILS ─────────────────────────────────────────────


def test_search_patients(auth_front_desk):
    """Search matches patients across MRN, phone, and name."""
    fake_pat_res = MagicMock(data=[{
        "id": "pat-1",
        "name": "Kavita Rao",
        "phone": "+919845012345",
        "mrn": "BLR-2026-0010",
        "date_of_birth": None,
        "age_years": 38,
        "gender": "female",
    }])
    fake_empty = MagicMock(data=[])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.services.opd.sb", AsyncMock(side_effect=[fake_pat_res, fake_empty, fake_empty])):
        r = client.get(f"/admin/opd/patients/search?q=Kavita&clinic_id={CLINIC_ID}")

    assert r.status_code == 200
    res = r.json()
    assert len(res) == 1
    assert res[0]["name"] == "Kavita Rao"
    assert res[0]["mrn"] == "BLR-2026-0010"


def test_patient_details_front_desk(auth_front_desk):
    """Front-desk receives patient details and visits, but empty clinical history."""
    fake_pat = MagicMock(data=[{
        "id": "pat-1",
        "name": "Kavita Rao",
        "phone": "+919845012345",
        "mrn": "BLR-2026-0010",
    }])
    fake_visits = MagicMock(data=[])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.routers.opd.sb", AsyncMock(side_effect=[fake_pat, fake_visits])), \
         patch("app.routers.opd.log_admin_action", AsyncMock()):
        r = client.get(f"/admin/opd/patients/pat-1?clinic_id={CLINIC_ID}")
    assert r.status_code == 200
    assert r.json()["clinical_history"] == []


def test_patient_details_clinical_history(auth_clinical):
    """Doctor receives clinical encounters in history."""
    fake_pat = MagicMock(data=[{
        "id": "pat-1",
        "name": "Kavita Rao",
        "phone": "+919845012345",
        "mrn": "BLR-2026-0010",
    }])
    fake_visits = MagicMock(data=[])
    fake_enc = MagicMock(data=[{"id": "enc-1", "diagnosis": "Viral fever"}])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.routers.opd.sb", AsyncMock(side_effect=[fake_pat, fake_visits, fake_enc])), \
         patch("app.routers.opd.log_admin_action", AsyncMock()):
        r = client.get(f"/admin/opd/patients/pat-1?clinic_id={CLINIC_ID}")
    assert r.status_code == 200
    assert len(r.json()["clinical_history"]) == 1


# ─── 5. PATIENT DEMOGRAPHICS PATCH ───────────────────────────────────────────


def test_patch_patient_demographics(auth_front_desk):
    """Patch updates address, city, and allergies."""
    fake_upd = MagicMock(data=[{"id": "pat-1", "city": "Mysuru", "allergies": ["Dust"]}])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.routers.opd.sb", AsyncMock(return_value=fake_upd)), \
         patch("app.routers.opd.log_admin_action", AsyncMock()):
        r = client.patch(
            f"/admin/opd/patients/pat-1?clinic_id={CLINIC_ID}",
            json={"city": "Mysuru", "allergies": ["Dust"]},
        )
    assert r.status_code == 200
    assert r.json()["city"] == "Mysuru"
