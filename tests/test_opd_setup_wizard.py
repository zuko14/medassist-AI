"""Tests for Kriya OPD OS Setup & Go-Live Wizard (Phase 1.2).

Verifies:
1. All 14 setup checklist items computation.
2. Effective state calculation (NOT_CONFIGURED, CONFIGURING, READY, DEGRADED).
3. Settings mutation via CAS (PUT /admin/opd/setup/settings) advancing state.
4. Dry-run test visit simulation (POST /admin/opd/setup/dry-run).
5. Go-live gating: 409 if incomplete, 200 when all blocking items complete.
6. Display token generation and rotation.
7. Permission enforcement on setup wizard endpoints.
"""

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin
from app.routers.admin import AdminUser
from app.services.opd import dry_run, effective_state, go_live, rotate_display_token, setup_checklist

client = TestClient(app)

CLINIC_ID = str(uuid.uuid4())
ADMIN_USER = AdminUser("clinic_admin_1", role="clinic_admin", clinic_id=CLINIC_ID, permissions=["OPD_ADMIN"])
STAFF_USER = AdminUser("receptionist", role="staff", clinic_id=CLINIC_ID, permissions=["OPD_FRONT_DESK"])
UNAUTHORIZED_USER = AdminUser("nurse_joy", role="staff", clinic_id=CLINIC_ID, permissions=[])

DOC_ID = str(uuid.uuid4())

