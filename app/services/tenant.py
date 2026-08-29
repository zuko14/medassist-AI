"""Tenant resolution module for multi-tenant clinic isolation."""

import logging
from typing import Optional

from app.database import supabase
from app.config import settings

import time

logger = logging.getLogger(__name__)


# KA-19: DOCUMENTED PROPAGATION WINDOW
# The tenant cache is process-local with a 30s TTL. With 4 production processes
# (2 instances × 2 workers), invalidate_tenant_cache() only clears the calling
# process's copy. After an admin suspends a clinic, the other 3 workers continue
# serving it for up to 30 seconds. This is a bounded, documented trade-off:
# - Lowering TTL to 0 would cause a DB query on every inbound message (unacceptable load)
# - A shared cache (Redis/Memcached) adds infrastructure complexity disproportionate to risk
# - For DPDP deletion requests: coordinate via a separate flag checked pre-message-process
CACHE_TTL_SECONDS = 30  # 30s — max propagation delay for clinic state changes

# In-memory caches with TTL support
_tenant_cache: dict[str, dict] = {}
_branch_cache: dict[str, list[dict]] = {}


def _get_cached_item(cache: dict, key: str) -> Optional[any]:
    """Retrieve item from cache if present and not expired."""
    entry = cache.get(key)
    if entry is None:
        return None
    if isinstance(entry, dict) and "cached_at" in entry and "data" in entry:
        if time.time() - entry["cached_at"] < CACHE_TTL_SECONDS:
            return entry["data"]
        else:
            cache.pop(key, None)
            return None
    # Backward compatibility for direct un-wrapped values
    return entry


def _set_cached_item(cache: dict, key: str, data: any) -> None:
    """Store item in cache with current timestamp."""
    cache[key] = {
        "data": data,
        "cached_at": time.time(),
    }


class TenantNotFound(Exception):
    """Raised when no clinic matches the incoming WhatsApp number."""


class FeatureNotAvailable(Exception):
    """Raised when a clinic's plan does not include the requested feature."""


def _normalize_e164(raw: str) -> str:
    """Normalize a phone number to E.164 format for consistent DB lookups.

    Strips spaces, dashes, parentheses and ensures a leading '+'.
    Meta webhook payloads inconsistently include/omit the '+' prefix.
    """
    import re
    digits = re.sub(r"[^0-9+]", "", raw.strip())
    if not digits.startswith("+"):
        digits = f"+{digits}"
    return digits


