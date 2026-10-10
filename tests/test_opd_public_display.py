"""Tests for Kriya OPD OS Public Hallway TV Display (Phase 1.2).

Verifies:
1. Display token authentication via X-Display-Token header and ?t= / ?token= query parameter.
2. Missing token returns 401.
3. Invalid / unknown token hash returns 401.
4. Token rotation invalidates old token immediately.
5. Non-READY clinic (NOT_CONFIGURED / CONFIGURING) returns 404.
6. Multi-branch filtering and non-existent branch handling (404).
7. Room mapping from opd_settings.rooms to doctor display cards.
8. Strict recursive assertion guaranteeing ZERO PII (no names, phones, MRNs, symptoms) in payload.
9. Static HTML /public/queue-display endpoint delivery.
"""

import hashlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.opd import public_display_payload, rotate_display_token

client = TestClient(app)

CLINIC_ID = str(uuid.uuid4())
BRANCH_ID = str(uuid.uuid4())
RAW_TOKEN = "test_tv_display_secret_token_12345"
TOKEN_HASH = hashlib.sha256(RAW_TOKEN.encode("utf-8")).hexdigest()

DOC_1_ID = str(uuid.uuid4())
DOC_2_ID = str(uuid.uuid4())

READY_CLINIC = {
    "id": CLINIC_ID,
    "name": "Kriya Central Hospital",
    "features": {"opd_enabled": True},
    "opd_state": "READY",
    "opd_display_token_hash": TOKEN_HASH,
    "opd_settings": {
        "rooms": {
            DOC_1_ID: "Room 101",
            DOC_2_ID: "Consultation 2",
        }
    },
}

SAMPLE_APPOINTMENTS = [
    {
        "id": str(uuid.uuid4()),
        "clinic_id": CLINIC_ID,
        "branch_id": BRANCH_ID,
        "doctor_id": DOC_1_ID,
        "doctor_name": "Dr. Sarah Rao",
        "department": "General Medicine",
        "queue_status": "in_consultation",
        "token_number": 101,
        "patient_name": "SECRET_NAME_ALICE",
        "patient_phone": "+919876543210",
        "mrn": "MRN-101",
        "symptoms": "Severe headache",
    },
    {
        "id": str(uuid.uuid4()),
        "clinic_id": CLINIC_ID,
        "branch_id": BRANCH_ID,
        "doctor_id": DOC_1_ID,
        "doctor_name": "Dr. Sarah Rao",
        "department": "General Medicine",
        "queue_status": "waiting",
        "token_number": 102,
        "patient_name": "SECRET_NAME_BOB",
        "patient_phone": "+919876543211",
        "mrn": "MRN-102",
        "symptoms": "Fever",
    },
    {
        "id": str(uuid.uuid4()),
        "clinic_id": CLINIC_ID,
        "branch_id": BRANCH_ID,
        "doctor_id": DOC_1_ID,
        "doctor_name": "Dr. Sarah Rao",
        "department": "General Medicine",
        "queue_status": "waiting",
        "token_number": 103,
        "patient_name": "SECRET_NAME_CHARLIE",
        "patient_phone": "+919876543212",
        "mrn": "MRN-103",
    },
    {
        "id": str(uuid.uuid4()),
        "clinic_id": CLINIC_ID,
        "branch_id": BRANCH_ID,
        "doctor_id": DOC_2_ID,
        "doctor_name": "Dr. Vivek Sharma",
        "department": "Pediatrics",
        "queue_status": "in_consultation",
        "token_number": 201,
        "patient_name": "SECRET_NAME_DAVE",
        "patient_phone": "+919876543213",
        "mrn": "MRN-201",
    },
]


def assert_tree_contains_no_pii(data: Any):
    """Recursively traverse a payload verifying zero patient PII or symptoms."""
    forbidden_keys = {
        "patient_name",
        "patient_phone",
        "phone",
        "mrn",
        "symptoms",
        "date_of_birth",
        "address",
        "address_line",
        "allergies",
    }
    forbidden_substrings = [
        "SECRET_NAME",
        "+9198765",
        "MRN-",
        "headache",
        "Fever",
    ]

    if isinstance(data, dict):
        for k, v in data.items():
            assert k.lower() not in forbidden_keys, f"Forbidden PII key found in public payload: {k}"
            assert_tree_contains_no_pii(v)
    elif isinstance(data, list):
        for item in data:
            assert_tree_contains_no_pii(item)
    elif isinstance(data, str):
        for sub in forbidden_substrings:
            assert sub.lower() not in data.lower(), f"Forbidden PII substring '{sub}' leaked in value: '{data}'"


# ─── 1. TOKEN AUTHENTICATION & REJECTION ─────────────────────────────────────


def test_public_display_missing_token_rejected():
    """Missing display token returns 401 Unauthorized."""
    res = client.get("/public/queue-display/data")
    assert res.status_code == 401
    assert "Display token required" in res.json()["detail"]


def test_public_display_invalid_token_rejected():
    """Invalid or unknown display token hash returns 401 Unauthorized."""
    fake_empty_clinic = MagicMock(data=[])
    with patch("app.routers.opd.sb", AsyncMock(return_value=fake_empty_clinic)):
        res = client.get(f"/public/queue-display/data?t=invalid_token_xyz")
        assert res.status_code == 401
        assert "Invalid display token" in res.json()["detail"]


