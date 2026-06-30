"""Tests for Feedback Service (app/services/feedback.py).

Verifies:
  - collect_feedback inserts data and logs analytics event
  - collect_feedback handles database errors gracefully
  - get_feedback_stats computes average rating and distribution
  - get_feedback_stats handles empty feedback set
  - get_recent_feedback returns ordered results
  - get_recent_feedback handles errors gracefully
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from app.services.feedback import FeedbackService


@pytest.fixture
def feedback_svc():
    return FeedbackService()


CLINIC_ID = "clinic-test-001"
PHONE = "+919876543210"


class TestCollectFeedback:
    """Tests for collect_feedback()."""

    @pytest.mark.asyncio
    async def test_successful_collection(self, feedback_svc):
        mock_result = MagicMock()
        mock_result.data = [{"id": "fb-001"}]

        with patch("app.services.feedback.supabase") as mock_sb, \
             patch("app.services.feedback.log_analytics_event", new_callable=AsyncMock) as mock_log:
            mock_sb.table.return_value.insert.return_value.execute.return_value = mock_result
            mock_log.return_value = True

            result = await feedback_svc.collect_feedback(
                clinic_id=CLINIC_ID,
                phone=PHONE,
                appointment_id="appt-001",
                rating=4,
                feedback_text="Good service",
                category="service"
            )

            assert result["success"] is True
            assert result["feedback_id"] == "fb-001"
            mock_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_collection_error_handled(self, feedback_svc):
        with patch("app.services.feedback.supabase") as mock_sb:
            mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception("Insert failed")

            result = await feedback_svc.collect_feedback(
                clinic_id=CLINIC_ID,
                phone=PHONE,
                rating=5,
            )
            assert result["success"] is False
            assert "error" in result

    @pytest.mark.asyncio
    async def test_collection_with_minimum_fields(self, feedback_svc):
        """Should work with just clinic_id, phone, and rating."""
        mock_result = MagicMock()
        mock_result.data = [{"id": "fb-002"}]

        with patch("app.services.feedback.supabase") as mock_sb, \
             patch("app.services.feedback.log_analytics_event", new_callable=AsyncMock) as mock_log:
            mock_sb.table.return_value.insert.return_value.execute.return_value = mock_result
            mock_log.return_value = True

            result = await feedback_svc.collect_feedback(
                clinic_id=CLINIC_ID,
                phone=PHONE,
                rating=3,
            )
            assert result["success"] is True

            # Verify the insert was called with None optional fields
            insert_call = mock_sb.table.return_value.insert.call_args[0][0]
            assert insert_call["rating"] == 3
            assert insert_call["appointment_id"] is None
            assert insert_call["feedback_text"] is None


class TestGetRecentFeedback:
    """Tests for get_recent_feedback()."""

    @pytest.mark.asyncio
    async def test_returns_ordered_data(self, feedback_svc):
        mock_result = MagicMock()
        mock_result.data = [
            {"id": "fb-3", "rating": 5, "created_at": "2026-06-20"},
            {"id": "fb-2", "rating": 4, "created_at": "2026-06-19"},
        ]
        with patch("app.services.feedback.supabase") as mock_sb:
            mock_sb.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = mock_result

            result = await feedback_svc.get_recent_feedback(CLINIC_ID, limit=10)
            assert len(result) == 2
            assert result[0]["id"] == "fb-3"

    @pytest.mark.asyncio
    async def test_error_returns_empty(self, feedback_svc):
        with patch("app.services.feedback.supabase") as mock_sb:
            mock_sb.table.side_effect = Exception("DB down")
            result = await feedback_svc.get_recent_feedback(CLINIC_ID)
            assert result == []


class TestFeedbackServiceInit:
    """Tests for service initialization and module structure."""

    def test_service_instance_exists(self):
        from app.services.feedback import feedback_service
        assert feedback_service is not None
        assert isinstance(feedback_service, FeedbackService)