async def resolve_tenant(
    display_phone_number: str,
    phone_number_id: str = None,
) -> Optional[dict]:
    """
    Resolve clinic from the receiving WhatsApp number.

    Resolution order:
      1. phone_number_id lookup (immutable Meta ID — preferred)
      2. Normalized display_phone_number lookup against whatsapp_number
      3. Sandbox fallback (test/demo numbers → is_sandbox=True clinic)
      4. Single-tenant fallback (only if exactly 1 active clinic)
      5. Zero-clinic env-var fallback (initial setup bootstrap)

    For single-tenant mode (no clinics table), returns a
    synthetic clinic dict from environment variables.
    """
    phone = _normalize_e164(display_phone_number)

    # ── Cache check (keyed on phone_number_id if available, else phone) ──
    cache_key = phone_number_id or phone
    cached_clinic = _get_cached_item(_tenant_cache, cache_key)
    if cached_clinic is not None:
        if cached_clinic.get("is_active", True) and cached_clinic.get("status") != "DELETED" and not cached_clinic.get("deleted_at"):
            return cached_clinic
        else:
            raise TenantNotFound(f"Clinic for {cache_key} is inactive or deleted.")

    # ── Strategy 1: Lookup by phone_number_id (immutable Meta ID) ──
    db_failed = False
    db_error = None

    if phone_number_id:
        try:
            result = (
                supabase.table("clinics")
                .select("*")
                .eq("phone_number_id", phone_number_id)
                .eq("is_active", True)
                .execute()
            )
            if result.data:
                clinic = result.data[0]
                if clinic.get("status") == "DELETED" or clinic.get("deleted_at") is not None:
                    raise TenantNotFound(f"Clinic for phone_number_id={phone_number_id} has been deleted.")
                _set_cached_item(_tenant_cache, phone_number_id, clinic)
                _set_cached_item(_tenant_cache, phone, clinic)  # Dual-cache
                return clinic
        except TenantNotFound:
            raise
        except Exception as e:
            db_failed = True
            db_error = e
            logger.error(f"Clinics phone_number_id lookup failed for {phone_number_id}: {e}")

    # ── Strategy 2: Lookup by normalized display phone number ──
    if not db_failed:
        try:
            result = (
                supabase.table("clinics")
                .select("*")
                .eq("whatsapp_number", phone)
                .eq("is_active", True)
                .execute()
            )

            if result.data:
                clinic = result.data[0]
                if clinic.get("status") == "DELETED" or clinic.get("deleted_at") is not None:
                    raise TenantNotFound(f"Clinic for {phone} has been deleted.")
                _set_cached_item(_tenant_cache, phone, clinic)
                return clinic

        except TenantNotFound:
            raise
        except Exception as e:
            db_failed = True
            db_error = e
            logger.error(f"Clinics table lookup encountered database error for {phone}: {e}")

    # If DB query failed with an exception, DO NOT silently fall back to default tenant!
    if db_failed:
        raise RuntimeError(f"Database error during tenant resolution for {phone}: {db_error}") from db_error

    # ── Strategy 3: Sandbox fallback for test/demo numbers (DEV/STAGING ONLY) ──
    # Sandbox fallback: dev/staging convenience ONLY. In production this
    # routed the patients of a misconfigured tenant — names, symptoms,
    # bookings — into the sandbox clinic where sandbox admins could read
    # them (KRIYA-012 / T2.1).
    if settings.app_env != "production":
        try:
            sandbox_res = (
                supabase.table("clinics")
                .select("*")
                .eq("is_sandbox", True)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
            if sandbox_res.data:
                clinic = sandbox_res.data[0]
                logger.info(
                    f"Routed unrecognized phone {phone} to sandbox clinic "
                    f"'{clinic.get('name')}' (id={clinic.get('id')})"
                )
                _set_cached_item(_tenant_cache, cache_key, clinic)
                return clinic
        except Exception as e:
            logger.warning(f"Sandbox clinic lookup failed: {e}")

    # ── Strategy 4: Single-tenant fallback ──
    # If the database contains more than 1 active clinic, routing an unknown
    # phone number to an arbitrary clinic is an active cross-tenant security hazard (C1).
    try:
        active_clinics_res = (
            supabase.table("clinics")
            .select("*")
            .eq("is_active", True)
            .neq("status", "DELETED")
            .limit(2)
            .execute()
        )
        active_clinics = active_clinics_res.data or []
        if len(active_clinics) > 1:
            logger.error(
                f"TENANT_NOT_FOUND phone={phone}: multi-tenant deployment has {len(active_clinics)}+ active clinics. "
                "Refusing to guess tenant."
            )
            raise TenantNotFound(
                f"No clinic registered for WhatsApp number {phone} in multi-tenant environment."
            )

        if len(active_clinics) == 1:
            clinic = active_clinics[0]
            clinic_phone = clinic.get("whatsapp_number") or phone
            _set_cached_item(_tenant_cache, clinic_phone, clinic)
            return clinic
    except TenantNotFound:
        raise
    except Exception as e:
        logger.warning(f"Fallback clinic count lookup failed: {e}")

    # ── Strategy 5: Zero-clinic env-var fallback (initial setup) ──
    clinic = _build_fallback_clinic()
    _set_cached_item(_tenant_cache, clinic.get("whatsapp_number", phone), clinic)
    return clinic




def _build_fallback_clinic() -> dict:
    """Build a synthetic clinic dict from environment variables for backward compat."""
    return {
        "id": "default",
        "name": settings.hospital_name,
        "whatsapp_number": settings.hospital_phone,
        "plan": "enterprise",
        "is_active": True,
        "config": {
            "meta_phone_number_id": settings.whatsapp_phone_number_id,
            "meta_access_token": settings.whatsapp_token,
            "clinic_name": settings.hospital_name,
            "language": "en",
            "timezone": "Asia/Kolkata",
        },
    }


async def get_clinic_by_id(clinic_id: Optional[str]) -> dict:
    """Get clinic by its UUID or fallback to the primary active clinic."""
    if not clinic_id or str(clinic_id).strip().lower() in ("default", "none", "null", ""):
        try:
            fallback = (
                supabase.table("clinics")
                .select("*")
                .eq("is_active", True)
                .neq("status", "DELETED")
                .order("created_at")
                .limit(1)
                .execute()
            )
            if fallback.data:
                return fallback.data[0]
        except Exception as e:
            logger.warning(f"Fallback clinic lookup failed: {e}")
        return _build_fallback_clinic()

    try:
        result = supabase.table("clinics").select("*").eq("id", str(clinic_id).strip()).execute()

        if not result.data:
            raise TenantNotFound(f"Clinic {clinic_id} not found")
        clinic = result.data[0]
        if clinic.get("status") == "DELETED" or clinic.get("deleted_at") is not None:
            raise TenantNotFound(f"Clinic {clinic_id} has been deleted")
        return clinic
    except TenantNotFound:
        raise
    except Exception as e:
        logger.warning(f"Error looking up clinic {clinic_id} by ID: {e}")
        raise TenantNotFound(f"Clinic {clinic_id} lookup error: {e}") from e


def get_clinic_contact(clinic: dict, key: str, fallback: str) -> str:
    """Read a per-clinic contact/location value (phone, address, maps_link,
    emergency_number) from the clinic's config JSONB, falling back to the
    platform-wide default when the clinic hasn't configured its own."""
    return (clinic.get("config") or {}).get(key) or fallback


def invalidate_tenant_cache(whatsapp_number: str = None, phone_number_id: str = None):
    """Call after /admin clinic update to clear stale cache for both phone and phone_number_id."""
    if whatsapp_number or phone_number_id:
        if whatsapp_number:
            _tenant_cache.pop(whatsapp_number, None)
            try:
                norm_phone = _normalize_e164(whatsapp_number)
                _tenant_cache.pop(norm_phone, None)
            except Exception:
                pass
        if phone_number_id:
            _tenant_cache.pop(phone_number_id, None)

        # Purge any cache entries pointing to the same clinic
        for k in list(_tenant_cache.keys()):
            entry = _tenant_cache[k]
            clinic_data = entry.get("data") if isinstance(entry, dict) else entry
            if isinstance(clinic_data, dict):
                c_phone = clinic_data.get("whatsapp_number")
                c_pid = clinic_data.get("phone_number_id")
                if (whatsapp_number and (c_phone == whatsapp_number or c_pid == whatsapp_number)) or \
                   (phone_number_id and (c_pid == phone_number_id or c_phone == phone_number_id)):
                    _tenant_cache.pop(k, None)
    else:
        _tenant_cache.clear()


# ─── Plan Feature Registry ───────────────────────────────────────────────────
#
# Plans:
#   soloclinic  — Solo doctor / small clinic (booking + payments only)
#   diagstream  — Diagnostics / lab-only centres (lab reports + lab-test
#                 booking; no *doctor* booking)
#   essential   — Full-service hospital (everything except enterprise wildcard)
#   polyclinic  — Multi-branch hospital / polyclinic + diagnostics (essential + multi_branch)
#   enterprise  — Unlimited (all current + future features via wildcard)

PLAN_FEATURES: dict[str, set[str]] = {
    "soloclinic": {
        "booking",
        "reminders",
        "multilingual",
        "emergency_escalation",
        "clinical_firewall",
        "admin_dashboard",
        "roster_management",
        "compliance_dpdp",
        "compliance_nmc",
        "payments_razorpay",  # WhatsApp-native checkout for solo doctors
    },
    "diagstream": {
        "multilingual",
        "emergency_escalation",
        "clinical_firewall",
        "compliance_dpdp",
        "compliance_nmc",
        "lab_reports",
        "diagnostic_reports",
        "ai_report_summary",
        "pii_sanitization",
        "multi_branch",  # Diagnostic centers can also run multiple branches
        "lab_test_booking",
    },
    "essential": {
        "booking",
        "reminders",
        "multilingual",
        "emergency_escalation",
        "clinical_firewall",
        "admin_dashboard",
        "roster_management",
        "compliance_dpdp",
        "compliance_nmc",
        "lab_reports",
        "ai_report_summary",
        "pii_sanitization",
        "feedback",
        "analytics",
        "multi_department",
        "payments_razorpay",
        "staff_training",
    },
    "polyclinic": {
        "booking",
        "reminders",
        "multilingual",
        "emergency_escalation",
        "clinical_firewall",
        "admin_dashboard",
        "roster_management",
        "compliance_dpdp",
        "compliance_nmc",
        "lab_reports",
        "diagnostic_reports",
        "ai_report_summary",
        "pii_sanitization",
        "feedback",
        "analytics",
        "multi_department",
        "payments_razorpay",
        "staff_training",
        "multi_branch",  # Multi-branch support
        "lab_test_booking",
    },
    "enterprise": {
        # Sentinel — checked first, bypasses set lookup entirely
        "*"
    },
}

# Flat, sorted list of every named feature across all plans — excludes the
# "*" enterprise wildcard sentinel. Used by GET /admin/me (app/routers/admin.py)
# to tell the admin panel frontend which tabs to show, without duplicating
# this registry in JS.
ALL_FEATURES: list[str] = sorted(
    {feature for features in PLAN_FEATURES.values() for feature in features if feature != "*"}
)

# Human-readable label for every feature in ALL_FEATURES. Lives here — next to
# PLAN_FEATURES — so the plan registry and its display names can never drift
# apart. Consumed by GET /platform/plan-tiers for the owner dashboard's plan
# feature-matrix widget. Any new feature added to PLAN_FEATURES must get a
# label here; test_plan_feature_labels_cover_all_features guards that.
FEATURE_LABELS: dict[str, str] = {
    "admin_dashboard": "Admin Dashboard",
    "ai_report_summary": "AI Report Summaries",
    "analytics": "Analytics & Insights",
    "booking": "Appointment Booking",
    "clinical_firewall": "Clinical Safety Firewall",
    "compliance_dpdp": "DPDP Consent & Compliance",
    "compliance_nmc": "NMC Telemedicine Compliance",
    "diagnostic_reports": "Diagnostic Report Delivery",
    "emergency_escalation": "Emergency Escalation",
    "feedback": "Patient Feedback Collection",
    "lab_reports": "Lab Report Delivery",
    "lab_test_booking": "Lab Test Booking",
    "multi_branch": "Multi-Branch Support",
    "multi_department": "Multi-Department Routing",
    "multilingual": "Multilingual Replies",
    "payments_razorpay": "Razorpay Payments",
    "pii_sanitization": "PII Sanitization",
    "reminders": "Automated Reminders",
    "roster_management": "Doctor Roster & Leave",
    "staff_training": "Staff Training & Onboarding",
}


def has_feature(clinic: dict, feature: str) -> bool:
    """
    Check whether a clinic's plan includes a given feature.

    Resolution order:
      1. Enterprise plan → always True (wildcard)
      2. Per-clinic JSONB override in clinic["features"] → explicit True/False
      3. Plan-level feature set → membership check
    """
    plan: str = clinic.get("plan", "soloclinic")

    # 1. Enterprise wildcard
    if plan == "enterprise":
        return True

    # 2. Per-clinic JSONB overrides (allows upselling single features to basic clients)
    overrides: dict = clinic.get("features") or {}
    if feature in overrides:
        override_val = overrides[feature]
        if not isinstance(override_val, bool):
            # Defensive — log and fall through to plan check
            import logging

            logging.getLogger(__name__).warning(
                f"Non-bool override for feature '{feature}' on clinic {clinic.get('id')} — ignoring"
            )
        else:
            return override_val

    # 3. Plan-level membership
    allowed: set = PLAN_FEATURES.get(plan, set())
    if feature in allowed:
        return True
    if feature.startswith("reminders") and "reminders" in allowed:
        return True
    return False


def require_feature(clinic: dict, feature: str) -> None:
    """
    Hard gate — raises HTTP 403 if clinic doesn't have the feature.
    Use in admin API endpoints where a missing feature should surface as an error.
    Use has_feature() in bot flows where you want silent graceful degradation instead.
    """
    from fastapi import HTTPException

    if not has_feature(clinic, feature):
        raise HTTPException(
            status_code=403,
            detail=f"Feature '{feature}' is not available on your current plan. "
            f"Please upgrade to access this functionality.",
        )


# ─── Branch Resolution ───────────────────────────────────────────────────────
# Multi-instance cache semantics:
# In a multi-worker deployment (e.g. 2 uvicorn worker processes or 2 Render instances),
# each process maintains a local in-memory _branch_cache with a 300s TTL.
# When an admin updates a branch via API, invalidate_branch_cache() clears the local worker's
# cache, while other workers will naturally refresh from the database upon TTL expiry or
# inter-process message bus notification. Database is always authoritative.


async def get_clinic_branches(clinic_id: str) -> list[dict]:
    """Get active branches for a clinic, ordered by display_order.

    Returns [] for single-branch / legacy clinics.
    Results are cached in-memory with TTL; call invalidate_branch_cache() on admin update.
    """
    cached_branches = _get_cached_item(_branch_cache, clinic_id)
    if cached_branches is not None:
        return cached_branches

    try:
        result = (
            supabase.table("branches")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("is_active", True)
            .order("display_order")
            .execute()
        )

        branches = result.data or []
        _set_cached_item(_branch_cache, clinic_id, branches)
        return branches

    except Exception as e:
        logger.warning(f"Branch lookup failed for clinic {clinic_id}: {e}")
        _set_cached_item(_branch_cache, clinic_id, [])
        return []


def has_branches(clinic: dict, branches: list) -> bool:
    """
    Returns True if the clinic has the multi_branch feature AND
    actually has 2 or more active branches configured.
    """
    return has_feature(clinic, "multi_branch") and len(branches) >= 2


def invalidate_branch_cache(clinic_id: str = None):
    """Clear branch cache. Call after admin creates/updates/deletes a branch."""
    if clinic_id:
        _branch_cache.pop(clinic_id, None)
    else:
        _branch_cache.clear()


async def get_branch_by_id(branch_id: str) -> Optional[dict]:
    """Get a single branch by its UUID."""
    # Check cache first
    for key in list(_branch_cache.keys()):
        branches = _get_cached_item(_branch_cache, key)
        if isinstance(branches, list):
            for branch in branches:
                if branch.get("id") == branch_id:
                    return branch

    try:
        result = supabase.table("branches").select("*").eq("id", branch_id).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.warning(f"Branch lookup failed for {branch_id}: {e}")
        return None
