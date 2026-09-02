"""Admin router for analytics and management — Security Hardened."""

import asyncio
import csv
import hashlib
import io
import logging
import re
import secrets
from datetime import date, datetime, time as time_type, timedelta, timezone
from typing import Literal, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
    UploadFile,
    File,
    Form,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings
from app.database import (
    is_valid_clinic_scope,
    supabase,
    check_in_appointment,
    call_next_patient,
    get_patient_queue_status,
    get_genuine_patients,
    invalidate_doctor_cache,
    invalidate_holiday_cache,
)
from app.services.tenant import (
    ALL_FEATURES,
    CANCELLATION_WINDOW_CHOICES,
    cancellation_window_hours,
    get_clinic_by_id,
    has_feature,
    invalidate_tenant_cache,
    require_feature,
)
from app.services.analytics import analytics_service
from app.services.broadcast import broadcast_service
from app.services.lab_reports import LabReportService
from app.services.permissions import (
    enforce_branch_scope,
    require_permission,
    resolve_owned_branch,
)
from app.services.prescriptions import PrescriptionService
from app.utils.security import login_rate_limiter
from app.utils.validators import normalize_phone, validate_phone
from app.database import sb  # T5.1: off-loop query execution

logger = logging.getLogger(__name__)

# Every clinic on this platform operates in India; the scheduler already
# pins Asia/Kolkata. Dashboard "today" must use the same day boundary.
CLINIC_TZ = ZoneInfo("Asia/Kolkata")

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic(auto_error=False)


class AdminUser(str):
    """Authenticated admin user with RBAC role, clinic scope, staff user ID,
    delegated permissions, optional branch scope, and optional staff role."""

    username: str
    role: str
    clinic_id: Optional[str]
    user_id: Optional[str]
    permissions: list[str]
    branch_id: Optional[str]
    staff_role: Optional[str]

    def __new__(
        cls,
        username: str,
        role: str = "clinic_admin",
        clinic_id: Optional[str] = None,
        user_id: Optional[str] = None,
        permissions: Optional[list[str]] = None,
        branch_id: Optional[str] = None,
        staff_role: Optional[str] = None,
    ):
        obj = super().__new__(cls, username)
        obj.username = username
        obj.role = role
        obj.clinic_id = clinic_id
        obj.user_id = user_id
        obj.permissions = permissions or []
        obj.branch_id = branch_id
        obj.staff_role = staff_role
        return obj

    def can_access_clinic(self, target_clinic_id: str) -> bool:
        """Tenant boundary check. Fails CLOSED.

        An admin row with no clinic_id that is not a super_admin is a
        misconfigured account, not a platform account. Before migration 051,
        clinic_id was nullable for every role, so an unscoped 'staff' row could
        read the data of every tenant (KRIYA-002).

        Do NOT restore the `if not self.clinic_id: return True` branch. It is
        the exact hole that the chk_admin_scope constraint in 051 now prevents.
        """
        if self.role == "super_admin":
            return True
        if not self.clinic_id:
            # KA-03: Unscoped non-super-admin is ALWAYS denied.
            # Shadow mode has been removed — fail closed.
            logger.error(
                f"TENANT_SCOPE_DENIED user='{self.username}' "
                f"role={self.role} target='{target_clinic_id}' "
                f"— unscoped non-super-admin account"
            )
            return False
        if target_clinic_id == "default":
            return True              # caller resolves 'default' -> self.clinic_id
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
                # unscoped: logging administrative action into admin_audit_logs table
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
    """Check plain password against stored bcrypt hash. Fails closed on plaintext or malformed hashes."""
    if not stored_hash or not plain_password:
        return False
    if stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$") or stored_hash.startswith("$2y$"):
        try:
            import bcrypt

            return bcrypt.checkpw(
                plain_password.encode("utf-8"), stored_hash.encode("utf-8")
            )
        except (Exception, BaseException):
            return False
    # Fail closed: non-bcrypt stored credentials fail authentication
    return False


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password with bcrypt for storage in clinic_admins.password_hash."""
    import bcrypt

    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


#: Name of the HttpOnly cookie carrying the admin session token.
ADMIN_SESSION_COOKIE = "kriya_admin_session"


def _hash_session_token(token: str) -> str:
    """SHA-256 of a session token. Only the hash is ever stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _authenticate_password(
    username: str, password: str, client_ip: str
) -> AdminUser:
    """Validate a username/password pair and return the authorized user.

    Shared by the session-login route and the legacy HTTP Basic fallback so
    that brute-force accounting, the clinic_admins lookup, the env super-admin
    fallback and the database-outage 503 behave identically on both paths.
    Raises HTTPException on any failure.
    """
    if await asyncio.to_thread(login_rate_limiter.is_rate_limited, client_ip):
        logger.warning(f"Admin login rate limit exceeded - IP={client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again in 60 seconds.",
            headers={"Retry-After": "60"},
        )

    # 1. Check database clinic_admins table
    _db_unavailable = False
    try:
        res = (
    # unscoped: global_auth_lookup - username is unique platform-wide and the
    # clinic is the RESULT of this lookup, so it cannot be a predicate of it.
            await sb(supabase.table("clinic_admins")
            .select("*")
            .eq("username", username)
            .eq("is_active", True))
        )
        if res.data and len(res.data) > 0:
            user_row = res.data[0]
            if check_password_hash(password, user_row.get("password_hash", "")):
                await asyncio.to_thread(login_rate_limiter.reset, client_ip)
                return AdminUser(
                    username=user_row["username"],
                    role=user_row.get("role", "clinic_admin"),
                    clinic_id=user_row.get("clinic_id"),
                    user_id=user_row.get("id"),
                    permissions=user_row.get("permissions") or [],
                    branch_id=user_row.get("branch_id"),
                    staff_role=user_row.get("staff_role"),
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Database error during admin auth lookup: {e}")
        _db_unavailable = True

    # 2. Fallback to global env credentials (Super Admin)
    username_ok = secrets.compare_digest(
        username.encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )
    if username_ok and password_ok:
        await asyncio.to_thread(login_rate_limiter.reset, client_ip)
        return AdminUser(
            username=username,
            role="super_admin",
            clinic_id=None,
            user_id="super_admin_env",
        )

    # If the database was unreachable, we cannot verify clinic_admin
    # credentials - return 503 instead of penalising the user with a
    # failed-attempt counter bump and a misleading 401.
    if _db_unavailable:
        logger.error(
            f"Admin auth blocked by database outage - IP={client_ip}, user='{username}'"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable. Please try again in a few moments.",
            headers={"Retry-After": "30"},
        )

    # Record failed attempt ONLY on invalid credentials (T3.2b)
    await asyncio.to_thread(login_rate_limiter.record_attempt, client_ip)
    remaining = await asyncio.to_thread(login_rate_limiter.remaining_attempts, client_ip)
    logger.warning(
        f"Failed admin login attempt - IP={client_ip}, user='{username}', remaining={remaining}"
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


async def create_admin_session(
    user: AdminUser, ip_address: str, user_agent: str
) -> Optional[str]:
    """Mint a session token and persist its hash. Returns None if storage failed.

    A None return is not fatal: the caller tells the client to keep using HTTP
    Basic, so a missing admin_sessions table (migration 067 not yet applied)
    degrades to the previous behaviour rather than locking every admin out.
    """
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.admin_session_hours
    )
    row = {
        "token_hash": _hash_session_token(token),
        "username": user.username,
        "role": user.role,
        "clinic_id": user.clinic_id,
        "user_id": str(user.user_id) if user.user_id else None,
        "branch_id": user.branch_id,
        "staff_role": user.staff_role,
        "permissions": list(user.permissions or []),
        "expires_at": expires_at.isoformat(),
        "ip_address": ip_address,
        "user_agent": (user_agent or "")[:500],
    }
    try:
        # unscoped: insert_scoped_by_payload — admin_sessions is a platform
        # table, not a tenant one; the row carries the session's clinic_id
        # snapshot and is only ever read back by its unique token hash.
        await sb(supabase.table("admin_sessions").insert(row))
        return token
    except Exception as e:
        logger.error(f"Could not create admin session for '{user.username}': {e}")
        return None


async def resolve_admin_session(token: str) -> Optional[AdminUser]:
    """Look up a live session by token. Returns None if absent/expired/revoked."""
    try:
        res = (
    # unscoped: global_auth_lookup - a session token is globally unique and the
    # clinic is the RESULT of resolving it.
            await sb(supabase.table("admin_sessions")
            .select("*")
            .eq("token_hash", _hash_session_token(token))
            .is_("revoked_at", "null"))
        )
    except Exception as e:
        logger.warning(f"Admin session lookup failed: {e}")
        return None

    if not res.data:
        return None
    row = res.data[0]

    try:
        expires_at = datetime.fromisoformat(
            str(row["expires_at"]).replace("Z", "+00:00")
        )
    except (ValueError, KeyError, TypeError):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return None

    return AdminUser(
        username=row["username"],
        role=row.get("role", "clinic_admin"),
        clinic_id=row.get("clinic_id"),
        user_id=row.get("user_id"),
        permissions=row.get("permissions") or [],
        branch_id=row.get("branch_id"),
        staff_role=row.get("staff_role"),
    )


async def revoke_admin_session(token: str) -> None:
    """Revoke a single session. Idempotent; never raises."""
    try:
        await sb(
    # unscoped: global_auth_lookup - revocation is keyed on the globally unique
    # session token hash.
            supabase.table("admin_sessions")
            .update({"revoked_at": datetime.now(timezone.utc).isoformat()})
            .eq("token_hash", _hash_session_token(token))
            .is_("revoked_at", "null")
        )
    except Exception as e:
        logger.warning(f"Admin session revoke failed: {e}")


async def revoke_sessions_for_user(username: str) -> None:
    """Revoke every live session for a username.

    This is the point of holding sessions server-side: with HTTP Basic there
    was no way to cut off a credential that was already in someone's hands.
    """
    try:
        await sb(
    # unscoped: global_auth_lookup - username is unique platform-wide.
            supabase.table("admin_sessions")
            .update({"revoked_at": datetime.now(timezone.utc).isoformat()})
            .eq("username", username)
            .is_("revoked_at", "null")
        )
    except Exception as e:
        logger.warning(f"Bulk session revoke failed for '{username}': {e}")


async def verify_credentials(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> AdminUser:
    """Authenticate an admin request.

    Order: session cookie first, then HTTP Basic. Basic is retained so existing
    API clients and scripts keep working, and so a deployment where migration
    067 has not been applied still authenticates.
    """
    session_token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if session_token:
        session_user = await resolve_admin_session(session_token)
        if session_user is not None:
            return session_user
        # A presented-but-dead cookie is an expired or revoked session, not an
        # anonymous request. Say so rather than falling through to a Basic
        # challenge the browser would answer from its own credential cache.
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired. Please sign in again.",
            )

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Basic"},
        )

    client_ip = request.client.host if request.client else "unknown"
    return await _authenticate_password(
        credentials.username, credentials.password, client_ip
    )


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=500)


@router.post("/login")
async def admin_login(body: AdminLoginRequest, request: Request, response: Response):
    """Exchange credentials for a revocable, expiring HttpOnly session cookie."""
    client_ip = request.client.host if request.client else "unknown"
    user = await _authenticate_password(body.username, body.password, client_ip)

    token = await create_admin_session(
        user, client_ip, request.headers.get("user-agent", "")
    )
    if token:
        response.set_cookie(
            key=ADMIN_SESSION_COOKIE,
            value=token,
            max_age=settings.admin_session_hours * 3600,
            httponly=True,  # unreachable from JS, so XSS cannot exfiltrate it
            secure=settings.app_env == "production",
            samesite="strict",  # admin panel is same-origin: this is also the CSRF control
            path="/",
        )

    await log_admin_action(
        user=user,
        action="admin_login",
        resource_type="session",
        resource_id=user.username,
        details={"session": bool(token)},
        ip_address=client_ip,
    )

    return {
        "success": True,
        "username": user.username,
        "role": user.role,
        "clinic_id": user.clinic_id,
        # False means migration 067 is not applied and the client must keep
        # sending HTTP Basic. The panel checks this.
        "session": bool(token),
    }


@router.post("/logout")
async def admin_logout(request: Request, response: Response):
    """Revoke the current session server-side and clear the cookie."""
    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if token:
        await revoke_admin_session(token)
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    return {"success": True}


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
    """Resolve the ONE clinic this /admin request operates on, or refuse.

    Returns a real clinic id. NEVER returns the "default" sentinel, an empty
    string, or None.

    Why this function fails closed (KRIYA-TENANT-001, production incident
    2026-09-01)
    ------------------------------------------------------------------
    This used to return the literal string "default" for a super_admin with no
    clinic_id. Every caller then did:

        query = supabase.table("doctors").select("*")
        query = query.eq("clinic_id", effective_clinic_id)

    so "default" meant NO WHERE CLAUSE — a silent cross-tenant wildcard, on
    reads AND on writes AND on deletes. A super_admin who opened the admin
    panel believing it showed one clinic was in fact looking at every tenant's
    doctors at once, and `DELETE /admin/doctors/{id}` deleted rows by primary
    key with no tenant predicate at all. That is how a live clinic's entire
    doctor roster was destroyed from what looked like a test-clinic panel, and
    why its patients stopped seeing doctors on WhatsApp.

    The /admin panel is a SINGLE-TENANT surface. There is no "all clinics"
    mode here — cross-tenant reporting lives in /platform, which authenticates
    separately. A super_admin must therefore name the clinic they are acting
    on (?clinic_id=<uuid>); an unspecified scope is an error, not a wildcard.

    Do NOT reintroduce a branch that returns "default", "" or None. Callers
    interpolate this straight into a tenant predicate.
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
        if not is_valid_clinic_scope(requested_clinic_id) and user.clinic_id:
            requested_clinic_id = user.clinic_id

    if not is_valid_clinic_scope(requested_clinic_id):
        # Only reachable for a super_admin (every other role is already denied
        # by can_access_clinic). Make them pick a clinic instead of silently
        # operating on all of them.
        logger.warning(
            "TENANT_SCOPE_REQUIRED user=%s role=%s - /admin call with no clinic scope",
            getattr(user, "username", user),
            getattr(user, "role", "?"),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No clinic selected. Choose a clinic before using the admin "
                "panel (super-admin accounts must pass ?clinic_id=<clinic id>)."
            ),
        )

    return str(requested_clinic_id).strip()


async def resolve_clinic_id_for_write(
    user: AdminUser, requested_clinic_id: str = "default"
) -> str:
    """Resolve the clinic_id to stamp on a row that is about to be INSERTed.

    Thin alias for enforce_clinic_access(), kept because ~20 write paths call
    it by this name and the name documents intent at the call site.

    It used to fall back to "the first clinic in the clinics table by
    created_at" when the scope was unresolved. That silently wrote one
    tenant's new doctors, leaves and holidays into a DIFFERENT tenant — the
    write-side twin of the read-side wildcard described in
    enforce_clinic_access(). There is no safe guess here: an unresolved scope
    is now a 400.
    """
    return enforce_clinic_access(user, requested_clinic_id)


@router.get("/clinics")
async def list_admin_clinics(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Clinics this account may act on, for the panel's clinic selector.

    /admin is single-tenant: every request operates on exactly one clinic
    (see enforce_clinic_access). A super_admin has no clinic of their own, so
    without this they had no way to name one — which is how "no clinic
    selected" silently became "every clinic at once".
    """
    if user.role != "super_admin":
        # Honours the tenant boundary like every other /admin route: asking
        # for someone else's clinic is a 403, not a filtered-down 200.
        scope = enforce_clinic_access(user, clinic_id)
        return {"clinics": [{"id": scope, "name": None}]}

    rows = (
        # unscoped: platform_sweep — super_admin choosing which tenant to act on
        await sb(supabase.table("clinics")
        .select("id, name, whatsapp_number, is_sandbox, status")
        .neq("status", "DELETED")
        .order("name")
        .limit(500))
    )
    return {
        "clinics": [
            {
                "id": c["id"],
                "name": c.get("name") or "Clinic",
                "whatsapp_number": c.get("whatsapp_number"),
                "is_sandbox": bool(c.get("is_sandbox")),
            }
            for c in (rows.data or [])
        ]
    }


