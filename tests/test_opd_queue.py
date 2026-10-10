"""Tests for Kriya OPD OS Live Queue Board & Stage Orchestration (Phase 1.2).

Verifies:
1. Stage transition validation (ALLOWED_TRANSITIONS matrix; forbidden transitions -> 422).
2. Optimistic concurrency control / CAS (stale expected_from -> 409).
3. Role & doctor permission guards on stage updates.
4. Doctor call-next: finishes prior patient, claims next waiting, enforces doctor isolation.
5. Live queue board projection & ETag caching (304 Not Modified on matching If-None-Match).
6. Patient recall endpoint (WhatsApp notification & logging).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin, opd
from app.routers.admin import AdminUser
from app.services.opd import ALLOWED_TRANSITIONS, STAGES, transition_stage

client = TestClient(app)

CLINIC_ID = str(uuid.uuid4())
DOC_ID = str(uuid.uuid4())
OTHER_DOC_ID = str(uuid.uuid4())

FRONT_DESK_USER = AdminUser("receptionist", role="staff", clinic_id=CLINIC_ID, permissions=["OPD_FRONT_DESK"])
DOCTOR_USER = AdminUser("dr_rao", role="staff", clinic_id=CLINIC_ID, permissions=["OPD_CLINICAL"], doctor_id=DOC_ID)
MOCK_CLINIC = {"id": CLINIC_ID, "features": {"opd_enabled": True}, "opd_state": "READY", "opd_settings": {}}


@pytest.fixture
def auth_front_desk():
    app.dependency_overrides[admin.verify_credentials] = lambda: FRONT_DESK_USER
    yield
    app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.fixture
def auth_doctor():
    app.dependency_overrides[admin.verify_credentials] = lambda: DOCTOR_USER
    yield
    app.dependency_overrides.pop(admin.verify_credentials, None)


# ─── 1. ALLOWED TRANSITIONS MATRIX ──────────────────────────────────────────


def test_allowed_transitions_matrix():
    """Verify state transition rules."""
    assert "in_consultation" in ALLOWED_TRANSITIONS["waiting"]
    assert "vitals_pending" in ALLOWED_TRANSITIONS["waiting"]
    assert "billing" in ALLOWED_TRANSITIONS["in_consultation"]
    assert "completed" in ALLOWED_TRANSITIONS["in_consultation"]
    assert "completed" in ALLOWED_TRANSITIONS["billing"]
    # Completed is a terminal state with no outbound transitions
    assert len(ALLOWED_TRANSITIONS["completed"]) == 0


def test_forbidden_transition_raises_422(auth_front_desk):
    """Attempting an invalid stage transition returns 422 Unprocessable Entity."""
    appt_id = str(uuid.uuid4())
    fake_appt = {
        "id": appt_id,
        "clinic_id": CLINIC_ID,
        "queue_status": "completed",
        "doctor_id": DOC_ID,
    }

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.services.opd.sb", AsyncMock(return_value=MagicMock(data=[fake_appt]))):
        r = client.post(
            f"/admin/opd/queue/{appt_id}/stage?clinic_id={CLINIC_ID}",
            json={"expected_from": "completed", "to_stage": "waiting"},
        )
    assert r.status_code == 422
    assert "not permitted" in r.text


# ─── 2. CAS & OPTIMISTIC CONCURRENCY CONTROL ─────────────────────────────────


def test_stage_change_cas_conflict_raises_409(auth_front_desk):
    """When expected_from doesn't match the current stage, CAS raises 409 Conflict."""
    appt_id = str(uuid.uuid4())
    fake_appt = {
        "id": appt_id,
        "clinic_id": CLINIC_ID,
        "queue_status": "in_consultation",  # Actually in consultation, but caller expects waiting!
        "doctor_id": DOC_ID,
    }

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.services.opd.sb", AsyncMock(return_value=MagicMock(data=[fake_appt]))):
        r = client.post(
            f"/admin/opd/queue/{appt_id}/stage?clinic_id={CLINIC_ID}",
            json={"expected_from": "waiting", "to_stage": "in_consultation"},
        )
    assert r.status_code == 409
    assert "Queue stage changed concurrently" in r.text


# ─── 3. PERMISSION GUARDS ───────────────────────────────────────────────────


def test_doctor_cannot_call_next_for_another_doctor(auth_doctor):
    """Doctor role cannot call patients for a different doctor's queue (403)."""
    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)):
        r = client.post(
            f"/admin/opd/queue/call-next?clinic_id={CLINIC_ID}",
            json={"doctor_id": OTHER_DOC_ID},
        )
    assert r.status_code == 403
    assert "own queue" in r.text


