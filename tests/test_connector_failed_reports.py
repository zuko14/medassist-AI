"""Tests for Per-Report Failure Tracking (Finding #6)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from connectors.runner import record_report_failure, record_report_success


@pytest.mark.asyncio
async def test_record_report_failure_new_entry():
    """Verify first failure for a report inserts a new row with failure_count=1."""
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    # Table select returns empty list (new failure)
    mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[]
    )

    with patch("connectors.runner.supabase", mock_sb):
        await record_report_failure(
            clinic_id="clinic-123",
            connector_type="mocdoc",
            external_report_id="VAM-1001_R1",
            error_message="Selector timeout",
            vam_id="VAM-1001",
            patient_name="Alice Smith",
        )

        mock_sb.table.assert_called_with("connector_failed_reports")
        inserted_data = mock_table.insert.call_args[0][0]
        assert inserted_data["clinic_id"] == "clinic-123"
        assert inserted_data["external_report_id"] == "VAM-1001_R1"
        assert inserted_data["failure_count"] == 1
        assert inserted_data["last_error"] == "Selector timeout"


@pytest.mark.asyncio
async def test_record_report_failure_threshold_alert():
    """Verify consecutive failure reaching threshold (3) triggers admin alert."""
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    # Table select returns existing row with failure_count=2
    mock_existing_row = {
        "id": "failed-row-uuid",
        "clinic_id": "clinic-123",
        "connector_type": "mocdoc",
        "external_report_id": "VAM-1001_R1",
        "failure_count": 2,
    }
    mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[mock_existing_row]
    )

    with patch("connectors.runner.supabase", mock_sb), patch(
        "connectors.runner.send_admin_alert", new_callable=AsyncMock
    ) as mock_alert:

        await record_report_failure(
            clinic_id="clinic-123",
            connector_type="mocdoc",
            external_report_id="VAM-1001_R1",
            error_message="Bill due pending",
            vam_id="VAM-1001",
            patient_name="Alice Smith",
            alert_threshold=3,
        )

        # Update should increment to 3
        update_data = mock_table.update.call_args[0][0]
        assert update_data["failure_count"] == 3
        assert update_data["last_error"] == "Bill due pending"

        # Alert should be triggered
        mock_alert.assert_called_once()
        alert_text = mock_alert.call_args[0][1]
        assert "Consecutive Failures: 3" in alert_text
        assert "VAM-1001_R1" in alert_text


@pytest.mark.asyncio
async def test_record_report_success_resolves_failure():
    """Verify successful report upload sets resolved_at timestamp."""
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    with patch("connectors.runner.supabase", mock_sb):
        await record_report_success(
            clinic_id="clinic-123",
            connector_type="mocdoc",
            external_report_id="VAM-1001_R1",
        )

        mock_sb.table.assert_called_with("connector_failed_reports")
        update_data = mock_table.update.call_args[0][0]
        assert "resolved_at" in update_data
        assert update_data["resolved_at"] is not None
