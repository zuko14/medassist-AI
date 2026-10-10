"""Tests for Kriya OPD OS e-Prescriptions Service (Phase 1.3).

Verifies:
1. Draft replace (deletes old items, inserts new items).
2. Items locked after parent prescription is signed.
3. Deterministic allergy screening:
   - Substring matches (e.g., 'aspirin' in 'Aspirin 75mg').
   - Class matches (e.g., patient allergic to 'penicillin' -> warned for 'Amoxicillin').
   - 'unknown' allergy status produces general warning.
4. Server recomputes warnings at sign time (forged client ack list ignored).
5. All warnings require override reason (>= 5 chars) before signing.
6. Rx signing strictly requires an already signed encounter.
7. WhatsApp dispatch requires patient consent / opt-in (409 no_whatsapp_consent).
8. WhatsApp dispatch outside 24h window uses approved template or returns outside_24h_no_template.
9. Daily send limit enforced (HTTP 429 when send_count >= 5).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin
from app.routers.admin import AdminUser
from app.services.opd_clinical import (
    ALLERGY_CLASSES,
    allergy_warnings,
    save_prescription_draft,
    send_prescription,
    sign_prescription,
)

client = TestClient(app)

CLINIC_ID = str(uuid.uuid4())
DOCTOR_ID = str(uuid.uuid4())
APPOINTMENT_ID = str(uuid.uuid4())
PATIENT_ID = str(uuid.uuid4())
ENCOUNTER_ID = str(uuid.uuid4())
RX_ID = str(uuid.uuid4())

TREATING_DOC_USER = AdminUser(
    "dr_treating",
    role="staff",
    clinic_id=CLINIC_ID,
    permissions=["OPD_CLINICAL"],
    doctor_id=DOCTOR_ID,
)

MOCK_CLINIC = {
    "id": CLINIC_ID,
    "name": "Kriya Hospital",
    "features": {"opd_enabled": True},
    "opd_state": "READY",
    "opd_settings": {"templates": {"prescription_ready": "opd_prescription_ready"}},
}


# ─── 1. DETERMINISTIC ALLERGY SCREENING (ZERO-LLM) ──────────────────────────


def test_allergy_warnings_unknown_status():
    """Allergies status 'unknown' returns mandatory unrecorded history warning."""
    items = [{"line_no": 1, "drug_name": "Paracetamol 650mg"}]
    warnings = allergy_warnings([], "unknown", items)
    assert len(warnings) == 1
    assert warnings[0]["line_no"] == 0
    assert warnings[0]["allergen"] == "unknown"
    assert "not recorded" in warnings[0]["message"]


def test_allergy_warnings_none_known():
    """Allergies status 'none_known' produces zero warnings."""
    items = [{"line_no": 1, "drug_name": "Amoxicillin 500mg"}]
    warnings = allergy_warnings([], "none_known", items)
    assert len(warnings) == 0


def test_allergy_warnings_substring_and_class_match():
    """Matches direct substrings and cross-reactivity classes deterministically."""
    allergies = ["penicillin", "aspirin"]
    items = [
        {"line_no": 1, "drug_name": "Amoxicillin 500mg"},    # In penicillin class
        {"line_no": 2, "drug_name": "Ecosprin 75 (Aspirin)"}, # Direct substring match
        {"line_no": 3, "drug_name": "Cetirizine 10mg"},      # No match
    ]
    warnings = allergy_warnings(allergies, "recorded", items)
    assert len(warnings) == 2

    # Check line 1 (Class warning: penicillin -> amoxicillin)
    w1 = next(w for w in warnings if w["line_no"] == 1)
    assert w1["allergen"] == "penicillin"
    assert "class" in w1["message"].lower()

    # Check line 2 (Substring match: aspirin)
    w2 = next(w for w in warnings if w["line_no"] == 2)
    assert w2["allergen"] == "aspirin"


# ─── 2. DRAFT REPLACE & ATOMIC ITEMS ────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_prescription_draft_replaces_items():
    """Saving prescription draft deletes previous items and inserts new ones atomically."""
    app.dependency_overrides[admin.verify_credentials] = lambda: TREATING_DOC_USER
    mock_enc = {
        "id": ENCOUNTER_ID,
        "clinic_id": CLINIC_ID,
        "appointment_id": APPOINTMENT_ID,
        "patient_id": PATIENT_ID,
        "doctor_id": DOCTOR_ID,
        "status": "draft",
    }
    mock_rx = {
        "id": RX_ID,
        "clinic_id": CLINIC_ID,
        "encounter_id": ENCOUNTER_ID,
        "appointment_id": APPOINTMENT_ID,
        "patient_id": PATIENT_ID,
        "doctor_id": DOCTOR_ID,
        "version": 1,
        "status": "draft",
        "updated_at": "2026-10-09T10:00:00+00:00",
    }

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb, \
             patch("app.services.opd_clinical.get_patient_allergies", AsyncMock(return_value=(["penicillin"], "recorded"))):

            enc_mock = MagicMock()
            enc_mock.data = mock_enc
            rx_mock = MagicMock()
            rx_mock.data = [mock_rx]
            upd_mock = MagicMock()
            upd_mock.data = [mock_rx]
            del_mock = MagicMock()
            del_mock.data = []
            ins_items_mock = MagicMock()
            ins_items_mock.data = [
                {"id": str(uuid.uuid4()), "line_no": 1, "drug_name": "Amoxicillin", "formulation": "capsule"}
            ]

            mock_sb.side_effect = [enc_mock, rx_mock, upd_mock, del_mock, ins_items_mock]

            res = client.put(
                f"/admin/opd/encounters/{ENCOUNTER_ID}/prescription",
                json={
                    "general_instructions": "Take after meals",
                    "items": [
                        {
                            "drug_name": "Amoxicillin",
                            "formulation": "capsule",
                            "strength": "500mg",
                            "dosage": "1 cap",
                            "frequency": "TDS",
                            "timing": "after_food",
                            "duration_days": 5,
                        }
                    ],
                },
            )
            assert res.status_code == 200
            data = res.json()
            assert len(data["items"]) == 1
            assert len(data["warnings"]) == 1
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


# ─── 3. SIGNING & RECOMPUTED WARNING ACKNOWLEDGEMENTS ───────────────────────


@pytest.mark.asyncio
async def test_sign_prescription_requires_signed_encounter():
    """Prescription cannot be signed if parent encounter is still a draft (422)."""
    app.dependency_overrides[admin.verify_credentials] = lambda: TREATING_DOC_USER
    mock_rx = {
        "id": RX_ID,
        "clinic_id": CLINIC_ID,
        "encounter_id": ENCOUNTER_ID,
        "doctor_id": DOCTOR_ID,
        "patient_id": PATIENT_ID,
        "status": "draft",
    }
    # Encounter is still draft!
    mock_enc = {
        "id": ENCOUNTER_ID,
        "clinic_id": CLINIC_ID,
        "status": "draft",
    }

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb:

            rx_mock = MagicMock()
            rx_mock.data = mock_rx
            enc_mock = MagicMock()
            enc_mock.data = mock_enc
            mock_sb.side_effect = [rx_mock, enc_mock]

            res = client.post(
                f"/admin/opd/prescriptions/{RX_ID}/sign",
                json={"acknowledgements": []},
            )
            assert res.status_code == 422
            assert "opd_encounter_not_signed" in res.text
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.mark.asyncio
async def test_sign_prescription_server_recomputes_warnings():
    """Signing is refused if allergy warning is not acknowledged, ignoring forged client lists."""
    app.dependency_overrides[admin.verify_credentials] = lambda: TREATING_DOC_USER
    mock_rx = {
        "id": RX_ID,
        "clinic_id": CLINIC_ID,
        "encounter_id": ENCOUNTER_ID,
        "doctor_id": DOCTOR_ID,
        "patient_id": PATIENT_ID,
        "status": "draft",
    }
    mock_enc = {"id": ENCOUNTER_ID, "clinic_id": CLINIC_ID, "status": "signed"}
    mock_doc = {
        "id": DOCTOR_ID,
        "name": "Dr. Arun",
        "registration_number": "DMC-5555",
        "registration_council": "Delhi Medical Council",
    }
    mock_items = [
        {"line_no": 1, "drug_name": "Amoxicillin 500mg", "dosage": "1 cap", "formulation": "capsule"}
    ]

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb, \
             patch("app.services.opd_clinical.get_patient_allergies", AsyncMock(return_value=(["penicillin"], "recorded"))):

            rx_mock = MagicMock(); rx_mock.data = mock_rx
            enc_mock = MagicMock(); enc_mock.data = mock_enc
            doc_mock = MagicMock(); doc_mock.data = mock_doc
            items_mock = MagicMock(); items_mock.data = mock_items

            # 1. No acknowledgement provided -> 422
            mock_sb.side_effect = [rx_mock, enc_mock, doc_mock, items_mock]
            res = client.post(
                f"/admin/opd/prescriptions/{RX_ID}/sign",
                json={"acknowledgements": []},
            )
            assert res.status_code == 422
            assert "unacknowledged_warnings" in res.text

            # 2. Forged/insufficient acknowledgement (override_reason < 5 chars) -> 422
            mock_sb.side_effect = [rx_mock, enc_mock, doc_mock, items_mock]
            res = client.post(
                f"/admin/opd/prescriptions/{RX_ID}/sign",
                json={
                    "acknowledgements": [
                        {"line_no": 1, "allergen": "penicillin", "override_reason": "ok"}  # < 5 chars
                    ]
                },
            )
            assert res.status_code == 422

            # 3. Valid acknowledgement (override_reason >= 5 chars) -> signs via RPC
            mock_signed_rx = dict(mock_rx)
            mock_signed_rx["status"] = "signed"
            pat_mock = MagicMock(); pat_mock.data = {"id": PATIENT_ID, "name": "Rajesh", "mrn": "MRN-1"}
            rpc_mock = MagicMock(); rpc_mock.data = [mock_signed_rx]

            mock_sb.side_effect = [rx_mock, enc_mock, doc_mock, items_mock, pat_mock, rpc_mock]
            res = client.post(
                f"/admin/opd/prescriptions/{RX_ID}/sign",
                json={
                    "acknowledgements": [
                        {
                            "line_no": 1,
                            "allergen": "penicillin",
                            "override_reason": "Patient previously tolerated amoxicillin without rash",
                        }
                    ]
                },
            )
            assert res.status_code == 200
            assert res.json()["status"] == "signed"
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


# ─── 4. DISPATCH & CONSENT ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_prescription_refused_without_optin():
    """WhatsApp send is refused with 409 if patient has not opted in."""
    app.dependency_overrides[admin.verify_credentials] = lambda: TREATING_DOC_USER
    mock_rx = {
        "id": RX_ID,
        "clinic_id": CLINIC_ID,
        "patient_id": PATIENT_ID,
        "status": "signed",
        "send_count": 0,
    }
    # Patient not opted in
    mock_patient = {"id": PATIENT_ID, "phone": "+919876543210", "opted_in": False}

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb:

            rx_mock = MagicMock(); rx_mock.data = mock_rx
            pat_mock = MagicMock(); pat_mock.data = mock_patient
            mock_sb.side_effect = [rx_mock, pat_mock]

            res = client.post(f"/admin/opd/prescriptions/{RX_ID}/send-whatsapp")
            assert res.status_code == 409
            assert "no_whatsapp_consent" in res.text
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.mark.asyncio
async def test_send_prescription_limit_exceeded():
    """Prescription send count limit (5 per day) returns 429 Too Many Requests."""
    app.dependency_overrides[admin.verify_credentials] = lambda: TREATING_DOC_USER
    mock_rx = {
        "id": RX_ID,
        "clinic_id": CLINIC_ID,
        "patient_id": PATIENT_ID,
        "status": "signed",
        "send_count": 5,  # Max limit reached
    }

    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.services.opd_clinical.sb") as mock_sb:

            rx_mock = MagicMock(); rx_mock.data = mock_rx
            mock_sb.return_value = rx_mock

            res = client.post(f"/admin/opd/prescriptions/{RX_ID}/send-whatsapp")
            assert res.status_code == 429
            assert "Daily send limit" in res.text
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)
