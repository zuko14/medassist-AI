"""Continuous Care — cases, coordinator queue, doctor inbox and one-tap actions (migration 148; WORKFLOWS W7–W8).

One open case per (person, enrollment, reason) (ADR-CC-14) with a working-hours SLA; every transition writes an
append-only case event; concurrent edits are compare-and-set on updated_at (409 for the loser). Actions never carry a
medicine or dose (ADR-CC-04, Pydantic extra="forbid" + a DB CHECK); visits are booked only on slots that
get_available_slots offers (holidays, leave, sessions — VERIFICATION D16) through book_appointment, pay-at-counter
'confirmed', and CC itself sends cc_visit_booked_v1 (D5). Zero-LLM.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services.cc import _now, _rows, actor_id, actor_name, get_settings, raise_for
from app.utils.helpers import today_ist

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
OPEN = ["open", "acknowledged", "awaiting_doctor", "actioned"]
CLINICAL_ACTIONS = ("book_review_visit", "order_lab", "book_home_sample", "recheck_in_days", "continue_plan", "refer",
                    "end")
COORDINATOR_ACTIONS = ("call_patient_now", "pause")


def working_deadline(start: datetime, hours: int, reply_hours: dict) -> datetime:
    """`hours` working hours after `start` inside the clinic's reply hours (default Mon–Sat 09:00–18:00 IST)."""
    days = reply_hours.get("days") or "Mon-Sat"
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    if "-" in days and "," not in days:
        a, b = days.split("-")
        allowed = set(names[names.index(a):names.index(b) + 1])
    else:
        allowed = {d.strip() for d in days.split(",")}
    h0, m0 = map(int, (reply_hours.get("from") or "09:00").split(":"))
    h1, m1 = map(int, (reply_hours.get("to") or "18:00").split(":"))
    t = start.astimezone(IST)
    left = timedelta(hours=hours)
    for _ in range(400):
        day_start = t.replace(hour=h0, minute=m0, second=0, microsecond=0)
        day_end = t.replace(hour=h1, minute=m1, second=0, microsecond=0)
        if t.strftime("%a") in allowed and t < day_end:
            t = max(t, day_start)
            room = day_end - t
            if left <= room:
                return t + left
            left -= room
        t = (t + timedelta(days=1)).replace(hour=h0, minute=m0, second=0, microsecond=0)
    return t


async def event(clinic_id: str, case_id: str, kind: str, body: Optional[str] = None, user=None) -> None:
    # unscoped: insert_scoped_by_payload
    await sb(supabase.table("cc_case_events").insert({
        "clinic_id": clinic_id, "case_id": case_id, "kind": kind, "body": (body or "")[:1000] or None,
        "actor_id": actor_id(user) if user else None, "actor_name": actor_name(user) if user else "system"}))


async def open_case(clinic: dict, *, patient_id: str, reason: str, tier: str, enrollment_id: Optional[str] = None,
                    family_member_id: Optional[str] = None, evaluation_id: Optional[str] = None,
                    radar_row_id: Optional[str] = None, branch_id: Optional[str] = None, summary: Optional[dict] = None,
                    urgent_alert: bool = True) -> Optional[str]:
    """Open a case, or — when one is already open for this reason — record that the reason was seen again and raise
    its tier if needed (never a duplicate). Urgent cases alert the coordinators in-app at once (W13)."""
    cid = clinic["id"]
    s = get_settings(clinic)
    tier = tier if tier in ("urgent", "watch", "routine") else "watch"
    hours = s["sla_hours"]["urgent" if tier == "urgent" else "watch" if tier == "watch" else "overdue"]
    due = working_deadline(datetime.now(timezone.utc), int(hours), s["reply_hours"])
    q = scoped_query("cc_cases", cid, "id, tier, status").eq("patient_id", patient_id).eq("reason", reason).in_("status", OPEN)
    q = q.eq("enrollment_id", enrollment_id) if enrollment_id else q.is_("enrollment_id", "null")
    cur = _rows(await sb(q.limit(1)))
    if cur:
        c = cur[0]
        rank = {"routine": 0, "watch": 1, "urgent": 2}
        if rank[tier] > rank[c["tier"]]:
            await sb(supabase.table("cc_cases").update({"tier": tier, "due_by": due.isoformat(), "evaluation_id": evaluation_id,
                                                        "summary": summary or {}, "updated_at": _now()})
                     .eq("clinic_id", cid).eq("id", c["id"]))
        await event(cid, c["id"], "reason_seen_again", None)
        return c["id"]
    try:
        # unscoped: insert_scoped_by_payload
        row = _rows(await sb(supabase.table("cc_cases").insert({
            "clinic_id": cid, "patient_id": patient_id, "family_member_id": family_member_id, "enrollment_id": enrollment_id,
            "branch_id": branch_id, "reason": reason[:60], "tier": tier, "due_by": due.isoformat(),
            "evaluation_id": evaluation_id, "radar_row_id": radar_row_id, "summary": summary or {}})))[0]
    except Exception as e:
        if "uq_cc_case_open" in str(e):                    # a parallel trigger opened it first
            again = _rows(await sb(q.limit(1)))
            return again[0]["id"] if again else None
        raise
    await event(cid, row["id"], "opened", None)
    if tier == "urgent" and urgent_alert:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("admin_notifications").insert({
            "clinic_id": cid, "admin_id": None, "is_read": False, "title": "[Care] Urgent case",
            "message": f"An urgent care case was opened ({reason.split(':')[-1].replace('_', ' ')}). "
                       "Open the Care queue — it is at the top."}))
    return row["id"]


