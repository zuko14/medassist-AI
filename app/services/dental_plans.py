"""Dental treatment plans: multi-sitting courses, reminders, reviews, quotas.

Why this exists
---------------
Dental care is a COURSE, not a visit. A Root Canal is typically 2-4 sittings,
an implant or aligner case many more, and the next sitting is fixed at the
front desk after the dentist has seen the patient — often with a different
dentist when the first one is not free. A plain appointment models one visit.

Model (migration 089)
---------------------
* ``dental_treatment_plans`` — one row per course: patient, treatment, tooth
  numbers, planned sittings, quote, WhatsApp consent, notify switches.
* Each sitting IS an ``appointments`` row (booking_type 'consultation') carrying
  ``treatment_plan_id`` + ``sitting_number``. It is booked through
  ``database.book_appointment`` and offered only from ``get_available_slots``,
  so the existing slot-uniqueness index, leave/holiday checks, check-in, queue
  and Appointments page all apply to sittings unchanged.

Outbound (all dental-only, all counted against owner-set monthly limits)
------------------------------------------------------------------------
* patient: confirmation when a sitting is booked, reminder the day before
* doctor:  per-sitting assignment notice (manual) + daily schedule digest
* review:  "How was your sitting?" with three quick-reply buttons, after a
           sitting is completed; the tap is handled by handle_review_reply()

Interaction with the generic jobs (app/services/scheduler.py)
-------------------------------------------------------------
* The dental reminder job runs at 08:30 IST and sets ``reminder_24h_sent`` so
  the generic 09:00 job skips that sitting. If the dental send is impossible
  (limit reached, template not approved) the flag is left alone and the
  generic reminder still goes out — a patient is never left un-reminded
  because of a quota.
* Plan sittings are booked with ``followup_sent`` already True: the review
  replaces the generic post-visit follow-up.
* A patient who is not messageable (no WhatsApp consent and never messaged the
  clinic) gets their sittings booked with every reminder flag already set, so
  NO job — dental or generic — ever messages them.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.database import sb, supabase
from app.tenancy import is_valid_clinic_scope

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── Quotas ──────────────────────────────────────────────────────────────────
QUOTA_KINDS = ("patient", "doctor", "review")
QUOTA_LABELS = {"patient": "Patient sitting messages", "doctor": "Doctor reminders",
                "review": "Review requests"}
#: Monthly defaults until the owner sets per-clinic limits (clinics.config.dental_message_limits).
DEFAULT_MESSAGE_LIMITS = {"patient": 1000, "doctor": 300, "review": 500}
MAX_MESSAGE_LIMIT = 100_000
QUOTA_WARN_PERCENT = 90

# ── Meta templates ─────────────────────────────────────────────────────────
REVIEW_PAYLOAD_PREFIX = "dentrev:"
#: 3 = Excellent, 2 = Good, 1 = Needs improvement
REVIEW_OPTIONS = ((3, "Excellent"), (2, "Good"), (1, "Needs improvement"))

#: The five dental templates, in the exact shape Meta's
#: POST /{waba_id}/message_templates expects. Submitted per clinic from the
#: owner panel. Bodies never start or end with a variable (Meta rejects that).
DENTAL_TEMPLATES = {
    "confirmation": {
        "name": "dental_sitting_confirmation",
        "category": "UTILITY",
        "body": ("Hello {{1}}, your {{2}} appointment at {{3}} is confirmed: sitting {{4}} of {{5}} "
                 "with {{6}} on {{7}} at {{8}}. Please reply here or call the clinic if you need to change it."),
        "example": ["Ravi", "Root Canal Treatment", "Smile Dental", "2", "3", "Dr. Priya", "Mon, 06 Oct", "10:30 AM"],
    },
    "reminder": {
        "name": "dental_sitting_reminder",
        "category": "UTILITY",
        "body": ("Reminder from {{1}}: {{2}}, your {{3}} sitting {{4}} of {{5}} with {{6}} is tomorrow, "
                 "{{7}} at {{8}}. Please arrive 10 minutes early."),
        "example": ["Smile Dental", "Ravi", "Root Canal Treatment", "2", "3", "Dr. Priya", "Mon, 06 Oct", "10:30 AM"],
    },
    "doctor_sitting": {
        "name": "dental_doctor_sitting",
        "category": "UTILITY",
        "body": ("Hello {{1}}, a dental sitting has been assigned to you: {{2}} for {{3}}, sitting {{4}} of {{5}}, "
                 "on {{6}} at {{7}}. Please plan your chair time accordingly."),
        "example": ["Dr. Priya", "Ravi Kumar", "Root Canal Treatment", "2", "3", "Mon, 06 Oct", "10:30 AM"],
    },
    "doctor_schedule": {
        "name": "dental_doctor_schedule",
        "category": "UTILITY",
        "body": ("Hello {{1}}, your dental sittings for {{2}} at {{3}}: {{4}}. "
                 "Please contact the front desk for any changes."),
        "example": ["Dr. Priya", "Mon, 06 Oct", "Smile Dental",
                    "10:30 AM Ravi Kumar - Root Canal 2/3; 12:00 PM Sita - Implant 1/4"],
    },
    "review": {
        "name": "dental_sitting_review",
        "category": "UTILITY",
        "body": ("Thank you for visiting {{1}}, {{2}}. How was your {{3}} sitting today? "
                 "Please tap an option below."),
        "example": ["Smile Dental", "Ravi", "Root Canal Treatment"],
        "buttons": [label for _, label in REVIEW_OPTIONS],
    },
}

# ── Settings (clinics.config, admin-editable) ──────────────────────────────
SETTINGS_DEFAULTS = {
    "dental_review_enabled": True,
    "dental_doctor_digest_enabled": True,
    "dental_google_review_link": "",
}


class DentalError(ValueError):
    """User-facing validation error (400/409). The message is safe to show."""


# ═══════════════════════════════════════════════════════════════════════════
# Pure helpers
# ═══════════════════════════════════════════════════════════════════════════


def ist_now() -> datetime:
    return datetime.now(IST)


def current_month() -> str:
    return ist_now().strftime("%Y-%m")


def flat(text, limit: int = 300) -> str:
    """Meta rejects newlines, tabs and 4+ spaces in a template parameter."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit] or "-"