BASE_CLINIC = {
    "id": CLINIC_ID,
    "name": "Kriya Metro Clinic",
    "address": "123 Health Ave, Bengaluru",
    "phone": "+918012345678",
    "features": {"opd_enabled": True},
    "opd_state": "CONFIGURING",
    "opd_settings": {
        "vitals_required": True,
        "after_checkin_stage": "vitals_pending",
        "billing_after_consult": True,
        "token_rule": "per_doctor_daily",
        "payment_modes": ["cash", "upi"],
        "upi_vpa": "kriya@upi",
        "confirmed_steps": [],
        "dry_run_passed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    },
    "phone_number_id": "PNID123",
    "whatsapp_access_token": "WAKEY123",
}

COMPLETE_DOCTOR = {
    "id": DOC_ID,
    "clinic_id": CLINIC_ID,
    "name": "Dr. Sarah Rao",
    "department": "General Medicine",
    "is_active": True,
    "morning_start": "09:00",
    "slot_duration_minutes": 15,
    "registration_number": "KMC-98765",
    "registration_council": "Karnataka Medical Council",
    "consultation_fee": 500,
}

ADMIN_RECORD = {
    "id": str(uuid.uuid4()),
    "clinic_id": CLINIC_ID,
    "role": "clinic_admin",
    "permissions": ["OPD_ADMIN", "OPD_FRONT_DESK"],
    "doctor_id": DOC_ID,
}


@pytest.fixture
def auth_admin():
    app.dependency_overrides[admin.verify_credentials] = lambda: ADMIN_USER
    yield
    app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.fixture
def auth_staff():
    app.dependency_overrides[admin.verify_credentials] = lambda: STAFF_USER
    yield
    app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.fixture
def auth_unauth():
    app.dependency_overrides[admin.verify_credentials] = lambda: UNAUTHORIZED_USER
    yield
    app.dependency_overrides.pop(admin.verify_credentials, None)


# ─── 1. CHECKLIST 14 ITEMS CALCULATION ───────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_checklist_all_14_items_complete():
    """All 14 checklist items return True when requirements are satisfied."""
    clinic = dict(BASE_CLINIC)

    fake_doctors = MagicMock(data=[COMPLETE_DOCTOR])
    fake_admins = MagicMock(data=[ADMIN_RECORD])

    with patch("app.services.opd.sb", AsyncMock(side_effect=[fake_doctors, fake_admins])):
        items = await setup_checklist(clinic)

    assert len(items) == 14
    keys = {item["key"] for item in items}
    expected_keys = {
        "clinic_profile",
        "operating_hours",
        "branches",
        "departments",
        "doctor_roster",
        "doctor_registration",
        "consult_durations",
        "consult_fees",
        "front_desk_permissions",
        "doctor_logins",
        "token_rules",
        "billing_setup",
        "whatsapp_templates",
        "test_run_go_live",
    }
    assert keys == expected_keys

    # Check that blocking items are marked appropriately
    blocking_map = {i["key"]: i["blocking"] for i in items}
    assert blocking_map["whatsapp_templates"] is False
    assert blocking_map["clinic_profile"] is True
    assert blocking_map["test_run_go_live"] is True

    # All blocking items should be done in this complete fixture
    failing = [i["key"] for i in items if i["blocking"] and not i["done"]]
    assert failing == []


@pytest.mark.asyncio
async def test_setup_checklist_flags_incomplete_doctors():
    """Missing NMC registration or fee marks corresponding items incomplete."""
    clinic = dict(BASE_CLINIC)
    incomplete_doc = dict(COMPLETE_DOCTOR)
    incomplete_doc["registration_number"] = None
    incomplete_doc["consultation_fee"] = None

    fake_doctors = MagicMock(data=[incomplete_doc])
    fake_admins = MagicMock(data=[ADMIN_RECORD])

    with patch("app.services.opd.sb", AsyncMock(side_effect=[fake_doctors, fake_admins])):
        items = await setup_checklist(clinic)

    item_map = {i["key"]: i for i in items}
    assert item_map["doctor_registration"]["done"] is False
    assert "registration" in item_map["doctor_registration"]["detail"].lower()
    assert item_map["consult_fees"]["done"] is False


# ─── 2. EFFECTIVE STATE CALCULATION ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_effective_state_calculation():
    """Effective state maps NOT_CONFIGURED, CONFIGURING, READY, and DEGRADED correctly."""
    # 1. NOT_CONFIGURED
    c_not_configured = {"id": CLINIC_ID, "opd_state": "NOT_CONFIGURED"}
    assert await effective_state(c_not_configured) == "NOT_CONFIGURED"

    # 2. CONFIGURING
    c_configuring = {"id": CLINIC_ID, "opd_state": "CONFIGURING"}
    assert await effective_state(c_configuring) == "CONFIGURING"

    # 3. READY (all items pass)
    c_ready = dict(BASE_CLINIC)
    c_ready["opd_state"] = "READY"
    fake_passing_checklist = [
        {"key": "clinic_profile", "blocking": True, "done": True},
        {"key": "test_run_go_live", "blocking": True, "done": True},
    ]
    assert await effective_state(c_ready, checklist=fake_passing_checklist) == "READY"

    # 4. DEGRADED (READY clinic with blocking check failing)
    fake_failing_checklist = [
        {"key": "clinic_profile", "blocking": True, "done": True},
        {"key": "doctor_registration", "blocking": True, "done": False},
    ]
    assert await effective_state(c_ready, checklist=fake_failing_checklist) == "DEGRADED"


# ─── 3. GET SETUP ENDPOINT ───────────────────────────────────────────────────


def test_get_setup_endpoint_success(auth_admin):
    """GET /admin/opd/setup returns checklist and effective state."""
    fake_clinic = dict(BASE_CLINIC)
    fake_doctors = MagicMock(data=[COMPLETE_DOCTOR])
    fake_admins = MagicMock(data=[ADMIN_RECORD])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=fake_clinic)), \
         patch("app.services.opd.sb", AsyncMock(side_effect=[fake_doctors, fake_admins])):
        res = client.get(f"/admin/opd/setup?clinic_id={CLINIC_ID}")
        assert res.status_code == 200
        data = res.json()
        assert data["state"] == "CONFIGURING"
        assert len(data["checklist"]) == 14
        assert "settings" in data


def test_get_setup_endpoint_unauthorized(auth_unauth):
    """Staff user without any OPD permission is rejected with 403."""
    res = client.get(f"/admin/opd/setup?clinic_id={CLINIC_ID}")
    assert res.status_code == 403


# ─── 4. PUT SETTINGS VIA CAS ─────────────────────────────────────────────────


