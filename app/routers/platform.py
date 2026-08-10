"""Platform Owner / Super-Admin router for cross-hospital analytics and governance."""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from app.config import settings
from app.database import supabase
from app.routers.admin import AdminUser, check_password_hash, hash_password, log_admin_action
from app.routers.clinics import CreateClinicRequest, provision_clinic
from app.services.analytics import analytics_service
from app.utils.security import login_rate_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform", tags=["platform"])
security = HTTPBasic()


async def verify_owner_credentials(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
) -> AdminUser:
    """Verify platform owner credentials using dedicated OWNER_USERNAME and OWNER_PASSWORD.
    
    Independent of clinic_admins database table and X-Admin-Secret.
    """
    if not settings.owner_username or not settings.owner_password:
        raise HTTPException(
            status_code=503,
            detail="Platform owner dashboard not configured. Set OWNER_USERNAME and OWNER_PASSWORD.",
        )

    client_ip = request.client.host if request.client else "unknown"

    if login_rate_limiter.is_rate_limited(client_ip):
        remaining_wait = 60
        logger.warning(f"Platform owner login rate limit exceeded — IP={client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {remaining_wait} seconds.",
            headers={"Retry-After": str(remaining_wait)},
        )

    login_rate_limiter.record_attempt(client_ip)

    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.owner_username.encode("utf-8"),
    )
    password_ok = check_password_hash(
        credentials.password, settings.owner_password
    )

    if username_ok and password_ok:
        login_rate_limiter.reset(client_ip)
        return AdminUser(
            username=credentials.username,
            role="platform_owner",
            clinic_id=None,
            user_id="platform_owner_env",
        )

    remaining = login_rate_limiter.remaining_attempts(client_ip)
    logger.warning(
        f"Failed platform owner login attempt — IP={client_ip}, "
        f"user='{credentials.username}', remaining={remaining}"
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid owner credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


class ResetAdminPasswordRequest(BaseModel):
    username: str
    new_password: str = Field(..., min_length=8)


@router.put("/reset-admin-password")
async def reset_clinic_admin_password(
    body: ResetAdminPasswordRequest,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Owner-mediated password reset — the forgot-password path for clinic_admins.

    There's no email/SMS infra to verify a locked-out admin's identity
    directly, so resets go through the platform owner (already a separate,
    trusted auth tier), same as how these accounts are provisioned initially.
    """
    client_ip = request.client.host if request.client else "unknown"

    if login_rate_limiter.check_and_record(f"admin-reset:{client_ip}"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password reset attempts. Try again in 60 seconds.",
            headers={"Retry-After": "60"},
        )

    res = (
        supabase.table("clinic_admins")
        .select("id")
        .eq("username", body.username)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Admin account not found")

    supabase.table("clinic_admins").update(
        {"password_hash": hash_password(body.new_password)}
    ).eq("username", body.username).execute()

    await log_admin_action(
        user=owner,
        action="reset_admin_password",
        resource_type="clinic_admin",
        resource_id=body.username,
        ip_address=client_ip,
    )

    return {"success": True, "message": f"Password reset for '{body.username}'"}


VALID_ADMIN_ROLES = {"clinic_admin", "staff"}


@router.post("/clinics")
async def platform_create_clinic(
    req: CreateClinicRequest,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Onboard a new hospital/clinic/diagnostic center from the owner platform UI.

    Same provisioning logic as the curl-based POST /admin/clinics (X-Admin-Secret),
    just gated by owner Basic Auth instead, so the browser never needs to hold
    ADMIN_SECRET. Auto-provisions a clinic_admin login, same as the curl path.
    """
    client_ip = request.client.host if request.client else "unknown"
    result = await provision_clinic(req)
    await log_admin_action(
        user=owner,
        action="create_clinic",
        resource_type="clinic",
        resource_id=result.get("clinic", {}).get("id"),
        details={"name": req.name, "plan": req.plan, "whatsapp_number": req.whatsapp_number},
        ip_address=client_ip,
    )
    return result


class ClinicAdminCreate(BaseModel):
    clinic_id: Optional[str] = None  # None = platform-level (no clinic) account
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)
    role: str = "clinic_admin"


@router.get("/clinic-admins")
async def list_clinic_admins(
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """List all clinic_admins accounts (no password hashes) for the owner's
    account-management dashboard."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_clinic_admins",
        resource_type="platform",
        ip_address=client_ip,
    )

    res = (
        supabase.table("clinic_admins")
        .select("id, clinic_id, username, role, is_active, created_at")
        .order("created_at", desc=True)
        .execute()
    )
    return {"success": True, "admins": res.data or []}


@router.post("/clinic-admins")
async def create_clinic_admin(
    body: ClinicAdminCreate,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Create a new clinic_admins login — the missing self-service piece of
    onboarding, since these accounts previously had to be inserted by hand."""
    client_ip = request.client.host if request.client else "unknown"

    if body.role not in VALID_ADMIN_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"role must be one of {sorted(VALID_ADMIN_ROLES)}",
        )

    if body.clinic_id:
        clinic_res = (
            supabase.table("clinics").select("id").eq("id", body.clinic_id).execute()
        )
        if not clinic_res.data:
            raise HTTPException(status_code=404, detail="Clinic not found")

    existing = (
        supabase.table("clinic_admins")
        .select("id")
        .eq("username", body.username)
        .execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="Username already exists")

    insert_res = (
        supabase.table("clinic_admins")
        .insert(
            {
                "clinic_id": body.clinic_id,
                "username": body.username,
                "password_hash": hash_password(body.password),
                "role": body.role,
                "is_active": True,
            }
        )
        .execute()
    )

    await log_admin_action(
        user=owner,
        action="create_clinic_admin",
        resource_type="clinic_admin",
        resource_id=body.username,
        details={"clinic_id": body.clinic_id, "role": body.role},
        ip_address=client_ip,
    )

    created = insert_res.data[0] if insert_res.data else None
    return {"success": True, "admin": created}


@router.put("/clinic-admins/{admin_id}/toggle")
async def toggle_clinic_admin(
    admin_id: str,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Activate/deactivate a clinic_admins login — for offboarding staff
    without deleting their audit trail."""
    client_ip = request.client.host if request.client else "unknown"

    res = (
        supabase.table("clinic_admins")
        .select("id, is_active")
        .eq("id", admin_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Admin account not found")

    new_status = not res.data[0]["is_active"]
    supabase.table("clinic_admins").update({"is_active": new_status}).eq(
        "id", admin_id
    ).execute()

    await log_admin_action(
        user=owner,
        action="toggle_clinic_admin",
        resource_type="clinic_admin",
        resource_id=admin_id,
        details={"is_active": new_status},
        ip_address=client_ip,
    )

    return {"success": True, "is_active": new_status}


@router.get("/overview")
async def get_platform_overview(
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Headline metrics across all onboarded hospitals."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_platform_overview",
        resource_type="platform",
        ip_address=client_ip,
    )

    try:
        # 1. Fetch clinics list (explicit columns only — no secrets)
        clinics_res = (
            supabase.table("clinics")
            .select("id, name, whatsapp_number, plan, is_active, created_at")
            .execute()
        )
        clinics = clinics_res.data or []

        total_clinics = len(clinics)
        active_clinics = sum(1 for c in clinics if c.get("is_active"))
        inactive_clinics = total_clinics - active_clinics

        # Plan distribution
        clinics_by_plan = {
            "soloclinic": 0,
            "diagstream": 0,
            "essential": 0,
            "polyclinic": 0,
            "enterprise": 0,
        }
        for c in clinics:
            p = c.get("plan", "soloclinic")
            clinics_by_plan[p] = clinics_by_plan.get(p, 0) + 1

        # New clinics this month
        now = datetime.now(timezone.utc)
        start_of_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()
        new_clinics_this_month = sum(
            1 for c in clinics if c.get("created_at", "") >= start_of_month
        )

        # 2. Total patients count platform-wide
        patients_res = (
            supabase.table("patients")
            .select("id", count="exact")
            .execute()
        )
        total_patients = patients_res.count if patients_res.count is not None else len(patients_res.data or [])

        # 3. Total appointments count platform-wide
        appts_res = (
            supabase.table("appointments")
            .select("id", count="exact")
            .execute()
        )
        total_appointments = appts_res.count if appts_res.count is not None else len(appts_res.data or [])

        return {
            "success": True,
            "total_clinics": total_clinics,
            "active_clinics": active_clinics,
            "inactive_clinics": inactive_clinics,
            "clinics_by_plan": clinics_by_plan,
            "new_clinics_this_month": new_clinics_this_month,
            "total_patients_platform_wide": total_patients,
            "total_appointments_platform_wide": total_appointments,
        }

    except Exception as e:
        logger.error(f"Error fetching platform overview: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch overview: {e}")


@router.get("/clinics")
async def get_platform_clinics_leaderboard(
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Per-clinic leaderboard table showing usage, activity, and confirmed revenue."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_platform_clinics",
        resource_type="platform",
        ip_address=client_ip,
    )

    try:
        # Strict explicit column projection
        clinics_res = (
            supabase.table("clinics")
            .select("id, name, whatsapp_number, plan, is_active, created_at")
            .execute()
        )
        clinics = clinics_res.data or []
        start_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        async def fetch_clinic_metrics(c: dict) -> dict:
            clinic_id = c["id"]
            
            # Fetch appointments for this clinic in last 30d
            appts_res = (
                supabase.table("appointments")
                .select("id, status, amount_paise, payment_id, created_at")
                .eq("clinic_id", clinic_id)
                .gte("created_at", start_30d)
                .execute()
            )
            appts = appts_res.data or []
            appointments_count_30d = len(appts)

            # Revenue calculation (sum of confirmed payments)
            confirmed_revenue_paise = sum(
                a.get("amount_paise", 0) or 0
                for a in appts
                if a.get("status") == "confirmed" and a.get("payment_id")
            )
            confirmed_revenue_inr = round(confirmed_revenue_paise / 100.0, 2)

            # Patient count
            pat_res = (
                supabase.table("patients")
                .select("id", count="exact")
                .eq("clinic_id", clinic_id)
                .execute()
            )
            patients_count = pat_res.count if pat_res.count is not None else len(pat_res.data or [])

            # Last activity calculation (latest appointment created_at or clinic created_at)
            last_activity = c.get("created_at")
            if appts:
                latest_appt_time = max(a.get("created_at", "") for a in appts if a.get("created_at"))
                if latest_appt_time and latest_appt_time > last_activity:
                    last_activity = latest_appt_time

            return {
                "id": clinic_id,
                "name": c.get("name"),
                "whatsapp_number": c.get("whatsapp_number"),
                "plan": c.get("plan"),
                "is_active": c.get("is_active", True),
                "created_at": c.get("created_at"),
                "appointments_count_30d": appointments_count_30d,
                "patients_count": patients_count,
                "revenue_inr_30d": confirmed_revenue_inr,
                "last_activity": last_activity,
            }

        # Run clinic metric queries concurrently via asyncio.gather
        tasks = [fetch_clinic_metrics(c) for c in clinics]
        leaderboard = await asyncio.gather(*tasks) if tasks else []

        # Sort by revenue descending by default
        leaderboard.sort(key=lambda item: (item["revenue_inr_30d"], item["appointments_count_30d"]), reverse=True)

        return {"success": True, "clinics": leaderboard}

    except Exception as e:
        logger.error(f"Error fetching platform clinics leaderboard: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch leaderboard: {e}")


@router.get("/clinics/{clinic_id}")
async def get_platform_clinic_detail(
    clinic_id: str,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Drill-down into detailed analytics for a single clinic."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_platform_clinic_detail",
        resource_type="clinic",
        resource_id=clinic_id,
        ip_address=client_ip,
    )

    try:
        # Fetch clinic basic info
        clinic_res = (
            supabase.table("clinics")
            .select("id, name, whatsapp_number, plan, is_active, created_at")
            .eq("id", clinic_id)
            .execute()
        )
        if not clinic_res.data:
            raise HTTPException(status_code=404, detail="Clinic not found")

        clinic = clinic_res.data[0]
        stats = await analytics_service.get_dashboard_stats(clinic_id=clinic_id, days=30)

        return {
            "success": True,
            "clinic": clinic,
            "analytics": stats,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching clinic detail for {clinic_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch clinic detail: {e}")


@router.get("/revenue")
async def get_platform_revenue_analytics(
    request: Request,
    days: int = 30,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Platform-wide financial trends, daily revenue timeseries, and refund metrics."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_platform_revenue",
        resource_type="platform",
        ip_address=client_ip,
    )

    try:
        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Query appointments with payments
        appts_res = (
            supabase.table("appointments")
            .select("clinic_id, status, amount_paise, created_at, payment_id")
            .gte("created_at", start_date)
            .execute()
        )
        appts = appts_res.data or []

        # Revenue timeseries by date (YYYY-MM-DD) and clinic
        daily_revenue: dict[str, dict[str, float]] = {}
        total_confirmed_count = 0
        total_refunded_count = 0
        total_revenue_paise = 0

        for a in appts:
            st = a.get("status")
            if st == "confirmed" and a.get("payment_id"):
                total_confirmed_count += 1
                amt = a.get("amount_paise", 0) or 0
                total_revenue_paise += amt

                created = a.get("created_at", "")[:10]  # YYYY-MM-DD
                clinic_id = a.get("clinic_id", "unknown")

                if created not in daily_revenue:
                    daily_revenue[created] = {}
                daily_revenue[created][clinic_id] = daily_revenue[created].get(clinic_id, 0.0) + (amt / 100.0)

            # "refunded" means money was actually paid back. "cancelled" is a
            # much broader bucket that includes bookings that were never paid
            # (direct/free bookings, or a pending_payment hold that simply
            # expired) — counting those as refunds would inflate the refund
            # rate shown on this dashboard, which exists specifically to be a
            # trustworthy revenue number.
            elif st == "refunded" and a.get("payment_id"):
                total_refunded_count += 1

        refund_rate = (
            round(
                (total_refunded_count / (total_confirmed_count + total_refunded_count))
                * 100.0,
                2,
            )
            if (total_confirmed_count + total_refunded_count) > 0
            else 0.0
        )

        # Sort timeseries
        sorted_timeseries = [
            {"date": d, "clinics": daily_revenue[d], "total_inr": sum(daily_revenue[d].values())}
            for d in sorted(daily_revenue.keys())
        ]

        return {
            "success": True,
            "period_days": days,
            "total_revenue_inr": round(total_revenue_paise / 100.0, 2),
            "total_confirmed_payments": total_confirmed_count,
            "total_refunded_payments": total_refunded_count,
            "refund_rate_percentage": refund_rate,
            "timeseries": sorted_timeseries,
        }

    except Exception as e:
        logger.error(f"Error fetching revenue analytics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch revenue analytics: {e}")


@router.get("/activity")
async def get_platform_activity_analytics(
    request: Request,
    days: int = 30,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Platform usage trends: event counts and processed message volume."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_platform_activity",
        resource_type="platform",
        ip_address=client_ip,
    )

    try:
        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # 1. Query analytics_events
        events_res = (
            supabase.table("analytics_events")
            .select("clinic_id, event_type, created_at")
            .gte("created_at", start_date)
            .execute()
        )
        events = events_res.data or []

        daily_events: dict[str, dict[str, int]] = {}
        event_type_breakdown: dict[str, int] = {}

        for e in events:
            created = e.get("created_at", "")[:10]
            clinic_id = e.get("clinic_id", "unknown")
            ev_type = e.get("event_type", "unknown")

            event_type_breakdown[ev_type] = event_type_breakdown.get(ev_type, 0) + 1

            if created not in daily_events:
                daily_events[created] = {}
            daily_events[created][clinic_id] = daily_events[created].get(clinic_id, 0) + 1

        # 2. Query processed_messages (if clinic_id exists)
        daily_messages: dict[str, int] = {}
        try:
            msg_res = (
                supabase.table("processed_messages")
                .select("created_at")
                .gte("created_at", start_date)
                .execute()
            )
            for m in (msg_res.data or []):
                created = m.get("created_at", "")[:10]
                if created:
                    daily_messages[created] = daily_messages.get(created, 0) + 1
        except Exception as msg_err:
            logger.warning(f"Could not fetch processed_messages activity: {msg_err}")

        sorted_activity = [
            {
                "date": d,
                "events_by_clinic": daily_events.get(d, {}),
                "total_events": sum(daily_events.get(d, {}).values()),
                "message_volume": daily_messages.get(d, 0),
            }
            for d in sorted(set(list(daily_events.keys()) + list(daily_messages.keys())))
        ]

        return {
            "success": True,
            "period_days": days,
            "event_type_breakdown": event_type_breakdown,
            "activity_timeseries": sorted_activity,
        }

    except Exception as e:
        logger.error(f"Error fetching activity analytics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch activity analytics: {e}")


@router.get("/departments")
async def get_platform_department_analytics(
    request: Request,
    days: int = 30,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Cross-hospital department traffic rankings and peak booking hours.

    Platform-owner-only — clinic admins have no route to this data since it
    aggregates across tenants (separate router, separate credential set).
    """
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_platform_departments",
        resource_type="platform",
        ip_address=client_ip,
    )

    try:
        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        clinics_res = supabase.table("clinics").select("id, name").execute()
        clinic_names = {c["id"]: c["name"] for c in (clinics_res.data or [])}

        appts_res = (
            supabase.table("appointments")
            .select("clinic_id, department, created_at")
            .gte("created_at", start_date)
            .execute()
        )
        appts = appts_res.data or []

        # Platform-wide department leaderboard
        dept_totals: dict[str, int] = {}
        # Per-hospital department breakdown
        by_clinic: dict[str, dict[str, int]] = {}
        # Peak hours (0-23), platform-wide
        hour_counts: dict[int, int] = {h: 0 for h in range(24)}

        for a in appts:
            dept = a.get("department") or "Unknown"
            clinic_id = a.get("clinic_id", "unknown")
            created = a.get("created_at", "")

            dept_totals[dept] = dept_totals.get(dept, 0) + 1
            by_clinic.setdefault(clinic_id, {})
            by_clinic[clinic_id][dept] = by_clinic[clinic_id].get(dept, 0) + 1

            if created:
                try:
                    hour = datetime.fromisoformat(created.replace("Z", "+00:00")).hour
                    hour_counts[hour] += 1
                except ValueError:
                    pass

        department_leaderboard = sorted(
            [{"department": k, "count": v} for k, v in dept_totals.items()],
            key=lambda x: x["count"],
            reverse=True,
        )

        hospital_departments = [
            {
                "clinic_id": clinic_id,
                "clinic_name": clinic_names.get(clinic_id, clinic_id),
                "departments": sorted(
                    [{"department": d, "count": c} for d, c in depts.items()],
                    key=lambda x: x["count"],
                    reverse=True,
                ),
            }
            for clinic_id, depts in by_clinic.items()
        ]
        hospital_departments.sort(
            key=lambda h: sum(d["count"] for d in h["departments"]), reverse=True
        )

        peak_hours = [{"hour": h, "count": hour_counts[h]} for h in range(24)]

        return {
            "success": True,
            "period_days": days,
            "department_leaderboard": department_leaderboard,
            "hospital_departments": hospital_departments,
            "peak_hours": peak_hours,
        }

    except Exception as e:
        logger.error(f"Error fetching department analytics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch department analytics: {e}")
