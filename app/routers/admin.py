"""Admin router for analytics and management — Security Hardened."""

import asyncio
import logging
import re
import secrets
from datetime import date, datetime, time as time_type
from typing import Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
    UploadFile,
    File,
    Form,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings
from app.database import supabase
from app.services.tenant import (
    ALL_FEATURES,
    get_clinic_by_id,
    has_feature,
    invalidate_tenant_cache,
    require_feature,
)
from app.services.analytics import analytics_service
from app.services.lab_reports import LabReportService
from app.services.prescriptions import PrescriptionService
from app.utils.security import login_rate_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic()


class AdminUser(str):
    """Authenticated admin user with RBAC role, clinic scope, and staff user ID."""

    username: str
    role: str
    clinic_id: Optional[str]
    user_id: Optional[str]

    def __new__(
        cls,
        username: str,
        role: str = "clinic_admin",
        clinic_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        obj = super().__new__(cls, username)
        obj.username = username
        obj.role = role
        obj.clinic_id = clinic_id
        obj.user_id = user_id
        return obj

    def can_access_clinic(self, target_clinic_id: str) -> bool:
        """Check if user has permission to access the specified clinic."""
        if self.role == "super_admin":
            return True
        if target_clinic_id == "default":
            return True
        if not self.clinic_id:
            return True
        return str(self.clinic_id) == str(target_clinic_id)


async def log_admin_action(
    user: AdminUser,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> None:
    """Log administrative actions for NABH/DPDP staff identity audit compliance."""
    try:
        def _insert():
            return (
                supabase.table("admin_audit_logs")
                .insert(
                    {
                        "clinic_id": user.clinic_id if user.clinic_id and user.clinic_id != "default" else None,
                        "user_id": user.user_id if user.user_id != "super_admin_env" else None,
                        "username": user.username,
                        "role": user.role,
                        "action": action,
                        "resource_type": resource_type,
                        "resource_id": str(resource_id) if resource_id else None,
                        "details": details or {},
                        "ip_address": ip_address or "unknown",
                    }
                )
                .execute()
            )

        await asyncio.to_thread(_insert)
    except Exception as e:
        logger.error(f"Failed to record admin audit log for action '{action}' by '{user.username}': {e}")


def check_password_hash(plain_password: str, stored_hash: str) -> bool:
    """Check plain password against stored hash (bcrypt or constant-time comparison)."""
    if stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$"):
        try:
            import bcrypt

            return bcrypt.checkpw(
                plain_password.encode("utf-8"), stored_hash.encode("utf-8")
            )
        except Exception:
            pass
    return secrets.compare_digest(
        plain_password.encode("utf-8"), stored_hash.encode("utf-8")
    )


async def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
) -> AdminUser:
    """Verify admin credentials with brute-force protection and tenant isolation.

    Checks the `clinic_admins` table first, then falls back to global environment settings.
    """
    client_ip = request.client.host if request.client else "unknown"

    if login_rate_limiter.is_rate_limited(client_ip):
        remaining_wait = 60
        logger.warning(f"Admin login rate limit exceeded — IP={client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {remaining_wait} seconds.",
            headers={"Retry-After": str(remaining_wait)},
        )

    login_rate_limiter.record_attempt(client_ip)

    # 1. Check database clinic_admins table
    try:
        res = (
            supabase.table("clinic_admins")
            .select("*")
            .eq("username", credentials.username)
            .eq("is_active", True)
            .execute()
        )
        if res.data and len(res.data) > 0:
            user_row = res.data[0]
            if check_password_hash(
                credentials.password, user_row.get("password_hash", "")
            ):
                login_rate_limiter.reset(client_ip)
                return AdminUser(
                    username=user_row["username"],
                    role=user_row.get("role", "clinic_admin"),
                    clinic_id=user_row.get("clinic_id"),
                    user_id=user_row.get("id"),
                )
    except Exception as e:
        logger.warning(f"Database error during admin auth lookup: {e}")

    # 2. Fallback to global env credentials (Super Admin)
    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.admin_username.encode("utf-8"),
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.admin_password.encode("utf-8"),
    )

    if username_ok and password_ok:
        login_rate_limiter.reset(client_ip)
        return AdminUser(
            username=credentials.username,
            role="super_admin",
            clinic_id=None,
            user_id="super_admin_env",
        )

    remaining = login_rate_limiter.remaining_attempts(client_ip)
    logger.warning(
        f"Failed admin login attempt — IP={client_ip}, "
        f"user='{credentials.username}', remaining={remaining}"
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


def enforce_clinic_access(
    user: AdminUser, requested_clinic_id: str = "default"
) -> str:
    """Enforce tenant isolation boundaries.

    Returns effective clinic_id or raises 403 Forbidden if user tries to access a clinic
    outside their authorized scope.
    """
    if isinstance(user, AdminUser):
        if not user.can_access_clinic(requested_clinic_id):
            logger.warning(
                f"Tenant boundary violation attempt: user '{user.username}' (role={user.role}, clinic_id={user.clinic_id}) "
                f"attempted to access clinic_id='{requested_clinic_id}'"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Access to clinic '{requested_clinic_id}' is restricted",
            )
        if requested_clinic_id == "default" and user.clinic_id:
            return user.clinic_id

    return requested_clinic_id


async def resolve_clinic_id_for_write(
    user: AdminUser, requested_clinic_id: str = "default"
) -> str:
    """Resolve a clinic_id for a row that is about to be INSERTed (or otherwise
    written with an equality filter that has no "show everything" fallback).

    "default" is a sentinel meaning "no clinic specified" — the admin frontend
    never sends a real clinic_id, so every write defaulted to the literal
    string "default". That is never an actual clinics.id value. Writing it
    into a row's clinic_id column desyncs that row from every downstream
    query that filters by the real UUID — most importantly the WhatsApp
    bot's get_doctors()/get_available_slots(), which use the clinic resolved
    from the incoming WhatsApp number. A doctor, leave, or holiday written
    with clinic_id='default' becomes permanently invisible to patients even
    though it shows up fine in the admin panel that just created it (the
    admin panel's own list endpoints skip the clinic_id filter entirely when
    it's still "default").
    """
    effective = enforce_clinic_access(user, requested_clinic_id)
    if effective != "default":
        return effective
    clinics = (
        supabase.table("clinics").select("id").order("created_at").limit(1).execute()
    )
    if not clinics.data:
        raise HTTPException(
            status_code=400, detail="No clinic configured. Create a clinic first."
        )
    return clinics.data[0]["id"]


@router.get("/me")
async def get_current_admin(user: AdminUser = Depends(verify_credentials)):
    """Return the caller's identity plus their clinic's plan and resolved
    feature set, so the admin panel frontend can show/hide tabs without
    duplicating the PLAN_FEATURES registry in JS."""
    if user.role == "super_admin" or not user.clinic_id:
        return {
            "username": user.username,
            "role": user.role,
            "clinic_id": user.clinic_id,
            "plan": None,
            "features": None,
        }

    clinic = await get_clinic_by_id(user.clinic_id)
    plan = clinic.get("plan", "soloclinic")
    features = (
        list(ALL_FEATURES)
        if plan == "enterprise"
        else [f for f in ALL_FEATURES if has_feature(clinic, f)]
    )
    return {
        "username": user.username,
        "role": user.role,
        "clinic_id": user.clinic_id,
        "plan": plan,
        "features": features,
    }


class LeaveCreate(BaseModel):
    doctor_name: str
    leave_date: date
    leave_type: str  # full, half_morning, half_evening
    end_date: Optional[date] = None
    reason: Optional[str] = None


class DoctorCreate(BaseModel):
    name: str
    specialization: str
    department: str
    available_days: str = "Mon,Tue,Wed,Thu,Fri"
    morning_slots: list[str] = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]
    evening_slots: list[str] = ["17:00", "17:30", "18:00", "18:30"]
    is_active: bool = True
    consultation_fee: int = 500
    morning_start: Optional[time_type] = None
    morning_end: Optional[time_type] = None
    evening_start: Optional[time_type] = None
    evening_end: Optional[time_type] = None
    slot_duration_minutes: int = 30


