"""Database module for Supabase integration (Multi-Tenant Scoped)."""

import asyncio
import logging
import time
from typing import Optional
from supabase import create_client, Client

from app.config import settings

logger = logging.getLogger(__name__)

# TTL Caches for static metadata (5 minutes)
_doctor_cache: dict[str, dict] = {}
_holiday_cache: dict[str, dict] = {}
DOCTOR_CACHE_TTL_SECONDS = 300
HOLIDAY_CACHE_TTL_SECONDS = 300

# Initialize Supabase client
supabase: Client = create_client(
    settings.supabase_url, settings.supabase_service_role_key
)


async def get_patient_by_phone(clinic_id: str, phone: str) -> Optional[dict]:
    """Get patient by phone number and clinic_id."""
    try:
        result = (
            supabase.table("patients")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("phone", phone)
            .execute()
        )
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting patient: {e}")
        return None


async def create_patient(
    clinic_id: str,
    phone: str,
    name: Optional[str] = None,
    language: Optional[str] = None,
) -> dict:
    """Create a new patient."""
    try:
        data = {
            "clinic_id": clinic_id,
            "phone": phone,
            "name": name,
            "language": language,
            "opted_in": True,
            "opted_in_at": "now()",
            "last_seen_at": "now()",
        }
        result = supabase.table("patients").insert(data).execute()
        return result.data[0]
    except Exception as e:
        logger.error(f"Error creating patient: {e}")
        raise


async def update_patient(clinic_id: str, phone: str, updates: dict) -> bool:
    """Update patient data."""
    try:
        supabase.table("patients").update(updates).eq("clinic_id", clinic_id).eq(
            "phone", phone
        ).execute()
        return True
    except Exception as e:
        logger.error(f"Error updating patient: {e}")
        return False


async def get_conversation(clinic_id: str, phone: str) -> Optional[dict]:
    """Get conversation session for phone."""
    try:
        result = (
            supabase.table("conversations")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("phone", phone)
            .execute()
        )
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
            "session_expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).isoformat(),
            "last_message_at": "now()",
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
        supabase.table("conversations").update(updates).eq("clinic_id", clinic_id).eq(
            "phone", phone
        ).execute()
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


async def get_doctors(
    clinic_id: str,
    department: Optional[str] = None,
    active_only: bool = True,
    branch_id: Optional[str] = None,
) -> list:
    """Get doctors, optionally filtered by department and/or branch.

    When branch_id is provided, only returns doctors assigned to that branch
    via the doctor_branches junction table.
    """
    try:
        if branch_id:
            # Branch-scoped: join through doctor_branches
            return await get_doctors_at_branch(
                clinic_id, branch_id, department, active_only
            )

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


async def get_doctors_at_branch(
    clinic_id: str,
    branch_id: str,
    department: Optional[str] = None,
    active_only: bool = True,
) -> list:
    """Get doctors assigned to a specific branch via doctor_branches junction.

    Returns doctor dicts enriched with 'branch_session' field indicating
    which session ('morning', 'evening', 'both') applies at this branch.
    """
    try:
        # Get doctor_ids assigned to this branch
        db_result = (
            supabase.table("doctor_branches")
            .select("doctor_id, session")
            .eq("branch_id", branch_id)
            .execute()
        )

        if not db_result.data:
            return []

        # Build lookup: {doctor_id: session}
        doctor_session_map = {
            row["doctor_id"]: row["session"] for row in db_result.data
        }
        doctor_ids = list(doctor_session_map.keys())

        # Fetch doctor details
        query = (
            supabase.table("doctors")
            .select("*")
            .eq("clinic_id", clinic_id)
            .in_("id", doctor_ids)
        )

        if department:
            query = query.eq("department", department)
        if active_only:
            query = query.eq("is_active", True)

        result = query.execute()
        doctors = result.data or []

        # Enrich with branch_session
        for doc in doctors:
            doc["branch_session"] = doctor_session_map.get(doc["id"], "both")

        return doctors
    except Exception as e:
        logger.error(f"Error getting doctors at branch {branch_id}: {e}")
        return []


async def get_doctor_by_name(clinic_id: str, name: str) -> Optional[dict]:
    """Get doctor by name with TTL cache."""
    cache_key = f"{clinic_id}:{name}"
    cached = _doctor_cache.get(cache_key)
    if cached and (time.time() - cached.get("cached_at", 0) < DOCTOR_CACHE_TTL_SECONDS):
        return cached.get("data")

    try:
        result = (
            supabase.table("doctors")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("name", name)
            .execute()
        )
        if result.data:
            doc = result.data[0]
            _doctor_cache[cache_key] = {"data": doc, "cached_at": time.time()}
            return doc
        return None
    except Exception as e:
        logger.error(f"Error getting doctor: {e}")
        return None


