"""AI Receptionist control room — /admin/voice (migration 098).

Visible only when the platform owner enabled clinics.features.ai_receptionist.
View routes need VOICE_VIEW (transcripts are patient data); changing settings,
lexicon, takeover and outbound calls need VOICE_MANAGE. clinic_admin and
super_admin hold both. Every query is scoped to the ONE clinic resolved by
enforce_clinic_access.
"""

import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import sb, supabase
from app.routers.admin import AdminUser, enforce_clinic_access, log_admin_action, verify_credentials
from app.services.tenant import ai_receptionist_enabled, get_clinic_by_id, invalidate_tenant_cache
from app.voice import outbound, store
from app.voice.dates import IST, norm
from app.voice.phone import to_e164
from app.voice.session import CallSession
from app.voice.tools import CallContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/voice", tags=["voice-admin"])

CALL_COLUMNS = ("id, call_ref, direction, mode, caller_phone, exophone, status, language, primary_intent, "
                "intents, outcome, handoff_reason, started_at, ended_at, telephony_seconds, cost_paise, quality, "
                "automation_paused, takeover_requested_at")
_SUCCESS = {"booked", "payment_link_sent", "answered", "report_sent", "cancelled", "refunded", "cancelled_late",
            "rescheduled", "status_told", "none", "not_ready", "nothing_to_change"}


def _can(user: AdminUser, perm: str) -> bool:
    return user.role in ("clinic_admin", "super_admin") or perm in (user.permissions or [])