@router.get("/me")
async def get_current_admin(
    clinic_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Return the caller's identity plus their clinic's plan and resolved
    feature set, so the admin panel frontend can show/hide tabs without
    duplicating the PLAN_FEATURES registry in JS."""
    base_response = {
        "username": user.username,
        "role": user.role,
        "clinic_id": user.clinic_id,
        "permissions": user.permissions,
        "branch_id": user.branch_id,
        "staff_role": user.staff_role,
        # Env-credential principals have no clinic_admins row, so they cannot
        # change their own username or password. The panel hides those actions
        # rather than offering a button that can only ever return a 400.
        "env_account": user.user_id in ("super_admin_env", "platform_owner_env"),
    }
    # A super_admin has no clinic of their own, but the panel makes them pick
    # one to act on. Without a scope they get features=None and the sidebar
    # shows every tab — including Doctor Leaves on a diagnostics-only tenant.
    # When they name the tenant, answer for that tenant.
    scoped_clinic_id = user.clinic_id
    if not scoped_clinic_id and clinic_id and is_valid_clinic_scope(clinic_id):
        scoped_clinic_id = enforce_clinic_access(user, clinic_id)

    if not scoped_clinic_id:
        return {
            **base_response,
            "plan": None,
            "features": None,
        }

    clinic = await get_clinic_by_id(scoped_clinic_id)
    plan = clinic.get("plan", "soloclinic")
    features = (
        list(ALL_FEATURES)
        if plan == "enterprise"
        else [f for f in ALL_FEATURES if has_feature(clinic, f)]
    )
    return {
        **base_response,
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

    # unscoped: caller self-password change by user.id
    res = (
    # unscoped: login authentication by username
        await sb(supabase.table("clinic_admins")
        .select("id, password_hash")
        .eq("id", user.user_id))
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Admin account not found")

    if not check_password_hash(body.current_password, res.data[0]["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    # unscoped: caller self-password update by user.id
    await sb(supabase.table("clinic_admins").update(
        {"password_hash": hash_password(body.new_password)}
    ).eq("id", user.user_id))

    # A password change must invalidate credentials already in circulation,
    # which is the entire reason sessions are held server-side (AUDIT-P1-2).
    await revoke_sessions_for_user(user.username)

    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=user,
        action="change_password",
        resource_type="clinic_admin",
        resource_id=user.user_id,
        ip_address=client_ip,
    )

    return {"success": True, "message": "Password updated successfully"}


#: Usernames a database account may never take. Both belong to env-credential
#: principals that have no clinic_admins row, and _authenticate_password()
#: checks the database BEFORE the env fallback — letting a tenant account
#: occupy one of these names would put a tenant-controlled row in front of a
#: platform credential on the login path.
def _reserved_usernames() -> set[str]:
    return {
        (settings.admin_username or "").strip().lower(),
        (settings.owner_username or "").strip().lower(),
    } - {""}


class ChangeUsernameRequest(BaseModel):
    """Self-service rename. The current password is required because a rename
    is a credential change: without it, anyone holding a live session cookie
    could rename the account and lock the real owner out of their own panel.
    """

    current_password: str
    new_username: str = Field(..., min_length=3, max_length=64)

    @field_validator("new_username")
    @classmethod
    def _validate_username(cls, v: str) -> str:
        v = (v or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", v):
            raise ValueError(
                "Username may use letters, numbers, dot, underscore and hyphen, "
                "and must start and end with a letter or number."
            )
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters.")
        return v


@router.put("/change-username")
async def change_username(
    body: ChangeUsernameRequest,
    request: Request,
    user: AdminUser = Depends(verify_credentials),
):
    """Self-service username change for DB-backed clinic_admins accounts.

    Clinic logins are auto-provisioned at onboarding as
    `<clinic-name-slug><6 hex>` (app/routers/clinics.py), e.g.
    "visakhamultispeciala3f9c1" — correct, unique, and unusable by a human at
    a reception desk. This lets the account pick its own name.

    Every live session for the OLD name is revoked. That is not just hygiene:
    admin_sessions stores a SNAPSHOT of the username and resolve_admin_session()
    rebuilds AdminUser from it, so a session left alive across a rename would
    keep asserting the old identity in audit logs, and a later
    revoke_sessions_for_user(new_name) would not match it. The caller is signed
    out and signs back in under the new name.
    """
    if not user.user_id or user.user_id in ("super_admin_env", "platform_owner_env"):
        raise HTTPException(
            status_code=400,
            detail=(
                "This account's username comes from an environment variable and "
                "can't be changed here. Contact your platform administrator."
            ),
        )

    new_username = body.new_username
    if new_username.lower() in _reserved_usernames():
        raise HTTPException(status_code=409, detail="That username is reserved.")

    # The caller's own row, by primary key.
    # unscoped: unique_row_key
    res = (
        await sb(supabase.table("clinic_admins")
        .select("id, username, password_hash")
        .eq("id", user.user_id))
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Admin account not found")
    row = res.data[0]
    old_username = row["username"]

    if not check_password_hash(body.current_password, row.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    if new_username == old_username:
        return {
            "success": True,
            "username": old_username,
            "relogin_required": False,
            "message": "That is already your username.",
        }

    # Exact match, deliberately not ILIKE. The username column's UNIQUE index
    # is case-sensitive and the login path matches exactly
    # (_authenticate_password does .eq("username", ...)), so an exact check is
    # precisely the database's own rule — it cannot reject a name the INSERT
    # would have accepted. ILIKE would also treat "_" as a single-character
    # wildcard, and "_" is legal in a username here, so "ab_de" would collide
    # with "abcde" and the user would be told a free name was taken.
    # unscoped: global_auth_lookup
    clash = (
        await sb(supabase.table("clinic_admins")
        .select("id")
        .eq("username", new_username)
        .neq("id", user.user_id)
        .limit(1))
    )
    if clash.data:
        raise HTTPException(status_code=409, detail="That username is already taken.")

    try:
        # The caller's own row, by primary key.
        # unscoped: unique_row_key
        updated = await sb(supabase.table("clinic_admins").update(
            {"username": new_username}
        ).eq("id", user.user_id))
    except Exception as e:
        # The UNIQUE index is the real arbiter — the check above can lose a race
        # with a concurrent rename or a platform-owner account creation.
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(
                status_code=409, detail="That username is already taken."
            )
        logger.error(f"Username change failed for {user.user_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not update username")

    if not updated.data:
        raise HTTPException(status_code=404, detail="Admin account not found")

    await revoke_sessions_for_user(old_username)

    # Everything below is bookkeeping: the rename has already committed and the
    # old sessions are already dead. An exception here would return a 500 to a
    # caller whose username DID change, leaving them signed out, unaware of the
    # new name, and unable to get back in. Match the defensive read used by
    # delete_doctor rather than assuming request is populated.
    client_ip = request.client.host if (request and request.client) else "unknown"
    await log_admin_action(
        user=user,
        action="change_username",
        resource_type="clinic_admin",
        resource_id=user.user_id,
        details={"from": old_username, "to": new_username},
        ip_address=client_ip,
    )

    return {
        "success": True,
        "username": new_username,
        "relogin_required": True,
        "message": "Username updated. Please sign in again with your new username.",
    }


class StaffCreate(BaseModel):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)
    staff_role: str = "STAFF"
    extra_permissions: list[str] = Field(default_factory=list)
    branch_id: Optional[str] = None


class StaffUpdate(BaseModel):
    staff_role: Optional[str] = None
    extra_permissions: Optional[list[str]] = None
    branch_id: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("/staff")
async def list_staff(
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("STAFF_VIEW")),
):
    """List staff accounts for this clinic (no password hashes)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    # unscoped: tenant-scoped operation with verified clinic authorization
    query = supabase.table("clinic_admins").select(
        "id, username, role, staff_role, permissions, branch_id, is_active, created_at"
    ).eq("role", "staff")
    query = query.eq("clinic_id", effective_clinic_id)
    result = await sb(query.order("created_at", desc=True).limit(2000))
    return {"staff": result.data or []}


@router.post("/staff")
async def create_staff(
    body: StaffCreate,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("STAFF_CREATE")),
):
    """Clinic admin self-service: create a staff login scoped to this clinic,
    with a role preset and optional extra delegated permissions / branch scope.
    Role is fixed to 'staff' at the tier level — caller cannot mint clinic_admin/
    super_admin accounts. Grants are capped to the caller's own authority."""
    from app.services.permissions import (
        cap_permissions_to_authority,
        resolve_permissions,
        validate_staff_role,
    )

    effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)

    try:
        validate_staff_role(body.staff_role)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        resolved_permissions = resolve_permissions(body.staff_role, body.extra_permissions)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    final_permissions = cap_permissions_to_authority(
        requested=resolved_permissions,
        granter_permissions=user.permissions,
        granter_role=user.role,
    )

    if body.branch_id:
        branch_check = (
            # unscoped: tenant-scoped operation with verified clinic authorization
            await sb(supabase.table("branches")
            .select("id")
            .eq("id", body.branch_id)
            .eq("clinic_id", effective_clinic_id))
        )
        if not branch_check.data:
            raise HTTPException(
                status_code=400, detail="Selected branch does not belong to your clinic."
            )
        # If caller is branch-scoped staff, they cannot assign to a branch other than their own
        if user.role == "staff" and user.branch_id and str(body.branch_id) != str(user.branch_id):
            raise HTTPException(
                status_code=403, detail="You cannot assign staff to a branch other than your own."
            )
    elif user.role == "staff" and user.branch_id:
        # Branch-scoped staff cannot create tenant-wide (unscoped) staff
        body.branch_id = str(user.branch_id)

    # unscoped: check username global uniqueness
    existing = (
        # unscoped: checking global username uniqueness across all clinic admins
        await sb(supabase.table("clinic_admins")
        .select("id")
        .eq("username", body.username))
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="Username already exists")

    result = (
        # unscoped: inserting new clinic staff member with explicit clinic_id
        await sb(supabase.table("clinic_admins")
        .insert(
            {
                "clinic_id": effective_clinic_id,
                "username": body.username,
                "password_hash": hash_password(body.password),
                "role": "staff",
                "staff_role": body.staff_role,
                "permissions": final_permissions,
                "branch_id": body.branch_id,
                "is_active": True,
            }
        ))
    )
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create staff account")

    client_ip = request.client.host if (request and request.client) else "unknown"
    await log_admin_action(
        user=user,
        action="create_staff",
        resource_type="clinic_admin",
        resource_id=result.data[0]["id"],
        details={"staff_role": body.staff_role, "permissions": final_permissions, "branch_id": body.branch_id},
        ip_address=client_ip,
    )
    return {"success": True, "staff": result.data[0]}


@router.put("/staff/{staff_id}")
async def update_staff(
    staff_id: str,
    body: StaffUpdate,
    request: Request = None,
    user: AdminUser = Depends(require_permission("STAFF_UPDATE")),
):
    """Edit an existing staff account's role, delegated permissions, branch
    scope, or active status. Grants are re-capped to the caller's own
    authority on every edit — a staff granter can never escalate a target
    account past their own permission ceiling."""
    from app.services.permissions import (
        cap_permissions_to_authority,
        resolve_permissions,
        validate_staff_role,
    )

    res = (
    # unscoped: login authentication by username
        await sb(supabase.table("clinic_admins")
        .select("id, clinic_id, role, staff_role, permissions, branch_id, is_active, username")
        .eq("id", staff_id))
    )
    if not res.data or res.data[0]["role"] != "staff":
        raise HTTPException(status_code=404, detail="Staff account not found")
    target = res.data[0]

    enforce_clinic_access(user, target["clinic_id"] or "default")

    # If caller is branch-scoped staff, verify target staff is in their branch
    if user.role == "staff" and user.branch_id:
        if str(target.get("branch_id")) != str(user.branch_id):
            raise HTTPException(
                status_code=403, detail="You cannot edit staff outside your assigned branch."
            )

    update_data: dict = {}
    if body.is_active is not None:
        update_data["is_active"] = body.is_active

    if body.staff_role is not None or body.extra_permissions is not None:
        new_role = body.staff_role or target.get("staff_role") or "CUSTOM_ROLE"
        try:
            validate_staff_role(new_role)
            resolved = resolve_permissions(new_role, body.extra_permissions or [])
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        update_data["staff_role"] = new_role
        update_data["permissions"] = cap_permissions_to_authority(
            requested=resolved,
            granter_permissions=user.permissions,
            granter_role=user.role,
        )

    if body.branch_id is not None:
        if body.branch_id:
            branch_check = (
                # unscoped: tenant-scoped operation with verified clinic authorization
                await sb(supabase.table("branches")
                .select("id")
                .eq("id", body.branch_id)
                .eq("clinic_id", target["clinic_id"]))
            )
            if not branch_check.data:
                raise HTTPException(
                    status_code=400, detail="Selected branch does not belong to this clinic."
                )
            if user.role == "staff" and user.branch_id and str(body.branch_id) != str(user.branch_id):
                raise HTTPException(
                    status_code=403, detail="You cannot assign staff to a branch other than your own."
                )
            update_data["branch_id"] = body.branch_id
        else:
            if user.role == "staff" and user.branch_id:
                raise HTTPException(
                    status_code=403, detail="Branch-scoped staff cannot make accounts tenant-wide."
                )
            update_data["branch_id"] = None

    if not update_data:
        raise HTTPException(status_code=400, detail="No changes provided")

    # unscoped: update staff member within clinic after clinic verification
    result = await sb(supabase.table("clinic_admins").update(update_data).eq("id", staff_id))

    client_ip = request.client.host if (request and request.client) else "unknown"
    await log_admin_action(
        user=user,
        action="update_staff",
        resource_type="clinic_admin",
        resource_id=staff_id,
        details={
            "before": {
                "staff_role": target.get("staff_role"),
                "permissions": target.get("permissions"),
                "branch_id": target.get("branch_id"),
                "is_active": target.get("is_active"),
            },
            "after": update_data,
        },
        ip_address=client_ip,
    )
    return {"success": True, "staff": result.data[0] if result.data else None}


@router.put("/staff/{staff_id}/toggle")
async def toggle_staff(
    staff_id: str,
    request: Request = None,
    user: AdminUser = Depends(require_permission("STAFF_UPDATE")),
):
    """Activate/deactivate a staff account — for offboarding without deleting
    their audit trail."""
    res = (
    # unscoped: login authentication by username
        await sb(supabase.table("clinic_admins")
        .select("id, clinic_id, is_active, role, branch_id, username")
        .eq("id", staff_id))
    )
    if not res.data or res.data[0]["role"] != "staff":
        raise HTTPException(status_code=404, detail="Staff account not found")
    target = res.data[0]
    enforce_clinic_access(user, target["clinic_id"])

    if user.role == "staff" and user.branch_id and str(target.get("branch_id")) != str(user.branch_id):
        raise HTTPException(
            status_code=403, detail="You cannot toggle staff outside your assigned branch."
        )

    new_status = not target["is_active"]
    # unscoped: toggle staff member active status after clinic verification
    await sb(supabase.table("clinic_admins").update({"is_active": new_status}).eq(
        "id", staff_id
    ))

    # Offboarding has to end the session the person is holding right now, not
    # just stop the next login.
    if not new_status and target.get("username"):
        await revoke_sessions_for_user(target["username"])

    client_ip = request.client.host if (request and request.client) else "unknown"
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