async def get_case(clinic_id: str, case_id: str, branch_id: Optional[str] = None) -> dict:
    rows = _rows(await sb(scoped_query("cc_cases", clinic_id).eq("id", case_id).limit(1)))
    if not rows or (branch_id and rows[0].get("branch_id") and str(rows[0]["branch_id"]) != str(branch_id)):
        raise HTTPException(status_code=404, detail="Case not found.")
    return rows[0]


async def _cas(clinic_id: str, case: dict, expected: Optional[str], patch: dict) -> dict:
    if expected is not None and str(case["updated_at"]) != str(expected):
        raise HTTPException(status_code=409, detail={"code": "cc_conflict", "message": "Someone else changed this case. "
                                                                                      "It has been reloaded."})
    done = _rows(await sb(supabase.table("cc_cases").update({**patch, "updated_at": _now()}).eq("clinic_id", clinic_id)
                          .eq("id", case["id"]).eq("updated_at", case["updated_at"])))
    if not done:
        raise HTTPException(status_code=409, detail={"code": "cc_conflict", "message": "Someone else changed this case. "
                                                                                      "It has been reloaded."})
    return done[0]


async def transition(clinic_id: str, case_id: str, action: str, user, *, expected_updated_at: Optional[str] = None,
                     note: Optional[str] = None, closed_reason: Optional[str] = None,
                     branch_id: Optional[str] = None) -> dict:
    c = await get_case(clinic_id, case_id, branch_id)
    if c["status"] in ("closed", "cancelled") and action != "note":
        raise HTTPException(status_code=409, detail="This case is already closed.")
    if action == "acknowledge":
        if c["status"] != "open":
            raise HTTPException(status_code=409, detail="Only an open case can be acknowledged.")
        out = await _cas(clinic_id, c, expected_updated_at, {"status": "acknowledged", "assigned_to": actor_id(user),
                                                              "assigned_to_name": actor_name(user)})
        await event(clinic_id, case_id, "assigned", None, user)
    elif action == "assign":
        out = await _cas(clinic_id, c, expected_updated_at, {"assigned_to": actor_id(user), "assigned_to_name": actor_name(user)})
        await event(clinic_id, case_id, "assigned", None, user)
    elif action == "request_doctor":
        if c["status"] not in ("open", "acknowledged"):
            raise HTTPException(status_code=409, detail="This case cannot be sent to the doctor now.")
        out = await _cas(clinic_id, c, expected_updated_at, {"status": "awaiting_doctor"})
        await event(clinic_id, case_id, "status_change", "Sent to the doctor", user)
    elif action == "escalate":
        out = await _cas(clinic_id, c, expected_updated_at, {"escalated_at": _now()})
        await event(clinic_id, case_id, "escalated", note, user)
    elif action == "close":
        if closed_reason not in ("patient_declined", "unreachable", "transferred", "duplicate", "clinical_decision"):
            # 'verified' only from cc_verify evidence (loophole X2)
            raise HTTPException(status_code=422, detail="Close with a reason: patient declined, unreachable, transferred, "
                                                        "duplicate or clinical decision.")
        out = await _cas(clinic_id, c, expected_updated_at, {"status": "closed", "closed_reason": closed_reason,
                                                              "closed_at": _now()})
        await event(clinic_id, case_id, "status_change", f"Closed: {closed_reason.replace('_', ' ')}", user)
    elif action == "note":
        if not (note or "").strip():
            raise HTTPException(status_code=422, detail="Write the note.")
        await event(clinic_id, case_id, "note", note.strip(), user)
        out = c
    else:
        raise HTTPException(status_code=404, detail="Unknown case action.")
    return out


