"""Phase 1.1 OPD OS feature gate, entitlement, and staff-doctor link tests.

Verifies:
1. Enterprise plan wildcard ("*") does not grant OPD without explicit owner opt-in.
2. Partner accounts and diagnostic-only plans cannot enable OPD (400 eligibility guard).
3. /admin/me surfaces opd_enabled, opd_state, opd_doctor_id.
4. Disable with blockers returns 409 + preview; confirm_disable=True disables safely.
5. Re-enabling an already live clinic transitions to READY.
6. Doctor role requires linked doctor_id; cross-tenant doctor_id is 422; duplicate doctor_id is 409.
7. Doctor registration numbers are validated, and DELETE /admin/doctors with FK rows returns 409.
"""

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routers import admin, platform
from app.routers.admin import AdminUser
from app.services.tenant import opd_eligible, opd_enabled

client = TestClient(app)

OWNER_AUTH = {
    "Authorization": "Basic "
    + base64.b64encode(f"{settings.owner_username}:{settings.owner_password}".encode()).decode()
}


# ─── 1. PRIMITIVES: opd_eligible & opd_enabled ─────────────────────────────


def test_enterprise_wildcard_does_not_enable_opd():
    """Enterprise plan with wildcard features does not enable OPD without explicit opt-in."""
    clinic = {"plan": "enterprise", "features": {"*": True}}
    assert opd_enabled(clinic) is False

    clinic_opted = {"plan": "enterprise", "features": {"*": True, "opd_enabled": True}}
    assert opd_enabled(clinic_opted) is True


def test_opd_eligible_guards():
    """Only hospital/clinic tenants with consultation booking can enable OPD."""
    # Standard clinic plans
    assert opd_eligible({"account_type": "tenant", "plan": "soloclinic"}) is True
    assert opd_eligible({"account_type": "tenant", "plan": "polyclinic"}) is True
    assert opd_eligible({"account_type": "tenant", "plan": "enterprise"}) is True
    assert opd_eligible({"account_type": "tenant", "plan": "dental"}) is True

    # Corporate partner accounts cannot
    assert opd_eligible({"account_type": "corporate_partner", "plan": "polyclinic"}) is False

    # Diagnostic-only plans cannot
    assert opd_eligible({"account_type": "tenant", "plan": "diagstream"}) is False
    assert opd_eligible({"account_type": "tenant", "plan": "diagbooking"}) is False


# ─── 2. PLATFORM ROUTER: TOGGLE, ELIGIBILITY & DEACTIVATION ───────────────


@pytest.fixture
def owner_session():
    app.dependency_overrides[platform.verify_owner_credentials] = lambda: AdminUser(
        "owner", role="platform_owner", user_id="platform_owner_env"
    )
    yield
    app.dependency_overrides.pop(platform.verify_owner_credentials, None)


def test_platform_toggle_rejects_ineligible_clinic(owner_session):
    """Platform feature toggle returns 400 when attempting to enable OPD on ineligible clinics."""
    clinic_id = str(uuid.uuid4())
    fake_diag = MagicMock(data=[{"account_type": "tenant", "plan": "diagstream", "features": {}}])

    with patch.object(platform, "sb", AsyncMock(return_value=fake_diag)):
        r = client.patch(
            f"/platform/clinics/{clinic_id}/features",
            json={"feature": "opd_enabled", "enabled": True},
            headers=OWNER_AUTH,
        )
    assert r.status_code == 400
    assert "only available to regular clinic/hospital tenants" in r.json()["detail"]


def test_platform_toggle_enables_opd_and_provisions_defaults(owner_session):
    """Platform toggle enables OPD, provisions settings/templates, and sets CONFIGURING or READY."""
    clinic_id = str(uuid.uuid4())
    fake_clinic = MagicMock(data=[{"account_type": "tenant", "plan": "polyclinic", "features": {}}])
    fake_full = MagicMock(data=[{"opd_settings": {}, "opd_state": "NOT_CONFIGURED"}])

    with patch.object(platform, "sb", AsyncMock(side_effect=[fake_clinic, fake_full, MagicMock(data=[{}])])) as sb_mock, \
         patch("app.services.opd.provision_defaults", new_callable=AsyncMock) as prov_mock, \
         patch.object(platform, "log_admin_action", AsyncMock()):
        r = client.patch(
            f"/platform/clinics/{clinic_id}/features",
            json={"feature": "opd_enabled", "enabled": True},
            headers=OWNER_AUTH,
        )
    assert r.status_code == 200
    prov_mock.assert_called_once_with(clinic_id)
    # Update call should set opd_state to CONFIGURING
    upd_call = sb_mock.call_args_list[-1]
    assert upd_call.args[0].request.json["opd_state"] == "CONFIGURING"


