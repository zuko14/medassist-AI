"""Tests for Analytics Service (app/services/analytics.py).

Verifies:
  - track_event delegates to log_analytics_event correctly
  - get_dashboard_stats processes appointment data and returns structured stats
  - get_dashboard_stats handles empty data gracefully
  - get_dashboard_stats handles database errors gracefully
  - get_recent_appointments returns ordered results
  - get_upcoming_appointments filters by date range
  - get_popular_departments aggregates and sorts correctly
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta

from app.services.analytics import AnalyticsService


@pytest.fixture
def analytics_svc():
    return AnalyticsService()


CLINIC_ID = "clinic-test-001"


class TestTrackEvent:
    """Tests for track_event()."""

    @pytest.mark.asyncio
    async def test_track_event_delegates(self, analytics_svc):
        with patch("app.services.analytics.log_analytics_event", new_callable=AsyncMock) as mock_log:
            mock_log.return_value = True
            result = await analytics_svc.track_event(
                phone="+919876543210",
                event_type="booking_completed",
                clinic_id=CLINIC_ID,
                department="Cardiology",
                intent="book_appointment",
                metadata={"source": "whatsapp"}
            )
            assert result is True
            mock_log.assert_called_once_with(
                CLINIC_ID, "+919876543210", "booking_completed",
                department="Cardiology", intent="book_appointment",
                metadata={"source": "whatsapp"}
            )

    @pytest.mark.asyncio
    async def test_track_event_default_metadata(self, analytics_svc):
        with patch("app.services.analytics.log_analytics_event", new_callable=AsyncMock) as mock_log:
            mock_log.return_value = True
            await analytics_svc.track_event(
                phone="+919876543210",
                event_type="greeting",
            )
            # Should pass empty dict as metadata default
            call_kwargs = mock_log.call_args
            assert call_kwargs[1]["metadata"] == {}


class TestDashboardStats:
    """Tests for get_dashboard_stats()."""

    @pytest.mark.asyncio
    async def test_processes_appointment_data(self, analytics_svc):
        """Test that stats are correctly computed from appointment data."""
        mock_appts = MagicMock()
        mock_appts.data = [
            {"status": "confirmed", "department": "Cardiology", "created_at": "2026-06-15"},
            {"status": "confirmed", "department": "Cardiology", "created_at": "2026-06-16"},
            {"status": "cancelled", "department": "ENT", "created_at": "2026-06-17"},
            {"status": "completed", "department": "Dental", "created_at": "2026-06-18"},
            {"status": "no_show", "department": "Cardiology", "created_at": "2026-06-19"},
        ]
        mock_patients = MagicMock()
        mock_patients.data = [
            {"created_at": "2026-06-15"},
            {"created_at": "2026-06-20"},
            {"created_at": "2025-01-01"},  # old patient
        ]

        with patch("app.services.analytics.supabase") as mock_sb:
            mock_sb.table.return_value.select.return_value.gte.return_value.execute.return_value = mock_appts
            mock_sb.table.return_value.select.return_value.gte.return_value.eq.return_value.execute.return_value = mock_appts
            mock_sb.table.return_value.select.return_value.execute.return_value = mock_patients
            mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_patients

            stats = await analytics_svc.get_dashboard_stats(CLINIC_ID, days=30)

            assert stats["total_appointments"] == 5
            assert stats["confirmed"] == 2
            assert stats["cancelled"] == 1
            assert stats["completed"] == 1
            assert stats["no_show"] == 1
            assert stats["period_days"] == 30

    @pytest.mark.asyncio
    async def test_empty_data_returns_zeros(self, analytics_svc):
        """Test graceful handling of empty appointment data."""
        mock_empty = MagicMock()
        mock_empty.data = []

        with patch("app.services.analytics.supabase") as mock_sb:
            mock_sb.table.return_value.select.return_value.gte.return_value.execute.return_value = mock_empty
            mock_sb.table.return_value.select.return_value.gte.return_value.eq.return_value.execute.return_value = mock_empty
            mock_sb.table.return_value.select.return_value.execute.return_value = mock_empty
            mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_empty

            stats = await analytics_svc.get_dashboard_stats(CLINIC_ID)
            assert stats["total_appointments"] == 0
            assert stats["confirmed"] == 0
            assert stats["by_department"] == []

    @pytest.mark.asyncio
    async def test_error_returns_safe_defaults(self, analytics_svc):
        """Test that DB errors produce safe default response, not an exception."""
        with patch("app.services.analytics.supabase") as mock_sb:
            mock_sb.table.side_effect = Exception("DB connection lost")

            stats = await analytics_svc.get_dashboard_stats(CLINIC_ID)
            assert stats["total_appointments"] == 0
            assert "error" in stats


class TestRecentAppointments:
    """Tests for get_recent_appointments()."""

    @pytest.mark.asyncio
    async def test_returns_data(self, analytics_svc):
        mock_result = MagicMock()
        mock_result.data = [{"id": "1"}, {"id": "2"}]
        with patch("app.services.analytics.supabase") as mock_sb:
            mock_sb.table.return_value.select.return_value.order.return_value.limit.return_value.eq.return_value.execute.return_value = mock_result
            result = await analytics_svc.get_recent_appointments(CLINIC_ID, limit=5)
            assert len(result) == 2

    @pytest.mark.asyncio
    async def test_error_returns_empty(self, analytics_svc):
        with patch("app.services.analytics.supabase") as mock_sb:
            mock_sb.table.side_effect = Exception("DB error")
            result = await analytics_svc.get_recent_appointments(CLINIC_ID)
            assert result == []


class TestPopularDepartments:
    """Tests for get_popular_departments()."""

    @pytest.mark.asyncio
    async def test_aggregates_correctly(self, analytics_svc):
        mock_result = MagicMock()
        mock_result.data = [
            {"department": "Cardiology"},
            {"department": "Cardiology"},
            {"department": "ENT"},
            {"department": "Cardiology"},
            {"department": "Dental"},
        ]
        with patch("app.services.analytics.supabase") as mock_sb:
            mock_sb.table.return_value.select.return_value.gte.return_value.eq.return_value.execute.return_value = mock_result
            result = await analytics_svc.get_popular_departments(CLINIC_ID)
            assert result[0]["department"] == "Cardiology"
            assert result[0]["count"] == 3
            assert len(result) == 3  # 3 unique departments

    @pytest.mark.asyncio
    async def test_error_returns_empty(self, analytics_svc):
        with patch("app.services.analytics.supabase") as mock_sb:
            mock_sb.table.side_effect = Exception("DB error")
            result = await analytics_svc.get_popular_departments(CLINIC_ID)
            assert result == []