async def _scope(user: AdminUser, clinic_id: str, perm: str) -> tuple:
    if not _can(user, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not ai_receptionist_enabled(clinic):
        raise HTTPException(status_code=403, detail="The AI receptionist is not enabled for this clinic.")
    return scope, clinic


def _ip(request: Optional[Request]) -> str:
    return request.client.host if request and request.client else "unknown"


# ---- calls ----

@router.get("/calls")
async def list_calls(clinic_id: str = "default", status: Optional[str] = None, direction: Optional[str] = None,
                     q: Optional[str] = None, days: int = 7, limit: int = 50, offset: int = 0,
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "VOICE_VIEW")
    since = (datetime.now(IST) - timedelta(days=max(1, min(days, 90)))).isoformat()
    query = (supabase.table("voice_calls").select(CALL_COLUMNS, count="exact").eq("clinic_id", scope)
             .gte("started_at", since))
    if status:
        query = query.eq("status", status)
    if direction in ("inbound", "outbound"):
        query = query.eq("direction", direction)
    if q:
        term = re.sub(r"[^\w+\-]", "", q)[:30]
        if term:
            query = query.or_(f"call_ref.ilike.%{term}%,caller_phone.ilike.%{term}%")
    res = await sb(query.order("started_at", desc=True).range(max(0, offset), max(0, offset) + max(1, min(limit, 100)) - 1))
    return {"calls": res.data or [], "total": res.count or 0}


@router.get("/live")
async def live_calls(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "VOICE_VIEW")
    since = (datetime.now(IST) - timedelta(hours=2)).isoformat()
    res = await sb(supabase.table("voice_calls").select(CALL_COLUMNS + ", dialog").eq("clinic_id", scope)
                   .in_("status", ["in_progress", "ringing"]).gte("started_at", since)
                   .order("started_at", desc=True).limit(50))
    rows = []
    for r in res.data or []:
        d = r.pop("dialog", None) or {}
        r["current_workflow"] = (d.get("task") or {}).get("wf")
        r["waiting_for"] = d.get("expect")
        rows.append(r)
    return {"calls": rows}


def _views(call: dict, events: list) -> dict:
    """The panel's 'what did the patient ask', 'why did Kriya respond so', action trace and proofs."""
    first_user = next((e for e in events if e["kind"] == "turn_user"), None)
    first_nlu = next((e for e in events if e["kind"] == "nlu"), None)
    last_nlu = next((e for e in reversed(events) if e["kind"] == "nlu"), None)
    dialog = call.get("dialog") or {}
    task = dialog.get("task") or {}
    return {
        "patient_request": {"transcript": (first_user or {}).get("text"),
                            "understood": (first_nlu or {}).get("data")},
        "why": {"last_understanding": (last_nlu or {}).get("data"), "current_workflow": task.get("wf"),
                "collected": {k: v for k, v in (task.get("slots") or {}).items()
                              if k in ("department", "specialty", "date", "time_period", "clock_time",
                                       "patient_name", "relation")},
                "waiting_for": dialog.get("expect"), "completed": dialog.get("outcomes", [])},
        "trace": [e for e in events if e["kind"] in ("tool", "handoff", "safety", "error")],
        "proofs": [e for e in events if e["kind"] == "tool" and e["name"].endswith("VERIFY")],
        "conversation": [e for e in events if e["kind"] in ("turn_user", "turn_agent")],
    }


@router.get("/calls/{call_id}")
async def call_detail(call_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "VOICE_VIEW")
    call = await store.get_call(scope, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    ev = await sb(supabase.table("voice_call_events").select("id, ts, kind, name, status, text, data, duration_ms")
                  .eq("clinic_id", scope).eq("call_id", call_id).order("id").limit(2000))
    events = ev.data or []
    return {"call": call, "events": events, **_views(call, events)}


async def _flag(call_id: str, scope: str, fields: dict) -> dict:
    res = await sb(supabase.table("voice_calls").update(fields).eq("clinic_id", scope).eq("id", call_id)
                   .eq("status", "in_progress"))
    if not res.data:
        raise HTTPException(status_code=409, detail="This call is no longer in progress.")
    return {"success": True}


@router.post("/calls/{call_id}/takeover")
async def takeover(call_id: str, request: Request, clinic_id: str = "default",
                   user: AdminUser = Depends(verify_credentials)):
    """Kriya stops at the caller's next utterance and transfers to reception."""
    scope, _ = await _scope(user, clinic_id, "VOICE_MANAGE")
    out = await _flag(call_id, scope, {"takeover_requested_at": datetime.now(IST).isoformat()})
    await log_admin_action(user, "voice_takeover", "voice_call", call_id, ip_address=_ip(request))
    return out


@router.post("/calls/{call_id}/pause")
async def pause(call_id: str, request: Request, clinic_id: str = "default",
                user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "VOICE_MANAGE")
    out = await _flag(call_id, scope, {"automation_paused": True})
    await log_admin_action(user, "voice_pause", "voice_call", call_id, ip_address=_ip(request))
    return out


# ---- analytics ----

@router.get("/analytics")
async def analytics(clinic_id: str = "default", days: int = 7, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "VOICE_VIEW")
    days = max(1, min(days, 90))
    since = (datetime.now(IST) - timedelta(days=days)).isoformat()
    # ponytail: 5000 most recent calls per window; page this when a clinic exceeds it.
    res = await sb(supabase.table("voice_calls").select("status, direction, language, primary_intent, outcome, "
                                                        "quality, cost_paise, telephony_seconds, dialog")
                   .eq("clinic_id", scope).gte("started_at", since).order("started_at", desc=True).limit(5000))
    rows = res.data or []
    by_status, by_intent, by_lang = Counter(), Counter(), {}
    per_wf: dict = {}
    scores = []
    for r in rows:
        by_status[r["status"]] += 1
        if r.get("primary_intent"):
            by_intent[r["primary_intent"]] += 1
        lang = r.get("language") or "?"
        L = by_lang.setdefault(lang, {"calls": 0, "succeeded": 0, "handoffs": 0})
        L["calls"] += 1
        outs = (r.get("dialog") or {}).get("outcomes") or []
        if any(o.get("outcome") in _SUCCESS for o in outs):
            L["succeeded"] += 1
        if r["status"] == "handed_off":
            L["handoffs"] += 1
        for o in outs:
            w = per_wf.setdefault(o.get("wf"), {"total": 0, "autonomous": 0})
            w["total"] += 1
            if o.get("outcome") in _SUCCESS:
                w["autonomous"] += 1
        if (r.get("quality") or {}).get("score") is not None:
            scores.append(r["quality"]["score"])
    autonomy = {wf: round(100 * v["autonomous"] / v["total"], 1) for wf, v in per_wf.items()
                if wf not in ("HUMAN", "CALL") and v["total"]}
    total_wf = sum(v["total"] for k, v in per_wf.items() if k not in ("HUMAN", "CALL"))
    overall = round(100 * sum(v["autonomous"] for k, v in per_wf.items() if k not in ("HUMAN", "CALL")) / total_wf, 1) \
        if total_wf else None
    ev = await sb(supabase.table("voice_call_events").select("text, data").eq("clinic_id", scope)
                  .eq("kind", "nlu").eq("name", "NOT_UNDERSTOOD").gte("ts", since).limit(2000))
    phrases = Counter(norm(e.get("text") or "") for e in ev.data or [] if e.get("text"))
    return {
        "days": days, "total_calls": len(rows), "by_status": dict(by_status), "by_intent": dict(by_intent),
        "by_language": by_lang, "autonomy_by_workflow": autonomy, "autonomy_overall": overall,
        "avg_quality": round(sum(scores) / len(scores), 1) if scores else None,
        "minutes": round(sum(int(r.get("telephony_seconds") or 0) for r in rows) / 60, 1),
        "cost_paise": sum(int(r.get("cost_paise") or 0) for r in rows),
        "top_failure_phrases": [{"phrase": p, "count": c} for p, c in phrases.most_common(20) if p],
        "note": "Autonomy = workflows completed with a verified result and no human, from voice_calls.dialog.",
    }


# ---- settings, lexicon, numbers, usage ----

class Hours(BaseModel):
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    days: str = Field(max_length=40)


class Outbound(BaseModel):
    auto_leads: bool = False
    window_start: str = Field("10:00", pattern=r"^\d{2}:\d{2}$")
    window_end: str = Field("19:00", pattern=r"^\d{2}:\d{2}$")
    max_attempts: int = Field(2, ge=1, le=3)
    daily_cap: int = Field(30, ge=0, le=500)


class VoiceSettingsIn(BaseModel):
    primary_language: str = Field(pattern=r"^(te|hi|en)-IN$")
    assistant_name: str = Field(min_length=2, max_length=30)
    speaker: Optional[str] = Field(None, max_length=30)
    pace: float = Field(1.0, ge=0.5, le=2.0)
    reception_hours: Hours
    emergency_number: Optional[str] = Field(None, max_length=20)
    outbound: Outbound


@router.get("/settings")
async def get_settings(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "VOICE_VIEW")
    cfg = store.voice_config(clinic)
    return {"settings": cfg, "budget_paise": cfg["monthly_budget_paise"]}


@router.put("/settings")
async def put_settings(body: VoiceSettingsIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    """Hospital-editable voice settings. The monthly budget is NOT editable here:
    only the platform owner sets it (PUT /platform/voice/clinics/{id}/budget)."""
    if (body.outbound.window_start < "09:00" or body.outbound.window_end > "21:00"
            or body.outbound.window_start >= body.outbound.window_end):
        raise HTTPException(status_code=422, detail="Outbound calls are allowed only between 09:00 and 21:00.")
    scope, _ = await _scope(user, clinic_id, "VOICE_MANAGE")
    # unscoped: unique_row_key (clinics.id IS the tenant, resolved by enforce_clinic_access)
    res = await sb(supabase.table("clinics").select("config").eq("id", scope).limit(1))
    config = dict((res.data or [{}])[0].get("config") or {})
    voice = dict(config.get("voice") or {})
    voice.update(body.model_dump())
    config["voice"] = voice
    # unscoped: unique_row_key (clinics.id IS the tenant, resolved by enforce_clinic_access)
    await sb(supabase.table("clinics").update({"config": config}).eq("id", scope))
    invalidate_tenant_cache()
    await log_admin_action(user, "voice_settings_update", "clinic", scope, details=body.model_dump(),
                           ip_address=_ip(request))
    return {"success": True}


class LexiconIn(BaseModel):
    kind: str = Field(pattern=r"^(specialty_synonym|doctor_alias|test_alias|pronunciation)$")
    phrase: str = Field(min_length=1, max_length=80)
    canonical: str = Field(min_length=1, max_length=120)
    language: Optional[str] = Field(None, max_length=8)


@router.get("/lexicon")
async def list_lexicon(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "VOICE_VIEW")
    res = await sb(supabase.table("voice_lexicon_entries").select("*").eq("clinic_id", scope)
                   .order("created_at", desc=True).limit(2000))
    return {"entries": res.data or []}


@router.post("/lexicon")
async def add_lexicon(body: LexiconIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "VOICE_MANAGE")
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("voice_lexicon_entries").insert(
            {**body.model_dump(), "clinic_id": scope, "created_by": user.username}))
    except Exception as e:
        if "23505" in str(e) or "uq_voice_lexicon_phrase" in str(e):
            raise HTTPException(status_code=409, detail="That phrase already exists.")
        raise HTTPException(status_code=500, detail="Could not save the entry.")
    await log_admin_action(user, "voice_lexicon_add", "voice_lexicon", res.data[0]["id"], ip_address=_ip(request))
    return {"entry": res.data[0]}


@router.delete("/lexicon/{entry_id}")
async def delete_lexicon(entry_id: str, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "VOICE_MANAGE")
    res = await sb(supabase.table("voice_lexicon_entries").delete().eq("clinic_id", scope).eq("id", entry_id))
    if not res.data:
        raise HTTPException(status_code=404, detail="Entry not found")
    await log_admin_action(user, "voice_lexicon_delete", "voice_lexicon", entry_id, ip_address=_ip(request))
    return {"success": True}


@router.get("/numbers")
async def list_numbers(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "VOICE_VIEW")
    res = await sb(supabase.table("voice_numbers").select("id, branch_id, exophone, published_number, "
                                                          "reception_number, label, is_active")
                   .eq("clinic_id", scope).order("created_at"))
    return {"numbers": res.data or []}


@router.get("/usage")
async def usage(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "VOICE_VIEW")
    u = await store.month_usage(scope)
    budget = int(store.voice_config(clinic).get("monthly_budget_paise") or 0)
    return {**u, "budget_paise": budget, "budget_used_pct": round(100 * u["cost_paise"] / budget, 1) if budget else None,
            "rates_configured": bool(store.usage_cost_paise(60, 1000, 60))}


# ---- test console ----

class TestTurnIn(BaseModel):
    call_id: Optional[str] = None
    text: Optional[str] = Field(None, max_length=400)
    caller_phone: Optional[str] = Field(None, max_length=20)


@router.post("/test-turn")
async def test_turn(body: TestTurnIn, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    """Text conversation with the real engine in TEST mode: reads are real,
    writes (bookings, cancels, report sends, callbacks) are simulated and
    labelled so in the trace. No telephony, no TTS cost."""
    scope, clinic = await _scope(user, clinic_id, "VOICE_MANAGE")
    if body.call_id:
        call = await store.get_call(scope, body.call_id)
        if not call or call.get("mode") != "test":
            raise HTTPException(status_code=404, detail="Test call not found")
    else:
        phone = to_e164(body.caller_phone) or "+910000000000"
        call = await store.create_call(scope, {"direction": "inbound", "mode": "test", "caller_phone": phone,
                                               "language": store.voice_config(clinic)["primary_language"]})
        if not call:
            raise HTTPException(status_code=503, detail="Could not start a test call.")
    ctx = CallContext(call_id=call["id"], call_ref=call["call_ref"], clinic=clinic, branch_id=call.get("branch_id"),
                      caller_phone=call["caller_phone"], mode="test", reception_number=None,
                      lang=call.get("language") or "te-IN", correlation_id=call["call_ref"])
    session = CallSession(ctx, dialog_state=call.get("dialog") or None)
    reply = await session.handle(body.text) if body.call_id and body.text else await session.start()
    if reply.control != "continue":
        await session.finish("handed_off" if reply.control == "transfer" else "completed")
    return {"call_id": call["id"], "call_ref": call["call_ref"], "say": reply.texts, "control": reply.control}


# ---- outbound ----

class OutboundIn(BaseModel):
    phone: str = Field(min_length=10, max_length=20)
    purpose: str = Field("lead_followup", pattern=r"^(lead_followup|callback_request)$")
    interest: Optional[str] = Field(None, max_length=60)


@router.post("/outbound")
async def create_outbound(body: OutboundIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "VOICE_MANAGE")
    try:
        job = await outbound.enqueue(clinic, body.phone, body.purpose, {"interest": body.interest}, user.username)
    except outbound.OutboundRefused as e:
        raise HTTPException(status_code=409, detail=str(e))
    await log_admin_action(user, "voice_outbound_enqueue", "voice_outbound_job", job["id"], ip_address=_ip(request))
    return {"job": job}


@router.get("/outbound")
async def list_outbound(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "VOICE_VIEW")
    res = await sb(supabase.table("voice_outbound_jobs").select("*").eq("clinic_id", scope)
                   .order("created_at", desc=True).limit(100))
    return {"jobs": res.data or []}
