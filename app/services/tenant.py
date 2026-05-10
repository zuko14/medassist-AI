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

    # Fallback: single-tenant mode using env vars
    # This preserves backward compatibility when clinics table doesn't exist
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


# ─── PLAN-BASED FEATURE GATING ──────────────────────────────────────────

PLAN_FEATURES: dict[str, list[str]] = {
    "basic": [
        "appointments",
        "reminders",
    ],
    "pro": [
        "appointments",
        "reminders",
        "lab_reports",
        "prescriptions",
    ],
    "enterprise": [
        "appointments",
        "reminders",
        "lab_reports",
        "prescriptions",
        "khata",
        "analytics",
        "custom_prompt",
        "bulk_blast",
    ],
}

UPGRADE_MESSAGE: dict[str, str] = {
    "lab_reports":   "Lab report delivery requires the Pro plan.",
    "khata":         "KhataBot requires the Enterprise plan.",
    "bulk_blast":    "SchemeBlast requires the Enterprise plan.",
    "analytics":     "Analytics requires the Enterprise plan.",
    "custom_prompt": "Custom AI personality requires the Enterprise plan.",
}


def can_use(clinic: dict, feature: str) -> bool:
    """Check if clinic's plan includes the given feature."""
    plan = clinic.get("plan", "basic")
    return feature in PLAN_FEATURES.get(plan, [])


def require_feature(clinic: dict, feature: str) -> None:
    """
    Call before any feature handler. Raises FeatureNotAvailable
    with a user-friendly message if the plan doesn't allow it.
    """
    if not can_use(clinic, feature):
        msg = UPGRADE_MESSAGE.get(feature, "This feature is not available on your plan.")
        raise FeatureNotAvailable(msg)
