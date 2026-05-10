"""Database module for Supabase integration (Multi-Tenant Scoped)."""

import logging
from typing import Optional
from supabase import create_client, Client

from app.config import settings

logger = logging.getLogger(__name__)

# Initialize Supabase client
supabase: Client = create_client(
    settings.supabase_url,
    settings.supabase_service_role_key
)


async def get_patient_by_phone(clinic_id: str, phone: str) -> Optional[dict]:
    """Get patient by phone number and clinic_id."""
    try:
        result = supabase.table("patients").select("*").eq("clinic_id", clinic_id).eq("phone", phone).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting patient: {e}")
        return None


async def create_patient(clinic_id: str, phone: str, name: Optional[str] = None, language: Optional[str] = None) -> dict:
    """Create a new patient."""
    try:
        data = {
            "clinic_id": clinic_id,
            "phone": phone,
            "name": name,
            "language": language,
            "opted_in": True,
            "opted_in_at": "now()",
            "last_seen_at": "now()"
        }
        result = supabase.table("patients").insert(data).execute()
        return result.data[0]
    except Exception as e:
        logger.error(f"Error creating patient: {e}")
        raise


async def update_patient(clinic_id: str, phone: str, updates: dict) -> bool:
    """Update patient data."""
    try:
        supabase.table("patients").update(updates).eq("clinic_id", clinic_id).eq("phone", phone).execute()
        return True
    except Exception as e:
        logger.error(f"Error updating patient: {e}")
        return False


async def get_conversation(clinic_id: str, phone: str) -> Optional[dict]:
    """Get conversation session for phone."""
    try:
        result = supabase.table("conversations").select("*").eq("clinic_id", clinic_id).eq("phone", phone).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting conversation: {e}")
        return None


async def create_conversation(clinic_id: str, phone: str) -> dict:
    """Create a new conversation session."""
    try:
        from datetime import datetime, timedelta, timezone

        data = {
            "clinic_id": clinic_id,
            "phone": phone,
            "state": "idle",
            "context": {},
            "session_expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            "last_message_at": "now()"
        }
        result = supabase.table("conversations").insert(data).execute()
        return result.data[0]
    except Exception as e:
        logger.error(f"Error creating conversation: {e}")
        raise


async def update_conversation(clinic_id: str, phone: str, updates: dict) -> bool:
    """Update conversation session."""
    try:
        updates["updated_at"] = "now()"
        supabase.table("conversations").update(updates).eq("clinic_id", clinic_id).eq("phone", phone).execute()
        return True
    except Exception as e:
        logger.error(f"Error updating conversation: {e}")
        return False


async def get_or_create_conversation(clinic_id: str, phone: str) -> dict:
    """Get existing conversation or create new one."""
    conv = await get_conversation(clinic_id, phone)
    if conv:
        return conv
    return await create_conversation(clinic_id, phone)


async def get_doctors(clinic_id: str, department: Optional[str] = None, active_only: bool = True) -> list:
    """Get doctors, optionally filtered by department."""
    try:
        query = supabase.table("doctors").select("*").eq("clinic_id", clinic_id)

        if department:
            query = query.eq("department", department)
        if active_only:
            query = query.eq("is_active", True)

        result = query.execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting doctors: {e}")
        return []


async def get_doctor_by_name(clinic_id: str, name: str) -> Optional[dict]:
    """Get doctor by name."""
    try:
        result = supabase.table("doctors").select("*").eq("clinic_id", clinic_id).eq("name", name).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting doctor: {e}")
        return None


