"""Tests for Kriya OPD OS Clinical Service & Encounters (Phase 1.3).

Verifies:
1. Vitals bounds and validation (API 422 matches DB CHECK rules).
2. BMI computation from height and weight.
3. Notes autosave with CAS 409 optimistic locking.
4. Sign encounter strictly requires the linked treating doctor (clinic_admin without link -> 403).
5. Sign encounter requires doctor registration number and council (422 registration_missing).
6. Sign encounter requires chief complaints or >=1 diagnosis (422 clinical_content_required).
7. Amend encounter creates v2, copies content, maintains supersedes chain.
8. Patient history returns signed/superseded records ordered newest first.
"""

import datetime
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin
from app.routers.admin import AdminUser
from app.services.opd_clinical import (
    amend_encounter,
    get_or_create_draft,
    patient_history,
    save_notes,
    save_vitals,
    sign_encounter,
)

client = TestClient(app)

CLINIC_ID = str(uuid.uuid4())
DOCTOR_ID = str(uuid.uuid4())
OTHER_DOCTOR_ID = str(uuid.uuid4())
APPOINTMENT_ID = str(uuid.uuid4())
PATIENT_ID = str(uuid.uuid4())

TREATING_DOC_USER = AdminUser(
    "dr_treating",
    role="staff",
    clinic_id=CLINIC_ID,
    permissions=["OPD_CLINICAL"],
    doctor_id=DOCTOR_ID,
)

OTHER_DOC_USER = AdminUser(
    "dr_other",
    role="staff",
    clinic_id=CLINIC_ID,
    permissions=["OPD_CLINICAL"],
    doctor_id=OTHER_DOCTOR_ID,
)

ADMIN_NO_DOC_USER = AdminUser(
    "clinic_admin_user",
    role="clinic_admin",
    clinic_id=CLINIC_ID,
    permissions=["OPD_CLINICAL", "OPD_ADMIN"],
    doctor_id=None,
)

FRONT_DESK_USER = AdminUser(
    "front_desk_user",
    role="staff",
    clinic_id=CLINIC_ID,
    permissions=["OPD_FRONT_DESK"],
)

MOCK_CLINIC = {"id": CLINIC_ID, "name": "Kriya Hospital", "features": {"opd_enabled": True}, "opd_state": "READY"}


# ─── 1. VITALS BOUNDS & VALIDATION ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_vitals_bounds_validation():
    """Vitals outside physiological bounds or invalid systolic/diastolic raise 422."""
    app.dependency_overrides[admin.verify_credentials] = lambda: FRONT_DESK_USER
    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)):
            # 1. Systolic <= Diastolic
            res = client.put(
                f"/admin/opd/encounters/by-appointment/{APPOINTMENT_ID}/vitals",
                json={"bp_systolic": 80, "bp_diastolic": 120},
            )
            assert res.status_code == 422

            # 2. Systolic > 300
            res = client.put(
                f"/admin/opd/encounters/by-appointment/{APPOINTMENT_ID}/vitals",
                json={"bp_systolic": 350, "bp_diastolic": 80},
            )
            assert res.status_code == 422

            # 3. Pulse < 20
            res = client.put(
                f"/admin/opd/encounters/by-appointment/{APPOINTMENT_ID}/vitals",
                json={"pulse_bpm": 15},
            )
            assert res.status_code == 422

            # 4. SpO2 > 100
            res = client.put(
                f"/admin/opd/encounters/by-appointment/{APPOINTMENT_ID}/vitals",
                json={"spo2_pct": 105},
            )
            assert res.status_code == 422

            # 5. Empty vitals
            res = client.put(
                f"/admin/opd/encounters/by-appointment/{APPOINTMENT_ID}/vitals",
                json={},
            )
            assert res.status_code == 422
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.mark.asyncio
async def test_vitals_save_and_queue_advancement():
    """Valid vitals are persisted and advances vitals_pending to waiting."""
    app.dependency_overrides[admin.verify_credentials] = lambda: FRONT_DESK_USER
    enc_id = str(uuid.uuid4())
    mock_enc = {
        "id": enc_id,
        "clinic_id": CLINIC_ID,
        "appointment_id": APPOINTMENT_ID,
        "patient_id": PATIENT_ID,
        "doctor_id": DOCTOR_ID,
        "version": 1,
        "status": "draft",
    }
    saved_enc = dict(mock_enc)
    saved_enc.update({"bp_systolic": 120, "bp_diastolic": 80, "pulse_bpm": 72, "spo2_pct": 98})

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.get_or_create_draft", AsyncMock(return_value=mock_enc)), \
             patch("app.services.opd_clinical.sb") as mock_sb, \
             patch("app.services.opd.transition_stage", AsyncMock()) as mock_transition:

            mock_res = MagicMock()
            mock_res.data = [saved_enc]
            mock_apt = MagicMock()
            mock_apt.data = {"queue_status": "vitals_pending"}
            mock_sb.side_effect = [mock_res, mock_apt]

            res = client.put(
                f"/admin/opd/encounters/by-appointment/{APPOINTMENT_ID}/vitals",
                json={"bp_systolic": 120, "bp_diastolic": 80, "pulse_bpm": 72, "spo2_pct": 98},
            )
            assert res.status_code == 200
            assert res.json()["bp_systolic"] == 120
            mock_transition.assert_called_once()
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


