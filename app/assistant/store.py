"""assistant_runs persistence (migration 108): idempotent create, CAS transitions.

Every query is scoped to (clinic_id, username): a run is visible and mutable
only by the user who made it (BOLA -> the router answers 404). State changes are
compare-and-set on the current state, so two parallel confirms or cancels can
never both win (KK-FR-024). Expiry and the stuck-EXECUTING sweep are applied
lazily on reads (architecture.md section 5).
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import sb, supabase

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("NEEDS_CLARIFICATION", "NEEDS_CONFIRMATION", "EXECUTING", "VERIFYING", "UNKNOWN_OUTCOME")
EXPIRING_STATES = ("NEEDS_CLARIFICATION", "NEEDS_CONFIRMATION")
STUCK_AFTER = timedelta(minutes=2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid_or_none(value) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None  # env principals (super_admin_env) have no clinic_admins row


async def get_by_request(clinic_id: str, username: str, client_request_id: str) -> Optional[dict]:
    res = await sb(
        supabase.table("assistant_runs").select("*")
        .eq("clinic_id", clinic_id).eq("username", username)
        .eq("client_request_id", client_request_id).limit(1)
    )
    return res.data[0] if res.data else None


async def create_run(clinic_id: str, user, run: dict, ip_address: str) -> tuple[dict, bool]:
    """Insert a RECEIVED run, or return the existing one for this client_request_id.

    Returns (row, created). A lost insert race returns the winner's row.
    """
    existing = await get_by_request(clinic_id, user.username, run["client_request_id"])
    if existing:
        return existing, False
    row = {
        **run,
        "clinic_id": clinic_id,
        "user_id": _uuid_or_none(getattr(user, "user_id", None)),
        "username": user.username,
        "role": user.role,
        "state": "RECEIVED",
        "ip_address": ip_address,
    }
    try:
        res = await sb(
            # unscoped: insert_scoped_by_payload
            supabase.table("assistant_runs").insert(row)
        )
        return res.data[0], True
    except Exception as e:
        if getattr(e, "code", None) != "23505" and "duplicate key" not in str(e):
            raise
        winner = await get_by_request(clinic_id, user.username, run["client_request_id"])
        if winner is None:
            raise
        return winner, False


async def transition(clinic_id: str, username: str, run_id: str, from_states: tuple, to_state: str,
                     **fields) -> Optional[dict]:
    """CAS: move the run to `to_state` only if it is still in one of `from_states`.

    Returns the updated row, or None when another request moved it first (or it
    is not this user's run).
    """
    res = await sb(
        supabase.table("assistant_runs")
        .update({**fields, "state": to_state, "updated_at": _now().isoformat()})
        .eq("id", run_id).eq("clinic_id", clinic_id).eq("username", username)
        .in_("state", list(from_states))
    )
    return res.data[0] if res.data else None


async def expire_lapsed(clinic_id: str, username: str) -> None:
    """Lazy guards: lapsed clarifications/confirmations -> EXPIRED; EXECUTING for
    more than 2 minutes (worker restart, lost dispatch) -> UNKNOWN_OUTCOME."""
    now = _now()
    await sb(
        supabase.table("assistant_runs")
        .update({"state": "EXPIRED", "updated_at": now.isoformat()})
        .eq("clinic_id", clinic_id).eq("username", username)
        .in_("state", list(EXPIRING_STATES)).lt("expires_at", now.isoformat())
    )
    stuck = await sb(
        supabase.table("assistant_runs")
        .update({"state": "UNKNOWN_OUTCOME", "error_code": "stuck", "updated_at": now.isoformat()})
        .eq("clinic_id", clinic_id).eq("username", username)
        .in_("state", ["EXECUTING"]).lt("updated_at", (now - STUCK_AFTER).isoformat())
    )
    for row in stuck.data or []:
        logger.warning(f"ASSISTANT_RUN_STUCK run={row.get('id')} -> UNKNOWN_OUTCOME")


async def get_run(clinic_id: str, username: str, run_id: str) -> Optional[dict]:
    await expire_lapsed(clinic_id, username)
    res = await sb(
        supabase.table("assistant_runs").select("*")
        .eq("id", run_id).eq("clinic_id", clinic_id).eq("username", username).limit(1)
    )
    return res.data[0] if res.data else None


async def list_runs(clinic_id: str, username: str, active: bool, limit: int) -> list[dict]:
    await expire_lapsed(clinic_id, username)
    query = (supabase.table("assistant_runs").select("*")
             .eq("clinic_id", clinic_id).eq("username", username))
    if active:
        query = query.in_("state", list(ACTIVE_STATES))
    res = await sb(query.order("created_at", desc=True).limit(limit))
    return res.data or []
