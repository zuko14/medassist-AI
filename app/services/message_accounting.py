"""Centralized Message Accounting Service for Kriya AI.

Single responsibility: log outbound WhatsApp messages and query usage.

SECURITY INVARIANTS:
  1. ALL monetary calculations use integer paise — NEVER floats.
  2. Meta pricing rates are read from the database (meta_pricing_config),
     NEVER hardcoded in Python.
  3. Customer-facing methods (get_clinic_usage) NEVER return cost/pricing fields.
  4. Platform-owner methods (get_platform_usage) return full financial breakdown.
  5. Logging is fire-and-forget — a failed INSERT never blocks message delivery.
  6. Billing period is CALENDAR MONTH (1st–last day).

ARCHITECTURE:
  This service is the ONLY code path that writes to outbound_message_ledger
  and the ONLY code path that reads meta_pricing_config. No router or
  template should ever query these tables directly.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Category Resolution ──────────────────────────────────────────────────────
# Template names follow Meta convention: category prefix or known mapping.
# Freeform (text/interactive/document/location) messages default to 'utility'.

_MARKETING_TEMPLATE_PREFIXES = (
    "followup_",
    "promo_",
    "marketing_",
    "campaign_",
    "re_engage_",
)

_AUTHENTICATION_TEMPLATE_PREFIXES = (
    "otp_",
    "auth_",
    "verify_",
)


def resolve_category(message_type: str, template_name: Optional[str] = None) -> str:
    """Determine the Meta billing category for a message.

    Categories (per Meta Cloud API pricing):
      - utility:        Transactional messages (appointment confirmations, reminders, reports)
      - marketing:      Promotional messages (follow-ups, campaigns, re-engagement)
      - authentication: OTP / verification messages
      - service:        Messages within the 24-hour customer-service window (free tier)

    For template messages, category is inferred from the template name prefix.
    For freeform messages, the default is 'utility' since Kriya AI's bot flows
    are overwhelmingly transactional (booking, reminders, lab reports).
    """
    if message_type == "mark_read":
        return "service"  # mark_read is not billable, but categorized for completeness

    if template_name:
        name_lower = template_name.lower()
        if name_lower.startswith(_MARKETING_TEMPLATE_PREFIXES):
            return "marketing"
        if name_lower.startswith(_AUTHENTICATION_TEMPLATE_PREFIXES):
            return "authentication"
        # All other templates are utility (appointment_confirmation, reminder_24h, etc.)
        return "utility"

    # Freeform messages (text, interactive, document, location) = utility
    return "utility"


# ── In-memory pricing cache ─────────────────────────────────────────────────
# Meta pricing changes infrequently (quarterly at most). Cache with 5-min TTL
# to avoid a DB query on every message send.

import time
from app.database import sb  # T5.1: off-loop query execution

_pricing_cache: Optional[dict] = None
_pricing_cache_at: float = 0.0
_PRICING_CACHE_TTL = 300  # 5 minutes


async def _get_pricing() -> dict:
    """Read Meta pricing rates from database with in-memory caching.

    Returns dict with keys: utility_paise, marketing_paise,
    authentication_paise, service_paise.
    """
    global _pricing_cache, _pricing_cache_at

    if _pricing_cache and (time.time() - _pricing_cache_at) < _PRICING_CACHE_TTL:
        return _pricing_cache

    try:
        from app.database import supabase

        result = (
            await sb(supabase.table("meta_pricing_config")
            .select("utility_paise, marketing_paise, authentication_paise, service_paise")
            .eq("id", "default"))
        )
        if result.data:
            _pricing_cache = result.data[0]
            _pricing_cache_at = time.time()
            return _pricing_cache
    except Exception as e:
        logger.warning(f"Failed to read meta_pricing_config, using fallback: {e}")

    # Fallback — same as migration seed values
    fallback = {
        "utility_paise": 12,
        "marketing_paise": 75,
        "authentication_paise": 10,
        "service_paise": 0,
    }
    _pricing_cache = fallback
    _pricing_cache_at = time.time()
    return fallback


def invalidate_pricing_cache() -> None:
    """Call after owner updates meta_pricing_config via PUT /platform/pricing."""
    global _pricing_cache, _pricing_cache_at
    _pricing_cache = None
    _pricing_cache_at = 0.0


# ── Plan Tier Cache ──────────────────────────────────────────────────────────

_plan_tiers_cache: Optional[dict] = None
_plan_tiers_cache_at: float = 0.0
_PLAN_TIERS_CACHE_TTL = 300  # 5 minutes


async def _get_plan_tiers() -> dict:
    """Read plan tier quotas from database with in-memory caching.

    Returns dict keyed by plan_name: {
        "soloclinic": {"included_messages_month": 500, "display_name": "Solo Clinic", ...},
        ...
    }
    """
    global _plan_tiers_cache, _plan_tiers_cache_at

    if _plan_tiers_cache and (time.time() - _plan_tiers_cache_at) < _PLAN_TIERS_CACHE_TTL:
        return _plan_tiers_cache

    try:
        from app.database import supabase

        result = (
            await sb(supabase.table("plan_tiers")
            .select("plan_name, display_name, monthly_price_paise, included_messages_month, overage_price_paise")
            .eq("is_active", True))
        )
        if result.data:
            _plan_tiers_cache = {row["plan_name"]: row for row in result.data}
            _plan_tiers_cache_at = time.time()
            return _plan_tiers_cache
    except Exception as e:
        logger.warning(f"Failed to read plan_tiers, using fallback: {e}")

    # Fallback — same as migration seed values
    fallback = {
        "soloclinic": {"included_messages_month": 500, "display_name": "Solo Clinic"},
        "diagstream": {"included_messages_month": 1000, "display_name": "DiagStream"},
        "essential": {"included_messages_month": 2500, "display_name": "Essential"},
        "polyclinic": {"included_messages_month": 5000, "display_name": "PolyClinic"},
        "enterprise": {"included_messages_month": 0, "display_name": "Enterprise"},
    }
    _plan_tiers_cache = fallback
    _plan_tiers_cache_at = time.time()
    return fallback


def invalidate_plan_tiers_cache() -> None:
    """Call after owner updates plan_tiers via PUT /platform/plan-tiers."""
    global _plan_tiers_cache, _plan_tiers_cache_at
    _plan_tiers_cache = None
    _plan_tiers_cache_at = 0.0


# ── Outbound Message Logging ────────────────────────────────────────────────


async def log_outbound(
    clinic_id: str,
    recipient_phone: str,
    message_type: str,
    source_service: str,
    send_success: bool = True,
    meta_message_id: Optional[str] = None,
    template_name: Optional[str] = None,
) -> None:
    """Log an outbound message to the ledger. Fire-and-forget safe.

    This is called from WhatsAppService._make_request() after every
    Meta API call. It MUST NOT raise — a logging failure should never
    prevent message delivery in a healthcare system.

    Args:
        clinic_id: UUID of the sending clinic.
        recipient_phone: Patient phone number (E.164).
        message_type: One of text/template/interactive_buttons/etc.
        source_service: Which service originated the send.
        send_success: Whether the Meta API call succeeded.
        meta_message_id: wamid from Meta response (None on failure).
        template_name: Template name if message_type == 'template'.
    """
    try:
        from app.database import supabase

        category = resolve_category(message_type, template_name)

        await sb(supabase.table("outbound_message_ledger").insert({
            "clinic_id": clinic_id,
            "recipient_phone": recipient_phone,
            "message_type": message_type,
            "template_name": template_name,
            "category": category,
            "direction": "outbound",
            "send_success": send_success,
            "source_service": source_service,
            "meta_message_id": meta_message_id,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }))

        logger.debug(
            f"Ledger: logged {message_type} ({category}) to {recipient_phone[:6]}*** "
            f"for clinic {clinic_id[:8]}... success={send_success}"
        )
    except Exception as e:
        # NEVER raise — fire-and-forget safety for healthcare
        logger.error(f"Ledger write failed (non-fatal): {e}")


# ── Usage Queries ────────────────────────────────────────────────────────────


def _billing_period(reference: Optional[datetime] = None) -> tuple[str, str]:
    """Return (start, end) ISO strings for the current calendar month.

    Calendar month billing: 1st 00:00:00 UTC to last day 23:59:59 UTC.
    """
    now = reference or datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    # End of month: start of next month
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)

    return start.isoformat(), end.isoformat()


async def get_clinic_usage(clinic_id: str, plan_name: str) -> dict:
    """Get message usage for a clinic's current billing period.

    CUSTOMER-SAFE: Returns ONLY volumetric usage — no costs, no pricing,
    no Meta rates, no markup, no financial fields whatsoever.

    Args:
        clinic_id: UUID of the clinic.
        plan_name: The clinic's plan tier name (e.g. 'essential').

    Returns:
        {
            "plan": "essential",
            "plan_display_name": "Essential",
            "billing_period": {"start": "2026-08-01T...", "end": "2026-09-01T..."},
            "included_messages": 2500,
            "is_unlimited": False,
            "messages_sent": 1847,
            "messages_remaining": 653,
            "overage_count": 0,
            "usage_percentage": 73.88,
            "daily_breakdown": [{"date": "2026-08-01", "count": 42}, ...],
            "by_category": {"utility": 1600, "marketing": 247}
        }
    """
    from app.database import supabase

    period_start, period_end = _billing_period()
    plan_tiers = await _get_plan_tiers()
    tier = plan_tiers.get(plan_name, {})
    included = tier.get("included_messages_month", 500)
    is_unlimited = included == 0  # enterprise

    try:
        # Query ledger for this clinic in current billing period
        # Exclude mark_read from billable counts
        result = (
            await sb(supabase.table("outbound_message_ledger")
            .select("category, sent_at, send_success")
            .eq("clinic_id", clinic_id)
            .eq("send_success", True)
            .neq("message_type", "mark_read")
            .gte("sent_at", period_start)
            .lt("sent_at", period_end))
        )
        rows = result.data or []
    except Exception as e:
        logger.error(f"Failed to query usage for clinic {clinic_id}: {e}")
        rows = []

    total_sent = len(rows)

    # Category breakdown
    by_category: dict[str, int] = {}
    daily_counts: dict[str, int] = {}
    for row in rows:
        cat = row.get("category", "utility")
        by_category[cat] = by_category.get(cat, 0) + 1

        day = row.get("sent_at", "")[:10]
        if day:
            daily_counts[day] = daily_counts.get(day, 0) + 1

    if is_unlimited:
        remaining = None  # unlimited
        overage = 0
        usage_pct = 0.0
    else:
        remaining = max(0, included - total_sent)
        overage = max(0, total_sent - included)
        usage_pct = round((total_sent / included) * 100, 2) if included > 0 else 0.0

    daily_breakdown = sorted(
        [{"date": d, "count": c} for d, c in daily_counts.items()],
        key=lambda x: x["date"],
    )

    return {
        "plan": plan_name,
        "plan_display_name": tier.get("display_name", plan_name),
        "period_start": period_start[:10],
        "period_end": period_end[:10],
        "included_messages": included,
        "is_unlimited": is_unlimited,
        "messages_sent": total_sent,
        "messages_remaining": remaining,
        "overage_count": overage,
        "usage_percent": usage_pct,
        "daily_breakdown": daily_breakdown,
        "by_category": by_category,
    }


async def get_platform_usage(days: int = 30) -> dict:
    """Get platform-wide messaging usage with full financial breakdown.

    OWNER-ONLY: Returns Meta cost estimates, pricing rates, per-clinic
    financial detail. NEVER call this from a clinic-facing API.

    Args:
        days: Number of days to look back. Default 30.

    Returns full financial breakdown including estimated_meta_cost_inr.
    """
    from app.database import supabase
    from datetime import timedelta

    start_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    pricing = await _get_pricing()
    plan_tiers = await _get_plan_tiers()

    # Fetch all clinics
    try:
        clinics_res = (
            await sb(supabase.table("clinics")
            .select("id, name, plan, is_active"))
        )
        clinics = clinics_res.data or []
    except Exception as e:
        logger.error(f"Failed to fetch clinics for platform usage: {e}")
        return {"success": False, "error": str(e)}

    # Fetch all ledger entries in period (exclude mark_read from billing)
    try:
        ledger_res = (
            await sb(supabase.table("outbound_message_ledger")
            .select("clinic_id, category, send_success, message_type")
            .eq("send_success", True)
            .neq("message_type", "mark_read")
            .gte("sent_at", start_date))
        )
        ledger_rows = ledger_res.data or []
    except Exception as e:
        logger.error(f"Failed to fetch ledger for platform usage: {e}")
        ledger_rows = []

    # Aggregate by clinic and category
    usage_by_clinic: dict[str, dict[str, int]] = {}
    for row in ledger_rows:
        cid = row.get("clinic_id")
        if not cid:
            continue
        bucket = usage_by_clinic.setdefault(cid, {
            "utility": 0, "marketing": 0, "authentication": 0, "service": 0,
        })
        cat = row.get("category", "utility")
        bucket[cat] = bucket.get(cat, 0) + 1

    # Build per-clinic breakdown with costs
    clinics_table = []
    total_utility = 0
    total_marketing = 0
    total_authentication = 0
    total_service = 0

    for c in clinics:
        cid = c["id"]
        u = usage_by_clinic.get(cid, {
            "utility": 0, "marketing": 0, "authentication": 0, "service": 0,
        })
        util_count = u.get("utility", 0)
        mkt_count = u.get("marketing", 0)
        auth_count = u.get("authentication", 0)
        svc_count = u.get("service", 0)

        total_utility += util_count
        total_marketing += mkt_count
        total_authentication += auth_count
        total_service += svc_count

        # Cost calculation — integer paise arithmetic, then convert to INR
        cost_paise = (
            util_count * pricing.get("utility_paise", 12)
            + mkt_count * pricing.get("marketing_paise", 75)
            + auth_count * pricing.get("authentication_paise", 10)
            + svc_count * pricing.get("service_paise", 0)
        )

        plan = c.get("plan", "soloclinic")
        tier = plan_tiers.get(plan, {})
        included = tier.get("included_messages_month", 500)
        outbound_total = util_count + mkt_count + auth_count + svc_count

        clinics_table.append({
            "clinic_id": cid,
            "clinic_name": c.get("name"),
            "plan": plan,
            "is_active": c.get("is_active", True),
            "included_messages": included,
            "outbound_total": outbound_total,
            "utility_count": util_count,
            "marketing_count": mkt_count,
            "authentication_count": auth_count,
            "service_count": svc_count,
            "overage_count": max(0, outbound_total - included) if included > 0 else 0,
            "estimated_cost_inr": round(cost_paise / 100, 2),
        })

    clinics_table.sort(key=lambda x: x["outbound_total"], reverse=True)

    total_outbound = total_utility + total_marketing + total_authentication + total_service
    total_cost_paise = (
        total_utility * pricing.get("utility_paise", 12)
        + total_marketing * pricing.get("marketing_paise", 75)
        + total_authentication * pricing.get("authentication_paise", 10)
        + total_service * pricing.get("service_paise", 0)
    )

    return {
        "success": True,
        "period_days": days,
        "total_outbound": total_outbound,
        "total_utility": total_utility,
        "total_marketing": total_marketing,
        "total_authentication": total_authentication,
        "total_service": total_service,
        "total_estimated_cost_inr": round(total_cost_paise / 100, 2),
        "pricing": {
            "source": "database",
            "utility_inr": round(pricing.get("utility_paise", 12) / 100, 2),
            "marketing_inr": round(pricing.get("marketing_paise", 75) / 100, 2),
            "authentication_inr": round(pricing.get("authentication_paise", 10) / 100, 2),
            "service_inr": round(pricing.get("service_paise", 0) / 100, 2),
        },
        "clinics": clinics_table,
    }