def fmt_day(d) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return d.strftime("%a, %d %b")


def fmt_time(t) -> str:
    from app.utils.helpers import format_slot_time
    return format_slot_time(t)


def doctor_title(name: Optional[str]) -> str:
    n = (name or "").strip()
    if not n:
        return "Doctor"
    return n if n.lower().startswith("dr") else f"Dr. {n}"


def first_name(name: Optional[str]) -> str:
    parts = (name or "").split()
    return parts[0] if parts else "there"


def message_limits(config: Optional[dict]) -> dict:
    """Owner-set monthly limits per kind. A missing/invalid value falls back to
    the default; 0 means that kind is switched off for the clinic."""
    raw = (config or {}).get("dental_message_limits") or {}
    out = {}
    for kind in QUOTA_KINDS:
        v = raw.get(kind) if isinstance(raw, dict) else None
        valid = isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_MESSAGE_LIMIT
        out[kind] = v if valid else DEFAULT_MESSAGE_LIMITS[kind]
    return out


def messaging_addon_paise(config: Optional[dict]) -> int:
    raw = (config or {}).get("dental_messaging_addon_paise")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0


def dental_settings(config: Optional[dict]) -> dict:
    cfg = config or {}
    out = {}
    for k, default in SETTINGS_DEFAULTS.items():
        v = cfg.get(k)
        out[k] = v if isinstance(v, type(default)) else default
    return out