async def get_available_slots(clinic_id: str, doctor_name: str, date_str: str) -> tuple[list, Optional[str]]:
    """Get available slots for a doctor on a specific date. Returns (slots, reason)."""
    from datetime import datetime, date as dt_date, timedelta

    try:
        # Parse date
        check_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        # Check if it's a hospital holiday
        holiday = supabase.table("hospital_holidays").select("name").eq("clinic_id", clinic_id).eq("holiday_date", date_str).execute()
        if holiday.data:
            return [], "hospital_closed"  # Hospital closed

        # Check doctor leaves
        leave = supabase.table("doctor_leaves").select("leave_type").eq("clinic_id", clinic_id).eq("doctor_name", doctor_name).eq("leave_date", date_str).execute()

        blocked_sessions = []
        if leave.data:
            leave_type = leave.data[0]["leave_type"]
            if leave_type == "full":
                return [], "doctor_on_leave"  # Full day leave
            elif leave_type == "half_morning":
                blocked_sessions = ["morning"]
            elif leave_type == "half_evening":
                blocked_sessions = ["evening"]

        # Get doctor's configured slots
        doc = await get_doctor_by_name(clinic_id, doctor_name)
        if not doc:
            return [], "doctor_not_found"

        day_name = check_date.strftime("%a")
        available_days = doc.get("available_days", "Mon,Tue,Wed,Thu,Fri").split(",")
        if day_name not in available_days:
            return [], "doctor_off_day"  # Doctor doesn't work this day

        # Build slot list
        all_slots = []
        morning_slots = doc.get("morning_slots", ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"])
        evening_slots = doc.get("evening_slots", ["17:00", "17:30", "18:00", "18:30"])

        if "morning" not in blocked_sessions:
            all_slots.extend(morning_slots)
        if "evening" not in blocked_sessions:
            all_slots.extend(evening_slots)

        # Get already booked slots
        booked = supabase.table("appointments").select("appointment_time").eq("clinic_id", clinic_id).eq("doctor_name", doctor_name).eq("appointment_date", date_str).eq("status", "confirmed").execute()
        booked_times = {row["appointment_time"] for row in booked.data}

        # Filter out booked slots
        available = [s for s in all_slots if s not in booked_times]

        # If today, filter out past slots (+30 min buffer)
        if check_date == dt_date.today():
            cutoff = (datetime.now() + timedelta(minutes=30)).strftime("%H:%M")
            available = [s for s in available if s > cutoff]

        return available, None

    except Exception as e:
        logger.error(f"Error getting available slots: {e}")
        return [], "error"


async def find_next_available_date(clinic_id: str, doctor_name: str, from_date_str: str) -> tuple:
    """Find next available date with slots for a doctor."""
    from datetime import datetime, timedelta

    try:
        from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()

        for i in range(14):  # Check up to 14 days
            check_date = from_date + timedelta(days=i)
            check_date_str = check_date.strftime("%Y-%m-%d")

            # Check holiday
            holiday = supabase.table("hospital_holidays").select("name").eq("clinic_id", clinic_id).eq("holiday_date", check_date_str).execute()
            if holiday.data:
                continue

            slots, _ = await get_available_slots(clinic_id, doctor_name, check_date_str)
            if slots:
                return check_date_str, slots, None

        return None, [], "no_availability_14_days"

    except Exception as e:
        logger.error(f"Error finding next available date: {e}")
        return None, [], "error"


async def book_appointment(clinic_id: str, data: dict) -> dict:
    """Book an appointment with race condition protection."""
    try:
        data["clinic_id"] = clinic_id
        
        # Check for existing booking at same slot
        conflict = supabase.table("appointments").select("id").eq("clinic_id", clinic_id).eq("doctor_name", data["doctor_name"]).eq("appointment_date", data["appointment_date"]).eq("appointment_time", data["appointment_time"]).eq("status", "confirmed").execute()

        if conflict.data:
            return {"success": False, "reason": "slot_taken"}

        # Generate booking reference
        from app.utils.helpers import generate_booking_reference
        ref = generate_booking_reference()
        data["booking_ref"] = ref

        # Insert appointment
        result = supabase.table("appointments").insert(data).execute()

        # Update patient visit count
        if data.get("patient_phone"):
            patient = await get_patient_by_phone(clinic_id, data["patient_phone"])
            if patient:
                new_count = (patient.get("visit_count") or 0) + 1
                await update_patient(clinic_id, data["patient_phone"], {"visit_count": new_count})

        return {"success": True, "appointment": result.data[0]}

    except Exception as e:
        logger.error(f"Error booking appointment: {e}")
        # Check if it's a unique constraint violation
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            return {"success": False, "reason": "slot_taken"}
        return {"success": False, "reason": "error"}


async def get_appointment_by_ref(clinic_id: str, booking_ref: str) -> Optional[dict]:
    """Get appointment by booking reference."""
    try:
        result = supabase.table("appointments").select("*").eq("clinic_id", clinic_id).eq("booking_ref", booking_ref).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting appointment: {e}")
        return None


async def cancel_appointment(clinic_id: str, appointment_id: str) -> bool:
    """Cancel an appointment."""
    try:
        result = supabase.table("appointments") \
                         .update({"status": "cancelled"}) \
                         .eq("clinic_id", clinic_id) \
                         .eq("id", appointment_id) \
                         .execute()
        return bool(result.data)
    except Exception as e:
        logger.error(f"Cancel appointment error: {e}")
        return False


async def get_patient_appointments(clinic_id: str, phone: str, status: Optional[str] = None) -> list:
    """Get appointments for a patient."""
    try:
        query = supabase.table("appointments").select("*").eq("clinic_id", clinic_id).eq("patient_phone", phone)
        if status:
            query = query.eq("status", status)
        result = query.order("appointment_date", desc=False).execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting patient appointments: {e}")
        return []


async def log_analytics_event(clinic_id: str, phone: str, event_type: str, **kwargs) -> bool:
    """Log an analytics event."""
    try:
        data = {
            "clinic_id": clinic_id,
            "phone": phone,
            "event_type": event_type,
            **{k: v for k, v in kwargs.items() if v is not None}
        }
        supabase.table("analytics_events").insert(data).execute()
        return True
    except Exception as e:
        logger.error(f"Error logging analytics: {e}")
        return False


async def delete_patient_data(clinic_id: str, phone: str) -> bool:
    """Delete all patient data (DPDP compliance)."""
    try:
        # Get patient ID
        patient = await get_patient_by_phone(clinic_id, phone)
        if patient:
            # Delete appointments
            supabase.table("appointments").delete().eq("clinic_id", clinic_id).eq("patient_id", patient["id"]).execute()
            # Delete patient
            supabase.table("patients").delete().eq("clinic_id", clinic_id).eq("phone", phone).execute()
            # Delete conversation
            supabase.table("conversations").delete().eq("clinic_id", clinic_id).eq("phone", phone).execute()
        return True
    except Exception as e:
        logger.error(f"Error deleting patient data: {e}")
        return False