async def get_available_slots(
    clinic_id: str,
    doctor_name: Optional[str] = None,
    date_str: Optional[str] = None,
    branch_id: Optional[str] = None,
    branch_session: Optional[str] = None,
) -> tuple[list, Optional[str]]:
    """Get available slots for a doctor on a specific date using parallel queries & metadata caching.

    Supports 2-arg (doctor_name, date_str) and 3-arg (clinic_id, doctor_name, date_str) calls.
    """
    from datetime import datetime, date as dt_date, timedelta

    if date_str is None:
        date_str = doctor_name
        doctor_name = clinic_id
        clinic_id = "default"

    try:
        check_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        async def _fetch_holiday():
            cache_key = f"{clinic_id}:{date_str}"
            cached = _holiday_cache.get(cache_key)
            if cached and (time.time() - cached.get("cached_at", 0) < HOLIDAY_CACHE_TTL_SECONDS):
                return cached.get("data")

            res = (
                supabase.table("hospital_holidays")
                .select("name")
                .eq("clinic_id", clinic_id)
                .eq("holiday_date", date_str)
                .execute()
            )
            data = res.data or []
            _holiday_cache[cache_key] = {"data": data, "cached_at": time.time()}
            return data

        async def _fetch_leave():
            res = (
                supabase.table("doctor_leaves")
                .select("leave_type")
                .eq("clinic_id", clinic_id)
                .eq("doctor_name", doctor_name)
                .eq("leave_date", date_str)
                .execute()
            )
            return res.data or []

        async def _fetch_booked():
            res = (
                supabase.table("appointments")
                .select("appointment_time")
                .eq("clinic_id", clinic_id)
                .eq("doctor_name", doctor_name)
                .eq("appointment_date", date_str)
                .eq("status", "confirmed")
                .execute()
            )
            return res.data or []

        async def _fetch_doc():
            res = get_doctor_by_name(clinic_id, doctor_name)
            if hasattr(res, "__await__"):
                return await res
            return res

        # Execute non-interdependent queries in parallel via asyncio.gather
        holiday_data, leave_data, doc, booked_data = await asyncio.gather(
            _fetch_holiday(),
            _fetch_leave(),
            _fetch_doc(),
            _fetch_booked(),
        )

        if holiday_data:
            return [], "hospital_closed"

        blocked_sessions = []
        if leave_data:
            leave_type = leave_data[0]["leave_type"]
            if leave_type == "full":
                return [], "doctor_on_leave"
            elif leave_type == "half_morning":
                blocked_sessions = ["morning"]
            elif leave_type == "half_evening":
                blocked_sessions = ["evening"]

        if not doc:
            return [], "doctor_not_found"

        day_name = check_date.strftime("%a")
        available_days = doc.get("available_days", "Mon,Tue,Wed,Thu,Fri").split(",")
        if day_name not in available_days:
            return [], "doctor_off_day"

        raw_morn = doc.get("morning_slots")
        if raw_morn is not None:
            morning_slots = raw_morn
        elif doc.get("morning_start") is None and doc.get("morning_end") is None and "morning_slots" in doc:
            morning_slots = []
        else:
            morning_slots = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]

        raw_eve = doc.get("evening_slots")
        if raw_eve is not None:
            evening_slots = raw_eve
        elif doc.get("evening_start") is None and doc.get("evening_end") is None and "evening_slots" in doc:
            evening_slots = []
        else:
            evening_slots = ["17:00", "17:30", "18:00", "18:30"]

        include_morning = "morning" not in blocked_sessions
        include_evening = "evening" not in blocked_sessions

        if branch_session == "morning":
            include_evening = False
        elif branch_session == "evening":
            include_morning = False

        all_slots = []
        if include_morning:
            all_slots.extend(morning_slots)
        if include_evening:
            all_slots.extend(evening_slots)

        booked_times = {row["appointment_time"] for row in booked_data if "appointment_time" in row}
        available = [s for s in all_slots if s not in booked_times]

        if check_date == dt_date.today():
            cutoff = (datetime.now() + timedelta(minutes=30)).strftime("%H:%M")
            available = [s for s in available if s > cutoff]

        return available, None

    except Exception as e:
        logger.error(f"Error getting available slots: {e}")
        return [], "error"