def quota_level(used: int, limit: int) -> dict:
    used = max(0, int(used or 0))
    limit = max(0, int(limit or 0))
    percent = 100 if limit == 0 else min(100, used * 100 // limit)
    level = "full" if used >= limit else "warning" if percent >= QUOTA_WARN_PERCENT else "ok"
    return {"used": used, "limit": limit, "remaining": max(0, limit - used),
            "percent": percent, "level": level}


def review_payload(appointment_id: str, score: int) -> str:
    return f"{REVIEW_PAYLOAD_PREFIX}{appointment_id}:{score}"


def parse_review_payload(payload: Optional[str]) -> Optional[tuple]:
    m = re.fullmatch(re.escape(REVIEW_PAYLOAD_PREFIX) + r"([0-9a-fA-F-]{36}):([123])", payload or "")
    return (m.group(1), int(m.group(2))) if m else None


def meta_template_payload(key: str) -> dict:
    """The request body for Meta's template-creation API."""
    t = DENTAL_TEMPLATES[key]
    components: list = [{"type": "BODY", "text": t["body"], "example": {"body_text": [t["example"]]}}]
    if t.get("buttons"):
        components.append({"type": "BUTTONS",
                           "buttons": [{"type": "QUICK_REPLY", "text": b} for b in t["buttons"]]})
    return {"name": t["name"], "language": "en", "category": t["category"], "components": components}


def _body(*params) -> list:
    return [{"type": "body", "parameters": [{"type": "text", "text": flat(p)} for p in params]}]


def sitting_label(appt: dict, plan: dict) -> tuple:
    return str(appt.get("sitting_number") or "?"), str(plan.get("planned_sittings") or "?")


def summarize(plan: dict, sittings: list) -> dict:
    live = [s for s in sittings if s.get("status") != "cancelled"]
    done = [s for s in live if s.get("status") == "completed"]
    upcoming = sorted(
        (s for s in live if s.get("status") == "confirmed"),
        key=lambda s: (s.get("appointment_date") or "", s.get("appointment_time") or ""),
    )
    collected = sum(int(s.get("amount_collected_paise") or 0) for s in sittings)
    quoted = plan.get("quoted_amount_paise")
    used_numbers = {s.get("sitting_number") for s in live}
    next_number = next((n for n in range(1, 31) if n not in used_numbers), None)
    return {
        "sittings_done": len(done),
        "sittings_booked": len(upcoming),
        "next_sitting_number": next_number,
        "next_sitting": upcoming[0] if upcoming else None,
        "collected_paise": collected,
        "balance_paise": (max(0, quoted - collected) if isinstance(quoted, int) else None),
        "ratings": [s.get("review_rating") for s in done if s.get("review_rating")],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Database access — every query carries the clinic predicate
# ═══════════════════════════════════════════════════════════════════════════


def _scope(clinic_id: Optional[str]) -> str:
    if not is_valid_clinic_scope(clinic_id):
        raise ValueError(f"Refusing dental operation on invalid clinic_id: {clinic_id!r}")
    return str(clinic_id)


async def get_plan(clinic_id: str, plan_id: str) -> Optional[dict]:
    res = await sb(supabase.table("dental_treatment_plans").select("*")
                   .eq("clinic_id", _scope(clinic_id)).eq("id", plan_id).limit(1))
    return (res.data or [None])[0]


async def plan_sittings(clinic_id: str, plan_id: str) -> list:
    res = await sb(supabase.table("appointments").select("*")
                   .eq("clinic_id", _scope(clinic_id)).eq("treatment_plan_id", plan_id)
                   .order("sitting_number").order("appointment_date").order("appointment_time"))
    return res.data or []


async def get_sitting(clinic_id: str, appointment_id: str) -> Optional[dict]:
    res = await sb(supabase.table("appointments").select("*")
                   .eq("clinic_id", _scope(clinic_id)).eq("id", appointment_id).limit(1))
    row = (res.data or [None])[0]
    return row if row and row.get("treatment_plan_id") else None


async def get_doctor(clinic_id: str, doctor_id: str) -> Optional[dict]:
    res = await sb(supabase.table("doctors").select("*")
                   .eq("clinic_id", _scope(clinic_id)).eq("id", doctor_id).limit(1))
    return (res.data or [None])[0]


async def is_whatsapp_contact(clinic_id: str, phone: str) -> bool:
    """Has this number ever messaged the clinic's WhatsApp (a patients row)?"""
    res = await sb(supabase.table("patients").select("id")
                   .eq("clinic_id", _scope(clinic_id)).eq("phone", phone).limit(1))
    return bool(res.data)


async def patient_messageable(clinic_id: str, plan: dict) -> bool:
    """Transactional sitting messages need either the front desk's recorded
    consent or a patient who has messaged this clinic on WhatsApp before."""
    if not plan.get("notify_patient"):
        return False
    if plan.get("whatsapp_consent"):
        return True
    return await is_whatsapp_contact(clinic_id, plan["patient_phone"])


# ── Quota (atomic, via the reserve_message_quota RPC) ──────────────────────


async def reserve_quota(clinic: dict, kind: str) -> bool:
    """Take one message from this month's allowance, or refuse. Never raises:
    a quota-service failure refuses (fail closed on spend)."""
    limit = message_limits(clinic.get("config"))[kind]
    month = current_month()
    try:
        res = await sb(supabase.rpc("reserve_message_quota", {
            "p_clinic_id": clinic["id"], "p_month": month, "p_kind": kind, "p_limit": limit,
        }))
        used = res.data if isinstance(res.data, int) and not isinstance(res.data, bool) else -1
    except Exception as e:
        logger.error(f"DENTAL_QUOTA_RESERVE_FAILED clinic={clinic.get('id')} kind={kind}: {e}")
        return False
    if used < 0:
        await _warn_threshold(clinic, kind, month, limit, full=True)
        return False
    if limit and used * 100 >= limit * QUOTA_WARN_PERCENT:
        await _warn_threshold(clinic, kind, month, limit, full=used >= limit)
    return True


async def release_quota(clinic: dict, kind: str) -> None:
    try:
        await sb(supabase.rpc("release_message_quota", {
            "p_clinic_id": clinic["id"], "p_month": current_month(), "p_kind": kind,
        }))
    except Exception as e:
        logger.error(f"DENTAL_QUOTA_RELEASE_FAILED clinic={clinic.get('id')} kind={kind}: {e}")


async def _warn_threshold(clinic: dict, kind: str, month: str, limit: int, full: bool) -> None:
    """Bell notification at 90 % and at 100 %, once each per kind per month.
    The conditional UPDATE is the exactly-once claim across workers."""
    flag = "warned_100" if full else "warned_90"
    try:
        claim = await sb(
            supabase.table("clinic_message_quota_usage").update({flag: True})
            .eq("clinic_id", clinic["id"]).eq("period_month", month).eq("kind", kind).eq(flag, False)
        )
        if not claim.data:
            return
        label = QUOTA_LABELS[kind]
        title = (f"Monthly limit reached: {label}" if full
                 else f"{QUOTA_WARN_PERCENT}% of monthly limit used: {label}")
        message = (
            f"Your clinic has used all {limit:,} {label.lower()} for this month. These messages are paused "
            "until next month. Please contact Kriya (Data & Support > Messages to Kriya) to increase the limit."
            if full else
            f"Your clinic has used {QUOTA_WARN_PERCENT}% of its {limit:,} {label.lower()} for this month. "
            "Please contact Kriya (Data & Support > Messages to Kriya) if you need a higher limit."
        )
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("admin_notifications").insert({
            "clinic_id": clinic["id"], "admin_id": None, "title": title[:255],
            "message": message, "is_read": False,
        }))
    except Exception as e:
        logger.warning(f"Dental quota warning failed clinic={clinic.get('id')} kind={kind}: {e}")


async def usage(clinic_id: str, config: Optional[dict], month: Optional[str] = None) -> dict:
    month = month or current_month()
    res = await sb(supabase.table("clinic_message_quota_usage").select("kind, used")
                   .eq("clinic_id", _scope(clinic_id)).eq("period_month", month))
    used = {r["kind"]: r["used"] for r in (res.data or [])}
    limits = message_limits(config)
    return {"month": month,
            "kinds": {k: {"label": QUOTA_LABELS[k], **quota_level(used.get(k, 0), limits[k])}
                      for k in QUOTA_KINDS}}


# ── Sending ────────────────────────────────────────────────────────────────


async def _send(clinic: dict, kind: str, phone: str, template_key: str, components: list,
                source: str, fallback: Optional[tuple] = None) -> bool:
    """Reserve quota, send the dental template, fall back to an already-approved
    generic template when given, give the quota back if nothing was sent."""
    from app.services.whatsapp import whatsapp_service

    if not phone:
        return False
    if not await reserve_quota(clinic, kind):
        return False
    ok = False
    try:
        ok = await whatsapp_service.send_template(
            clinic, phone, DENTAL_TEMPLATES[template_key]["name"], components=components, _source=source)
        if not ok and fallback:
            ok = await whatsapp_service.send_template(
                clinic, phone, fallback[0], components=fallback[1], _source=source)
    except Exception as e:
        logger.error(f"Dental send failed ({template_key}) clinic={clinic.get('id')}: {e}")
        ok = False
    if not ok:
        await release_quota(clinic, kind)
    return bool(ok)


async def send_patient_confirmation(clinic: dict, plan: dict, appt: dict) -> bool:
    from app.templates.whatsapp_templates import TEMPLATES

    if not await patient_messageable(clinic["id"], plan):
        return False
    n, total = sitting_label(appt, plan)
    hospital = clinic.get("name") or "the clinic"
    doctor = doctor_title(appt.get("doctor_name"))
    comps = _body(first_name(plan["patient_name"]), plan["treatment_name"], hospital, n, total,
                  doctor, fmt_day(appt["appointment_date"]), fmt_time(appt["appointment_time"]))
    fallback = (TEMPLATES["appointment_confirmation"]["name"],
                TEMPLATES["appointment_confirmation"]["components_builder"](
                    flat(doctor), flat(plan["treatment_name"]), fmt_day(appt["appointment_date"]),
                    fmt_time(appt["appointment_time"]), flat(hospital)))
    return await _send(clinic, "patient", plan["patient_phone"], "confirmation", comps,
                       "dental_patient", fallback)


async def send_patient_reminder(clinic: dict, plan: dict, appt: dict) -> bool:
    """No fallback here on purpose: when this fails the generic 09:00 reminder
    job (which checks reminder_24h_sent) is the fallback."""
    if not await patient_messageable(clinic["id"], plan):
        return False
    n, total = sitting_label(appt, plan)
    comps = _body(clinic.get("name") or "your clinic", first_name(plan["patient_name"]),
                  plan["treatment_name"], n, total, doctor_title(appt.get("doctor_name")),
                  fmt_day(appt["appointment_date"]), fmt_time(appt["appointment_time"]))
    return await _send(clinic, "patient", plan["patient_phone"], "reminder", comps, "dental_patient")


async def send_doctor_sitting(clinic: dict, plan: dict, appt: dict, doctor: Optional[dict]) -> bool:
    phone = (doctor or {}).get("whatsapp_phone")
    if not phone or not plan.get("notify_doctor") or not doctor:
        return False
    n, total = sitting_label(appt, plan)
    comps = _body(doctor_title(doctor.get("name")), plan["patient_name"], plan["treatment_name"], n, total,
                  fmt_day(appt["appointment_date"]), fmt_time(appt["appointment_time"]))
    return await _send(clinic, "doctor", phone, "doctor_sitting", comps, "dental_doctor")


async def send_review_request(clinic: dict, plan: dict, appt: dict) -> bool:
    comps = _body(clinic.get("name") or "our clinic", first_name(plan["patient_name"]), plan["treatment_name"])
    for idx, (score, _label) in enumerate(REVIEW_OPTIONS):
        comps.append({"type": "button", "sub_type": "quick_reply", "index": str(idx),
                      "parameters": [{"type": "payload", "payload": review_payload(appt["id"], score)}]})
    return await _send(clinic, "review", plan["patient_phone"], "review", comps, "dental_review")


# ── Plan / sitting operations ──────────────────────────────────────────────


async def doctor_branch_session(doctor: dict, branch_id: Optional[str]) -> Optional[str]:
    """The session ('morning' | 'evening' | 'both') this doctor works at the
    branch, or None when no branch applies. A doctor with no branch rows at all
    works everywhere (single-location clinics). A doctor assigned elsewhere but
    not here raises: a branch plan's sitting must be with a dentist of that branch.

    doctor_branches has no clinic_id; the doctor was already resolved under a
    clinic-scoped query (get_doctor), which is what scopes this lookup.
    """
    if not branch_id:
        return None
    res = await sb(supabase.table("doctor_branches").select("branch_id, session")
                   .eq("doctor_id", doctor["id"]))
    rows = res.data or []
    if not rows:
        return None
    match = next((r for r in rows if str(r.get("branch_id")) == str(branch_id)), None)
    if not match:
        raise DentalError("This doctor does not work at this plan's branch. Choose a doctor of that branch.")
    return match.get("session") or "both"


async def available_slots(clinic_id: str, doctor: dict, day: str,
                          branch_id: Optional[str] = None) -> tuple:
    from app.database import get_available_slots

    session = await doctor_branch_session(doctor, branch_id)
    if session in ("morning", "evening"):
        return await get_available_slots(_scope(clinic_id), doctor["name"], day,
                                         branch_id=branch_id, branch_session=session)
    return await get_available_slots(_scope(clinic_id), doctor["name"], day)


async def schedule_sitting(clinic: dict, plan: dict, doctor: dict, day: str, time_str: str,
                           sitting_number: Optional[int] = None) -> dict:
    """Book one sitting for a plan with a doctor who is actually free then.

    Raises DentalError for anything the front desk can fix (taken slot, doctor
    on leave, plan closed). The slot-uniqueness index stays the final word.
    """
    from app.database import book_appointment

    clinic_id = _scope(clinic["id"])
    if plan.get("status") != "active":
        raise DentalError("This treatment plan is closed. Reopen it before booking another sitting.")
    if not doctor.get("is_active", True):
        raise DentalError("This doctor is inactive.")
    try:
        day_d = date.fromisoformat(day)
    except (TypeError, ValueError):
        raise DentalError("Choose a valid date.")
    if day_d < ist_now().date():
        raise DentalError("A sitting cannot be booked in the past.")

    slots, reason = await available_slots(clinic_id, doctor, day, plan.get("branch_id"))
    wanted = (time_str or "")[:5]
    if wanted not in {str(s)[:5] for s in (slots or [])}:
        reasons = {
            "hospital_closed": "The clinic is closed on that day (holiday).",
            "doctor_on_leave": "This doctor is on leave that day.",
            "doctor_off_day": "This doctor does not work on that day.",
            "doctor_not_found": "Doctor not found.",
        }
        raise DentalError(reasons.get(reason or "", "That time is not free for this doctor. Pick another slot."))

    sittings = await plan_sittings(clinic_id, plan["id"])
    live_numbers = {s.get("sitting_number") for s in sittings if s.get("status") != "cancelled"}
    number = sitting_number or next((n for n in range(1, 31) if n not in live_numbers), None)
    if number is None or number > 30:
        raise DentalError("A plan can have at most 30 sittings.")
    if number in live_numbers:
        raise DentalError(f"Sitting {number} is already booked for this plan.")

    messageable = await patient_messageable(clinic_id, plan)
    data = {
        "patient_phone": plan["patient_phone"],
        "patient_name": plan["patient_name"],
        "doctor_id": doctor["id"],
        "doctor_name": doctor["name"],
        "department": doctor.get("department") or "Dental",
        "appointment_date": day,
        "appointment_time": wanted,
        "booking_type": "consultation",
        "status": "confirmed",
        "treatment_id": plan.get("treatment_id"),
        "treatment_name": plan["treatment_name"],
        "treatment_plan_id": plan["id"],
        "sitting_number": number,
        # The review replaces the generic post-visit follow-up for sittings.
        "followup_sent": True,
    }
    if plan.get("branch_id"):
        # Keeps the sitting visible to that branch's pinned staff on the
        # Appointments page, exactly like a branch booking made on WhatsApp.
        data["branch_id"] = plan["branch_id"]
        br = await sb(supabase.table("branches").select("name")
                      .eq("clinic_id", clinic_id).eq("id", plan["branch_id"]).limit(1))
        if br.data and br.data[0].get("name"):
            data["branch_name"] = br.data[0]["name"]
    if not messageable:
        # Nobody may message this patient: pre-set every reminder flag so no
        # job, dental or generic, ever picks the sitting up.
        data.update({"reminder_24h_sent": True, "reminder_2h_sent": True})

    result = await book_appointment(clinic_id, data)
    if not result.get("success"):
        if result.get("reason") == "slot_taken":
            raise DentalError("That slot was just taken, or this sitting number was just booked. Refresh and retry.")
        raise DentalError("Could not book the sitting. Please try again.")
    return result["appointment"]


async def link_existing_appointment(clinic_id: str, plan: dict, appointment_id: str) -> dict:
    """Make a patient's WhatsApp booking sitting 1 of a new plan."""
    res = await sb(supabase.table("appointments").select("*")
                   .eq("clinic_id", _scope(clinic_id)).eq("id", appointment_id).limit(1))
    appt = (res.data or [None])[0]
    if not appt:
        raise DentalError("Booking not found.")
    if appt.get("treatment_plan_id"):
        raise DentalError("That booking already belongs to a treatment plan.")
    if appt.get("booking_type") not in (None, "consultation") or appt.get("status") not in ("confirmed", "completed"):
        raise DentalError("Only a confirmed or completed consultation booking can start a plan.")
    upd = await sb(supabase.table("appointments")
                   .update({"treatment_plan_id": plan["id"], "sitting_number": 1, "followup_sent": True})
                   .eq("clinic_id", clinic_id).eq("id", appointment_id).is_("treatment_plan_id", "null"))
    if not upd.data:
        raise DentalError("That booking was just linked to another plan.")
    return upd.data[0]


async def complete_sitting(clinic_id: str, appt: dict, notes: Optional[str],
                           amount_paise: Optional[int]) -> dict:
    if appt.get("status") not in ("confirmed", "completed"):
        raise DentalError("Only a booked sitting can be marked done.")
    update: dict = {}
    if appt.get("status") == "confirmed":
        update.update({"status": "completed", "completed_at": datetime.now(timezone.utc).isoformat()})
    if notes is not None:
        update["sitting_notes"] = notes.strip()[:2000] or None
    if amount_paise is not None:
        update["amount_collected_paise"] = amount_paise
    if not update:
        return appt
    res = await sb(supabase.table("appointments").update(update)
                   .eq("clinic_id", _scope(clinic_id)).eq("id", appt["id"])
                   .in_("status", ["confirmed", "completed"]))
    if not res.data:
        raise DentalError("The sitting changed meanwhile. Refresh and try again.")
    return res.data[0]


async def close_plan_if_done(clinic_id: str, plan: dict) -> Optional[dict]:
    sittings = await plan_sittings(clinic_id, plan["id"])
    done = sum(1 for s in sittings if s.get("status") == "completed")
    if plan.get("status") == "active" and done >= int(plan.get("planned_sittings") or 0):
        now = datetime.now(timezone.utc).isoformat()
        res = await sb(supabase.table("dental_treatment_plans")
                       .update({"status": "completed", "completed_at": now, "updated_at": now})
                       .eq("clinic_id", _scope(clinic_id)).eq("id", plan["id"]).eq("status", "active"))
        return (res.data or [None])[0]
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled jobs (registered in app/services/scheduler.py)
# ═══════════════════════════════════════════════════════════════════════════


async def _dental_clinics() -> list:
    from app.services.tenant import dental_plans_enabled

    res = await sb(
        # unscoped: platform_sweep
        supabase.table("clinics").select("*").eq("plan", "dental").eq("is_active", True)
    )
    return [c for c in (res.data or []) if c.get("status") != "DELETED" and dental_plans_enabled(c)]


async def _plans_by_id(clinic_id: str, plan_ids: set) -> dict:
    if not plan_ids:
        return {}
    res = await sb(supabase.table("dental_treatment_plans").select("*")
                   .eq("clinic_id", clinic_id).in_("id", list(plan_ids)))
    return {p["id"]: p for p in (res.data or [])}


async def _sittings_on(clinic_id: str, day: str, only_unreminded: bool = False) -> list:
    q = (supabase.table("appointments").select("*").eq("clinic_id", clinic_id)
         .eq("appointment_date", day).eq("status", "confirmed")
         .not_.is_("treatment_plan_id", "null"))
    if only_unreminded:
        q = q.eq("reminder_24h_sent", False)
    return (await sb(q.limit(2000))).data or []


async def run_patient_reminders() -> dict:
    """08:30 IST: dental reminder for tomorrow's sittings. Sets reminder_24h_sent
    only on success, so the generic 09:00 reminder is the fallback."""
    from app.services.subscription import automated_outbound_allowed

    sent = skipped = 0
    tomorrow = (ist_now().date() + timedelta(days=1)).isoformat()
    for clinic in await _dental_clinics():
        if not automated_outbound_allowed(clinic):
            continue
        try:
            rows = await _sittings_on(clinic["id"], tomorrow, only_unreminded=True)
            plans = await _plans_by_id(clinic["id"], {r["treatment_plan_id"] for r in rows})
            for appt in rows:
                plan = plans.get(appt["treatment_plan_id"])
                if plan and plan.get("status") == "active" and await send_patient_reminder(clinic, plan, appt):
                    # unscoped: unique_row_key
                    await sb(supabase.table("appointments").update({"reminder_24h_sent": True})
                             .eq("id", appt["id"]).eq("reminder_24h_sent", False))
                    sent += 1
                else:
                    skipped += 1
        except Exception as e:
            logger.error(f"Dental patient reminders failed for clinic={clinic.get('id')}: {e}")
    logger.info(f"Dental patient reminders: sent={sent} skipped={skipped}")
    return {"sent": sent, "skipped": skipped}


async def run_doctor_digests() -> dict:
    """Evening: each dentist gets ONE message listing tomorrow's sittings.
    dental_doctor_digests' unique key makes it exactly-once across workers and
    reruns; a failed send removes the claim so the later run retries."""
    from app.services.subscription import automated_outbound_allowed

    sent = 0
    tomorrow_d = ist_now().date() + timedelta(days=1)
    tomorrow = tomorrow_d.isoformat()
    for clinic in await _dental_clinics():
        if not automated_outbound_allowed(clinic):
            continue
        if not dental_settings(clinic.get("config"))["dental_doctor_digest_enabled"]:
            continue
        try:
            rows = await _sittings_on(clinic["id"], tomorrow)
            plans = await _plans_by_id(clinic["id"], {r["treatment_plan_id"] for r in rows})
            by_doctor: dict = {}
            for appt in rows:
                plan = plans.get(appt["treatment_plan_id"])
                if plan and plan.get("notify_doctor") and appt.get("doctor_id"):
                    by_doctor.setdefault(appt["doctor_id"], []).append((appt, plan))
            for doctor_id, items in by_doctor.items():
                doctor = await get_doctor(clinic["id"], doctor_id)
                if not doctor or not doctor.get("whatsapp_phone"):
                    continue
                try:
                    # unscoped: insert_scoped_by_payload
                    claim = await sb(supabase.table("dental_doctor_digests").insert({
                        "clinic_id": clinic["id"], "doctor_id": doctor_id,
                        "digest_date": tomorrow, "sittings_count": len(items),
                    }))
                except Exception:
                    continue  # already sent (unique key) or claim failed: never double-send
                items.sort(key=lambda it: it[0].get("appointment_time") or "")
                lines = "; ".join(
                    f"{fmt_time(a['appointment_time'])} {p['patient_name']} - {p['treatment_name']} "
                    f"{a.get('sitting_number') or '?'}/{p.get('planned_sittings') or '?'}"
                    for a, p in items
                )
                comps = _body(doctor_title(doctor.get("name")), fmt_day(tomorrow_d),
                              clinic.get("name") or "the clinic")
                comps[0]["parameters"].append({"type": "text", "text": flat(lines, 900)})
                if await _send(clinic, "doctor", doctor["whatsapp_phone"], "doctor_schedule", comps,
                               "dental_doctor"):
                    sent += 1
                else:
                    claim_id = (claim.data or [{}])[0].get("id")
                    if claim_id:
                        await sb(supabase.table("dental_doctor_digests").delete()
                                 .eq("clinic_id", clinic["id"]).eq("id", claim_id))
        except Exception as e:
            logger.error(f"Dental doctor digests failed for clinic={clinic.get('id')}: {e}")
    logger.info(f"Dental doctor digests: sent={sent}")
    return {"sent": sent}


async def run_review_requests() -> dict:
    """Every 30 min, 09:00-21:00 IST: ask for a rating an hour after a sitting
    is completed (or the next morning for sittings closed out overnight)."""
    from app.services.consent import consent_service
    from app.services.subscription import automated_outbound_allowed

    now = ist_now()
    if not 9 <= now.hour < 21:
        return {"sent": 0, "skipped": 0}
    sent = skipped = 0
    newest = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    oldest = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    for clinic in await _dental_clinics():
        if not automated_outbound_allowed(clinic):
            continue
        if not dental_settings(clinic.get("config"))["dental_review_enabled"]:
            continue
        try:
            rows = (await sb(
                supabase.table("appointments").select("*").eq("clinic_id", clinic["id"])
                .eq("status", "completed").not_.is_("treatment_plan_id", "null")
                .is_("review_requested_at", "null")
                .lte("completed_at", newest).gte("completed_at", oldest).limit(500)
            )).data or []
            plans = await _plans_by_id(clinic["id"], {r["treatment_plan_id"] for r in rows})
            for appt in rows:
                # Claim first (compare-and-set): two workers can never both ask.
                # unscoped: unique_row_key
                claim = await sb(supabase.table("appointments")
                                 .update({"review_requested_at": datetime.now(timezone.utc).isoformat()})
                                 .eq("id", appt["id"]).is_("review_requested_at", "null"))
                if not claim.data:
                    continue
                plan = plans.get(appt["treatment_plan_id"])
                ok = (bool(plan)
                      and await patient_messageable(clinic["id"], plan)
                      and await consent_service.accepts_engagement(clinic["id"], plan["patient_phone"])
                      and await send_review_request(clinic, plan, appt))
                sent += int(bool(ok))
                skipped += int(not ok)
        except Exception as e:
            logger.error(f"Dental review requests failed for clinic={clinic.get('id')}: {e}")
    logger.info(f"Dental review requests: sent={sent} skipped={skipped}")
    return {"sent": sent, "skipped": skipped}


# ═══════════════════════════════════════════════════════════════════════════
# Inbound: the patient tapped a review button
# ═══════════════════════════════════════════════════════════════════════════


async def handle_review_reply(manager, clinic: dict, phone: str, payload: str) -> bool:
    """Record a rating. Returns True when the payload was a dental review
    (handled, whatever the outcome), False when it is not ours.

    The payload is only trusted after checking the sitting belongs to THIS
    clinic AND to THIS sender's phone, so a forged payload cannot rate someone
    else's visit.
    """
    parsed = parse_review_payload(payload)
    if not parsed:
        return False
    appointment_id, score = parsed
    try:
        appt = await get_sitting(clinic["id"], appointment_id)
        if not appt or appt.get("patient_phone") != phone:
            logger.warning(f"Dental review payload rejected for clinic={clinic.get('id')}")
            await manager.whatsapp.send_text(clinic, phone, "Thank you for your message.")
            return True
        if appt.get("review_rating"):
            await manager.whatsapp.send_text(clinic, phone, "Thank you! We already have your feedback for this visit. 🙏")
            return True
        await sb(supabase.table("appointments")
                 .update({"review_rating": score, "review_received_at": datetime.now(timezone.utc).isoformat()})
                 .eq("clinic_id", clinic["id"]).eq("id", appointment_id).is_("review_rating", "null"))
        if score >= 2:
            text = f"Thank you for your feedback, {first_name(appt.get('patient_name'))}! 🙏"
            link = (dental_settings(clinic.get("config")).get("dental_google_review_link") or "").strip()
            if score == 3 and link:
                text += f"\n\nIt would mean a lot to us if you shared your experience on Google:\n{link}"
            await manager.whatsapp.send_text(clinic, phone, text)
        else:
            await manager.whatsapp.send_text(
                clinic, phone,
                "We're sorry your visit wasn't as good as it should have been. "
                "Our team will contact you shortly to make it right.")
            # unscoped: insert_scoped_by_payload
            await sb(supabase.table("admin_notifications").insert({
                "clinic_id": clinic["id"], "admin_id": None,
                "title": f"Patient feedback needs attention: {appt.get('patient_name') or 'Patient'}"[:255],
                "message": (f"{appt.get('patient_name') or 'A patient'} rated sitting "
                            f"{appt.get('sitting_number') or '?'} of {appt.get('treatment_name') or 'treatment'} "
                            f"with {appt.get('doctor_name') or 'the doctor'} as 'Needs improvement'. "
                            f"Please call them."),
                "is_read": False,
            }))
    except Exception as e:
        logger.error(f"Dental review reply failed for clinic={clinic.get('id')}: {e}")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Meta template submission (owner panel)
# ═══════════════════════════════════════════════════════════════════════════


def _meta_account(clinic: dict) -> tuple:
    cfg = clinic.get("config") or {}
    token, waba = cfg.get("meta_access_token"), cfg.get("meta_waba_id")
    if not token or not waba:
        raise DentalError("This clinic has no Meta access token / WhatsApp Business Account id configured.")
    return str(token), str(waba)


def _templates_url(waba: str) -> str:
    from app.config import settings

    return f"https://graph.facebook.com/{settings.whatsapp_api_version}/{waba}/message_templates"


async def submit_templates(clinic: dict) -> list:
    """Create the five dental templates in the clinic's own WhatsApp Business
    Account. Meta reviews them (usually minutes, sometimes a day). Re-submitting
    an existing name is reported as 'exists', not treated as a failure."""
    import httpx

    token, waba = _meta_account(clinic)
    url = _templates_url(waba)
    results = []
    async with httpx.AsyncClient(timeout=20) as client:
        for key in DENTAL_TEMPLATES:
            body = meta_template_payload(key)
            try:
                r = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
                data = r.json() if r.content else {}
                if r.status_code < 300:
                    results.append({"name": body["name"], "result": "submitted", "status": data.get("status")})
                else:
                    err = data.get("error") or {}
                    msg = err.get("error_user_msg") or err.get("message") or f"HTTP {r.status_code}"
                    exists = "already exists" in str(msg).lower() or err.get("error_subcode") == 2388024
                    results.append({"name": body["name"], "result": "exists" if exists else "error",
                                    "detail": str(msg)[:300]})
            except Exception as e:
                results.append({"name": body["name"], "result": "error", "detail": f"Network error: {e}"[:300]})
    return results


async def template_statuses(clinic: dict) -> list:
    import httpx

    token, waba = _meta_account(clinic)
    wanted = {t["name"] for t in DENTAL_TEMPLATES.values()}
    found: dict = {}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(_templates_url(waba), params={"fields": "name,status,category,language", "limit": 200},
                             headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 300:
            err = (r.json().get("error") or {}) if r.content else {}
            raise DentalError(f"Meta refused the status check: {err.get('message') or r.status_code}")
        for t in r.json().get("data", []):
            if t.get("name") in wanted:
                found[t["name"]] = t
    return [{"name": n, "status": (found.get(n) or {}).get("status", "NOT_SUBMITTED"),
             "category": (found.get(n) or {}).get("category")} for n in sorted(wanted)]