async def list_cases(clinic_id: str, status: Optional[str], tier: Optional[str], assigned_to: Optional[str],
                     branch_id: Optional[str], limit: int = 200) -> list:
    from app.utils.validators import mask_phone
    q = scoped_query("cc_cases", clinic_id)
    q = q.eq("status", status) if status else q.in_("status", ["open", "acknowledged", "awaiting_doctor", "actioned"])
    if tier:
        q = q.eq("tier", tier)
    if assigned_to:
        q = q.eq("assigned_to", assigned_to)
    if branch_id:
        q = q.eq("branch_id", branch_id)
    rows = _rows(await sb(q.order("due_by").limit(limit)))
    pats = {}
    if rows:
        pats = {p["id"]: p for p in _rows(await sb(scoped_query("patients", clinic_id, "id, name, phone, mrn, age_years, gender")
                                                   .in_("id", list({r["patient_id"] for r in rows}))))}
    now = datetime.now(timezone.utc)
    rank = {"urgent": 0, "watch": 1, "routine": 2}
    out = []
    for r in rows:
        p = pats.get(r["patient_id"]) or {}
        due = datetime.fromisoformat(str(r["due_by"]).replace("Z", "+00:00"))
        out.append({**r, "patient_name": p.get("name"), "mrn": p.get("mrn"), "age_years": p.get("age_years"),
                    "gender": p.get("gender"), "phone_masked": mask_phone(p.get("phone") or ""),
                    "sla_breached": due < now, "sla_minutes_left": int((due - now).total_seconds() // 60)})
    return sorted(out, key=lambda r: (rank.get(r["tier"], 3), r["due_by"]))


async def case_detail(clinic_id: str, case_id: str, branch_id: Optional[str] = None) -> dict:
    c = await get_case(clinic_id, case_id, branch_id)
    events = _rows(await sb(scoped_query("cc_case_events", clinic_id).eq("case_id", case_id).order("created_at")))
    actions = _rows(await sb(scoped_query("cc_actions", clinic_id).eq("case_id", case_id).order("created_at")))
    return {**c, "events": events, "actions": actions}


async def sla_sweep(clinic: dict) -> int:
    """Every 15 min: breached open cases are escalated once (CC_ADMIN sees them red; in-app notification)."""
    cid = clinic["id"]
    n = 0
    for c in _rows(await sb(scoped_query("cc_cases", cid, "id, tier, reason, due_by, escalated_at, updated_at")
                            .in_("status", ["open", "acknowledged", "awaiting_doctor"]).is_("escalated_at", "null")
                            .lt("due_by", _now()).limit(500))):
        done = _rows(await sb(supabase.table("cc_cases").update({"escalated_at": _now()}).eq("clinic_id", cid)
                              .eq("id", c["id"]).is_("escalated_at", "null")))
        if done:
            await event(cid, c["id"], "escalated", "SLA passed")
            n += 1
    if n:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("admin_notifications").insert({
            "clinic_id": cid, "admin_id": None, "is_read": False, "title": "[Care] Cases past their response time",
            "message": f"{n} care case(s) passed their SLA. Open the Care queue."}))
    return n


# ── doctor inbox + one-tap actions (W8) ──────────────────────────────────────

async def inbox(clinic_id: str, doctor_id: Optional[str], branch_id: Optional[str]) -> list:
    rows = await list_cases(clinic_id, "awaiting_doctor", None, None, branch_id)
    if not rows:
        return []
    enr = {e["id"]: e for e in _rows(await sb(scoped_query("cc_enrollments", clinic_id, "id, primary_doctor_id, protocol_code")
                                              .in_("id", [r["enrollment_id"] for r in rows if r.get("enrollment_id")]
                                                   or ["00000000-0000-0000-0000-000000000000"])))}
    for r in rows:
        e = enr.get(r.get("enrollment_id")) or {}
        r["protocol_code"] = e.get("protocol_code")
        r["mine"] = bool(doctor_id) and str(e.get("primary_doctor_id")) == str(doctor_id)
    return sorted(rows, key=lambda r: (not r["mine"], {"urgent": 0, "watch": 1}.get(r["tier"], 2), r["due_by"]))


async def first_free_slot(clinic_id: str, doctor: dict, start: date, days: int = 14) -> Optional[tuple]:
    from app.database import get_available_slots
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        slots, err = await get_available_slots(clinic_id, doctor["name"], d)
        if not err and slots:
            return d, slots[0]
    return None


async def take_action(clinic: dict, case_id: str, code: str, params: dict, user, *, role: str,
                      idempotency_key: Optional[str], branch_id: Optional[str] = None) -> dict:
    cid = clinic["id"]
    c = await get_case(cid, case_id, branch_id)
    if c["status"] in ("closed", "cancelled"):
        raise HTTPException(status_code=409, detail="This case is closed.")
    if idempotency_key:
        prior = _rows(await sb(scoped_query("cc_actions", cid).eq("idempotency_key", idempotency_key).limit(1)))
        if prior:
            return prior[0]
    if role != "doctor" and code in CLINICAL_ACTIONS:
        raise HTTPException(status_code=403, detail="Only the doctor can take this action.")
    enr = None
    if c.get("enrollment_id"):
        enr = _rows(await sb(scoped_query("cc_enrollments", cid).eq("id", c["enrollment_id"]).limit(1)))
        enr = enr[0] if enr else None
    row = {"clinic_id": cid, "case_id": case_id, "action_code": code, "params": params or {}, "decided_by": actor_id(user),
           "decided_by_name": actor_name(user), "decider_role": role, "idempotency_key": idempotency_key}
    appt = None
    if code == "book_review_visit":
        appt = await _book_review(clinic, c, enr, params)
        row.update(appointment_id=appt["id"], status="booked", due_on=str(appt["appointment_date"])[:10])
    elif code == "order_lab":
        row["due_on"] = (today_ist() + timedelta(days=int(params.get("within_days") or 7))).isoformat()
        row["status"] = "created"
    elif code == "recheck_in_days":
        row["due_on"] = (today_ist() + timedelta(days=int(params.get("days") or 3))).isoformat()
    elif code == "call_patient_now" and enr and enr.get("channel_pref") in ("voice", "both"):
        from app.services import cc_channels
        job = await cc_channels.enqueue_voice(clinic, enr, reason="case")
        if job:
            row.update(voice_job_id=job, status="booked", due_on=today_ist().isoformat())
    try:
        # unscoped: insert_scoped_by_payload
        act = _rows(await sb(supabase.table("cc_actions").insert(row)))[0]
    except Exception as e:
        if "uq_cc_actions_idem" in str(e):
            return _rows(await sb(scoped_query("cc_actions", cid).eq("idempotency_key", idempotency_key).limit(1)))[0]
        raise_for(e)
    new_status = "actioned" if code not in ("continue_plan",) else "closed"
    patch = {"status": new_status}
    if new_status == "closed":
        patch.update(closed_reason="clinical_decision", closed_at=_now())
    await sb(supabase.table("cc_cases").update({**patch, "updated_at": _now()}).eq("clinic_id", cid).eq("id", case_id))
    await event(cid, case_id, "doctor_decision" if role == "doctor" else "status_change",
                code.replace("_", " ") + (f" — {appt['appointment_date']} {str(appt['appointment_time'])[:5]}" if appt else ""),
                user)
    if appt and enr:
        from app.services import cc_channels
        await cc_channels.send_visit_booked(clinic, enr, appt)
    if code in ("pause", "end") and enr:
        from app.services import cc as core
        await core.set_status(cid, enr["id"], "paused" if code == "pause" else "ended_by_clinic",
                              params.get("reason") or "ended by the care team")
    return act


async def _book_review(clinic: dict, case: dict, enr: Optional[dict], params: dict) -> dict:
    """The slot must be one get_available_slots offers (holiday, leave, sessions, taken) — never forced."""
    from app.database import book_appointment, get_available_slots
    cid = clinic["id"]
    doctor_id = params.get("doctor_id") or (enr or {}).get("primary_doctor_id")
    doc = _rows(await sb(scoped_query("doctors", cid, "id, name, department, is_active").eq("id", doctor_id).limit(1)))
    if not doc or doc[0].get("is_active") is False:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    doc = doc[0]
    if params.get("date") and params.get("time"):
        slots, err = await get_available_slots(cid, doc["name"], params["date"])
        if err or params["time"] not in slots:
            raise HTTPException(status_code=409, detail={"code": "slot_unavailable", "message":
                                "That slot is not free (holiday, leave, session or already booked). Pick another."})
        day, slot = params["date"], params["time"]
    else:
        free = await first_free_slot(cid, doc, today_ist() + timedelta(days=1))
        if not free:
            raise HTTPException(status_code=409, detail="No free slot in the next 14 days for this doctor.")
        day, slot = free
    pat = _rows(await sb(scoped_query("patients", cid, "id, name, phone").eq("id", case["patient_id"]).limit(1)))[0]
    res = await book_appointment(cid, {
        "patient_id": pat["id"], "patient_phone": pat["phone"], "patient_name": pat.get("name") or "Patient",
        "family_member_id": case.get("family_member_id"), "doctor_id": doc["id"], "doctor_name": doc["name"],
        "department": doc.get("department"), "appointment_date": day, "appointment_time": slot, "status": "confirmed",
        "booking_type": "consultation", "booking_channel": "front_desk", "is_walk_in": False,
        "notes": "Booked by the care team (Continuous Care)"})
    if not res.get("success"):
        raise HTTPException(status_code=409, detail={"code": "slot_unavailable", "message":
                            "That slot was just taken. Pick another."})
    return res["appointment"]