class DoctorUpdate(BaseModel):
    name: Optional[str] = None
    specialization: Optional[str] = None
    department: Optional[str] = None
    available_days: Optional[str] = None
    morning_slots: Optional[list[str]] = None
    evening_slots: Optional[list[str]] = None
    is_active: Optional[bool] = None
    consultation_fee: Optional[int] = None
    morning_start: Optional[time_type] = None
    morning_end: Optional[time_type] = None
    evening_start: Optional[time_type] = None
    evening_end: Optional[time_type] = None
    slot_duration_minutes: Optional[int] = None


class PaymentSettingsUpdate(BaseModel):
    """Self-service payment settings a clinic_admin can set for their own
    clinic. Partial update — only fields explicitly sent are changed."""

    razorpay_key_id: Optional[str] = None
    razorpay_key_secret: Optional[str] = None
    razorpay_webhook_secret: Optional[str] = None
    payment_mode: Optional[Literal["full", "partial", "none"]] = None
    payment_deposit_percent: Optional[int] = None

    @field_validator("payment_deposit_percent")
    @classmethod
    def validate_percent_range(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (1 <= v <= 99):
            raise ValueError("payment_deposit_percent must be between 1 and 99")
        return v


class BranchCreate(BaseModel):
    name: str
    short_name: Optional[str] = None
    address: Optional[str] = None
    landmark: Optional[str] = None
    maps_link: Optional[str] = None
    phone: Optional[str] = None
    is_diagnostic: bool = False
    display_order: int = 0


class BranchUpdate(BaseModel):
    name: Optional[str] = None
    short_name: Optional[str] = None
    address: Optional[str] = None
    landmark: Optional[str] = None
    maps_link: Optional[str] = None
    phone: Optional[str] = None
    is_diagnostic: Optional[bool] = None
    is_active: Optional[bool] = None
    display_order: Optional[int] = None


class DoctorBranchAssign(BaseModel):
    doctor_id: str
    session: str = "both"  # morning | evening | both


class PrescriptionCreate(BaseModel):
    patient_phone: str = Field(..., description="Patient phone number")
    patient_name: str = Field(..., min_length=1, description="Patient full name")
    medicine_name: str = Field(..., min_length=1, description="Medicine / drug name")
    dosage: str = Field(..., min_length=1, description="Dosage (e.g. 500mg, 1 tablet)")
    frequency: str = Field(..., min_length=1, description="Frequency (e.g. twice daily)")
    reminder_times: list[str] = Field(
        ...,
        min_length=1,
        description="List of reminder times in HH:MM format (e.g. ['08:00', '20:00'])",
    )
    start_date: date = Field(..., description="Start date of prescription")
    end_date: date = Field(..., description="End date of prescription")
    notes: Optional[str] = Field(None, description="Optional notes/instructions")
    clinic_id: Optional[str] = Field(None, description="Optional clinic ID override")

    @field_validator("reminder_times")
    @classmethod
    def validate_reminder_times(cls, times: list[str]) -> list[str]:
        time_regex = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
        for t in times:
            if not isinstance(t, str) or not time_regex.match(t.strip()):
                raise ValueError(
                    f"Invalid reminder time format: '{t}'. Must be HH:MM format (00:00 to 23:59)."
                )
        return [t.strip() for t in times]

    @model_validator(mode="after")
    def validate_date_range(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date cannot be earlier than start_date")
        return self


@router.get("/stats")
async def get_stats(
    clinic_id: str = "default", days: int = 30, user: AdminUser = Depends(verify_credentials)
):
    """Get dashboard statistics."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    return await analytics_service.get_dashboard_stats(effective_clinic_id, days)


@router.get("/appointments/recent")
async def get_recent_appointments(
    clinic_id: str = "default", limit: int = 20, user: AdminUser = Depends(verify_credentials)
):
    """Get recent appointments."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    return await analytics_service.get_recent_appointments(effective_clinic_id, limit)


@router.get("/appointments/upcoming")
async def get_upcoming_appointments(
    clinic_id: str = "default", days: int = 7, user: AdminUser = Depends(verify_credentials)
):
    """Get upcoming appointments."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    return await analytics_service.get_upcoming_appointments(effective_clinic_id, days)


@router.get("/departments/popular")
async def get_popular_departments(
    clinic_id: str = "default", days: int = 30, user: AdminUser = Depends(verify_credentials)
):
    """Get popular departments."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    return await analytics_service.get_popular_departments(effective_clinic_id, days)


@router.get("/doctors")
async def get_doctors(
    clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)
):
    """Get all doctors."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("doctors").select("*")
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        result = query.execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting doctors: {e}")
        raise HTTPException(status_code=500, detail="Failed to get doctors")


def _apply_slot_config(data: dict) -> dict:
    """Regenerate morning_slots/evening_slots from start/end/duration if provided.

    Mutates and returns `data` in place. Raises HTTPException(422) if a
    shift's end time isn't after its start time.
    """
    from app.utils.helpers import generate_slots
    from datetime import time as time_type

    def _parse_time(val):
        if val is None or isinstance(val, time_type):
            return val
        if isinstance(val, str):
            parts = val.split(":")
            return time_type(int(parts[0]), int(parts[1]))
        return val

    duration = data.get("slot_duration_minutes") or 30

    morning_start = _parse_time(data.get("morning_start"))
    morning_end = _parse_time(data.get("morning_end"))
    if morning_start is not None and morning_end is not None:
        if morning_end <= morning_start:
            raise HTTPException(
                status_code=422, detail="morning_end must be after morning_start"
            )
        data["morning_slots"] = generate_slots(morning_start, morning_end, duration)

    evening_start = _parse_time(data.get("evening_start"))
    evening_end = _parse_time(data.get("evening_end"))
    if evening_start is not None and evening_end is not None:
        if evening_end <= evening_start:
            raise HTTPException(
                status_code=422, detail="evening_end must be after evening_start"
            )
        data["evening_slots"] = generate_slots(evening_start, evening_end, duration)

    return data


@router.post("/doctors")
async def create_doctor(
    doctor: DoctorCreate,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Create a new doctor."""
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
        doctor_data = doctor.dict()
        doctor_data = _apply_slot_config(doctor_data)
        doctor_data["clinic_id"] = effective_clinic_id
        result = supabase.table("doctors").insert(doctor_data).execute()
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating doctor: {e}")
        raise HTTPException(status_code=500, detail="Failed to create doctor")


@router.put("/doctors/{doctor_id}")
async def update_doctor(
    doctor_id: str,
    doctor: DoctorUpdate,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Update an existing doctor."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        update_data = doctor.dict(exclude_unset=True)
        update_data = _apply_slot_config(update_data)
        if not update_data:
            return {"message": "No fields to update"}
        query = supabase.table("doctors").update(update_data)
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        result = query.eq("id", doctor_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Doctor not found")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating doctor: {e}")
        raise HTTPException(status_code=500, detail="Failed to update doctor")


@router.delete("/doctors/{doctor_id}")
async def delete_doctor(
    doctor_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)
):
    """Delete a doctor."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("doctors").delete()
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        query.eq("id", doctor_id).execute()
        # Note: if doctor has appointments, foreign key constraints might fail unless cascading is enabled
        return {"success": True}
    except Exception as e:
        logger.error(f"Error deleting doctor: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete doctor")


@router.get("/leaves")
async def get_leaves(
    doctor: Optional[str] = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Get doctor leaves."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("doctor_leaves").select("*")
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        if doctor:
            query = query.eq("doctor_name", doctor)
        result = query.order("leave_date").execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting leaves: {e}")
        raise HTTPException(status_code=500, detail="Failed to get leaves")


@router.post("/leaves")
async def create_leave(
    leave: LeaveCreate,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Create a doctor leave (single day or date range)."""
    from datetime import timedelta

    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
        start_date = leave.leave_date
        end_date = leave.end_date or start_date

        if end_date < start_date:
            raise HTTPException(
                status_code=400, detail="End date cannot be before start date"
            )

        leaves_to_insert = []
        current_date = start_date

        while current_date <= end_date:
            leave_data = leave.dict(exclude={"end_date"})
            leave_data["leave_date"] = str(current_date)
            leave_data["clinic_id"] = effective_clinic_id
            leaves_to_insert.append(leave_data)
            current_date += timedelta(days=1)

        result = supabase.table("doctor_leaves").insert(leaves_to_insert).execute()

        # Return the first inserted leave just to satisfy the previous API signature somewhat
        # in case the frontend depends on it
        if result.data:
            return result.data[0]
        return {"status": "success", "count": len(leaves_to_insert)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating leave: {e}")
        raise HTTPException(status_code=500, detail="Failed to create leave")


@router.delete("/leaves/{leave_id}")
async def delete_leave(
    leave_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)
):
    """Delete a doctor leave."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("doctor_leaves").delete()
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        query.eq("id", leave_id).execute()
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Error deleting leave: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete leave")


@router.get("/holidays")
async def get_holidays(
    clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)
):
    """Get hospital holidays."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("hospital_holidays").select("*").order("holiday_date")
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        result = query.execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting holidays: {e}")
        raise HTTPException(status_code=500, detail="Failed to get holidays")


@router.post("/holidays")
async def create_holiday(
    holiday_date: date,
    name: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Create a hospital holiday."""
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
        result = (
            supabase.table("hospital_holidays")
            .insert(
                {
                    "clinic_id": effective_clinic_id,
                    "holiday_date": str(holiday_date),
                    "name": name,
                }
            )
            .execute()
        )
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating holiday: {e}")
        raise HTTPException(status_code=500, detail="Failed to create holiday")


@router.delete("/holidays/{holiday_date}")
async def delete_holiday(
    holiday_date: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Delete a hospital holiday."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("hospital_holidays").delete()
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        query.eq("holiday_date", holiday_date).execute()
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Error deleting holiday: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete holiday")


@router.delete("/appointments/{appointment_id}")
async def cancel_appointment_by_admin(
    appointment_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Cancel a confirmed appointment.

    Routes through PaymentService.admin_cancel_confirmed_booking() so a
    Razorpay-paid booking gets refunded and the patient is notified over
    WhatsApp, instead of silently flipping status with no refund and no
    notification (see payment.py for why this matters).
    """
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        from app.services.payment import payment_service

        result = await payment_service.admin_cancel_confirmed_booking(
            appointment_id,
            clinic_id=effective_clinic_id,
            admin_notes=f"Cancelled by admin: {user}",
        )
        if result["success"]:
            return {"success": True}
        return {"success": False, "message": result.get("reason", "Failed")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling appointment {appointment_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════ LAB REPORTS ═══════


@router.post("/lab-reports/upload")
async def upload_lab_report(
    file: UploadFile = File(...),
    patient_phone: str = Form(...),
    patient_name: str = Form(...),
    report_name: str = Form(...),
    report_type: str = Form("General"),
    clinic_id: str = Form("default"),
    user: AdminUser = Depends(verify_credentials),
):
    """Upload and send a lab report to a patient via WhatsApp."""
    try:
        effective_clinic_id = enforce_clinic_access(user, clinic_id)
        file_bytes = await file.read()
        result = await LabReportService().upload_and_send(
            clinic_id=effective_clinic_id,
            file_bytes=file_bytes,
            filename=file.filename,
            content_type=file.content_type or "application/pdf",
            patient_phone=patient_phone,
            patient_name=patient_name,
            report_name=report_name,
            report_type=report_type,
        )
        return {
            "success": True,
            "message": "Report sent to patient via WhatsApp",
            "report": result,
        }
    except Exception as e:
        logger.error(f"Lab report upload error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/lab-reports")
async def get_lab_reports(
    clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)
):
    """Get all lab reports."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    result = await LabReportService().get_all_reports(effective_clinic_id)
    return {"reports": result}


@router.post("/lab-reports/{report_id}/resend")
async def resend_lab_report(
    report_id: str,
    user: str = Depends(verify_credentials),
):
    """Resend a lab report to the patient."""
    try:
        await LabReportService().resend_report(report_id)
        return {"success": True, "message": "Report resent successfully"}
    except Exception as e:
        logger.error(f"Lab report resend error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/patients")
async def get_patients(
    clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)
):
    """Get all patients with appointment counts."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        result = supabase.rpc(
            "get_patients_with_counts",
            {"p_clinic_id": effective_clinic_id},
        ).execute()
        if result.data:
            return {"patients": result.data}
        if effective_clinic_id == "default":
            patients = supabase.table("patients").select("*").order("phone").execute()
        else:
            patients = (
                supabase.table("patients")
                .select("*")
                .eq("clinic_id", effective_clinic_id)
                .order("phone")
                .execute()
            )
        return {"patients": patients.data or []}
    except Exception:
        # Fallback if RPC doesn't exist
        if effective_clinic_id == "default":
            patients = supabase.table("patients").select("*").order("phone").execute()
        else:
            patients = (
                supabase.table("patients")
                .select("*")
                .eq("clinic_id", effective_clinic_id)
                .order("phone")
                .execute()
            )
        return {"patients": patients.data or []}


# ═══════ PRESCRIPTIONS ═══════


@router.post("/prescriptions")
async def add_prescription(
    body: PrescriptionCreate,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Add a new prescription reminder with strict Pydantic input validation."""
    effective_clinic_id = body.clinic_id or clinic_id
    effective_clinic_id = enforce_clinic_access(user, effective_clinic_id)
    try:
        result = await PrescriptionService().add_prescription(
            clinic_id=effective_clinic_id,
            patient_phone=body.patient_phone,
            patient_name=body.patient_name,
            medicine_name=body.medicine_name,
            dosage=body.dosage,
            frequency=body.frequency,
            reminder_times=body.reminder_times,
            start_date=str(body.start_date),
            end_date=str(body.end_date),
            notes=body.notes,
        )
        return {"success": True, "prescription": result}
    except Exception as e:
        logger.error(f"Prescription add error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prescriptions")
async def get_prescriptions(
    active_only: bool = False,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Get all prescriptions."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    result = await PrescriptionService().get_all_prescriptions(
        effective_clinic_id, active_only
    )
    return {"prescriptions": result}


@router.post("/prescriptions/{prescription_id}/deactivate")
async def deactivate_prescription(
    prescription_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Deactivate a prescription reminder."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        await PrescriptionService().deactivate_prescription(
            effective_clinic_id, prescription_id
        )
        return {"success": True}
    except Exception as e:
        logger.error(f"Prescription deactivate error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════ PAYMENTS & BOOKINGS ═══════


@router.get("/bookings")
async def get_bookings(
    clinic_id: str = "default",
    status: Optional[str] = None,
    limit: int = 50,
    user: AdminUser = Depends(verify_credentials),
):
    """Get all bookings with payment information."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("appointments").select(
            "id, clinic_id, patient_phone, patient_name, department, doctor_name, "
            "appointment_date, appointment_time, status, razorpay_order_id, "
            "payment_id, amount_paise, hold_expires_at, booking_ref, created_at, updated_at"
        )
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        if status:
            query = query.eq("status", status)
        result = query.order("created_at", desc=True).limit(limit).execute()
        return {"bookings": result.data or []}
    except Exception as e:
        logger.error(f"Error getting bookings: {e}")
        raise HTTPException(status_code=500, detail="Failed to get bookings")


@router.get("/bookings/pending-review")
async def get_pending_review_bookings(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Get bookings in pending_review status — needs human eyes."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = (
            supabase.table("appointments").select("*").eq("status", "pending_review")
        )
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        result = query.order("created_at", desc=True).execute()
        return {"bookings": result.data or []}
    except Exception as e:
        logger.error(f"Error getting pending review bookings: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to get pending review bookings"
        )


@router.post("/bookings/{booking_id}/confirm")
async def admin_confirm_booking(
    booking_id: str,
    body: dict = None,
    user: str = Depends(verify_credentials),
):
    """Manually confirm a pending_review booking (admin override)."""
    try:
        from app.services.payment import payment_service

        admin_notes = (body or {}).get("admin_notes", f"Confirmed by admin: {user}")
        result = await payment_service.admin_confirm_booking(booking_id, admin_notes)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result.get("reason", "Failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin confirm booking error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bookings/{booking_id}/reject")
async def admin_reject_booking(
    booking_id: str,
    body: dict = None,
    user: str = Depends(verify_credentials),
):
    """Manually reject a pending_review booking + initiate refund."""
    try:
        from app.services.payment import payment_service

        admin_notes = (body or {}).get("admin_notes", f"Rejected by admin: {user}")
        result = await payment_service.admin_reject_booking(booking_id, admin_notes)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result.get("reason", "Failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin reject booking error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bookings/{booking_id}/refund")
async def admin_refund_booking(
    booking_id: str,
    body: dict = None,
    user: str = Depends(verify_credentials),
):
    """Initiate a refund for a confirmed booking."""
    try:
        from app.services.payment import payment_service

        reason = (body or {}).get("reason", f"Admin refund by {user}")
        result = await payment_service.initiate_refund(booking_id, reason)
        if not result["success"]:
            raise HTTPException(
                status_code=400, detail=result.get("reason", "Refund failed")
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin refund error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/payment-events/{booking_id}")
async def get_payment_events(
    booking_id: str,
    user: str = Depends(verify_credentials),
):
    """Get the payment audit trail for a booking."""
    try:
        result = (
            supabase.table("payment_events")
            .select("*")
            .eq("booking_id", booking_id)
            .order("created_at", desc=False)
            .execute()
        )
        return {"events": result.data or []}
    except Exception as e:
        logger.error(f"Error getting payment events: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payment events")


@router.get("/payments/reconciliation")
async def get_payment_reconciliation(
    date_str: Optional[str] = None,
    user: str = Depends(verify_credentials),
):
    """Get daily payment reconciliation summary."""
    try:
        from app.services.payment import payment_service

        summary = await payment_service.get_daily_reconciliation(date_str)
        return summary
    except Exception as e:
        logger.error(f"Reconciliation error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get reconciliation data")


@router.get("/payments/stats")
async def get_payment_stats(
    clinic_id: str = "default",
    days: int = 30,
    user: AdminUser = Depends(verify_credentials),
):
    """Get payment statistics for the dashboard."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        def _scope(query):
            if effective_clinic_id != "default":
                return query.eq("clinic_id", effective_clinic_id)
            return query

        # Total confirmed with payments
        confirmed = _scope(
            supabase.table("appointments")
            .select("id, amount_paise", count="exact")
            .eq("status", "confirmed")
            .not_.is_("payment_id", "null")
            .gte("created_at", cutoff)
        ).execute()

        # Total pending review
        pending = _scope(
            supabase.table("appointments")
            .select("id", count="exact")
            .eq("status", "pending_review")
        ).execute()

        # Total refunded
        refunded = _scope(
            supabase.table("appointments")
            .select("id, amount_paise", count="exact")
            .eq("status", "refunded")
            .gte("created_at", cutoff)
        ).execute()

        # Total expired
        expired = _scope(
            supabase.table("appointments")
            .select("id", count="exact")
            .eq("status", "expired")
            .gte("created_at", cutoff)
        ).execute()

        confirmed_amount = sum(b.get("amount_paise", 0) for b in (confirmed.data or []))
        refunded_amount = sum(b.get("amount_paise", 0) for b in (refunded.data or []))

        # Signature failures (payment_events isn't clinic-scoped directly; left global)
        sig_failures = (
            supabase.table("payment_events")
            .select("id", count="exact")
            .eq("event_type", "signature_failed")
            .gte("created_at", cutoff)
            .execute()
        )

        return {
            "confirmed_count": len(confirmed.data or []),
            "confirmed_amount_rupees": confirmed_amount / 100,
            "pending_review_count": len(pending.data or []),
            "refunded_count": len(refunded.data or []),
            "refunded_amount_rupees": refunded_amount / 100,
            "expired_count": len(expired.data or []),
            "signature_failures": len(sig_failures.data or []),
            "period_days": days,
        }
    except Exception as e:
        logger.error(f"Payment stats error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payment stats")


@router.get("/settings/payment")
async def get_payment_settings(
    clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)
):
    """Return this clinic's payment settings, with secrets masked."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(effective_clinic_id)
    cfg = clinic.get("config") or {}

    def _mask(secret: Optional[str]) -> Optional[str]:
        if not secret:
            return None
        return "•" * max(0, len(secret) - 4) + secret[-4:]

    key_id = cfg.get("razorpay_key_id")
    key_secret = cfg.get("razorpay_key_secret")
    default_mode = "full" if (key_id and key_secret) else "none"

    return {
        "razorpay_key_id": key_id,
        "razorpay_key_secret_masked": _mask(key_secret),
        "razorpay_webhook_secret_masked": _mask(cfg.get("razorpay_webhook_secret")),
        "payment_mode": cfg.get("payment_mode", default_mode),
        "payment_deposit_percent": cfg.get("payment_deposit_percent"),
    }


@router.put("/settings/payment")
async def update_payment_settings(
    body: PaymentSettingsUpdate,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Self-service update of a clinic's own Razorpay keys and payment mode.
    A clinic_admin may only update their own clinic (enforced via
    enforce_clinic_access); diagstream clinics are rejected — they don't
    take bookings, so payments_razorpay isn't in their feature set."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(effective_clinic_id)
    require_feature(clinic, "payments_razorpay")

    cfg = dict(clinic.get("config") or {})
    updates = body.dict(exclude_unset=True)

    for key in ("razorpay_key_id", "razorpay_key_secret", "razorpay_webhook_secret"):
        if key in updates and updates[key] and updates[key].strip():
            cfg[key] = updates[key].strip()

    if "payment_mode" in updates and updates["payment_mode"] is not None:
        cfg["payment_mode"] = updates["payment_mode"]
    if (
        "payment_deposit_percent" in updates
        and updates["payment_deposit_percent"] is not None
    ):
        cfg["payment_deposit_percent"] = updates["payment_deposit_percent"]

    final_mode = cfg.get("payment_mode", "full")
    final_percent = cfg.get("payment_deposit_percent")
    if final_mode == "partial" and not (
        isinstance(final_percent, int) and 1 <= final_percent <= 99
    ):
        raise HTTPException(
            status_code=422,
            detail="payment_deposit_percent (1-99) is required when payment_mode is 'partial'",
        )

    result = (
        supabase.table("clinics")
        .update({"config": cfg})
        .eq("id", effective_clinic_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Clinic not found")

    updated_clinic = result.data[0]
    invalidate_tenant_cache(updated_clinic["whatsapp_number"])

    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=user,
        action="update_payment_settings",
        resource_type="clinic_config",
        resource_id=effective_clinic_id,
        details={
            "payment_mode": cfg.get("payment_mode"),
            "razorpay_configured": bool(cfg.get("razorpay_key_id")),
        },
        ip_address=client_ip,
    )

    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════════
# CONNECTOR MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/connectors", dependencies=[Depends(verify_credentials)])
async def get_connectors(clinic_id: Optional[str] = None):
    """Get all integration connectors with status info."""
    try:
        query = supabase.table("integration_connectors").select("*")
        if clinic_id:
            query = query.eq("clinic_id", clinic_id)
        result = query.order("created_at", desc=True).execute()
        return {"connectors": result.data or []}
    except Exception as e:
        logger.error(f"Failed to get connectors: {e}")
        raise HTTPException(status_code=500, detail="Failed to get connectors")


class ConnectorToggle(BaseModel):
    is_enabled: bool


@router.post(
    "/connectors/{connector_id}/toggle", dependencies=[Depends(verify_credentials)]
)
async def toggle_connector(connector_id: str, body: ConnectorToggle):
    """Toggle a connector ON or OFF. This is the primary kill switch."""
    try:
        result = (
            supabase.table("integration_connectors")
            .update(
                {
                    "is_enabled": body.is_enabled,
                    "updated_at": datetime.now().isoformat(),
                }
            )
            .eq("id", connector_id)
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Connector not found")

        status = "enabled" if body.is_enabled else "disabled"
        logger.info(f"Connector {connector_id} {status}")
        return {"message": f"Connector {status}", "connector": result.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to toggle connector: {e}")
        raise HTTPException(status_code=500, detail="Failed to toggle connector")


@router.get(
    "/connectors/{connector_id}/audit-log", dependencies=[Depends(verify_credentials)]
)
async def get_connector_audit_log(connector_id: str, limit: int = 20):
    """Get recent audit log entries for a connector."""
    try:
        # First get the connector to find its clinic_id and type
        connector = (
            supabase.table("integration_connectors")
            .select("clinic_id, connector_type")
            .eq("id", connector_id)
            .single()
            .execute()
        )

        if not connector.data:
            raise HTTPException(status_code=404, detail="Connector not found")

        logs = (
            supabase.table("connector_audit_log")
            .select("*")
            .eq("clinic_id", connector.data["clinic_id"])
            .eq("connector_type", connector.data["connector_type"])
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )

        return {"audit_log": logs.data or []}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get audit log: {e}")
        raise HTTPException(status_code=500, detail="Failed to get audit log")


@router.get("/connectors/failed-reports")
async def get_connector_failed_reports(
    clinic_id: str = "default",
    unresolved_only: bool = True,
    user: AdminUser = Depends(verify_credentials),
):
    """Get per-report failure tracking records for staff visibility."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("connector_failed_reports").select("*")
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        if unresolved_only:
            query = query.is_("resolved_at", "null")
        result = query.order("last_attempt_at", desc=True).execute()
        return {"failed_reports": result.data or []}
    except Exception as e:
        logger.error(f"Failed to get connector failed reports: {e}")
        raise HTTPException(status_code=500, detail="Failed to get failed reports")


@router.get("/audit-logs")
async def get_admin_audit_logs(
    clinic_id: str = "default",
    limit: int = 50,
    user: AdminUser = Depends(verify_credentials),
):
    """Get administrative staff action audit logs for compliance auditing (NABH / DPDP)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("admin_audit_logs").select("*")
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        result = query.order("created_at", desc=True).limit(limit).execute()
        return {"audit_logs": result.data or []}
    except Exception as e:
        logger.error(f"Failed to get admin audit logs: {e}")
        raise HTTPException(status_code=500, detail="Failed to get admin audit logs")


# ═══════ BRANCHES ═══════


@router.get("/branches")
async def get_branches(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Get all branches for a clinic."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("branches").select("*")
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        query = query.order("display_order")
        result = query.execute()
        return {"branches": result.data or []}
    except Exception as e:
        logger.error(f"Error getting branches: {e}")
        raise HTTPException(status_code=500, detail="Failed to get branches")


@router.post("/branches")
async def create_branch(
    branch: BranchCreate,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Create a new branch."""
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
        branch_data = branch.dict()
        branch_data["clinic_id"] = effective_clinic_id
        result = supabase.table("branches").insert(branch_data).execute()

        # Invalidate branch cache
        from app.services.tenant import invalidate_branch_cache

        invalidate_branch_cache(effective_clinic_id)

        return {"success": True, "branch": result.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating branch: {e}")
        raise HTTPException(status_code=500, detail="Failed to create branch")


@router.put("/branches/{branch_id}")
async def update_branch(
    branch_id: str,
    branch: BranchUpdate,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Update a branch."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        update_data = branch.dict(exclude_unset=True)
        if not update_data:
            return {"message": "No fields to update"}
        query = supabase.table("branches").update(update_data)
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        result = query.eq("id", branch_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Branch not found")

        # Invalidate branch cache
        from app.services.tenant import invalidate_branch_cache

        invalidate_branch_cache(effective_clinic_id)

        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating branch: {e}")
        raise HTTPException(status_code=500, detail="Failed to update branch")


@router.delete("/branches/{branch_id}")
async def delete_branch(
    branch_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Soft-delete a branch (deactivate it)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("branches").update({"is_active": False})
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        query.eq("id", branch_id).execute()

        # Invalidate branch cache
        from app.services.tenant import invalidate_branch_cache

        invalidate_branch_cache(effective_clinic_id)

        return {"success": True}
    except Exception as e:
        logger.error(f"Error deleting branch: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete branch")


@router.get("/branches/{branch_id}/doctors")
async def get_branch_doctors(
    branch_id: str,
    user: str = Depends(verify_credentials),
):
    """Get doctors assigned to a specific branch."""
    try:
        result = (
            supabase.table("doctor_branches")
            .select("*, doctors(*)")
            .eq("branch_id", branch_id)
            .execute()
        )
        return {"doctor_branches": result.data or []}
    except Exception as e:
        logger.error(f"Error getting branch doctors: {e}")
        raise HTTPException(status_code=500, detail="Failed to get branch doctors")


@router.post("/branches/{branch_id}/doctors")
async def assign_doctor_to_branch(
    branch_id: str,
    body: DoctorBranchAssign,
    user: str = Depends(verify_credentials),
):
    """Assign a doctor to a branch with session control."""
    try:
        data = {
            "doctor_id": body.doctor_id,
            "branch_id": branch_id,
            "session": body.session,
        }
        result = supabase.table("doctor_branches").insert(data).execute()
        return result.data[0]
    except Exception as e:
        error_msg = str(e).lower()
        if "duplicate" in error_msg or "unique" in error_msg:
            raise HTTPException(
                status_code=409, detail="Doctor already assigned to this branch"
            )
        logger.error(f"Error assigning doctor to branch: {e}")
        raise HTTPException(status_code=500, detail="Failed to assign doctor to branch")


@router.delete("/branches/{branch_id}/doctors/{doctor_id}")
async def remove_doctor_from_branch(
    branch_id: str,
    doctor_id: str,
    user: str = Depends(verify_credentials),
):
    """Remove a doctor from a branch."""
    try:
        supabase.table("doctor_branches").delete().eq("branch_id", branch_id).eq(
            "doctor_id", doctor_id
        ).execute()
        return {"success": True}
    except Exception as e:
        logger.error(f"Error removing doctor from branch: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to remove doctor from branch"
        )


@router.put("/branches/{branch_id}/doctors/{doctor_id}")
async def update_doctor_branch_session(
    branch_id: str,
    doctor_id: str,
    body: DoctorBranchAssign,
    user: str = Depends(verify_credentials),
):
    """Update a doctor's session assignment at a branch."""
    try:
        result = (
            supabase.table("doctor_branches")
            .update({"session": body.session})
            .eq("branch_id", branch_id)
            .eq("doctor_id", doctor_id)
            .execute()
        )
        if not result.data:
            raise HTTPException(
                status_code=404, detail="Doctor-branch assignment not found"
            )
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating doctor branch session: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to update doctor branch session"
        )