class LabTestCreate(BaseModel):
    name: str
    sample_type: Optional[str] = None
    prep_instructions: Optional[str] = None
    fasting_required: bool = False
    price_rupees: int
    turnaround_hours: Optional[int] = None
    is_active: bool = True
    branch_id: Optional[str] = None

    @field_validator("price_rupees")
    @classmethod
    def validate_price(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("price_rupees must be greater than 0")
        return v


class LabTestUpdate(BaseModel):
    name: Optional[str] = None
    sample_type: Optional[str] = None
    prep_instructions: Optional[str] = None
    fasting_required: Optional[bool] = None
    price_rupees: Optional[int] = None
    turnaround_hours: Optional[int] = None
    is_active: Optional[bool] = None
    branch_id: Optional[str] = None

    @field_validator("price_rupees")
    @classmethod
    def validate_price(cls, v: Optional[int]) -> Optional[int]:
        """Same rule as LabTestCreate.

        Without it an edit could set a price the create route would reject: 0
        made the booking fall through to the generic booking fee, and a
        negative price reached Razorpay as a negative order amount.
        """
        if v is not None and v <= 0:
            raise ValueError("price_rupees must be greater than 0")
        return v


class LabCollectionWindowUpdate(BaseModel):
    start: str
    end: str
    days: str = "Mon,Tue,Wed,Thu,Fri,Sat"
    sunday_start: Optional[str] = None
    sunday_end: Optional[str] = None

    @field_validator("start", "end", "sunday_start", "sunday_end")
    @classmethod
    def validate_time_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("Time must be in HH:MM format")
        return v


class PaymentSettingsUpdate(BaseModel):
    """Self-service payment settings a clinic_admin can set for their own
    clinic. Partial update — only fields explicitly sent are changed."""

    razorpay_key_id: Optional[str] = None
    razorpay_key_secret: Optional[str] = None
    razorpay_webhook_secret: Optional[str] = None
    payment_mode: Optional[Literal["full", "partial", "none"]] = None
    payment_deposit_percent: Optional[int] = None
    #: Hours before the slot up to which a cancellation is still refundable.
    #: 0 = refundable any time before the appointment starts. Fixed tiers only,
    #: so a stray value can never quietly change who gets their money back.
    cancellation_window_hours: Optional[Literal[0, 2, 4, 6, 12, 24]] = None

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
    # Reception / front-desk line quoted when a patient picks "Talk to Staff".
    # Distinct from the emergency desk: one is a callback, the other is a
    # medical emergency, and clinics routinely staff them differently.
    hospital_staff_phone: Optional[str] = None
    phone_number_id: Optional[str] = None      # Meta WhatsApp phone_number_id for dual-key routing
    is_sandbox: Optional[bool] = None           # Mark as test/sandbox clinic for demo number routing

    # ── Patient follow-ups (Hospital Profile -> Patient Follow-ups) ──────────
    followup_enabled: Optional[bool] = None
    followup_days: Optional[int] = Field(default=None, ge=1, le=30)
    followup_message: Optional[str] = None
    followup_message_template_name: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("name cannot be blank")
        return v.strip() if v else v

    @field_validator("followup_message")
    @classmethod
    def validate_followup_message(cls, v: Optional[str]) -> Optional[str]:
        # 700 chars matches flatten_for_template_param()'s cap, which is what
        # actually goes into the Meta template parameter. Rejecting here beats
        # silently truncating the clinic's wording at send time.
        if v is not None and len(v.strip()) > 700:
            raise ValueError("followup_message must be 700 characters or fewer")
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
    # Insurance/TPA panels whose reports go to the clinic's desk number instead
    # of the patient. Empty string clears the rule (unlike the credential
    # fields above, where empty means "keep what is stored").
    report_routing_providers: Optional[str] = None
    report_routing_phone: Optional[str] = None


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
        # unscoped: tenant-scoped operation with verified clinic authorization
        query = supabase.table("doctors").select("*")
        query = query.eq("clinic_id", effective_clinic_id)
        result = await sb(query.limit(2000))
        doctors = result.data or []

        # Enrich each doctor with their branch assignments
        if doctors:
            doctor_ids = [d["id"] for d in doctors]
            db_result = (
        # unscoped: doctor branch association
                await sb(supabase.table("doctor_branches")
                .select("doctor_id, branch_id, session, branches(id, name, is_active)")
                .in_("doctor_id", doctor_ids)
                .limit(2000))
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
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("DOCTORS_CREATE")),
):
    """Create a new doctor and optionally assign to a branch."""
    effective_clinic_id = None
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)

        # Extract branch fields before building the doctors-table payload
        requested_branch_id = doctor.branch_id
        requested_branch_session = doctor.branch_session or "both"

        if user.role == "staff" and user.branch_id:
            if requested_branch_id:
                enforce_branch_scope(user, requested_branch_id)
            else:
                requested_branch_id = str(user.branch_id)

        try:
            doctor_data = doctor.model_dump(exclude={"branch_id", "branch_session"})
        except AttributeError:
            doctor_data = doctor.dict()
            doctor_data.pop("branch_id", None)
            doctor_data.pop("branch_session", None)

        doctor_data = _apply_slot_config(doctor_data)
        doctor_data["clinic_id"] = effective_clinic_id
        # unscoped: inserting new doctor record with explicit clinic_id
        result = await sb(supabase.table("doctors").insert(doctor_data))
        new_doctor = result.data[0]

        # ── Branch assignment ──
        branch_id_to_assign = requested_branch_id

        if not branch_id_to_assign:
            # Auto-select single branch for single-branch clinics
            branches_result = (
                # unscoped: tenant-scoped operation with verified clinic authorization
                await sb(supabase.table("branches")
                .select("id")
                .eq("clinic_id", effective_clinic_id)
                .eq("is_active", True))
            )
            clinic_branches = branches_result.data if isinstance(branches_result.data, list) else []
            if len(clinic_branches) == 1:
                branch_id_to_assign = clinic_branches[0]["id"]

        if branch_id_to_assign:
            # IDOR check: verify branch belongs to this clinic
            branch_check = (
                # unscoped: tenant-scoped operation with verified clinic authorization
                await sb(supabase.table("branches")
                .select("id")
                .eq("id", branch_id_to_assign)
                .eq("clinic_id", effective_clinic_id))
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
        # unscoped: doctor branch association
                await sb(supabase.table("doctor_branches").insert({
                    "doctor_id": new_doctor["id"],
                    "branch_id": branch_id_to_assign,
                    "session": requested_branch_session,
                }))
                new_doctor["branch_id"] = branch_id_to_assign
                new_doctor["branch_session"] = requested_branch_session
            except Exception as be:
                logger.warning(
                    f"Doctor {new_doctor['id']} created but branch assignment failed: {be}"
                )

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="create_doctor",
            resource_type="doctor",
            resource_id=new_doctor["id"],
            details={"name": new_doctor.get("name"), "branch_id": branch_id_to_assign},
            ip_address=client_ip,
        )
        invalidate_doctor_cache(clinic_id=effective_clinic_id)

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
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("DOCTORS_UPDATE")),
):
    """Update an existing doctor, optionally changing branch assignment."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        # ── Tenant ownership gate (KA-P1-05) ────────────────────────────────
        # Every downstream step in this handler keys on doctor_id alone: the
        # staff branch pre-check below, the branch-only fetch, and the
        # doctor_branches delete/insert (which sits on a table with no
        # clinic_id column at all). Previously the only clinic predicate was
        # on the profile UPDATE, so a body containing ONLY branch fields left
        # update_data empty, skipped that UPDATE, and read + rewrote another
        # tenant's doctor.
        #
        # Resolve ownership once, here, and let everything after it rely on
        # this row. One guard at the top is smaller than a predicate on each
        # of the four call sites, and cannot be forgotten by the next edit.
        # unscoped: clinic_id predicate is applied conditionally on the next line
        owner_query = supabase.table("doctors").select("*").eq("id", doctor_id)
        owner_query = owner_query.eq("clinic_id", effective_clinic_id)
        owner_res = await sb(owner_query)
        if not owner_res.data:
            # 404 rather than 403: do not confirm that a doctor_id exists in
            # some other tenant. The 400-vs-404 difference was an enumeration
            # oracle for doctor IDs across the whole platform.
            raise HTTPException(status_code=404, detail="Doctor not found")
        existing_doctor = owner_res.data[0]

        # Branch-scoped staff check on existing doctor
        if user.role == "staff" and user.branch_id:
            doc_branches = (
        # doctor_branches has no clinic_id column; doctor_id ownership was
        # unscoped: verified against effective_clinic_id by the gate above
                await sb(supabase.table("doctor_branches")
                .select("branch_id")
                .eq("doctor_id", doctor_id))
            )
            if doc_branches.data:
                assigned_branch_ids = [str(b["branch_id"]) for b in doc_branches.data]
                if str(user.branch_id) not in assigned_branch_ids:
                    raise HTTPException(
                        status_code=403, detail="Doctor is not assigned to your branch."
                    )

        # Extract branch fields before building the doctors-table payload
        requested_branch_id = doctor.branch_id
        requested_branch_session = doctor.branch_session

        if user.role == "staff" and user.branch_id and requested_branch_id:
            enforce_branch_scope(user, requested_branch_id)

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
            # unscoped: updating doctor profile by doctor_id within verified clinic
            query = supabase.table("doctors").update(update_data)
            query = query.eq("clinic_id", effective_clinic_id)
            result = await sb(query.eq("id", doctor_id))
            if not result.data:
                raise HTTPException(status_code=404, detail="Doctor not found")
            updated_doctor = result.data[0]
        else:
            # Only a branch change was requested. Reuse the row the ownership
            # gate already fetched — re-reading it here by id alone was the
            # unscoped cross-tenant read in KA-P1-05.
            updated_doctor = existing_doctor

        # ── Branch assignment upsert ──
        if requested_branch_id is not None:
            doc_clinic_id = updated_doctor.get("clinic_id") or effective_clinic_id

            branch_check = (
                # unscoped: tenant-scoped operation with verified clinic authorization
                await sb(supabase.table("branches")
                .select("id")
                .eq("id", requested_branch_id)
                .eq("clinic_id", doc_clinic_id))
            )
            if not branch_check.data:
                raise HTTPException(
                    status_code=400,
                    detail="Selected branch does not belong to your clinic.",
                )

            session_val = requested_branch_session or "both"

            # scoped: clear doctor branch associations for validated doctor
        # unscoped: doctor branch association
            await sb(supabase.table("doctor_branches").delete().eq("doctor_id", doctor_id))
        # unscoped: doctor branch association
            await sb(supabase.table("doctor_branches").insert({
                "doctor_id": doctor_id,
                "branch_id": requested_branch_id,
                "session": session_val,
            }))
            updated_doctor["branch_id"] = requested_branch_id
            updated_doctor["branch_session"] = session_val

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="update_doctor",
            resource_type="doctor",
            resource_id=doctor_id,
            details={"updated_fields": list(update_data.keys()), "branch_id": requested_branch_id},
            ip_address=client_ip,
        )
        invalidate_doctor_cache(clinic_id=effective_clinic_id)

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
    doctor_id: str,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("DOCTORS_DELETE")),
):
    """Delete a doctor."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        # Branch-scoped staff check
        if user.role == "staff" and user.branch_id:
            doc_branches = (
        # unscoped: doctor branch association
                await sb(supabase.table("doctor_branches")
                .select("branch_id")
                .eq("doctor_id", doctor_id))
            )
            if doc_branches.data:
                assigned_branch_ids = [str(b["branch_id"]) for b in doc_branches.data]
                if str(user.branch_id) not in assigned_branch_ids:
                    raise HTTPException(
                        status_code=403, detail="Doctor is not assigned to your branch."
                    )

        # Read the row BEFORE deleting, inside the tenant scope. Two reasons:
        #   1. A doctor id from another clinic now 404s instead of silently
        #      deleting nothing (or, before the scope fix, deleting THAT row).
        #   2. The audit log keeps the full row. This delete is a hard DELETE
        #      and doctor_branches cascades off it, so when a live clinic's
        #      roster was destroyed on 2026-09-01 the audit trail held only a
        #      UUID — nothing to restore from without a database PITR.
        existing = (
            # unscoped: tenant-scoped operation with verified clinic authorization
            await sb(supabase.table("doctors")
            .select("*")
            .eq("id", doctor_id)
            .eq("clinic_id", effective_clinic_id))
        )
        if not existing.data:
            raise HTTPException(status_code=404, detail="Doctor not found")
        snapshot = existing.data[0]

        # unscoped: soft-deleting doctor record by doctor_id within verified clinic
        query = supabase.table("doctors").delete()
        query = query.eq("clinic_id", effective_clinic_id)
        await sb(query.eq("id", doctor_id))

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="delete_doctor",
            resource_type="doctor",
            resource_id=doctor_id,
            details={"deleted_row": snapshot},
            ip_address=client_ip,
        )
        invalidate_doctor_cache(clinic_id=effective_clinic_id)

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting doctor: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete doctor")


@router.get("/lab-tests")
async def get_lab_tests_admin(
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Get the clinic's lab test catalog."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        # unscoped: tenant-scoped operation with verified clinic authorization
        query = supabase.table("lab_tests").select("*")
        query = query.eq("clinic_id", effective_clinic_id)
        if branch_id:
            query = query.eq("branch_id", branch_id)
        result = await sb(query.order("name").limit(2000))
        return result.data or []
    except Exception as e:
        logger.error(f"Error fetching lab tests for clinic_id={effective_clinic_id}: {e}")
        raise HTTPException(status_code=500, detail=_friendly_db_error(e, "Failed to fetch lab tests"))


@router.post("/lab-tests")
async def create_lab_test(
    test: LabTestCreate,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("LAB_TESTS_MANAGE")),
):
    """Create a new lab test catalog entry."""
    effective_clinic_id = None
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)

        if test.branch_id:
            enforce_branch_scope(user, test.branch_id)
            branch_check = (
                # unscoped: tenant-scoped operation with verified clinic authorization
                await sb(supabase.table("branches")
                .select("id")
                .eq("id", test.branch_id)
                .eq("clinic_id", effective_clinic_id))
            )
            if not branch_check.data:
                raise HTTPException(
                    status_code=400, detail="Selected branch does not belong to your clinic."
                )

        try:
            test_data = test.model_dump(exclude={"price_rupees"})
        except AttributeError:
            test_data = test.dict(exclude={"price_rupees"})
        test_data["price_paise"] = test.price_rupees * 100
        test_data["clinic_id"] = effective_clinic_id

        # unscoped: insert lab test with effective_clinic_id
        result = await sb(supabase.table("lab_tests").insert(test_data))
        new_test = result.data[0]

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="create_lab_test",
            resource_type="lab_test",
            resource_id=new_test["id"],
            details={"name": new_test.get("name")},
            ip_address=client_ip,
        )
        return new_test
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error creating lab test for clinic_id={effective_clinic_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail=_friendly_db_error(e, "Failed to create lab test")
        )


@router.put("/lab-tests/{test_id}")
async def update_lab_test(
    test_id: str,
    test: LabTestUpdate,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("LAB_TESTS_MANAGE")),
):
    """Update an existing lab test catalog entry."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        if test.branch_id:
            enforce_branch_scope(user, test.branch_id)

        try:
            update_data = test.model_dump(exclude_unset=True, exclude={"price_rupees"})
        except AttributeError:
            update_data = test.dict(exclude_unset=True, exclude={"price_rupees"})
        if test.price_rupees is not None:
            update_data["price_paise"] = test.price_rupees * 100
        if not update_data:
            return {"message": "No fields to update"}

        # unscoped: updating lab test catalog item by test_id within verified clinic
        query = supabase.table("lab_tests").update(update_data)
        query = query.eq("clinic_id", effective_clinic_id)
        result = await sb(query.eq("id", test_id))
        if not result.data:
            raise HTTPException(status_code=404, detail="Lab test not found")

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="update_lab_test",
            resource_type="lab_test",
            resource_id=test_id,
            details={"updated_fields": list(update_data.keys())},
            ip_address=client_ip,
        )
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating lab test {test_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=_friendly_db_error(e, "Failed to update lab test")
        )


@router.delete("/lab-tests/{test_id}")
async def delete_lab_test(
    test_id: str,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("LAB_TESTS_MANAGE")),
):
    """Delete a lab test catalog entry."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        # unscoped: soft-deleting lab test catalog item by test_id within verified clinic
        query = supabase.table("lab_tests").delete()
        query = query.eq("clinic_id", effective_clinic_id)
        result = await sb(query.eq("id", test_id))
        if not result.data:
            raise HTTPException(status_code=404, detail="Lab test not found")

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="delete_lab_test",
            resource_type="lab_test",
            resource_id=test_id,
            details={},
            ip_address=client_ip,
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting lab test {test_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=_friendly_db_error(e, "Failed to delete lab test")
        )


