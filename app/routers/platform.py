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
from app.routers.admin import (
    AdminUser,
    ConnectorCredentialsUpdate,
    check_password_hash,
    hash_password,
    log_admin_action,
    upsert_connector_credentials,
)
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
            .select("id, name, whatsapp_number, plan, features, is_active, created_at")
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


class ClinicFeatureOverride(BaseModel):
    """Per-clinic feature toggle — lets the owner switch a single bundled
    plan feature (e.g. lab_reports) on/off for one clinic without changing
    its plan tier. True/False sets an explicit override; null clears the
    override so the clinic falls back to its plan's default for that feature."""

    feature: str
    enabled: Optional[bool] = None


@router.patch("/clinics/{clinic_id}/features")
async def update_clinic_feature(
    clinic_id: str,
    body: ClinicFeatureOverride,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Set or clear a per-clinic feature override (clinics.features JSONB).

    Same mechanism `has_feature()` already reads (app/services/tenant.py) —
    this just exposes it through the owner-authenticated dashboard instead of
    requiring a raw SQL update or the X-Admin-Secret curl API.
    """
    from app.services.tenant import ALL_FEATURES, invalidate_tenant_cache

    if body.feature not in ALL_FEATURES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown feature '{body.feature}'. Valid: {sorted(ALL_FEATURES)}",
        )

    client_ip = request.client.host if request.client else "unknown"

    clinic_res = (
        supabase.table("clinics").select("features").eq("id", clinic_id).execute()
    )
    if not clinic_res.data:
        raise HTTPException(status_code=404, detail="Clinic not found")

    features = dict(clinic_res.data[0].get("features") or {})
    if body.enabled is None:
        features.pop(body.feature, None)
    else:
        features[body.feature] = body.enabled

    result = (
        supabase.table("clinics")
        .update({"features": features})
        .eq("id", clinic_id)
        .execute()
    )
    invalidate_tenant_cache()

    await log_admin_action(
        user=owner,
        action="update_clinic_feature",
        resource_type="clinic",
        resource_id=clinic_id,
        details={"feature": body.feature, "enabled": body.enabled},
        ip_address=client_ip,
    )

    return {"success": True, "clinic": result.data[0] if result.data else None}


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


# ═══════════════════════════════════════════════════════════════════════════════
# CALLMEDEX PROCESSING CENTERS
#
# A "CallMedex processing center" is architecturally just a clinic with an
# enabled `integration_connectors` row (MocDoc/Crelio/CloudLIMS) — the same
# connector self-service clinic admins already use for their own EMR portal
# login. CallMedex report jobs land via app.integrations.callmedex, wire
# through that connector, and get delivered over WhatsApp by
# app/integrations/callmedex/workers/runner.py, which writes a `lab_reports`
# row tagged source='callmedex' (see migrations/026_lab_reports_callmedex_
# attribution.sql). This dashboard reads exactly that data; no new tables.
# ═══════════════════════════════════════════════════════════════════════════════


@router.put("/clinics/{clinic_id}/connector")
async def platform_upsert_connector(
    clinic_id: str,
    body: ConnectorCredentialsUpdate,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Owner-curated setup of a clinic's EMR connector (MocDoc/Crelio/CloudLIMS) —
    e.g. enrolling a diagnostic center as a CallMedex processing center by
    configuring its portal login. Delegates to the same self-service logic
    clinic admins use (app.routers.admin.upsert_connector_credentials), just
    reachable under owner Basic Auth so onboarding a new processing center
    doesn't require that clinic's own admin credentials.
    """
    return await upsert_connector_credentials(
        body=body, request=request, clinic_id=clinic_id, user=owner
    )


class CallMedexWhatsAppSettingsUpdate(BaseModel):
    """Partial update — an omitted/blank field never overwrites the stored
    value (same convention as ConnectorCredentialsUpdate)."""

    phone_number_id: Optional[str] = None
    api_token: Optional[str] = None


@router.get("/callmedex/whatsapp-settings")
async def get_callmedex_whatsapp_settings(
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """The single WhatsApp number CallMedex uses to deliver every CallMedex-
    booked report, platform-wide (not per-clinic). Never returns the raw
    token — only whether one is configured."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_callmedex_whatsapp_settings",
        resource_type="platform",
        ip_address=client_ip,
    )

    res = (
        supabase.table("callmedex_whatsapp_settings")
        .select("phone_number_id, api_token_encrypted, updated_at, updated_by")
        .eq("id", "default")
        .execute()
    )
    row = res.data[0] if res.data else None

    if row and row.get("phone_number_id") and row.get("api_token_encrypted"):
        return {
            "success": True,
            "source": "database",
            "phone_number_id": row["phone_number_id"],
            "token_set": True,
            "updated_at": row.get("updated_at"),
            "updated_by": row.get("updated_by"),
        }

    # No owner-configured override yet — report what the env-var fallback holds.
    from app.integrations.callmedex.config.settings import callmedex_settings

    env_token = callmedex_settings.whatsapp_api_token.get_secret_value()
    return {
        "success": True,
        "source": "environment",
        "phone_number_id": callmedex_settings.whatsapp_phone_number_id,
        "token_set": bool(env_token) and env_token != "dev_whatsapp_token",
        "updated_at": None,
        "updated_by": None,
    }


@router.put("/callmedex/whatsapp-settings")
async def update_callmedex_whatsapp_settings(
    body: CallMedexWhatsAppSettingsUpdate,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Owner-only add/change of CallMedex's single global WhatsApp number.
    Takes effect immediately — the CallMedex delivery path reads this table
    live on every send, no redeploy needed."""
    client_ip = request.client.host if request.client else "unknown"

    if not body.phone_number_id and not body.api_token:
        raise HTTPException(
            status_code=400,
            detail="Provide phone_number_id and/or api_token to update",
        )

    existing = (
        supabase.table("callmedex_whatsapp_settings")
        .select("*")
        .eq("id", "default")
        .execute()
    )
    row = dict(existing.data[0]) if existing.data else {"id": "default"}

    if body.phone_number_id and body.phone_number_id.strip():
        row["phone_number_id"] = body.phone_number_id.strip()

    if body.api_token and body.api_token.strip():
        key = settings.connector_encryption_key
        if not key:
            raise HTTPException(
                status_code=500,
                detail="Connector encryption is not configured on this server — contact support before saving the token",
            )
        from app.utils.connector_crypto import encrypt_password

        row["api_token_encrypted"] = encrypt_password(body.api_token.strip(), key)

    row["updated_at"] = datetime.now(timezone.utc).isoformat()
    row["updated_by"] = owner.username

    supabase.table("callmedex_whatsapp_settings").upsert(row).execute()

    await log_admin_action(
        user=owner,
        action="update_callmedex_whatsapp_settings",
        resource_type="platform",
        details={
            "phone_number_id_changed": bool(body.phone_number_id),
            "token_changed": bool(body.api_token),
        },
        ip_address=client_ip,
    )

    return {"success": True, "phone_number_id": row.get("phone_number_id")}


@router.get("/callmedex/centers")
async def get_callmedex_processing_centers(
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Every clinic enrolled as a CallMedex processing center (an enabled EMR
    connector), plus its CallMedex report-delivery stats."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_callmedex_centers",
        resource_type="platform",
        ip_address=client_ip,
    )

    try:
        connectors_res = (
            supabase.table("integration_connectors")
            .select(
                "id, clinic_id, connector_type, is_enabled, last_run_at, last_success_at, last_error"
            )
            .eq("is_enabled", True)
            .execute()
        )
        connectors = connectors_res.data or []
        if not connectors:
            return {
                "success": True,
                "centers": [],
                "total_active_centers": 0,
                "total_reports_delivered": 0,
            }

        clinic_ids = list({c["clinic_id"] for c in connectors})
        clinics_res = (
            supabase.table("clinics")
            .select("id, name, whatsapp_number, is_active")
            .in_("id", clinic_ids)
            .execute()
        )
        clinics_by_id = {c["id"]: c for c in (clinics_res.data or [])}

        start_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        reports_res = (
            supabase.table("lab_reports")
            .select("clinic_id, uploaded_at")
            .eq("source", "callmedex")
            .in_("clinic_id", clinic_ids)
            .execute()
        )
        reports_by_clinic: dict[str, list] = {}
        for r in reports_res.data or []:
            reports_by_clinic.setdefault(r["clinic_id"], []).append(r.get("uploaded_at", ""))

        centers = []
        total_delivered = 0
        for conn in connectors:
            cid = conn["clinic_id"]
            clinic = clinics_by_id.get(cid)
            if not clinic:
                continue
            timestamps = reports_by_clinic.get(cid, [])
            delivered_30d = sum(1 for t in timestamps if t >= start_30d)
            total_delivered += len(timestamps)

            centers.append(
                {
                    "clinic_id": cid,
                    "clinic_name": clinic.get("name"),
                    "whatsapp_number": clinic.get("whatsapp_number"),
                    "is_active": clinic.get("is_active", True),
                    "connector_id": conn["id"],
                    "connector_type": conn["connector_type"],
                    "reports_delivered_total": len(timestamps),
                    "reports_delivered_30d": delivered_30d,
                    "last_delivery_at": max(timestamps) if timestamps else None,
                    "last_run_at": conn.get("last_run_at"),
                    "last_success_at": conn.get("last_success_at"),
                    "last_error": conn.get("last_error"),
                }
            )

        centers.sort(key=lambda x: x["reports_delivered_30d"], reverse=True)

        return {
            "success": True,
            "centers": centers,
            "total_active_centers": len(centers),
            "total_reports_delivered": total_delivered,
        }

    except Exception as e:
        logger.error(f"Error fetching CallMedex processing centers: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch CallMedex centers: {e}")


# Meta WhatsApp Cloud API per-message pricing (INR), per master billing spec.
UTILITY_MSG_COST_INR = 0.12
MARKETING_MSG_COST_INR = 0.75


@router.get("/messaging-usage")
async def get_platform_messaging_usage(
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Platform-wide WhatsApp outbound message volume and estimated Meta
    Cloud API billing, broken down by clinic and template category."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_platform_messaging_usage",
        resource_type="platform",
        ip_address=client_ip,
    )

    try:
        clinics_res = (
            supabase.table("clinics")
            .select("id, name, plan, is_active")
            .execute()
        )
        clinics = clinics_res.data or []
        start_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        appts_res = (
            supabase.table("appointments")
            .select("clinic_id, status, reminder_24h_sent, reminder_2h_sent, followup_sent, created_at")
            .gte("created_at", start_30d)
            .execute()
        )
        appts = appts_res.data or []

        reports_res = (
            supabase.table("lab_reports")
            .select("clinic_id, sent_at")
            .gte("sent_at", start_30d)
            .execute()
        )
        reports = reports_res.data or []

        usage_by_clinic: dict[str, dict] = {}

        def bucket(clinic_id: str) -> dict:
            return usage_by_clinic.setdefault(
                clinic_id, {"utility": 0, "marketing": 0}
            )

        for a in appts:
            cid = a.get("clinic_id")
            if not cid:
                continue
            u = bucket(cid)
            if a.get("status") == "confirmed":
                u["utility"] += 1
            if a.get("status") == "cancelled":
                u["utility"] += 1
            if a.get("reminder_24h_sent"):
                u["utility"] += 1
            if a.get("reminder_2h_sent"):
                u["utility"] += 1
            if a.get("followup_sent"):
                u["marketing"] += 1

        for r in reports:
            cid = r.get("clinic_id")
            if not cid or not r.get("sent_at"):
                continue
            bucket(cid)["utility"] += 1

        clinics_table = []
        total_utility = 0
        total_marketing = 0
        for c in clinics:
            u = usage_by_clinic.get(c["id"], {"utility": 0, "marketing": 0})
            utility_count = u["utility"]
            marketing_count = u["marketing"]
            total_utility += utility_count
            total_marketing += marketing_count
            cost_inr = round(
                utility_count * UTILITY_MSG_COST_INR
                + marketing_count * MARKETING_MSG_COST_INR,
                2,
            )
            clinics_table.append(
                {
                    "clinic_id": c["id"],
                    "clinic_name": c.get("name"),
                    "plan": c.get("plan"),
                    "is_active": c.get("is_active", True),
                    "outbound_total": utility_count + marketing_count,
                    "utility_count": utility_count,
                    "marketing_count": marketing_count,
                    "estimated_cost_inr": cost_inr,
                }
            )

        clinics_table.sort(key=lambda x: x["outbound_total"], reverse=True)

        total_outbound = total_utility + total_marketing
        total_cost_inr = round(
            total_utility * UTILITY_MSG_COST_INR
            + total_marketing * MARKETING_MSG_COST_INR,
            2,
        )

        return {
            "success": True,
            "period_days": 30,
            "total_outbound": total_outbound,
            "total_utility": total_utility,
            "total_marketing": total_marketing,
            "total_service": 0,
            "total_estimated_cost_inr": total_cost_inr,
            "pricing": {
                "utility_inr": UTILITY_MSG_COST_INR,
                "marketing_inr": MARKETING_MSG_COST_INR,
                "service_inr": 0.0,
            },
            "clinics": clinics_table,
        }

    except Exception as e:
        logger.error(f"Error fetching platform messaging usage: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch messaging usage: {e}")
