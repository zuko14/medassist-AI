"""Appointment service for booking and management."""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from app.config import settings
from app.database import (
    get_available_slots, find_next_available_date, book_appointment,
    get_patient_appointments, cancel_appointment, get_doctors, get_doctor_by_name
)

logger = logging.getLogger(__name__)


class AppointmentService:
    """Service for appointment operations."""

    async def get_available_doctors(self, department: Optional[str] = None) -> list:
        """Get list of available doctors."""
        return await get_doctors(department)

    async def check_slot_availability(
        self,
        doctor_name: str,
        appointment_date: str,
        appointment_time: str
    ) -> bool:
        """Check if a specific slot is available."""
        slots, _ = await get_available_slots(doctor_name, appointment_date)
        return appointment_time in slots

    async def get_next_available_slots(
        self,
        doctor_name: str,
        from_date: Optional[str] = None,
        days: int = 7
    ) -> list[dict]:
        """Get next available slots for a doctor."""
        if not from_date:
            from_date = date.today().strftime("%Y-%m-%d")

        available = []
        current = datetime.strptime(from_date, "%Y-%m-%d").date()

        for i in range(days):
            check_date = (current + timedelta(days=i)).strftime("%Y-%m-%d")
            slots, _ = await get_available_slots(doctor_name, check_date)
            if slots:
                available.append({
                    "date": check_date,
                    "slots": slots
                })

        return available

    async def find_alternative_doctors(
        self,
        department: str,
        exclude_doctor: str,
        from_date: Optional[str] = None
    ) -> list[dict]:
        """Find alternative doctors in the same department."""
        if not from_date:
            from_date = date.today().strftime("%Y-%m-%d")

        doctors = await get_doctors(department)
        alternatives = []

        for doc in doctors:
            if doc["name"] == exclude_doctor:
                continue

            # Check if this doctor has availability in next 7 days
            for i in range(7):
                check_date = (datetime.strptime(from_date, "%Y-%m-%d") + timedelta(days=i)).strftime("%Y-%m-%d")
                slots, _ = await get_available_slots(doc["name"], check_date)
                if slots:
                    alternatives.append({
                        "name": doc["name"],
                        "specialization": doc["specialization"],
                        "next_available": check_date,
                        "next_slot": slots[0]
                    })
                    break

        return alternatives

    async def get_appointment_summary(self, appointment_id: str) -> Optional[dict]:
        """Get appointment summary."""
        from app.database import supabase

        try:
            result = supabase.table("appointments").select("*").eq("id", appointment_id).execute()
            if result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error getting appointment: {e}")
            return None

    async def cancel_and_notify(self, appointment_id: str) -> bool:
        """Cancel appointment and notify patient."""
        return await cancel_appointment(appointment_id)

    async def get_upcoming_appointments(
        self,
        phone: str,
        days: int = 7
    ) -> list[dict]:
        """Get upcoming appointments for a patient."""
        from_date = date.today().strftime("%Y-%m-%d")
        to_date = (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")

        appointments = await get_patient_appointments(phone, status="confirmed")

        # Filter by date range
        upcoming = []
        for appt in appointments:
            appt_date = appt["appointment_date"]
            if from_date <= appt_date <= to_date:
                upcoming.append(appt)

        return upcoming

    async def get_appointment_history(
        self,
        phone: str,
        limit: int = 10
    ) -> list[dict]:
        """Get appointment history for a patient."""
        appointments = await get_patient_appointments(phone)
        return appointments[:limit]


# Global instance
appointment_service = AppointmentService()