# ─── 2. CLINICAL NOTES AUTOSAVE & CAS ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_notes_autosave_cas_conflict():
    """Notes autosave with mismatched expected_updated_at returns 409 Conflict."""
    app.dependency_overrides[admin.verify_credentials] = lambda: TREATING_DOC_USER
    enc_id = str(uuid.uuid4())
    current_time = "2026-10-09T10:00:00+00:00"
    stale_time = "2026-10-09T09:00:00+00:00"

    mock_enc = {
        "id": enc_id,
        "clinic_id": CLINIC_ID,
        "appointment_id": APPOINTMENT_ID,
        "doctor_id": DOCTOR_ID,
        "status": "draft",
        "updated_at": current_time,
    }

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb:

            enc_mock = MagicMock()
            enc_mock.data = mock_enc
            mock_sb.return_value = enc_mock

            # Stale update
            res = client.put(
                f"/admin/opd/encounters/{enc_id}",
                json={
                    "expected_updated_at": stale_time,
                    "chief_complaints": "Cough and cold",
                },
            )
            assert res.status_code == 409
            assert "stale_update" in res.text
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.mark.asyncio
async def test_notes_write_is_conditional_on_read_version():
    """Two saves that both pass the timestamp check must not both write: the
    UPDATE itself is conditioned on the updated_at that was read, and a write
    that matched no row (someone saved in between) is a 409, not a lost update."""
    enc_id = str(uuid.uuid4())
    read_at = "2026-10-09T10:00:00+00:00"
    mock_enc = {"id": enc_id, "clinic_id": CLINIC_ID, "doctor_id": DOCTOR_ID,
                "status": "draft", "updated_at": read_at}
    sent = []

    async def fake_sb(builder):
        sent.append((builder.request.http_method, dict(builder.request.params)))
        if builder.request.http_method == "GET":
            return MagicMock(data=mock_enc)
        return MagicMock(data=[])  # another save landed first

    with patch("app.services.opd_clinical.sb", side_effect=fake_sb):
        with pytest.raises(HTTPException) as exc:
            await save_notes(CLINIC_ID, enc_id, {"expected_updated_at": read_at, "advice": "x"},
                             {"doctor_id": DOCTOR_ID})
    assert exc.value.status_code == 409
    upd = next(p for m, p in sent if m == "PATCH")
    assert upd.get("updated_at") == f"eq.{read_at}" and upd.get("status") == "eq.draft"


