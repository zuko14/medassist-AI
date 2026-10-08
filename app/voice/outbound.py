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
import re
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


def _blocked(p: Optional[dict], attested: bool) -> Optional[str]:
    """The eligibility rule on a patients row (None = no row)."""
    if not p:
        # Staff attested this person asked to be contacted (walk-in enquiry,
        # health camp, referral...): consent is recorded on the job instead.
        return None if attested else "not_a_contact_of_this_clinic"
    if p.get("opted_in") is False or p.get("data_consent_declined_at"):
        return "do_not_contact"  # an opt-out always wins over an attestation
    return None


async def _eligible(clinic_id: str, phone: str, attested: bool = False) -> Optional[str]:
    """None if this person may be called, else the reason."""
    res = await sb(supabase.table("patients").select("id, opted_in, data_consent_declined_at")
                   .eq("clinic_id", clinic_id).eq("phone", phone).limit(1))
    return _blocked((res.data or [None])[0], attested)


def _after(ts, cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")) >= cutoff
    except ValueError:
        return True  # unreadable: assume recent, i.e. do not call again


def _attested(job: dict) -> bool:
    return bool((job.get("context") or {}).get("consent"))


async def enqueue(clinic: dict, phone: str, purpose: str, context: dict, created_by: str) -> dict:
    if not ai_receptionist_enabled(clinic):
        raise OutboundRefused("The AI receptionist is not enabled for this clinic.")
    e164 = to_e164(phone)
    if not e164:
        raise OutboundRefused("Not a valid phone number.")
    reason = await _eligible(clinic["id"], e164, bool((context or {}).get("consent")))
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


BULK_MAX = 500
_SKIP_TEXT = {"invalid_number": "Not a valid phone number",
              "duplicate": "Listed twice in this import",
              "do_not_contact": "Opted out — never called",
              "not_a_contact_of_this_clinic": "Never contacted the clinic",
              "already_queued": "A call is already queued",
              "called_recently": "Called in the last 7 days"}


async def enqueue_bulk(clinic: dict, contacts: list, consent: Optional[dict], created_by: str) -> dict:
    """Queue lead follow-up calls for many people at once.

    contacts: [{"phone": ..., "interest": ...}]. consent: {"source": ...} when
    staff attested these people asked to be contacted, else None (then only
    existing contacts of the clinic are queued). Same rules as enqueue(): an
    opt-out is never called; someone with a live job, or called in the last 7
    days, is skipped. Calls go out one by one inside the outbound window and
    under the daily cap -- dispatch_due paces them.
    """
    if not ai_receptionist_enabled(clinic):
        raise OutboundRefused("The AI receptionist is not enabled for this clinic.")
    cid = clinic["id"]
    skipped, wanted, seen = [], {}, set()
    for c in contacts[:BULK_MAX]:
        raw = str((c or {}).get("phone") or "").strip()
        e164 = to_e164(raw)
        if not e164:
            skipped.append({"phone": raw, "reason": _SKIP_TEXT["invalid_number"]})
        elif e164 in seen:
            skipped.append({"phone": e164, "reason": _SKIP_TEXT["duplicate"]})
        else:
            seen.add(e164)
            wanted[e164] = ((c or {}).get("interest") or "").strip()[:60] or None
    phones = list(wanted)
    patients, recent = {}, set()
    week = datetime.now(IST) - timedelta(days=7)
    for i in range(0, len(phones), 200):
        chunk = phones[i:i + 200]
        res = await sb(supabase.table("patients").select("phone, opted_in, data_consent_declined_at")
                       .eq("clinic_id", cid).in_("phone", chunk))
        patients.update({p["phone"]: p for p in res.data or []})
        res = await sb(supabase.table("voice_outbound_jobs").select("patient_phone, status, created_at")
                       .eq("clinic_id", cid).eq("purpose", "lead_followup").in_("patient_phone", chunk)
                       .order("created_at", desc=True).limit(2000))
        for j in res.data or []:
            active = j["status"] in ("queued", "dialing")
            if active or _after(j.get("created_at"), week):
                recent.add((j["patient_phone"], active))
    live = {p for p, active in recent if active}
    called = {p for p, _ in recent}
    rows = []
    for phone in phones:
        reason = _blocked(patients.get(phone), bool(consent))
        if not reason and phone in live:
            reason = "already_queued"
        elif not reason and phone in called:
            reason = "called_recently"
        if reason:
            skipped.append({"phone": phone, "reason": _SKIP_TEXT[reason]})
            continue
        ctx = {"interest": wanted[phone]}
        if consent:
            ctx["consent"] = {**consent, "attested_by": created_by, "at": datetime.now(IST).isoformat()}
        rows.append({"clinic_id": cid, "patient_phone": phone, "purpose": "lead_followup",
                     "context": ctx, "created_by": created_by})
    queued = 0
    if rows:
        try:
            # unscoped: insert_scoped_by_payload
            queued = len((await sb(supabase.table("voice_outbound_jobs").insert(rows))).data or [])
        except Exception as e:
            if "uq_voice_outbound_active" not in str(e) and "23505" not in str(e):
                raise
            # A concurrent enqueue raced us for one of these people: insert one by one.
            for row in rows:
                try:
                    # unscoped: insert_scoped_by_payload
                    await sb(supabase.table("voice_outbound_jobs").insert(row))
                    queued += 1
                except Exception:
                    skipped.append({"phone": row["patient_phone"], "reason": _SKIP_TEXT["already_queued"]})
    return {"queued": queued, "skipped": skipped, "truncated": max(0, len(contacts) - BULK_MAX)}


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


async def _in_flight(clinic_id: str) -> int:
    """Outbound calls of this clinic still ringing or talking. A row stuck in a
    live status (a lost provider callback) stops counting after 15 minutes,
    longer than the longest allowed call, so it cannot block the queue."""
    since = (datetime.now(IST) - timedelta(minutes=15)).isoformat()
    res = await sb(supabase.table("voice_calls").select("id").eq("clinic_id", clinic_id)
                   .eq("direction", "outbound").in_("status", ["queued", "ringing", "in_progress"])
                   .gte("started_at", since).limit(50))
    return len(res.data or [])


async def dispatch_due(limit: int = 20) -> int:
    """Scheduler body (under a distributed lock). Places due calls; returns how many."""
    now = datetime.now(IST)
    res = await sb(
        # unscoped: platform_sweep
        supabase.table("voice_outbound_jobs").select("*").eq("status", "queued")
        .lte("next_attempt_at", now.isoformat()).order("next_attempt_at").limit(limit))
    placed = 0
    busy: dict = {}  # clinic -> calls in flight, counted once per sweep
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
        if cid not in busy:
            busy[cid] = await _in_flight(cid)
        if busy[cid] >= max(1, int(out_cfg.get("concurrent") or 1)):
            # One by one: wait for the current call to end. Moving the job back
            # a little rotates the queue so other clinics' calls are not starved.
            await sb(supabase.table("voice_outbound_jobs")
                     .update({"next_attempt_at": (now + timedelta(minutes=3)).isoformat()})
                     .eq("clinic_id", cid).eq("id", job["id"]))
            continue
        if await _eligible(cid, job["patient_phone"], _attested(job)):
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
            busy[cid] += 1
        except Exception as e:
            error = type(e).__name__
            detail = str(e)[:200]
            if isinstance(e, httpx.HTTPStatusError):
                # The status says what to fix (401 credentials, 400/403 flow URL,
                # caller id or account restrictions); the body has Exotel's reason.
                error = f"exotel_http_{e.response.status_code}"
                detail = (e.response.text or "")[:300]
            # Never log a full phone number (Exotel echoes From/CallerId).
            detail = re.sub(r"\d{6,}(\d{4})", r"XXXXXX\1", detail)
            logger.error(f"VOICE_DIAL_FAILED job={job['id']}: {error}: {detail}")
            await store.update_call(cid, call["id"], {"status": "failed", "outcome": "dial_failed"})
            await job_finished({**call, "outbound_job_id": job["id"]}, success=False, error=error)
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
