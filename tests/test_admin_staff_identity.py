"""Tests for Individual Staff Identity and Administrative Action Logging (Finding #12)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers.admin import AdminUser, log_admin_action, get_admin_audit_logs


@pytest.mark.asyncio
async def test_admin_user_staff_identity():
    """Verify AdminUser captures user_id, username, role, and clinic_id."""
    admin = AdminUser(
        username="dr_smith",
        role="doctor",
        clinic_id="clinic-123",
        user_id="user-uuid-99",
    )

    assert admin.username == "dr_smith"
    assert admin.role == "doctor"
    assert admin.clinic_id == "clinic-123"
    assert admin.user_id == "user-uuid-99"


@pytest.mark.asyncio
async def test_log_admin_action():
    """Verify log_admin_action inserts structured staff identity into admin_audit_logs."""
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    admin = AdminUser(
        username="staff_jane",
        role="receptionist",
        clinic_id="clinic-456",
        user_id="user-uuid-88",
    )

    with patch("app.routers.admin.supabase", mock_sb):
        await log_admin_action(
            user=admin,
            action="DOCTOR_UPDATE",
            resource_type="doctor",
            resource_id="doc-77",
            details={"changed": "slots"},
            ip_address="192.168.1.1",
        )

        mock_sb.table.assert_called_with("admin_audit_logs")
        inserted_data = mock_table.insert.call_args[0][0]
        assert inserted_data["username"] == "staff_jane"
        assert inserted_data["role"] == "receptionist"
        assert inserted_data["clinic_id"] == "clinic-456"
        assert inserted_data["user_id"] == "user-uuid-88"
        assert inserted_data["action"] == "DOCTOR_UPDATE"


@pytest.mark.asyncio
async def test_get_admin_audit_logs_endpoint():
    """Verify GET /admin/audit-logs retrieves staff action audit logs scoped to clinic."""
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_query = MagicMock()
    mock_table.select.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.order.return_value = mock_query
    mock_query.limit.return_value = mock_query

    mock_audit_entries = [
        {
            "id": "log-1",
            "username": "dr_smith",
            "action": "DOCTOR_UPDATE",
            "created_at": "2026-08-02T12:00:00Z",
        }
    ]
    mock_query.execute.return_value = MagicMock(data=mock_audit_entries)

    admin = AdminUser(
        username="dr_smith",
        role="clinic_admin",
        clinic_id="clinic-456",
        user_id="user-uuid-88",
    )

    with patch("app.routers.admin.supabase", mock_sb):
        response = await get_admin_audit_logs(clinic_id="clinic-456", limit=50, user=admin)
        assert "audit_logs" in response
        assert len(response["audit_logs"]) == 1
        assert response["audit_logs"][0]["username"] == "dr_smith"
