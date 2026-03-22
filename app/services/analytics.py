"""Analytics service for tracking events and metrics."""

import logging
from datetime import datetime, timedelta
from typing import Optional

from app.database import log_analytics_event, supabase

logger = logging.getLogger(__name__)


class AnalyticsService:
    """Service for analytics and reporting."""

    async def track_event(
        self,
        phone: str,
        event_type: str,
        department: Optional[str] = None,
        intent: Optional[str] = None,
        metadata: Optional[dict] = None
    ) -> bool:
        """Track an analytics event."""
        return await log_analytics_event(
            phone,
            event_type,
            department=department,
            intent=intent,
            metadata=metadata or {}
        )

    async def get_dashboard_stats(self, days: int = 30) -> dict:
        """Get dashboard statistics."""
        try:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

            # Fetch all appointments in period and count in Python
            all_appts = supabase.table("appointments").select("status,department,created_at").gte("created_at", from_date).execute()
            appts = all_appts.data or []

            total_appointments = len(appts)
            confirmed = sum(1 for a in appts if a.get("status") == "confirmed")
            cancelled = sum(1 for a in appts if a.get("status") == "cancelled")
            completed = sum(1 for a in appts if a.get("status") == "completed")
            no_show = sum(1 for a in appts if a.get("status") == "no_show")

            # Department breakdown
            dept_counts = {}
            for a in appts:
                d = a.get("department", "Unknown")
                dept_counts[d] = dept_counts.get(d, 0) + 1
            by_department = sorted(
                [{"department": k, "count": v} for k, v in dept_counts.items()],
                key=lambda x: x["count"], reverse=True
            )

            # Patients
            all_patients = supabase.table("patients").select("created_at").execute()
            patients = all_patients.data or []
            total_patients = len(patients)
            new_patients = sum(1 for p in patients if p.get("created_at", "") >= from_date)

            return {
                "period_days": days,
                "total_appointments": total_appointments,
                "confirmed": confirmed,
                "cancelled": cancelled,
                "completed": completed,
                "no_show": no_show,
                "new_patients": new_patients,
                "total_patients": total_patients,
                "by_department": by_department
            }

        except Exception as e:
            logger.error(f"Error getting dashboard stats: {e}")
            return {
                "period_days": days,
                "total_appointments": 0,
                "confirmed": 0,
                "cancelled": 0,
                "completed": 0,
                "no_show": 0,
                "new_patients": 0,
                "total_patients": 0,
                "by_department": [],
                "error": str(e)
            }

    async def get_recent_appointments(self, limit: int = 20) -> list:
        """Get recent appointments."""
        try:
            result = supabase.table("appointments").select("*").order("created_at", desc=True).limit(limit).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting recent appointments: {e}")
            return []

    async def get_upcoming_appointments(self, days: int = 7) -> list:
        """Get upcoming appointments."""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            future = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

            result = supabase.table("appointments").select("*").gte("appointment_date", today).lte("appointment_date", future).order("appointment_date").execute()
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting upcoming appointments: {e}")
            return []

    async def get_popular_departments(self, days: int = 30) -> list:
        """Get most popular departments."""
        try:
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

            result = supabase.table("appointments").select("department").gte("created_at", from_date).execute()

            dept_counts = {}
            for row in result.data:
                dept = row["department"]
                dept_counts[dept] = dept_counts.get(dept, 0) + 1

            # Sort by count
            sorted_depts = sorted(dept_counts.items(), key=lambda x: x[1], reverse=True)
            return [{"department": d[0], "count": d[1]} for d in sorted_depts]

        except Exception as e:
            logger.error(f"Error getting popular departments: {e}")
            return []


# Global instance
analytics_service = AnalyticsService()
