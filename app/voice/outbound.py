"""Outbound calls: WhatsApp leads followed up by voice, and staff-requested calls.

Who may be called (same rule as the WhatsApp Leads page, migrations 092/093):
a patient row of THIS clinic, not STOPped (opted_in is not False) and no
explicit consent refusal (data_consent_declined_at is null). Never an arbitrary
number. Calls are placed only inside the clinic's outbound window (default
10:00-19:00 IST), at most outbound.daily_cap per clinic per day, at most
outbound.max_attempts per job, and never twice for one live job (DB unique index).
The agent introduces itself by name on behalf of the hospital and answers
truthfully if asked whether it is automated (responses.ai_disclosure).
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from app.config import settings
from app.database import sb, supabase
from app.services.tenant import ai_receptionist_enabled, get_clinic_by_id

from . import store
from .dates import IST
from .phone import to_e164

logger = logging.getLogger(__name__)


class OutboundRefused(Exception):
    pass


async def _eligible(clinic_id: str, phone: str) -> Optional[str]:
    """None if this person may be called, else the reason."""
    res = await sb(supabase.table("patients").select("id, opted_in, data_consent_declined_at")
                   .eq("clinic_id", clinic_id).eq("phone", phone).limit(1))
    p = (res.data or [None])[0]
    if not p:
        return "not_a_contact_of_this_clinic"
    if p.get("opted_in") is False or p.get("data_consent_declined_at"):
        return "do_not_contact"
    return None


async def enqueue(clinic: dict, phone: str, purpose: str, context: dict, created_by: str) -> dict:
    if not ai_receptionist_enabled(clinic):
        raise OutboundRefused("The AI receptionist is not enabled for this clinic.")
    e164 = to_e164(phone)
    if not e164:
        raise OutboundRefused("Not a valid phone number.")
    reason = await _eligible(clinic["id"], e164)
    if reason:
        raise OutboundRefused({"not_a_contact_of_this_clinic": "This number has never contacted the clinic.",
                               "do_not_contact": "This person opted out. Do not call."}[reason])
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("voice_outbound_jobs").insert({
            "clinic_id": clinic["id"], "patient_phone": e164, "purpose": purpose, "context": context or {},
            "created_by": created_by}))
        return res.data[0]
    except Exception as e:
        if "uq_voice_outbound_active" in str(e) or "23505" in str(e):
            raise OutboundRefused("A call to this person is already queued.")
        raise


def next_window_start(window: dict, now: Optional[datetime] = None) -> Optional[datetime]:
    """None when `now` is inside the outbound window, else when it next opens."""
    now = (now or datetime.now(IST)).astimezone(IST)
    start, end = window.get("window_start", "10:00"), window.get("window_end", "19:00")
    hhmm = now.strftime("%H:%M")
    if start <= hhmm < end:
        return None
    h, m = (int(x) for x in start.split(":"))
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return nxt if hhmm < start else nxt + timedelta(days=1)


async def _dial(phone: str, exophone: str, job_id: str) -> str:
    """Exotel 'connect number to call flow'. Returns the Exotel Call Sid."""
    url = f"https://{settings.exotel_api_host}/v1/Accounts/{settings.exotel_account_sid}/Calls/connect.json"
    cb = f"{settings.medassist_url.rstrip('/')}/voice/exotel/status?k={settings.voice_stream_token}"
    async with httpx.AsyncClient(timeout=15, auth=(settings.exotel_api_key, settings.exotel_api_token)) as c:
        r = await c.post(url, data={"From": phone, "CallerId": exophone, "Url": settings.exotel_outbound_flow_url,
                                    "CallType": "trans", "StatusCallback": cb, "CustomField": job_id})
    r.raise_for_status()
    sid = ((r.json() or {}).get("Call") or {}).get("Sid")
    if not sid:
        raise RuntimeError("Exotel response had no Call.Sid")
    return sid


async def _today_count(clinic_id: str) -> int:
    start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    res = await sb(supabase.table("voice_calls").select("id").eq("clinic_id", clinic_id)
                   .eq("direction", "outbound").gte("started_at", start).limit(1000))
    return len(res.data or [])


async def dispatch_due(limit: int = 20) -> int:
    """Scheduler body (under a distributed lock). Places due calls; returns how many."""
    now = datetime.now(IST)
    res = await sb(
        # unscoped: platform_sweep
        supabase.table("voice_outbound_jobs").select("*").eq("status", "queued")
        .lte("next_attempt_at", now.isoformat()).order("next_attempt_at").limit(limit))
    placed = 0
    for job in res.data or []:
        cid = job["clinic_id"]
        try:
            clinic = await get_clinic_by_id(cid)
        except Exception:
            clinic = None
        if not clinic or not ai_receptionist_enabled(clinic):
            await sb(supabase.table("voice_outbound_jobs").update({"status": "skipped", "last_error": "disabled"})
                     .eq("clinic_id", cid).eq("id", job["id"]))
            continue
        out_cfg = store.voice_config(clinic)["outbound"]
        later = next_window_start(out_cfg, now)
        if later is None and await _today_count(cid) >= int(out_cfg.get("daily_cap") or 0):
            later = next_window_start(out_cfg, now.replace(hour=23, minute=59))
        if later is not None:
            await sb(supabase.table("voice_outbound_jobs").update({"next_attempt_at": later.isoformat()})
                     .eq("clinic_id", cid).eq("id", job["id"]))
            continue
        if await _eligible(cid, job["patient_phone"]):
            await sb(supabase.table("voice_outbound_jobs").update({"status": "skipped", "last_error": "do_not_contact"})
                     .eq("clinic_id", cid).eq("id", job["id"]))
            continue
        nums = await sb(supabase.table("voice_numbers").select("exophone").eq("clinic_id", cid)
                        .eq("is_active", True).order("created_at").limit(1))
        if not nums.data:
            await sb(supabase.table("voice_outbound_jobs").update({"status": "failed", "last_error": "no_number"})
                     .eq("clinic_id", cid).eq("id", job["id"]))
            continue
        claimed = await sb(supabase.table("voice_outbound_jobs")
                           .update({"status": "dialing", "attempts": int(job["attempts"]) + 1,
                                    "updated_at": now.isoformat()})
                           .eq("clinic_id", cid).eq("id", job["id"]).eq("status", "queued"))
        if not claimed.data:
            continue  # another worker took it
        call = await store.create_call(cid, {"direction": "outbound", "status": "queued",
                                             "caller_phone": job["patient_phone"], "exophone": nums.data[0]["exophone"],
                                             "outbound_job_id": job["id"],
                                             "handoff_packet": {"interest": (job.get("context") or {}).get("interest")},
                                             "language": store.voice_config(clinic)["primary_language"]})
        if not call:
            await sb(supabase.table("voice_outbound_jobs").update({"status": "queued", "last_error": "db_write"})
                     .eq("clinic_id", cid).eq("id", job["id"]))
            continue
        try:
            sid = await _dial(job["patient_phone"], nums.data[0]["exophone"], job["id"])
            await store.update_call(cid, call["id"], {"provider_call_sid": sid, "status": "ringing"})
            await sb(supabase.table("voice_outbound_jobs").update({"call_id": call["id"]})
                     .eq("clinic_id", cid).eq("id", job["id"]))
            placed += 1
        except Exception as e:
            logger.error(f"VOICE_DIAL_FAILED job={job['id']}: {type(e).__name__}: {str(e)[:200]}")
            await store.update_call(cid, call["id"], {"status": "failed", "outcome": "dial_failed"})
            await job_finished({**call, "outbound_job_id": job["id"]}, success=False, error=type(e).__name__)
    return placed


async def job_finished(call: dict, success: bool, error: Optional[str] = None) -> None:
    job_id, cid = call.get("outbound_job_id"), call.get("clinic_id")
    if not job_id or not cid:
        return
    res = await sb(supabase.table("voice_outbound_jobs").select("attempts").eq("clinic_id", cid).eq("id", job_id))
    attempts = int(((res.data or [{}])[0]).get("attempts") or 0)
    try:
        clinic = await get_clinic_by_id(cid)
        max_attempts = int(store.voice_config(clinic)["outbound"].get("max_attempts") or 1)
    except Exception:
        max_attempts = 1
    if success:
        fields = {"status": "completed"}
    elif attempts < max_attempts:
        fields = {"status": "queued", "next_attempt_at": (datetime.now(IST) + timedelta(hours=2)).isoformat(),
                  "last_error": error}
    else:
        fields = {"status": "failed", "last_error": error}
    fields["updated_at"] = datetime.now(IST).isoformat()
    await sb(supabase.table("voice_outbound_jobs").update(fields).eq("clinic_id", cid).eq("id", job_id))


async def autoqueue_leads() -> int:
    """Hourly: queue a follow-up call for recent WhatsApp leads of clinics that
    switched outbound.auto_leads on. Skips anyone who booked, opted out, or was
    called in the last 7 days."""
    res = await sb(
        # unscoped: platform_sweep
        supabase.table("clinics").select("id, features, config, account_type, plan, name, is_active")
        .eq("account_type", "tenant").eq("is_active", True))
    queued = 0
    since = (datetime.now(IST) - timedelta(hours=24)).isoformat()
    week = (datetime.now(IST) - timedelta(days=7)).isoformat()
    for clinic in res.data or []:
        if not ai_receptionist_enabled(clinic) or not store.voice_config(clinic)["outbound"].get("auto_leads"):
            continue
        cid = clinic["id"]
        ev = await sb(supabase.table("analytics_events").select("phone, intent, department")
                      .eq("clinic_id", cid).eq("event_type", "lead_interest").gte("created_at", since).limit(500))
        seen = {}
        for e in ev.data or []:
            if e.get("phone") and e["phone"] not in seen:
                seen[e["phone"]] = e.get("department") or (e.get("intent") or "").replace("_", " ")
        cap = int(store.voice_config(clinic)["outbound"].get("daily_cap") or 0)
        for phone, interest in list(seen.items())[:cap]:
            booked = await sb(supabase.table("appointments").select("id").eq("clinic_id", cid)
                              .eq("patient_phone", phone).gte("created_at", since).limit(1))
            called = await sb(supabase.table("voice_outbound_jobs").select("id").eq("clinic_id", cid)
                              .eq("patient_phone", phone).gte("created_at", week).limit(1))
            if booked.data or called.data:
                continue
            try:
                await enqueue(clinic, phone, "lead_followup", {"interest": interest}, "auto_leads")
                queued += 1
            except OutboundRefused:
                continue
    return queued
