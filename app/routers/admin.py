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
from app.database import (
    supabase,
    check_in_appointment,
    call_next_patient,
    get_patient_queue_status,
)
from app.services.tenant import (
    ALL_FEATURES,
    get_clinic_by_id,
    has_feature,
    invalidate_tenant_cache,
    require_feature,
)
from app.services.analytics import analytics_service
from app.services.broadcast import broadcast_service
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
                        "user_id": user.user_id if user.user_id not in ("super_admin_env", "platform_owner_env") else None,
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
        except (Exception, BaseException):
            return False
    try:
        return secrets.compare_digest(
            plain_password.encode("utf-8"), stored_hash.encode("utf-8")
        )
    except Exception:
        return False


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password with bcrypt for storage in clinic_admins.password_hash."""
    import bcrypt

    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


async def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
) -> AdminUser:
    """Verify admin credentials with brute-force protection and tenant isolation.

    Checks the `clinic_admins` table first, then falls back to global environment settings.
    """
    client_ip = request.client.host if request.client else "unknown"

    if login_rate_limiter.check_and_record(client_ip):
        remaining_wait = 60
        logger.warning(f"Admin login rate limit exceeded — IP={client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {remaining_wait} seconds.",
            headers={"Retry-After": str(remaining_wait)},
        )

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


async def require_admin(user: AdminUser = Depends(verify_credentials)) -> AdminUser:
    """Gate for admin-only actions (finances, settings, roster, integrations).
    'staff' accounts pass login but are limited to front-desk operations
    (appointments, check-in, bookings, patients, lab report handling)."""
    if user.role == "staff":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an admin account. Staff accounts are limited to front-desk operations.",
        )
    return user


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


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


@router.put("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: AdminUser = Depends(verify_credentials),
):
    """Self-service password change for DB-backed clinic_admins accounts."""
    if not user.user_id or user.user_id in ("super_admin_env", "platform_owner_env"):
        raise HTTPException(
            status_code=400,
            detail="This account's password is set via an environment variable and can't be changed here. Contact your platform administrator.",
        )

    res = (
        supabase.table("clinic_admins")
        .select("id, password_hash")
        .eq("id", user.user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Admin account not found")

    if not check_password_hash(body.current_password, res.data[0]["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    supabase.table("clinic_admins").update(
        {"password_hash": hash_password(body.new_password)}
    ).eq("id", user.user_id).execute()

    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=user,
        action="change_password",
        resource_type="clinic_admin",
        resource_id=user.user_id,
        ip_address=client_ip,
    )

    return {"success": True, "message": "Password updated successfully"}


class StaffCreate(BaseModel):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)


@router.get("/staff")
async def list_staff(
    clinic_id: str = "default", user: AdminUser = Depends(require_admin)
):
    """List front-desk staff accounts for this clinic (no password hashes)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    query = supabase.table("clinic_admins").select(
        "id, username, role, is_active, created_at"
    ).eq("role", "staff")
    if effective_clinic_id != "default":
        query = query.eq("clinic_id", effective_clinic_id)
    result = query.order("created_at", desc=True).execute()
    return {"staff": result.data or []}


@router.post("/staff")
async def create_staff(
    body: StaffCreate,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_admin),
):
    """Clinic admin self-service: create a front-desk staff login scoped to
    this clinic, so staff credentials don't require going through the
    platform owner. Role is fixed to 'staff' — clinic admins can't mint
    other clinic_admin/super_admin accounts from here."""
    effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)

    existing = (
        supabase.table("clinic_admins")
        .select("id")
        .eq("username", body.username)
        .execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="Username already exists")

    result = (
        supabase.table("clinic_admins")
        .insert(
            {
                "clinic_id": effective_clinic_id,
                "username": body.username,
                "password_hash": hash_password(body.password),
                "role": "staff",
                "is_active": True,
            }
        )
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create staff account")

    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=user,
        action="create_staff",
        resource_type="clinic_admin",
        resource_id=body.username,
        ip_address=client_ip,
    )
    return {"success": True, "staff": result.data[0]}


@router.put("/staff/{staff_id}/toggle")
async def toggle_staff(
    staff_id: str, request: Request, user: AdminUser = Depends(require_admin)
):
    """Activate/deactivate a staff account — for offboarding without deleting
    their audit trail."""
    res = (
        supabase.table("clinic_admins")
        .select("id, clinic_id, is_active, role")
        .eq("id", staff_id)
        .execute()
    )
    if not res.data or res.data[0]["role"] != "staff":
        raise HTTPException(status_code=404, detail="Staff account not found")
    enforce_clinic_access(user, res.data[0]["clinic_id"])

    new_status = not res.data[0]["is_active"]
    supabase.table("clinic_admins").update({"is_active": new_status}).eq(
        "id", staff_id
    ).execute()

    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=user,
        action="toggle_staff",
        resource_type="clinic_admin",
        resource_id=staff_id,
        details={"is_active": new_status},
        ip_address=client_ip,
    )
    return {"success": True, "is_active": new_status}


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
    morning_slots: Optional[list[str]] = None
    evening_slots: Optional[list[str]] = None
    is_active: bool = True
    consultation_fee: int = 500
    morning_start: Optional[time_type] = None
    morning_end: Optional[time_type] = None
    evening_start: Optional[time_type] = None
    evening_end: Optional[time_type] = None
    slot_duration_minutes: int = 30
    branch_id: Optional[str] = None
    branch_session: str = "both"


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
    branch_id: Optional[str] = None
    branch_session: Optional[str] = None


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


