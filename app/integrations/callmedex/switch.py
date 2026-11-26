"""Owner master switch for the whole CallMedex ↔ Kriya connection (migration 158, platform_switches 'callmedex').

Fails closed: OFF until the owner turns it on, and OFF whenever the row cannot be read (missing table, DB error
before the first good read). Cached per process for CACHE_TTL_SECONDS so the webhook path stays in-memory; an
owner change applies at once in the process that made it and within the TTL in every other worker.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status

from app.database import sb, supabase

logger = logging.getLogger(__name__)

SWITCH_KEY = "callmedex"
CACHE_TTL_SECONDS = 30.0
_cache: dict = {"on": False, "at": float("-inf"), "row": None}


async def _refresh() -> None:
    try:
        # unscoped: platform_switches is platform-level (no clinic_id); one row by primary key
        res = await sb(supabase.table("platform_switches").select("key, enabled, updated_at, updated_by")
                       .eq("key", SWITCH_KEY).limit(1))
        row = (res.data or [None])[0]
        _cache.update(on=bool(row and row.get("enabled") is True), row=row)
    except Exception as e:  # noqa: BLE001 - keep the last known value; never default to ON
        logger.warning(f"CallMedex switch read failed (keeping {'ON' if _cache['on'] else 'OFF'}): {type(e).__name__}")
    _cache["at"] = time.monotonic()


async def callmedex_enabled() -> bool:
    if time.monotonic() - _cache["at"] > CACHE_TTL_SECONDS:
        await _refresh()
    return _cache["on"]


async def switch_status() -> dict:
    await _refresh()
    row = _cache["row"] or {}
    return {"enabled": _cache["on"], "updated_at": row.get("updated_at"), "updated_by": row.get("updated_by")}


async def set_enabled(enabled: bool, by: Optional[str]) -> dict:
    row = {"enabled": bool(enabled), "updated_by": (by or "owner")[:120],
           "updated_at": datetime.now(timezone.utc).isoformat()}
    # unscoped: platform_switches is platform-level (no clinic_id); one row by primary key
    res = await sb(supabase.table("platform_switches").update(row).eq("key", SWITCH_KEY))
    if not res.data:   # row seeded by migration 158; recreate it if someone removed it
        # unscoped: platform_switches is platform-level (no clinic_id)
        await sb(supabase.table("platform_switches").insert({"key": SWITCH_KEY, **row}))
    return await switch_status()


async def require_callmedex_enabled() -> None:
    """Router dependency: every CallMedex API answers 503 while the owner has the integration switched off."""
    if not await callmedex_enabled():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="The CallMedex integration is switched off by the platform owner.")