async def find_next_available_date(
    clinic_id: str, doctor_name: str, from_date_str: str
) -> tuple:
    """Find next available date with slots for a doctor."""
    from datetime import datetime, timedelta

    try:
        from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()

        for i in range(14):  # Check up to 14 days
            check_date = from_date + timedelta(days=i)
            check_date_str = check_date.strftime("%Y-%m-%d")

            # Check holiday
            holiday = (
                supabase.table("hospital_holidays")
                .select("name")
                .eq("clinic_id", clinic_id)
                .eq("holiday_date", check_date_str)
                .execute()
            )
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
        conflict = (
            supabase.table("appointments")
            .select("id")
            .eq("clinic_id", clinic_id)
            .eq("doctor_name", data["doctor_name"])
            .eq("appointment_date", data["appointment_date"])
            .eq("appointment_time", data["appointment_time"])
            .eq("status", "confirmed")
            .execute()
        )

        if conflict.data:
            return {"success": False, "reason": "slot_taken"}

        # Generate booking reference
        from app.utils.helpers import generate_booking_reference

        ref = generate_booking_reference()
        data["booking_ref"] = ref

        # Include branch_id and branch_name if present in data
        # (These are set by the conversation flow when a branch is selected)

        # Insert appointment
        result = supabase.table("appointments").insert(data).execute()

        # Update patient visit count
        if data.get("patient_phone"):
            patient = await get_patient_by_phone(clinic_id, data["patient_phone"])
            if patient:
                new_count = (patient.get("visit_count") or 0) + 1
                await update_patient(
                    clinic_id, data["patient_phone"], {"visit_count": new_count}
                )

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
        result = (
            supabase.table("appointments")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("booking_ref", booking_ref)
            .execute()
        )
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"Error getting appointment: {e}")
        return None


async def cancel_appointment(clinic_id: str, appointment_id: str) -> bool:
    """Cancel an appointment."""
    try:
        result = (
            supabase.table("appointments")
            .update({"status": "cancelled"})
            .eq("clinic_id", clinic_id)
            .eq("id", appointment_id)
            .execute()
        )
        return bool(result.data)
    except Exception as e:
        logger.error(f"Cancel appointment error: {e}")
        return False


async def get_patient_appointments(
    clinic_id: str, phone: str, status: Optional[str] = None
) -> list:
    """Get appointments for a patient."""
    try:
        query = (
            supabase.table("appointments")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("patient_phone", phone)
        )
        if status:
            query = query.eq("status", status)
        result = query.order("appointment_date", desc=False).execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting patient appointments: {e}")
        return []


