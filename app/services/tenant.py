"""Tenant resolution module for multi-tenant clinic isolation."""

import logging
from typing import Optional

from app.database import supabase
from app.config import settings

logger = logging.getLogger(__name__)

# In-memory cache: {whatsapp_number: clinic_dict}
# Avoids a DB hit on every single message.
# Cache is cleared on clinic update via /admin.
_tenant_cache: dict[str, dict] = {}


class TenantNotFound(Exception):
    """Raised when no clinic matches the incoming WhatsApp number."""
    pass


class FeatureNotAvailable(Exception):
    """Raised when a clinic's plan does not include the requested feature."""
    pass


async def resolve_tenant(display_phone_number: str) -> Optional[dict]:
    """
    Resolve clinic from the receiving WhatsApp number.
    display_phone_number comes from Meta payload metadata.
    Format: "+919876543210" (E.164, with + prefix)

    For single-tenant mode (no clinics table), returns a
    synthetic clinic dict from environment variables.
    """
    # Normalize: Meta sometimes sends without +
    phone = display_phone_number if display_phone_number.startswith("+") \
            else f"+{display_phone_number}"

    # Check cache first
    if phone in _tenant_cache:
        clinic = _tenant_cache[phone]
        if clinic.get("is_active", True):
            return clinic
        else:
            raise TenantNotFound(f"Clinic for {phone} is inactive.")

    # Try DB lookup
    try:
        result = supabase.table("clinics") \
            .select("*") \
            .eq("whatsapp_number", phone) \
            .eq("is_active", True) \
            .execute()

        if result.data:
            clinic = result.data[0]
            _tenant_cache[phone] = clinic
            return clinic

    except Exception as e:
        logger.warning(f"Clinics table lookup failed (may not exist yet): {e}")

    # Fallback: single-tenant mode
    # Fetch the first clinic in the database as the fallback
    try:
        fallback = supabase.table("clinics").select("*").order("created_at").limit(1).execute()
        if fallback.data:
            clinic = fallback.data[0]
            _tenant_cache[phone] = clinic
            return clinic
    except Exception as e:
        logger.warning(f"Fallback clinic lookup failed: {e}")

    # Absolute fallback using env vars (will fail if DB expects UUID, but safe if table doesn't exist)
    clinic = _build_fallback_clinic()
    _tenant_cache[phone] = clinic
    return clinic


def _build_fallback_clinic() -> dict:
    """Build a synthetic clinic dict from environment variables for backward compat."""
    return {
        "id": "default",
        "name": settings.hospital_name,
        "whatsapp_number": settings.hospital_phone,
        "plan": "pro",
        "is_active": True,
        "config": {
            "meta_phone_number_id": settings.whatsapp_phone_number_id,
            "meta_access_token": settings.whatsapp_token,
            "clinic_name": settings.hospital_name,
            "language": "en",
            "timezone": "Asia/Kolkata",
        }
    }


async def get_clinic_by_id(clinic_id: str) -> dict:
    """Get clinic by its UUID."""
    if clinic_id == "default":
        try:
            fallback = supabase.table("clinics").select("*").order("created_at").limit(1).execute()
            if fallback.data:
                return fallback.data[0]
        except Exception:
            pass
        return _build_fallback_clinic()

    result = supabase.table("clinics") \
        .select("*") \
        .eq("id", clinic_id) \
        .execute()

    if not result.data:
        raise TenantNotFound(f"Clinic {clinic_id} not found")
    return result.data[0]


def invalidate_tenant_cache(whatsapp_number: str = None):
    """Call after /admin clinic update to clear stale cache."""
    if whatsapp_number:
        _tenant_cache.pop(whatsapp_number, None)
    else:
        _tenant_cache.clear()


# ─── Plan Feature Registry ───────────────────────────────────────────────────

PLAN_FEATURES: dict[str, set[str]] = {
    "basic": {
        "booking",
        "reminders_basic",   # 24hr + 2hr reminders
        "multilingual",
        "emergency_escalation",
        "admin_dashboard",
    },
    "pro": {
        "booking",
        "reminders_basic",
        "reminders_post_visit",  # post-visit feedback trigger
        "multilingual",
        "emergency_escalation",
        "admin_dashboard",
        "lab_reports",
        "feedback",
        "analytics",
        "multi_department",
    },
    "enterprise": {
        # Sentinel — checked first, bypasses set lookup entirely
        "*"
    },
}


def has_feature(clinic: dict, feature: str) -> bool:
    """
    Check whether a clinic's plan includes a given feature.

    Resolution order:
      1. Enterprise plan → always True (wildcard)
      2. Per-clinic JSONB override in clinic["features"] → explicit True/False
      3. Plan-level feature set → membership check
    """
    plan: str = clinic.get("plan", "basic")

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
    return feature in allowed


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
                   f"Please upgrade to access this functionality."
        )