def test_platform_toggle_re_enable_live_clinic_sets_ready(owner_session):
    """If went_live_at exists in opd_settings, re-enabling transitions to READY."""
    clinic_id = str(uuid.uuid4())
    fake_clinic = MagicMock(data=[{"account_type": "tenant", "plan": "polyclinic", "features": {}}])
    fake_full = MagicMock(data=[{"opd_settings": {"went_live_at": "2026-10-09T00:00:00Z"}, "opd_state": "DISABLED"}])

    with patch.object(platform, "sb", AsyncMock(side_effect=[fake_clinic, fake_full, MagicMock(data=[{}])])) as sb_mock, \
         patch("app.services.opd.provision_defaults", new_callable=AsyncMock), \
         patch.object(platform, "log_admin_action", AsyncMock()):
        r = client.patch(
            f"/platform/clinics/{clinic_id}/features",
            json={"feature": "opd_enabled", "enabled": True},
            headers=OWNER_AUTH,
        )
    assert r.status_code == 200
    upd_call = sb_mock.call_args_list[-1]
    assert upd_call.args[0].request.json["opd_state"] == "READY"


def test_platform_toggle_disable_with_blockers_returns_409(owner_session):
    """Disabling with active queue or open drafts without confirm_disable returns 409."""
    clinic_id = str(uuid.uuid4())
    fake_clinic = MagicMock(data=[{"account_type": "tenant", "plan": "polyclinic", "features": {"opd_enabled": True}}])
    fake_preview = {
        "blocking": True,
        "blocking_reasons": ["1 patient currently in consultation"],
        "retained": {"encounters": 10},
    }

    with patch.object(platform, "sb", AsyncMock(return_value=fake_clinic)), \
         patch("app.services.opd.deactivation_preview", new_callable=AsyncMock, return_value=fake_preview):
        r = client.patch(
            f"/platform/clinics/{clinic_id}/features",
            json={"feature": "opd_enabled", "enabled": False, "confirm_disable": False},
            headers=OWNER_AUTH,
        )
    assert r.status_code == 409
    data = r.json()["detail"]
    assert "active queue or draft records exist" in data["message"]
    assert data["preview"]["blocking"] is True


def test_platform_toggle_disable_with_confirm_disable_succeeds(owner_session):
    """Disabling with confirm_disable=True succeeds and sets state to DISABLED."""
    clinic_id = str(uuid.uuid4())
    fake_clinic = MagicMock(data=[{"account_type": "tenant", "plan": "polyclinic", "features": {"opd_enabled": True}}])
    fake_preview = {
        "blocking": True,
        "blocking_reasons": ["1 open shift"],
        "retained": {"invoices": 5},
    }

    with patch.object(platform, "sb", AsyncMock(side_effect=[fake_clinic, MagicMock(data=[{}])])) as sb_mock, \
         patch("app.services.opd.deactivation_preview", new_callable=AsyncMock, return_value=fake_preview), \
         patch.object(platform, "log_admin_action", AsyncMock()):
        r = client.patch(
            f"/platform/clinics/{clinic_id}/features",
            json={"feature": "opd_enabled", "enabled": False, "confirm_disable": True},
            headers=OWNER_AUTH,
        )
    assert r.status_code == 200
    upd_call = sb_mock.call_args_list[-1]
    assert upd_call.args[0].request.json["opd_state"] == "DISABLED"


# ─── 3. ADMIN /admin/me ENDPOINT ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_me_surfaces_opd_flags():
    """GET /admin/me returns opd_enabled, opd_state, and opd_doctor_id."""
    clinic_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    admin_user = AdminUser(
        username="drsharma",
        role="staff",
        clinic_id=clinic_id,
        user_id="user-1",
        staff_role="DOCTOR",
        doctor_id=doc_id,
        permissions=["OPD_CLINICAL"],
    )

    fake_clinic = {
        "id": clinic_id,
        "name": "Apollo Clinic",
        "plan": "polyclinic",
        "features": {"opd_enabled": True},
        "opd_state": "READY",
    }

    with patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic):
        res = await admin.get_current_admin(clinic_id=clinic_id, user=admin_user)
    assert res["opd_enabled"] is True
    assert res["opd_state"] == "READY"
    assert res["opd_doctor_id"] == doc_id


