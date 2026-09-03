"""Tests for the post-discharge health check-in scheduler job."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.scheduler import SchedulerService


@pytest.mark.asyncio
async def test_send_health_checkins_sends_3day_and_marks_flag(granted_job_lock):
    service = SchedulerService()
    mock_appt = {
        "id": "appt-1",
        "clinic_id": "clinic-1",
        "patient_phone": "+919876543210",
        "patient_name": "Ravi Kumar",
        "doctor_name": "Dr. Rao",
        "status": "confirmed",
    }

    mock_sb = MagicMock()
    # First query (3-day) returns the appointment, second query (7-day) returns none
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.side_effect = [
        MagicMock(data=[mock_appt]),
        MagicMock(data=[]),
    ]
    mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = None

    with patch("app.services.scheduler.supabase", mock_sb), patch(
        "app.services.scheduler.get_clinic_by_id", new_callable=AsyncMock
    ) as mock_get_clinic, patch(
        "app.services.scheduler.whatsapp_service.send_interactive_buttons", new_callable=AsyncMock
    ) as mock_send:
        mock_get_clinic.return_value = {"id": "clinic-1", "name": "Test Hospital"}

        await service.send_health_checkins()

        mock_send.assert_called_once()
        sent_phone = mock_send.call_args[0][1]
        assert sent_phone == "+919876543210"

        # Confirm the 3-day flag was marked sent
        update_call_args = mock_sb.table.return_value.update.call_args[0][0]
        assert update_call_args == {"health_checkin_3d_sent": True}
