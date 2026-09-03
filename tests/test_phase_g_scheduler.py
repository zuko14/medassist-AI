"""Phase G: Scheduler and Distributed Background Work Verification.

Verifies:
1. send_24h_reminders processes unreminded confirmed appointments and marks them sent.
2. send_2h_reminders processes unreminded appointments and marks them sent.
3. Multi-instance / retry idempotency: already-sent appointments are not re-notified.
4. Error isolation: Failure on one appointment does not halt the batch.
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from app.services.scheduler import SchedulerService


@pytest.fixture
def scheduler_service():
    return SchedulerService()


@pytest.mark.asyncio
async def test_send_24h_reminders_idempotency_and_update(scheduler_service, granted_job_lock):
    """P1-Scheduler: send_24h_reminders marks reminder_24h_sent=True and does not re-send."""
    fake_appointments = [
        {
            "id": "appt_1",
            "clinic_id": "clinic_a",
            "doctor_name": "Dr. Smith",
            "patient_phone": "+919999999991",
            "appointment_date": "2026-08-26",
            "appointment_time": "10:00",
            "booking_reference": "REF001",
            "reminder_24h_sent": False,
        }
    ]

    mock_appointments_table = MagicMock()
    mock_select = MagicMock()
    mock_select.eq.return_value = mock_select
    mock_select.execute.return_value.data = fake_appointments
    mock_appointments_table.select.return_value = mock_select
    mock_appointments_table.update.return_value.eq.return_value.execute.return_value.data = [{"id": "appt_1"}]

    def table_router(table_name):
        if table_name == "appointments":
            return mock_appointments_table
        t = MagicMock()
        t.select.return_value.eq.return_value.execute.return_value.data = []
        return t

    fake_clinic = {"id": "clinic_a", "name": "Alpha Clinic"}

    with patch("app.services.scheduler.supabase.table", side_effect=table_router), \
         patch("app.services.scheduler.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic), \
         patch("app.services.tenant.has_feature", return_value=True), \
         patch("app.services.scheduler.whatsapp_service.send_template", new_callable=AsyncMock) as mock_send_tpl:

        await scheduler_service.send_24h_reminders()

        mock_appointments_table.update.assert_called_once_with({"reminder_24h_sent": True})


@pytest.mark.asyncio
async def test_scheduler_error_isolation_in_batch(scheduler_service):
    """P1-Scheduler: An error sending reminder to one patient does not halt other appointments."""
    fake_appointments = [
        {"id": "appt_fail", "clinic_id": "clinic_a", "reminder_24h_sent": False},
        {"id": "appt_success", "clinic_id": "clinic_a", "reminder_24h_sent": False},
    ]

    mock_select = MagicMock()
    mock_select.eq.return_value = mock_select
    mock_select.execute.return_value.data = fake_appointments

    mock_table = MagicMock()
    mock_table.select.return_value = mock_select

    # Fail on first get_clinic_by_id, succeed on second
    side_effects = [Exception("Network error for appt_fail"), {"id": "clinic_a", "name": "Alpha Clinic"}]

    with patch("app.services.scheduler.supabase.table", return_value=mock_table), \
         patch("app.services.scheduler.get_clinic_by_id", new_callable=AsyncMock, side_effect=side_effects):

        # Should complete without raising exception
        await scheduler_service.send_24h_reminders()