async def log_analytics_event(
    clinic_id: str, phone: str, event_type: str, **kwargs
) -> bool:
    """Log an analytics event."""
    try:
        data = {
            "clinic_id": clinic_id,
            "phone": phone,
            "event_type": event_type,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        supabase.table("analytics_events").insert(data).execute()
        return True
    except Exception as e:
        logger.error(f"Error logging analytics: {e}")
        return False


async def delete_patient_data(clinic_id: str, phone: str) -> bool:
    """Delete / anonymize patient data (DPDP + NMC compliance).

    Tiered strategy:
      Tier 1 (Clinical records — appointments, lab_reports, prescriptions):
        Anonymized (PII replaced with [REDACTED]), NOT deleted.
        Retained per NMC 7-year mandate for medical audit purposes.

      Tier 2 (Session / chat data — conversations, analytics):
        Fully deleted. These are transient operational records.

    This satisfies both the DPDP Act right to erasure (personal data is gone)
    and the NMC clinical record retention requirement (clinical record exists
    but contains no personally identifiable information).
    """
    try:
        # ── Tier 1: Anonymize clinical records (preserve structure, erase PII) ──
        from app.services.data_retention import data_retention_service

        await data_retention_service.anonymize_clinical_records(clinic_id, phone)

        # ── Tier 2: Delete conversation / session data ──
        supabase.table("conversations").delete().eq("clinic_id", clinic_id).eq(
            "phone", phone
        ).execute()

        # Delete analytics events (operational, not clinical)
        try:
            supabase.table("analytics_events").delete().eq("clinic_id", clinic_id).eq(
                "phone", phone
            ).execute()
        except Exception:
            pass  # Analytics table may not have phone column in all versions

        logger.info(
            f"Data deletion: conversations purged, clinical records anonymized "
            f"for phone={phone[:6]}*** in clinic={clinic_id}"
        )
        return True
    except Exception as e:
        logger.error(f"Error in tiered data deletion: {e}")
        return False


async def check_in_appointment(clinic_id: str, appointment_id: str) -> Optional[dict]:
    """Assign the next sequential token number for this appointment's doctor+date.

    Race-safe: relies on the UNIQUE partial index idx_unique_queue_token
    (migration 021) to reject collisions, and retries with the next number
    on conflict instead of allowing duplicate tokens under concurrent check-ins.
    """
    try:
        appt_result = (
            supabase.table("appointments")
            .select("doctor_name, appointment_date")
            .eq("clinic_id", clinic_id)
            .eq("id", appointment_id)
            .execute()
        )
        if not appt_result.data:
            return None
        doctor_name = appt_result.data[0]["doctor_name"]
        appointment_date = appt_result.data[0]["appointment_date"]

        max_retries = 5
        for attempt in range(max_retries):
            max_result = (
                supabase.table("appointments")
                .select("token_number")
                .eq("clinic_id", clinic_id)
                .eq("doctor_name", doctor_name)
                .eq("appointment_date", appointment_date)
                .order("token_number", desc=True)
                .limit(1)
                .execute()
            )
            current_max = (
                max_result.data[0]["token_number"]
                if max_result.data and max_result.data[0]["token_number"]
                else 0
            )
            next_token = current_max + 1 + attempt

            try:
                result = (
                    supabase.table("appointments")
                    .update({"token_number": next_token, "queue_status": "waiting"})
                    .eq("clinic_id", clinic_id)
                    .eq("id", appointment_id)
                    .execute()
                )
                return result.data[0] if result.data else None
            except Exception as e:
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    logger.info(
                        f"check_in_appointment: token {next_token} collision for "
                        f"{doctor_name}/{appointment_date}, retrying (attempt {attempt + 1})"
                    )
                    continue
                raise

        logger.error(f"check_in_appointment: exhausted retries for {appointment_id}")
        return None

    except Exception as e:
        logger.error(f"Error checking in appointment: {e}")
        return None


async def call_next_patient(clinic_id: str, doctor_name: str, date_str: str) -> Optional[dict]:
    """Mark the current in_consultation patient done, and the next waiting
    patient in_consultation. Race-safe: the claiming UPDATE is conditioned
    on queue_status still being 'waiting', so a concurrent caller that
    already claimed the same row causes a retry instead of a double-serve."""
    try:
        supabase.table("appointments").update({"queue_status": "done"}).eq(
            "clinic_id", clinic_id
        ).eq("doctor_name", doctor_name).eq("appointment_date", date_str).eq(
            "queue_status", "in_consultation"
        ).execute()

        max_retries = 5
        for _ in range(max_retries):
            next_result = (
                supabase.table("appointments")
                .select("*")
                .eq("clinic_id", clinic_id)
                .eq("doctor_name", doctor_name)
                .eq("appointment_date", date_str)
                .eq("queue_status", "waiting")
                .order("token_number")
                .limit(1)
                .execute()
            )
            if not next_result.data:
                return None

            candidate = next_result.data[0]
            claimed = (
                supabase.table("appointments")
                .update({"queue_status": "in_consultation"})
                .eq("clinic_id", clinic_id)
                .eq("id", candidate["id"])
                .eq("queue_status", "waiting")
                .execute()
            )
            if claimed.data:
                return claimed.data[0]

        return None
    except Exception as e:
        logger.error(f"Error calling next patient: {e}")
        return None


async def get_patient_queue_status(clinic_id: str, phone: str, date_str: str) -> Optional[dict]:
    """Look up a patient's queue position for today's appointment."""
    try:
        result = (
            supabase.table("appointments")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("patient_phone", phone)
            .eq("appointment_date", date_str)
            .eq("status", "confirmed")
            .execute()
        )
        if not result.data:
            return None

        appt = result.data[0]
        if not appt.get("token_number"):
            return {"checked_in": False, "doctor_name": appt.get("doctor_name")}

        serving_result = (
            supabase.table("appointments")
            .select("token_number")
            .eq("clinic_id", clinic_id)
            .eq("doctor_name", appt["doctor_name"])
            .eq("appointment_date", date_str)
            .in_("queue_status", ["waiting", "in_consultation"])
            .order("token_number")
            .limit(1)
            .execute()
        )
        currently_serving = (
            serving_result.data[0]["token_number"] if serving_result.data else appt["token_number"]
        )
        patients_ahead = max(0, appt["token_number"] - currently_serving)

        return {
            "checked_in": True,
            "token_number": appt["token_number"],
            "currently_serving": currently_serving,
            "patients_ahead": patients_ahead,
            "doctor_name": appt["doctor_name"],
        }
    except Exception as e:
        logger.error(f"Error getting patient queue status: {e}")
        return None


async def get_family_members(clinic_id: str, primary_phone: str) -> list:
    """Return all family members registered under a primary phone number."""
    try:
        result = (
            supabase.table("family_members")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("primary_phone", primary_phone)
            .order("created_at")
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting family members: {e}")
        return []


async def add_family_member(
    clinic_id: str,
    primary_phone: str,
    full_name: str,
    relationship: Optional[str] = None,
) -> Optional[dict]:
    """Save a family member / dependent profile."""
    try:
        data = {
            "clinic_id": clinic_id,
            "primary_phone": primary_phone,
            "full_name": full_name,
            "relationship": relationship,
        }
        result = supabase.table("family_members").insert(data).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error adding family member: {e}")
        return None