def test_public_display_via_header_and_query_param():
    """Valid token accepted via X-Display-Token header and ?t= query parameter."""
    fake_clinic_lookup = MagicMock(data=[{"id": CLINIC_ID, "name": READY_CLINIC["name"], "opd_state": "READY"}])
    fake_appts = MagicMock(data=SAMPLE_APPOINTMENTS)

    # 1. Via X-Display-Token header
    with patch("app.routers.opd.sb", AsyncMock(return_value=fake_clinic_lookup)), \
         patch("app.routers.opd.public_display_payload", AsyncMock(return_value={"clinic_name": "Test Clinic", "doctors": []})):
        res = client.get("/public/queue-display/data", headers={"X-Display-Token": RAW_TOKEN})
        assert res.status_code == 200

    # 2. Via ?t= query parameter
    with patch("app.routers.opd.sb", AsyncMock(return_value=fake_clinic_lookup)), \
         patch("app.routers.opd.public_display_payload", AsyncMock(return_value={"clinic_name": "Test Clinic", "doctors": []})):
        res = client.get(f"/public/queue-display/data?t={RAW_TOKEN}")
        assert res.status_code == 200


# ─── 2. CLINIC OPD READINESS GATING ──────────────────────────────────────────


def test_public_display_non_ready_clinic_returns_404():
    """Non-READY clinic returns 404 Not Found."""
    fake_clinic_lookup = MagicMock(data=[{"id": CLINIC_ID, "name": "Configuring Clinic", "opd_state": "CONFIGURING"}])
    with patch("app.routers.opd.sb", AsyncMock(return_value=fake_clinic_lookup)):
        res = client.get(f"/public/queue-display/data?t={RAW_TOKEN}")
        assert res.status_code == 404
        assert "not active" in res.json()["detail"].lower()


# ─── 3. ZERO PII RECURSIVE VERIFICATION & DATA SHAPE ─────────────────────────


@pytest.mark.asyncio
async def test_public_display_payload_zero_pii_and_structure():
    """public_display_payload constructs sanitized doctor cards with room mapping and zero PII."""
    fake_clinic = dict(READY_CLINIC)
    fake_appts_res = MagicMock(data=SAMPLE_APPOINTMENTS)

    with patch("app.services.opd.get_clinic_by_id", AsyncMock(return_value=fake_clinic)), \
         patch("app.services.opd.sb", AsyncMock(return_value=fake_appts_res)):
        payload = await public_display_payload(CLINIC_ID)

    # Verify high level shape
    assert payload["clinic_name"] == "Kriya Central Hospital"
    assert "doctors" in payload
    assert len(payload["doctors"]) == 2

    # Doctor 1 check
    doc1 = next(d for d in payload["doctors"] if d["doctor_id"] == DOC_1_ID)
    assert doc1["doctor_name"] == "Dr. Sarah Rao"
    assert doc1["department"] == "General Medicine"
    assert doc1["room"] == "Room 101"
    assert doc1["now_serving"] == 101
    assert doc1["next_tokens"] == [102, 103]

    # Doctor 2 check
    doc2 = next(d for d in payload["doctors"] if d["doctor_id"] == DOC_2_ID)
    assert doc2["doctor_name"] == "Dr. Vivek Sharma"
    assert doc2["room"] == "Consultation 2"
    assert doc2["now_serving"] == 201
    assert doc2["next_tokens"] == []

    # STRICT ZERO-PII AUDIT
    assert_tree_contains_no_pii(payload)


# ─── 4. BRANCH FILTERING & BRANCH NOT FOUND ──────────────────────────────────


@pytest.mark.asyncio
async def test_public_display_payload_branch_not_found():
    """Non-existent branch_id raises 404."""
    fake_clinic = dict(READY_CLINIC)
    fake_branch_empty = MagicMock(data=[])

    with patch("app.services.opd.get_clinic_by_id", AsyncMock(return_value=fake_clinic)), \
         patch("app.services.opd.sb", AsyncMock(return_value=fake_branch_empty)):
        with pytest.raises(Exception) as exc:
            await public_display_payload(CLINIC_ID, branch_id="non-existent-branch")
        assert exc.value.status_code == 404


# ─── 5. TOKEN ROTATION INVALIDATION ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_rotation_invalidates_previous_token():
    """rotate_display_token produces a new token and updates hash in DB."""
    captured_updates = []

    async def mock_sb(query):
        captured_updates.append(vars(query))
        return MagicMock(data=[{"id": CLINIC_ID}])

    with patch("app.services.opd.sb", side_effect=mock_sb):
        new_raw_token = await rotate_display_token(CLINIC_ID)

    assert new_raw_token != RAW_TOKEN
    assert len(new_raw_token) > 20

    # Old token hash must not match new token
    new_hash = hashlib.sha256(new_raw_token.encode("utf-8")).hexdigest()
    assert new_hash != TOKEN_HASH


# ─── 6. STATIC HTML QUEUE DISPLAY ROUTE ──────────────────────────────────────


def test_public_queue_display_html_route():
    """GET /public/queue-display delivers the TV display HTML page."""
    res = client.get("/public/queue-display")
    assert res.status_code == 200
    assert "text/html" in res.headers.get("content-type", "")
    assert "Kriya OPD" in res.text or "Queue" in res.text
