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
    mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.is_.return_value.execute.return_value = MagicMock(
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
    mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.is_.return_value.execute.return_value = MagicMock(
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


def test_admin_alert_reports_refused_send_as_failure():
    """send_text returns False (never raises) when outside the 24h window.

    Treating that as success meant a connector could fail repeatedly while
    every alert about it was silently dropped.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from connectors.runner import send_admin_alert

    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value \
        .single.return_value.execute.return_value.data = {
            "config": {"admin_alert_phone": "+919999999929"}
        }

    mock_wa = MagicMock()
    mock_wa.send_text = AsyncMock(return_value=False)

    with patch("connectors.runner.supabase", mock_sb), \
         patch("connectors.runner._scope_by_branch", lambda q, b: q), \
         patch("app.services.whatsapp.whatsapp_service", mock_wa), \
         patch("app.services.tenant.get_clinic_by_id",
               new_callable=AsyncMock, return_value={"id": "c1", "name": "Test"}):
        assert asyncio.run(send_admin_alert("c1", "connector is down")) is False

        mock_wa.send_text = AsyncMock(return_value=True)
        assert asyncio.run(send_admin_alert("c1", "connector is down")) is True


def test_admin_alert_missing_phone_returns_false():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from connectors.runner import send_admin_alert

    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value \
        .single.return_value.execute.return_value.data = {"config": {}}

    with patch("connectors.runner.supabase", mock_sb), \
         patch("connectors.runner._scope_by_branch", lambda q, b: q), \
         patch("app.services.tenant.get_clinic_by_id",
               new_callable=AsyncMock, return_value={"id": "c1", "name": "Test"}):
        assert asyncio.run(send_admin_alert("c1", "msg")) is False


def _alert_mocks(template_name, send_text_ok, send_template_ok):
    from unittest.mock import AsyncMock, MagicMock
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value \
        .single.return_value.execute.return_value.data = {
            "config": {"admin_alert_phone": "+919999999929"}
        }
    mock_wa = MagicMock()
    mock_wa.send_text = AsyncMock(return_value=send_text_ok)
    mock_wa.send_template = AsyncMock(return_value=send_template_ok)
    return mock_sb, mock_wa


def test_admin_alert_falls_back_to_template_with_flattened_body():
    """Outside the 24h window the alert must go via template.

    Meta rejects newlines/tabs/4+ spaces inside template parameters (132000),
    and every connector alert body is multi-line — so it must be flattened.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch
    from connectors.runner import send_admin_alert

    mock_sb, mock_wa = _alert_mocks("connector_admin_alert", False, True)
    multiline = "⚠️ MocDoc Connector Alert\n\nFound: 17\nFailed: 17\n\nCheck dashboard."

    with patch("connectors.runner.supabase", mock_sb), \
         patch("connectors.runner._scope_by_branch", lambda q, b: q), \
         patch("connectors.runner.settings.admin_alert_template_name", "connector_admin_alert"), \
         patch("app.services.whatsapp.whatsapp_service", mock_wa), \
         patch("app.services.tenant.get_clinic_by_id",
               new_callable=AsyncMock, return_value={"id": "c1", "name": "Test"}):
        assert asyncio.run(send_admin_alert("c1", multiline)) is True

    body = mock_wa.send_template.await_args.kwargs["components"][0]
    sent_text = body["parameters"][0]["text"]
    assert "\n" not in sent_text and "\t" not in sent_text
    assert "    " not in sent_text
    assert len(sent_text) <= 1000
    assert "Failed: 17" in sent_text


def test_admin_alert_false_when_template_also_refused():
    import asyncio
    from unittest.mock import AsyncMock, patch
    from connectors.runner import send_admin_alert

    mock_sb, mock_wa = _alert_mocks("connector_admin_alert", False, False)
    with patch("connectors.runner.supabase", mock_sb), \
         patch("connectors.runner._scope_by_branch", lambda q, b: q), \
         patch("connectors.runner.settings.admin_alert_template_name", "connector_admin_alert"), \
         patch("app.services.whatsapp.whatsapp_service", mock_wa), \
         patch("app.services.tenant.get_clinic_by_id",
               new_callable=AsyncMock, return_value={"id": "c1", "name": "Test"}):
        assert asyncio.run(send_admin_alert("c1", "boom")) is False


def test_admin_alert_skips_template_when_unconfigured():
    import asyncio
    from unittest.mock import AsyncMock, patch
    from connectors.runner import send_admin_alert

    mock_sb, mock_wa = _alert_mocks("", False, True)
    with patch("connectors.runner.supabase", mock_sb), \
         patch("connectors.runner._scope_by_branch", lambda q, b: q), \
         patch("connectors.runner.settings.admin_alert_template_name", ""), \
         patch("app.services.whatsapp.whatsapp_service", mock_wa), \
         patch("app.services.tenant.get_clinic_by_id",
               new_callable=AsyncMock, return_value={"id": "c1", "name": "Test"}):
        assert asyncio.run(send_admin_alert("c1", "boom")) is False
    mock_wa.send_template.assert_not_awaited()
