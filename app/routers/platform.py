"""Platform Owner / Super-Admin router for cross-hospital analytics and governance."""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

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
from app.services.broadcast import broadcast_service
from app.services.subscription import (
    DAILY_REPORT_LIMIT_TIERS,
    compute_subscription_state,
    ist_today,
    limit_state,
    renewal_window,
)
from app.services.tenant import invalidate_branch_cache, invalidate_tenant_cache
from app.utils.security import login_rate_limiter
from app.database import sb  # T5.1: off-loop query execution

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

    # T5.1: the limiter is synchronous and hits the rate_limits table; this
    # dependency runs on every /platform request, so it was a blocking DB
    # round-trip on the event loop each time.
    if await asyncio.to_thread(login_rate_limiter.check_and_record, client_ip):
        remaining_wait = 60
        logger.warning(f"Platform owner login rate limit exceeded — IP={client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {remaining_wait} seconds.",
            headers={"Retry-After": str(remaining_wait)},
        )

    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.owner_username.encode("utf-8"),
    )
    if settings.owner_password.startswith(("$2b$", "$2a$", "$2y$")):
        password_ok = check_password_hash(credentials.password, settings.owner_password)
    else:
        password_ok = secrets.compare_digest(
            credentials.password.encode("utf-8"),
            settings.owner_password.encode("utf-8"),
        )

    if username_ok and password_ok:
        await asyncio.to_thread(login_rate_limiter.reset, client_ip)
        return AdminUser(
            username=credentials.username,
            role="platform_owner",
            clinic_id=None,
            user_id="platform_owner_env",
        )

    remaining = await asyncio.to_thread(
        login_rate_limiter.remaining_attempts, client_ip
    )
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

    if await asyncio.to_thread(
        login_rate_limiter.check_and_record, f"admin-reset:{client_ip}"
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password reset attempts. Try again in 60 seconds.",
            headers={"Retry-After": "60"},
        )

    # platform-scoped: reset password by username
    res = (
        # unscoped: platform super-admin resetting password by username across clinic_admins
        await sb(supabase.table("clinic_admins")
        .select("id")
        .eq("username", body.username))
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Admin account not found")

    # platform-scoped: update admin password hash
    # unscoped: platform super-admin updating password hash for specified username
    await sb(supabase.table("clinic_admins").update(
        {"password_hash": hash_password(body.new_password)}
    ).eq("username", body.username))

    # An owner-mediated reset is the lock-out recovery path, so it is also the
    # path used after a suspected compromise. It must terminate the sessions
    # the old password already produced, not just change the next login.
    from app.routers.admin import revoke_sessions_for_user

    await revoke_sessions_for_user(body.username)

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
        # unscoped: platform_admin
        await sb(supabase.table("clinic_admins")
        .select("id, clinic_id, username, role, is_active, created_at")
        .order("created_at", desc=True))
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
            # unscoped: platform super-admin verifying target clinic exists before creating admin
            await sb(supabase.table("clinics").select("id").eq("id", body.clinic_id))
        )
        if not clinic_res.data:
            raise HTTPException(status_code=404, detail="Clinic not found")

    # platform-scoped: check admin username uniqueness
    existing = (
        # unscoped: platform super-admin checking global username uniqueness across clinic admins
        await sb(supabase.table("clinic_admins")
        .select("id")
        .eq("username", body.username))
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="Username already exists")

    insert_res = (
        # unscoped: platform super-admin verifying target clinic exists before creating admin
        await sb(supabase.table("clinic_admins")
        .insert(
            {
                "clinic_id": body.clinic_id,
                "username": body.username,
                "password_hash": hash_password(body.password),
                "role": body.role,
                "is_active": True,
            }
        ))
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

    # platform-scoped: fetch clinic admin status
    res = (
        # unscoped: platform super-admin fetching clinic admin active status by admin_id
        await sb(supabase.table("clinic_admins")
        .select("id, is_active")
        .eq("id", admin_id))
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Admin account not found")

    new_status = not res.data[0]["is_active"]
    # platform-scoped: toggle clinic admin
    # unscoped: platform super-admin toggling clinic admin active status by admin_id
    await sb(supabase.table("clinic_admins").update({"is_active": new_status}).eq(
        "id", admin_id
    ))

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
        # platform-scoped: aggregate platform clinics list
        clinics_res = (
            # unscoped: platform_admin
            await sb(supabase.table("clinics")
            .select("id, name, whatsapp_number, plan, is_active, created_at"))
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
        # platform-scoped: platform total patient count
        patients_res = (
            # unscoped: platform super-admin aggregating total patient count across all clinics
            await sb(supabase.table("patients")
            .select("id", count="exact"))
        )
        total_patients = patients_res.count if patients_res.count is not None else len(patients_res.data or [])

        # 3. Total appointments count platform-wide
        # platform-scoped: platform total appointments count
        appts_res = (
            # unscoped: platform_admin
            await sb(supabase.table("appointments")
            .select("id", count="exact"))
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


async def _fetch_clinic_roster_counts() -> dict[str, dict]:
    """Active-doctor count and distinct department set, keyed by clinic_id.

    Pages through `doctors` because PostgREST caps an unbounded select at
    1000 rows — a silent truncation here would understate the roster of the
    largest hospitals, which are exactly the ones plan pricing depends on.

    Returns {} on any failure: roster counts are a sizing aid, not a reason
    to fail the whole fleet leaderboard.
    """
    page_size = 1000
    max_pages = 100  # ponytail: 100k-doctor ceiling; server-side aggregate if ever hit
    roster: dict[str, dict] = {}

    try:
        for page in range(max_pages):
            offset = page * page_size
            docs_res = (
                # unscoped: platform_admin
                await sb(supabase.table("doctors")
                .select("clinic_id, department, is_active")
                .range(offset, offset + page_size - 1))
            )
            rows = docs_res.data
            if not isinstance(rows, list):
                logger.warning("Roster counts skipped — unexpected doctors payload type")
                return {}
            for d in rows:
                clinic_id = d.get("clinic_id")
                # is_active defaults to true in schema; only an explicit False excludes.
                if not clinic_id or d.get("is_active") is False:
                    continue
                entry = roster.setdefault(clinic_id, {"doctors": 0, "departments": set()})
                entry["doctors"] += 1
                dept = (d.get("department") or "").strip()
                if dept:
                    entry["departments"].add(dept)
            if len(rows) < page_size:
                break
        else:
            logger.warning(
                f"Roster scan hit the {max_pages}-page cap — counts may be incomplete"
            )
    except Exception as e:
        logger.warning(f"Roster counts unavailable for leaderboard: {e}")
        return {}

    return roster


async def _fetch_clinic_branch_counts(window_days: int = 30) -> dict[str, dict]:
    """Per-clinic branch census, keyed by clinic_id.

    Branches are the unit the platform bills on: a diagnostic chain that opens
    a second collection centre costs twice what a single-centre one does. The
    owner therefore needs the count on the leaderboard, and needs to see when
    it moved -- a branch added quietly in a clinic admin panel is a silent
    change to what that clinic owes.

    Paginated for the same reason the roster scan is: PostgREST caps an
    unbounded select at 1000 rows, and an undercount here would understate a
    bill. Returns {} on failure -- the leaderboard must still render.
    """
    page_size = 1000
    max_pages = 100  # ponytail: 100k-branch ceiling; server-side aggregate if ever hit
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    census: dict[str, dict] = {}

    try:
        for page in range(max_pages):
            offset = page * page_size
            res = (
                # unscoped: platform_admin
                await sb(supabase.table("branches")
                .select("clinic_id, is_active, is_diagnostic, created_at")
                .range(offset, offset + page_size - 1))
            )
            rows = res.data
            if not isinstance(rows, list):
                logger.warning("Branch census skipped — unexpected branches payload type")
                return {}
            for b in rows:
                clinic_id = b.get("clinic_id")
                if not clinic_id:
                    continue
                entry = census.setdefault(
                    clinic_id,
                    {"total": 0, "active": 0, "diagnostic": 0, "added_recently": 0, "newest_at": None},
                )
                entry["total"] += 1
                # is_active defaults true in schema; only an explicit False excludes.
                if b.get("is_active") is not False:
                    entry["active"] += 1
                if b.get("is_diagnostic"):
                    entry["diagnostic"] += 1
                created = b.get("created_at")
                if created:
                    if created >= cutoff:
                        entry["added_recently"] += 1
                    if not entry["newest_at"] or created > entry["newest_at"]:
                        entry["newest_at"] = created
            if len(rows) < page_size:
                break
        else:
            logger.warning(
                f"Branch census hit the {max_pages}-page cap — counts may be incomplete"
            )
    except Exception as e:
        logger.warning(f"Branch census unavailable for leaderboard: {e}")
        return {}

    return census


def _billable_locations(active_branches: int) -> int:
    """What the clinic is billed for.

    A clinic with no branch rows at all is still one physical location, so the
    floor is 1 rather than 0 -- otherwise every single-site clinic on the
    platform would read as costing nothing.
    """
    return max(1, active_branches)


@router.get("/branch-changes")
async def get_platform_branch_changes(
    request: Request,
    days: int = 30,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Branches opened or closed across the fleet — the billing watchlist.

    Sourced from branches.created_at rather than the audit log so it is correct
    for branches created before branch auditing existed; audit rows are folded
    in on top for the who/when of removals.
    """
    days = max(1, min(int(days or 30), 365))
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_platform_branch_changes",
        resource_type="platform",
        ip_address=client_ip,
    )

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        clinics_res = (
            # unscoped: platform_admin
            await sb(supabase.table("clinics").select("id, name, plan"))
        )
        clinic_map = {c["id"]: c for c in (clinics_res.data or [])}

        added_res = (
            # unscoped: platform_admin
            await sb(supabase.table("branches")
            .select("id, clinic_id, name, short_name, is_active, is_diagnostic, created_at")
            .gte("created_at", cutoff)
            .order("created_at", desc=True))
        )
        additions = []
        for b in (added_res.data or []):
            clinic = clinic_map.get(b.get("clinic_id")) or {}
            additions.append({
                "branch_id": b.get("id"),
                "branch_name": b.get("name"),
                "short_name": b.get("short_name"),
                "clinic_id": b.get("clinic_id"),
                "clinic_name": clinic.get("name"),
                "plan": clinic.get("plan"),
                "is_active": b.get("is_active", True),
                "is_diagnostic": b.get("is_diagnostic", False),
                "created_at": b.get("created_at"),
            })

        # Removals and deactivations only exist in the audit trail — the row is
        # gone from `branches`.
        removals = []
        try:
            audit_res = (
                # unscoped: platform_admin
                await sb(supabase.table("admin_audit_logs")
                .select("clinic_id, username, action, resource_id, details, created_at")
                .in_("action", ["deactivate_branch", "delete_branch"])
                .gte("created_at", cutoff)
                .order("created_at", desc=True)
                .limit(200))
            )
            for row in (audit_res.data or []):
                clinic = clinic_map.get(row.get("clinic_id")) or {}
                removals.append({
                    "branch_id": row.get("resource_id"),
                    "clinic_id": row.get("clinic_id"),
                    "clinic_name": clinic.get("name"),
                    "action": row.get("action"),
                    "by": row.get("username"),
                    "details": row.get("details") or {},
                    "created_at": row.get("created_at"),
                })
        except Exception as e:
            # Pre-dates branch auditing, or the table is unavailable. Additions
            # are the billing-relevant half and must still be returned.
            logger.warning(f"Branch removal audit unavailable: {e}")

        census = await _fetch_clinic_branch_counts(window_days=days)
        total_billable = sum(
            _billable_locations((census.get(cid) or {}).get("active", 0))
            for cid in clinic_map
        )

        return {
            "success": True,
            "window_days": days,
            "additions": additions,
            "removals": removals,
            "additions_count": len(additions),
            "removals_count": len(removals),
            "total_billable_locations": total_billable,
        }
    except Exception as e:
        logger.error(f"Error fetching platform branch changes: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch branch changes: {e}")


async def _fetch_daily_usage_map(usage_date=None) -> dict:
    """{clinic_id: usage row} for one Asia/Kolkata day, whole fleet, one query.

    Returns {} on failure: the leaderboard renders without today's counters
    rather than 500-ing the owner's home page over a usage widget.
    """
    day = (usage_date or ist_today()).isoformat()
    try:
        res = (
            # unscoped: platform_admin
            await sb(supabase.table("clinic_daily_usage")
            .select("clinic_id, usage_date, reports_delivered_count, "
                    "prescriptions_sent_count, reminders_sent_count, "
                    "followups_sent_count, total_outbound_count")
            .eq("usage_date", day))
        )
        return {r["clinic_id"]: r for r in (res.data or []) if r.get("clinic_id")}
    except Exception as e:
        logger.warning(f"Daily usage map lookup failed for {day}: {e}")
        return {}


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
            # unscoped: platform_admin
            await sb(supabase.table("clinics")
            .select("id, name, whatsapp_number, plan, is_active, created_at, "
                    "daily_report_limit, subscription_start_date, subscription_end_date, "
                    "grace_period_days, subscription_status, last_renewed_at"))
        )
        clinics = clinics_res.data or []
        start_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        # Today's report counters for the whole fleet in ONE query. Per-clinic
        # reads here would be one extra round-trip per hospital on the busiest
        # page in the console.
        usage_today = await _fetch_daily_usage_map()

        # Roster snapshot: active doctors + distinct departments per clinic.
        # One paginated scan of `doctors` rather than two extra queries per
        # clinic — this is the sizing signal the owner prices plans off, so it
        # must be exact, not truncated at PostgREST's default 1000-row cap.
        # Informational only: if it fails, the leaderboard still renders.
        roster = await _fetch_clinic_roster_counts()
        # Branch census: the platform bills per location, so the owner needs the
        # count and any recent movement in the same table they price plans off.
        branch_census = await _fetch_clinic_branch_counts()

        async def fetch_clinic_metrics(c: dict) -> dict:
            clinic_id = c["id"]
            
            # Fetch appointments for this clinic in last 30d
            appts_res = (
                # unscoped: platform_admin
                await sb(supabase.table("appointments")
                .select("id, status, amount_paise, payment_id, created_at")
                .eq("clinic_id", clinic_id)
                .gte("created_at", start_30d))
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
                # unscoped: platform_admin
                await sb(supabase.table("patients")
                .select("id", count="exact")
                .eq("clinic_id", clinic_id))
            )
            patients_count = pat_res.count if pat_res.count is not None else len(pat_res.data or [])

            # Last activity calculation (latest appointment created_at or clinic created_at)
            last_activity = c.get("created_at")
            if appts:
                latest_appt_time = max(a.get("created_at", "") for a in appts if a.get("created_at"))
                if latest_appt_time and latest_appt_time > last_activity:
                    last_activity = latest_appt_time

            clinic_roster = roster.get(clinic_id) or {}
            clinic_departments = sorted(clinic_roster.get("departments") or ())
            branches = branch_census.get(clinic_id) or {}
            active_branches = branches.get("active", 0)

            reports_today = (usage_today.get(clinic_id) or {}).get(
                "reports_delivered_count", 0
            )

            return {
                "id": clinic_id,
                "name": c.get("name"),
                "whatsapp_number": c.get("whatsapp_number"),
                "plan": c.get("plan"),
                "is_active": c.get("is_active", True),
                "created_at": c.get("created_at"),
                "subscription": compute_subscription_state(c),
                "daily_reports": limit_state(c.get("daily_report_limit"), reports_today),
                "doctors_count": clinic_roster.get("doctors", 0),
                "departments_count": len(clinic_departments),
                "departments": clinic_departments,
                "branches_count": active_branches,
                "diagnostic_branches_count": branches.get("diagnostic", 0),
                "billable_locations": _billable_locations(active_branches),
                "branches_added_30d": branches.get("added_recently", 0),
                "newest_branch_at": branches.get("newest_at"),
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
            # unscoped: platform_admin
            await sb(supabase.table("clinics")
            .select("id, name, whatsapp_number, plan, features, is_active, created_at, "
                    "daily_report_limit, subscription_start_date, subscription_end_date, "
                    "grace_period_days, subscription_status, last_renewed_at")
            .eq("id", clinic_id))
        )
        if not clinic_res.data:
            raise HTTPException(status_code=404, detail="Clinic not found")

        clinic = clinic_res.data[0]
        stats = await analytics_service.get_dashboard_stats(clinic_id=clinic_id, days=30)

        from app.services.subscription import get_clinic_status

        return {
            "success": True,
            "clinic": clinic,
            "analytics": stats,
            "lifecycle": await get_clinic_status(clinic),
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
        # unscoped: platform super-admin fetching feature flags for specified clinic_id
        await sb(supabase.table("clinics").select("features").eq("id", clinic_id))
    )
    if not clinic_res.data:
        raise HTTPException(status_code=404, detail="Clinic not found")

    features = dict(clinic_res.data[0].get("features") or {})
    if body.enabled is None:
        features.pop(body.feature, None)
    else:
        features[body.feature] = body.enabled

    result = (
        # unscoped: platform super-admin updating feature flags for specified clinic_id
        await sb(supabase.table("clinics")
        .update({"features": features})
        .eq("id", clinic_id))
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
            # unscoped: platform_admin
            await sb(supabase.table("appointments")
            .select("clinic_id, status, amount_paise, created_at, payment_id")
            .gte("created_at", start_date))
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
            # unscoped: platform super-admin querying system-wide analytics events
            await sb(supabase.table("analytics_events")
            .select("clinic_id, event_type, created_at")
            .gte("created_at", start_date))
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
                # platform-scoped: platform activity log from processed messages
        # unscoped: platform super-admin querying cross-tenant recent message activity log
        await sb(supabase.table("processed_messages")
                .select("created_at")
                .gte("created_at", start_date))
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

        # unscoped: platform super-admin listing clinics for cross-tenant appointment volume report
        clinics_res = await sb(supabase.table("clinics").select("id, name"))
        clinic_names = {c["id"]: c["name"] for c in (clinics_res.data or [])}

        appts_res = (
            # unscoped: platform super-admin listing clinics for cross-tenant appointment volume report
            await sb(supabase.table("appointments")
            .select("clinic_id, department, created_at")
            .gte("created_at", start_date))
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

    # platform-scoped: fetch callmedex whatsapp settings
    res = (
        # unscoped: platform super-admin fetching CallMedex WhatsApp settings for target clinic
        await sb(supabase.table("callmedex_whatsapp_settings")
        .select("phone_number_id, api_token_encrypted, updated_at, updated_by")
        .eq("id", "default"))
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
        # platform-scoped: read callmedex whatsapp settings
        # unscoped: platform super-admin fetching CallMedex WhatsApp settings for target clinic
        await sb(supabase.table("callmedex_whatsapp_settings")
        .select("*")
        .eq("id", "default"))
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

    # platform-scoped: upsert callmedex whatsapp settings
    # unscoped: platform super-admin fetching CallMedex WhatsApp settings for target clinic
    await sb(supabase.table("callmedex_whatsapp_settings").upsert(row))

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
            # unscoped: platform super-admin monitoring connector health across all clinics
            await sb(supabase.table("integration_connectors")
            .select(
                "id, clinic_id, connector_type, is_enabled, last_run_at, last_success_at, last_error"
            )
            .eq("is_enabled", True))
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
            # unscoped: platform_admin
            await sb(supabase.table("clinics")
            .select("id, name, whatsapp_number, is_active")
            .in_("id", clinic_ids))
        )
        clinics_by_id = {c["id"]: c for c in (clinics_res.data or [])}

        start_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        reports_res = (
            # unscoped: platform super-admin querying recent lab reports across all connectors
            await sb(supabase.table("lab_reports")
            .select("clinic_id, uploaded_at")
            .eq("source", "callmedex")
            .in_("clinic_id", clinic_ids))
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


# ═══════ MESSAGING USAGE & BILLING (OWNER-ONLY, FULL FINANCIAL VISIBILITY) ═══════
# These endpoints are OWNER-ONLY. They return full Meta pricing, cost estimates,
# and financial breakdowns. The customer-facing equivalent is GET /admin/messaging-usage.


@router.get("/messaging-usage")
async def get_platform_messaging_usage(
    request: Request,
    days: int = 30,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Platform-wide outbound message usage with full financial breakdown.

    Reads actual per-message counts from outbound_message_ledger and pricing
    from meta_pricing_config. This replaces the old appointment-estimation approach.

    OWNER-ONLY: Returns Meta cost estimates, pricing rates, per-clinic
    financial detail. This data MUST NEVER be exposed to clinic-facing APIs.
    """
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_platform_messaging_usage",
        resource_type="platform",
        ip_address=client_ip,
    )

    try:
        from app.services.message_accounting import get_platform_usage

        usage = await get_platform_usage(days=days)
        return usage

    except Exception as e:
        logger.error(f"Error fetching platform messaging usage: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch messaging usage: {e}")


@router.get("/pricing")
async def get_pricing_config(
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Get current Meta messaging pricing configuration.

    OWNER-ONLY. Returns per-category cost rates in paise and INR.
    """
    try:
        # platform-scoped: fetch meta pricing config
        result = (
            # unscoped: platform super-admin reading global Meta pricing configuration
            await sb(supabase.table("meta_pricing_config")
            .select("*")
            .eq("id", "default"))
        )
        if not result.data:
            return {"error": "No pricing config found. Run migration 033."}

        config = result.data[0]
        return {
            "utility_paise": config.get("utility_paise", 12),
            "marketing_paise": config.get("marketing_paise", 75),
            "authentication_paise": config.get("authentication_paise", 10),
            "service_paise": config.get("service_paise", 0),
            "utility_inr": round(config.get("utility_paise", 12) / 100, 2),
            "marketing_inr": round(config.get("marketing_paise", 75) / 100, 2),
            "authentication_inr": round(config.get("authentication_paise", 10) / 100, 2),
            "service_inr": round(config.get("service_paise", 0) / 100, 2),
            "currency": config.get("currency", "INR"),
            "effective_from": config.get("effective_from"),
            "updated_at": config.get("updated_at"),
            "updated_by": config.get("updated_by"),
        }
    except Exception as e:
        logger.error(f"Error fetching pricing config: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch pricing config")


@router.put("/pricing")
async def update_pricing_config(
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Update Meta messaging pricing configuration.

    OWNER-ONLY. All values in integer paise. Invalidates pricing cache.
    """
    body = await request.json()

    # Validate — all paise values must be non-negative integers
    allowed_fields = {"utility_paise", "marketing_paise", "authentication_paise", "service_paise"}
    update_data = {}
    for field in allowed_fields:
        if field in body:
            val = body[field]
            if not isinstance(val, int) or val < 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"{field} must be a non-negative integer (paise)",
                )
            update_data[field] = val

    if not update_data:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    update_data["updated_by"] = owner.username
    update_data["effective_from"] = datetime.now(timezone.utc).isoformat()
    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        # platform-scoped: update meta pricing config
        # unscoped: platform super-admin reading global Meta pricing configuration
        await sb(supabase.table("meta_pricing_config").update(update_data).eq("id", "default"))

        # Invalidate the in-memory cache
        from app.services.message_accounting import invalidate_pricing_cache
        invalidate_pricing_cache()

        await log_admin_action(
            user=owner,
            action="update_pricing_config",
            resource_type="platform",
            details=update_data,
        )

        return {"success": True, "updated_fields": list(update_data.keys())}

    except Exception as e:
        logger.error(f"Error updating pricing config: {e}")
        raise HTTPException(status_code=500, detail="Failed to update pricing config")


@router.get("/plan-tiers")
async def get_plan_tiers(
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Get all plan tier configurations with quotas, bundled features, and adoption.

    OWNER-ONLY. `features` per plan is read straight from PLAN_FEATURES
    (app/services/tenant.py) — the same registry has_feature() gates the bot
    on — so the dashboard's "what's in this plan" widget can never drift from
    what a clinic on that plan actually gets. `clinics_count` is live adoption
    per tier, for pricing decisions.
    """
    from app.services.tenant import ALL_FEATURES, FEATURE_LABELS, PLAN_FEATURES

    try:
        # platform-scoped: fetch plan tiers
        result = (
            # unscoped: platform super-admin querying subscription plan tiers
            await sb(supabase.table("plan_tiers")
            .select("*")
            .order("included_messages_month"))
        )
        tiers = result.data or []

        # Live adoption per plan. Best-effort — a failure here must not hide
        # the plan/feature matrix itself.
        adoption: dict[str, int] = {}
        try:
            clinics_res = (
                # unscoped: platform_admin
                await sb(supabase.table("clinics").select("plan, is_active"))
            )
            for c in clinics_res.data or []:
                if c.get("is_active") is False:
                    continue
                adoption[c.get("plan") or "soloclinic"] = (
                    adoption.get(c.get("plan") or "soloclinic", 0) + 1
                )
        except Exception as e:
            logger.warning(f"Plan adoption counts unavailable: {e}")

        for t in tiers:
            plan_name = t.get("plan_name")
            plan_features = PLAN_FEATURES.get(plan_name, set())
            includes_all = "*" in plan_features
            t["includes_all_features"] = includes_all
            t["features"] = list(ALL_FEATURES) if includes_all else sorted(plan_features)
            t["clinics_count"] = adoption.get(plan_name, 0)

        return {
            "plan_tiers": tiers,
            "all_features": ALL_FEATURES,
            "feature_labels": FEATURE_LABELS,
        }
    except Exception as e:
        logger.error(f"Error fetching plan tiers: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch plan tiers")


@router.put("/plan-tiers/{plan_name}")
async def update_plan_tier(
    plan_name: str,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Update a plan tier's message quota or pricing.

    OWNER-ONLY. Allows updating included_messages_month, overage_price_paise,
    monthly_price_paise, and display_name without code changes.
    """
    valid_plans = {"soloclinic", "diagstream", "essential", "polyclinic", "enterprise"}
    if plan_name not in valid_plans:
        raise HTTPException(status_code=400, detail=f"Invalid plan: {plan_name}")

    body = await request.json()
    allowed = {"display_name", "monthly_price_paise", "included_messages_month", "overage_price_paise", "is_active"}
    update_data = {}
    for field in allowed:
        if field in body:
            val = body[field]
            if field in ("monthly_price_paise", "included_messages_month", "overage_price_paise"):
                if not isinstance(val, int) or val < 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{field} must be a non-negative integer",
                    )
            update_data[field] = val

    if not update_data:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    try:
        # platform-scoped: update plan tier configuration
        result = (
            # unscoped: platform super-admin querying subscription plan tiers
            await sb(supabase.table("plan_tiers")
            .update(update_data)
            .eq("plan_name", plan_name))
        )

        # Invalidate the plan tiers cache
        from app.services.message_accounting import invalidate_plan_tiers_cache
        invalidate_plan_tiers_cache()

        await log_admin_action(
            user=owner,
            action="update_plan_tier",
            resource_type="platform",
            resource_id=plan_name,
            details=update_data,
        )

        return {"success": True, "plan": plan_name, "updated_fields": list(update_data.keys())}

    except Exception as e:
        logger.error(f"Error updating plan tier {plan_name}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update plan tier")


# ═══════════════════════════════════════════════════════════════════════════════
# PLATFORM OWNER BROADCAST & NOTIFICATION SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════


class BroadcastCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    message: str = Field(..., min_length=1)
    target_type: Literal["ALL", "SELECTIVE", "SINGLE"] = "ALL"
    target_clinic_ids: Optional[List[str]] = None


@router.post("/broadcasts")
async def create_broadcast(
    body: BroadcastCreateRequest,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Platform owner endpoint to create and dispatch system-wide or selective broadcast messages."""
    client_ip = request.client.host if request.client else "unknown"
    try:
        broadcast = await broadcast_service.create_broadcast(
            sender_id=owner.username,
            title=body.title,
            message=body.message,
            target_type=body.target_type,
            target_clinic_ids=body.target_clinic_ids,
        )

        await log_admin_action(
            user=owner,
            action="create_broadcast",
            resource_type="broadcast",
            resource_id=broadcast.get("id"),
            details={
                "title": body.title,
                "target_type": body.target_type,
                "target_clinic_ids": body.target_clinic_ids,
            },
            ip_address=client_ip,
        )

        return {"success": True, "broadcast": broadcast}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Error creating broadcast: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create broadcast: {e}")


@router.get("/broadcasts")
async def list_broadcasts(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Retrieve platform broadcast history with live delivery & read metrics."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_broadcasts",
        resource_type="platform",
        ip_address=client_ip,
    )
    broadcasts = await broadcast_service.get_broadcasts(limit=limit, offset=offset)
    return {"success": True, "broadcasts": broadcasts}


@router.get("/broadcasts/{broadcast_id}")
async def get_broadcast_detail(
    broadcast_id: str,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Get single broadcast record and delivery summary."""
    client_ip = request.client.host if request.client else "unknown"
    summary = await broadcast_service.get_broadcast_by_id(broadcast_id)
    if not summary:
        raise HTTPException(status_code=404, detail="Broadcast not found")

    await log_admin_action(
        user=owner,
        action="view_broadcast_detail",
        resource_type="broadcast",
        resource_id=broadcast_id,
        ip_address=client_ip,
    )
    return {"success": True, **summary}


# ═══════════════════════════════════════════════════════════════════════════════
# SAFE CLINIC LIFECYCLE MANAGEMENT & DELETION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/clinics/{clinic_id}/deletion-preview")
async def get_clinic_deletion_preview(
    clinic_id: str,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Preview entity impact stats before soft-deleting a clinic."""
    clinic_res = (
        # unscoped: platform_admin
        await sb(supabase.table("clinics")
        .select("id, name, whatsapp_number, plan, is_active, status, created_at")
        .eq("id", clinic_id))
    )
    if not clinic_res.data:
        raise HTTPException(status_code=404, detail="Clinic not found")

    clinic = clinic_res.data[0]
    if clinic.get("status") == "DELETED":
        raise HTTPException(status_code=400, detail="Clinic is already deleted")

    doc_res = (
        # unscoped: platform super-admin soft-deleting all doctors for offboarded clinic
        await sb(supabase.table("doctors")
        .select("id", count="exact")
        .eq("clinic_id", clinic_id))
    )
    doctor_count = doc_res.count if doc_res.count is not None else len(doc_res.data or [])

    appt_res = (
        # unscoped: platform super-admin cancelling pending appointments for offboarded clinic
        await sb(supabase.table("appointments")
        .select("id", count="exact")
        .eq("clinic_id", clinic_id))
    )
    appointment_count = appt_res.count if appt_res.count is not None else len(appt_res.data or [])

    pat_res = (
        # unscoped: platform_admin
        await sb(supabase.table("patients")
        .select("id", count="exact")
        .eq("clinic_id", clinic_id))
    )
    patient_count = pat_res.count if pat_res.count is not None else len(pat_res.data or [])

    adm_res = (
        # unscoped: platform super-admin disabling all admin logins for offboarded clinic
        await sb(supabase.table("clinic_admins")
        .select("id", count="exact")
        .eq("clinic_id", clinic_id)
        .eq("is_active", True))
    )
    active_admin_count = adm_res.count if adm_res.count is not None else len(adm_res.data or [])

    return {
        "success": True,
        "clinic": clinic,
        "impact_preview": {
            "doctor_count": doctor_count,
            "appointment_count": appointment_count,
            "patient_count": patient_count,
            "active_admin_count": active_admin_count,
            "note": "Historical appointments and patients will remain immutably preserved for audit compliance.",
        },
    }


@router.delete("/clinics/{clinic_id}")
async def delete_clinic(
    clinic_id: str,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Safe non-destructive soft-deletion of a clinic preserving historical compliance records."""
    client_ip = request.client.host if request.client else "unknown"

    # 1. Fetch clinic
    clinic_res = (
        # unscoped: platform_admin
        await sb(supabase.table("clinics")
        .select("id, name, whatsapp_number, is_active, status")
        .eq("id", clinic_id))
    )
    if not clinic_res.data:
        raise HTTPException(status_code=404, detail="Clinic not found")

    clinic = clinic_res.data[0]
    if clinic.get("status") == "DELETED":
        raise HTTPException(status_code=400, detail="Clinic is already deleted")

    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        # 2. Soft-delete master clinic row
        # unscoped: platform super-admin soft-deleting clinic record
        await sb(supabase.table("clinics").update(
            {
                "status": "DELETED",
                "deleted_at": now_iso,
                "is_active": False,
            }
        ).eq("id", clinic_id))

        # 3. Deactivate all clinic admin accounts for this tenant
        # unscoped: platform super-admin deactivating all admin accounts for clinic
        await sb(supabase.table("clinic_admins").update(
            {"is_active": False}
        ).eq("clinic_id", clinic_id))

        # 4. Deactivate all integration connectors
        # unscoped: platform super-admin deactivating all integration connectors for clinic
        await sb(supabase.table("integration_connectors").update(
            {"is_enabled": False}
        ).eq("clinic_id", clinic_id))

        # 5. Invalidate tenant & branch in-memory caches
        invalidate_tenant_cache(clinic.get("whatsapp_number"))
        invalidate_branch_cache(clinic_id)

        # 6. Audit logging
        await log_admin_action(
            user=owner,
            action="soft_delete_clinic",
            resource_type="clinic",
            resource_id=clinic_id,
            details={
                "clinic_name": clinic.get("name"),
                "whatsapp_number": clinic.get("whatsapp_number"),
                "deleted_at": now_iso,
            },
            ip_address=client_ip,
        )

        return {
            "success": True,
            "message": f"Clinic '{clinic.get('name')}' successfully soft-deleted.",
            "clinic_id": clinic_id,
            "deleted_at": now_iso,
        }

    except Exception as e:
        logger.error(f"Error during soft-delete of clinic {clinic_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete clinic: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Subscription lifecycle & daily report limits (OWNER ONLY)
# ═══════════════════════════════════════════════════════════════════════════


class SubscriptionUpdate(BaseModel):
    """Owner-side edit of a clinic limit tier and subscription window.

    Every field is optional; only what is sent is written. daily_report_limit
    is a fixed tier, enforced here AND by a CHECK constraint in migration 068.
    """

    daily_report_limit: Optional[Literal[0, 50, 100, 200, 300, 500]] = None
    subscription_start_date: Optional[str] = None  # ISO-8601
    grace_period_days: Optional[int] = Field(default=None, ge=0, le=30)
    subscription_status: Optional[Literal["active", "grace_period", "suspended", "trial"]] = None


async def _load_clinic_lifecycle_row(clinic_id: str) -> dict:
    """Fetch the columns the lifecycle needs, or 404. Explicit projection only."""
    try:
        res = (
            # unscoped: platform_admin
            await sb(supabase.table("clinics")
            .select("id, name, plan, is_active, daily_report_limit, "
                    "subscription_start_date, subscription_end_date, "
                    "grace_period_days, subscription_status, last_renewed_at, "
                    "whatsapp_number, phone_number_id")
            .eq("id", clinic_id))
        )
    except Exception as e:
        logger.error(f"Failed to load clinic {clinic_id} for lifecycle: {e}")
        raise HTTPException(status_code=500, detail="Failed to load clinic")
    if not res.data:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return res.data[0]


def _invalidate_clinic(clinic: dict) -> None:
    """Drop the tenant cache so new limits apply within this process.

    The cache is process-local with a 30s TTL, so the other production workers
    converge on their own inside that window — the propagation delay already
    documented in app/services/tenant.py, not a new one.
    """
    try:
        invalidate_tenant_cache(
            whatsapp_number=clinic.get("whatsapp_number"),
            phone_number_id=clinic.get("phone_number_id"),
        )
    except Exception as e:
        logger.warning(f"Tenant cache invalidation failed after lifecycle edit: {e}")


@router.get("/clinics/{clinic_id}/subscription")
async def get_clinic_subscription(
    clinic_id: str,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Effective subscription state + today report-limit state for one clinic."""
    from app.services.subscription import get_clinic_status

    clinic = await _load_clinic_lifecycle_row(clinic_id)
    return {
        "success": True,
        "clinic_id": clinic_id,
        "clinic_name": clinic.get("name"),
        "daily_report_limit_tiers": list(DAILY_REPORT_LIMIT_TIERS),
        **await get_clinic_status(clinic),
    }


@router.patch("/clinics/{clinic_id}/subscription")
async def update_clinic_subscription(
    clinic_id: str,
    body: SubscriptionUpdate,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Set the daily report tier and/or re-date a subscription window.

    Changing subscription_start_date recomputes the end date as start + 30
    days, so the owner never has to keep the two in sync by hand.
    """
    from app.services.subscription import (
        SUBSCRIPTION_PERIOD_DAYS,
        get_clinic_status,
        parse_timestamp,
    )

    clinic = await _load_clinic_lifecycle_row(clinic_id)
    client_ip = request.client.host if request.client else "unknown"

    updates: dict = {}
    if body.daily_report_limit is not None:
        updates["daily_report_limit"] = body.daily_report_limit
    if body.grace_period_days is not None:
        updates["grace_period_days"] = body.grace_period_days
    if body.subscription_status is not None:
        updates["subscription_status"] = body.subscription_status
    if body.subscription_start_date is not None:
        start = parse_timestamp(body.subscription_start_date)
        if start is None:
            raise HTTPException(
                status_code=422,
                detail="subscription_start_date must be an ISO-8601 timestamp",
            )
        updates["subscription_start_date"] = start.isoformat()
        updates["subscription_end_date"] = (
            start + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)
        ).isoformat()

    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    try:
        # unscoped: unique_row_key
        res = await sb(supabase.table("clinics").update(updates).eq("id", clinic_id))
    except Exception as e:
        logger.error(f"Failed to update subscription for {clinic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update subscription")
    if not res.data:
        raise HTTPException(status_code=404, detail="Clinic not found")

    merged = {**clinic, **res.data[0]}
    _invalidate_clinic(merged)

    await log_admin_action(
        user=owner,
        action="update_clinic_subscription",
        resource_type="clinic",
        resource_id=clinic_id,
        details=updates,
        ip_address=client_ip,
    )

    return {
        "success": True,
        "clinic_id": clinic_id,
        "updated": updates,
        **await get_clinic_status(merged),
    }


@router.post("/clinics/{clinic_id}/renew")
async def renew_clinic_subscription(
    clinic_id: str,
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Renew for 30 days, backdated to the previous expiry.

    The new window starts at the OLD end date, not at now, so any grace days
    already consumed come out of the period being paid for — the platform is
    always paid for a full 30 days. Renewal also clears a suspension, which is
    the only thing that does.
    """
    from app.services.subscription import STATUS_ACTIVE, get_clinic_status

    clinic = await _load_clinic_lifecycle_row(clinic_id)
    client_ip = request.client.host if request.client else "unknown"
    now = datetime.now(timezone.utc)

    before = compute_subscription_state(clinic, now=now)
    start, end = renewal_window(clinic, now=now)

    updates = {
        "subscription_start_date": start.isoformat(),
        "subscription_end_date": end.isoformat(),
        "subscription_status": STATUS_ACTIVE,
        "last_renewed_at": now.isoformat(),
    }

    try:
        # unscoped: unique_row_key
        res = await sb(supabase.table("clinics").update(updates).eq("id", clinic_id))
    except Exception as e:
        logger.error(f"Failed to renew clinic {clinic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to renew subscription")
    if not res.data:
        raise HTTPException(status_code=404, detail="Clinic not found")

    merged = {**clinic, **res.data[0]}
    _invalidate_clinic(merged)

    await log_admin_action(
        user=owner,
        action="renew_clinic_subscription",
        resource_type="clinic",
        resource_id=clinic_id,
        details={
            "previous_status": before["status"],
            "previous_end": before["subscription_end_date"],
            "grace_days_consumed": before["grace_day"],
            "new_start": updates["subscription_start_date"],
            "new_end": updates["subscription_end_date"],
        },
        ip_address=client_ip,
    )

    return {
        "success": True,
        "clinic_id": clinic_id,
        "previous_status": before["status"],
        "grace_days_consumed": before["grace_day"],
        **await get_clinic_status(merged, now=now),
    }


@router.get("/subscriptions")
async def get_platform_subscriptions(
    request: Request,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Fleet-wide lifecycle board: who is in grace, who is suspended, and who
    is at or near their daily report limit. The owner alert surface."""
    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_platform_subscriptions",
        resource_type="platform",
        ip_address=client_ip,
    )

    try:
        res = (
            # unscoped: platform_admin
            await sb(supabase.table("clinics")
            .select("id, name, plan, is_active, whatsapp_number, daily_report_limit, "
                    "subscription_start_date, subscription_end_date, grace_period_days, "
                    "subscription_status, last_renewed_at"))
        )
        clinics = res.data or []
    except Exception as e:
        logger.error(f"Failed to fetch clinics for subscription board: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch subscriptions")

    usage = await _fetch_daily_usage_map()

    rows = []
    counts = {"active": 0, "grace_period": 0, "suspended": 0, "trial": 0}
    alerts: dict = {"grace": [], "suspended": [], "limit_warning": [], "limit_blocked": []}

    for c in clinics:
        state = compute_subscription_state(c)
        used = (usage.get(c["id"]) or {}).get("reports_delivered_count", 0)
        limits = limit_state(c.get("daily_report_limit"), used)

        counts[state["status"]] = counts.get(state["status"], 0) + 1

        row = {
            "clinic_id": c["id"],
            "clinic_name": c.get("name"),
            "plan": c.get("plan"),
            "whatsapp_number": c.get("whatsapp_number"),
            "is_active": c.get("is_active", True),
            "last_renewed_at": c.get("last_renewed_at"),
            "subscription": state,
            "daily_reports": limits,
        }
        rows.append(row)

        if state["status"] == "grace_period":
            alerts["grace"].append(row)
        elif state["status"] == "suspended":
            alerts["suspended"].append(row)
        if limits["level"] == "blocked":
            alerts["limit_blocked"].append(row)
        elif limits["level"] == "warning":
            alerts["limit_warning"].append(row)

    # Most urgent first: suspended, then grace by days left, then by usage.
    rank = {"suspended": 0, "grace_period": 1, "trial": 2, "active": 3}
    rows.sort(key=lambda r: (
        rank.get(r["subscription"]["status"], 9),
        r["subscription"].get("grace_days_left") or 99,
        -(r["daily_reports"]["percent"] or 0),
    ))

    return {
        "success": True,
        "usage_date": ist_today().isoformat(),
        "counts": counts,
        "alerts": alerts,
        "daily_report_limit_tiers": list(DAILY_REPORT_LIMIT_TIERS),
        "clinics": rows,
    }


@router.get("/outbound-audit")
async def get_outbound_audit(
    request: Request,
    clinic_id: Optional[str] = None,
    source_class: Optional[str] = None,
    days: int = 7,
    limit: int = 200,
    owner: AdminUser = Depends(verify_owner_credentials),
):
    """Granular per-message outbound audit feed.

    OWNER-ONLY — carries estimated Meta cost per message. Each entry names the
    recipient phone, the patient, what kind of message it was, whether Meta
    accepted it, and when.
    """
    from app.services.message_accounting import OUTBOUND_CLASSES, get_outbound_audit_feed

    if source_class and source_class not in OUTBOUND_CLASSES:
        raise HTTPException(
            status_code=422,
            detail=f"source_class must be one of {', '.join(OUTBOUND_CLASSES)}",
        )

    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=owner,
        action="view_outbound_audit",
        resource_type="platform",
        resource_id=clinic_id,
        details={"source_class": source_class, "days": days},
        ip_address=client_ip,
    )

    return await get_outbound_audit_feed(
        clinic_id=clinic_id, source_class=source_class, days=days, limit=limit
    )