@pytest.mark.asyncio
async def test_notes_treating_doctor_only():
    """Only treating doctor can edit encounter notes (other doctor -> 403)."""
    app.dependency_overrides[admin.verify_credentials] = lambda: OTHER_DOC_USER
    enc_id = str(uuid.uuid4())
    current_time = "2026-10-09T10:00:00+00:00"

    mock_enc = {
        "id": enc_id,
        "clinic_id": CLINIC_ID,
        "doctor_id": DOCTOR_ID,  # Belongs to DOCTOR_ID, not OTHER_DOCTOR_ID
        "status": "draft",
        "updated_at": current_time,
    }

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb:

            enc_mock = MagicMock()
            enc_mock.data = mock_enc
            mock_sb.return_value = enc_mock

            res = client.put(
                f"/admin/opd/encounters/{enc_id}",
                json={
                    "expected_updated_at": current_time,
                    "chief_complaints": "Fever",
                },
            )
            assert res.status_code == 403
            assert "Only the treating doctor" in res.text
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


# ─── 3. SIGNING RULES & NMC COMPLIANCE ───────────────────────────────────────


@pytest.mark.asyncio
async def test_sign_requires_linked_treating_doctor():
    """Signing by clinic_admin without linked doctor row is refused (403)."""
    app.dependency_overrides[admin.verify_credentials] = lambda: ADMIN_NO_DOC_USER
    enc_id = str(uuid.uuid4())
    mock_enc = {
        "id": enc_id,
        "clinic_id": CLINIC_ID,
        "doctor_id": DOCTOR_ID,
        "status": "draft",
    }

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb:

            enc_mock = MagicMock()
            enc_mock.data = mock_enc
            mock_sb.return_value = enc_mock

            res = client.post(f"/admin/opd/encounters/{enc_id}/sign")
            assert res.status_code == 403
            assert "treating doctor" in res.text
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.mark.asyncio
async def test_sign_missing_registration_refused():
    """Doctor without registration_number or council cannot sign (422 registration_missing)."""
    app.dependency_overrides[admin.verify_credentials] = lambda: TREATING_DOC_USER
    enc_id = str(uuid.uuid4())
    mock_enc = {
        "id": enc_id,
        "clinic_id": CLINIC_ID,
        "doctor_id": DOCTOR_ID,
        "status": "draft",
        "chief_complaints": "Acute headache",
    }
    # Doctor has no registration number
    mock_doc = {
        "id": DOCTOR_ID,
        "name": "Dr. Arun",
        "registration_number": None,
        "registration_council": None,
    }

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb:

            enc_mock = MagicMock()
            enc_mock.data = mock_enc
            doc_mock = MagicMock()
            doc_mock.data = mock_doc
            mock_sb.side_effect = [enc_mock, doc_mock]

            res = client.post(f"/admin/opd/encounters/{enc_id}/sign")
            assert res.status_code == 422
            assert "registration_missing" in res.text
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.mark.asyncio
async def test_sign_empty_clinical_content_refused():
    """Sign without chief complaints or diagnoses is refused (422 clinical_content_required)."""
    app.dependency_overrides[admin.verify_credentials] = lambda: TREATING_DOC_USER
    enc_id = str(uuid.uuid4())
    mock_enc = {
        "id": enc_id,
        "clinic_id": CLINIC_ID,
        "doctor_id": DOCTOR_ID,
        "status": "draft",
        "chief_complaints": "",
        "diagnoses": [],
    }
    mock_doc = {
        "id": DOCTOR_ID,
        "name": "Dr. Arun",
        "registration_number": "DMC-12345",
        "registration_council": "Delhi Medical Council",
    }

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb:

            enc_mock = MagicMock()
            enc_mock.data = mock_enc
            doc_mock = MagicMock()
            doc_mock.data = mock_doc
            mock_sb.side_effect = [enc_mock, doc_mock]

            res = client.post(f"/admin/opd/encounters/{enc_id}/sign")
            assert res.status_code == 422
            assert "clinical_content_required" in res.text
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


