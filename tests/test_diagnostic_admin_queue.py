"""Tests for Diagnostic Center Admin Queue, Triage Resolution, and Stats Endpoints."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException

from app.routers.admin import (
    AdminUser,
    ResolveMatchRequest,
    ResendReportRequest,
    get_diagnostic_reports_queue,
    resolve_report_match,
    resend_lab_report,
    get_diagnostic_stats,
)


@pytest.mark.asyncio
async def test_get_diagnostic_reports_queue():
    """Verify queue endpoint returns needs_review, failed reports, and connector failures."""
    admin = AdminUser("diag_staff", role="clinic_admin", clinic_id="clinic-diag-1", user_id="user-diag-1")

    mock_sb = MagicMock()
    mock_lab_table = MagicMock()
    mock_conn_table = MagicMock()

    def table_side_effect(name):
        if name == "lab_reports":
            return mock_lab_table
        elif name == "connector_failed_reports":
            return mock_conn_table
        return MagicMock()

    mock_sb.table.side_effect = table_side_effect

    # Mock lab_reports data
    mock_lab_table.select.return_value.eq.return_value.in_.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "lr-1",
                "clinic_id": "clinic-diag-1",
                "patient_phone": "+919876543210",
                "patient_name": "Ravi Kumar",
                "status": "needs_review",
                "error_message": "Patient name mismatch with registered patient",
            },
            {
                "id": "lr-2",
                "clinic_id": "clinic-diag-1",
                "patient_phone": "+919876543211",
                "patient_name": "Sita Devi",
                "status": "failed",
                "error_message": "WhatsApp rejected message",
            },
        ]
    )

    # Mock connector_failed_reports data
    mock_conn_table.select.return_value.is_.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "cfr-1",
                "clinic_id": "clinic-diag-1",
                "vam_id": "VAM-101",
                "external_report_id": "DOC-99",
                "last_error": "MocDoc download timeout",
            }
        ]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        res = await get_diagnostic_reports_queue(clinic_id="clinic-diag-1", user=admin)

    assert len(res["needs_review"]) == 1
    assert res["needs_review"][0]["id"] == "lr-1"
    assert len(res["failed_reports"]) == 1
    assert res["failed_reports"][0]["id"] == "lr-2"
    assert len(res["connector_failures"]) == 1
    assert res["connector_failures"][0]["id"] == "cfr-1"
    assert res["total_queued"] == 3


@pytest.mark.asyncio
async def test_resolve_report_match_valid():
    """Verify resolving a match updates phone, match_source, and records audit log."""
    admin = AdminUser("diag_staff", role="clinic_admin", clinic_id="clinic-diag-1", user_id="user-diag-1")
    req = MagicMock()
    req.client.host = "127.0.0.1"

    body = ResolveMatchRequest(
        patient_phone="9876543210",
        patient_name="Ravi Kumar Corrected",
        send_now=False,
    )

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "lr-1",
                "clinic_id": "clinic-diag-1",
                "patient_phone": "+910000000000",
                "patient_name": "Ravi Kumar",
                "status": "needs_review",
            }
        ]
    )
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{}])
    mock_table.insert.return_value.execute.return_value = MagicMock(data=[{}])

    with patch("app.routers.admin.supabase", mock_sb), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock) as mock_audit:
        res = await resolve_report_match(
            report_id="lr-1",
            body=body,
            clinic_id="clinic-diag-1",
            request=req,
            user=admin,
        )

    assert res["success"] is True
    assert res["patient_phone"] == "+919876543210"
    assert res["status"] == "matched"
    mock_audit.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_report_match_invalid_phone():
    """Verify resolving with an invalid phone fails with HTTP 400."""
    admin = AdminUser("diag_staff", role="clinic_admin", clinic_id="clinic-diag-1", user_id="user-diag-1")

    body = ResolveMatchRequest(
        patient_phone="123",  # invalid
        patient_name="Ravi Kumar",
        send_now=False,
    )

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "lr-1", "clinic_id": "clinic-diag-1"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc_info:
            await resolve_report_match(
                report_id="lr-1",
                body=body,
                clinic_id="clinic-diag-1",
                user=admin,
            )
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_get_diagnostic_stats():
    """Verify diagnostic stats aggregation endpoint."""
    admin = AdminUser("diag_staff", role="clinic_admin", clinic_id="clinic-diag-1", user_id="user-diag-1")

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

    # Mock lab_reports
    mock_lab_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[
            {"id": "1", "status": "sent", "uploaded_at": "2099-01-01T10:00:00Z", "file_path": "a/b.pdf"},
            {"id": "2", "status": "needs_review", "uploaded_at": "2099-01-01T11:00:00Z", "file_path": "pending_review/2"},
            {"id": "3", "status": "failed", "uploaded_at": "2099-01-01T12:00:00Z", "file_path": "a/c.pdf"},
        ]
    )

    # Mock integration_connectors
    mock_conn_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "conn-1",
                "connector_type": "mocdoc",
                "is_enabled": True,
                "last_run_at": "2099-01-01T12:00:00Z",
                "last_success_at": "2099-01-01T12:00:00Z",
                "last_error": None,
            }
        ]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        stats = await get_diagnostic_stats(clinic_id="clinic-diag-1", user=admin)

    assert stats["reports_today"]["total"] == 3
    assert stats["reports_today"]["sent"] == 1
    assert stats["reports_today"]["needs_review"] == 1
    assert stats["reports_today"]["failed"] == 1
    assert stats["connector"]["health"] == "healthy"
