"""Tests for RBAC and Multi-Tenant Isolation in Admin Router."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import AdminUser, enforce_clinic_access, check_password_hash

client = TestClient(app)


def test_admin_user_str_compatibility():
    """Verify AdminUser behaves as a string subclass for backward compatibility."""
    user = AdminUser("admin_user", role="clinic_admin", clinic_id="clinic-123")
    assert user == "admin_user"
    assert str(user) == "admin_user"
    assert user.role == "clinic_admin"
    assert user.clinic_id == "clinic-123"


def test_admin_user_can_access_clinic():
    """Verify can_access_clinic method logic."""
    super_admin = AdminUser("super", role="super_admin", clinic_id=None)
    clinic_admin = AdminUser("clinic_mgr", role="clinic_admin", clinic_id="clinic-123")

    # Super admin can access everything
    assert super_admin.can_access_clinic("clinic-123") is True
    assert super_admin.can_access_clinic("clinic-456") is True
    assert super_admin.can_access_clinic("default") is True

    # Clinic admin can access their clinic & default
    assert clinic_admin.can_access_clinic("clinic-123") is True
    assert clinic_admin.can_access_clinic("default") is True

    # Clinic admin CANNOT access another clinic
    assert clinic_admin.can_access_clinic("clinic-456") is False


def test_enforce_clinic_access_super_admin():
    """Super admin can access requested clinic without modification."""
    super_admin = AdminUser("super", role="super_admin", clinic_id=None)
    eff = enforce_clinic_access(super_admin, "clinic-456")
    assert eff == "clinic-456"


def test_enforce_clinic_access_clinic_admin_assigned():
    """Clinic admin gets their assigned clinic when 'default' is requested."""
    clinic_admin = AdminUser("clinic_mgr", role="clinic_admin", clinic_id="clinic-123")
    eff = enforce_clinic_access(clinic_admin, "default")
    assert eff == "clinic-123"

    eff_explicit = enforce_clinic_access(clinic_admin, "clinic-123")
    assert eff_explicit == "clinic-123"


def test_enforce_clinic_access_cross_tenant_forbidden():
    """Clinic admin attempting to access another clinic throws 403 Forbidden."""
    clinic_admin = AdminUser("clinic_mgr", role="clinic_admin", clinic_id="clinic-123")
    with pytest.raises(HTTPException) as exc:
        enforce_clinic_access(clinic_admin, "clinic-999")
    assert exc.value.status_code == 403
    assert "Forbidden" in exc.value.detail


def test_check_password_hash_plain_comparison():
    """Verify plain password constant-time comparison."""
    assert check_password_hash("secret123", "secret123") is True
    assert check_password_hash("secret123", "wrongpass") is False