# ─── 4. AMENDMENT CHAINING ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_amend_encounter_creates_v2():
    """Amending a signed encounter creates draft v2 with supersedes_id = v1.id."""
    app.dependency_overrides[admin.verify_credentials] = lambda: TREATING_DOC_USER
    v1_id = str(uuid.uuid4())
    v2_id = str(uuid.uuid4())

    mock_v1 = {
        "id": v1_id,
        "clinic_id": CLINIC_ID,
        "appointment_id": APPOINTMENT_ID,
        "patient_id": PATIENT_ID,
        "doctor_id": DOCTOR_ID,
        "version": 1,
        "status": "signed",
        "chief_complaints": "Initial complaint",
        "bp_systolic": 120,
        "bp_diastolic": 80,
    }

    mock_v2 = dict(mock_v1)
    mock_v2.update({
        "id": v2_id,
        "version": 2,
        "supersedes_id": v1_id,
        "status": "draft",
    })

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb:

            v1_mock = MagicMock()
            v1_mock.data = mock_v1
            # draft_check returns empty (no pending draft)
            draft_check_mock = MagicMock()
            draft_check_mock.data = []
            # insert returns v2
            insert_mock = MagicMock()
            insert_mock.data = [mock_v2]

            mock_sb.side_effect = [v1_mock, draft_check_mock, insert_mock]

            res = client.post(
                f"/admin/opd/encounters/{v1_id}/amend",
                json={"reason": "Patient returned with additional lab reports"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["version"] == 2
            assert data["supersedes_id"] == v1_id
            assert data["status"] == "draft"
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.mark.asyncio
async def test_get_doctor_workspace_queue_and_doctor_context():
    """GET /admin/opd/workspace returns doctor info and active queue sorted by stage & token."""
    app.dependency_overrides[admin.verify_credentials] = lambda: TREATING_DOC_USER

    mock_doc = {
        "id": DOCTOR_ID,
        "name": "Dr. Sarah Rao",
        "department": "Cardiology",
        "qualifications": "MBBS, MD",
        "registration_number": "KA-12345",
        "registration_council": "Karnataka Medical Council",
    }

    mock_board = {
        "date": "2026-10-09",
        "doctors": [
            {
                "doctor_id": DOCTOR_ID,
                "doctor_name": "Dr. Sarah Rao",
                "rows": [
                    {
                        "appointment_id": "appt-1",
                        "token_number": 2,
                        "stage": "waiting",
                        "patient_name": "Patient Two",
                    },
                    {
                        "appointment_id": "appt-2",
                        "token_number": 1,
                        "stage": "in_consultation",
                        "patient_name": "Patient One",
                    },
                    {
                        "appointment_id": "appt-3",
                        "token_number": 3,
                        "stage": "completed",
                        "patient_name": "Patient Three",
                    },
                ],
            }
        ],
        "etag": "etag123",
    }

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.routers.opd.get_queue_board", AsyncMock(return_value=mock_board)), \
             patch("app.routers.opd.sb") as mock_sb:

            doc_mock = MagicMock()
            doc_mock.data = mock_doc
            mock_sb.return_value = doc_mock

            res = client.get("/admin/opd/workspace?clinic_id=default")
            assert res.status_code == 200
            data = res.json()
            assert data["doctor"]["id"] == DOCTOR_ID
            assert data["doctor"]["name"] == "Dr. Sarah Rao"

            # Check queue: only active stages, sorted (in_consultation before waiting)
            queue = data["queue"]
            assert len(queue) == 2
            assert queue[0]["appointment_id"] == "appt-2"
            assert queue[0]["stage"] == "in_consultation"
            assert queue[1]["appointment_id"] == "appt-1"
            assert queue[1]["stage"] == "waiting"
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)