class ClinicProfileUpdate(BaseModel):
    """Self-service profile a clinic_admin can set for their own clinic —
    the things every clinic must provide: the name shown to patients in
    WhatsApp, the address and Google Maps link sent in chat, and the
    emergency desk phone number shown during emergency escalation."""

    name: Optional[str] = None
    hospital_address: Optional[str] = None
    hospital_maps_link: Optional[str] = None
    hospital_emergency_number: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("name cannot be blank")
        return v.strip() if v else v


class BranchCreate(BaseModel):
    name: Optional[str] = None          # Auto-generated as "{ClinicName} - {short_name}" if omitted
    short_name: str                     # Locality name: "Madhurwada", "Kancharpalem", "Gajuwaka"
    address: Optional[str] = None
    landmark: Optional[str] = None
    maps_link: Optional[str] = None
    phone: Optional[str] = None
    is_diagnostic: bool = False
    display_order: int = 0

    @field_validator("short_name")
    @classmethod
    def validate_short_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Branch locality / short name is required (e.g. Madhurwada, Gajuwaka)")
        return v.strip()


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


class ConnectorCredentialsUpdate(BaseModel):
    """Self-service MocDoc/HMIS connector credentials a clinic_admin can set
    for their own clinic (or one of their own branches). Partial update —
    an empty/omitted password never overwrites the stored one."""

    connector_type: str = "mocdoc"
    branch_id: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    base_url: Optional[str] = None
    clinic_slug: Optional[str] = None
    admin_alert_phone: Optional[str] = None
    poll_interval_minutes: Optional[int] = None
    is_enabled: Optional[bool] = None


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
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Get all doctors, optionally filtered by branch_id.

    Response enriched with branch assignment info from doctor_branches."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("doctors").select("*")
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        result = query.execute()
        doctors = result.data or []

        # Enrich each doctor with their branch assignments
        if doctors:
            doctor_ids = [d["id"] for d in doctors]
            db_result = (
                supabase.table("doctor_branches")
                .select("doctor_id, branch_id, session, branches(id, name, is_active)")
                .in_("doctor_id", doctor_ids)
                .execute()
            )
            # Build lookup: {doctor_id: [{branch_id, branch_name, session}, ...]}
            branch_map: dict[str, list[dict]] = {}
            for row in (db_result.data or []):
                did = row["doctor_id"]
                branch_info = row.get("branches") or {}
                branch_map.setdefault(did, []).append({
                    "branch_id": row["branch_id"],
                    "branch_name": branch_info.get("name", ""),
                    "session": row["session"],
                })
            for doc in doctors:
                assignments = branch_map.get(doc["id"], [])
                doc["branches"] = assignments
                # Primary branch = first assignment (for simple display)
                if assignments:
                    doc["branch_name"] = assignments[0]["branch_name"]
                    doc["branch_id"] = assignments[0]["branch_id"]
                else:
                    doc["branch_name"] = None
                    doc["branch_id"] = None

        # Optional: filter to a specific branch
        if branch_id:
            doctors = [d for d in doctors if any(
                b["branch_id"] == branch_id for b in d.get("branches", [])
            )]

        return doctors
    except Exception as e:
        logger.error(f"Error getting doctors: {e}")
        raise HTTPException(status_code=500, detail="Failed to get doctors")


def _friendly_db_error(e: Exception, default: str) -> str:
    """Turn a raw Supabase/Postgres error into an actionable admin-facing
    message instead of a generic 500, without leaking table/column internals.

    The doctor-add flow kept surfacing an opaque "Failed to create doctor"
    with no way to tell duplicate name vs. broken clinic linkage vs. bad
    input apart, from either the UI or (without shell/DB access) the logs —
    so the same-looking failure could have three different real causes.
    """
    msg = str(e).lower()
    if "duplicate" in msg or "unique" in msg:
        return "A doctor with this name already exists."
    if "foreign key" in msg:
        return "This clinic account isn't linked to a valid clinic. Contact support."
    if "check constraint" in msg or "violates check" in msg:
        return "One of the values entered isn't valid (check department/fee/hours)."
    if "value too long" in msg:
        return "One of the fields is too long — please shorten it."
    return default


def _apply_slot_config(data: dict) -> dict:
    """Regenerate morning_slots/evening_slots from start/end/duration if provided.

    Mutates and returns `data` in place. Raises HTTPException(422) if shift timing
    is invalid or if both morning and evening shifts are completely disabled.
    """
    from app.utils.helpers import generate_slots
    from datetime import time as time_type

    def _parse_time(val):
        if val is None or isinstance(val, time_type):
            return val
        if isinstance(val, str):
            val = val.strip()
            if not val or val == "00:00":
                return None
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
    elif morning_start is not None or morning_end is not None:
        raise HTTPException(
            status_code=422, detail="Both morning_start and morning_end must be provided for morning shift"
        )
    else:
        if "morning_start" in data or "morning_end" in data or data.get("morning_slots") is None:
            data["morning_slots"] = []
            data["morning_start"] = None
            data["morning_end"] = None

    evening_start = _parse_time(data.get("evening_start"))
    evening_end = _parse_time(data.get("evening_end"))
    if evening_start is not None and evening_end is not None:
        if evening_end <= evening_start:
            raise HTTPException(
                status_code=422, detail="evening_end must be after evening_start"
            )
        data["evening_slots"] = generate_slots(evening_start, evening_end, duration)
    elif evening_start is not None or evening_end is not None:
        raise HTTPException(
            status_code=422, detail="Both evening_start and evening_end must be provided for evening shift"
        )
    else:
        if "evening_start" in data or "evening_end" in data or data.get("evening_slots") is None:
            data["evening_slots"] = []
            data["evening_start"] = None
            data["evening_end"] = None

    has_morning = bool(data.get("morning_slots"))
    has_evening = bool(data.get("evening_slots"))
    touching_shifts = any(
        k in data for k in ("morning_start", "morning_end", "evening_start", "evening_end", "morning_slots", "evening_slots")
    )
    if touching_shifts and not has_morning and not has_evening:
        raise HTTPException(
            status_code=422, detail="At least one shift (morning or evening) must be enabled"
        )

    for key in ("morning_start", "morning_end", "evening_start", "evening_end"):
        if isinstance(data.get(key), time_type):
            data[key] = data[key].isoformat()

    return data



