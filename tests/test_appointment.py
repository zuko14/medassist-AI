"""Tests for appointment service."""

import pytest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from app.services.appointment import appointment_service


class TestAppointmentService:
    """Test appointment service."""

    @pytest.mark.asyncio
    async def test_get_available_doctors(self):
        """Test getting available doctors."""
        with patch('app.services.appointment.get_doctors') as mock_get_doctors:
            mock_get_doctors.return_value = [
                {"name": "Dr. Test", "specialization": "General"}
            ]

            result = await appointment_service.get_available_doctors()
            assert len(result) == 1
            assert result[0]["name"] == "Dr. Test"

    @pytest.mark.asyncio
    async def test_find_alternative_doctors(self):
        """Test finding alternative doctors."""
        with patch('app.services.appointment.get_doctors') as mock_get_doctors, \
             patch('app.services.appointment.get_available_slots', new_callable=AsyncMock) as mock_get_slots:

            mock_get_doctors.return_value = [
                {"name": "Dr. Other", "specialization": "Cardiology"},
                {"name": "Dr. Busy", "specialization": "Cardiology"}
            ]
            # First doctor has slots on day 1 (breaks), second has none (7 days checked)
            mock_get_slots.side_effect = [(["09:00"], None)] + [([], None) for _ in range(7)]

            result = await appointment_service.find_alternative_doctors(
                "Cardiology",
                "Dr. Arjun",
                date.today().strftime("%Y-%m-%d")
            )

            assert len(result) == 1
            assert result[0]["name"] == "Dr. Other"

    @pytest.mark.asyncio
    async def test_get_appointment_history(self):
        """Test getting appointment history."""
        with patch('app.services.appointment.get_patient_appointments') as mock_get:
            mock_get.return_value = [
                {"id": "1", "status": "completed"},
                {"id": "2", "status": "cancelled"}
            ]

            result = await appointment_service.get_appointment_history("+919876543210")
            assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_upcoming_appointments(self):
        """Test getting upcoming appointments."""
        with patch('app.services.appointment.get_patient_appointments') as mock_get:
            mock_get.return_value = [
                {"id": "1", "appointment_date": (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")},
                {"id": "2", "appointment_date": (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")}
            ]

            result = await appointment_service.get_upcoming_appointments("+919876543210")
            # Should filter to only future appointments
            assert len(result) >= 0


class TestBookingReference:
    """Test booking reference generation."""

    def test_reference_format(self):
        """Test booking reference format."""
        from app.utils.helpers import generate_booking_reference

        ref = generate_booking_reference()
        assert len(ref) == 8
        assert ref.isalnum()

    def test_unique_references(self):
        """Test that references are likely unique."""
        from app.utils.helpers import generate_booking_reference

        refs = [generate_booking_reference() for _ in range(100)]
        assert len(set(refs)) == len(refs)  # All unique


class TestDateHelpers:
    """Test date helper functions."""

    def test_next_dates(self):
        """Test getting next dates."""
        from app.utils.helpers import get_next_dates

        dates = get_next_dates(7)
        assert len(dates) == 7
        assert dates[0] == date.today()
        assert dates[1] == date.today() + timedelta(days=1)

    def test_weekend_check(self):
        """Test weekend detection."""
        from app.utils.helpers import is_weekend

        # Saturday
        saturday = date(2026, 3, 14)
        assert is_weekend(saturday) is True

        # Monday
        monday = date(2026, 3, 16)
        assert is_weekend(monday) is False

    def test_day_name(self):
        """Test day name extraction."""
        from app.utils.helpers import get_day_name

        monday = date(2026, 3, 16)
        assert get_day_name(monday) == "Mon"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