CSV_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
CSV_MAX_DATA_ROWS = 25_000
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection."""
    if value and (value.startswith(_CSV_FORMULA_PREFIXES) or value.lstrip().startswith(_CSV_FORMULA_PREFIXES)):
        return "'" + value
    return value


_CSV_HEADER_ALIASES: dict[str, set[str]] = {
    "name": {"name", "test name", "testname", "test_name"},
    "price_rupees": {
        "price_rupees", "price", "price in rupees", "price(rs)", "price (rs)",
        "mrp", "rate", "amount",
    },
    "sample_type": {"sample_type", "sample type", "specimen", "specimen type"},
    "turnaround_hours": {
        "turnaround_hours", "turnaround hours", "turnaround (hours)", "tat", "tat (hours)",
    },
    "fasting_required": {"fasting_required", "fasting", "fasting required"},
    "prep_instructions": {"prep_instructions", "preparation", "prep instructions", "instructions"},
}


def _normalize_csv_headers(fieldnames: list[str]) -> dict[str, str]:
    """Map each raw CSV header to its canonical field name, case/whitespace-insensitively.

    Headers that don't match any known alias are left unmapped (ignored on
    each row) rather than rejected — a stray "Notes" column shouldn't block
    an otherwise-valid import.
    """
    header_map: dict[str, str] = {}
    for raw in fieldnames:
        norm = raw.strip().lower()
        for canonical, aliases in _CSV_HEADER_ALIASES.items():
            if norm in aliases:
                header_map[raw] = canonical
                break
    return header_map


@router.post("/lab-tests/import-csv")
async def import_lab_tests_csv(
    file: UploadFile = File(...),
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("LAB_TESTS_MANAGE")),
):
    """Atomic bulk-import lab tests from a CSV file.

    Pipeline:
      1. File size check (max 5MB)
      2. UTF-8 decoding
      3. Header presence & canonical column resolution (name, price_rupees required)
      4. Complete in-memory pre-flight row validation (max 25k rows, ranges, types, intra-file duplicates)
      5. Rejection gate: if any validation error, return 422 with structured error list and mutate 0 records
      6. Database upsert phase for validated rows
    """
    effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
    raw = await file.read()

    # 1. File size guard
    if len(raw) > CSV_MAX_FILE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds maximum size limit of {CSV_MAX_FILE_BYTES // (1024 * 1024)} MB.",
        )

    # 2. UTF-8 decoding
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded CSV")

    # 3. Header check
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV file has no header row")

    header_map = _normalize_csv_headers(reader.fieldnames)
    canonical_present = set(header_map.values())
    if not {"name", "price_rupees"}.issubset(canonical_present):
        raise HTTPException(
            status_code=400,
            detail=(
                "CSV must include a test-name column (e.g. 'name' or 'Test Name') "
                "and a price column (e.g. 'price_rupees' or 'Price in Rupees')"
            ),
        )

    # 4. In-memory pre-flight row validation (zero DB writes)
    validated_rows = []
    errors = []
    seen_names: dict[str, int] = {}

    for i, raw_row in enumerate(reader, start=2):  # header is row 1
        if i - 1 > CSV_MAX_DATA_ROWS:
            raise HTTPException(
                status_code=400,
                detail=f"CSV exceeds maximum allowed limit of {CSV_MAX_DATA_ROWS:,} data rows.",
            )

        row = {
            header_map.get(k, k): _sanitize_csv_cell(v or "")
            for k, v in raw_row.items()
            if k is not None
        }
        name = row.get("name", "").strip()
        price_raw = (
            row.get("price_rupees", "").strip().replace(",", "").replace("₹", "")
        )

        # Validate name
        if not name:
            errors.append(
                {
                    "row": i,
                    "column": "name",
                    "value": "",
                    "problem": "Missing test name.",
                    "expected": "Non-empty text",
                }
            )
            continue
        if len(name) > 200:
            errors.append(
                {
                    "row": i,
                    "column": "name",
                    "value": name[:50] + "...",
                    "problem": "Name exceeds 200 characters.",
                    "expected": "≤ 200 characters",
                }
            )
            continue

        # Validate price
        try:
            price_rupees_val = float(price_raw)
            if price_rupees_val <= 0:
                raise ValueError("Price must be positive")
        except ValueError:
            errors.append(
                {
                    "row": i,
                    "column": "price_rupees",
                    "value": price_raw,
                    "problem": "Price must be a positive number.",
                    "expected": "Positive number (e.g. 500)",
                }
            )
            continue

        # Duplicate check within the CSV file
        name_lower = name.lower()
        if name_lower in seen_names:
            errors.append(
                {
                    "row": i,
                    "column": "name",
                    "value": name,
                    "problem": f"Duplicate test name in CSV (first defined at row {seen_names[name_lower]}).",
                    "expected": "Unique test names within file",
                }
            )
            continue
        seen_names[name_lower] = i

        # Optional turnaround_hours
        turnaround_raw = row.get("turnaround_hours", "").strip()
        turnaround = None
        if turnaround_raw:
            if turnaround_raw.isdigit() and int(turnaround_raw) > 0:
                turnaround = int(turnaround_raw)
            else:
                errors.append(
                    {
                        "row": i,
                        "column": "turnaround_hours",
                        "value": turnaround_raw,
                        "problem": "Turnaround hours must be a positive integer.",
                        "expected": "Positive integer (e.g. 24)",
                    }
                )
                continue

        # Optional sample_type and prep_instructions lengths
        sample_type = row.get("sample_type", "").strip() or None
        if sample_type and len(sample_type) > 100:
            errors.append(
                {
                    "row": i,
                    "column": "sample_type",
                    "value": sample_type[:50],
                    "problem": "Sample type exceeds 100 characters.",
                    "expected": "≤ 100 characters",
                }
            )
            continue

        prep_instructions = row.get("prep_instructions", "").strip() or None
        if prep_instructions and len(prep_instructions) > 500:
            errors.append(
                {
                    "row": i,
                    "column": "prep_instructions",
                    "value": prep_instructions[:50],
                    "problem": "Prep instructions exceed 500 characters.",
                    "expected": "≤ 500 characters",
                }
            )
            continue

        fasting = row.get("fasting_required", "").strip().lower() in ("true", "1", "yes")

        validated_rows.append(
            {
                "clinic_id": effective_clinic_id,
                "name": name,
                "price_paise": int(round(price_rupees_val * 100)),
                "sample_type": sample_type,
                "turnaround_hours": turnaround,
                "fasting_required": fasting,
                "prep_instructions": prep_instructions,
            }
        )

    # 5. Rejection gate: if any validation error occurred, abort entire import
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "created": 0,
                "updated": 0,
                "errors": errors,
                "message": f"Import rejected: {len(errors)} validation error(s). Zero records were modified.",
            },
        )

    if not validated_rows:
        raise HTTPException(status_code=400, detail="CSV contains no valid data rows.")

    # unscoped: fetching existing lab test names within verified clinic scope for upsert matching
    existing_result = await sb(supabase.table("lab_tests").select("id, name").eq("clinic_id", effective_clinic_id))
    existing_map = {r["name"].lower(): r["id"] for r in (existing_result.data or [])}

    created, updated = 0, 0
    for test_data in validated_rows:
        name_lower = test_data["name"].lower()
        if name_lower in existing_map:
            # unscoped: updating existing lab test within verified clinic
            await sb(supabase.table("lab_tests").update(test_data).eq("id", existing_map[name_lower]))
            updated += 1
        else:
            # unscoped: inserting new lab test within verified clinic
            await sb(supabase.table("lab_tests").insert(test_data))
            created += 1

    await log_admin_action(
        user=user,
        action="import_lab_tests_csv",
        resource_type="lab_test",
        resource_id=None,
        details={"created": created, "updated": updated, "total": len(validated_rows)},
        ip_address="unknown",
    )
    return {
        "created": created,
        "updated": updated,
        "total_imported": len(validated_rows),
        "errors": [],
    }


@router.get("/lab-tests/csv-template")
async def download_lab_test_csv_template(
    user: AdminUser = Depends(verify_credentials),
):
    """Download a canonical CSV template for lab tests / diagnostic services catalog import."""
    template_content = (
        "name,price_rupees,sample_type,turnaround_hours,fasting_required,prep_instructions\n"
        "Complete Blood Count (CBC),350,Blood,24,false,No special preparation needed\n"
        "Fasting Blood Sugar (FBS),150,Blood,12,true,Fast for 8-10 hours prior to sample collection\n"
        "Thyroid Profile (T3 T4 TSH),750,Blood,24,false,Can be taken at any time of day\n"
        "Lipid Profile,600,Blood,24,true,12 hours overnight fasting mandatory\n"
        "Urine Routine & Microscopy,180,Urine,12,false,Collect clean catch midstream sample\n"
    )
    return Response(
        content=template_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="lab_tests_template.csv"'
        },
    )


@router.put("/lab-collection-window")
async def update_lab_collection_window(
    payload: LabCollectionWindowUpdate,
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    user: AdminUser = Depends(require_permission("LAB_TESTS_MANAGE")),
):
    """Set the daily sample collection window for a branch, or the clinic
    itself for single-location clinics (branch_id omitted)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    window = {"start": payload.start, "end": payload.end, "days": payload.days}
    if payload.sunday_start and payload.sunday_end:
        window["sunday_start"] = payload.sunday_start
        window["sunday_end"] = payload.sunday_end

    try:
        if branch_id:
            enforce_branch_scope(user, branch_id)
            branch_result = (
                # unscoped: tenant-scoped operation with verified clinic authorization
                await sb(supabase.table("branches")
                .select("config")
                .eq("id", branch_id)
                .eq("clinic_id", effective_clinic_id))
            )
            if not branch_result.data:
                raise HTTPException(status_code=404, detail="Branch not found")
            config = branch_result.data[0].get("config") or {}
            config["lab_collection"] = window
            # unscoped: updating branch configuration by branch_id within verified clinic
            await sb(supabase.table("branches").update({"config": config}).eq("id", branch_id))
        else:
            # unscoped: updating clinic configuration by clinic_id within verified clinic
            clinic_result = await sb(supabase.table("clinics").select("config").eq("id", effective_clinic_id))
            if not clinic_result.data:
                raise HTTPException(status_code=404, detail="Clinic not found")
            config = clinic_result.data[0].get("config") or {}
            config["lab_collection"] = window
            # unscoped: updating clinic configuration by clinic_id within verified clinic
            await sb(supabase.table("clinics").update({"config": config}).eq("id", effective_clinic_id))

        return {"success": True, "lab_collection": window}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating lab collection window: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=_friendly_db_error(e, "Failed to update collection window")
        )