@router.post("/doctors")
async def create_doctor(
    doctor: DoctorCreate,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_admin),
):
    """Create a new doctor and optionally assign to a branch."""
    effective_clinic_id = None
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)

        # Extract branch fields before building the doctors-table payload
        requested_branch_id = doctor.branch_id
        requested_branch_session = doctor.branch_session or "both"

        try:
            doctor_data = doctor.model_dump(exclude={"branch_id", "branch_session"})
        except AttributeError:
            doctor_data = doctor.dict()
            doctor_data.pop("branch_id", None)
            doctor_data.pop("branch_session", None)

        doctor_data = _apply_slot_config(doctor_data)
        doctor_data["clinic_id"] = effective_clinic_id
        result = supabase.table("doctors").insert(doctor_data).execute()
        new_doctor = result.data[0]

        # ── Branch assignment ──
        # Determine which branch_id to use:
        #   1. Explicit branch_id from the form
        #   2. Auto-select if clinic has exactly 1 branch
        branch_id_to_assign = requested_branch_id

        if not branch_id_to_assign:
            # Auto-select single branch for single-branch clinics
            branches_result = (
                supabase.table("branches")
                .select("id")
                .eq("clinic_id", effective_clinic_id)
                .eq("is_active", True)
                .execute()
            )
            clinic_branches = branches_result.data or []
            if len(clinic_branches) == 1:
                branch_id_to_assign = clinic_branches[0]["id"]

        if branch_id_to_assign:
            # IDOR check: verify branch belongs to this clinic
            branch_check = (
                supabase.table("branches")
                .select("id")
                .eq("id", branch_id_to_assign)
                .eq("clinic_id", effective_clinic_id)
                .execute()
            )
            if not branch_check.data:
                logger.warning(
                    f"IDOR attempt: branch_id={branch_id_to_assign} does not belong "
                    f"to clinic_id={effective_clinic_id}"
                )
                raise HTTPException(
                    status_code=400,
                    detail="Selected branch does not belong to your clinic.",
                )

            # Create junction record
            try:
                supabase.table("doctor_branches").insert({
                    "doctor_id": new_doctor["id"],
                    "branch_id": branch_id_to_assign,
                    "session": requested_branch_session,
                }).execute()
                new_doctor["branch_id"] = branch_id_to_assign
                new_doctor["branch_session"] = requested_branch_session
            except Exception as be:
                # Non-fatal: doctor created, branch assignment failed
                logger.warning(
                    f"Doctor {new_doctor['id']} created but branch assignment failed: {be}"
                )

        return new_doctor
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error creating doctor for clinic_id={effective_clinic_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail=_friendly_db_error(e, "Failed to create doctor")
        )


@router.put("/doctors/{doctor_id}")
async def update_doctor(
    doctor_id: str,
    doctor: DoctorUpdate,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_admin),
):
    """Update an existing doctor, optionally changing branch assignment."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        # Extract branch fields before building the doctors-table payload
        requested_branch_id = doctor.branch_id
        requested_branch_session = doctor.branch_session

        try:
            update_data = doctor.model_dump(exclude_unset=True, exclude={"branch_id", "branch_session"})
        except AttributeError:
            update_data = doctor.dict(exclude_unset=True)
            update_data.pop("branch_id", None)
            update_data.pop("branch_session", None)

        update_data = _apply_slot_config(update_data)
        if not update_data and requested_branch_id is None:
            return {"message": "No fields to update"}

        updated_doctor = None
        if update_data:
            query = supabase.table("doctors").update(update_data)
            if effective_clinic_id != "default":
                query = query.eq("clinic_id", effective_clinic_id)
            result = query.eq("id", doctor_id).execute()
            if not result.data:
                raise HTTPException(status_code=404, detail="Doctor not found")
            updated_doctor = result.data[0]
        else:
            # Only branch change, fetch the doctor for response
            result = supabase.table("doctors").select("*").eq("id", doctor_id).execute()
            if not result.data:
                raise HTTPException(status_code=404, detail="Doctor not found")
            updated_doctor = result.data[0]

        # ── Branch assignment upsert ──
        if requested_branch_id is not None:
            # Resolve effective clinic_id for IDOR check
            doc_clinic_id = updated_doctor.get("clinic_id") or effective_clinic_id

            # IDOR check: verify branch belongs to the doctor's clinic
            branch_check = (
                supabase.table("branches")
                .select("id")
                .eq("id", requested_branch_id)
                .eq("clinic_id", doc_clinic_id)
                .execute()
            )
            if not branch_check.data:
                raise HTTPException(
                    status_code=400,
                    detail="Selected branch does not belong to your clinic.",
                )

            session_val = requested_branch_session or "both"

            # Remove existing assignments and add the new one
            # (for single-branch dropdown; multi-branch uses the /branches/{id}/doctors endpoint)
            supabase.table("doctor_branches").delete().eq("doctor_id", doctor_id).execute()
            supabase.table("doctor_branches").insert({
                "doctor_id": doctor_id,
                "branch_id": requested_branch_id,
                "session": session_val,
            }).execute()
            updated_doctor["branch_id"] = requested_branch_id
            updated_doctor["branch_session"] = session_val

        return updated_doctor
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error updating doctor {doctor_id} for clinic_id={effective_clinic_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail=_friendly_db_error(e, "Failed to update doctor")
        )


@router.delete("/doctors/{doctor_id}")
async def delete_doctor(
    doctor_id: str, clinic_id: str = "default", user: AdminUser = Depends(require_admin)
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
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
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
    leave_id: str, clinic_id: str = "default", user: AdminUser = Depends(require_admin)
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
    clinic_id: str = "default", user: AdminUser = Depends(require_admin)
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
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
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


@router.post("/appointments/{appointment_id}/check-in")
async def check_in_appointment_endpoint(
    appointment_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Assign the next OPD token number to an arriving patient."""
    effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
    result = await check_in_appointment(effective_clinic_id, appointment_id)
    if not result:
        raise HTTPException(
            status_code=404, detail="Appointment not found or check-in failed"
        )
    await log_admin_action(
        user=user,
        action="PATIENT_CHECK_IN",
        resource_type="appointment",
        resource_id=appointment_id,
        details={"token_number": result.get("token_number")},
    )
    await _notify_patient_checked_in(effective_clinic_id, result)
    return result


