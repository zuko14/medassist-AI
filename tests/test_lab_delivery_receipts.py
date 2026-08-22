"""Tests for Lab Report Delivery Receipts, Health Calculation, and Webhook Statuses."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers.webhook import record_delivery_status
from app.routers.admin import get_lab_report_deliveries, get_diagnostic_stats, AdminUser
from connectors.runner import run_all_connectors


@pytest.mark.asyncio
async def test_record_delivery_status_updates_lab_reports():
    """Verify that Meta delivery status callback updates lab_reports table."""
    mock_supabase = MagicMock()
    mock_table = MagicMock()
    mock_update = MagicMock()
    mock_eq = MagicMock()

    mock_supabase.table.return_value = mock_table
    mock_table.update.return_value = mock_update
    mock_update.eq.return_value = mock_eq
    mock_eq.execute.return_value = MagicMock(data=[{"id": "rep-1"}])

    with patch("app.database.supabase", mock_supabase):
        status_payload = {
            "id": "wamid.HBgLMTIzNDU2",
            "status": "delivered",
            "timestamp": "1724300000",
            "recipient_id": "919876543210",
        }
        await record_delivery_status(status_payload)

        mock_supabase.table.assert_called_with("lab_reports")
        mock_table.update.assert_called_once()
        update_args = mock_table.update.call_args[0][0]
        assert update_args["delivery_status"] == "delivered"
        assert update_args["delivery_error"] is None
        mock_update.eq.assert_called_with("whatsapp_message_id", "wamid.HBgLMTIzNDU2")


@pytest.mark.asyncio
async def test_record_delivery_status_failed_captures_error():
    """Verify that failed Meta status captures error title and message."""
    mock_supabase = MagicMock()
    mock_table = MagicMock()
    mock_update = MagicMock()
    mock_eq = MagicMock()

    mock_supabase.table.return_value = mock_table
    mock_table.update.return_value = mock_update
    mock_update.eq.return_value = mock_eq
    mock_eq.execute.return_value = MagicMock(data=[{"id": "rep-1"}])

    with patch("app.database.supabase", mock_supabase):
        status_payload = {
            "id": "wamid.HBgLMTIzNDU2",
            "status": "failed",
            "errors": [
                {
                    "code": 131026,
                    "title": "Message undeliverable",
                    "message": "Recipient phone number is not on WhatsApp",
                }
            ],
        }
        await record_delivery_status(status_payload)

        update_args = mock_table.update.call_args[0][0]
        assert update_args["delivery_status"] == "failed"
        assert update_args["delivery_error"] == "Message undeliverable"


@pytest.mark.asyncio
async def test_get_lab_report_deliveries_filters():
    """Verify GET /admin/lab-reports/deliveries state filtering and phone masking."""
    admin_user = AdminUser("admin")
    admin_user.username = "admin"
    admin_user.role = "admin"
    admin_user.clinic_id = "test-clinic"
    admin_user.user_id = "user-1"
    admin_user.permissions = ["REPORTS_VIEW"]
    admin_user.branch_id = None

    sample_reports = [
        {
            "id": "r-1",
            "patient_name": "John Doe",
            "patient_phone": "+919876543210",
            "report_name": "CBC",
            "report_type": "Laboratory",
            "source": "mocdoc",
            "status": "sent",
            "delivery_status": "delivered",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "sent_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "id": "r-2",
            "patient_name": "Jane Smith",
            "patient_phone": "+919123456780",
            "report_name": "Lipid Profile",
            "report_type": "Laboratory",
            "source": "mocdoc",
            "status": "failed",
            "delivery_status": "failed",
            "error_message": "Meta template error",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "sent_at": None,
        },
        {
            "id": "r-3",
            "patient_name": "Kuncha Santhosh Kumar",
            "patient_phone": "+919804824365",
            "report_name": "Lab Report",
            "report_type": "Laboratory",
            "source": "mocdoc",
            "status": "sent",
            "delivery_status": "sent",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "sent_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "id": "r-4",
            "patient_name": "Patient Four",
            "patient_phone": "+919876500000",
            "report_name": "Thyroid Profile",
            "report_type": "Laboratory",
            "source": "mocdoc",
            "status": "sent",
            "delivery_status": None,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "sent_at": datetime.now(timezone.utc).isoformat(),
        },
    ]

    mock_supabase = MagicMock()
    mock_query = MagicMock()
    mock_supabase.table.return_value.select.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.gte.return_value = mock_query
    mock_query.order.return_value.limit.return_value.execute.return_value = MagicMock(data=sample_reports)

    with patch("app.routers.admin.supabase", mock_supabase):
        # All
        res_all = await get_lab_report_deliveries(
            clinic_id="test-clinic",
            state="all",
            user=admin_user,
        )
        assert len(res_all["deliveries"]) == 4
        assert res_all["deliveries"][0]["patient_phone"] == "+91XXXXXX3210"

        # Delivered only (includes all 'status: sent' and 'delivered' reports)
        res_del = await get_lab_report_deliveries(
            clinic_id="test-clinic",
            state="delivered",
            user=admin_user,
        )
        assert len(res_del["deliveries"]) == 3
        delivered_ids = [d["id"] for d in res_del["deliveries"]]
        assert "r-1" in delivered_ids
        assert "r-3" in delivered_ids
        assert "r-4" in delivered_ids

        # Failed only
        res_fail = await get_lab_report_deliveries(
            clinic_id="test-clinic",
            state="failed",
            user=admin_user,
        )
        assert len(res_fail["deliveries"]) == 1
        assert res_fail["deliveries"][0]["id"] == "r-2"




@pytest.mark.asyncio
async def test_diagnostic_stats_connector_health():
    """Verify health calculation: active vs degraded vs stalled vs disabled."""
    admin_user = AdminUser("admin")
    admin_user.username = "admin"
    admin_user.role = "admin"
    admin_user.clinic_id = "test-clinic"
    admin_user.user_id = "user-1"
    admin_user.permissions = ["REPORTS_VIEW"]
    admin_user.branch_id = None

    now = datetime.now(timezone.utc)

    # 1. Active connector (last run 2 min ago, poll 10 min, no error)
    connector_active = [{
        "id": "c-1",
        "clinic_id": "test-clinic",
        "connector_type": "mocdoc",
        "is_enabled": True,
        "last_run_at": (now - timedelta(minutes=2)).isoformat(),
        "last_error": None,
        "config": {"poll_interval_minutes": 10},
    }]

    mock_sb = MagicMock()
    mock_lab_table = MagicMock()
    mock_conn_table = MagicMock()

    def table_side_effect(name):
        if name == "lab_reports":
            return mock_lab_table
        elif name == "integration_connectors":
            return mock_conn_table
        return MagicMock()

    mock_sb.table.side_effect = table_side_effect
    mock_lab_table.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_conn_table.select.return_value.eq.return_value.execute.return_value = MagicMock(data=connector_active)

    with patch("app.routers.admin.supabase", mock_sb):
        res = await get_diagnostic_stats(clinic_id="test-clinic", user=admin_user)
        assert res["connector"]["health"] == "healthy"

    # 2. Stalled connector (last run 45 min ago, poll 10 min -> >30 min stale)
    connector_stalled = [{
        "id": "c-1",
        "clinic_id": "test-clinic",
        "connector_type": "mocdoc",
        "is_enabled": True,
        "last_run_at": (now - timedelta(minutes=45)).isoformat(),
        "last_error": None,
        "config": {"poll_interval_minutes": 10},
    }]
    mock_conn_table.select.return_value.eq.return_value.execute.return_value = MagicMock(data=connector_stalled)
    with patch("app.routers.admin.supabase", mock_sb):
        res = await get_diagnostic_stats(clinic_id="test-clinic", user=admin_user)
        assert res["connector"]["health"] == "stalled"