def test_put_setup_settings_advances_state(auth_admin):
    """PUT /admin/opd/setup/settings merges settings and transitions NOT_CONFIGURED to CONFIGURING."""
    init_clinic = dict(BASE_CLINIC)
    init_clinic["opd_state"] = "NOT_CONFIGURED"

    updated_clinic = dict(init_clinic)
    updated_clinic["opd_state"] = "CONFIGURING"
    updated_clinic["opd_settings"] = dict(init_clinic["opd_settings"])
    updated_clinic["opd_settings"]["upi_vpa"] = "newupi@bank"

    fake_doctors = MagicMock(data=[COMPLETE_DOCTOR])
    fake_admins = MagicMock(data=[ADMIN_RECORD])
    fake_update_res = MagicMock(data=[updated_clinic])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(side_effect=[init_clinic, updated_clinic])), \
         patch("app.routers.opd.sb", AsyncMock(return_value=fake_update_res)), \
         patch("app.services.opd.sb", AsyncMock(side_effect=[fake_doctors, fake_admins])), \
         patch("app.routers.opd.log_admin_action", AsyncMock()):
        payload = {"upi_vpa": "newupi@bank", "vitals_required": True}
        res = client.put(f"/admin/opd/setup/settings?clinic_id={CLINIC_ID}", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["state"] == "CONFIGURING"
        assert data["settings"]["upi_vpa"] == "newupi@bank"


# ─── 5. DRY-RUN SIMULATION ───────────────────────────────────────────────────


def test_dry_run_simulation_success(auth_admin):
    """POST /admin/opd/setup/dry-run checks active doctors, token preview, and updates dry_run_passed_at."""
    clinic = dict(BASE_CLINIC)
    fake_docs = MagicMock(data=[COMPLETE_DOCTOR])
    fake_tok = MagicMock(data=[{"token_number": 12}])
    fake_upd = MagicMock(data=[{"id": CLINIC_ID}])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=clinic)), \
         patch("app.services.opd.sb", AsyncMock(side_effect=[fake_docs, fake_tok, fake_upd])), \
         patch("app.routers.opd.log_admin_action", AsyncMock()):
        res = client.post(f"/admin/opd/setup/dry-run?clinic_id={CLINIC_ID}")
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert len(data["checks"]) == 4
        check_names = [c["name"] for c in data["checks"]]
        assert "active_doctors" in check_names
        assert "token_generation_preview" in check_names


# ─── 6. GO-LIVE GATING ───────────────────────────────────────────────────────


def test_go_live_blocked_when_incomplete(auth_admin):
    """POST /admin/opd/setup/go-live returns 409 when blocking checklist items fail."""
    clinic = dict(BASE_CLINIC)
    # Doctor has missing registration
    bad_doc = dict(COMPLETE_DOCTOR)
    bad_doc["registration_number"] = None

    fake_doctors = MagicMock(data=[bad_doc])
    fake_admins = MagicMock(data=[ADMIN_RECORD])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=clinic)), \
         patch("app.services.opd.sb", AsyncMock(side_effect=[fake_doctors, fake_admins])):
        res = client.post(f"/admin/opd/setup/go-live?clinic_id={CLINIC_ID}")
        assert res.status_code == 409
        err = res.json()["detail"]
        assert err["error"] == "setup_incomplete"
        failing_keys = [i["key"] for i in err["failing_items"]]
        assert "doctor_registration" in failing_keys


def test_go_live_succeeds_when_complete(auth_admin):
    """POST /admin/opd/setup/go-live transitions clinic to READY when all blocking items pass."""
    clinic = dict(BASE_CLINIC)
    fake_doctors = MagicMock(data=[COMPLETE_DOCTOR])
    fake_admins = MagicMock(data=[ADMIN_RECORD])
    fake_clinic_upd = MagicMock(data=[{"id": CLINIC_ID, "opd_state": "READY"}])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=clinic)), \
         patch("app.services.opd.sb", AsyncMock(side_effect=[fake_doctors, fake_admins, fake_clinic_upd])), \
         patch("app.routers.opd.log_admin_action", AsyncMock()):
        res = client.post(f"/admin/opd/setup/go-live?clinic_id={CLINIC_ID}")
        assert res.status_code == 200
        data = res.json()
        assert data["state"] == "READY"
        assert data["went_live_at"] is not None


# ─── 7. DISPLAY TOKEN ROTATION ───────────────────────────────────────────────


def test_rotate_display_token(auth_admin):
    """POST /admin/opd/display-token generates a new token and returns display URL."""
    fake_upd = MagicMock(data=[{"id": CLINIC_ID}])

    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=BASE_CLINIC)), \
         patch("app.services.opd.sb", AsyncMock(return_value=fake_upd)), \
         patch("app.routers.opd.log_admin_action", AsyncMock()):
        res = client.post(f"/admin/opd/display-token?clinic_id={CLINIC_ID}")
        assert res.status_code == 200
        url = res.json()["url"]
        assert url.startswith("/public/queue-display#t=")