async def _notify_patient_checked_in(clinic_id: str, appointment: dict) -> None:
    """Push the assigned OPD token to the patient over WhatsApp immediately
    on check-in, instead of relying on them to text 'queue status' to find out."""
    try:
        from app.services.whatsapp import whatsapp_service
        from app.templates.whatsapp_templates import get_message

        phone = appointment.get("patient_phone")
        if not phone:
            return
        today_str = datetime.now().strftime("%Y-%m-%d")
        status = await get_patient_queue_status(clinic_id, phone, today_str)
        if not status or not status.get("checked_in"):
            return
        clinic = await get_clinic_by_id(clinic_id)
        msg = get_message(
            "queue_status_waiting",
            "en",
            token=status["token_number"],
            doctor=status["doctor_name"],
            current=status["currently_serving"],
            ahead=status["patients_ahead"],
        )
        await whatsapp_service.send_text(clinic, phone, msg, _source="admin")
    except Exception as e:
        logger.error(f"Failed to send check-in token notification: {e}")


@router.post("/doctors/{doctor_name}/queue/call-next")
async def call_next_patient_endpoint(
    doctor_name: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Mark current in_consultation done, and advance the next waiting patient."""
    effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
    today_str = datetime.now().strftime("%Y-%m-%d")
    result = await call_next_patient(effective_clinic_id, doctor_name, today_str)
    if not result:
        return {"message": "No patients waiting in queue"}
    await log_admin_action(
        user=user,
        action="QUEUE_CALL_NEXT",
        resource_type="appointment",
        resource_id=result.get("id"),
        details={
            "doctor_name": doctor_name,
            "token_number": result.get("token_number"),
        },
    )
    await _notify_patient_its_turn(result)
    return result


async def _notify_patient_its_turn(appointment: dict) -> None:
    """Tell the newly-advanced patient over WhatsApp that it's their turn now,
    mirroring _notify_patient_checked_in — the queue result row already has
    everything needed (patient_phone, doctor_name, token_number), so no extra
    DB round-trip is required."""
    try:
        from app.services.whatsapp import whatsapp_service
        from app.templates.whatsapp_templates import get_message

        phone = appointment.get("patient_phone")
        if not phone:
            return
        clinic = await get_clinic_by_id(appointment.get("clinic_id"))
        msg = get_message(
            "queue_your_turn",
            "en",
            token=appointment.get("token_number"),
            doctor=appointment.get("doctor_name"),
        )
        await whatsapp_service.send_text(clinic, phone, msg, _source="admin")
    except Exception as e:
        logger.error(f"Failed to send call-next turn notification: {e}")


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
    user: AdminUser = Depends(require_admin),
):
    """Add a new prescription reminder with strict Pydantic input validation."""
    effective_clinic_id = body.clinic_id or clinic_id
    effective_clinic_id = enforce_clinic_access(user, effective_clinic_id)
    clinic = await get_clinic_by_id(effective_clinic_id)
    require_feature(clinic, "booking")
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
    clinic = await get_clinic_by_id(effective_clinic_id)
    require_feature(clinic, "booking")
    result = await PrescriptionService().get_all_prescriptions(
        effective_clinic_id, active_only
    )
    return {"prescriptions": result}


@router.post("/prescriptions/{prescription_id}/deactivate")
async def deactivate_prescription(
    prescription_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_admin),
):
    """Deactivate a prescription reminder."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(effective_clinic_id)
    require_feature(clinic, "booking")
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
    clinic_id: str = "default",
    body: dict = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Manually confirm a pending_review booking (admin override)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        from app.services.payment import payment_service

        admin_notes = (body or {}).get("admin_notes", f"Confirmed by admin: {user}")
        result = await payment_service.admin_confirm_booking(
            booking_id, clinic_id=effective_clinic_id, admin_notes=admin_notes
        )
        if not result["success"]:
            status_code = 404 if result.get("reason") == "booking_not_found" else 400
            raise HTTPException(status_code=status_code, detail=result.get("reason", "Failed"))
        await log_admin_action(
            user=user,
            action="BOOKING_MANUAL_CONFIRM",
            resource_type="appointment",
            resource_id=booking_id,
            details={"admin_notes": admin_notes},
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin confirm booking error: {e}")
        raise HTTPException(status_code=500, detail="Internal error")


@router.post("/bookings/{booking_id}/reject")
async def admin_reject_booking(
    booking_id: str,
    clinic_id: str = "default",
    body: dict = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Manually reject a pending_review booking + initiate refund."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        from app.services.payment import payment_service

        admin_notes = (body or {}).get("admin_notes", f"Rejected by admin: {user}")
        result = await payment_service.admin_reject_booking(
            booking_id, clinic_id=effective_clinic_id, admin_notes=admin_notes
        )
        if not result["success"]:
            status_code = 404 if result.get("reason") == "booking_not_found" else 400
            raise HTTPException(status_code=status_code, detail=result.get("reason", "Failed"))
        await log_admin_action(
            user=user,
            action="BOOKING_MANUAL_REJECT",
            resource_type="appointment",
            resource_id=booking_id,
            details={"admin_notes": admin_notes},
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin reject booking error: {e}")
        raise HTTPException(status_code=500, detail="Internal error")


@router.post("/bookings/{booking_id}/refund")
async def admin_refund_booking(
    booking_id: str,
    body: dict = None,
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
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


@router.get("/profile")
async def get_clinic_profile(
    clinic_id: str = "default", user: AdminUser = Depends(require_admin)
):
    """Return the things every clinic must self-provide: the bot's display
    name, hospital address, Google Maps link, and emergency desk phone
    (all shared with patients in chat). Everything else is backend/platform
    configured."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(effective_clinic_id)
    cfg = clinic.get("config") or {}
    return {
        "name": clinic.get("name") or settings.hospital_name,
        "hospital_address": cfg.get("address") or settings.hospital_address,
        "hospital_maps_link": cfg.get("maps_link") or settings.hospital_maps_link,
        "hospital_emergency_number": cfg.get("emergency_number") or settings.hospital_emergency_number,
    }


@router.put("/profile")
async def update_clinic_profile(
    body: ClinicProfileUpdate,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_admin),
):
    """Self-service update of a clinic's own display name, address, and
    Google Maps link. A clinic_admin may only update their own clinic
    (enforced via enforce_clinic_access)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(effective_clinic_id)

    cfg = dict(clinic.get("config") or {})
    updates = (
        body.model_dump(exclude_unset=True)
        if hasattr(body, "model_dump")
        else body.dict(exclude_unset=True)
    )

    row_updates: dict = {}
    if "name" in updates and updates["name"]:
        row_updates["name"] = updates["name"]
    if "hospital_address" in updates and updates["hospital_address"] is not None:
        cfg["address"] = updates["hospital_address"].strip()
    if "hospital_maps_link" in updates and updates["hospital_maps_link"] is not None:
        cfg["maps_link"] = updates["hospital_maps_link"].strip()
    if "hospital_emergency_number" in updates and updates["hospital_emergency_number"] is not None:
        cfg["emergency_number"] = updates["hospital_emergency_number"].strip()
    row_updates["config"] = cfg

    target_clinic_id = clinic.get("id")
    if not target_clinic_id or target_clinic_id == "default":
        db_clinics = (
            supabase.table("clinics")
            .select("*")
            .order("created_at")
            .limit(1)
            .execute()
        )
        if db_clinics.data:
            target_clinic_id = db_clinics.data[0]["id"]
            result = (
                supabase.table("clinics")
                .update(row_updates)
                .eq("id", target_clinic_id)
                .execute()
            )
            if not result.data:
                raise HTTPException(status_code=404, detail="Clinic not found")
            updated_clinic = result.data[0]
        else:
            insert_res = (
                supabase.table("clinics")
                .insert({
                    "name": row_updates.get("name") or settings.hospital_name,
                    "whatsapp_number": settings.hospital_phone,
                    "plan": "enterprise",
                    "config": cfg,
                })
                .execute()
            )
            if not insert_res.data:
                raise HTTPException(
                    status_code=500, detail="Failed to initialize default clinic settings"
                )
            updated_clinic = insert_res.data[0]
            target_clinic_id = updated_clinic["id"]
    else:
        result = (
            supabase.table("clinics")
            .update(row_updates)
            .eq("id", target_clinic_id)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Clinic not found")
        updated_clinic = result.data[0]

    invalidate_tenant_cache(updated_clinic.get("whatsapp_number"))

    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=user,
        action="update_clinic_profile",
        resource_type="clinic_config",
        resource_id=target_clinic_id,
        details={
            "name": updated_clinic.get("name"),
            "address_set": bool(cfg.get("address")),
            "maps_link_set": bool(cfg.get("maps_link")),
            "emergency_number_set": bool(cfg.get("emergency_number")),
        },
        ip_address=client_ip,
    )

    return {"success": True}


@router.get("/settings/payment")
async def get_payment_settings(
    clinic_id: str = "default", user: AdminUser = Depends(require_admin)
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
    user: AdminUser = Depends(require_admin),
):
    """Self-service update of a clinic's own Razorpay keys and payment mode.
    A clinic_admin may only update their own clinic (enforced via
    enforce_clinic_access); diagstream clinics are rejected — they don't
    take bookings, so payments_razorpay isn't in their feature set."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(effective_clinic_id)
    require_feature(clinic, "payments_razorpay")

    cfg = dict(clinic.get("config") or {})
    updates = (
        body.model_dump(exclude_unset=True)
        if hasattr(body, "model_dump")
        else body.dict(exclude_unset=True)
    )

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

    target_clinic_id = clinic.get("id")
    if not target_clinic_id or target_clinic_id == "default":
        # Check if any clinic exists in DB
        db_clinics = (
            supabase.table("clinics")
            .select("*")
            .order("created_at")
            .limit(1)
            .execute()
        )
        if db_clinics.data:
            target_clinic_id = db_clinics.data[0]["id"]
            result = (
                supabase.table("clinics")
                .update({"config": cfg})
                .eq("id", target_clinic_id)
                .execute()
            )
            if not result.data:
                raise HTTPException(status_code=404, detail="Clinic not found")
            updated_clinic = result.data[0]
        else:
            # First-time setup: initialize default clinic record
            insert_res = (
                supabase.table("clinics")
                .insert({
                    "name": settings.hospital_name,
                    "whatsapp_number": settings.hospital_phone,
                    "plan": "enterprise",
                    "config": cfg,
                })
                .execute()
            )
            if not insert_res.data:
                raise HTTPException(
                    status_code=500, detail="Failed to initialize default clinic settings"
                )
            updated_clinic = insert_res.data[0]
            target_clinic_id = updated_clinic["id"]
    else:
        result = (
            supabase.table("clinics")
            .update({"config": cfg})
            .eq("id", target_clinic_id)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Clinic not found")
        updated_clinic = result.data[0]

    invalidate_tenant_cache(updated_clinic.get("whatsapp_number"))

    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=user,
        action="update_payment_settings",
        resource_type="clinic_config",
        resource_id=target_clinic_id,
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


def _mask_connector(row: dict) -> dict:
    """Strip/mask credential fields before returning a connector row to the
    admin panel — the raw encrypted password blob must never round-trip
    over the wire, and the username is shown only partially masked."""
    row = dict(row)
    cfg = dict(row.get("config") or {})
    username = cfg.get("username")
    cfg["username_masked"] = (
        (username[:2] + "•" * max(0, len(username) - 2)) if username else None
    )
    cfg["password_set"] = bool(cfg.get("password_encrypted") or cfg.get("password"))
    cfg.pop("username", None)
    cfg.pop("password", None)
    cfg.pop("password_encrypted", None)
    row["config"] = cfg
    return row


@router.get("/connectors")
async def get_connectors(
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    user: AdminUser = Depends(require_admin),
):
    """Get all integration connectors with status info (credentials masked)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("integration_connectors").select("*")
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        if branch_id:
            query = query.eq("branch_id", branch_id)
        result = query.order("created_at", desc=True).execute()
        connectors = [_mask_connector(row) for row in (result.data or [])]
        return {"connectors": connectors}
    except Exception as e:
        logger.error(f"Failed to get connectors: {e}")
        raise HTTPException(status_code=500, detail="Failed to get connectors")


class ConnectorToggle(BaseModel):
    is_enabled: bool


@router.put("/connectors")
async def upsert_connector_credentials(
    body: ConnectorCredentialsUpdate,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_admin),
):
    """Self-service create/update of a clinic's (or one of its branches')
    MocDoc connector credentials. A clinic_admin may only write their own
    clinic's connector; diagnostic-report capability (`lab_reports`) is
    required — this is not exposed to clinics that don't take lab reports."""
    effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
    clinic = await get_clinic_by_id(effective_clinic_id)
    require_feature(clinic, "lab_reports")

    if body.branch_id:
        branch = (
            supabase.table("branches")
            .select("id")
            .eq("id", body.branch_id)
            .eq("clinic_id", effective_clinic_id)
            .execute()
        )
        if not branch.data:
            raise HTTPException(status_code=404, detail="Branch not found for this clinic")

    query = (
        supabase.table("integration_connectors")
        .select("*")
        .eq("clinic_id", effective_clinic_id)
        .eq("connector_type", body.connector_type)
    )
    query = query.eq("branch_id", body.branch_id) if body.branch_id else query.is_("branch_id", "null")
    existing = query.execute()
    existing_row = existing.data[0] if existing.data else None

    cfg = dict(existing_row.get("config") or {}) if existing_row else {}

    if body.username is not None and body.username.strip():
        cfg["username"] = body.username.strip()
    if body.password is not None and body.password.strip():
        key = settings.connector_encryption_key
        if not key:
            raise HTTPException(
                status_code=500,
                detail="Connector encryption is not configured on this server — contact support before saving credentials",
            )
        from app.utils.connector_crypto import encrypt_password

        cfg["password_encrypted"] = encrypt_password(body.password.strip(), key)
        cfg.pop("password", None)
    if body.base_url is not None and body.base_url.strip():
        cfg["base_url"] = body.base_url.strip()
    if body.clinic_slug is not None and body.clinic_slug.strip():
        cfg["clinic_slug"] = body.clinic_slug.strip()
    if body.admin_alert_phone is not None and body.admin_alert_phone.strip():
        cfg["admin_alert_phone"] = body.admin_alert_phone.strip()
    if body.poll_interval_minutes is not None:
        cfg["poll_interval_minutes"] = body.poll_interval_minutes

    now = datetime.now().isoformat()
    if existing_row:
        update_data = {"config": cfg, "updated_at": now}
        if body.is_enabled is not None:
            update_data["is_enabled"] = body.is_enabled
        result = (
            supabase.table("integration_connectors")
            .update(update_data)
            .eq("id", existing_row["id"])
            .execute()
        )
        saved = result.data[0]
    else:
        insert_data = {
            "clinic_id": effective_clinic_id,
            "branch_id": body.branch_id,
            "connector_type": body.connector_type,
            "config": cfg,
            "is_enabled": bool(body.is_enabled) if body.is_enabled is not None else False,
        }
        result = supabase.table("integration_connectors").insert(insert_data).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to save connector credentials")
        saved = result.data[0]

    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=user,
        action="upsert_connector_credentials",
        resource_type="integration_connector",
        resource_id=saved.get("id"),
        details={
            "connector_type": body.connector_type,
            "branch_id": body.branch_id,
            "username_changed": bool(body.username),
            "password_changed": bool(body.password),
        },
        ip_address=client_ip,
    )

    return {"success": True, "connector": _mask_connector(saved)}


@router.post("/connectors/{connector_id}/toggle")
async def toggle_connector(
    connector_id: str,
    body: ConnectorToggle,
    user: AdminUser = Depends(require_admin),
):
    """Toggle a connector ON or OFF. This is the primary kill switch."""
    try:
        connector = (
            supabase.table("integration_connectors")
            .select("clinic_id")
            .eq("id", connector_id)
            .execute()
        )
        if not connector.data:
            raise HTTPException(status_code=404, detail="Connector not found")
        enforce_clinic_access(user, connector.data[0]["clinic_id"])

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
        return {"message": f"Connector {status}", "connector": _mask_connector(result.data[0])}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to toggle connector: {e}")
        raise HTTPException(status_code=500, detail="Failed to toggle connector")


@router.get("/connectors/{connector_id}/audit-log")
async def get_connector_audit_log(
    connector_id: str,
    limit: int = 20,
    user: AdminUser = Depends(require_admin),
):
    """Get recent audit log entries (run history — found/uploaded/failed
    counts per poll) for a connector."""
    try:
        # First get the connector to find its clinic_id, type and branch
        connector = (
            supabase.table("integration_connectors")
            .select("clinic_id, connector_type, branch_id")
            .eq("id", connector_id)
            .single()
            .execute()
        )

        if not connector.data:
            raise HTTPException(status_code=404, detail="Connector not found")
        enforce_clinic_access(user, connector.data["clinic_id"])

        query = (
            supabase.table("connector_audit_log")
            .select("*")
            .eq("clinic_id", connector.data["clinic_id"])
            .eq("connector_type", connector.data["connector_type"])
        )
        branch_id = connector.data.get("branch_id")
        query = query.eq("branch_id", branch_id) if branch_id else query.is_("branch_id", "null")
        logs = query.order("created_at", desc=True).limit(limit).execute()

        return {"audit_log": logs.data or []}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get audit log: {e}")
        raise HTTPException(status_code=500, detail="Failed to get audit log")


@router.get("/connectors/failed-reports")
async def get_connector_failed_reports(
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    unresolved_only: bool = True,
    user: AdminUser = Depends(verify_credentials),
):
    """Get per-report failure tracking records for staff visibility — this
    is the "which patient documents failed to send" history."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("connector_failed_reports").select("*")
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        if branch_id:
            query = query.eq("branch_id", branch_id)
        if unresolved_only:
            query = query.is_("resolved_at", "null")
        result = query.order("last_attempt_at", desc=True).execute()
        return {"failed_reports": result.data or []}
    except Exception as e:
        logger.error(f"Failed to get connector failed reports: {e}")
        raise HTTPException(status_code=500, detail="Failed to get failed reports")


@router.post("/connectors/failed-reports/{failed_report_id}/resolve")
async def resolve_connector_failed_report(
    failed_report_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Mark a failed report as manually resolved (e.g. staff re-uploaded the
    document by hand) so it drops off the unresolved failures list."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        query = supabase.table("connector_failed_reports").update(
            {"resolved_at": datetime.now().isoformat()}
        ).eq("id", failed_report_id)
        if effective_clinic_id != "default":
            query = query.eq("clinic_id", effective_clinic_id)
        result = query.execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Failed report not found")
        return {"success": True, "failed_report": result.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to resolve connector failed report: {e}")
        raise HTTPException(status_code=500, detail="Failed to resolve failed report")


@router.get("/audit-logs")
async def get_admin_audit_logs(
    clinic_id: str = "default",
    limit: int = 50,
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
):
    """Create a new branch."""
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)

        try:
            branch_data = branch.model_dump()
        except AttributeError:
            branch_data = branch.dict()

        # Auto-generate name from clinic name + locality if not explicitly set
        if not branch_data.get("name"):
            try:
                clinic_result = (
                    supabase.table("clinics")
                    .select("name")
                    .eq("id", effective_clinic_id)
                    .limit(1)
                    .execute()
                )
                clinic_name = (
                    clinic_result.data[0]["name"]
                    if clinic_result.data
                    else "Clinic"
                )
            except Exception:
                clinic_name = "Clinic"
            branch_data["name"] = f"{clinic_name} - {branch_data['short_name']}"

        branch_data["clinic_id"] = effective_clinic_id
        result = supabase.table("branches").insert(branch_data).execute()

        # Invalidate branch cache
        from app.services.tenant import invalidate_branch_cache

        invalidate_branch_cache(effective_clinic_id)

        return {"success": True, "branch": result.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e).lower()
        if "foreign key" in error_msg:
            raise HTTPException(
                status_code=400,
                detail="Your clinic account isn't linked to a valid clinic. Contact support.",
            )
        logger.error(f"Error creating branch: {e}")
        raise HTTPException(status_code=500, detail="Failed to create branch")


@router.put("/branches/{branch_id}")
async def update_branch(
    branch_id: str,
    branch: BranchUpdate,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_admin),
):
    """Update a branch."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        update_data = branch.dict(exclude_unset=True)
        if not update_data:
            return {"message": "No fields to update"}

        # Re-derive name when short_name changes and name wasn't explicitly set
        if "short_name" in update_data and "name" not in update_data:
            try:
                clinic_result = (
                    supabase.table("clinics")
                    .select("name")
                    .eq("id", effective_clinic_id)
                    .limit(1)
                    .execute()
                )
                clinic_name = (
                    clinic_result.data[0]["name"]
                    if clinic_result.data
                    else "Clinic"
                )
            except Exception:
                clinic_name = "Clinic"
            update_data["name"] = f"{clinic_name} - {update_data['short_name']}"

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
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
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
    user: AdminUser = Depends(require_admin),
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


# ═══════ MESSAGING USAGE (CUSTOMER-SAFE) ═══════
# This endpoint returns ONLY volumetric usage data.
# It NEVER returns costs, pricing, Meta rates, markup, or any financial fields.
# All financial visibility is restricted to /platform/* (owner-only).


@router.get("/messaging-usage")
async def get_messaging_usage(
    user: AdminUser = Depends(verify_credentials),
):
    """Get outbound message usage for the current billing period.

    SECURITY: This is a CUSTOMER-FACING endpoint. The response MUST NOT
    contain any fields related to costs, pricing, Meta rates, markup,
    or Kriya's messaging economics. Only volumetric counts are returned.

    Returns:
        - plan name and display name
        - billing period (calendar month start/end)
        - included messages in plan
        - messages sent this period
        - messages remaining
        - usage percentage
        - daily breakdown (date + count)
        - category breakdown (utility/marketing counts only)
    """
    try:
        from app.services.message_accounting import get_clinic_usage

        # Determine which clinic this admin belongs to
        clinic_id = user.clinic_id
        plan_name = "essential"  # default

        if clinic_id and clinic_id != "default":
            # Fetch clinic plan from database
            try:
                clinic_data = await get_clinic_by_id(clinic_id)
                plan_name = clinic_data.get("plan", "essential")
            except Exception:
                pass
        elif user.role == "super_admin":
            # Super admin viewing default — show aggregate or default
            clinic_id = "default"

        if not clinic_id or clinic_id == "default":
            # For super_admin without a specific clinic, return guidance
            return {
                "message": "Super admin: use /platform/messaging-usage for platform-wide view. "
                           "Specify clinic_id query parameter for per-clinic usage.",
                "plan": "N/A",
            }

        usage = await get_clinic_usage(clinic_id, plan_name)

        await log_admin_action(
            user=user,
            action="VIEW_MESSAGING_USAGE",
            resource_type="billing",
            details={"clinic_id": clinic_id, "messages_sent": usage.get("messages_sent", 0)},
        )

        return usage

    except Exception as e:
        logger.error(f"Error fetching messaging usage: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to fetch messaging usage"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# CLINIC ADMIN IN-APP NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/notifications")
async def get_admin_notifications(
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    user: AdminUser = Depends(verify_credentials),
):
    """Retrieve in-app broadcast alerts for the authenticated clinic admin."""
    clinic_id = await resolve_clinic_id_for_write(user)
    notifications = await broadcast_service.get_admin_notifications(
        clinic_id=clinic_id,
        admin_id=user.user_id,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )
    return {"success": True, "notifications": notifications}


@router.get("/notifications/unread-count")
async def get_admin_notifications_unread_count(
    user: AdminUser = Depends(verify_credentials),
):
    """Get the live count of unread notifications for the header bell badge."""
    clinic_id = await resolve_clinic_id_for_write(user)
    count = await broadcast_service.get_unread_count(
        clinic_id=clinic_id,
        admin_id=user.user_id,
    )
    return {"success": True, "unread_count": count}


@router.patch("/notifications/{notification_id}/read")
async def mark_admin_notification_read(
    notification_id: str,
    user: AdminUser = Depends(verify_credentials),
):
    """Mark a specific notification as read within the authenticated admin's tenant scope."""
    clinic_id = await resolve_clinic_id_for_write(user)
    success = await broadcast_service.mark_notification_read(
        notification_id=notification_id,
        clinic_id=clinic_id,
        admin_id=user.user_id,
    )
    if not success:
        raise HTTPException(
            status_code=404,
            detail="Notification not found or does not belong to this clinic",
        )
    return {"success": True, "message": "Notification marked as read"}


@router.post("/notifications/mark-all-read")
async def mark_all_admin_notifications_read(
    user: AdminUser = Depends(verify_credentials),
):
    """Mark all unread notifications as read for the authenticated admin's clinic."""
    clinic_id = await resolve_clinic_id_for_write(user)
    updated_count = await broadcast_service.mark_all_notifications_read(
        clinic_id=clinic_id,
        admin_id=user.user_id,
    )
    return {
        "success": True,
        "message": f"Marked {updated_count} notifications as read",
        "updated_count": updated_count,
    }