# ─── 4. STAFF & DOCTOR LINKING VALIDATION ──────────────────────────────────


@pytest.mark.asyncio
async def test_staff_create_doctor_role_requires_doctor_id():
    """Creating a staff account with DOCTOR role requires doctor_id."""
    clinic_id = str(uuid.uuid4())
    admin_user = AdminUser("admin", role="clinic_admin", clinic_id=clinic_id, user_id="u-1")
    body = admin.StaffCreate(
        username="drno_id",
        password="ValidPassword123!",
        staff_role="DOCTOR",
        doctor_id=None,
    )

    with pytest.raises(HTTPException) as exc:
        await admin.create_staff(body, clinic_id=clinic_id, user=admin_user)
    assert exc.value.status_code == 422
    assert "A doctor account must be linked to a doctor profile" in exc.value.detail


@pytest.mark.asyncio
async def test_staff_create_cross_tenant_doctor_id_refused():
    """Linking a doctor from another clinic returns 422."""
    clinic_id = str(uuid.uuid4())
    other_clinic = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    admin_user = AdminUser("admin", role="clinic_admin", clinic_id=clinic_id, user_id="u-1")

    body = admin.StaffCreate(
        username="dr_other",
        password="ValidPassword123!",
        staff_role="DOCTOR",
        doctor_id=doc_id,
    )

    # Doctor check returns empty for this clinic_id
    with patch("app.routers.admin.sb", AsyncMock(return_value=MagicMock(data=[]))):
        with pytest.raises(HTTPException) as exc:
            await admin.create_staff(body, clinic_id=clinic_id, user=admin_user)
    assert exc.value.status_code == 422
    assert "Selected doctor does not belong to your clinic" in exc.value.detail


@pytest.mark.asyncio
async def test_staff_create_duplicate_doctor_id_refused():
    """Linking a doctor already linked to another staff account returns 409."""
    clinic_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    admin_user = AdminUser("admin", role="clinic_admin", clinic_id=clinic_id, user_id="u-1")

    body = admin.StaffCreate(
        username="dr_dup",
        password="ValidPassword123!",
        staff_role="DOCTOR",
        doctor_id=doc_id,
    )

    # First query: doctor exists. Second query: duplicate found in clinic_admins.
    with patch(
        "app.routers.admin.sb",
        AsyncMock(side_effect=[
            MagicMock(data=[{"id": doc_id}]),
            MagicMock(data=[{"id": "existing-staff-id"}]),
        ]),
    ):
        with pytest.raises(HTTPException) as exc:
            await admin.create_staff(body, clinic_id=clinic_id, user=admin_user)
    assert exc.value.status_code == 409
    assert "This doctor is already linked to a staff login" in exc.value.detail


# ─── 5. DOCTOR REGISTRATION VALIDATION & DELETE FK GUARD ───────────────────


def test_doctor_create_registration_regex():
    """Doctor registration numbers are checked for valid length and characters."""
    # Valid
    doc = admin.DoctorCreate(
        name="Dr. Mehta",
        department="General Medicine",
        specialization="General",
        registration_number="MCI/2015/12345",
        registration_council="Medical Council of India",
    )
    assert doc.registration_number == "MCI/2015/12345"

    # Too short
    with pytest.raises(ValueError):
        admin.DoctorCreate(
            name="Dr. Mehta",
            department="General Medicine",
            specialization="General",
            registration_number="AB",
        )

    # Invalid characters
    with pytest.raises(ValueError):
        admin.DoctorCreate(
            name="Dr. Mehta",
            department="General Medicine",
            specialization="General",
            registration_number="MCI@123$",
        )


@pytest.mark.asyncio
async def test_doctor_delete_fk_23503_maps_to_409():
    """DELETE /admin/doctors maps Postgres FK error 23503 to 409 conflict."""
    clinic_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    admin_user = AdminUser("admin", role="clinic_admin", clinic_id=clinic_id, user_id="u-1")

    # Simulate PostgREST raising an exception containing error code 23503
    fk_error = Exception("23503: update or delete on table 'doctors' violates foreign key constraint")

    with patch("app.routers.admin.sb", AsyncMock(side_effect=fk_error)):
        with pytest.raises(HTTPException) as exc:
            await admin.delete_doctor(doc_id, clinic_id="default", user=admin_user)
    assert exc.value.status_code == 409
    assert "Doctor has OPD clinical or billing records" in exc.value.detail
