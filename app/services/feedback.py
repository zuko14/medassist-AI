"""Feedback collection service."""

import logging
from datetime import datetime
from typing import Optional

from app.database import supabase, log_analytics_event

logger = logging.getLogger(__name__)


class FeedbackService:
    """Service for collecting and managing patient feedback."""

    async def collect_feedback(
        self,
        phone: str,
        appointment_id: Optional[str] = None,
        rating: Optional[int] = None,
        feedback_text: Optional[str] = None,
        category: Optional[str] = None
    ) -> dict:
        """Collect feedback from a patient."""
        try:
            data = {
                "phone": phone,
                "appointment_id": appointment_id,
                "rating": rating,
                "feedback_text": feedback_text,
                "category": category,
                "created_at": "now()"
            }

            result = supabase.table("feedback").insert(data).execute()

            # Log analytics event
            await log_analytics_event(phone, "feedback_submitted", metadata={"rating": rating})

            return {"success": True, "feedback_id": result.data[0]["id"]}

        except Exception as e:
            logger.error(f"Error collecting feedback: {e}")
            return {"success": False, "error": str(e)}

    async def get_feedback_stats(self, days: int = 30) -> dict:
        """Get feedback statistics."""
        try:
            from_date = (datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

            # Get all feedback in period
            result = supabase.table("feedback").select("*").gte("created_at", from_date).execute()

            feedbacks = result.data or []

            if not feedbacks:
                return {
                    "total_feedback": 0,
                    "average_rating": 0,
                    "rating_distribution": {}
                }

            # Calculate stats
            ratings = [f["rating"] for f in feedbacks if f.get("rating")]
            avg_rating = sum(ratings) / len(ratings) if ratings else 0

            # Rating distribution
            distribution = {}
            for r in ratings:
                distribution[r] = distribution.get(r, 0) + 1

            return {
                "total_feedback": len(feedbacks),
                "average_rating": round(avg_rating, 2),
                "rating_distribution": distribution
            }

        except Exception as e:
            logger.error(f"Error getting feedback stats: {e}")
            return {"error": str(e)}

    async def get_recent_feedback(self, limit: int = 20) -> list:
        """Get recent feedback entries."""
        try:
            result = supabase.table("feedback").select("*").order("created_at", desc=True).limit(limit).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting recent feedback: {e}")
            return []


# Global instance
feedback_service = FeedbackService()