def test_doctor_cannot_transition_others_consultation(auth_doctor):
    """Doctor cannot transition consultations belonging to another doctor (403)."""
    appt_id = str(uuid.uuid4())
    fake_appt = {
        "id": appt_id,
        "clinic_id": CLINIC_ID,
        "queue_status": "in_consultation",
        "doctor_id": OTHER_DOC_ID,  # Belongs to other doctor
    }

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.routers.opd.sb", AsyncMock(return_value=MagicMock(data=[fake_appt]))):
        r = client.post(
            f"/admin/opd/queue/{appt_id}/stage?clinic_id={CLINIC_ID}",
            json={"expected_from": "in_consultation", "to_stage": "completed"},
        )
    assert r.status_code == 403
    assert "own consultations" in r.text


# ─── 4. CALL NEXT EXECUTION ──────────────────────────────────────────────────


def test_doctor_call_next_success(auth_doctor):
    """Calling next claims next patient in waiting queue and updates status."""
    active_appt = {
        "id": "appt-active",
        "clinic_id": CLINIC_ID,
        "token_number": 1,
        "patient_name": "Active Patient",
        "queue_status": "in_consultation",
        "doctor_id": DOC_ID,
    }
    next_appt = {
        "id": "appt-next",
        "clinic_id": CLINIC_ID,
        "token_number": 2,
        "patient_name": "Next Patient",
        "queue_status": "waiting",
        "doctor_id": DOC_ID,
        "patient_id": "pat-2",
        "created_at": "2026-10-09T09:00:00Z",
    }
    updated_next = dict(next_appt)
    updated_next["queue_status"] = "in_consultation"

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.services.opd.sb", AsyncMock(side_effect=[
             MagicMock(data=[active_appt]),  # currently in consultation
             MagicMock(data=[active_appt]),  # active appt update (to billing)
             MagicMock(data=[next_appt]),    # waiting list fetch
             MagicMock(data=[updated_next]), # next appt claim update (to in_consultation)
             MagicMock(data=[]),             # encounter check
             MagicMock(data=[{}]),           # encounter draft insert
         ])), \
         patch("app.routers.opd.log_admin_action", AsyncMock()):
        r = client.post(
            f"/admin/opd/queue/call-next?clinic_id={CLINIC_ID}",
            json={"doctor_id": DOC_ID},
        )

    assert r.status_code == 200
    data = r.json()
    assert data["called"]["token_number"] == 2
    assert data["called"]["stage"] == "in_consultation"


# ─── 5. LIVE QUEUE BOARD & ETAG 304 CACHING ─────────────────────────────────


def test_live_queue_board_and_etag_caching(auth_front_desk):
    """Queue board returns structure and honors If-None-Match with 304."""
    appt1 = {
        "id": "a1",
        "clinic_id": CLINIC_ID,
        "token_number": 1,
        "patient_name": "Suresh",
        "queue_status": "waiting",
        "doctor_id": DOC_ID,
        "priority": "normal",
        "created_at": "2026-10-09T09:00:00Z",
    }
    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.services.opd.sb", AsyncMock(return_value=MagicMock(data=[appt1]))):
        # 1. First fetch
        r1 = client.get(f"/admin/opd/queue?clinic_id={CLINIC_ID}")
        assert r1.status_code == 200
        etag = r1.headers.get("ETag")
        assert etag is not None
        body = r1.json()
        assert len(body["doctors"]) == 1
        assert body["doctors"][0]["counts"]["waiting"] == 1

        # 2. Re-fetch with matching If-None-Match
        r2 = client.get(
            f"/admin/opd/queue?clinic_id={CLINIC_ID}",
            headers={"If-None-Match": etag},
        )
        assert r2.status_code == 304


# ─── 6. RECALL NOTIFICATION ─────────────────────────────────────────────────


def test_recall_patient_whatsapp(auth_front_desk):
    """Recalling patient triggers WhatsApp notification."""
    appt = {
        "id": "a1",
        "clinic_id": CLINIC_ID,
        "patient_phone": "+919876543210",
        "token_number": "D1-005",
        "doctor_name": "Dr. Rao",
    }

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.routers.opd.sb", AsyncMock(return_value=MagicMock(data=[appt]))), \
         patch("app.services.whatsapp.whatsapp_service.send_text", AsyncMock(return_value=True)):
        r = client.post(f"/admin/opd/queue/a1/recall?clinic_id={CLINIC_ID}")

    assert r.status_code == 200
    assert r.json()["notified"] is True
