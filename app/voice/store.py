"""Persistence for the voice receptionist: number routing, calls, events,
lexicon, per-clinic voice config, usage and budget.

Every query on a tenant table carries .eq("clinic_id", ...) except the two
lookups whose result IS the tenant (Exophone, provider call sid); those are
annotated for tests/test_lint_unscoped_queries.py.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings
from app.database import sb, supabase
from app.tenancy import is_valid_clinic_scope
from .phone import to_e164

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_CONFIG: dict = {
    "primary_language": "te-IN",
    "assistant_name": "Kriya",
    "speaker": None,                  # None -> settings.sarvam_tts_speaker
    "pace": 1.0,
    "mode": "live",                   # live | shadow (observe only, always transfer)
    "reception_hours": {"start": "09:00", "end": "20:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"},
    "emergency_number": None,
    "monthly_budget_paise": 0,        # 0 = no cap
    "outbound": {"auto_leads": False, "window_start": "10:00", "window_end": "19:00",
                 "max_attempts": 2, "daily_cap": 30},
    "transcript_retention_days": None,  # None -> settings.voice_transcript_retention_days
}

_EVENT_KINDS = {"turn_user", "turn_agent", "nlu", "tool", "system", "error", "handoff", "safety", "latency"}


def voice_config(clinic: Optional[dict]) -> dict:
    """clinics.config.voice merged over DEFAULT_CONFIG (one level deep)."""
    raw = ((clinic or {}).get("config") or {}).get("voice") or {}
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out and isinstance(out[k], dict) and isinstance(v, dict):
                out[k].update(v)
            elif k in out:
                out[k] = v
    return out


def within_hours(window: dict, now: Optional[datetime] = None) -> bool:
    now = (now or datetime.now(IST)).astimezone(IST)
    days = [d.strip()[:3].title() for d in str(window.get("days") or "").split(",") if d.strip()]
    if days and now.strftime("%a") not in days:
        return False
    hhmm = now.strftime("%H:%M")
    return str(window.get("start") or "00:00") <= hhmm < str(window.get("end") or "23:59")


def new_call_ref(now: Optional[datetime] = None) -> str:
    now = (now or datetime.now(IST)).astimezone(IST)
    return f"CALL-{now:%Y%m%d}-{secrets.token_hex(3).upper()}"


async def resolve_number(exophone: str) -> Optional[dict]:
    """The active voice_numbers row for a dialled Exophone, or None. Fails closed."""
    if not exophone:
        return None
    candidates = [exophone]
    norm = to_e164(exophone)
    if norm and norm not in candidates:
        candidates.append(norm)
    for num in candidates:
        try:
            res = await sb(
                # unscoped: global_auth_lookup
                supabase.table("voice_numbers").select("*").eq("exophone", num).eq("is_active", True).limit(1)
            )
            if res.data:
                return res.data[0]
        except Exception as e:
            logger.error(f"VOICE_NUMBER_LOOKUP_FAILED for {num}: {e}")
    return None


async def create_call(clinic_id: str, fields: dict) -> Optional[dict]:
    if not is_valid_clinic_scope(clinic_id):
        raise ValueError("create_call needs a real clinic_id")
    row = {**fields, "clinic_id": clinic_id, "call_ref": fields.get("call_ref") or new_call_ref()}
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("voice_calls").insert(row))
        return (res.data or [None])[0]
    except Exception as e:
        if "uq_voice_calls_provider_sid" in str(e) or "23505" in str(e):
            return await get_call_by_sid(fields.get("provider_call_sid"))
        logger.error(f"VOICE_CALL_INSERT_FAILED clinic={clinic_id}: {e}")
        return None


async def get_call_by_sid(provider_call_sid: Optional[str], provider: str = "exotel") -> Optional[dict]:
    if not provider_call_sid:
        return None
    try:
        res = await sb(
            # unscoped: meta_callback_by_unique_id
            supabase.table("voice_calls").select("*").eq("provider", provider)
            .eq("provider_call_sid", provider_call_sid).limit(1)
        )
        return (res.data or [None])[0]
    except Exception as e:
        logger.error(f"VOICE_CALL_LOOKUP_FAILED: {e}")
        return None


async def get_call(clinic_id: str, call_id: str) -> Optional[dict]:
    res = await sb(supabase.table("voice_calls").select("*").eq("clinic_id", clinic_id).eq("id", call_id).limit(1))
    return (res.data or [None])[0]


async def update_call(clinic_id: str, call_id: str, fields: dict) -> None:
    """Never raises: a failed status write must not drop a live call."""
    try:
        await sb(supabase.table("voice_calls").update(fields).eq("clinic_id", clinic_id).eq("id", call_id))
    except Exception as e:
        logger.error(f"VOICE_CALL_UPDATE_FAILED call={call_id}: {e}")


async def add_event(clinic_id: str, call_id: str, kind: str, name: str, *, status: Optional[str] = None,
                    text: Optional[str] = None, data: Optional[dict] = None,
                    duration_ms: Optional[int] = None, correlation_id: Optional[str] = None) -> None:
    """Append to the call timeline. Never raises."""
    if kind not in _EVENT_KINDS:
        kind = "system"
    row = {"clinic_id": clinic_id, "call_id": call_id, "kind": kind, "name": name[:80], "status": status,
           "text": text[:2000] if text else None, "data": data or {}, "duration_ms": duration_ms,
           "correlation_id": correlation_id}
    try:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("voice_call_events").insert(row))
    except Exception as e:
        logger.warning(f"VOICE_EVENT_DROPPED call={call_id} {kind}/{name}: {e}")


async def load_lexicon(clinic_id: str) -> list:
    try:
        res = await sb(supabase.table("voice_lexicon_entries").select("kind, phrase, canonical, is_active")
                       .eq("clinic_id", clinic_id).eq("is_active", True).limit(2000))
        return res.data or []
    except Exception as e:
        logger.warning(f"VOICE_LEXICON_LOAD_FAILED clinic={clinic_id}: {e}")
        return []


# ---- usage and budget ----

def usage_cost_paise(stt_seconds: float, tts_chars: int, telephony_seconds: int) -> int:
    """Provider cost from YOUR configured rates (settings.voice_*). 0 when unset."""
    return int(round(
        stt_seconds / 60 * settings.voice_stt_paise_per_minute
        + tts_chars / 1000 * settings.voice_tts_paise_per_1k_chars
        + telephony_seconds / 60 * settings.voice_telephony_paise_per_minute
    ))


def month_start_ist(now: Optional[datetime] = None) -> datetime:
    now = (now or datetime.now(IST)).astimezone(IST)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def month_usage(clinic_id: str, now: Optional[datetime] = None) -> dict:
    """Calls, minutes and cost for this IST calendar month (paged, never capped at 1000)."""
    start = month_start_ist(now).isoformat()
    calls = minutes = cost = 0
    offset = 0
    while True:
        res = await sb(supabase.table("voice_calls").select("telephony_seconds, cost_paise")
                       .eq("clinic_id", clinic_id).gte("started_at", start).range(offset, offset + 999))
        rows = res.data or []
        calls += len(rows)
        minutes += sum(int(r.get("telephony_seconds") or 0) for r in rows)
        cost += sum(int(r.get("cost_paise") or 0) for r in rows)
        if len(rows) < 1000:
            break
        offset += 1000
    return {"calls": calls, "minutes": round(minutes / 60, 1), "cost_paise": cost}


async def budget_exceeded(clinic: dict) -> bool:
    """True when the clinic's monthly voice budget is set and used up. A failed
    read answers False: the caller is then still served, and the call is billed."""
    budget = int(voice_config(clinic).get("monthly_budget_paise") or 0)
    if budget <= 0:
        return False
    try:
        return (await month_usage(clinic["id"]))["cost_paise"] >= budget
    except Exception as e:
        logger.warning(f"VOICE_BUDGET_READ_FAILED clinic={clinic.get('id')}: {e}")
        return False