@router.get("/leaves")
async def get_leaves(
    doctor: Optional[str] = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Get doctor leaves."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        # unscoped: tenant-scoped operation with verified clinic authorization
        query = supabase.table("doctor_leaves").select("*")
        query = query.eq("clinic_id", effective_clinic_id)
        if doctor:
            query = query.eq("doctor_name", doctor)
        result = await sb(query.order("leave_date").limit(2000))
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting leaves: {e}")
        raise HTTPException(status_code=500, detail="Failed to get leaves")


@router.post("/leaves")
async def create_leave(
    leave: LeaveCreate,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("DOCTOR_LEAVES_CREATE")),
):
    """Create a doctor leave (single day or date range)."""
    from datetime import timedelta

    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)

        # Branch-scoped staff check on doctor
        if user.role == "staff" and user.branch_id:
            doc_res = (
                # unscoped: tenant-scoped operation with verified clinic authorization
                await sb(supabase.table("doctors")
                .select("id")
                .eq("name", leave.doctor_name)
                .eq("clinic_id", effective_clinic_id))
            )
            if doc_res.data:
                doc_id = doc_res.data[0]["id"]
                doc_branches = (
        # unscoped: doctor branch association
                    await sb(supabase.table("doctor_branches")
                    .select("branch_id")
                    .eq("doctor_id", doc_id))
                )
                if doc_branches.data:
                    assigned_branch_ids = [str(b["branch_id"]) for b in doc_branches.data]
                    if str(user.branch_id) not in assigned_branch_ids:
                        raise HTTPException(
                            status_code=403,
                            detail="Doctor is not assigned to your branch.",
                        )

        start_date = leave.leave_date
        end_date = leave.end_date or start_date

        if end_date < start_date:
            raise HTTPException(
                status_code=400, detail="End date cannot be before start date"
            )

        leaves_to_insert = []
        current_date = start_date

        while current_date <= end_date:
            try:
                leave_data = leave.model_dump(exclude={"end_date"})
            except AttributeError:
                leave_data = leave.dict(exclude={"end_date"})
            leave_data["leave_date"] = str(current_date)
            leave_data["clinic_id"] = effective_clinic_id
            leaves_to_insert.append(leave_data)
            current_date += timedelta(days=1)

        # unscoped: insert doctor leaves for validated doctor
        result = await sb(supabase.table("doctor_leaves").insert(leaves_to_insert))

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="create_leave",
            resource_type="doctor_leave",
            resource_id=leave.doctor_name,
            details={"start_date": str(start_date), "end_date": str(end_date), "count": len(leaves_to_insert)},
            ip_address=client_ip,
        )

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
    leave_id: str,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("DOCTOR_LEAVES_DELETE")),
):
    """Delete a doctor leave."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        if user.role == "staff" and user.branch_id:
            leave_res = (
                # unscoped: tenant-scoped operation with verified clinic authorization
                await sb(supabase.table("doctor_leaves")
                .select("doctor_name, clinic_id")
                .eq("id", leave_id))
            )
            if leave_res.data:
                doc_name = leave_res.data[0]["doctor_name"]
                doc_res = (
                    # unscoped: tenant-scoped operation with verified clinic authorization
                    await sb(supabase.table("doctors")
                    .select("id")
                    .eq("name", doc_name)
                    .eq("clinic_id", leave_res.data[0]["clinic_id"]))
                )
                if doc_res.data:
                    doc_id = doc_res.data[0]["id"]
                    doc_branches = (
        # unscoped: doctor branch association
                        await sb(supabase.table("doctor_branches")
                        .select("branch_id")
                        .eq("doctor_id", doc_id))
                    )
                    if doc_branches.data:
                        assigned_branch_ids = [str(b["branch_id"]) for b in doc_branches.data]
                        if str(user.branch_id) not in assigned_branch_ids:
                            raise HTTPException(
                                status_code=403,
                                detail="Doctor is not assigned to your branch.",
                            )

        # unscoped: deleting doctor leave record by leave_id within verified clinic
        query = supabase.table("doctor_leaves").delete()
        query = query.eq("clinic_id", effective_clinic_id)
        await sb(query.eq("id", leave_id))

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="delete_leave",
            resource_type="doctor_leave",
            resource_id=leave_id,
            ip_address=client_ip,
        )

        return {"status": "deleted"}
    except HTTPException:
        raise
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
        # unscoped: tenant-scoped operation with verified clinic authorization
        query = supabase.table("hospital_holidays").select("*").order("holiday_date")
        query = query.eq("clinic_id", effective_clinic_id)
        result = await sb(query.limit(2000))
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting holidays: {e}")
        raise HTTPException(status_code=500, detail="Failed to get holidays")


@router.post("/holidays")
async def create_holiday(
    holiday_date: date,
    name: str,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("HOLIDAYS_CREATE")),
):
    """Create a hospital holiday."""
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
        result = (
            # unscoped: inserting hospital holiday record with explicit clinic_id
            await sb(supabase.table("hospital_holidays")
            .insert(
                {
                    "clinic_id": effective_clinic_id,
                    "holiday_date": str(holiday_date),
                    "name": name,
                }
            ))
        )

        # The bot caches "is this date a holiday?" for 5 minutes, including the
        # negative answer. Without this the clinic would keep taking bookings
        # for a day it just closed.
        invalidate_holiday_cache(effective_clinic_id, str(holiday_date))

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="create_holiday",
            resource_type="hospital_holiday",
            resource_id=str(holiday_date),
            details={"name": name},
            ip_address=client_ip,
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
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("HOLIDAYS_DELETE")),
):
    """Delete a hospital holiday."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        # unscoped: deleting hospital holiday record by holiday_id within verified clinic
        query = supabase.table("hospital_holidays").delete()
        query = query.eq("clinic_id", effective_clinic_id)
        await sb(query.eq("holiday_date", holiday_date))

        # Mirror of create_holiday: without this the clinic stays shut to
        # patients for up to 5 minutes after it is reopened.
        invalidate_holiday_cache(effective_clinic_id, holiday_date)

        client_ip = request.client.host if (request and request.client) else "unknown"
        await log_admin_action(
            user=user,
            action="delete_holiday",
            resource_type="hospital_holiday",
            resource_id=holiday_date,
            ip_address=client_ip,
        )

        return {"status": "deleted"}
    except HTTPException:
        raise
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
    try:
        result = await check_in_appointment(effective_clinic_id, appointment_id)
    except Exception as e:
        logger.error(f"Error during check-in for appointment {appointment_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Check-in failed due to server error: {e}"
        )
    if not result:
        raise HTTPException(
            status_code=404, detail="Appointment not found"
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
    """Upload and send a lab report to a patient via WhatsApp.
    
    Routes through authoritative patient_match_service and fail-closed PDF validator (W1.4).
    """
    try:
        effective_clinic_id = enforce_clinic_access(user, clinic_id)
        file_bytes = await file.read()
        if len(file_bytes) == 0:
            raise HTTPException(status_code=400, detail="Empty file uploaded")
        if not file_bytes.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF")

        # 1. Authoritative Patient Matching Verification (W1.4)
        from app.services.patient_match import patient_match_service
        match_res = await patient_match_service.match(
            clinic_id=effective_clinic_id,
            scraped_name=patient_name,
            scraped_phone=patient_phone,
        )
        effective_phone = match_res.normalized_phone or patient_phone
        effective_name = match_res.patient_name or patient_name

        # If patient matching determines unsafe match, hold for review
        if not match_res.is_safe_to_send:
            logger.warning(
                f"Admin manual lab report upload held for review: {match_res.review_reason}"
            )
            nr_id = await LabReportService().store_for_review(
                clinic_id=effective_clinic_id,
                patient_phone=effective_phone,
                patient_name=effective_name,
                report_name=report_name,
                report_type=report_type,
                review_reason=match_res.review_reason or "Held by patient match gate",
                file_bytes=file_bytes,
                filename=file.filename or f"manual_upload_{uuid4().hex[:8]}.pdf",
                content_type=file.content_type or "application/pdf",
                source="admin_manual",
                match_confidence=match_res.match_confidence,
                match_source=match_res.match_source,
            )
            return {
                "success": True,
                "status": "needs_review",
                "message": f"Report held for review: {match_res.review_reason}",
                "report_id": nr_id,
            }

        # 2. Dispatch via LabReportService
        result = await LabReportService().upload_and_send(
            clinic_id=effective_clinic_id,
            file_bytes=file_bytes,
            filename=file.filename or f"manual_upload_{uuid4().hex[:8]}.pdf",
            content_type=file.content_type or "application/pdf",
            patient_phone=effective_phone,
            patient_name=effective_name,
            report_name=report_name,
            report_type=report_type,
            source="admin_manual",
            match_confidence=match_res.match_confidence,
            match_source=match_res.match_source,
            matched_patient_id=match_res.matched_patient_id,
        )
        return {
            "success": True,
            "message": "Report sent to patient via WhatsApp",
            "report": result,
        }
    except HTTPException:
        raise
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
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Resend a lab report to the patient with strict tenant scoping."""
    try:
        effective_clinic_id = enforce_clinic_access(user, clinic_id)
        await LabReportService().resend_report(report_id, clinic_id=effective_clinic_id)
        return {"success": True, "message": "Report resent successfully"}
    except ValueError as e:
        logger.warning(f"Lab report resend validation: {e}")
        raise HTTPException(
            status_code=404 if "not found" in str(e).lower() else 400,
            detail=str(e),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lab report resend error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/patients")
async def get_patients(
    clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)
):
    """Get all patients with appointment counts, for ONE clinic.

    The platform-wide `if effective_clinic_id == "default"` branch that used to
    live here returned every tenant's patients — names and phone numbers, i.e.
    other clinics' PHI under the DPDP Act — to any principal whose scope
    resolved to the sentinel. enforce_clinic_access() no longer produces that
    sentinel, and the branch is gone rather than left unreachable.
    """
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        patients = await get_genuine_patients(effective_clinic_id)
        return {"patients": patients}
    except Exception as e:
        logger.error(f"Error loading genuine patients: {e}")
        return {"patients": []}


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
        # unscoped: tenant-scoped operation with verified clinic authorization
        query = supabase.table("appointments").select(
            "id, clinic_id, patient_phone, patient_name, department, doctor_name, "
            "appointment_date, appointment_time, status, razorpay_payment_link_id, "
            "payment_id, amount_paise, hold_expires_at, booking_ref, created_at, updated_at"
        )
        query = query.eq("clinic_id", effective_clinic_id)
        if status:
            query = query.eq("status", status)
        result = await sb(query.order("created_at", desc=True).limit(limit))
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
            # unscoped: tenant-scoped operation with verified clinic authorization
            supabase.table("appointments").select("*").eq("status", "pending_review")
        )
        query = query.eq("clinic_id", effective_clinic_id)
        result = await sb(query.order("created_at", desc=True).limit(2000))
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
    """Initiate a refund for a confirmed booking with strict tenant scoping."""
    try:
        from app.services.payment import payment_service
        from app.services.tenant import get_clinic_by_id

        # 1. Fetch booking to verify existence and tenant ownership
        # scoped: fetch booking by id and enforce clinic access below
        booking_result = (
            # unscoped: tenant-scoped operation with verified clinic authorization
            await sb(supabase.table("appointments")
            .select("*")
            .eq("id", booking_id))
        )
        if not booking_result.data:
            raise HTTPException(status_code=404, detail="Booking not found")

        booking = booking_result.data[0]
        booking_clinic_id = booking.get("clinic_id") or "default"

        # 2. Enforce clinic access (raises 403 if user lacks access to this clinic)
        enforce_clinic_access(user, booking_clinic_id)

        # 3. Resolve the booking's clinic for per-clinic Razorpay credentials
        clinic = None
        try:
            clinic = await get_clinic_by_id(booking_clinic_id)
        except Exception as ce:
            logger.warning(f"Could not load clinic {booking_clinic_id} for refund: {ce}")

        reason = (body or {}).get("reason", f"Admin refund by {user.username or user.role}")
        req_idempotency_key = (body or {}).get("idempotency_key")
        result = await payment_service.initiate_refund(
            booking_id, reason, clinic=clinic, idempotency_key=req_idempotency_key
        )
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
    clinic_id: str = "default",
    user: AdminUser = Depends(require_admin),
):
    """Get the payment audit trail for a booking."""
    try:
        # IDOR check: the booking must live in the caller's active clinic.
        # This used to be skipped for super_admin, which let a super_admin read
        # any tenant's payment trail by guessing a booking id.
        effective_clinic_id = enforce_clinic_access(user, clinic_id)
        booking_check = (
            # unscoped: tenant-scoped operation with verified clinic authorization
            await sb(supabase.table("appointments")
            .select("id")
            .eq("id", booking_id)
            .eq("clinic_id", effective_clinic_id))
        )
        if not booking_check.data:
            raise HTTPException(status_code=404, detail="Booking not found")

        # scoped: fetch payment events for verified booking
        result = (
            # unscoped: tenant-scoped operation with verified clinic authorization
            await sb(supabase.table("payment_events")
            .select("*")
            .eq("booking_id", booking_id)
            .order("created_at", desc=False))
        )
        return {"events": result.data or []}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting payment events: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payment events")


@router.get("/payments/reconciliation")
async def get_payment_reconciliation(
    date_str: Optional[str] = None,
    clinic_id: Optional[str] = None,
    user: AdminUser = Depends(require_admin),
):
    """Get daily payment reconciliation summary scoped to user's clinic."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id or user.clinic_id or "default")
    try:
        from app.services.payment import payment_service

        summary = await payment_service.get_daily_reconciliation(date_str, clinic_id=effective_clinic_id)
        return summary
    except HTTPException:
        raise
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
            return query.eq("clinic_id", effective_clinic_id)

        # scoped: total confirmed with payments
        confirmed = await sb(_scope(
            # unscoped: tenant-scoped operation with verified clinic authorization
            supabase.table("appointments")
            .select("id, amount_paise", count="exact")
            .eq("status", "confirmed")
            .not_.is_("payment_id", "null")
            .gte("created_at", cutoff)
        ))

        # scoped: total pending review
        pending = await sb(_scope(
            # unscoped: tenant-scoped operation with verified clinic authorization
            supabase.table("appointments")
            .select("id", count="exact")
            .eq("status", "pending_review")
        ))

        # scoped: total refunded
        refunded = await sb(_scope(
            # unscoped: tenant-scoped operation with verified clinic authorization
            supabase.table("appointments")
            .select("id, amount_paise", count="exact")
            .eq("status", "refunded")
            .gte("created_at", cutoff)
        ))

        # scoped: total expired
        expired = await sb(_scope(
            # unscoped: tenant-scoped operation with verified clinic authorization
            supabase.table("appointments")
            .select("id", count="exact")
            .eq("status", "expired")
            .gte("created_at", cutoff)
        ))

        confirmed_amount = sum(b.get("amount_paise", 0) for b in (confirmed.data or []))
        refunded_amount = sum(b.get("amount_paise", 0) for b in (refunded.data or []))

        # global-read: signature failures from payment_events (left platform-wide)
        sig_failures = (
            # unscoped: tenant-scoped operation with verified clinic authorization
            await sb(supabase.table("payment_events")
            .select("id", count="exact")
            .eq("event_type", "signature_failed")
            .gte("created_at", cutoff))
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
        # Blank means "fall back to the clinic's WhatsApp/contact number", which
        # is what _handle_human_escalation does — so show it blank rather than
        # pre-filling a number the clinic never chose.
        "hospital_staff_phone": cfg.get("staff_phone") or "",
        # Patient follow-ups. Resolved the same way followup_config() resolves
        # them at send time, so the panel shows what will actually happen
        # rather than a blank field that reads as "off".
        "followup_enabled": (
            cfg["followup_enabled"]
            if isinstance(cfg.get("followup_enabled"), bool)
            else settings.followup_enabled_default
        ),
        "followup_days": cfg.get("followup_days") or settings.followup_days_after_visit,
        "followup_message": cfg.get("followup_message") or "",
        "followup_message_template_name": (
            cfg.get("followup_message_template_name")
            or settings.followup_message_template_name
            or ""
        ),
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
    if "hospital_staff_phone" in updates and updates["hospital_staff_phone"] is not None:
        staff_phone = updates["hospital_staff_phone"].strip()
        if staff_phone:
            cfg["staff_phone"] = staff_phone
        else:
            # Clearing restores the WhatsApp-number fallback.
            cfg.pop("staff_phone", None)
    # Patient follow-up settings. Read back by followup_config() in
    # app/services/scheduler.py; stored in clinics.config like every other
    # per-clinic override (e.g. lab_report_template_name).
    if "followup_enabled" in updates and updates["followup_enabled"] is not None:
        cfg["followup_enabled"] = bool(updates["followup_enabled"])
    if "followup_days" in updates and updates["followup_days"] is not None:
        cfg["followup_days"] = int(updates["followup_days"])
    if "followup_message" in updates and updates["followup_message"] is not None:
        cfg["followup_message"] = updates["followup_message"].strip()
    if (
        "followup_message_template_name" in updates
        and updates["followup_message_template_name"] is not None
    ):
        cfg["followup_message_template_name"] = updates[
            "followup_message_template_name"
        ].strip()
    row_updates["config"] = cfg
    # Top-level columns for tenant routing (migration 043)
    if "phone_number_id" in updates and updates["phone_number_id"] is not None:
        row_updates["phone_number_id"] = updates["phone_number_id"].strip()
    if "is_sandbox" in updates and updates["is_sandbox"] is not None:
        row_updates["is_sandbox"] = updates["is_sandbox"]

    target_clinic_id = clinic["id"]
    result = (
        # unscoped: updating clinic configuration filtered by target_clinic_id
        await sb(supabase.table("clinics")
        .update(row_updates)
        .eq("id", target_clinic_id))
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
        # Resolved through the same helper the refund gate and the booking
        # confirmation use, so the panel shows what is actually enforced —
        # including the platform default when the clinic has never set one.
        "cancellation_window_hours": cancellation_window_hours(clinic),
        "cancellation_window_choices": list(CANCELLATION_WINDOW_CHOICES),
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
    if (
        "cancellation_window_hours" in updates
        and updates["cancellation_window_hours"] is not None
    ):
        cfg["cancellation_window_hours"] = updates["cancellation_window_hours"]

    final_mode = cfg.get("payment_mode", "full")
    final_percent = cfg.get("payment_deposit_percent")
    if final_mode == "partial" and not (
        isinstance(final_percent, int) and 1 <= final_percent <= 99
    ):
        raise HTTPException(
            status_code=422,
            detail="payment_deposit_percent (1-99) is required when payment_mode is 'partial'",
        )

    target_clinic_id = clinic["id"]
    result = (
        # unscoped: updating clinic configuration filtered by target_clinic_id
        await sb(supabase.table("clinics")
        .update({"config": cfg})
        .eq("id", target_clinic_id))
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
            "cancellation_window_hours": cfg.get("cancellation_window_hours"),
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
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
    """Get all integration connectors with status info (credentials masked)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        # unscoped: tenant-scoped operation with verified clinic authorization
        query = supabase.table("integration_connectors").select("*")
        query = query.eq("clinic_id", effective_clinic_id)
        if branch_id:
            query = query.eq("branch_id", branch_id)
        result = await sb(query.order("created_at", desc=True).limit(2000))
        connectors = [_mask_connector(row) for row in (result.data or [])]
        return {"connectors": connectors}
    except Exception as e:
        logger.error(f"Failed to get connectors: {e}")
        raise HTTPException(status_code=500, detail="Failed to get connectors")


@router.get("/connectors/types")
async def get_connector_types(
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
    """List every registered connector type and its credential schema, so
    the admin panel can render a type-appropriate form instead of
    hardcoding MocDoc-specific field names."""
    from connectors.runner import CONNECTOR_REGISTRY

    display_names = {"mocdoc": "MocDoc"}
    return {
        "types": [
            {
                "type": connector_type,
                "display_name": display_names.get(connector_type, connector_type.title()),
                "schema": getattr(connector_cls, "CONFIG_SCHEMA", []),
            }
            for connector_type, connector_cls in CONNECTOR_REGISTRY.items()
        ]
    }


class ConnectorToggle(BaseModel):
    is_enabled: bool


@router.put("/connectors")
async def upsert_connector_credentials(
    body: ConnectorCredentialsUpdate,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
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
            # unscoped: tenant-scoped operation with verified clinic authorization
            await sb(supabase.table("branches")
            .select("id")
            .eq("id", body.branch_id)
            .eq("clinic_id", effective_clinic_id))
        )
        if not branch.data:
            raise HTTPException(status_code=404, detail="Branch not found for this clinic")

    query = (
        # unscoped: tenant-scoped operation with verified clinic authorization
        supabase.table("integration_connectors")
        .select("*")
        .eq("clinic_id", effective_clinic_id)
        .eq("connector_type", body.connector_type)
    )
    query = query.eq("branch_id", body.branch_id) if body.branch_id else query.is_("branch_id", "null")
    existing = await sb(query)
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
        from app.utils.connector_crypto import encrypt_password, fernet_key_problem

        key_issue = fernet_key_problem(key)
        if key_issue:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"CONNECTOR_ENCRYPTION_KEY on this server is invalid: {key_issue}. "
                    "Generate a valid key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
                    "and set it in the CONNECTOR_ENCRYPTION_KEY environment variable before saving credentials."
                ),
            )
        try:
            cfg["password_encrypted"] = encrypt_password(body.password.strip(), key)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Password encryption failed ({type(e).__name__}): {e}. Check CONNECTOR_ENCRYPTION_KEY.",
            )
        cfg.pop("password", None)
    if body.base_url is not None and body.base_url.strip():
        val = body.base_url.strip()
        if not val.startswith(("http://", "https://")):
            val = f"https://{val}"
        from urllib.parse import urlparse
        p = urlparse(val)
        scheme = p.scheme or "https"
        netloc = p.netloc or p.path.split("/")[0]
        cfg["base_url"] = f"{scheme}://{netloc}".rstrip("/")
    if body.clinic_slug is not None and body.clinic_slug.strip():
        cfg["clinic_slug"] = body.clinic_slug.strip()
    if body.admin_alert_phone is not None and body.admin_alert_phone.strip():
        cfg["admin_alert_phone"] = body.admin_alert_phone.strip()
    if body.poll_interval_minutes is not None:
        cfg["poll_interval_minutes"] = body.poll_interval_minutes

    # Provider routing. Both halves are clearable — a stale desk number left
    # behind after the providers are removed would keep diverting reports away
    # from patients, so an empty value deletes the key rather than keeping it.
    if body.report_routing_phone is not None:
        routing_phone = body.report_routing_phone.strip()
        if routing_phone:
            from app.utils.validators import normalize_phone, validate_phone

            normalized_routing_phone = normalize_phone(routing_phone)
            if not validate_phone(normalized_routing_phone):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"TPA desk number '{routing_phone}' is not a valid phone "
                        f"number — reports for the listed providers would silently "
                        f"go back to patients"
                    ),
                )
            cfg["report_routing_phone"] = normalized_routing_phone
        else:
            cfg.pop("report_routing_phone", None)
    if body.report_routing_providers is not None:
        routing_providers = body.report_routing_providers.strip()
        if routing_providers:
            cfg["report_routing_providers"] = routing_providers
        else:
            cfg.pop("report_routing_providers", None)

    now = datetime.now().isoformat()
    try:
        if existing_row:
            update_data = {"config": cfg, "updated_at": now}
            if body.is_enabled is not None:
                update_data["is_enabled"] = body.is_enabled
            # scoped: update connector config for validated clinic
            result = (
                # unscoped: updating integration connector configuration by connector_id
                await sb(supabase.table("integration_connectors")
                .update(update_data)
                .eq("id", existing_row["id"]))
            )
            if not result.data:
                raise HTTPException(status_code=500, detail="Update returned no data — row may have been deleted")
            saved = result.data[0]
        else:
            insert_data = {
                "clinic_id": effective_clinic_id,
                "branch_id": body.branch_id,
                "connector_type": body.connector_type,
                "config": cfg,
                "is_enabled": bool(body.is_enabled) if body.is_enabled is not None else False,
            }
            # unscoped: insert connector for validated clinic
            result = await sb(supabase.table("integration_connectors").insert(insert_data))
            if not result.data:
                raise HTTPException(status_code=500, detail="Failed to save connector credentials")
            saved = result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Connector credentials save failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error saving connector: {str(e)}")

    client_ip = request.client.host if request.client else "unknown"
    try:
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
                # This endpoint is the other way polling gets switched off, so
                # the toggle endpoint's audit row is not the whole story.
                "is_enabled": saved.get("is_enabled"),
                "enabled_changed": bool(existing_row) and bool(existing_row.get("is_enabled")) != bool(saved.get("is_enabled")),
            },
            ip_address=client_ip,
        )
    except Exception as e:
        logger.warning(f"Audit log failed for connector save (non-fatal): {e}")

    return {"success": True, "connector": _mask_connector(saved)}


@router.post("/connectors/{connector_id}/toggle")
async def toggle_connector(
    connector_id: str,
    body: ConnectorToggle,
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
    """Toggle a connector ON or OFF. This is the primary kill switch."""
    try:
        connector = (
            # unscoped: tenant-scoped operation with verified clinic authorization
            await sb(supabase.table("integration_connectors")
            .select("clinic_id")
            .eq("id", connector_id))
        )
        if not connector.data:
            raise HTTPException(status_code=404, detail="Connector not found")
        enforce_clinic_access(user, connector.data[0]["clinic_id"])

        result = (
            # unscoped: updating integration connector configuration by connector_id
            await sb(supabase.table("integration_connectors")
            .update(
                {
                    "is_enabled": body.is_enabled,
                    "updated_at": datetime.now().isoformat(),
                }
            )
            .eq("id", connector_id))
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


async def _load_connector_for_action(connector_id: str, user: "AdminUser", clinic_id: str) -> dict:
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    row = (
        # unscoped: tenant-scoped operation with verified clinic authorization
        await sb(supabase.table("integration_connectors")
        .select("*")
        .eq("id", connector_id)
        .eq("clinic_id", effective_clinic_id))
    )
    if not row.data:
        raise HTTPException(status_code=404, detail="Connector not found")
    connector = row.data[0]
    enforce_branch_scope(user, connector.get("branch_id"))
    return connector


# ── In-memory connector task tracker ──────────────────────────────
# Stores results for fire-and-forget test/run operations.
# Keyed by connector_id.  Auto-cleaned after 10 minutes.
_connector_tasks: dict[str, dict] = {}

def _clean_stale_tasks() -> None:
    """Remove task entries older than 10 minutes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    stale = [k for k, v in _connector_tasks.items() if v.get("started_at", cutoff) < cutoff]
    for k in stale:
        _connector_tasks.pop(k, None)

async def _run_connector_background(
    connector_id: str,
    clinic_id: str,
    connector_type: str,
    dry_run: bool,
    branch_id: str | None,
) -> None:
    """Run connector in background and store result in _connector_tasks."""
    from connectors.runner import run_connector
    try:
        result = await run_connector(
            clinic_id=clinic_id,
            connector_type=connector_type,
            dry_run=dry_run,
            branch_id=branch_id,
            ignore_enabled=True,
        )
        run_status = result.get("run_status", "")
        success = run_status in ("dry_run", "success", "partial") if dry_run else run_status in ("success", "partial")
        _connector_tasks[connector_id] = {
            **_connector_tasks.get(connector_id, {}),
            "status": "done",
            "success": success,
            "result": result,
            "finished_at": datetime.now(timezone.utc),
        }
    except Exception as e:
        logger.error(f"Background connector {'test' if dry_run else 'run'} failed for {connector_id}: {e}")
        _connector_tasks[connector_id] = {
            **_connector_tasks.get(connector_id, {}),
            "status": "error",
            "success": False,
            "result": {"error_message": str(e)},
            "finished_at": datetime.now(timezone.utc),
        }


@router.post("/connectors/{connector_id}/test")
async def test_connector(
    connector_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
    """Kick off a dry-run test in the background. Returns immediately.

    The test takes 1-5 minutes (browser startup, login, page scrape).
    Poll GET /connectors/{connector_id}/test-status for the result.
    """
    connector = await _load_connector_for_action(connector_id, user, clinic_id)

    _clean_stale_tasks()
    _connector_tasks[connector_id] = {
        "status": "running",
        "mode": "test",
        "started_at": datetime.now(timezone.utc),
    }

    asyncio.ensure_future(
        _run_connector_background(
            connector_id=connector_id,
            clinic_id=connector["clinic_id"],
            connector_type=connector.get("connector_type", "mocdoc"),
            dry_run=True,
            branch_id=connector.get("branch_id"),
        )
    )

    return {"success": True, "status": "running", "message": "Test started — this takes 1-2 minutes. Polling for result..."}


@router.get("/connectors/{connector_id}/test-status")
async def test_connector_status(
    connector_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
    """Poll for the result of a background test/run operation."""
    connector = await _load_connector_for_action(connector_id, user, clinic_id)

    task = _connector_tasks.get(connector_id)
    if task:
        return task

    # No in-process task record — either genuinely idle, or a prior run
    # was interrupted by a server restart (deploy/OOM) and the in-memory
    # dict was wiped. Fall back to the DB advisory lock: it's written at
    # run-start and outlives the process that set it.
    from connectors.runner import LOCK_LEASE

    locked_at = connector.get("locked_at")
    if locked_at:
        try:
            dt = datetime.fromisoformat(locked_at.replace("Z", "+00:00"))
            elapsed = datetime.now(timezone.utc) - dt
        except Exception:
            elapsed = None
        if elapsed is not None and elapsed < LOCK_LEASE:
            return {"status": "running", "message": "Test still in progress (resumed after restart)..."}
        # Lease expired: the run that held it never finished. Clear it so
        # the next test isn't blocked for the rest of the lease window.
        from connectors.runner import release_connector_lock
        await release_connector_lock(connector_id)
        return {
            "status": "error",
            "success": False,
            "result": {"error_message": "Previous test was interrupted (server restarted mid-run). Please try again."},
        }

    return {"status": "idle", "message": "No test in progress"}


@router.post("/connectors/{connector_id}/run-now")
async def run_connector_now(
    connector_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
    """Trigger one full connector poll cycle in the background. Returns immediately.

    Poll GET /connectors/{connector_id}/test-status for the result.
    """
    connector = await _load_connector_for_action(connector_id, user, clinic_id)

    _clean_stale_tasks()
    _connector_tasks[connector_id] = {
        "status": "running",
        "mode": "run",
        "started_at": datetime.now(timezone.utc),
    }

    asyncio.ensure_future(
        _run_connector_background(
            connector_id=connector_id,
            clinic_id=connector["clinic_id"],
            connector_type=connector.get("connector_type", "mocdoc"),
            dry_run=False,
            branch_id=connector.get("branch_id"),
        )
    )

    return {"success": True, "status": "running", "message": "Run started — this takes 2-5 minutes. Polling for result..."}


@router.get("/connectors/{connector_id}/audit-log")
async def get_connector_audit_log(
    connector_id: str,
    limit: int = 20,
    user: AdminUser = Depends(require_permission("CONNECTOR_MANAGE")),
):
    """Get recent audit log entries (run history — found/uploaded/failed
    counts per poll) for a connector."""
    try:
        # First get the connector to find its clinic_id, type and branch
        connector = (
            # unscoped: tenant-scoped operation with verified clinic authorization
            await sb(supabase.table("integration_connectors")
            .select("clinic_id, connector_type, branch_id")
            .eq("id", connector_id)
            .single())
        )

        if not connector.data:
            raise HTTPException(status_code=404, detail="Connector not found")
        enforce_clinic_access(user, connector.data["clinic_id"])

        query = (
            # unscoped: tenant-scoped operation with verified clinic authorization
            supabase.table("connector_audit_log")
            .select("*")
            .eq("clinic_id", connector.data["clinic_id"])
            .eq("connector_type", connector.data["connector_type"])
        )
        branch_id = connector.data.get("branch_id")
        query = query.eq("branch_id", branch_id) if branch_id else query.is_("branch_id", "null")
        logs = await sb(query.order("created_at", desc=True).limit(limit))

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
        # unscoped: tenant-scoped operation with verified clinic authorization
        query = supabase.table("connector_failed_reports").select("*")
        query = query.eq("clinic_id", effective_clinic_id)
        if branch_id:
            query = query.eq("branch_id", branch_id)
        if unresolved_only:
            query = query.is_("resolved_at", "null")
        result = await sb(query.order("last_attempt_at", desc=True).limit(2000))
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
        # unscoped: updating connector failed report resolution status by report_id
        query = supabase.table("connector_failed_reports").update(
            {"resolved_at": datetime.now().isoformat()}
        ).eq("id", failed_report_id)
        query = query.eq("clinic_id", effective_clinic_id)
        result = await sb(query)
        if not result.data:
            raise HTTPException(status_code=404, detail="Failed report not found")
        return {"success": True, "failed_report": result.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to resolve connector failed report: {e}")
        raise HTTPException(status_code=500, detail="Failed to resolve failed report")


class ResolveMatchRequest(BaseModel):
    patient_phone: str
    patient_name: Optional[str] = None
    send_now: bool = True


class ResendReportRequest(BaseModel):
    new_phone: Optional[str] = None


@router.get("/reports/queue")
async def get_diagnostic_reports_queue(
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    user: AdminUser = Depends(require_permission("REPORTS_VIEW")),
):
    """Retrieve unified triage queue for diagnostic center operations:
    - Reports in 'needs_review' state (conflict or missing phone)
    - Reports in 'failed' state
    - Unresolved connector failed reports
    """
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    target_branch = user.branch_id if user.role == "staff" and user.branch_id else branch_id
    if target_branch:
        enforce_branch_scope(user, target_branch)

    try:
        # 1. Fetch lab_reports in needs_review or failed
        # unscoped: tenant-scoped operation with verified clinic authorization
        lr_query = supabase.table("lab_reports").select("*")
        lr_query = lr_query.eq("clinic_id", effective_clinic_id)

        if status_filter == "needs_review":
            lr_query = lr_query.eq("status", "needs_review")
        elif status_filter == "failed":
            lr_query = lr_query.eq("status", "failed")
        else:
            lr_query = lr_query.in_("status", ["needs_review", "failed"])

        lr_res = await sb(lr_query.order("uploaded_at", desc=True).limit(100))
        lab_reports_queue = lr_res.data or []

        # 2. Fetch connector_failed_reports that are unresolved
        # unscoped: tenant-scoped operation with verified clinic authorization
        cfr_query = supabase.table("connector_failed_reports").select("*").is_("resolved_at", "null")
        cfr_query = cfr_query.eq("clinic_id", effective_clinic_id)
        if target_branch:
            cfr_query = cfr_query.eq("branch_id", target_branch)
        cfr_res = await sb(cfr_query.order("last_attempt_at", desc=True).limit(50))
        connector_failures = cfr_res.data or []

        return {
            "needs_review": [r for r in lab_reports_queue if r.get("status") == "needs_review"],
            "failed_reports": [r for r in lab_reports_queue if r.get("status") == "failed"],
            "connector_failures": connector_failures,
            "total_queued": len(lab_reports_queue) + len(connector_failures),
        }
    except Exception as e:
        logger.error(f"Failed to get diagnostic reports queue: {e}")
        raise HTTPException(status_code=500, detail="Failed to get reports queue")


@router.post("/reports/{report_id}/resolve-match")
async def resolve_report_match(
    report_id: str,
    body: ResolveMatchRequest,
    clinic_id: str = "default",
    request: Request = None,
    user: AdminUser = Depends(require_permission("REPORTS_RESOLVE")),
):
    """Manually resolve a report in 'needs_review' state and optionally trigger WhatsApp send."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)

    existing = (
        # unscoped: tenant-scoped operation with verified clinic authorization
        await sb(supabase.table("lab_reports")
        .select("*")
        .eq("id", report_id)
        .eq("clinic_id", effective_clinic_id))
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Report not found")
    report = existing.data[0]

    norm_phone = normalize_phone(body.patient_phone)
    if not validate_phone(norm_phone):
        raise HTTPException(status_code=400, detail="Invalid phone number format")

    update_payload = {
        "patient_phone": norm_phone,
        "patient_name": body.patient_name.strip() if body.patient_name else report.get("patient_name"),
        "match_source": "manual",
        "match_confidence": 1.0,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "resolved_by": user.username,
        "status": "matched",
    }

    # A row whose file_path never left the "pending_review/" sentinel has no
    # stored PDF, so there is nothing to deliver. Saying so beats silently
    # marking it resolved and letting staff believe the patient got the report.
    has_stored_pdf = bool(report.get("file_path")) and not str(
        report.get("file_path", "")
    ).startswith("pending_review")
    if body.send_now and not has_stored_pdf:
        raise HTTPException(
            status_code=409,
            detail=(
                "This report has no stored PDF (the source download failed when it "
                "was held). Re-upload it from the admin panel to send it."
            ),
        )

    # If send_now is True, attempt delivery via LabReportService
    if body.send_now and has_stored_pdf:
        try:
            lab_service = LabReportService()
            # scoped: update lab report queue item
            # unscoped: update lab report queue item
            await sb(supabase.table("lab_reports").update(update_payload).eq("id", report_id))
            await lab_service.resend_report(
                report_id, new_phone=norm_phone, clinic_id=effective_clinic_id
            )
            update_payload["status"] = "sent"
        except Exception as e:
            logger.error(f"Failed to resend resolved report {report_id}: {e}")
            update_payload["status"] = "failed"
            update_payload["error_message"] = str(e)
            # unscoped: update lab report queue item
            await sb(supabase.table("lab_reports").update(update_payload).eq("id", report_id))
    else:
            # unscoped: update lab report queue item
        await sb(supabase.table("lab_reports").update(update_payload).eq("id", report_id))

    client_ip = request.client.host if request and request.client else "unknown"
    await log_admin_action(
        user=user,
        action="resolve_report_match",
        resource_type="lab_report",
        resource_id=report_id,
        details={
            "old_phone": report.get("patient_phone"),
            "new_phone": norm_phone,
            "patient_name": update_payload.get("patient_name"),
            "send_now": body.send_now,
        },
        ip_address=client_ip,
    )

    return {
        "success": True,
        "report_id": report_id,
        "status": update_payload["status"],
        "patient_phone": norm_phone,
    }


@router.post("/reports/release-held-walkins")
async def release_held_walkin_reports(
    clinic_id: str = "default",
    limit: int = 200,
    request: Request = None,
    user: AdminUser = Depends(require_permission("REPORTS_RESOLVE")),
):
    """Deliver walk-in reports that were held by the old verification gate.

    Reports held with match_source="moc_doc_only" were parked because the phone
    number was not in the clinic's patient list. For a diagnostic centre that is
    true of every walk-in, so the gate blocked everything: a live client had 27
    discovered, 27 held, 0 delivered. The policy is now delivery-by-default, but
    the connector will not re-offer these — external_report_id dedup means an
    already-recorded report is never re-processed — so the backlog needs an
    explicit flush.

    Only rows with a stored PDF can be sent. Rows whose file_path never left the
    "pending_review/" sentinel have no file (the source download failed when
    they were held) and are reported separately rather than silently skipped.
    """
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    limit = max(1, min(int(limit or 200), 500))

    query = (
        supabase.table("lab_reports")
        .select("id, patient_phone, file_path, report_name, match_source, status")
        .eq("status", "needs_review")
        .eq("match_source", "moc_doc_only")
        .eq("clinic_id", effective_clinic_id)
        .limit(limit)
    )

    try:
        held = await sb(query)
    except Exception as e:
        logger.error(f"Could not list held walk-in reports: {e}")
        raise HTTPException(status_code=503, detail="Could not read the review queue")

    rows = held.data or []
    lab_service = LabReportService()
    sent, failed, no_pdf = 0, 0, 0
    errors: list[dict] = []

    for row in rows:
        file_path = str(row.get("file_path") or "")
        if not file_path or file_path.startswith("pending_review"):
            no_pdf += 1
            continue
        try:
            await lab_service.resend_report(row["id"], clinic_id=effective_clinic_id)
            # resend_report does not clear the review state on its own.
            await sb(
                # unscoped: unique_row_key
                supabase.table("lab_reports")
                .update(
                    {
                        "status": "sent",
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                        "resolved_by": user.username,
                        "error_message": None,
                    }
                )
                .eq("id", row["id"])
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"Release of held report {row.get('id')} failed: {e}")
            if len(errors) < 20:
                errors.append({"report_id": row.get("id"), "error": str(e)[:200]})

    await log_admin_action(
        user=user,
        action="release_held_walkin_reports",
        resource_type="lab_report",
        resource_id=None,
        details={"sent": sent, "failed": failed, "no_pdf": no_pdf, "examined": len(rows)},
        ip_address=request.client.host if (request and request.client) else "unknown",
    )

    return {
        "success": True,
        "examined": len(rows),
        "sent": sent,
        "failed": failed,
        "no_stored_pdf": no_pdf,
        "errors": errors,
        "message": (
            f"Delivered {sent} held walk-in report(s)."
            + (f" {failed} failed." if failed else "")
            + (
                f" {no_pdf} had no stored PDF and must be re-fetched from the source."
                if no_pdf
                else ""
            )
        ),
    }


@router.post("/reports/{report_id}/resend")
async def resend_lab_report_from_queue(
    report_id: str,
    body: Optional[ResendReportRequest] = None,
    clinic_id: str = "default",
    request: Request = None,
    user: AdminUser = Depends(require_permission("REPORTS_RESOLVE")),
):
    """Resend a previously failed or existing lab report via WhatsApp."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)

    existing = (
        # unscoped: tenant-scoped operation with verified clinic authorization
        await sb(supabase.table("lab_reports")
        .select("*")
        .eq("id", report_id)
        .eq("clinic_id", effective_clinic_id))
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Report not found")
    report = existing.data[0]

    new_phone = body.new_phone if body and body.new_phone else None
    if new_phone:
        new_phone = normalize_phone(new_phone)
        if not validate_phone(new_phone):
            raise HTTPException(status_code=400, detail="Invalid phone number format")

    lab_service = LabReportService()
    try:
        res = await lab_service.resend_report(
            report_id, new_phone=new_phone, clinic_id=effective_clinic_id
        )
    except Exception as e:
        logger.error(f"Resend failed for report {report_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to resend report: {str(e)}")

    client_ip = request.client.host if request and request.client else "unknown"
    await log_admin_action(
        user=user,
        action="resend_lab_report",
        resource_type="lab_report",
        resource_id=report_id,
        details={"new_phone": new_phone},
        ip_address=client_ip,
    )

    return {"success": True, "report": res}


@router.get("/lab-reports/deliveries")
@router.get("/reports/deliveries")
async def get_lab_report_deliveries(
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    days: int = 7,
    state: str = "all",
    user: AdminUser = Depends(require_permission("REPORTS_VIEW")),
):
    """Retrieve detailed per-report WhatsApp delivery log and receipt status.

    Returns newest first with masked phone numbers for privacy.
    """
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    target_branch = user.branch_id if user.role == "staff" and user.branch_id else branch_id
    if target_branch:
        enforce_branch_scope(user, target_branch)

    from app.utils.validators import mask_phone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    try:
        # Match the proven stats endpoint pattern: fetch rows, filter in Python.
        # This avoids PostgREST timezone comparison issues with .gte() on TIMESTAMPTZ.
        cols = (
            "id, clinic_id, patient_phone, patient_name, report_name, report_type, "
            "status, sent_at, uploaded_at, source, external_report_id, error_message, "
            "whatsapp_message_id, delivery_status, delivery_error, delivery_updated_at"
        )
        try:
            # unscoped: tenant-scoped operation with verified clinic authorization
            query = supabase.table("lab_reports").select(cols)
            query = query.eq("clinic_id", effective_clinic_id)
            res = await sb(query.order("uploaded_at", desc=True).limit(2000))
            all_records = res.data or []
        except Exception:
            # Fallback for older schema variations
            try:
                # unscoped: tenant-scoped operation with verified clinic authorization
                query = supabase.table("lab_reports").select("*")
                query = query.eq("clinic_id", effective_clinic_id)
                res = await sb(query.order("uploaded_at", desc=True).limit(2000))
                all_records = res.data or []
            except Exception:
                # unscoped: tenant-scoped operation with verified clinic authorization
                query = supabase.table("lab_reports").select("*")
                query = query.eq("clinic_id", effective_clinic_id)
                res = await sb(query.limit(2000))
                all_records = res.data or []

        # Sort newest first using either uploaded_at or sent_at
        all_records.sort(
            key=lambda r: r.get("uploaded_at") or r.get("sent_at") or "",
            reverse=True,
        )

        # Python-side date filter: keep records within cutoff or where date is unset
        records = [
            r for r in all_records
            if not (r.get("uploaded_at") or r.get("sent_at")) or (r.get("uploaded_at") or r.get("sent_at") or "") >= cutoff
        ]

        logger.info(
            f"Delivery log query: clinic={effective_clinic_id}, cutoff={cutoff}, "
            f"state_filter={state}, total_fetched={len(all_records)}, after_date_filter={len(records)}"
        )




        deliveries = []
        for r in records:
            status_col = (r.get("status") or "").lower()
            delivery_status = (r.get("delivery_status") or "").lower()
            sent_at = r.get("sent_at")
            delivery_updated_at = r.get("delivery_updated_at")

            # Standardized primary state derivation:
            if status_col == "sent" or delivery_status in ("read", "delivered", "sent"):
                derived_state = "delivered"
            elif status_col == "needs_review":
                derived_state = "needs_review"
            elif status_col == "failed" or delivery_status == "failed":
                derived_state = "failed"
            else:
                derived_state = status_col or "pending"


            if state != "all":
                target_state = state.lower()
                if derived_state != target_state:
                    continue

            phone = r.get("patient_phone") or ""
            deliveries.append({
                "id": r.get("id"),
                "patient_name": r.get("patient_name") or "Unknown",
                "patient_phone": mask_phone(phone),
                "report_name": r.get("report_name") or "Lab Report",
                "report_type": r.get("report_type") or "Laboratory",
                "source": r.get("source") or "admin",
                "external_report_id": r.get("external_report_id"),
                "status": status_col,
                "sent_at": sent_at,
                "delivery_status": delivery_status,
                "delivery_updated_at": delivery_updated_at,
                "delivery_error": r.get("delivery_error") or r.get("error_message"),
                "match_confidence": r.get("match_confidence"),
                "match_source": r.get("match_source"),
                "state": derived_state,
                "uploaded_at": r.get("uploaded_at"),
            })


        return {"deliveries": deliveries, "total": len(deliveries)}
    except Exception as e:
        logger.error(f"Failed to get lab report deliveries: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get lab report deliveries")



@router.get("/diagnostic/stats")
async def get_diagnostic_stats(
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    user: AdminUser = Depends(require_permission("REPORTS_VIEW")),
):
    """Get operational stats for Diagnostic Center dashboard."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    target_branch = user.branch_id if user.role == "staff" and user.branch_id else branch_id
    if target_branch:
        enforce_branch_scope(user, target_branch)

    # The clinic's day, not UTC's. At UTC midnight it is already 05:30 in IST,
    # so a UTC-based "today" silently attributed every report delivered between
    # 00:00 and 05:30 IST to the previous day.
    now_local = datetime.now(CLINIC_TZ)
    today_start = (
        now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
        .isoformat()
    )
    retention_cutoff = (datetime.now(timezone.utc) - timedelta(days=80)).isoformat()

    try:
        # 1. Query lab_reports for TODAY only.
        #
        # This used to select every lab_reports row for the clinic with no
        # date filter, no ordering and no limit, then filter "today" in Python.
        # PostgREST caps a response at 1000 rows, so once a busy diagnostic
        # center passed 1000 lifetime reports (about two weeks at ~100/day)
        # the tiles were computed from an arbitrary 1000-row slice and stopped
        # matching reality. Filter in SQL so the counts are exact.
        lr_query = (
            # unscoped: tenant-scoped operation with verified clinic authorization
            supabase.table("lab_reports")
            .select("id, status, uploaded_at, sent_at, ai_summary, ai_summary_sent")
            .gte("uploaded_at", today_start)
        )
        lr_query = lr_query.eq("clinic_id", effective_clinic_id)

        lr_res = await sb(lr_query.order("uploaded_at", desc=True).limit(5000))
        today_reports = lr_res.data or []

        sent_today = sum(1 for r in today_reports if r.get("status") == "sent")
        failed_today = sum(1 for r in today_reports if r.get("status") == "failed")

        # Of the reports actually delivered today, how many carried their AI
        # summary to the patient? A stored ai_summary only proves it was
        # generated — outside the 24h window the document template carries no
        # summary text, so the patient receives the PDF with no explanation.
        summary_delivered_today = sum(
            1 for r in today_reports
            if r.get("status") == "sent" and r.get("ai_summary_sent")
        )
        summary_missing_today = sent_today - summary_delivered_today

        # Open triage queues are deliberately all-time, not today-only: a report
        # stuck since yesterday still needs a human. Counted server-side so they
        # are not truncated by the row cap above.
        def _count(table: str, apply) -> int:
            # unscoped: tenant-scoped operation with verified clinic authorization
            q = supabase.table(table).select("id", count="exact")
            q = q.eq("clinic_id", effective_clinic_id)
            return apply(q).limit(1).execute().count or 0

        needs_review_total = _count(
            "lab_reports", lambda q: q.eq("status", "needs_review")
        )
        # "Delivery Failures" must agree with the Failed Deliveries Queue below
        # it. The queue counts unresolved connector_failed_reports (PDF download
        # failed, name conflict, WhatsApp send rejected); the tile counted only
        # lab_reports.status == 'failed', which those never produce — so the
        # dashboard showed "0 failures" directly above a list of 51 of them.
        connector_failures_open = _count(
            "connector_failed_reports",
            lambda q: q.is_("resolved_at", "null") if not target_branch
            else q.is_("resolved_at", "null").eq("branch_id", target_branch),
        )
        lab_failures_open = _count("lab_reports", lambda q: q.eq("status", "failed"))
        delivery_failures_open = connector_failures_open + lab_failures_open

        expiring_soon = _count(
            "lab_reports",
            lambda q: q.lte("uploaded_at", retention_cutoff).not_.is_("file_path", "null"),
        )

        # 2. Connector status
        # unscoped: tenant-scoped operation with verified clinic authorization
        conn_query = supabase.table("integration_connectors").select("*")
        conn_query = conn_query.eq("clinic_id", effective_clinic_id)
        if target_branch:
            conn_query = conn_query.eq("branch_id", target_branch)
        conn_res = await sb(conn_query)
        connectors = sorted(conn_res.data or [], key=lambda c: c.get("updated_at") or "", reverse=True)

        evaluated = []
        for c in connectors:
            is_enabled = c.get("is_enabled", False)
            last_error = c.get("last_error")
            poll_minutes = (c.get("config") or {}).get("poll_interval_minutes", 10)
            stale_after = timedelta(minutes=poll_minutes * 3)
            last_run_at = c.get("last_run_at")
            next_run_at = None
            age = None
            seconds_since_last_run = None

            if last_run_at:
                try:
                    dt = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
                    next_run_at = (dt + timedelta(minutes=poll_minutes)).isoformat()
                    age = datetime.now(timezone.utc) - dt
                    seconds_since_last_run = int(age.total_seconds())
                except Exception:
                    next_run_at = None

            if not is_enabled:
                health = "disabled"          # grey  — OFF
                next_run_at = None           # nothing polls a disabled connector
            elif age is None:
                health = "never_run"         # grey  — NEVER RUN
            elif age > stale_after:
                health = "stalled"           # red   — NOT RUNNING (worker dead)
            elif last_error:
                health = "warning"           # amber — RUNNING WITH ERRORS
            else:
                health = "healthy"           # green — ACTIVE / HEALTHY

            # Check if currently executing (lock held within 15 min)
            is_running_now = False
            locked_at = c.get("locked_at")
            if locked_at:
                try:
                    ldt = datetime.fromisoformat(locked_at.replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) - ldt < timedelta(minutes=15):
                        is_running_now = True
                except Exception:
                    pass

            evaluated.append({
                "id": c.get("id"),
                "branch_id": c.get("branch_id"),
                "connector_type": c.get("connector_type"),
                "is_enabled": is_enabled,
                "last_run_at": last_run_at,
                "last_success_at": c.get("last_success_at"),
                "last_error": last_error,
                "health": health,
                "poll_interval_minutes": poll_minutes,
                "next_run_at": next_run_at,
                "seconds_since_last_run": seconds_since_last_run,
                "is_running_now": is_running_now,
            })

        connector_info = None
        if evaluated:
            # Headline selection.
            #
            # Previously always evaluated[0] — the most recently UPDATED row.
            # With two connectors that produced a genuinely confusing screen:
            # the banner showed branch B's error while the Run History panel
            # below it (which queries ONE connector by id) showed branch A's
            # successful run. Same screen, two different connectors, nothing
            # saying so — it reads as "the run succeeded AND errored".
            #
            # Prefer an ENABLED connector that needs attention, so the headline
            # agrees with the "N of M connectors need attention" line beneath
            # it. Disabled rows stay excluded, which preserves the original
            # intent: a decommissioned branch must not pin the dashboard to its
            # old error. Falls back to most-recently-updated when everything
            # enabled is healthy.
            attention = [
                e for e in evaluated
                if e["is_enabled"] and e["health"] not in ("healthy", "disabled")
            ]
            connector_info = attention[0] if attention else evaluated[0]
            connector_info["connector_count"] = len(evaluated)
            connector_info["unhealthy_count"] = sum(
                1 for e in evaluated if e["health"] != "healthy"
            )

        return {
            "reports_today": {
                "total": len(today_reports),
                "sent": sent_today,
                "failed": delivery_failures_open,
                "failed_today": failed_today,
                "connector_failures_open": connector_failures_open,
                "needs_review": needs_review_total,
                "ai_summary_delivered": summary_delivered_today,
                "ai_summary_missing": summary_missing_today,
            },
            "expiring_retention_count": expiring_soon,
            "connector": connector_info,
        }
    except Exception as e:
        logger.error(f"Failed to get diagnostic stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get diagnostic stats")


@router.get("/audit-logs")
async def get_admin_audit_logs(
    clinic_id: str = "default",
    limit: int = 50,
    user: AdminUser = Depends(require_admin),
):
    """Get administrative staff action audit logs for compliance auditing (NABH / DPDP)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        # unscoped: tenant-scoped operation with verified clinic authorization
        query = supabase.table("admin_audit_logs").select("*")
        query = query.eq("clinic_id", effective_clinic_id)
        result = await sb(query.order("created_at", desc=True).limit(limit))
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
    """Get all branches for a clinic.

    Readable by any authenticated account in the clinic, like GET /doctors.
    Branch names and addresses are what the bot reads out to patients, so this
    is not privileged data — and gating it behind require_admin made the whole
    Branches page 403 for staff who legitimately hold DOCTOR_BRANCH_ASSIGN,
    hiding the doctor-assignment UI they had been granted. Writes below stay
    permission-gated.
    """
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        # unscoped: tenant-scoped operation with verified clinic authorization
        query = supabase.table("branches").select("*")
        query = query.eq("clinic_id", effective_clinic_id)
        query = query.order("display_order").limit(2000)
        result = await sb(query)
        return {"branches": result.data or []}
    except Exception as e:
        logger.error(f"Error getting branches: {e}")
        raise HTTPException(status_code=500, detail="Failed to get branches")


@router.post("/branches")
async def create_branch(
    branch: BranchCreate,
    clinic_id: str = "default",
    request: Request = None,
    user: AdminUser = Depends(require_permission("BRANCHES_MANAGE")),
):
    """Create a new branch."""
    if user.role not in ("super_admin", "clinic_admin") and getattr(user, "branch_id", None):
        raise HTTPException(
            status_code=403,
            detail="Your account is pinned to a single branch and cannot create new branches.",
        )
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
                    # unscoped: tenant-scoped operation with verified clinic authorization
                    await sb(supabase.table("clinics")
                    .select("name")
                    .eq("id", effective_clinic_id)
                    .limit(1))
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
        # unscoped: inserting new branch record with explicit clinic_id
        result = await sb(supabase.table("branches").insert(branch_data))

        # Invalidate branch cache
        from app.services.tenant import invalidate_branch_cache

        invalidate_branch_cache(effective_clinic_id)

        # A branch is the billing unit: the platform charges per location, so
        # adding one changes what this clinic owes. Audit it explicitly rather
        # than leaving branches.created_at as the only trace.
        new_branch = result.data[0]
        await log_admin_action(
            user=user,
            action="create_branch",
            resource_type="branch",
            resource_id=str(new_branch.get("id")),
            details={
                "name": new_branch.get("name"),
                "short_name": new_branch.get("short_name"),
                "is_diagnostic": new_branch.get("is_diagnostic", False),
                "billing_impact": "adds_one_billable_location",
            },
            ip_address=request.client.host if (request and request.client) else "unknown",
        )

        return {"success": True, "branch": new_branch}
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
    request: Request = None,
    user: AdminUser = Depends(require_permission("BRANCHES_MANAGE")),
):
    """Update a branch."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    # Delegated BRANCHES_MANAGE staff can be pinned to one branch. Verify this
    # branch belongs to the active clinic AND is theirs to touch before any
    # mutation — enforce_clinic_access alone would let a pinned staff edit a
    # sibling branch.
    resolve_owned_branch(user, branch_id, effective_clinic_id)
    try:
        update_data = branch.dict(exclude_unset=True)
        if not update_data:
            return {"message": "No fields to update"}

        # Re-derive name when short_name changes and name wasn't explicitly set
        if "short_name" in update_data and "name" not in update_data:
            try:
                clinic_result = (
                    # unscoped: tenant-scoped operation with verified clinic authorization
                    await sb(supabase.table("clinics")
                    .select("name")
                    .eq("id", effective_clinic_id)
                    .limit(1))
                )
                clinic_name = (
                    clinic_result.data[0]["name"]
                    if clinic_result.data
                    else "Clinic"
                )
            except Exception:
                clinic_name = "Clinic"
            update_data["name"] = f"{clinic_name} - {update_data['short_name']}"

        # unscoped: updating branch configuration by branch_id within verified clinic
        query = supabase.table("branches").update(update_data)
        query = query.eq("clinic_id", effective_clinic_id)
        result = await sb(query.eq("id", branch_id))
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



_BRANCH_DEPENDENT_TABLES = ["appointments", "doctor_branches", "integration_connectors", "clinic_admins"]


@router.delete("/branches/{branch_id}")
async def delete_branch(
    branch_id: str,
    clinic_id: str = "default",
    request: Request = None,
    user: AdminUser = Depends(require_permission("BRANCHES_MANAGE")),
):
    """Permanently delete a branch if nothing references it (appointments,
    doctor assignments, connectors, staff accounts). Otherwise deactivate
    it and report why, so duplicate/unused branches can actually be
    removed instead of accumulating forever as inactive clutter."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    resolve_owned_branch(user, branch_id, effective_clinic_id)
    try:
        from app.services.tenant import invalidate_branch_cache

        for table in _BRANCH_DEPENDENT_TABLES:
            # unscoped: tenant-scoped operation with verified clinic authorization
            dep = await sb(supabase.table(table).select("id").eq("branch_id", branch_id).limit(1))
            if dep.data:
                # unscoped: updating branch configuration by branch_id within verified clinic
                query = supabase.table("branches").update({"is_active": False})
                query = query.eq("clinic_id", effective_clinic_id)
                await sb(query.eq("id", branch_id))
                invalidate_branch_cache(effective_clinic_id)
                await log_admin_action(
                    user=user,
                    action="deactivate_branch",
                    resource_type="branch",
                    resource_id=branch_id,
                    details={
                        "reason": f"has {table} records",
                        "billing_impact": "removes_one_billable_location",
                    },
                    ip_address=request.client.host if (request and request.client) else "unknown",
                )
                label = table.replace("_", " ")
                return {
                    "success": True,
                    "deleted": False,
                    "message": f"Branch has existing {label} records — deactivated instead of deleted.",
                }

        # unscoped: deleting branch record by branch_id within verified clinic
        query = supabase.table("branches").delete()
        query = query.eq("clinic_id", effective_clinic_id)
        await sb(query.eq("id", branch_id))
        invalidate_branch_cache(effective_clinic_id)
        await log_admin_action(
            user=user,
            action="delete_branch",
            resource_type="branch",
            resource_id=branch_id,
            details={"billing_impact": "removes_one_billable_location"},
            ip_address=request.client.host if (request and request.client) else "unknown",
        )

        return {"success": True, "deleted": True, "message": "Branch permanently deleted."}
    except Exception as e:
        logger.error(f"Error deleting branch: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete branch")


@router.get("/branches/{branch_id}/doctors")
async def get_branch_doctors(
    branch_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("DOCTOR_BRANCH_ASSIGN")),
):
    """Get doctors assigned to a specific branch."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    resolve_owned_branch(user, branch_id, effective_clinic_id)
    try:
        result = (
        # unscoped: doctor branch association
            await sb(supabase.table("doctor_branches")
            .select("*, doctors(*)")
            .eq("branch_id", branch_id))
        )
        return {"doctor_branches": result.data or []}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting branch doctors: {e}")
        raise HTTPException(status_code=500, detail="Failed to get branch doctors")


@router.post("/branches/{branch_id}/doctors")
async def assign_doctor_to_branch(
    branch_id: str,
    body: DoctorBranchAssign,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("DOCTOR_BRANCH_ASSIGN")),
):
    """Assign a doctor to a branch with session control."""
    branch_clinic_id = enforce_clinic_access(user, clinic_id)
    resolve_owned_branch(user, branch_id, branch_clinic_id)

    # Verify doctor exists and belongs to the branch's clinic
    # unscoped: tenant-scoped operation with verified clinic authorization
    doc_query = (
        supabase.table("doctors")
        .select("id")
        .eq("id", body.doctor_id)
        .eq("clinic_id", branch_clinic_id)
    )
    doc_res = await sb(doc_query)
    if not doc_res.data:
        raise HTTPException(status_code=404, detail="Doctor not found in this clinic")

    try:
        data = {
            "doctor_id": body.doctor_id,
            "branch_id": branch_id,
            "session": body.session,
        }
        # unscoped: assign doctor to branch for validated branch
        result = await sb(supabase.table("doctor_branches").insert(data))

        client_ip = request.client.host if request.client else "unknown"
        await log_admin_action(
            user=user,
            action="assign_doctor_to_branch",
            resource_type="doctor_branch",
            resource_id=f"{branch_id}:{body.doctor_id}",
            details={"session": body.session},
            ip_address=client_ip,
        )
        invalidate_doctor_cache(clinic_id=branch_clinic_id)

        return result.data[0]
    except HTTPException:
        raise
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
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("DOCTOR_BRANCH_ASSIGN")),
):
    """Remove a doctor from a branch."""
    branch = resolve_owned_branch(user, branch_id, enforce_clinic_access(user, clinic_id))
    try:
        # unscoped: doctor branch association
        await sb(supabase.table("doctor_branches").delete().eq("branch_id", branch_id).eq(
            "doctor_id", doctor_id
        ))

        client_ip = request.client.host if request.client else "unknown"
        await log_admin_action(
            user=user,
            action="remove_doctor_from_branch",
            resource_type="doctor_branch",
            resource_id=f"{branch_id}:{doctor_id}",
            ip_address=client_ip,
        )
        invalidate_doctor_cache(clinic_id=branch.get("clinic_id"))

        return {"success": True}
    except HTTPException:
        raise
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
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("DOCTOR_BRANCH_ASSIGN")),
):
    """Update a doctor's session assignment at a branch."""
    branch = resolve_owned_branch(user, branch_id, enforce_clinic_access(user, clinic_id))
    try:
        result = (
        # unscoped: doctor branch association
            await sb(supabase.table("doctor_branches")
            .update({"session": body.session})
            .eq("branch_id", branch_id)
            .eq("doctor_id", doctor_id))
        )
        if not result.data:
            raise HTTPException(
                status_code=404, detail="Doctor-branch assignment not found"
            )

        client_ip = request.client.host if request.client else "unknown"
        await log_admin_action(
            user=user,
            action="update_doctor_branch_session",
            resource_type="doctor_branch",
            resource_id=f"{branch_id}:{doctor_id}",
            details={"session": body.session},
            ip_address=client_ip,
        )
        invalidate_doctor_cache(clinic_id=branch.get("clinic_id"))

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
    clinic_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Get outbound message usage for the current billing period.

    SECURITY: This is a CUSTOMER-FACING endpoint. The response MUST NOT
    contain any fields related to costs, pricing, Meta rates, markup,
    or Kriya's messaging economics. Only volumetric counts are returned.
    """
    try:
        from app.services.message_accounting import get_clinic_usage

        # Determine and enforce which clinic this admin belongs to
        effective_clinic_id = enforce_clinic_access(user, clinic_id)
        plan_name = "essential"
        try:
            clinic_data = await get_clinic_by_id(effective_clinic_id)
            plan_name = clinic_data.get("plan", "essential")
        except Exception:
            pass

        usage = await get_clinic_usage(effective_clinic_id, plan_name)

        await log_admin_action(
            user=user,
            action="VIEW_MESSAGING_USAGE",
            resource_type="billing",
            details={"clinic_id": effective_clinic_id, "messages_sent": usage.get("messages_sent", 0)},
        )

        return usage

    except HTTPException:
        raise
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
    clinic_id: Optional[str] = None,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    user: AdminUser = Depends(verify_credentials),
):
    """Retrieve in-app broadcast alerts for the authenticated clinic admin."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id) if clinic_id else await resolve_clinic_id_for_write(user)
    notifications = await broadcast_service.get_admin_notifications(
        clinic_id=effective_clinic_id,
        admin_id=user.user_id,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )
    return {"success": True, "notifications": notifications}


@router.get("/notifications/unread-count")
async def get_admin_notifications_unread_count(
    clinic_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Get the live count of unread notifications for the header bell badge."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id) if clinic_id else await resolve_clinic_id_for_write(user)
    count = await broadcast_service.get_unread_count(
        clinic_id=effective_clinic_id,
        admin_id=user.user_id,
    )
    return {"success": True, "unread_count": count}


@router.patch("/notifications/{notification_id}/read")
async def mark_admin_notification_read(
    notification_id: str,
    clinic_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Mark a specific notification as read within the authenticated admin's tenant scope."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id) if clinic_id else await resolve_clinic_id_for_write(user)
    success = await broadcast_service.mark_notification_read(
        notification_id=notification_id,
        clinic_id=effective_clinic_id,
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
    clinic_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Mark all unread notifications as read for the authenticated admin's clinic."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id) if clinic_id else await resolve_clinic_id_for_write(user)
    updated_count = await broadcast_service.mark_all_notifications_read(
        clinic_id=effective_clinic_id,
        admin_id=user.user_id,
    )
    return {
        "success": True,
        "message": f"Marked {updated_count} notifications as read",
        "updated_count": updated_count,
    }


# ═══════ SUBSCRIPTION & DAILY LIMITS (CUSTOMER-SAFE) ═══════
# Same hard boundary as /admin/messaging-usage: volumetric and lifecycle only.
# No cost, price, rate, markup or Meta economics field may ever appear here.


@router.get("/subscription")
async def get_subscription_status(
    clinic_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Subscription banner state + today report dispatch budget for this clinic.

    Backs the grace-period banner and the 80% / 100% daily-limit badges in the
    clinic admin panel. Read-only: a clinic admin can see their own lifecycle
    but only the platform owner can change it.
    """
    from app.services.subscription import get_clinic_status

    try:
        effective_clinic_id = enforce_clinic_access(user, clinic_id)
        clinic = await get_clinic_by_id(effective_clinic_id)
        return {"success": True, "clinic_id": effective_clinic_id, **await get_clinic_status(clinic)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching subscription status: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch subscription status")
