"""Platform Owner / Super-Admin router for cross-hospital analytics and governance."""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.database import supabase
from app.routers.admin import AdminUser, check_password_hash, log_admin_action
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
