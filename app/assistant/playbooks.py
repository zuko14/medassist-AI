"""Kriya Ketu playbooks (playbooks-spec.md): fixed, reviewed sequences over the existing /admin
endpoints. Zero tokens. PB-01 doctor leave (KK-P4-02).

Every read goes through `read` (the executor, as the caller); every write is one Step with a
read-back verify. The bookings a leave hits are chosen by database.leave_query_match() +
leave_affected(), the selection the 08:00 sweep and create_leave's immediate cancellation use.

PB-01 step 6 (appointment.cancel_bulk) is not built: since KK-P0-01, POST /admin/leaves itself
cancels, refunds and notifies today's and tomorrow's bookings (owner decision 2026-10-14). Such a
leave instead needs an explicit tick that names the number of bookings.

KK-P4-05 front-desk writes (one record each) live at the end: the record is found with the page's
own list endpoint, and anything but exactly one match is refused with the matches listed.
"""

import difflib
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from app import database
from app.assistant import present, resolve
from app.assistant.errors import EmptyPlan, PlanError
from app.voice.dates import IST

MAX_LEAVE_DAYS = 31
MAX_PREVIEW_BOOKINGS = 1000   # 10 pages of the Appointments list
PAGE = 100                    # GET /admin/appointments caps limit at 100
LEAVE_LABEL = {"full": "full day", "half_morning": "morning only", "half_evening": "evening only"}


def _now() -> datetime:
    return datetime.now(IST)


def _call(*a, **k):
    from app.assistant.executor import Call  # lazy: the executor imports the registry, which imports this
    return Call(*a, **k)


def _step(*a, **k):
    from app.assistant.executor import Step
    return Step(*a, **k)


def _span(params: dict) -> list:
    start = date.fromisoformat(params["date"])
    end = date.fromisoformat(params.get("date_end") or params["date"])
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _label(days: list) -> str:
    if len(days) == 1:
        return days[0].strftime("%d %b")
    return f"{days[0].strftime('%d %b')} to {days[-1].strftime('%d %b')}"


def _dates(days: list) -> str:
    return ", ".join(d.strftime("%d %b") for d in days)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


async def _get(read, path: str, query: dict = None):
    out = await read(_call("GET", path, query or {}))
    if out.status != "ok":
        raise RuntimeError(f"read {path} failed")  # the router answers with its fixed "unavailable" text
    return out.data


async def _bookings(read, doctor: str, first: date, last: date) -> list:
    """Confirmed bookings in the range, every page (the list's search narrows to the doctor)."""
    rows, offset = [], 0
    while True:
        data = await _get(read, "/admin/appointments", {
            "date_basis": "visit", "date_from": first.isoformat(), "date_to": last.isoformat(),
            "status": "confirmed", "q": doctor, "limit": PAGE, "offset": offset})
        page = data.get("appointments") or [] if isinstance(data, dict) else []
        rows += [a for a in page if isinstance(a, dict)]
        offset += len(page)
        if not page or offset >= int(data.get("total") or 0):
            return rows
        if offset >= MAX_PREVIEW_BOOKINGS:
            raise PlanError("too many bookings to preview")


def _doctor_row(doctors, name: str):
    return next((d for d in doctors if isinstance(d, dict) and d.get("name") == name), None) \
        if isinstance(doctors, list) else None


def _hit(rows: list, doctor: str, days: list, leave_type: str, doc, now: datetime) -> list:
    out = []
    for d in days:
        leave = {"doctor_name": doctor, "leave_date": d.isoformat(), "leave_type": leave_type}
        out += database.leave_affected([a for a in rows if database.leave_query_match(a, leave)], leave, doc, now)
    return out


async def _existing(read, doctor: str) -> dict:
    rows = await _get(read, "/admin/leaves", {"doctor": doctor})
    by_date: dict = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("leave_date"):
            by_date.setdefault(str(r["leave_date"])[:10], []).append(r)
    return by_date


def _booking_rows(hit: list) -> list:
    return [[present.fmt(a.get("appointment_date"), "date"), present.fmt(a.get("appointment_time")),
             present.fmt(a.get("patient_name")), present.fmt(a.get("booking_ref"))] for a in hit[:25]]


# ── doctor_leave.create (R2) ─────────────────────────────────────────────────

async def leave_create_preview(read, params: dict) -> dict:
    days = _span(params)
    now = _now()
    if days[0] < now.date():  # a literally written past date (D-29) is never added
        raise EmptyPlan(f"{days[0].strftime('%d %b %Y')} is in the past. Leave can only be added from today on.")
    if len(days) > MAX_LEAVE_DAYS:
        return {"affected": len(days)}  # build_plan refuses it (too_many) before any read
    doctor, leave_type = params["doctor"], params.get("leave_type") or "full"
    existing = await _existing(read, doctor)
    want = database.leave_blocked_sessions([{"leave_type": leave_type}])
    skipped = [d for d in days if want <= database.leave_blocked_sessions(existing.get(d.isoformat(), []))]
    todo = [d for d in days if d not in skipped]
    if not todo:
        raise EmptyPlan(f"{doctor} is already on leave on {_dates(skipped)}.")

    doctors = await _get(read, "/admin/doctors")
    hit = _hit(await _bookings(read, doctor, todo[0], todo[-1]), doctor, todo, leave_type,
               _doctor_row(doctors, doctor), now)
    near = [a for a in hit if str(a.get("appointment_date"))[:10] <= (now.date() + timedelta(days=1)).isoformat()]
    me = await _get(read, "/admin/me")

    bullets = [f"Add leave for {doctor}: {_label(days)}, {LEAVE_LABEL[leave_type]} ({_plural(len(todo), 'day')})."]
    if skipped:
        bullets.append(f"Already on leave (skipped): {_dates(skipped)}.")
    if not hit:
        bullets.append("No confirmed bookings are affected.")
    else:
        bullets.append(f"{_plural(len(hit), 'confirmed booking')} fall on these days.")
        if len(hit) > len(near):
            bullets.append(f"{len(hit) - len(near)} of them will be cancelled, paid ones refunded and the patients "
                           "told on WhatsApp by the daily 08:00 IST check (it looks up to 7 days ahead).")
    warnings = []
    if near:
        warnings.append(f"{_plural(len(near), 'booking')} today or tomorrow will be cancelled, paid ones refunded "
                        "and the patients told on WhatsApp now, as soon as you confirm.")
    if isinstance(me, dict) and me.get("branch_id"):
        warnings.append("Bookings are counted for your branch only; the doctor's bookings at other branches "
                        "are cancelled too.")
    preview = {"title": f"{doctor} on leave: {_label(days)}", "bullets": bullets, "warnings": warnings,
               "rows": _booking_rows(hit), "affected": len(todo), "requires_ack": bool(near),
               "snapshot": {"skipped": [d.isoformat() for d in skipped],
                            "bookings": sorted(str(a.get("id")) for a in hit)}}
    if near:
        preview["ack_text"] = (f"I understand {_plural(len(near), 'booking')} will be cancelled and "
                               "the patients told now")
    return preview


def leave_create_call(params: dict) -> list:
    days = _span(params)
    body = {"doctor_name": params["doctor"], "leave_date": days[0].isoformat(),
            "end_date": days[-1].isoformat() if len(days) > 1 else None,
            "leave_type": params.get("leave_type") or "full"}
    return [_step(f"Add leave for {params['doctor']}", _call("POST", "/admin/leaves", json=body),
                  verify=verify_leave_added)]


async def verify_leave_added(read, data, call):
    body = call.json
    start = date.fromisoformat(body["leave_date"])
    days = _span({"date": body["leave_date"], "date_end": body.get("end_date") or body["leave_date"]})
    out = await read(_call("GET", "/admin/leaves", {"doctor": body["doctor_name"]}))
    if out.status != "ok" or not isinstance(out.data, list):
        return None, "Couldn't read the Leaves list"
    by_date: dict = {}
    for r in out.data:
        if isinstance(r, dict) and r.get("leave_date"):
            by_date.setdefault(str(r["leave_date"])[:10], []).append(r)
    want = database.leave_blocked_sessions([{"leave_type": body["leave_type"]}])
    ok = all(want <= database.leave_blocked_sessions(by_date.get(d.isoformat(), [])) for d in days)
    return ok, (f"{_plural(len(days), 'day')} from {start.strftime('%d %b')} on the Leaves list" if ok
                else "Not all days are on the Leaves list")


# ── doctor_leave.delete (R2) ─────────────────────────────────────────────────

async def leave_delete_preview(read, params: dict) -> dict:
    days = _span(params)
    if len(days) > MAX_LEAVE_DAYS:
        return {"affected": len(days)}
    doctor = params["doctor"]
    existing = await _existing(read, doctor)
    rows = [r for d in days for r in existing.get(d.isoformat(), []) if r.get("id")]
    if not rows:
        raise EmptyPlan(f"No leave found for {doctor} on {_label(days)}.")
    verify = _removed(doctor)
    steps = [_step(f"Remove leave on {present.fmt(r['leave_date'], 'date')}",
                   _call("DELETE", f"/admin/leaves/{r['id']}"), verify=verify) for r in rows]
    return {"title": f"Remove {doctor}'s leave: {_label(days)}",
            "bullets": [f"{present.fmt(r['leave_date'], 'date')}: {LEAVE_LABEL.get(r.get('leave_type'), 'leave')}"
                        for r in rows],
            "warnings": ["Bookings already cancelled for this leave are not restored; patients can book again "
                         "once it is removed."],
            "snapshot": sorted(str(r["id"]) for r in rows), "affected": len(rows), "steps": steps}


def leave_delete_call(params: dict) -> list:
    return []  # the rows to remove are known only from the preview's read (build_plan takes its steps)


def _removed(doctor: str):
    async def verify(read, data, call):
        leave_id = call.path.rsplit("/", 1)[-1]
        out = await read(_call("GET", "/admin/leaves", {"doctor": doctor}))
        if out.status != "ok" or not isinstance(out.data, list):
            return None, "Couldn't read the Leaves list"
        gone = all(str(r.get("id")) != leave_id for r in out.data if isinstance(r, dict))
        return gone, "No longer on the Leaves list" if gone else "Still on the Leaves list"
    return verify


async def verify_leave_removed(read, data, call):  # registry rule: a write action declares a verify
    raise NotImplementedError("doctor_leave.delete verifies per step (_removed)")


# ── doctor_leave.impact (R0) ─────────────────────────────────────────────────

def leave_impact_call(params: dict) -> list:
    days = _span(params)
    return [_step("Read bookings", _call("GET", "/admin/appointments", {
                "date_basis": "visit", "date_from": days[0].isoformat(), "date_to": days[-1].isoformat(),
                "status": "confirmed", "q": params["doctor"], "limit": PAGE, "offset": 0})),
            _step("Read doctors", _call("GET", "/admin/doctors"))]


def leave_impact_present(outcomes: list, params: dict) -> dict:
    days, doctor = _span(params), params["doctor"]
    data = outcomes[0].data if outcomes else None
    rows = data.get("appointments") if isinstance(data, dict) else None
    doctors = outcomes[1].data if len(outcomes) > 1 else None
    hit = _hit([a for a in rows or [] if isinstance(a, dict)], doctor, days, params.get("leave_type") or "full",
               _doctor_row(doctors, doctor), _now())
    title = f"Bookings a leave for {doctor} would cancel, {_label(days)}"
    if isinstance(data, dict) and int(data.get("total") or 0) > PAGE:
        title += f" (first {PAGE} checked)"
    return present.table(title, "appointments", (
        ("Date", "appointment_date", "date"), ("Time", "appointment_time", "text"),
        ("Patient", "patient_name", "text"), ("Ref", "booking_ref", "text")), hit)


# ── KK-P4-05 front desk: check-in, cancel, booking confirm/reject, call-next, mark-all-read ──

async def verify_per_step(read, data, call):  # registry rule: a write action declares a verify
    raise NotImplementedError("these actions verify per step (the step is built by the preview)")


def desk_call(params: dict) -> list:
    return []  # the record is known only from the preview's read (build_plan takes its steps)


def _matching(rows, who: str) -> list:
    """The rows `who` names: exact ref, phone digits, or every word of it in the patient's name."""
    rows = [a for a in rows or [] if isinstance(a, dict)]
    if resolve.BOOKING_REF.fullmatch(who):
        return [a for a in rows if str(a.get("booking_ref") or "").upper() == who]
    if who.isdigit():
        return [a for a in rows if who in re.sub(r"\D", "", str(a.get("patient_phone") or ""))]
    return [a for a in rows if all(w in str(a.get("patient_name") or "").lower() for w in who.split())]


def _who(a: dict) -> str:
    return str(a.get("patient_name") or "the patient")


def _booking_line(a: dict) -> str:
    return (f"{a.get('booking_ref') or 'no ref'} ({_who(a)}, {present.fmt(a.get('appointment_date'), 'date')} "
            f"{present.fmt(a.get('appointment_time'))})")


def _one(rows, who: str, none_found: str) -> dict:
    """Exactly one booking, else a refusal that lists the matches (never a guess)."""
    found = _matching(rows, who)
    if not found:
        raise EmptyPlan(none_found)
    if len(found) > 1:
        more = f" and {len(found) - 5} more" if len(found) > 5 else ""
        raise EmptyPlan(f'{len(found)} bookings match "{who}": {"; ".join(_booking_line(a) for a in found[:5])}'
                        f"{more}. Say the booking ref.")
    return found[0]


async def _search(read, who: str, first: str, last: str, status: str) -> list:
    data = await _get(read, "/admin/appointments", {"date_basis": "visit", "date_from": first, "date_to": last,
                                                     "status": status, "q": who, "limit": PAGE, "offset": 0})
    return data.get("appointments") or [] if isinstance(data, dict) else []


def _snap(a: dict) -> dict:  # in the plan hash: a changed status or token means a new preview
    return {"id": str(a.get("id")), "status": a.get("status"), "token": a.get("token_number")}


def _with(a: dict) -> str:
    return a.get("doctor_name") or a.get("lab_test_name") or "the clinic"


def _booking_after(a: dict, ok, said):
    """Read the booking back (Appointments list, its own date, searched by ref or name) and apply `ok`."""
    day, q = str(a.get("appointment_date"))[:10], a.get("booking_ref") or _who(a)

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/appointments", {"date_basis": "visit", "date_from": day, "date_to": day,
                                                              "q": q, "limit": PAGE, "offset": 0}))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the booking back"
        row = next((r for r in out.data.get("appointments") or []
                    if isinstance(r, dict) and str(r.get("id")) == str(a.get("id"))), None)
        if row is None:
            return None, "Couldn't find the booking to check it"
        return (True, said(row)) if ok(row) else (False, f"The booking is {row.get('status')}")
    return verify


def _desk_step(label: str, call, verify) -> list:
    return [_step(label, call, verify=verify)]


async def _arriving(read, params: dict) -> dict:
    """Today's one confirmed booking `who` names, not yet given a token (check-in and OPD arrive)."""
    who, today = params["who"], _now().date().isoformat()
    a = _one(await _search(read, who, today, today, "confirmed"), who, f'No confirmed booking today matches "{who}".')
    if a.get("token_number"):
        raise EmptyPlan(f"{_who(a)} is already checked in (token {a['token_number']}).")
    return a


async def check_in_preview(read, params: dict) -> dict:
    a = await _arriving(read, params)
    return {"title": f"Check in {_who(a)}",
            "bullets": [f"{a.get('booking_ref') or 'Booking'}: {present.fmt(a.get('appointment_time'))} with {_with(a)}."],
            "warnings": ["The patient gets a token number on WhatsApp."], "snapshot": _snap(a),
            "steps": _desk_step(f"Check in {_who(a)}", _call("POST", f"/admin/appointments/{a['id']}/check-in"),
                                _booking_after(a, lambda r: bool(r.get("token_number")),
                                               lambda r: f"Token {r['token_number']}"))}


async def cancel_preview(read, params: dict) -> dict:
    who, today = params["who"], _now().date()
    a = _one(await _search(read, who, today.isoformat(), (today + timedelta(days=365)).isoformat(), "confirmed"),
             who, f'No confirmed booking from today on matches "{who}".')
    # payment.admin_cancel_confirmed_booking ends `cancelled`, or `refunded` when initiate_refund succeeded.
    return {"title": f"Cancel {_who(a)}'s booking", "bullets": [f"{_booking_line(a)} with {_with(a)}."],
            "warnings": ["If it was paid online, a refund is started. The patient is told on WhatsApp."],
            "snapshot": _snap(a),
            "steps": _desk_step(f"Cancel {a.get('booking_ref') or 'the booking'}",
                                _call("DELETE", f"/admin/appointments/{a['id']}"),
                                _booking_after(a, lambda r: r.get("status") in ("cancelled", "refunded"),
                                               lambda r: f"Status: {r['status']}"))}


async def _pending(read, who: str) -> dict:
    data = await _get(read, "/admin/bookings/pending-review")
    rows = data.get("bookings") if isinstance(data, dict) else None
    return _one(rows, who, f'No booking waiting for review matches "{who}".')


async def booking_confirm_preview(read, params: dict) -> dict:
    a = await _pending(read, params["who"])
    return {"title": f"Confirm {_who(a)}'s booking", "bullets": [f"{_booking_line(a)} with {_with(a)}."],
            "warnings": ["Only confirm after you have seen the payment in Razorpay / PhonePe. "
                         "The patient is told on WhatsApp."], "snapshot": _snap(a),
            "steps": _desk_step(f"Confirm {a.get('booking_ref') or 'the booking'}",
                                _call("POST", f"/admin/bookings/{a['id']}/confirm"),
                                _booking_after(a, lambda r: r.get("status") == "confirmed",
                                               lambda r: "Status: confirmed"))}


async def booking_reject_preview(read, params: dict) -> dict:
    a = await _pending(read, params["who"])
    # payment.admin_reject_booking refunds a captured payment first, then sets `cancelled`.
    return {"title": f"Reject {_who(a)}'s booking", "bullets": [f"{_booking_line(a)} with {_with(a)}."],
            "warnings": ["A captured payment is refunded. The patient is told on WhatsApp."], "snapshot": _snap(a),
            "steps": _desk_step(f"Reject {a.get('booking_ref') or 'the booking'}",
                                _call("POST", f"/admin/bookings/{a['id']}/reject"),
                                _booking_after(a, lambda r: r.get("status") in ("cancelled", "refunded"),
                                               lambda r: f"Status: {r['status']}"))}


def _queue_of(board) -> list:
    doctors = board.get("doctors") if isinstance(board, dict) else None
    return [r for d in doctors or [] if isinstance(d, dict) for r in d.get("rows") or [] if isinstance(r, dict)]


def _token(r: dict) -> str:
    return f"Token {r.get('token_number')} ({r.get('patient_name') or 'patient'})"


async def call_next_preview(read, params: dict) -> dict:
    """opd_call_next finishes the doctor's open consultation and calls the lowest waiting token; it is
    not idempotent, so both rows are in the plan hash and a moved queue means a new preview."""
    doctor, today = params["doctor"], _now().date().isoformat()
    doc = _doctor_row(await _get(read, "/admin/doctors"), doctor)
    if not doc or not doc.get("id"):
        raise EmptyPlan(f"{doctor} isn't on the doctor list.")
    did = str(doc["id"])
    rows = _queue_of(await _get(read, "/admin/opd/queue", {"date": today, "doctor_id": did}))
    current = next((r for r in rows if r.get("stage") == "in_consultation"), None)
    waiting = sorted((r for r in rows if r.get("stage") == "waiting"), key=lambda r: r.get("token_number") or 0)
    nxt = waiting[0] if waiting else None
    if not current and not nxt:
        raise EmptyPlan(f"No one is waiting for {doctor} and no consultation is open.")
    bullets = [f"{_token(current)} finishes the consultation."] if current else []
    bullets.append(f"{_token(nxt)} is called in ({len(waiting) - 1} more waiting)." if nxt
                   else "No one is waiting, so no one is called.")
    ids = {"current": str(current["appointment_id"]) if current else None,
           "next": str(nxt["appointment_id"]) if nxt else None}
    return {"title": f"Call next for {doctor}", "bullets": bullets, "snapshot": ids,
            "steps": _desk_step(f"Call next for {doctor}",
                                _call("POST", "/admin/opd/queue/call-next", json={"doctor_id": did}),
                                _called(did, today, ids))}


def _called(did: str, day: str, ids: dict):
    async def verify(read, data, call):
        called = data.get("called") if isinstance(data, dict) else None
        want = str(called["appointment_id"]) if isinstance(called, dict) and called.get("appointment_id") \
            else ids["next"]
        out = await read(_call("GET", "/admin/opd/queue", {"date": day, "doctor_id": did}))
        if out.status != "ok":
            return None, "Couldn't read the queue back"
        rows = {str(r.get("appointment_id")): r for r in _queue_of(out.data)}
        if want:
            row = rows.get(want)
            if row and row.get("stage") == "in_consultation":
                return True, f"{_token(row)} is in consultation"
            return False, "The called patient is not in consultation"
        row = rows.get(ids["current"])
        if row and row.get("stage") != "in_consultation":
            return True, "Consultation finished"
        return None, "Couldn't confirm the consultation finished"
    return verify


async def mark_read_preview(read, params: dict) -> dict:
    data = await _get(read, "/admin/notifications", {"unread_only": True, "limit": 100})
    rows = [n for n in (data.get("notifications") or [] if isinstance(data, dict) else []) if isinstance(n, dict)]
    if not rows:
        raise EmptyPlan("There are no unread notifications.")
    ids = sorted(str(n.get("id")) for n in rows)
    count = f"{len(rows)}{'+' if len(rows) >= 100 else ''}"
    # broadcast.mark_all_notifications_read filters on clinic_id only: read state is shared by the clinic.
    return {"title": "Mark all notifications as read",
            "bullets": [f"{count} unread notification{'' if len(rows) == 1 else 's'} will be marked as read."],
            "warnings": ["Read status is shared: they are marked read for everyone at this clinic."],
            "snapshot": ids,
            "steps": _desk_step("Mark all as read", _call("POST", "/admin/notifications/mark-all-read"),
                                _all_read(ids))}


def _all_read(ids: list):
    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/notifications", {"unread_only": True, "limit": 100}))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the notifications back"
        left = {str(n.get("id")) for n in out.data.get("notifications") or [] if isinstance(n, dict)} & set(ids)
        return (not left), ("None of them are unread now" if not left else f"{len(left)} still unread")
    return verify


# ── holiday.create (R3; released after D-27 / migration 109) ─────────────────

HOLIDAY_DEFAULT_NAME = "Clinic closed"
MAX_HOLIDAY_BOOKINGS = 10000  # the typed count's own ceiling (ConfirmIn.typed_count <= 10000)


async def holiday_create_preview(read, params: dict) -> dict:
    """POST /admin/holidays blocks new bookings for the day (get_available_slots); nothing cancels the
    confirmed ones already made, so their count is the typed confirmation and the warning says so."""
    day, name = params["date"], params.get("name") or HOLIDAY_DEFAULT_NAME
    if day < _now().date().isoformat():
        raise EmptyPlan(f"{present.fmt(day, 'date')} has already passed.")
    existing = await _get(read, "/admin/holidays")
    same = next((h for h in existing if isinstance(h, dict) and str(h.get("holiday_date"))[:10] == day), None) \
        if isinstance(existing, list) else None
    if same:
        raise EmptyPlan(f"{present.fmt(day, 'date')} is already a holiday ({same.get('name') or 'no name'}).")
    data = await _get(read, "/admin/appointments", {"date_basis": "visit", "date_from": day, "date_to": day,
                                                     "status": "confirmed", "limit": 1, "offset": 0})
    booked = int(data.get("total") or 0) if isinstance(data, dict) else 0
    me = await _get(read, "/admin/me")
    warnings = [f"Patients can no longer book {present.fmt(day, 'date')} on WhatsApp or the phone line."]
    if booked:
        warnings.append(f"{_plural(booked, 'confirmed booking')} on that day {'is' if booked == 1 else 'are'} "
                        "not cancelled. Cancel them or tell the patients from the Appointments page.")
    if isinstance(me, dict) and me.get("branch_id"):
        warnings.append("The holiday closes every branch; the booking count covers your branch only.")
    return {"title": f"Holiday on {present.fmt(day, 'date')}: {name}",
            "bullets": [f"{_plural(booked, 'confirmed booking')} on that day."], "warnings": warnings,
            "affected": booked, "snapshot": {"bookings": booked},
            "steps": [_step(f"Add holiday {present.fmt(day, 'date')}",
                            _call("POST", "/admin/holidays", {"holiday_date": day, "name": name}),
                            verify=_holiday_listed(day))]}


def _holiday_listed(day: str):
    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/holidays"))
        if out.status != "ok" or not isinstance(out.data, list):
            return None, "Couldn't read the Holidays list"
        ok = any(isinstance(h, dict) and str(h.get("holiday_date"))[:10] == day for h in out.data)
        return ok, "On the Holidays list" if ok else "Not on the Holidays list"
    return verify


# ── KK-P4-03: report resend (PB-02), lead message, connector run-now ─────────

MAX_RESEND = 20  # PB-02: "Resend all failed (N)" is R3 with N <= 20


def _when(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # TIMESTAMPTZ; a naive value is UTC


def _report_line(r: dict) -> str:
    return f"{r.get('report_name') or 'Report'} for {r.get('patient_name') or 'a patient'}"


def _resent(r: dict):
    """Read the delivery log back: the row is `sent` with a newer sent_at than the preview saw."""
    before = _when(r.get("sent_at"))

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/reports/deliveries", {"days": 30, "state": "all"}))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the delivery log back"
        row = next((x for x in out.data.get("deliveries") or []
                    if isinstance(x, dict) and str(x.get("id")) == str(r.get("id"))), None)
        if row is None:
            return None, "Couldn't find the report in the delivery log"
        sent = _when(row.get("sent_at"))
        if row.get("status") == "sent" and sent and (before is None or sent > before):
            return True, f"Sent {sent.astimezone(IST).strftime('%d %b %H:%M')}"
        return False, "No new send in the delivery log"
    return verify


def _resend_step(r: dict):
    return _step(f"Resend {_report_line(r)}", _call("POST", f"/admin/reports/{r['id']}/resend"), verify=_resent(r))


async def _deliveries(read, days: int, state: str) -> list:
    data = await _get(read, "/admin/reports/deliveries", {"days": days, "state": state})
    return [r for r in (data.get("deliveries") or [] if isinstance(data, dict) else []) if isinstance(r, dict)]


async def report_resend_preview(read, params: dict) -> dict:
    who = params["who"]
    rows = await _deliveries(read, 30, "all")
    if who.isdigit():
        found = [r for r in rows if re.sub(r"\D", "", str(r.get("patient_phone") or "")).endswith(who[-4:])]
    else:
        found = [r for r in rows if all(w in f"{r.get('patient_name')} {r.get('report_name')}".lower()
                                        for w in who.split())]
    if not found:
        raise EmptyPlan(f'No report in the last 30 days matches "{who}".')
    if len(found) > 1:
        listed = "; ".join(f"{_report_line(r)} ({present.fmt(r.get('sent_at') or r.get('uploaded_at'), 'time')})"
                           for r in found[:5])
        raise EmptyPlan(f'{len(found)} reports match "{who}": {listed}. Add the report name, e.g. "resend '
                        f'{found[0].get("patient_name") or "Ravi"}\'s {found[0].get("report_name") or "CBC"} report".')
    r = found[0]
    sent = _when(r.get("sent_at"))
    recent = r.get("state") == "delivered" and sent is not None and _now() - sent < timedelta(hours=24)
    preview = {"title": f"Resend {_report_line(r)}",
               "bullets": [f"Last state: {present.fmt(r.get('state'), 'label')}."],
               "warnings": ["The patient gets the report on WhatsApp again."],
               "snapshot": {"id": str(r.get("id")), "sent_at": r.get("sent_at")}, "requires_ack": recent,
               "steps": [_resend_step(r)]}
    if recent:
        preview["ack_text"] = f"I understand {r.get('patient_name') or 'the patient'} got this report in the last 24 hours and will get it again"
    return preview


async def report_resend_failed_preview(read, params: dict) -> dict:
    rows = await _deliveries(read, 1, "failed")
    if not rows:
        raise EmptyPlan("No report failed to send in the last day.")
    return {"title": f"Resend {_plural(len(rows), 'failed report')}",
            "bullets": [f"{_report_line(r)}." for r in rows[:8]] + ([f"…and {len(rows) - 8} more."] if len(rows) > 8 else []),
            "warnings": ["Each patient gets their report on WhatsApp. A report that fails again stays on the failed list."],
            "snapshot": sorted(str(r.get("id")) for r in rows), "steps": [_resend_step(r) for r in rows]}


async def lead_message_preview(read, params: dict) -> dict:
    """The text is sent exactly as the staff member wrote it. Opt-out and the 24 h window are checked
    here for a clear answer and again by the endpoint (leads.send_lead_message), which decides."""
    who, text = params["who"], params["message"]
    data = await _get(read, "/admin/leads", {"segment": "all", "days": 0, "q": who[:60], "limit": 100, "offset": 0})
    rows = [x for x in (data.get("rows") or [] if isinstance(data, dict) else []) if isinstance(x, dict)]
    found = [x for x in rows if (who in re.sub(r"\D", "", str(x.get("phone") or "")) if who.isdigit()
                                 else all(w in str(x.get("name") or "").lower() for w in who.split()))]
    if not found:
        raise EmptyPlan(f'No WhatsApp contact matches "{who}".')
    if len(found) > 1:
        listed = "; ".join(f"{x.get('name') or 'No name'} (…{str(x.get('phone') or '')[-4:]})" for x in found[:5])
        raise EmptyPlan(f'{len(found)} contacts match "{who}": {listed}. Use more of the name or the last digits.')
    lead = found[0]
    phone, name = str(lead.get("phone") or ""), lead.get("name") or "this contact"
    if "•" in phone:  # the leads RPC masks a do-not-contact number
        raise EmptyPlan(f"{name} asked not to be contacted. Kriya won't message them.")
    if not lead.get("window_open"):
        raise EmptyPlan(f"WhatsApp only allows a message within 24 hours of {name}'s last message. "
                        "Please call them instead.")
    return {"title": f"WhatsApp message to {name}", "bullets": [f'"{text}"'],
            "warnings": [f"The window to message {name} closes at "
                         f"{present.fmt(lead.get('window_closes_at'), 'time')}."],
            "snapshot": {"name": name, "window_closes_at": lead.get("window_closes_at")},  # no phone stored
            "steps": [_step(f"Message {name}", _call("POST", "/admin/leads/message", json={"phone": phone, "message": text}),
                            verify=_accepted("WhatsApp accepted the message"))]}


def _accepted(evidence: str):
    """For endpoints whose own answer is the outcome (ADR-004): 2xx only after the action happened.
    Re-verifying later (no response) can't tell, so it never resends."""
    async def verify(read, data, call):
        if isinstance(data, dict) and data.get("success") is True and data.get("status", "ok") in ("ok", "running", "queued"):
            return True, evidence
        return None, "Couldn't confirm from the response"
    return verify


async def connector_run_preview(read, params: dict) -> dict:
    data = await _get(read, "/admin/connectors")
    me = await _get(read, "/admin/me")
    branch = me.get("branch_id") if isinstance(me, dict) else None
    rows = [c for c in (data.get("connectors") or [] if isinstance(data, dict) else [])
            if isinstance(c, dict) and c.get("is_enabled") and (not branch or str(c.get("branch_id")) == str(branch))]
    if not rows:
        raise EmptyPlan("No report connector is switched on.")
    if len(rows) > 1:
        raise EmptyPlan(f"{len(rows)} connectors are switched on. Use Run now on the Report Connector page "
                        "for the one you want.")
    c = rows[0]
    kind = str(c.get("connector_type") or "report").title()
    return {"title": f"Run the {kind} connector now",
            "bullets": [f"Last run: {present.fmt(c.get('last_run_at'), 'time')}."],
            "warnings": ["It takes 2-5 minutes; new reports and any failures appear on the Report Connector page. "
                         "Don't start it again meanwhile."],
            "snapshot": {"id": str(c.get("id")), "last_run_at": c.get("last_run_at")},
            "steps": [_step(f"Run the {kind} connector", _call("POST", f"/admin/connectors/{c['id']}/run-now"),
                            verify=_accepted("Run started"))]}


# ── KK-P4-06: PB-03 leads -> calls, PB-04 funnel, PB-05 automatic lead calls ──

MAX_LEAD_CALLS = 100  # PB-03: the read-back (GET /admin/voice/outbound) shows the latest 100 jobs
_BULK = "/admin/voice/outbound/bulk"


def _digest(values) -> str:  # in the plan hash instead of phone numbers (nothing personal is stored)
    return hashlib.sha256("|".join(sorted(map(str, values))).encode()).hexdigest()


def _last10(phone) -> str:
    return re.sub(r"\D", "", str(phone or ""))[-10:]


async def lead_calls_preview(read, params: dict) -> dict:
    """PB-03. Reads hot/open leads only (booked and do-not-contact are never offered), asks the bulk
    endpoint for a dry run (KK-P4-06a: its own rules and reasons, nothing queued), and plans to send
    exactly the dry run's would_queue list, never with consent_attested (these are WhatsApp contacts)."""
    query = {"segment": params.get("segment") or "open", "days": int(params.get("days") or 7),
             "limit": MAX_LEAD_CALLS, "offset": 0}
    first = await read(_call("GET", "/admin/leads", query))
    if first.status != "ok":
        if first.error_code == "forbidden":
            raise EmptyPlan("You need the Leads Manage permission to call leads. Ask your clinic admin.")
        raise RuntimeError("leads read failed")
    data = first.data if isinstance(first.data, dict) else {}
    term = params.get("interest")
    if term:
        labels = [str(x) for x in data.get("interest_labels") or []]
        hit = [x for x in labels if term in x.lower() or x.lower() in term]
        if len(hit) != 1:
            raise EmptyPlan(f'I don\'t know the interest "{term}". Known interests: {", ".join(labels) or "none"}.')
        query["interest"] = hit[0]
        data = await _get(read, "/admin/leads", query)
    total = int(data.get("total") or 0)
    if total > MAX_LEAD_CALLS:
        return {"affected": total}  # build_plan refuses it (too_many): narrow it down
    rows = [x for x in data.get("rows") or [] if isinstance(x, dict) and x.get("phone") and "•" not in str(x["phone"])]
    if not rows:
        raise EmptyPlan("No leads match. Try another period or interest.")
    interest = {_last10(x["phone"]): (x.get("interests") or [None])[0] for x in rows}
    names = {_last10(x["phone"]): x.get("name") or "No name" for x in rows}
    dry = await read(_call("POST", _BULK, json={"contacts": [{"phone": str(x["phone"]), "interest": interest[_last10(x["phone"])]}
                                                            for x in rows], "consent_attested": False, "dry_run": True}))
    if dry.status != "ok" or not isinstance(dry.data, dict):
        raise RuntimeError("dry run failed")
    would = [str(p) for p in dry.data.get("would_queue") or []]
    reasons = Counter(str(s.get("reason")) for s in dry.data.get("skipped") or [] if isinstance(s, dict))
    skipped_line = "; ".join(f"{r} ({n})" for r, n in reasons.most_common())
    if not would:
        raise EmptyPlan(f"No one can be called. Skipped: {skipped_line or 'all of them'}.")
    out = ((await _get(read, "/admin/voice/settings")) or {}).get("settings", {}).get("outbound") or {}
    start, end, cap = out.get("window_start", "10:00"), out.get("window_end", "19:00"), int(out.get("daily_cap") or 0)
    bullets = [f"{_plural(len(would), 'lead')} will be called by the AI receptionist."]
    if skipped_line:
        bullets.append(f"Skipped: {skipped_line}.")
    bullets.append(f"Calls go out one by one between {start} and {end}, up to {cap} a day"
                   + (", so some go out on later days." if len(would) > cap else "."))
    contacts = [{"phone": p, "interest": interest.get(_last10(p))} for p in would]
    return {"title": f"Call {_plural(len(would), 'lead')}", "bullets": bullets,
            "warnings": ["Only people who messaged the clinic on WhatsApp are called; opted-out people never are."],
            "rows": [[names.get(_last10(p), "No name"), f"…{_last10(p)[-4:]}", interest.get(_last10(p)) or "—"]
                     for p in would[:25]],
            "affected": len(would), "snapshot": {"phones": _digest(would), "skipped": sum(reasons.values())},
            "steps": [_step(f"Queue {_plural(len(would), 'call')}",
                            _call("POST", _BULK, json={"contacts": contacts, "consent_attested": False}),
                            verify=_calls_queued(would))]}


def _calls_queued(phones: list):
    async def verify(read, data, call):
        late = {str(s.get("phone")) for s in (data or {}).get("skipped") or [] if isinstance(s, dict)} \
            if isinstance(data, dict) else set()   # raced by another enqueue between preview and confirm
        want = {p for p in phones if p not in late}
        out = await read(_call("GET", "/admin/voice/outbound"))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the call queue back"
        live = {str(j.get("patient_phone")) for j in out.data.get("jobs") or []
                if isinstance(j, dict) and j.get("status") in ("queued", "dialing")}
        if want <= live:
            return True, f"{_plural(len(want), 'call')} in the queue" + (f"; {len(late)} skipped" if late else "")
        return False, f"{len(want - live)} not in the queue"
    return verify


def leads_funnel_call(params: dict) -> list:
    return [_step("Read the call queue", _call("GET", "/admin/voice/outbound")),
            _step("Read booked leads", _call("GET", "/admin/leads", {"segment": "booked", "days": 30, "limit": 1,
                                                                      "offset": 0}))]


def leads_funnel_present(outcomes: list, params: dict) -> dict:
    """PB-04 from the job statuses (latest 100) and the booked-leads count. The per-call outcome and
    per-appointment attribution (N+1 reads) wait for the aggregate endpoint KK-P4-06b."""
    jobs = outcomes[0].data.get("jobs") if outcomes and isinstance(outcomes[0].data, dict) else []
    status = Counter(str(j.get("status")) for j in jobs or [] if isinstance(j, dict))
    booked = outcomes[1].data.get("total") if len(outcomes) > 1 and isinstance(outcomes[1].data, dict) else None
    data = {"waiting": status["queued"], "calling": status["dialing"], "done": status["completed"],
            "missed": status["failed"] + status["skipped"], "booked": booked}
    return present.metrics("Lead calls (latest 100)", "voice", data, (
        ("Waiting to be called", "waiting", "num"), ("Calling now", "calling", "num"),
        ("Calls finished", "done", "num"), ("Not reached", "missed", "num"),
        ("Leads who booked (30 days)", "booked", "num")))


async def auto_leads_preview(read, params: dict) -> dict:
    """PB-05. PUT /admin/voice/settings replaces the whole object, so the step sends back exactly what
    was read with one field changed; the full object is in the plan hash, so an edit in between means a
    new preview (D-13 still allows a lost update in the instant between confirm and the PUT)."""
    want = params["switch"] == "on"
    settings = ((await _get(read, "/admin/voice/settings")) or {}).get("settings")
    if not isinstance(settings, dict):
        raise RuntimeError("settings read failed")
    out = settings.get("outbound") or {}
    if bool(out.get("auto_leads")) == want:
        raise EmptyPlan(f"Automatic lead calls are already {'on' if want else 'off'}.")
    body = json.loads(json.dumps(settings))
    body.setdefault("outbound", {})["auto_leads"] = want
    bullets = [f"Automatic lead calls: {'off → on' if want else 'on → off'}."]
    if want:
        bullets.append(f"Every hour the receptionist calls new WhatsApp leads from the last 24 hours, between "
                       f"{out.get('window_start', '10:00')} and {out.get('window_end', '19:00')}, up to "
                       f"{out.get('daily_cap', 0)} a day. Booked, opted-out and recently called people are skipped.")
    return {"title": f"Turn automatic lead calls {'on' if want else 'off'}", "bullets": bullets,
            "warnings": ["The other voice settings are sent back unchanged."],
            "snapshot": _digest([json.dumps(settings, sort_keys=True)]),
            "steps": [_step("Save voice settings", _call("PUT", "/admin/voice/settings", json=body),
                            verify=_auto_leads_is(want))]}


def _auto_leads_is(want: bool):
    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/voice/settings"))
        settings = out.data.get("settings") if out.status == "ok" and isinstance(out.data, dict) else None
        if not isinstance(settings, dict):
            return None, "Couldn't read the voice settings back"
        ok = bool((settings.get("outbound") or {}).get("auto_leads")) == want
        return ok, f"Automatic lead calls are {'on' if want else 'off'}" if ok else "The setting did not change"
    return verify


# ── KK-P4-07: PB-06 register + walk-in token; OPD arrive / recall / stage ─────

_DUPES = "/admin/opd/patients/duplicate-check"


def _rows_of(data, key: str = "") -> list:
    rows = data.get(key) if isinstance(data, dict) and key else data
    return [r for r in rows or [] if isinstance(r, dict)] if isinstance(rows, list) else []


async def register_preview(read, params: dict) -> dict:
    """PB-06 steps 1-4. Consent comes only from the form's yes/no answers (REG-03); without data
    consent there is no registration (register_patient requires it). Candidates from the duplicate check
    are listed and nothing is written, unless staff said "new person" — only then are they sent as
    acknowledged_duplicates (REG-01/02). The token is a second request: the walk-in needs the new id."""
    if params.get("data_consent") != "yes":
        raise EmptyPlan("Registration needs the patient's consent to store their data, so Kriya won't register "
                        "them. You can still help them at the desk.")
    name, phone = params["name"], params["phone"]
    dupes = await read(_call("POST", _DUPES, json={"name": name, "phone": phone}))
    if dupes.status != "ok":
        raise RuntimeError("duplicate check failed")
    found = _rows_of(dupes.data, "candidates" if isinstance(dupes.data, dict) else "")
    ack = []
    if found:
        listed = "; ".join(f"{c.get('name') or 'No name'} (MRN {c.get('mrn') or '—'})" for c in found[:5])
        if params.get("new_person") != "yes":
            raise EmptyPlan(f"This phone number already has: {listed}. To give one of them a token, say "
                            f'"token for <name> with Dr <name>". If {name} is a different person, say the same '
                            "request again with \"as a new person\".")
        ack = sorted(f"{c['patient_id']}:{c['family_member_id']}" if c.get("family_member_id") else str(c["patient_id"])
                     for c in found if c.get("patient_id"))
    wa = params.get("whatsapp_opt_in") == "yes"
    body = {"phone": phone, "name": name, "data_consent": True, "whatsapp_opt_in": wa}
    if ack:
        body["acknowledged_duplicates"] = ack
    bullets = [f"Register {name} (phone …{phone[-4:]}).", "Consent to store their data: yes.",
               f"WhatsApp messages: {'yes' if wa else 'no'}."]
    if ack:
        bullets.append(f"A new record next to the existing one(s) on this phone: {listed}.")
    return {"title": f"Register {name}", "bullets": bullets,
            "warnings": ["Consent answers are what the patient told you; they are saved with the record."],
            "snapshot": {"phone": _digest([phone]), "candidates": ack},
            "steps": [_step(f"Register {name}", _call("POST", "/admin/opd/patients", json=body),
                            verify=_registered(name))]}


def _registered(name: str):
    async def verify(read, data, call):
        pid = data.get("patient_id") if isinstance(data, dict) else None
        if not pid:
            return None, "Couldn't confirm the new record"
        out = await read(_call("GET", f"/admin/opd/patients/{pid}"))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the new record back"
        if str(out.data.get("name") or "").strip().lower() != name.strip().lower():
            return False, "The saved record has a different name"
        return True, (f"Registered, MRN {out.data.get('mrn') or data.get('mrn') or '—'}. For a token, say "
                      f'"token for {name} with Dr <name>".')
    return verify


async def walk_in_preview(read, params: dict) -> dict:
    """PB-06 step 5 for an existing patient: one search match, the doctor's id, and no second token
    today in that doctor's queue. Read-back: the token in the queue (REG-04)."""
    who, doctor, today = params["who"], params["doctor"], _now().date().isoformat()
    rows = _rows_of(await _get(read, "/admin/opd/patients/search", {"q": who[:60], "limit": 20}))
    found = [p for p in rows if all(w.lower() in str(p.get("name") or "").lower() for w in who.split())]
    if not found:
        raise EmptyPlan(f'No registered patient matches "{who}". Register them first.')
    if len(found) > 1:
        listed = "; ".join(f"{p.get('name')} (MRN {p.get('mrn') or '—'})" for p in found[:5])
        raise EmptyPlan(f'{len(found)} patients match "{who}": {listed}. Use the full name.')
    p = found[0]
    doc = _doctor_row(await _get(read, "/admin/doctors"), doctor)
    if not doc or not doc.get("id"):
        raise EmptyPlan(f"{doctor} isn't on the doctor list.")
    did = str(doc["id"])
    queue = _queue_of(await _get(read, "/admin/opd/queue", {"date": today, "doctor_id": did}))
    open_row = next((r for r in queue if str(r.get("patient_name") or "").lower() == str(p.get("name") or "").lower()
                     and r.get("stage") not in ("completed", "cancelled")), None)
    if open_row:
        raise EmptyPlan(f"{p.get('name')} already has token {open_row.get('token_number')} with {doctor} today.")
    body = {"patient_id": str(p["patient_id"]), "doctor_id": did, "visit_type": params.get("visit_type") or "new"}
    if p.get("family_member_id"):
        body["family_member_id"] = str(p["family_member_id"])
    return {"title": f"Token for {p.get('name')} with {doctor}",
            "bullets": [f"MRN {p.get('mrn') or '—'}, {body['visit_type']} visit.", f"{len(queue)} in {doctor}'s queue today."],
            "snapshot": {"patient": body["patient_id"], "queue": len(queue)},
            "steps": [_step(f"Give {p.get('name')} a token", _call("POST", "/admin/opd/walk-ins", json=body),
                            verify=_in_queue(did, today))]}


def _in_queue(did, day: str):
    """The appointment the response names is on today's queue (did=None: any doctor)."""
    async def verify(read, data, call):
        aid = data.get("appointment_id") if isinstance(data, dict) else None
        if not aid:
            aid = call.path.split("/")[-2] if call.path.endswith("/arrive") else None
        if not aid:
            return None, "Couldn't tell which token was issued"
        out = await read(_call("GET", "/admin/opd/queue", {"date": day, **({"doctor_id": did} if did else {})}))
        if out.status != "ok":
            return None, "Couldn't read the queue back"
        row = next((r for r in _queue_of(out.data) if str(r.get("appointment_id")) == str(aid)), None)
        return (True, f"Token {row.get('token_number')} in the queue") if row else (False, "Not in the queue")
    return verify


async def arrive_preview(read, params: dict) -> dict:
    """KK-FR-061: on a live-OPD clinic a booked patient's arrival goes through the OPD desk endpoint
    (branch, OPD start stage, token message) instead of the legacy check-in."""
    a = await _arriving(read, params)
    return {"title": f"Mark {_who(a)} arrived",
            "bullets": [f"{a.get('booking_ref') or 'Booking'}: {present.fmt(a.get('appointment_time'))} with {_with(a)}."],
            "warnings": ["The patient gets a token number on WhatsApp."], "snapshot": _snap(a),
            "steps": [_step(f"Mark {_who(a)} arrived", _call("POST", f"/admin/opd/appointments/{a['id']}/arrive"),
                            verify=_in_queue(None, _now().date().isoformat()))]}


async def _queue_row(read, params: dict) -> dict:
    """The one row of today's queue the target names ("#5" or a name), narrowed by doctor if given."""
    target, today = params["target"], _now().date().isoformat()
    query = {"date": today}
    if params.get("doctor"):
        doc = _doctor_row(await _get(read, "/admin/doctors"), params["doctor"])
        if doc and doc.get("id"):
            query["doctor_id"] = str(doc["id"])
    rows = [r for r in _queue_of(await _get(read, "/admin/opd/queue", query))
            if r.get("stage") not in ("completed", "cancelled")]
    if target.startswith("#"):
        found = [r for r in rows if str(r.get("token_number")) == target[1:]]
    else:
        found = [r for r in rows if all(w in str(r.get("patient_name") or "").lower() for w in target.split())]
    label = f"token {target[1:]}" if target.startswith("#") else f'"{target}"'
    if not found:
        raise EmptyPlan(f"Nobody in today's queue matches {label}.")
    if len(found) > 1:
        listed = "; ".join(f"{_token(r)} with {r.get('doctor_name') or 'a doctor'}" for r in found[:5])
        raise EmptyPlan(f"{len(found)} queue entries match {label}: {listed}. Name the doctor too.")
    return found[0]


async def recall_preview(read, params: dict) -> dict:
    r = await _queue_row(read, params)
    return {"title": f"Recall {_token(r)}", "bullets": ["Sends the patient a WhatsApp reminder to come to the room."],
            "snapshot": {"id": str(r.get("appointment_id")), "stage": r.get("stage")},
            "steps": [_step(f"Recall {_token(r)}", _call("POST", f"/admin/opd/queue/{r['appointment_id']}/recall"),
                            verify=_recalled)]}


async def _recalled(read, data, call):
    """The endpoint's own answer is the outcome (ADR-004): notified only when WhatsApp took the message."""
    if isinstance(data, dict) and data.get("notified") is True:
        return True, "Recall sent on WhatsApp"
    return (False, "WhatsApp did not send the recall") if isinstance(data, dict) else (None, "Couldn't confirm the recall")


async def stage_preview(read, params: dict) -> dict:
    """The queue's CAS: expected_from is the stage the preview read; a moved row means a new preview."""
    r, to = await _queue_row(read, params), params["stage"]
    if r.get("stage") == to:
        raise EmptyPlan(f"{_token(r)} is already at {to.replace('_', ' ')}.")
    body = {"to_stage": to, "expected_from": r.get("stage")}
    return {"title": f"Move {_token(r)} to {to.replace('_', ' ')}",
            "bullets": [f"From {str(r.get('stage')).replace('_', ' ')} to {to.replace('_', ' ')}."],
            "snapshot": {"id": str(r.get("appointment_id")), "stage": r.get("stage")},
            "steps": [_step(f"Move {_token(r)}", _call("POST", f"/admin/opd/queue/{r['appointment_id']}/stage", json=body),
                            verify=_at_stage(str(r["appointment_id"]), to))]}


def _at_stage(aid: str, to: str):
    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/opd/queue", {"date": _now().date().isoformat()}))
        if out.status != "ok":
            return None, "Couldn't read the queue back"
        row = next((r for r in _queue_of(out.data) if str(r.get("appointment_id")) == aid), None)
        if row is None:
            return None, "Not on today's queue board"
        return (row.get("stage") == to), f"Now at {str(row.get('stage')).replace('_', ' ')}"
    return verify


# ── KK-P4-08: PB-07 lab price (single R2 / bulk R3), lab test create / deactivate, collection window ──

MAX_LAB_BULK = 200  # PB-07 cap
_LAB_PAGE = "Change it on the Test Catalogue page."


def _max_price() -> int:
    from app.routers.admin import MAX_LAB_PRICE_RUPEES  # lazy: the router module is heavy; same ceiling (D-31)
    return MAX_LAB_PRICE_RUPEES


def new_price(old: Decimal, change: list, step: int = 1) -> int:
    """The new whole-rupee price: set to / by rupees / by percent, rounded half-up to the nearest `step`."""
    kind, value = change[0], Decimal(change[1])
    raw = value if kind == "amount" else old + value if kind == "delta" else old * (100 + value) / 100
    return int((raw / step).quantize(Decimal(1), ROUND_HALF_UP) * step)


def _price(t: dict) -> Decimal:
    return Decimal(int(t.get("price_paise") or 0)) / 100


def _inr(rupees) -> str:
    return present.fmt(int(rupees) * 100, "paise")


def _test_line(t: dict) -> str:
    where = "one branch" if t.get("branch_id") else "all branches"
    return f"{t.get('name')} ({where}, {present.fmt(t.get('price_paise'), 'paise')})"


def _check_price(t: dict, new: int, ceiling: int = None) -> None:
    ceiling = ceiling or _max_price()
    if not 0 < new <= ceiling:
        raise EmptyPlan(f"The new price for {t.get('name')} would be {_inr(new)}. A price must be more than ₹0 "
                        f"and at most {_inr(ceiling)}; nothing was changed.")


async def _lab_tests(read) -> tuple[list, bool]:
    """The tests this user may edit: a branch-pinned user's branch and the shared ones (D-31), else all."""
    rows = await _get(read, "/admin/lab-tests")
    me = await _get(read, "/admin/me")
    mine = str(me.get("branch_id")) if isinstance(me, dict) and me.get("branch_id") else None
    rows = [t for t in rows if isinstance(t, dict)] if isinstance(rows, list) else []
    return [t for t in rows if not mine or t.get("branch_id") in (None, mine)], bool(mine)


def _one_test(rows: list, term: str, noun: str = "lab test", line=None, page: str = None) -> dict:
    """The exact name, else the one name holding every word; anything else is refused, never guessed."""
    line, page = line or _test_line, page or _LAB_PAGE
    exact = [t for t in rows if str(t.get("name") or "").strip().lower() == term]
    found = exact or [t for t in rows if all(w in str(t.get("name") or "").lower() for w in term.split())]
    if not found:
        names = {str(t.get("name") or "").lower(): str(t.get("name")) for t in rows}
        close = [names[n] for n in difflib.get_close_matches(term, list(names), n=3, cutoff=0.6)]
        raise EmptyPlan(f'No {noun} matches "{term}".' + (f" Did you mean {', '.join(close)}?" if close else ""))
    if len(found) > 1:
        more = f" and {len(found) - 5} more" if len(found) > 5 else ""
        raise EmptyPlan(f'{len(found)} {noun}s match "{term}": {"; ".join(line(t) for t in found[:5])}{more}. '
                        f"Say the full name. {page}")
    return found[0]


def _lab_row(tid, ok, said: str):
    """Read the Test Catalogue back and apply `ok` to the test's row."""
    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/lab-tests"))
        if out.status != "ok" or not isinstance(out.data, list):
            return None, "Couldn't read the Test Catalogue back"
        row = next((r for r in out.data if isinstance(r, dict) and str(r.get("id")) == str(tid)), None)
        if row is None:
            return None, "Couldn't find the test to check it"
        return (True, said) if ok(row) else (False, "The Test Catalogue shows a different value")
    return verify


def _price_step(t: dict, new: int):
    return _step(f"{t.get('name')}: {present.fmt(t.get('price_paise'), 'paise')} → {_inr(new)}",
                 _call("PUT", f"/admin/lab-tests/{t['id']}", json={"price_rupees": new}),
                 verify=_lab_row(t["id"], lambda r: int(r.get("price_paise") or 0) == new * 100, f"Price is {_inr(new)}"))


_PRICE_WARNING = "Patients see the new price on WhatsApp straight away; bookings already made keep their price."


async def lab_price_preview(read, params: dict) -> dict:
    rows, _ = await _lab_tests(read)
    t = _one_test(rows, params["test"])
    new = new_price(_price(t), params["price"])
    _check_price(t, new)
    if new == _price(t):
        raise EmptyPlan(f"{t.get('name')} is already {_inr(new)}.")
    return {"title": f"Change {t.get('name')}: {present.fmt(t.get('price_paise'), 'paise')} → {_inr(new)}",
            "bullets": [f"Offered at {'one branch' if t.get('branch_id') else 'all branches'}."],
            "rows": [[str(t.get("name")), present.fmt(t.get("price_paise"), "paise"), _inr(new)]],
            "warnings": [_PRICE_WARNING], "snapshot": {"id": str(t["id"]), "price_paise": t.get("price_paise")},
            "steps": [_price_step(t, new)]}


async def lab_price_bulk_preview(read, params: dict) -> dict:
    """PB-07 bulk: every test in scope (all, or a category), one PUT each; old prices are in the preview
    (rows + snapshot), so an undo is a new PB-07 back to them. Any new price out of range refuses it all."""
    rows, pinned = await _lab_tests(read)
    scope = params["scope"]
    if scope != "all":
        cats = sorted({str(t.get("category")) for t in rows if t.get("category")})
        rows = [t for t in rows if scope in str(t.get("category") or "").lower()]
        if not rows:
            raise EmptyPlan(f'No test category matches "{scope}". Categories: {", ".join(cats[:12]) or "none set"}.')
    step = int(params.get("round") or 1)
    plan = []
    for t in rows:
        new = new_price(_price(t), ["percent", params["price"]], step)
        _check_price(t, new)
        if new != _price(t):
            plan.append((t, new))
    if not plan:
        raise EmptyPlan("Every test in that list already has that price.")
    pct = Decimal(params["price"])
    bullets = [f"{_plural(len(plan), 'test')} ({'all tests' if scope == 'all' else 'category ' + scope}): "
               f"{'+' if pct > 0 else '-'}{format(abs(pct).normalize(), 'f')}%, rounded to the nearest ₹{step}."]
    if len(rows) > len(plan):
        bullets.append(f"{_plural(len(rows) - len(plan), 'test')} keep their price after rounding.")
    if pinned:
        bullets.append("Only your branch's tests and the shared ones.")
    return {"title": f"Change {_plural(len(plan), 'lab test price')}", "bullets": bullets,
            "warnings": [_PRICE_WARNING, "There is no automatic undo: the old prices are listed here."],
            "rows": [[str(t.get("name")), present.fmt(t.get("price_paise"), "paise"), _inr(new)] for t, new in plan[:25]],
            "snapshot": sorted((str(t["id"]), t.get("price_paise")) for t, _ in plan),
            "steps": [_price_step(t, new) for t, new in plan]}


async def lab_create_preview(read, params: dict) -> dict:
    name, price = params["name"], params["price"]
    if price[0] != "amount":
        raise EmptyPlan("Say the new test's price in rupees, e.g. ₹450.")
    new = new_price(Decimal(0), price)
    _check_price({"name": name}, new)
    rows = await _get(read, "/admin/lab-tests")
    same = next((t for t in rows if isinstance(t, dict) and str(t.get("name") or "").strip().lower() == name.lower()),
                None) if isinstance(rows, list) else None
    if same:
        raise EmptyPlan(f"{_test_line(same)} is already in the Test Catalogue. {_LAB_PAGE}")

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/lab-tests"))
        if out.status != "ok" or not isinstance(out.data, list):
            return None, "Couldn't read the Test Catalogue back"
        row = next((r for r in out.data if isinstance(r, dict) and r.get("branch_id") is None
                    and str(r.get("name") or "").strip().lower() == name.lower()), None)
        if row is None:
            return False, "Not in the Test Catalogue"
        if int(row.get("price_paise") or 0) != new * 100:
            return False, "The Test Catalogue shows a different price"
        return True, f"Added at {_inr(new)}"

    return {"title": f"Add lab test {name} at {_inr(new)}",
            "bullets": ["Offered at every branch and active; add its category and sample type on the page."],
            "warnings": ["Patients can book it on WhatsApp straight away."],
            "steps": [_step(f"Add {name}", _call("POST", "/admin/lab-tests", json={"name": name, "price_rupees": new}),
                            verify=verify)]}


async def lab_deactivate_preview(read, params: dict) -> dict:
    rows, _ = await _lab_tests(read)
    t = _one_test(rows, params["test"])
    if t.get("is_active") is False:
        raise EmptyPlan(f"{t.get('name')} is already inactive.")
    return {"title": f"Stop offering {t.get('name')}",
            "bullets": [f"{_test_line(t)}. It stays in the catalogue and can be switched back on the page."],
            "warnings": ["Patients can no longer book it on WhatsApp; bookings already made are not changed."],
            "snapshot": {"id": str(t["id"]), "is_active": t.get("is_active")},
            "steps": [_step(f"Deactivate {t.get('name')}",
                            _call("PUT", f"/admin/lab-tests/{t['id']}", json={"is_active": False}),
                            verify=_lab_row(t["id"], lambda r: r.get("is_active") is False, "Inactive"))]}


async def collection_window_preview(read, params: dict) -> dict:
    """Changes only the hours: the days and any Sunday hours are sent back as read. A pinned user sets
    their branch's own hours; anyone else the clinic's (branches with their own hours keep them)."""
    me = await _get(read, "/admin/me")
    mine = str(me.get("branch_id")) if isinstance(me, dict) and me.get("branch_id") else None
    query = {"branch_id": mine} if mine else {}
    data = await _get(read, "/admin/lab-collection-window", query)
    cur = (data.get("lab_collection") if isinstance(data, dict) else None) or {}
    start, end = params["window"].split("-")
    if (cur.get("start"), cur.get("end")) == (start, end):
        raise EmptyPlan(f"Sample collection hours are already {start} to {end}.")
    body = {"start": start, "end": end, "days": cur.get("days") or "Mon,Tue,Wed,Thu,Fri,Sat"}
    if cur.get("sunday_start") and cur.get("sunday_end"):
        body.update(sunday_start=cur["sunday_start"], sunday_end=cur["sunday_end"])
    bullets = [f"Days stay {body['days']}."]
    if "sunday_start" in body:
        bullets.append(f"Sunday hours stay {body['sunday_start']} to {body['sunday_end']}.")

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/lab-collection-window", query))
        got = out.data.get("lab_collection") if out.status == "ok" and isinstance(out.data, dict) else None
        if not isinstance(got, dict):
            return None, "Couldn't read the collection hours back"
        ok = (got.get("start"), got.get("end")) == (start, end)
        return ok, f"Now {start} to {end}" if ok else f"Still {got.get('start')} to {got.get('end')}"

    return {"title": f"Sample collection hours: {cur.get('start') or '—'}–{cur.get('end') or '—'} → {start}–{end}",
            "bullets": bullets,
            "warnings": ["For your branch only." if mine else "Branches with their own hours keep them.",
                         "Patients choosing a collection slot on WhatsApp see the new hours straight away."],
            "snapshot": cur,
            "steps": [_step("Save collection hours", _call("PUT", "/admin/lab-collection-window", query, json=body),
                            verify=verify)]}


# ── KK-P4-09: PB-08 clean-up (list + suggested fasting/prep fixes), auto-classify ──
# Owner (2026-10-15): price fixes and duplicate deletions stay on the page (each needs a person's
# choice per row); PB-09 import is a hand-off to the page's Import Price List (help.py).

MAX_PREP_FIXES = 200
_CLEANUP = "/admin/lab-tests/cleanup-suggestions"


def cleanup_call(params: dict) -> list:
    return [_step("Read", _call("GET", _CLEANUP))]


def cleanup_present(outcomes: list, params: dict) -> dict:
    out = present.metrics("Catalogue clean-up suggestions", "labtests", outcomes[0].data, (
        ("Tests checked", "summary.total_tests_analyzed", "text"),
        ("Duplicate groups", "summary.duplicate_groups_count", "text"),
        ("Suspicious prices", "summary.suspicious_prices_count", "text"),
        ("Missing fasting instructions", "summary.missing_prep_count", "text"),
        ("Without a heading", "summary.uncategorized_count", "text")))
    out["message"] += (" Kriya can add the missing fasting instructions and file unfiled tests; duplicates and "
                       "prices are fixed with Clean-Up Suggestions on the Test Catalogue page.")
    return out


async def fix_prep_preview(read, params: dict) -> dict:
    """The suggested fasting/prep fixes only, for tests this user may edit (one cleanup-apply call;
    CAT-05: never a deletion or a price fix)."""
    data = await _get(read, _CLEANUP)
    rows, _ = await _lab_tests(read)
    mine = {str(t.get("id")) for t in rows}
    fixes = [f for f in (data.get("missing_prep") or [] if isinstance(data, dict) else [])
             if isinstance(f, dict) and str(f.get("id")) in mine and f.get("suggested_prep_instructions")]
    if not fixes:
        raise EmptyPlan("No test is missing its fasting instructions.")
    body = {"delete_duplicate_ids": [], "price_fixes": [],
            "prep_fixes": [{"test_id": str(f["id"]), "fasting_required": True,
                            "preparation": str(f["suggested_prep_instructions"])[:500]} for f in fixes]}
    want = {p["test_id"]: p["preparation"] for p in body["prep_fixes"]}

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/lab-tests"))
        if out.status != "ok" or not isinstance(out.data, list):
            return None, "Couldn't read the Test Catalogue back"
        got = {str(r.get("id")): r for r in out.data if isinstance(r, dict) and str(r.get("id")) in want}
        done = sum(1 for i, prep in want.items()
                   if got.get(i, {}).get("fasting_required") is True and got.get(i, {}).get("prep_instructions") == prep)
        return done == len(want), f"{done} of {len(want)} tests now have fasting instructions"

    return {"title": f"Add fasting instructions to {_plural(len(fixes), 'test')}",
            "bullets": [f"{f.get('name')}: {str(f['suggested_prep_instructions'])[:80]}" for f in fixes[:8]]
            + ([f"…and {len(fixes) - 8} more."] if len(fixes) > 8 else []),
            "warnings": ["Patients booking these tests are told to fast. Duplicates and prices are not touched."],
            "affected": len(fixes), "snapshot": body,
            "steps": [_step(f"Add fasting instructions ({len(fixes)})",
                            _call("POST", "/admin/lab-tests/cleanup-apply", json=body), verify=verify)]}


async def auto_classify_preview(read, params: dict) -> dict:
    """The endpoint's own dry run (apply=false) is the preview; only tests without a heading are
    ever filed. Read-back: the tests unfiled at the preview are filed afterwards."""
    proposal = await _get_post(read, "/admin/lab-tests/auto-classify", {"apply": False})
    n = int(proposal.get("unfiled") or 0) if isinstance(proposal, dict) else 0
    if not n:
        raise EmptyPlan("Every test Kriya can file already has a heading.")
    rows = await _get(read, "/admin/lab-tests")
    before = sorted(str(t.get("id")) for t in rows if isinstance(t, dict) and not t.get("category")) \
        if isinstance(rows, list) else []
    me = await _get(read, "/admin/me")
    groups = [g for g in proposal.get("proposal") or [] if isinstance(g, dict)]

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/lab-tests"))
        if out.status != "ok" or not isinstance(out.data, list):
            return None, "Couldn't read the Test Catalogue back"
        was = set(before)
        filed = sum(1 for r in out.data if isinstance(r, dict) and str(r.get("id")) in was and r.get("category"))
        return filed >= n, f"{filed} tests now have a heading"

    warnings = ["Only tests without a heading are filed; a heading someone chose is never changed."]
    if isinstance(me, dict) and me.get("branch_id"):
        warnings.append("This files unfiled tests at every branch, not only yours.")
    return {"title": f"File {_plural(n, 'test')} under a heading",
            "bullets": [f"{g.get('category')}: {g.get('count')} (e.g. {', '.join(map(str, (g.get('examples') or [])[:3]))})"
                        for g in groups[:8]],
            "warnings": warnings, "affected": n, "snapshot": {"proposal": groups, "unfiled": before},
            "steps": [_step(f"File {_plural(n, 'test')}",
                            _call("POST", "/admin/lab-tests/auto-classify", json={"apply": True}), verify=verify)]}


async def _get_post(read, path: str, body: dict):
    """A read-only POST (registry.read_only_post) through the preview reader."""
    out = await read(_call("POST", path, json=body))
    if out.status != "ok":
        raise RuntimeError(f"read {path} failed")
    return out.data


# ── KK-P4-10: PB-10 treatments, PB-15 dental plans ──
# Treatment AI descriptions are a hand-off (help.py): the executor never stores response bodies, so a
# draft could not be shown for review.

MAX_TREATMENT_BULK = 100     # PB-10 cap
MAX_TREATMENT_PRICE = 10_000_000  # TreatmentUpdate.price_from_rupees le=1 crore
_TRT_PAGE = "Change it on the Treatments page."


def _trt_line(t: dict) -> str:
    return f"{t.get('name')} ({_inr(t.get('price_from_rupees') or 0)}{'' if t.get('is_active', True) else ', hidden'})"


async def _treatments(read) -> list:
    rows = await _get(read, "/admin/treatments")
    return [t for t in rows if isinstance(t, dict)] if isinstance(rows, list) else []


def _one_treatment(rows: list, term: str) -> dict:
    return _one_test(rows, term, noun="treatment", line=_trt_line, page=_TRT_PAGE)


def _trt_row(tid, ok, said: str):
    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/treatments"))
        if out.status != "ok" or not isinstance(out.data, list):
            return None, "Couldn't read the Treatments list back"
        row = next((r for r in out.data if isinstance(r, dict) and str(r.get("id")) == str(tid)), None)
        if row is None:
            return None, "Couldn't find the treatment to check it"
        return (True, said) if ok(row) else (False, "The Treatments list shows a different value")
    return verify


def _trt_price_step(t: dict, new: int):
    return _step(f"{t.get('name')}: {_inr(t.get('price_from_rupees') or 0)} → {_inr(new)}",
                 _call("PUT", f"/admin/treatments/{t['id']}", json={"price_from_rupees": new}),
                 verify=_trt_row(t["id"], lambda r: r.get("price_from_rupees") == new, f"Price from {_inr(new)}"))


_TRT_PRICE_WARNING = "Patients see the new \"from\" price on WhatsApp straight away; bookings already made keep theirs."


async def treatment_price_preview(read, params: dict) -> dict:
    t = _one_treatment(await _treatments(read), params["treatment"])
    old = Decimal(int(t.get("price_from_rupees") or 0))
    new = new_price(old, params["price"])
    _check_price(t, new, MAX_TREATMENT_PRICE)
    if new == old:
        raise EmptyPlan(f"{t.get('name')} is already from {_inr(new)}.")
    return {"title": f"Change {t.get('name')}: from {_inr(old)} → {_inr(new)}",
            "rows": [[str(t.get("name")), _inr(old), _inr(new)]], "warnings": [_TRT_PRICE_WARNING],
            "snapshot": {"id": str(t["id"]), "price": t.get("price_from_rupees")}, "steps": [_trt_price_step(t, new)]}


async def treatment_price_bulk_preview(read, params: dict) -> dict:
    rows = await _treatments(read)
    scope = params["scope"]
    if scope != "all":
        cats = sorted({str(t.get("category")) for t in rows if t.get("category")})
        rows = [t for t in rows if scope in str(t.get("category") or "").lower()
                or all(w in str(t.get("name") or "").lower() for w in scope.split())]
        if not rows:
            raise EmptyPlan(f'No treatment or category matches "{scope}". Categories: {", ".join(cats[:12]) or "none set"}.')
    step = int(params.get("round") or 1)
    plan = []
    for t in rows:
        old = Decimal(int(t.get("price_from_rupees") or 0))
        if old <= 0:
            continue  # "price on consultation": a percent of nothing stays nothing
        new = new_price(old, ["percent", params["price"]], step)
        _check_price(t, new, MAX_TREATMENT_PRICE)
        if new != old:
            plan.append((t, new))
    if not plan:
        raise EmptyPlan("No treatment in that list has a price to change.")
    pct = Decimal(params["price"])
    return {"title": f"Change {_plural(len(plan), 'treatment price')}",
            "bullets": [f"{'+' if pct > 0 else '-'}{format(abs(pct).normalize(), 'f')}%, rounded to the nearest ₹{step}; "
                        "treatments without a price are left alone."],
            "warnings": [_TRT_PRICE_WARNING, "There is no automatic undo: the old prices are listed here."],
            "rows": [[str(t.get("name")), _inr(t.get("price_from_rupees") or 0), _inr(new)] for t, new in plan[:25]],
            "snapshot": sorted((str(t["id"]), t.get("price_from_rupees")) for t, _ in plan),
            "steps": [_trt_price_step(t, new) for t, new in plan]}


async def treatment_status_preview(read, params: dict) -> dict:
    t = _one_treatment(await _treatments(read), params["treatment"])
    show = params["switch"] == "show"
    if bool(t.get("is_active", True)) == show:
        raise EmptyPlan(f"{t.get('name')} is already {'shown' if show else 'hidden'}.")
    return {"title": f"{'Show' if show else 'Hide'} {t.get('name')}",
            "warnings": ["Patients can book it on WhatsApp again." if show
                         else "Patients can no longer choose it on WhatsApp; bookings already made are not changed."],
            "snapshot": {"id": str(t["id"]), "is_active": t.get("is_active")},
            "steps": [_step(f"{'Show' if show else 'Hide'} {t.get('name')}",
                            _call("POST", "/admin/treatments/status", json={"treatment_ids": [str(t["id"])],
                                                                             "is_active": show}),
                            verify=_trt_row(t["id"], lambda r: bool(r.get("is_active")) == show,
                                            "Shown" if show else "Hidden"))]}


async def _doctor_by_name(read, name: str) -> tuple:
    doctors = await _get(read, "/admin/doctors")
    doctors = [d for d in doctors if isinstance(d, dict)] if isinstance(doctors, list) else []
    doc = next((d for d in doctors if d.get("name") == name), None)
    if not doc:
        raise EmptyPlan(f"{name} is not on the Doctors list.")
    return doc, {str(d.get("id")): d.get("name") for d in doctors}


async def treatment_doctors_preview(read, params: dict) -> dict:
    t = _one_treatment(await _treatments(read), params["treatment"])
    doc, names = await _doctor_by_name(read, params["doctor"])
    before = sorted(str(d) for d in t.get("doctor_ids") or [])
    want = [str(doc["id"])]
    if before == want:
        raise EmptyPlan(f"Only {doc.get('name')} already does {t.get('name')}.")
    now_text = ", ".join(names.get(d, "a removed doctor") for d in before) or "any active doctor"
    return {"title": f"{t.get('name')}: only {doc.get('name')}",
            "bullets": [f"Now: {now_text}.", f"After: {doc.get('name')} only."],
            "warnings": ["Patients booking it on WhatsApp are offered only this doctor."],
            "snapshot": {"id": str(t["id"]), "doctor_ids": before},
            "steps": [_step(f"Set doctors for {t.get('name')}",
                            _call("PUT", f"/admin/treatments/{t['id']}/doctors", json={"doctor_ids": want}),
                            verify=_trt_row(t["id"], lambda r: sorted(map(str, r.get("doctor_ids") or [])) == want,
                                            f"Only {doc.get('name')}"))]}


# ── PB-15 dental: plan (+ first sitting) and next sitting ──

def _last4(phone) -> str:
    return "…" + re.sub(r"\D", "", str(phone or ""))[-4:]


async def _free_sitting(read, params: dict) -> dict:
    """The sitting's doctor, day and time, checked against the dentist's free times (holiday, leave,
    off-day and taken slots removed by GET /admin/dental/slots: CAT-10). Refuses anything else."""
    if not params.get("doctor"):
        raise EmptyPlan("Say the doctor for the sitting, e.g. \"with Dr Rao\".")
    if not params.get("date"):
        raise EmptyPlan("Say the day of the sitting, e.g. \"Monday at 10:30\".")
    if params["date"] < _now().date().isoformat():
        raise EmptyPlan(f"{present.fmt(params['date'], 'date')} has already passed.")
    doc, _ = await _doctor_by_name(read, params["doctor"])
    data = await _get(read, "/admin/dental/slots", {"doctor_id": str(doc["id"]), "date": params["date"]})
    slots = [str(s)[:5] for s in (data.get("slots") or [] if isinstance(data, dict) else [])]
    day = present.fmt(params["date"], "date")
    if not slots:
        reason = (data.get("reason") if isinstance(data, dict) else None) or "no free time that day"
        raise EmptyPlan(f"{doc.get('name')} can't take a sitting on {day}: {reason}")
    if params.get("time") not in slots:
        free = ", ".join(slots[:12]) + (" …" if len(slots) > 12 else "")
        asked = f"{params['time']} is not free. " if params.get("time") else ""
        raise EmptyPlan(f"{asked}Free times with {doc.get('name')} on {day}: {free}. Say the time, e.g. \"at {slots[0]}\".")
    return {"doctor_id": str(doc["id"]), "doctor_name": doc.get("name"), "date": params["date"], "time": params["time"]}


def _sitting_line(s: dict) -> str:
    return f"{present.fmt(s['date'], 'date')} at {s['time']} with {s['doctor_name']}"


def _has_sitting(s: dict, sittings) -> bool:
    return any(str(x.get("appointment_date"))[:10] == s["date"] and str(x.get("appointment_time") or "")[:5] == s["time"]
               and x.get("status") != "cancelled" for x in sittings or [] if isinstance(x, dict))


async def _plan_sittings(read, pid: str):
    out = await read(_call("GET", f"/admin/dental/plans/{pid}"))
    return out.data.get("sittings") if out.status == "ok" and isinstance(out.data, dict) else None


async def dental_plan_preview(read, params: dict) -> dict:
    t = _one_treatment(await _treatments(read), params["treatment"])
    digits = re.sub(r"\D", "", params["phone"])[-10:]
    sittings = int(params.get("sittings") or t.get("default_sittings") or 1)
    body = {"patient_phone": params["phone"], "patient_name": params["name"], "treatment_id": str(t["id"]),
            "planned_sittings": sittings, "whatsapp_consent": params["whatsapp_consent"] == "yes",
            "notify_patient": True, "notify_doctor": True}
    quote = params.get("quote")
    if quote:
        body["quoted_amount_rupees"] = int(Decimal(quote[1]))
    bullets = [_plural(sittings, "sitting") + (f", quoted {_inr(body['quoted_amount_rupees'])}" if quote else "") + "."]
    first = None
    if params.get("date") or params.get("time"):
        first = await _free_sitting(read, params)
        body["first_sitting"] = {"doctor_id": first["doctor_id"], "date": first["date"], "time": first["time"],
                                 "send_patient": True, "notify_doctor": True}
        bullets.append(f"First sitting {_sitting_line(first)}.")
    bullets.append("WhatsApp: the patient agreed." if body["whatsapp_consent"] else
                   "WhatsApp: no consent recorded; the patient is messaged only if they have written to the clinic before.")
    existing = await _get(read, "/admin/dental/plans", {"q": digits, "limit": 50, "offset": 0})
    before = sorted(str(p.get("id")) for p in (existing.get("plans") or [] if isinstance(existing, dict) else [])
                    if isinstance(p, dict))
    warnings = [f"{_plural(len(before), 'plan')} already exist for this number."] if before else []

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/dental/plans", {"q": digits, "limit": 50, "offset": 0}))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the treatment plans back"
        new = [p for p in out.data.get("plans") or [] if isinstance(p, dict) and str(p.get("id")) not in before]
        if not new:
            return False, "No new plan for this number"
        if len(new) > 1:
            return None, "Several new plans for this number"
        if not first:
            return True, "Plan saved"
        found = await _plan_sittings(read, str(new[0]["id"]))
        if found is None:
            return None, "Plan saved; couldn't read its sittings"
        return (True, "Plan saved, first sitting booked") if _has_sitting(first, found) \
            else (False, "Plan saved, but the first sitting was not booked")

    return {"title": f"{t.get('name')} plan for {params['name']} ({_last4(params['phone'])})",
            "bullets": bullets, "warnings": warnings, "snapshot": {"plans": before, "slot": first},
            "steps": [_step("Save the plan" + (" and book the first sitting" if first else ""),
                            _call("POST", "/admin/dental/plans", json=body), verify=verify)]}


async def dental_sitting_preview(read, params: dict) -> dict:
    who = params["who"]
    data = await _get(read, "/admin/dental/plans", {"q": who, "status": "active", "limit": 50, "offset": 0})
    plans = [p for p in (data.get("plans") or [] if isinstance(data, dict) else []) if isinstance(p, dict)]
    if not plans:
        raise EmptyPlan(f'No active treatment plan matches "{who}".')
    if len(plans) > 1:
        listed = "; ".join(f"{p.get('treatment_name')} for {p.get('patient_name')} ({_last4(p.get('patient_phone'))})"
                           for p in plans[:5])
        raise EmptyPlan(f'{len(plans)} active plans match "{who}": {listed}. Say the full name or the last digits of the phone.')
    p = plans[0]
    s = await _free_sitting(read, params)
    pid = str(p["id"])

    async def verify(read, data, call):
        found = await _plan_sittings(read, pid)
        if found is None:
            return None, "Couldn't read the plan's sittings back"
        return (True, f"Booked {_sitting_line(s)}") if _has_sitting(s, found) else (False, "Not on the plan's sittings")

    return {"title": f"Next {p.get('treatment_name')} sitting for {p.get('patient_name')}",
            "bullets": [f"{_sitting_line(s)}."],
            "warnings": ["The patient gets a WhatsApp confirmation if they agreed to messages."],
            "snapshot": {"plan": pid, "slot": s},
            "steps": [_step("Book the sitting", _call("POST", f"/admin/dental/plans/{pid}/sittings", json={
                "doctor_id": s["doctor_id"], "date": s["date"], "time": s["time"], "send_patient": True,
                "notify_doctor": True}), verify=verify)]}


# ── KK-P4-11: PB-11 doctor schedule change, doctor create, branch assignment ──
# PUT /admin/doctors regenerates both shifts from the fields it is sent and blanks a shift that is
# missing, at a 30-minute default unless the duration is sent (D-34). Kriya always sends the full
# current shift fields and the doctor's own duration, as the Doctors page does, and never a branch field.

SCHEDULE_LOOKAHEAD_DAYS = 60
MAX_CONSULTATION_FEE = 100_000  # Kriya's own bound; DoctorCreate has none (D-34)
_SHIFTS = ("morning_start", "morning_end", "evening_start", "evening_end")


def _hm(value):
    return str(value)[:5] if value else None


def _slots(start: str, end: str, minutes: int) -> list:
    from app.utils.helpers import generate_slots  # the server's own generator (SCH-03)
    return generate_slots(time.fromisoformat(start), time.fromisoformat(end), minutes)


async def _doctor(read, name: str) -> dict:
    doctors = await _get(read, "/admin/doctors")
    doc = next((d for d in doctors if isinstance(d, dict) and d.get("name") == name), None) \
        if isinstance(doctors, list) else None
    if not doc:
        raise EmptyPlan(f"{name} is not on the Doctors list.")
    return doc


def _doctor_after(did: str, ok, said: str):
    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/doctors"))
        if out.status != "ok" or not isinstance(out.data, list):
            return None, "Couldn't read the Doctors list back"
        row = next((d for d in out.data if isinstance(d, dict) and str(d.get("id")) == did), None)
        if row is None:
            return None, "Couldn't find the doctor to check"
        return (True, said) if ok(row) else (False, f"The Doctors list does not show {said}")
    return verify


async def doctor_fee_preview(read, params: dict) -> dict:
    doc = await _doctor(read, params["doctor"])
    cur = int(doc.get("consultation_fee") or 0)
    kind, value = params["fee"][0], Decimal(params["fee"][1])
    new = value if kind == "amount" else cur + value if kind == "delta" else cur * (1 + value / 100)
    fee = int(new.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if not 0 < fee <= MAX_CONSULTATION_FEE:
        raise EmptyPlan(f"A consultation fee of {_inr(fee)} is outside ₹1 to {_inr(MAX_CONSULTATION_FEE)}.")
    if fee == cur:
        raise EmptyPlan(f"{doc.get('name')}'s fee is already {_inr(cur)}.")
    return {"title": f"Change {doc.get('name')}'s fee", "bullets": [f"Consultation fee: {_inr(cur)} → {_inr(fee)}."],
            "warnings": ["New bookings and WhatsApp quotes use the new fee straight away."],
            "snapshot": {"id": str(doc["id"]), "fee": cur},
            "steps": [_step(f"Save {doc.get('name')}'s fee",
                            _call("PUT", f"/admin/doctors/{doc['id']}", json={"consultation_fee": fee}),
                            verify=_doctor_after(str(doc["id"]), lambda r: int(r.get("consultation_fee") or 0) == fee,
                                                 f"fee {_inr(fee)}"))]}


async def doctor_details_preview(read, params: dict) -> dict:
    doc = await _doctor(read, params["doctor"])
    body = {f: params[f] for f in ("specialization", "department")
            if params.get(f) and params[f].strip().lower() != str(doc.get(f) or "").strip().lower()}
    if not body:
        raise EmptyPlan(f"{doc.get('name')} already has those details.")
    bullets = [f"{f.capitalize()}: {doc.get(f) or 'none'} → {v}." for f, v in body.items()]
    return {"title": f"Change {doc.get('name')}'s details", "bullets": bullets,
            "warnings": ["Patients see this on WhatsApp straight away."],
            "snapshot": {"id": str(doc["id"]), **{f: doc.get(f) for f in body}},
            "steps": [_step(f"Save {doc.get('name')}'s details", _call("PUT", f"/admin/doctors/{doc['id']}", json=body),
                            verify=_doctor_after(str(doc["id"]), lambda r: all(r.get(f) == v for f, v in body.items()),
                                                 ", ".join(f"{f} {v}" for f, v in body.items())))]}


async def doctor_schedule_preview(read, params: dict) -> dict:
    doc = await _doctor(read, params["doctor"])
    session, (start, end) = params["session"], params["window"].split("-")
    cur = {k: _hm(doc.get(k)) for k in _SHIFTS}
    old = (cur[f"{session}_start"], cur[f"{session}_end"])
    if old == (start, end):
        raise EmptyPlan(f"{doc.get('name')}'s {session} is already {start} to {end}.")
    new = {**cur, f"{session}_start": start, f"{session}_end": end}
    minutes = int(doc.get("slot_duration_minutes") or 30)
    session_slots = _slots(start, end, minutes)
    keep = set(session_slots)
    other = "evening" if session == "morning" else "morning"
    if new[f"{other}_start"] and new[f"{other}_end"]:
        keep |= set(_slots(new[f"{other}_start"], new[f"{other}_end"], minutes))
    today = _now().date()
    booked = [a for a in await _bookings(read, doc["name"], today, today + timedelta(days=SCHEDULE_LOOKAHEAD_DAYS))
              if a.get("doctor_name") == doc["name"]]
    outside = [a for a in booked if str(a.get("appointment_time") or "")[:5] not in keep]
    was = f"{old[0]}–{old[1]}" if old[0] else "none"
    bullets = [f"{session.capitalize()}: {was} → {start}–{end} ({len(session_slots)} slots of {minutes} min).",
               f"The {other} stays {new[other + '_start']}–{new[other + '_end']}." if new[f"{other}_start"]
               else f"No {other} hours."]
    warnings = ["Patients booking on WhatsApp see the new times straight away."]
    if outside:
        warnings.insert(0, f"{_plural(len(outside), 'booking')} in the next {SCHEDULE_LOOKAHEAD_DAYS} days "
                           f"{'falls' if len(outside) == 1 else 'fall'} outside the new hours. They are not moved: "
                           "cancel or call them from the Appointments page.")
    body = {k: v for k, v in new.items() if v}
    body["slot_duration_minutes"] = minutes
    return {"title": f"Change {doc.get('name')}'s {session} hours", "bullets": bullets, "warnings": warnings,
            "rows": _booking_rows(outside), "snapshot": {"id": str(doc["id"]), "shifts": cur, "minutes": minutes,
                                                         "outside": sorted(str(a.get("id")) for a in outside)},
            "steps": [_step(f"Save {doc.get('name')}'s hours", _call("PUT", f"/admin/doctors/{doc['id']}", json=body),
                            verify=_doctor_after(str(doc["id"]), lambda r: (_hm(r.get(f"{session}_start")),
                                                                            _hm(r.get(f"{session}_end"))) == (start, end),
                                                 f"{session.capitalize()} {start}–{end}"))]}


def _plain_name(name) -> str:
    return re.sub(r"^dr\.?\s+", "", str(name or "").strip().lower())


def branch_rows(data, active_only: bool = False) -> list:
    """GET /admin/branches answers {"branches": [...]}; reading it as a bare list found no branches at all."""
    rows = data.get("branches") if isinstance(data, dict) else data
    rows = [b for b in rows if isinstance(b, dict)] if isinstance(rows, list) else []
    return [b for b in rows if b.get("is_active", True)] if active_only else rows


def _match_branch(branches: list, term: str) -> list:
    """An exact name wins, so "Main" is not ambiguous beside "Main Road"; else every name containing it."""
    exact = [b for b in branches if " ".join(str(b.get("name") or "").lower().split()) == term]
    return exact or [b for b in branches if term in str(b.get("name") or "").lower()]


async def doctor_create_preview(read, params: dict) -> dict:
    name, fee = params["name"], params["fee"]
    if fee[0] != "amount":
        raise EmptyPlan("Say the consultation fee in rupees, e.g. ₹600.")
    fee_rupees = int(Decimal(fee[1]))
    if not 0 < fee_rupees <= MAX_CONSULTATION_FEE:
        raise EmptyPlan(f"A consultation fee of {_inr(fee_rupees)} is outside ₹1 to {_inr(MAX_CONSULTATION_FEE)}.")
    windows = {s: params.get(s) for s in ("morning", "evening") if params.get(s)}
    if not windows:
        raise EmptyPlan("Say the hours, e.g. \"mornings 9 to 1\" or \"evenings 5 to 8\".")
    doctors = await _get(read, "/admin/doctors")
    doctors = [d for d in doctors if isinstance(d, dict)] if isinstance(doctors, list) else []
    same = next((d for d in doctors if _plain_name(d.get("name")) == _plain_name(name)), None)
    if same:
        raise EmptyPlan(f"{same.get('name')} is already on the Doctors list.")
    body = {"name": name, "specialization": params["specialization"], "department": params["department"],
            "consultation_fee": fee_rupees, "available_days": params["days"]}
    bullets = [f"{params['specialization']} ({params['department']}), {_inr(fee_rupees)}, {params['days']}."]
    for s, w in windows.items():
        body[f"{s}_start"], body[f"{s}_end"] = w.split("-")
        bullets.append(f"{s.capitalize()} {w.replace('-', '–')}: "
                       f"{len(_slots(body[f'{s}_start'], body[f'{s}_end'], 30))} slots of 30 min.")
    body["slot_duration_minutes"] = 30
    me = await _get(read, "/admin/me")
    branches = branch_rows(await _get(read, "/admin/branches"), active_only=True)
    warnings = ["Patients can book this doctor on WhatsApp straight away."]
    if isinstance(me, dict) and me.get("branch_id"):
        warnings.append("The doctor is assigned to your branch.")  # the server pins it; a picked branch is ignored
    elif params.get("branch"):
        found = _match_branch(branches, params["branch"])
        names = ", ".join(str(b.get("name")) for b in branches[:12]) or "none"
        if len(found) != 1:
            raise EmptyPlan(f'{"No branch" if not found else f"{len(found)} branches"} match "{params["branch"]}". '
                            f"Branches: {names}.")
        body["branch_id"] = str(found[0]["id"])
        bullets.append(f"Branch: {found[0].get('name')} (morning and evening).")
    elif len(branches) > 1:
        warnings.append("Your clinic has several branches: assign the doctor to one next (\"assign Dr … to … branch\").")
    before = sorted(str(d.get("id")) for d in doctors)

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/doctors"))
        if out.status != "ok" or not isinstance(out.data, list):
            return None, "Couldn't read the Doctors list back"
        new = [d for d in out.data if isinstance(d, dict) and d.get("name") == name and str(d.get("id")) not in before]
        return (True, "On the Doctors list") if len(new) == 1 else (False, "Not on the Doctors list")

    return {"title": f"Add {name}", "bullets": bullets, "warnings": warnings, "snapshot": {"doctors": before},
            "steps": [_step(f"Add {name}", _call("POST", "/admin/doctors", json=body), verify=verify)]}


async def branch_assign_preview(read, params: dict) -> dict:
    doc = await _doctor(read, params["doctor"])
    branches = branch_rows(await _get(read, "/admin/branches"))
    term = params["branch"]
    found = _match_branch(branches, term)
    names = ", ".join(str(b.get("name")) for b in branches[:12]) or "none"
    if len(found) != 1:
        raise EmptyPlan(f'{"No branch" if not found else f"{len(found)} branches"} match "{term}". Branches: {names}.')
    branch = found[0]
    bid, session = str(branch["id"]), params.get("session") or "both"
    path = f"/admin/branches/{bid}/doctors"
    current = await _get(read, path)
    assigned = {str(r.get("doctor_id")) for r in (current.get("doctor_branches") or [] if isinstance(current, dict) else [])
                if isinstance(r, dict)}
    if str(doc["id"]) in assigned:
        raise EmptyPlan(f"{doc.get('name')} is already assigned to {branch.get('name')}. Change the session on the "
                        "Branches page.")

    async def verify(read, data, call):
        out = await read(_call("GET", path))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the branch's doctors back"
        ok = any(isinstance(r, dict) and str(r.get("doctor_id")) == str(doc["id"])
                 for r in out.data.get("doctor_branches") or [])
        return ok, f"Assigned to {branch.get('name')}" if ok else "Not on the branch's doctors"

    label = {"both": "morning and evening", "morning": "mornings", "evening": "evenings"}[session]
    return {"title": f"Assign {doc.get('name')} to {branch.get('name')} ({label})",
            "warnings": ["Patients at that branch can book this doctor straight away."],
            "snapshot": {"assigned": sorted(assigned)},
            "steps": [_step(f"Assign {doc.get('name')}", _call("POST", path, json={"doctor_id": str(doc["id"]),
                                                                                  "session": session}),
                            verify=verify)]}


# ── KK-P4-13: voice receptionist knowledge, lexicon, learning review (VOICE_MANAGE) ──
# voice.settings_update is held for D-13 (KK-P0-08): only voice.auto_leads edits the settings object.

_VOICE_PAGE = "Do it on the AI Receptionist page."
_LEARN = "/admin/voice/learning/suggestions"
_LEX_LABEL = {"doctor_alias": "doctor", "test_alias": "test", "specialty_synonym": "department"}


def _same(a, b) -> bool:
    return " ".join(str(a or "").lower().split()) == " ".join(str(b or "").lower().split())


async def _voice_get(read, path: str, query: dict = None):
    """A Voice View read; without that permission, a clear refusal instead of "unavailable"."""
    out = await read(_call("GET", path, query or {}))
    if out.status != "ok":
        if out.error_code == "forbidden":
            raise EmptyPlan("You need the Voice View permission too. Ask your clinic admin.")
        raise RuntimeError(f"read {path} failed")
    return out.data


def _clinical(*texts) -> bool:
    from app.services.clinical_firewall import screen_message
    return any(screen_message(t)[0] for t in texts)


def _entries(data) -> list:
    return [e for e in (data.get("entries") or [] if isinstance(data, dict) else []) if isinstance(e, dict)]


def _listed(path: str, ok, said: str, key: str = "entries", query: dict = None):
    """Read `path` back; True when some row satisfies `ok`."""
    async def verify(read, data, call):
        out = await read(_call("GET", path, query or {}))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read it back"
        hit = any(ok(r) for r in out.data.get(key) or [] if isinstance(r, dict))
        return (True, said) if hit else (False, "Not saved")
    return verify


def _gone(path: str, eid: str, said: str):
    async def verify(read, data, call):
        out = await read(_call("GET", path))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read it back"
        left = any(isinstance(r, dict) and str(r.get("id")) == eid for r in out.data.get("entries") or [])
        return (not left), said if not left else "Still there"
    return verify


async def knowledge_add_preview(read, params: dict) -> dict:
    """KnowledgeIn: the staff member's own question and answer, sent as typed (never composed)."""
    q, a = params["question"], params["answer"]
    if not (3 <= len(q) <= 200 and 2 <= len(a) <= 600):
        raise EmptyPlan("The question must be 3-200 characters and the answer 2-600.")
    if _clinical(q, a):  # POST /admin/voice/knowledge itself does not screen (D-35)
        raise EmptyPlan("That mentions medicines or diagnosis; the receptionist cannot say that.")
    rows = _entries(await _voice_get(read, "/admin/voice/knowledge"))
    if any(_same(e.get("question"), q) for e in rows):
        raise EmptyPlan(f'The receptionist already has an answer for "{q}". Remove it first, or change it on the '
                        "AI Receptionist page.")
    return {"title": "Teach the receptionist an answer", "bullets": [f'When a caller asks "{q}"', f'it says "{a}"'],
            "warnings": ["Callers hear this answer from the next call on."],
            "snapshot": {"entries": len(rows)},
            "steps": [_step("Save the answer", _call("POST", "/admin/voice/knowledge", json={"question": q, "answer": a}),
                            verify=_listed("/admin/voice/knowledge",
                                           lambda e: _same(e.get("question"), q) and _same(e.get("answer"), a),
                                           "The receptionist has the answer"))]}


async def knowledge_delete_preview(read, params: dict) -> dict:
    topic = params["topic"]
    rows = _entries(await _voice_get(read, "/admin/voice/knowledge"))
    found = [e for e in rows if all(w in str(e.get("question") or "").lower() for w in topic.split())]
    if len(found) != 1:
        listed = "; ".join(f'"{e.get("question")}"' for e in found[:5])
        raise EmptyPlan(f'No saved answer is about "{topic}".' if not found
                        else f'{len(found)} saved answers match "{topic}": {listed}. Use more of the question.')
    e = found[0]
    eid = str(e.get("id"))
    return {"title": "Remove a receptionist answer", "bullets": [f'"{e.get("question")}" → "{e.get("answer")}"'],
            "warnings": ["Callers who ask this get the receptionist's general answer instead."],
            "snapshot": {"id": eid, "question": e.get("question"), "answer": e.get("answer")},
            "steps": [_step("Remove the answer", _call("DELETE", f"/admin/voice/knowledge/{eid}"),
                            verify=_gone("/admin/voice/knowledge", eid, "Removed"))]}


def _target(catalogue, term: str) -> list:
    """(kind, name) of this clinic's own doctors / tests / departments the words name; exact first."""
    def plain(s):
        return re.sub(r"^dr\.?\s+", "", " ".join(str(s).lower().split()))
    want = plain(term)
    pairs = [(k, n) for k in _LEX_LABEL for n in (catalogue or {}).get(k) or []]
    exact = [p for p in pairs if plain(p[1]) == want]
    return exact or [p for p in pairs if want and all(w in plain(p[1]).split() for w in want.split())]


async def lexicon_add_preview(read, params: dict) -> dict:
    """LexiconIn: the target must be one of this clinic's own records (the Learning form's catalogue);
    its kind follows from which list it is on. Pronunciations stay on the page."""
    phrase, term = params["phrase"], params["target"]
    if not 1 <= len(phrase) <= 80:
        raise EmptyPlan("The phrase must be 1-80 characters.")
    if _clinical(phrase):
        raise EmptyPlan("That phrase trips the clinical safety screen.")
    data = await _voice_get(read, _LEARN)
    hits = _target(data.get("catalogue") if isinstance(data, dict) else None, term)
    if len(hits) != 1:
        listed = "; ".join(f"{n} ({_LEX_LABEL[k]})" for k, n in hits[:6])
        raise EmptyPlan(f'"{term}" is not one of this clinic\'s doctors, tests or departments.' if not hits
                        else f'"{term}" could be {listed}. Use the full name.')
    kind, canonical = hits[0]
    existing = _entries(await _voice_get(read, "/admin/voice/lexicon"))
    if any(e.get("kind") == kind and _same(e.get("phrase"), phrase) for e in existing):
        raise EmptyPlan(f'The receptionist already knows "{phrase}".')
    body = {"kind": kind, "phrase": phrase, "canonical": canonical}
    return {"title": f'Teach the receptionist "{phrase}"',
            "bullets": [f'Callers saying "{phrase}" mean the {_LEX_LABEL[kind]} {canonical}.'],
            "warnings": ["The receptionist (and Kriya Ketu's doctor matching) uses this from the next call on."],
            "snapshot": {"existing": len(existing)},
            "steps": [_step("Save the phrase", _call("POST", "/admin/voice/lexicon", json=body),
                            verify=_listed("/admin/voice/lexicon",
                                           lambda e: e.get("kind") == kind and _same(e.get("phrase"), phrase)
                                           and e.get("canonical") == canonical, "The receptionist knows the phrase"))]}


async def lexicon_delete_preview(read, params: dict) -> dict:
    phrase = params["phrase"]
    found = [e for e in _entries(await _voice_get(read, "/admin/voice/lexicon")) if _same(e.get("phrase"), phrase)]
    if len(found) != 1:
        raise EmptyPlan(f'The receptionist has no phrase "{phrase}".' if not found
                        else f'"{phrase}" is saved {len(found)} times. {_VOICE_PAGE}')
    e = found[0]
    eid = str(e.get("id"))
    return {"title": f'Remove the phrase "{e.get("phrase")}"',
            "bullets": [f'Callers saying "{e.get("phrase")}" will no longer be matched to {e.get("canonical")}.'],
            "snapshot": {"id": eid, "canonical": e.get("canonical")},
            "steps": [_step("Remove the phrase", _call("DELETE", f"/admin/voice/lexicon/{eid}"),
                            verify=_gone("/admin/voice/lexicon", eid, "Removed"))]}


async def _suggestion(read, phrase: str) -> dict:
    rows = [s for s in ((await _voice_get(read, _LEARN)) or {}).get("suggestions") or [] if isinstance(s, dict)]
    found = [s for s in rows if _same(s.get("patient_phrase"), phrase)] or \
        [s for s in rows if all(w in str(s.get("patient_phrase") or "").lower() for w in phrase.lower().split())]
    if len(found) != 1:
        listed = "; ".join(f'"{s.get("patient_phrase")}"' for s in found[:5])
        raise EmptyPlan(f'No suggestion waiting for review matches "{phrase}".' if not found
                        else f'{len(found)} suggestions match "{phrase}": {listed}. Use the whole phrase.')
    return found[0]


def _reviewed(sid: str, status: str, said: str):
    return _listed(_LEARN, lambda s: str(s.get("id")) == sid, said, key="suggestions", query={"status": status})


async def learning_approve_preview(read, params: dict) -> dict:
    """Approved exactly as suggested (no edits): a corrected phrase or target, and every FAQ (it needs the
    answer patients hear), are done on the page. The endpoint claims the suggestion by CAS."""
    s = await _suggestion(read, params["phrase"])
    phrase, sid = s.get("patient_phrase"), str(s.get("id"))
    if not s.get("safety_screen_passed"):
        raise EmptyPlan(f'"{phrase}" was blocked by the clinical safety screen: it can only be dismissed.')
    if s.get("category") not in _LEX_LABEL:
        raise EmptyPlan(f'"{phrase}" needs the answer patients should hear. Write it in Learning on the AI '
                        "Receptionist page.")
    if not s.get("proposed_canonical"):
        raise EmptyPlan(f'"{phrase}" has no suggested match. Choose one in Learning on the AI Receptionist page.')
    return {"title": f'Approve "{phrase}"',
            "bullets": [f'Callers saying "{phrase}" will mean the {_LEX_LABEL[s["category"]]} '
                        f'{s["proposed_canonical"]} (heard {s.get("evidence_count") or 1} times).'],
            "warnings": ["The receptionist uses this from the next call on."],
            "snapshot": {"id": sid, "status": s.get("status"), "canonical": s.get("proposed_canonical")},
            "steps": [_step("Approve", _call("POST", f"{_LEARN}/{sid}/approve", json={}),
                            verify=_reviewed(sid, "approved", "Approved"))]}


async def learning_dismiss_preview(read, params: dict) -> dict:
    s = await _suggestion(read, params["phrase"])
    sid = str(s.get("id"))
    return {"title": f'Dismiss "{s.get("patient_phrase")}"',
            "bullets": ["The receptionist will not learn this phrase."],
            "snapshot": {"id": sid, "status": s.get("status")},
            "steps": [_step("Dismiss", _call("POST", f"{_LEARN}/{sid}/dismiss"),
                            verify=_reviewed(sid, "dismissed", "Dismissed"))]}


# ── KK-P4-14: PB-12 morning briefing, PB-13 end of day (R0, on demand) ──
# Each section is one optional read of its page's endpoint as the caller: a section the route refuses
# (permission, feature off) is left out (BRF-01). Counts and doctor names only, never a patient.
# shortcut: per-doctor counts and first times come from the first 100 bookings (the list's page size).

def _brief_day(offset: int = 0) -> str:
    return (_now().date() + timedelta(days=offset)).isoformat()


def _confirmed_on(day: str, status: str = "confirmed", last: str = None):
    return _call("GET", "/admin/appointments", {"date_basis": "visit", "date_from": day, "date_to": last or day,
                                               "status": status, "limit": PAGE, "offset": 0})


def _total(data, key: str) -> int:
    rows = data.get(key) if isinstance(data, dict) else None
    total = data.get("total") if isinstance(data, dict) else None
    return int(total) if isinstance(total, int) else len(rows or [])


def _appt_rows(data) -> list:
    return [a for a in (data.get("appointments") or [] if isinstance(data, dict) else []) if isinstance(a, dict)]


def _on_leave(data, day: str) -> str:
    rows = [r for r in data or [] if isinstance(r, dict) and str(r.get("leave_date"))[:10] == day] \
        if isinstance(data, list) else []
    return ", ".join(f"{r.get('doctor_name')} ({LEAVE_LABEL.get(r.get('leave_type'), 'full day')})" for r in rows)


def _line_appointments(data) -> str:
    n = _total(data, "appointments")
    if not n:
        return "Appointments: none booked for today."
    by = Counter(str(a.get("doctor_name") or a.get("lab_test_name") or "Other") for a in _appt_rows(data))
    top = ", ".join(f"{d} {c}" for d, c in by.most_common(3))
    return f"Appointments: {n} today" + (f" · {top}" if top else "") + "."


def _line_queue(data) -> str:
    stages = Counter(str(r.get("stage")) for r in _queue_of(data))
    return f"OPD queue: {stages['waiting']} waiting, {stages['in_consultation']} in consultation."


_MORNING = (  # (page, label, call(), line(data))
    ("appointments", "Appointments", lambda: _confirmed_on(_brief_day()), _line_appointments),
    ("leaves", "Leaves", lambda: _call("GET", "/admin/leaves"),
     lambda d: f"On leave today: {_on_leave(d, _brief_day())}." if _on_leave(d, _brief_day())
     else "No doctor is on leave today."),
    ("opdqueue", "OPD queue", lambda: _call("GET", "/admin/opd/queue", {"date": _brief_day()}), _line_queue),
    ("payments", "Bookings to review", lambda: _call("GET", "/admin/bookings/pending-review"),
     lambda d: f"Bookings waiting for review: {_total(d, 'bookings')}."),
    ("diagreports", "Reports", lambda: _call("GET", "/admin/reports/deliveries", {"days": 1, "state": "failed"}),
     lambda d: f"Reports: {_plural(_total(d, 'deliveries'), 'report')} failed to send in the last day."
     if _total(d, "deliveries") else "Reports: none failed to send in the last day."),
    ("leads", "Hot leads", lambda: _call("GET", "/admin/leads", {"segment": "hot", "days": 1, "limit": 1, "offset": 0}),
     lambda d: f"Hot leads: {_total(d, 'rows')} since yesterday."),
    ("voice", "Receptionist", lambda: _call("GET", "/admin/voice/calls", {"days": 1, "limit": 1}),
     lambda d: f"AI receptionist: {_plural(_total(d, 'calls'), 'call')} in the last day."),
    (None, "Notifications", lambda: _call("GET", "/admin/notifications/unread-count"),
     lambda d: f"Unread notifications: {d.get('unread_count', 0) if isinstance(d, dict) else 0}."),
)


def _line_not_arrived(data) -> str:
    n = len([a for a in _appt_rows(data) if not a.get("token_number")])
    return f"Not arrived: {_plural(n, 'confirmed booking')} today never checked in." if n \
        else "Everyone booked for today has checked in."


def _line_tomorrow(data) -> str:
    n = _total(data, "appointments")
    times = sorted(str(a.get("appointment_time") or "")[:5] for a in _appt_rows(data) if a.get("appointment_time"))
    return f"Tomorrow: {_plural(n, 'booking')}" + (f", first at {times[0]}." if times else ".") if n \
        else "Tomorrow: no bookings yet."


_EOD = (
    ("appointments", "Not arrived", lambda: _confirmed_on(_brief_day()), _line_not_arrived),
    ("payments", "Payment pending", lambda: _confirmed_on(_brief_day(), "pending_payment", _brief_day(30)),
     lambda d: f"Payment pending: {_plural(_total(d, 'appointments'), 'booking')} waiting for payment."),
    ("diagreports", "Reports", _MORNING[4][2], _MORNING[4][3]),
    ("leaves", "Leaves tomorrow", lambda: _call("GET", "/admin/leaves"),
     lambda d: f"On leave tomorrow: {_on_leave(d, _brief_day(1))}. Their bookings for tomorrow are cancelled and "
               "patients told when the leave is saved." if _on_leave(d, _brief_day(1))
     else "No doctor is on leave tomorrow."),
    ("appointments", "Tomorrow", lambda: _confirmed_on(_brief_day(1)), _line_tomorrow),
)


def _brief_call(sections):
    def call(params: dict) -> list:
        return [_step(label, make(), optional=True) for _, label, make, _ in sections]
    return call


def _brief_present(sections, greeting: str):
    def view(outcomes: list, params: dict) -> dict:
        title = f"{greeting} · {_now().strftime('%a %d %b')}"
        cards = [{"type": "text", "text": title}]
        for (page, label, _, line), out in zip(sections, outcomes):
            if out.status == "ok":
                text = line(out.data)
            elif out.http_status and out.http_status < 500:
                continue  # the route refused it for this user or clinic: not their section
            else:
                text = f"{label}: couldn't load it just now."
            cards.append({"type": "help", "text": text, "page": page})
        return {"message": title, "navigate": None, "cards": cards}
    return view


briefing_morning_call, briefing_morning_present = _brief_call(_MORNING), _brief_present(_MORNING, "Good morning")
briefing_eod_call, briefing_eod_present = _brief_call(_EOD), _brief_present(_EOD, "End of day")


# ── KK-P4-15: OPD invoice create / issue, payment link (R2); booking refund (R3); home visit assignment (R2) ──
# Invoice create and issue are two actions: the issue path needs the new invoice's id, and a plan's steps are
# fixed at preview time. Cash receipts, OPD refunds and voids stay hand-offs (KK-P4-12).

MAX_REFUND_RUPEES = 10000  # the typed number's own ceiling (ConfirmIn.typed_count <= 10000)
_INVOICES = "/admin/opd/invoices"
_BILLING = "Do it on the OPD Billing page."


async def _get_or_refuse(read, path: str, query: dict = None, need: str = "OPD Billing"):
    """A read the action needs; a refusal names the missing permission instead of "unavailable"."""
    out = await read(_call("GET", path, query or {}))
    if out.status != "ok":
        if out.error_code == "forbidden":
            raise EmptyPlan(f"You need the {need} permission for that. Ask your clinic admin.")
        raise RuntimeError(f"read {path} failed")
    return out.data


def _named(rows, who: str, name_of) -> list:
    rows = [r for r in rows or [] if isinstance(r, dict)]
    return [r for r in rows if all(w in str(name_of(r) or "").lower() for w in who.split())]


def _only(found: list, who: str, noun: str, line) -> dict:
    if len(found) != 1:
        raise EmptyPlan(f'No {noun} matches "{who}".' if not found else
                        f'{len(found)} {noun}s match "{who}": {"; ".join(line(r) for r in found[:5])}. Use more of the name.')
    return found[0]


def _inv_name(inv: dict) -> str:
    return (inv.get("patient_snapshot") or {}).get("name") or "the patient"


def _inv_line(inv: dict) -> str:
    return f"{_inv_name(inv)} ({inv.get('invoice_number') or 'draft'}, {present.fmt(inv.get('total_paise'), 'paise')})"


async def _invoices(read, who: str, status: str = None) -> list:
    query = {"q": who[:60], **({"status": status} if status else {})}
    return _named(await _get_or_refuse(read, _INVOICES, query), who, _inv_name)


async def invoice_create_preview(read, params: dict) -> dict:
    """Today's visit from the live queue (by name or token); the endpoint makes the draft with the
    doctor's consultation fee, or returns the visit's existing one (refused here so nothing is implied)."""
    who, today = params["who"], _now().date().isoformat()
    rows = _queue_of(await _get_or_refuse(read, "/admin/opd/queue", {"date": today}, "OPD Front Desk"))
    found = [r for r in rows if str(r.get("token_number")) == who] if who.isdigit() else \
        _named(rows, who, lambda r: r.get("patient_name"))
    r = _only(found, who, "patient in today's queue", _token)
    aid = str(r.get("appointment_id"))
    existing = [i for i in await _invoices(read, who) if str(i.get("appointment_id")) == aid and i.get("status") != "void"]
    if existing:
        raise EmptyPlan(f"{r.get('patient_name')} already has an invoice for this visit: {_inv_line(existing[0])}.")

    async def verify(read, data, call):
        if isinstance(data, dict) and data.get("id") and str(data.get("appointment_id")) == aid:
            return True, f"Draft invoice ready: {present.fmt(data.get('total_paise'), 'paise')}"
        out = await read(_call("GET", _INVOICES, {"q": who[:60]}))
        rows = out.data if out.status == "ok" and isinstance(out.data, list) else None
        if rows is None:
            return None, "Couldn't read the invoices back"
        ok = any(isinstance(i, dict) and str(i.get("appointment_id")) == aid for i in rows)
        return ok, "Draft invoice ready" if ok else "No invoice for the visit"

    return {"title": f"Make the bill for {r.get('patient_name')}",
            "bullets": [f"{_token(r)}: a draft invoice with the doctor's consultation fee."],
            "warnings": ["Nothing is charged yet. Check the items, then say \"issue "
                         f"{str(r.get('patient_name') or '').split(' ')[0]}'s invoice\"."],
            "snapshot": {"appointment_id": aid, "stage": r.get("stage")},
            "steps": [_step("Create the draft invoice", _call("POST", f"/admin/opd/appointments/{aid}/invoice"),
                            verify=verify)]}


def _invoice_is(iid: str, ok, said):
    async def verify(read, data, call):
        out = await read(_call("GET", f"{_INVOICES}/{iid}"))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the invoice back"
        return (True, said(out.data)) if ok(out.data) else (False, f"The invoice is {out.data.get('status')}")
    return verify


async def invoice_issue_preview(read, params: dict) -> dict:
    who = params["who"]
    inv = _only(await _invoices(read, who, "draft"), who, "draft invoice", _inv_line)
    iid = str(inv.get("id"))
    inv = await _get_or_refuse(read, f"{_INVOICES}/{iid}")
    items = [i for i in inv.get("items") or [] if isinstance(i, dict)]
    if not items:
        raise EmptyPlan(f"{_inv_name(inv)}'s draft has no items yet. {_BILLING}")
    bullets = [f"{i.get('description')} × {i.get('quantity') or 1}: {present.fmt(i.get('line_total_paise'), 'paise')}"
               for i in items[:8]] + [f"Total: {present.fmt(inv.get('total_paise'), 'paise')}"]
    return {"title": f"Issue {_inv_name(inv)}'s invoice", "bullets": bullets,
            "warnings": ["Issuing gives the invoice its number; after that it can only be voided on the billing page. "
                         "An online payment already made for the booking is recorded against it."],
            "snapshot": {"id": iid, "updated_at": inv.get("updated_at"), "total": inv.get("total_paise"),
                         "items": len(items)},
            "steps": [_step("Issue the invoice", _call("POST", f"{_INVOICES}/{iid}/issue"),
                            verify=_invoice_is(iid, lambda i: i.get("status") in ("issued", "partially_paid", "paid"),
                                               lambda i: f"Issued {i.get('invoice_number') or ''}".strip()))]}


async def payment_link_preview(read, params: dict) -> dict:
    """The endpoint makes a new gateway link and WhatsApps it on every call (no idempotency, D-38),
    so the read-back is the response itself and an unknown outcome is never sent again."""
    who = params["who"]
    rows = await _invoices(read, who)
    due = [i for i in rows if i.get("status") in ("issued", "partially_paid")
           and int(i.get("total_paise") or 0) > int(i.get("paid_paise") or 0)]
    if not due:
        why = ("its invoice is still a draft: issue it first" if any(i.get("status") == "draft" for i in rows)
               else "nothing is due" if rows else "there is no invoice for that name")
        raise EmptyPlan(f'No payment link for "{who}": {why}.')
    inv = _only(due, who, "invoice with a balance", _inv_line)
    iid, balance = str(inv.get("id")), int(inv.get("total_paise") or 0) - int(inv.get("paid_paise") or 0)

    async def verify(read, data, call):
        if isinstance(data, dict) and data.get("url"):
            return True, "Payment link created"
        return None, "Couldn't confirm the link. Check the invoice before sending another"

    return {"title": f"Send a payment link to {_inv_name(inv)}",
            "bullets": [f"{inv.get('invoice_number') or 'Invoice'}: {present.fmt(balance, 'paise')} due."],
            "warnings": ["The patient gets the link on WhatsApp if their number is on file. Each request makes a new "
                         "link, so send it once."],
            "snapshot": {"id": iid, "balance": balance, "status": inv.get("status")},
            "steps": [_step("Create the payment link", _call("POST", f"{_INVOICES}/{iid}/payment-link"), verify=verify)]}


async def booking_refund_preview(read, params: dict) -> dict:
    """POST /admin/bookings/{id}/refund refunds the whole online payment (no partial refund), through the
    gateway it was paid with. The typed number is the amount in rupees (registry §4.3: typed amount)."""
    who, today = params["who"], _now().date()
    rows = await _search(read, who, (today - timedelta(days=30)).isoformat(), (today + timedelta(days=330)).isoformat(),
                         "confirmed")
    found = _matching(rows, who)
    if not found:
        raise EmptyPlan(f'No confirmed booking from the last 30 days on matches "{who}".')
    paid = [a for a in found if a.get("payment_id") and int(a.get("amount_paise") or 0) > 0]
    if not paid:
        raise EmptyPlan(f"{_who(found[0])}'s booking was not paid online, so there is nothing to refund here.")
    a = _only(paid, who, "paid booking", _booking_line)
    if a.get("refund_state") in ("refunded", "initiated"):
        raise EmptyPlan(f"A refund for {_booking_line(a)} is already {a['refund_state']}.")
    paise = int(a["amount_paise"])
    rupees = paise // 100
    if rupees > MAX_REFUND_RUPEES or rupees < 1:
        raise EmptyPlan(f"Refund {present.fmt(paise, 'paise')} on the Payments page.")
    warnings = [f"The full {present.fmt(paise, 'paise')} goes back through the payment gateway and the booking is "
                "marked refunded. This cannot be undone."]
    if a.get("refund_state") == "failed":
        warnings.append("An earlier refund attempt for this booking failed.")
    return {"title": f"Refund {_who(a)}'s booking", "bullets": [f"{_booking_line(a)} with {_with(a)}."],
            "warnings": warnings, "affected": rupees,
            "snapshot": {**_snap(a), "amount": paise, "refund_state": a.get("refund_state")},
            "steps": _desk_step(f"Refund {a.get('booking_ref') or 'the booking'}",
                                _call("POST", f"/admin/bookings/{a['id']}/refund", json={}),
                                _booking_after(a, lambda r: r.get("status") == "refunded"
                                               or r.get("refund_state") == "refunded", lambda r: "Refunded"))}


_HC = "/admin/home-collection"


async def homecollect_assign_preview(read, params: dict) -> dict:
    who, name, day = params["who"], params["phleb"], params.get("date") or _now().date().isoformat()
    visits = (await _get_or_refuse(read, f"{_HC}/visits", {"date": day}, "Home Collection Manage")).get("visits")
    v = _only(_named(visits, who, lambda x: x.get("patient_name")), who, f"home visit on {present.fmt(day, 'date')}",
              lambda x: f"{x.get('patient_name')} ({x.get('collection_slot')})")
    if v.get("status") != "confirmed":
        raise EmptyPlan(f"{v.get('patient_name')}'s visit is {v.get('status')}: only a confirmed visit can be assigned.")
    phlebs = (await _get_or_refuse(read, f"{_HC}/phlebotomists", {"date": day}, "Home Collection Manage")
              ).get("phlebotomists")
    p = _only(_named([x for x in phlebs or [] if x.get("is_active", True)], name, lambda x: x.get("name")), name,
              "phlebotomist", lambda x: str(x.get("name")))
    if p.get("off_on_date"):
        raise EmptyPlan(f"{p.get('name')} is on leave on {present.fmt(day, 'date')}. Choose someone else.")
    vid, pid = str(v["id"]), str(p["id"])
    if str(v.get("phlebotomist_id") or "") == pid:
        raise EmptyPlan(f"{v.get('patient_name')}'s visit is already assigned to {p.get('name')}.")

    async def verify(read, data, call):
        out = await read(_call("GET", f"{_HC}/visits", {"date": day}))
        rows = out.data.get("visits") if out.status == "ok" and isinstance(out.data, dict) else None
        row = next((x for x in rows or [] if isinstance(x, dict) and str(x.get("id")) == vid), None)
        if row is None:
            return None, "Couldn't read the visit back"
        ok = str(row.get("phlebotomist_id") or "") == pid
        return ok, f"Assigned to {p.get('name')}" if ok else "Not assigned"

    return {"title": f"Assign {v.get('patient_name')}'s home visit to {p.get('name')}",
            "bullets": [f"{present.fmt(day, 'date')}, {v.get('collection_slot')}: {v.get('lab_test_name') or 'home visit'}."],
            "warnings": ["The phlebotomist sees it in their visits straight away."],
            "snapshot": {"visit": vid, "from": v.get("phlebotomist_id"), "status": v.get("status")},
            "steps": [_step("Assign the visit", _call("POST", f"{_HC}/visits/{vid}/assign", json={"phlebotomist_id": pid}),
                            verify=verify)]}


AUTO_ASSIGN_DAYS = 7


async def homecollect_auto_assign_preview(read, params: dict) -> dict:
    """POST /auto-assign takes every confirmed, unassigned home visit from today on (up to 200) and the service
    picks a free phlebotomist, skipping anyone on leave that day (HC-01). It has no dry run, so the preview
    counts the next 7 days and says later dates are included; the response's counts are the result."""
    start = _now().date()
    days, waiting = [(start + timedelta(days=i)).isoformat() for i in range(AUTO_ASSIGN_DAYS)], {}
    for day in days:
        data = await _get_or_refuse(read, f"{_HC}/visits", {"date": day}, "Home Collection Manage")
        waiting[day] = sorted(str(v.get("id")) for v in data.get("visits") or []
                              if isinstance(v, dict) and v.get("status") == "confirmed" and not v.get("phlebotomist_id"))
    total = sum(len(v) for v in waiting.values())
    if not total:
        raise EmptyPlan("No confirmed home visit in the next 7 days is waiting for a phlebotomist. Later dates: use "
                        "Auto-assign on the Home Collections page.")
    phlebs = (await _get_or_refuse(read, f"{_HC}/phlebotomists", {"date": days[0]}, "Home Collection Manage")
              ).get("phlebotomists") or []
    off = [str(p.get("name")) for p in phlebs if isinstance(p, dict) and p.get("off_on_date")]
    bullets = [f"{_plural(total, 'unassigned visit')} in the next 7 days: "
               + ", ".join(f"{present.fmt(d, 'date')} {len(ids)}" for d, ids in waiting.items() if ids) + "."]
    if off:
        bullets.append(f"On leave today (skipped): {', '.join(off)}.")
    ids = [i for v in waiting.values() for i in v]

    async def verify(read, data, call):
        if isinstance(data, dict) and data.get("success"):
            return True, f"{data.get('assigned', 0)} assigned, {data.get('remaining', 0)} left with no free phlebotomist"
        for day, want in waiting.items():  # re-verify without a response: were the previewed visits assigned?
            out = await read(_call("GET", f"{_HC}/visits", {"date": day}))
            if out.status != "ok" or not isinstance(out.data, dict):
                return None, "Couldn't read the visits back"
            left = {str(v.get("id")) for v in out.data.get("visits") or [] if isinstance(v, dict)
                    and not v.get("phlebotomist_id")} & set(want)
            if left:
                return None, "Some visits are still unassigned"
        return True, "The previewed visits are assigned"

    return {"title": "Auto-assign home visits", "bullets": bullets,
            "warnings": ["Every unassigned confirmed visit from today on is included, later dates too (up to 200). "
                         "A visit with no free phlebotomist stays unassigned."],
            "affected": total, "snapshot": ids,
            "steps": [_step("Auto-assign", _call("POST", f"{_HC}/auto-assign"), verify=verify)]}


# ── voice.settings_update (released after D-13): one AI receptionist setting per request ──
# PUT /admin/voice/settings takes the whole object: the step sends back exactly what was read with one
# field changed, the whole read is in the plan hash, and since D-13 the server writes only that key.

_VS_LABEL = {"assistant_name": "Receptionist name", "primary_language": "Language", "pace": "Speaking pace",
             "reception_hours": "Reception hours", "emergency_number": "Emergency number",
             "outbound_window": "Lead-call hours", "daily_cap": "Lead calls a day", "max_attempts": "Tries per lead"}
_VS_LANG = {"te-IN": "Telugu", "hi-IN": "Hindi", "en-IN": "English"}
_VS_WARN = {"reception_hours": "Calls outside these hours are not answered by the receptionist.",
            "emergency_number": "Callers with an emergency are told this number.",
            "outbound_window": "Every lead call, including the automatic ones, waits for these hours.",
            "daily_cap": "Lead calls over the limit wait for the next day.",
            "primary_language": "New calls start in this language; callers can still switch."}


def _vs_hours(h: dict) -> str:
    return f"{h.get('start')}-{h.get('end')}" + (f" ({h.get('days')})" if h.get("days") else "")


def _vs_number(n) -> str:
    digits = re.sub(r"\D", "", str(n or ""))
    return digits if len(digits) <= 4 else f"…{digits[-4:]}"


def _vs_apply(settings: dict, field: str, value) -> tuple:
    """(new body, changed paths, shown before, shown after). Bounds = VoiceSettingsIn + the 09-21 rule."""
    body = json.loads(json.dumps(settings))
    out = body.setdefault("outbound", {})
    if field == "assistant_name":
        name = " ".join(str(value).split())
        if not 2 <= len(name) <= 30:
            raise EmptyPlan("The receptionist's name must be 2-30 characters.")
        before, body["assistant_name"] = settings.get("assistant_name"), name
        return body, [["assistant_name"]], str(before), name
    if field == "primary_language":
        if value not in _VS_LANG:
            raise EmptyPlan("The receptionist speaks Telugu, Hindi or English.")
        before, body["primary_language"] = settings.get("primary_language"), value
        return body, [["primary_language"]], _VS_LANG.get(before, str(before)), _VS_LANG[value]
    if field == "pace":
        old = float(settings.get("pace") or 1.0)
        new = round(old + float(value), 2) if str(value)[:1] in "+-" else round(float(value), 2)
        if not 0.5 <= new <= 2.0:
            raise EmptyPlan("Speaking pace must be between 0.5 and 2.0 (1.0 is normal).")
        body["pace"] = new
        return body, [["pace"]], str(old), str(new)
    if field == "reception_hours":
        start, end = value["window"].split("-")
        old = dict(settings.get("reception_hours") or {})
        body["reception_hours"] = {**old, "start": start, "end": end,
                                   **({"days": value["days"]} if value.get("days") else {})}
        return body, [["reception_hours"]], _vs_hours(old), _vs_hours(body["reception_hours"])
    if field == "emergency_number":
        digits = re.sub(r"\D", "", str(value))
        if not 3 <= len(digits) <= 15:
            raise EmptyPlan("The emergency number must be 3-15 digits.")
        before, body["emergency_number"] = settings.get("emergency_number"), digits
        return body, [["emergency_number"]], _vs_number(before), _vs_number(digits)
    if field == "outbound_window":
        start, end = value.split("-")
        if start < "09:00" or end > "21:00":
            raise EmptyPlan("Lead calls are allowed only between 09:00 and 21:00.")
        old = f"{out.get('window_start', '10:00')}-{out.get('window_end', '19:00')}"
        out["window_start"], out["window_end"] = start, end
        return body, [["outbound", "window_start"], ["outbound", "window_end"]], old, value
    if field == "daily_cap":
        if not 0 <= int(value) <= 500:
            raise EmptyPlan("The daily lead-call limit must be 0-500.")
        before, out["daily_cap"] = out.get("daily_cap"), int(value)
        return body, [["outbound", "daily_cap"]], str(before), str(int(value))
    if field == "max_attempts":
        if not 1 <= int(value) <= 3:
            raise EmptyPlan("Each lead can be tried 1 to 3 times.")
        before, out["max_attempts"] = out.get("max_attempts"), int(value)
        return body, [["outbound", "max_attempts"]], str(before), str(int(value))
    raise EmptyPlan("Kriya can't change that setting. Do it on the AI Receptionist page.")


def _at_path(d, path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


async def voice_settings_preview(read, params: dict) -> dict:
    field, value = params["setting"]
    settings = ((await _voice_get(read, "/admin/voice/settings")) or {}).get("settings")
    if not isinstance(settings, dict):
        raise RuntimeError("settings read failed")
    body, paths, before, after = _vs_apply(settings, field, value)
    label = _VS_LABEL.get(field, field)
    if all(_at_path(body, p) == _at_path(settings, p) for p in paths):
        raise EmptyPlan(f"{label} is already {after}.")
    want = {tuple(p): _at_path(body, p) for p in paths}

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/voice/settings"))
        now = out.data.get("settings") if out.status == "ok" and isinstance(out.data, dict) else None
        if not isinstance(now, dict):
            return None, "Couldn't read the voice settings back"
        ok = all(_at_path(now, list(p)) == v for p, v in want.items())
        return ok, f"{label}: {after}" if ok else "The setting did not change"

    return {"title": f"Change the receptionist's {label.lower()}", "bullets": [f"{label}: {before} → {after}."],
            "warnings": [w for w in (_VS_WARN.get(field), "The other voice settings are sent back unchanged.") if w],
            "snapshot": _digest([json.dumps(settings, sort_keys=True)]),
            "steps": [_step("Save voice settings", _call("PUT", "/admin/voice/settings", json=body), verify=verify)]}


# ── Voice-first entry: OPD vitals, counter payments, doctor on/off, IPD vitals and bed moves ──

_VITAL_LABELS = {"bp": "BP", "pulse_bpm": "Pulse", "temperature_c": "Temperature", "spo2_pct": "SpO2",
                 "weight_kg": "Weight", "height_cm": "Height", "resp_rate": "Resp. rate",
                 "blood_glucose_mg_dl": "Blood sugar", "pain_score": "Pain score"}
_VITAL_UNITS = {"pulse_bpm": " /min", "temperature_c": " °C", "spo2_pct": "%", "weight_kg": " kg", "height_cm": " cm",
                "resp_rate": " /min", "blood_glucose_mg_dl": " mg/dL", "pain_score": "/10"}
_OPD_VITALS = ("bp_systolic", "bp_diastolic", "pulse_bpm", "temperature_c", "spo2_pct", "weight_kg", "height_cm")


def _vital_lines(vitals: dict, before: dict = None) -> list:
    before = before or {}
    out = []
    if "bp_systolic" in vitals:
        old = f"{before['bp_systolic']}/{before['bp_diastolic']} → " if before.get("bp_systolic") else ""
        out.append(f"BP: {old}{vitals['bp_systolic']}/{vitals['bp_diastolic']}")
    for k, v in vitals.items():
        if k in _VITAL_LABELS:
            old = f"{_num_str(before[k])}{_VITAL_UNITS.get(k, '')} → " if before.get(k) is not None else ""
            out.append(f"{_VITAL_LABELS[k]}: {old}{_num_str(v)}{_VITAL_UNITS.get(k, '')}")
    return out


def _num_str(v) -> str:
    return format(Decimal(str(v)).normalize(), "f")


def _same_number(a, b) -> bool:
    try:
        return a is not None and b is not None and Decimal(str(a)) == Decimal(str(b))
    except ArithmeticError:
        return False


async def opd_vitals_preview(read, params: dict) -> dict:
    """The encounter endpoint writes every vital it is given and blanks the rest, so the body is the chart's
    current vitals with only the ones said replaced: "weight 62" never wipes the BP taken earlier."""
    if params["target"].startswith("bed:"):
        raise EmptyPlan("A bed is an inpatient: say \"vitals for bed …\" on an IPD clinic, or the OPD token.")
    said = {k: v for k, v in params["vitals"].items() if k in _OPD_VITALS}
    other = [_VITAL_LABELS[k] for k in params["vitals"] if k not in _OPD_VITALS and k in _VITAL_LABELS]
    if not said:
        raise EmptyPlan("The OPD chart takes BP, pulse, temperature, SpO2, weight and height.")
    r = await _queue_row(read, params)
    aid = str(r["appointment_id"])
    enc = await _get_or_refuse(read, f"/admin/opd/encounters/by-appointment/{aid}", need="OPD Front Desk")
    if not isinstance(enc, dict) or enc.get("status", "draft") != "draft":
        raise EmptyPlan(f"{_token(r)}'s visit is already signed: vitals are changed by the doctor's amendment.")
    current = {k: enc.get(k) for k in _OPD_VITALS if enc.get(k) is not None}
    if all(_same_number(current.get(k), v) for k, v in said.items()):
        raise EmptyPlan(f"{_token(r)}'s chart already has those vitals.")
    body = {**current, **said}
    warnings = [f"Not on the OPD chart, so not saved: {', '.join(other)}."] if other else []
    if r.get("stage") == "vitals_pending":
        warnings.append("The patient moves from vitals to waiting.")

    async def verify(read, data, call):
        out = await read(_call("GET", f"/admin/opd/encounters/by-appointment/{aid}"))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the chart back"
        ok = all(_same_number(out.data.get(k), v) for k, v in said.items())
        return (True, "On the chart") if ok else (False, "The chart shows other values")

    return {"title": f"Record vitals for {_token(r)}", "bullets": _vital_lines(said, current), "warnings": warnings,
            "snapshot": {"appointment_id": aid, "vitals": current, "stage": r.get("stage")},
            "steps": [_step("Save the vitals", _call("PUT", f"/admin/opd/encounters/by-appointment/{aid}/vitals",
                                                     json=body), verify=verify)]}


MAX_RECEIPT_RUPEES = 1_000_000  # ReceiptIn's own ceiling (100,000,000 paise)


async def receipt_preview(read, params: dict) -> dict:
    """R3, typed amount in rupees. The idempotency key is derived from the invoice, what it had been paid
    and this payment: a retry or a double confirm records one receipt; the next payment gets a new key."""
    who, mode, ref = params["who"], params["mode"], params.get("reference")
    rupees = int(Decimal(params["amount"]))
    if not 0 < rupees <= MAX_RECEIPT_RUPEES:
        raise EmptyPlan(f"{_inr(rupees)} is not an amount Kriya records. {_BILLING}")
    if mode in ("upi", "card") and not ref:
        raise EmptyPlan(f"A {mode.upper()} payment needs its transaction reference (UTR / RRN). "
                        f"Say it, e.g. \"UTR 412345678901\".")
    if who.startswith("bed:"):
        raise EmptyPlan("Say the patient's name or token for the OPD bill.")
    if who.startswith("#"):  # today's token -> its visit's invoice
        queue = _queue_of(await _get_or_refuse(read, "/admin/opd/queue", {"date": _now().date().isoformat()},
                                               "OPD Front Desk"))
        r = _only([q for q in queue if str(q.get("token_number")) == who[1:]], f"token {who[1:]}", "token today", _token)
        rows = [i for i in await _invoices(read, str(r.get("patient_name") or "").lower())
                if str(i.get("appointment_id")) == str(r.get("appointment_id"))]
    else:
        rows = await _invoices(read, who)
    due = [i for i in rows if i.get("status") in ("issued", "partially_paid")]
    if not due:
        why = ("the invoice is still a draft: issue it first" if any(i.get("status") == "draft" for i in rows)
               else "nothing is due" if rows else "there is no invoice for that name")
        raise EmptyPlan(f'No payment to record for "{who}": {why}.')
    inv = _only(due, who, "invoice with a balance", _inv_line)
    iid = str(inv.get("id"))
    inv = await _get_or_refuse(read, f"{_INVOICES}/{iid}")
    paid, total = int(inv.get("paid_paise") or 0), int(inv.get("total_paise") or 0)
    balance = total - int(inv.get("credited_paise") or 0) - paid
    if rupees * 100 > balance:
        raise EmptyPlan(f"{_inr(rupees)} is more than the {present.fmt(balance, 'paise')} due on "
                        f"{inv.get('invoice_number') or 'the invoice'}.")
    key = hashlib.sha256(f"ketu-receipt:{iid}:{paid}:{rupees}:{mode}:{ref or ''}".encode()).hexdigest()[:40]
    body = {"mode": mode, "amount_paise": rupees * 100, "idempotency_key": key, **({"reference": ref} if ref else {})}
    left = balance - rupees * 100
    bullets = [f"{inv.get('invoice_number') or 'Invoice'}: {present.fmt(balance, 'paise')} due.",
               f"{mode.upper()} {_inr(rupees)}" + (f", ref {ref}" if ref else "") + ".",
               "Paid in full." if left == 0 else f"{present.fmt(left, 'paise')} still due after this."]
    warnings = ["Cash goes into your open cash drawer shift."] if mode == "cash" else []
    return {"title": f"Record {_inr(rupees)} from {_inv_name(inv)}", "bullets": bullets, "warnings": warnings,
            "affected": rupees, "snapshot": {"id": iid, "paid": paid, "status": inv.get("status")},
            "steps": [_step("Record the payment", _call("POST", f"{_INVOICES}/{iid}/receipts", json=body),
                            verify=_invoice_is(iid, lambda i: int(i.get("paid_paise") or 0) >= paid + rupees * 100,
                                               lambda i: f"Recorded; {i.get('status', '').replace('_', ' ')}"))]}


async def doctor_active_preview(read, params: dict) -> dict:
    """Removing a doctor by voice switches them off (is_active): the row, history and bills stay. The hard
    delete stays on the Doctors page."""
    doc = await _doctor(read, params["doctor"])
    on = params["switch"] == "on"
    if bool(doc.get("is_active", True)) == on:
        raise EmptyPlan(f"{doc.get('name')} is already {'active' if on else 'switched off'}.")
    warnings = []
    if not on:
        today = _now().date()
        booked = [a for a in await _bookings(read, doc["name"], today, today + timedelta(days=SCHEDULE_LOOKAHEAD_DAYS))
                  if a.get("doctor_name") == doc["name"]]
        warnings.append("Patients can no longer book this doctor on WhatsApp or by phone.")
        if booked:
            warnings.insert(0, f"{_plural(len(booked), 'booking')} in the next {SCHEDULE_LOOKAHEAD_DAYS} days "
                               "stay as they are: cancel or move them from the Appointments page.")
    return {"title": f"{'Switch on' if on else 'Switch off'} {doc.get('name')}",
            "bullets": [f"{doc.get('name')}: {'off → active' if on else 'active → off'}."], "warnings": warnings,
            "snapshot": {"id": str(doc["id"]), "active": doc.get("is_active", True)},
            "steps": [_step(f"{'Switch on' if on else 'Switch off'} {doc.get('name')}",
                            _call("PUT", f"/admin/doctors/{doc['id']}", json={"is_active": on}),
                            verify=_doctor_after(str(doc["id"]), lambda r: bool(r.get("is_active")) == on,
                                                 "active" if on else "switched off"))]}


_IPD = "/admin/ipd"


def _beds(board) -> list:
    return [{**b, "_ward": w.get("name") or w.get("code")} for w in (board or {}).get("wards") or []
            if isinstance(w, dict) for b in w.get("beds") or [] if isinstance(b, dict)]


def _bed_ref(ref: str) -> tuple:
    """"12@icu" -> ("12", "icu"); a bed code is unique only inside its ward."""
    code, _, ward = str(ref).partition("@")
    return code, ward or None


def _in_ward(beds: list, ward) -> list:
    return [b for b in beds if not ward or ward in str(b.get("_ward") or "").lower()]


def _one_bed(beds: list, ref: str) -> dict:
    """Exactly one bed for "12" / "12@icu"; the same code in two wards is asked, never guessed."""
    code, ward = _bed_ref(ref)
    found = _in_ward([b for b in beds if _same_code(b.get("code"), code)], ward)
    if not found:
        raise EmptyPlan(f"There is no bed {code}" + (f" in {ward}" if ward else "") + ".")
    if len(found) > 1:
        raise EmptyPlan(f"Bed {code} is in {len(found)} wards ({', '.join(str(b.get('_ward')) for b in found[:5])}). "
                        f"Say the ward too, e.g. \"bed {code} in {found[0].get('_ward')}\".")
    return found[0]


def _bed_line(b: dict) -> str:
    occ = b.get("occupant") or {}
    return f"bed {b.get('code')} ({occ.get('patient_name') or 'patient'})"


def _same_code(a, b) -> bool:
    norm = lambda s: re.sub(r"[\s-]", "", str(s or "")).upper()  # noqa: E731
    return norm(a) == norm(b)


async def _inpatient_bed(read, target: str) -> dict:
    """The one occupied bed the target names ("bed:12" or the patient's name)."""
    beds = [b for b in _beds(await _get_or_refuse(read, f"{_IPD}/board", need="IPD"))
            if (b.get("occupant") or {}).get("admission_id") and b.get("status") == "occupied"]
    if target.startswith("bed:"):
        code, ward = _bed_ref(target[4:])
        found = _in_ward([b for b in beds if _same_code(b.get("code"), code)], ward)
        label = f"bed {code}" + (f" in {ward}" if ward else "")
    elif target.startswith("#"):
        raise EmptyPlan("A token is an OPD visit: say the inpatient's bed or name.")
    else:
        found = _named(beds, target, lambda b: (b.get("occupant") or {}).get("patient_name"))
        label = f'"{target}"'
    if not found:
        raise EmptyPlan(f"No admitted patient is in {label}." if target.startswith("bed:")
                        else f"No admitted patient matches {label}.")
    if len(found) > 1:
        raise EmptyPlan(f"{len(found)} inpatients match {label}: "
                        f"{'; '.join(_bed_line(b) + ' in ' + str(b.get('_ward')) for b in found[:5])}. "
                        "Say the bed and its ward, e.g. \"bed 5 in ICU\".")
    return found[0]


async def ipd_vitals_preview(read, params: dict) -> dict:
    """A nurse's charted observation, as said: one row, timed when it was said (params.observed_at)."""
    b = await _inpatient_bed(read, params["target"])
    occ = b["occupant"]
    adm = str(occ["admission_id"])
    body = {"kind": "vitals", "observed_at": params["observed_at"],
            **{k: v for k, v in params["vitals"].items() if k not in ("weight_kg", "height_cm")}}
    skipped = [_VITAL_LABELS[k] for k in ("weight_kg", "height_cm") if k in params["vitals"]]
    if len(body) == 2:
        raise EmptyPlan("The inpatient chart takes BP, pulse, temperature, SpO2, resp. rate, sugar and pain score.")
    when = present.fmt(params["observed_at"][11:16])

    async def verify(read, data, call):
        oid = data.get("id") if isinstance(data, dict) else None
        out = await read(_call("GET", f"{_IPD}/admissions/{adm}"))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the chart back"
        rows = [o for o in out.data.get("observations") or [] if isinstance(o, dict)]
        hit = next((o for o in rows if oid and str(o.get("id")) == str(oid)), None) or next(
            (o for o in rows if all(_same_number(o.get(k), v) for k, v in body.items() if k not in ("kind", "observed_at"))
             and str(o.get("observed_at") or "")[:16] == params["observed_at"][:16]), None)
        return (True, "On the inpatient chart") if hit else (False, "Not on the inpatient chart")

    return {"title": f"Chart vitals for {_bed_line(b)}",
            "bullets": _vital_lines({k: v for k, v in body.items() if k not in ("kind", "observed_at")}) +
            [f"Taken at {when}."],
            "warnings": [f"Not on the inpatient vitals chart, so not saved: {', '.join(skipped)}."] if skipped else [],
            "snapshot": {"admission_id": adm, "bed": b.get("code")},
            "steps": [_step("Chart the vitals", _call("POST", f"{_IPD}/admissions/{adm}/observations", json=body),
                            verify=verify)]}


async def ipd_transfer_preview(read, params: dict) -> dict:
    """The server re-checks the target bed is free and the patient is still where the preview saw them
    (expected_bed_id): a stale preview gets a 409, never a double booking."""
    board = await _get_or_refuse(read, f"{_IPD}/board", need="IPD")
    b = await _inpatient_bed(read, params["target"])
    dest = _one_bed(_beds(board), params["to_bed"])
    if dest.get("id") == b.get("id"):
        raise EmptyPlan(f"{_bed_line(b)} is already in bed {dest.get('code')}.")
    if dest.get("status") != "available" or not dest.get("is_active", True):
        raise EmptyPlan(f"Bed {dest.get('code')} is {str(dest.get('status') or 'not available').replace('_', ' ')}. "
                        "Pick a free bed (say \"free beds\" to see them).")
    adm = str(b["occupant"]["admission_id"])
    reason = params.get("reason") or "Bed change (by voice)"
    body = {"to_bed_id": str(dest["id"]), "expected_bed_id": str(b["id"]), "reason": reason}

    async def verify(read, data, call):
        out = await read(_call("GET", f"{_IPD}/board"))
        if out.status != "ok":
            return None, "Couldn't read the bed board back"
        now_in = next((x for x in _beds(out.data) if str((x.get("occupant") or {}).get("admission_id")) == adm), None)
        if now_in is None:
            return None, "Couldn't find the patient on the bed board"
        return (str(now_in.get("id")) == str(dest["id"])), f"In bed {now_in.get('code')}"

    return {"title": f"Move {(b['occupant'].get('patient_name') or 'the patient')} to bed {dest.get('code')}",
            "bullets": [f"Bed {b.get('code')} → bed {dest.get('code')}"
                        + (f" ({dest.get('bed_category')})" if dest.get("bed_category") else "") + ".",
                        f"Reason: {reason}."],
            "warnings": [f"Bed {b.get('code')} is marked for cleaning."],
            "snapshot": {"admission_id": adm, "from": str(b["id"]), "to": str(dest["id"]),
                         "to_status": dest.get("status"), "to_version": dest.get("version")},
            "steps": [_step("Move the patient", _call("POST", f"{_IPD}/admissions/{adm}/transfer", json=body),
                            verify=verify)]}


async def _ipd_admission(read, adm: str) -> Optional[dict]:
    out = await read(_call("GET", f"{_IPD}/admissions/{adm}"))
    return out.data.get("admission") if out.status == "ok" and isinstance(out.data, dict) else None


def _admission_is(adm: str, statuses: tuple, said: str):
    async def verify(read, data, call):
        row = await _ipd_admission(read, adm)
        if row is None:
            return None, "Couldn't read the admission back"
        return (row.get("status") in statuses), (said if row.get("status") in statuses else f"Status: {row.get('status')}")
    return verify


async def ipd_admit_preview(read, params: dict) -> dict:
    """A registered patient, an admitting doctor, the reason said, and a free bed if one is named (else the
    patient waits for a bed). The key is per patient per day, and the server allows one active stay: a
    repeated confirm never makes a second admission."""
    who = params["patient"]
    hits = await _get_or_refuse(read, "/admin/opd/patients/search", {"q": who}, "IPD Admissions")
    hits = [h for h in hits or [] if isinstance(h, dict)]
    found = hits if who.isdigit() else _named(hits, who.lower(), lambda h: h.get("name"))
    if not found:
        raise EmptyPlan(f'"{who}" is not registered. Register them first ("register {who}, phone …"), then admit.')
    p = _only(found, who, "registered patient",
              lambda h: f"{h.get('name')} ({h.get('mrn') or h.get('phone') or 'no MRN'})")
    doc = _doctor_row(await _get(read, "/admin/doctors"), params["doctor"])
    if not doc or not doc.get("id"):
        raise EmptyPlan(f"{params['doctor']} isn't on the doctor list.")
    bed = None
    if params.get("bed"):
        bed = _one_bed(_beds(await _get_or_refuse(read, f"{_IPD}/board", need="IPD")), params["bed"])
        if bed.get("status") != "available" or not bed.get("is_active", True):
            raise EmptyPlan(f"Bed {bed.get('code')} is {str(bed.get('status')).replace('_', ' ')}. "
                            "Name a free bed, or admit without one (the patient waits for a bed).")
    day = _now().date().isoformat()
    key = hashlib.sha256(f"ketu-admit:{p.get('patient_id')}:{p.get('family_member_id')}:{day}".encode()).hexdigest()[:40]
    body = {"patient_id": str(p["patient_id"]), "admitting_doctor_id": str(doc["id"]),
            "attending_doctor_id": str(doc["id"]), "reason": params["reason"], "priority": params["priority"],
            "idempotency_key": key, **({"family_member_id": str(p["family_member_id"])} if p.get("family_member_id") else {}),
            **({"bed_id": str(bed["id"])} if bed else {})}

    async def verify(read, data, call):
        adm = data.get("id") if isinstance(data, dict) else None
        if not adm:
            return None, "Couldn't tell which admission was made"
        row = await _ipd_admission(read, str(adm))
        if row is None:
            return None, "Couldn't read the admission back"
        want = ("admitted",) if bed else ("pending", "admitted")
        num = row.get("admission_number") or ""
        return (row.get("status") in want), (f"Admitted {num}".strip() if row.get("status") == "admitted"
                                             else f"Waiting for a bed {num}".strip())

    return {"title": f"Admit {p.get('name')}",
            "bullets": [f"{p.get('name')} ({p.get('mrn') or p.get('phone') or 'no MRN'}) under {doc.get('name')}.",
                        f"Reason: {params['reason']}.", f"Priority: {params['priority']}.",
                        f"Bed {bed.get('code')}." if bed else "No bed yet: the patient goes on the waiting list."],
            "warnings": ["Payer is self-pay; record insurance, attendant and the admission consent on the chart."],
            "snapshot": {"patient": str(p["patient_id"]), "bed": bed and str(bed["id"]),
                         "bed_status": bed and bed.get("status")},
            "steps": [_step(f"Admit {p.get('name')}", _call("POST", f"{_IPD}/admissions", json=body), verify=verify)]}


_DISCHARGE_LABEL = {"regular": "Regular discharge", "lama": "Left against medical advice (LAMA)",
                    "dama": "Discharged against medical advice (DAMA)", "absconded": "Absconded",
                    "transfer_out": "Transfer out to another hospital"}


async def ipd_discharge_preview(read, params: dict) -> dict:
    """Starts a discharge (admitted -> discharge pending). A death is recorded on the chart by the doctor, never
    by voice; completion (settlement) and the signed summary stay on the chart."""
    kind = params["discharge_type"]
    if kind == "death":
        raise EmptyPlan("A death is recorded on the patient's chart by the doctor, not by voice.")
    b = await _inpatient_bed(read, params["target"])
    occ, adm = b["occupant"], str(b["occupant"]["admission_id"])
    if occ.get("status") == "discharge_pending":
        raise EmptyPlan(f"{_bed_line(b)}'s discharge is already in progress.")
    warnings = ["Then: discharge summary, bill settlement and Complete discharge on the chart."]
    if kind in ("regular", "transfer_out"):
        warnings.insert(0, "This is the doctor's decision: only a doctor's own login can start it.")
    return {"title": f"Start discharge for {_bed_line(b)}", "bullets": [f"{_DISCHARGE_LABEL[kind]}."],
            "warnings": warnings, "snapshot": {"admission_id": adm, "status": occ.get("status")},
            "steps": [_step("Start the discharge", _call("POST", f"{_IPD}/admissions/{adm}/discharge/initiate",
                                                         json={"discharge_type": kind}),
                            verify=_admission_is(adm, ("discharge_pending",), "Discharge in progress"))]}


async def ipd_bed_status_preview(read, params: dict) -> dict:
    """Housekeeping: a bed in use (occupied / reserved) is never changed; taking one out of service needs a
    reason, and the server allows it only to an IPD admin."""
    to = params["state"]
    b = _one_bed(_beds(await _get_or_refuse(read, f"{_IPD}/board", need="IPD")), params["bed"])
    if b.get("status") in ("occupied", "reserved"):
        raise EmptyPlan(f"Bed {b.get('code')} is {b.get('status')}: it changes when the patient moves or leaves.")
    if b.get("status") == to:
        raise EmptyPlan(f"Bed {b.get('code')} is already {to}.")
    reason = params.get("reason")
    if to in ("maintenance", "blocked") and not reason:
        raise EmptyPlan(f"Say why bed {b.get('code')} is out of service, e.g. \"because the oxygen point is broken\".")
    body = {"status": to, **({"reason": reason} if reason else {})}

    async def verify(read, data, call):
        out = await read(_call("GET", f"{_IPD}/board"))
        row = next((x for x in _beds(out.data) if str(x.get("id")) == str(b["id"])), None) if out.status == "ok" else None
        if row is None:
            return None, "Couldn't read the bed board back"
        return (row.get("status") == to), f"Bed {row.get('code')}: {row.get('status')}"

    label = {"available": "free (ready for a patient)", "cleaning": "for cleaning",
             "maintenance": "under maintenance", "blocked": "blocked"}[to]
    return {"title": f"Mark bed {b.get('code')} {label}",
            "bullets": [f"Bed {b.get('code')}: {b.get('status')} → {to}."] + ([f"Reason: {reason}."] if reason else []),
            "warnings": ["Admissions can use it straight away."] if to == "available" else [],
            "snapshot": {"id": str(b["id"]), "status": b.get("status"), "version": b.get("version")},
            "steps": [_step(f"Mark bed {b.get('code')} {to}", _call("POST", f"{_IPD}/beds/{b['id']}/status", json=body),
                            verify=verify)]}


_SERVICES = "/admin/masters/services"
_SERVICE_NOISE = {"charge", "charges", "price", "prices", "rate", "rates", "tariff", "service", "services", "procedure",
                  "for", "the", "of", "a", "cost"}


async def service_price_preview(read, params: dict) -> dict:
    """One tariff row by its name words; the write carries the row's updated_at so a change made on the page
    meanwhile is refused (409) instead of overwritten. Bills already issued keep their price."""
    # price words are how it was asked, not part of the service's name ("room charges" -> "room")
    words = [w for w in params["service"].split() if w not in _SERVICE_NOISE] or params["service"].split()
    rows = [s for s in (await _get_or_refuse(read, _SERVICES, {"active_only": True}, "Services Manage") or {})
            .get("services") or [] if isinstance(s, dict)]
    found = [s for s in rows if all(w in str(s.get("name") or "").lower() for w in words)]
    exact = [s for s in found if " ".join(str(s.get("name") or "").lower().split()) == " ".join(words)]
    s = _only(exact or found, params["service"], "service",
              lambda s: f"{s.get('name')} ({present.fmt(s.get('price_paise'), 'paise')})")
    if s.get("source") != "master" or not s.get("id"):
        raise EmptyPlan(f"{s.get('name')} is an older price-list entry. Save it once on the Service Catalogue page.")
    old = Decimal(int(s.get("price_paise") or 0)) / 100
    new = new_price(old, params["price"])
    if not 0 < new <= 1_000_000:
        raise EmptyPlan(f"The new price for {s.get('name')} would be {_inr(new)}.")
    if new == old:
        raise EmptyPlan(f"{s.get('name')} is already {_inr(new)}.")
    sid = str(s["id"])

    async def verify(read, data, call):
        out = await read(_call("GET", _SERVICES, {"active_only": True}))
        rows = (out.data or {}).get("services") if out.status == "ok" and isinstance(out.data, dict) else None
        row = next((r for r in rows or [] if isinstance(r, dict) and str(r.get("id")) == sid), None)
        if row is None:
            return None, "Couldn't read the service back"
        return (int(row.get("price_paise") or 0) == new * 100), f"{row.get('name')}: {present.fmt(row.get('price_paise'), 'paise')}"

    return {"title": f"Change {s.get('name')}'s price",
            "bullets": [f"{s.get('name')}: {_inr(int(old))} → {_inr(new)}."],
            "warnings": ["New bills use the new price; bills already issued keep theirs."],
            "snapshot": {"id": sid, "price": s.get("price_paise"), "updated_at": s.get("updated_at")},
            "steps": [_step(f"Save {s.get('name')}'s price",
                            _call("PATCH", f"{_SERVICES}/{sid}",
                                  json={"standard_price_paise": new * 100,
                                        **({"expected_updated_at": s["updated_at"]} if s.get("updated_at") else {})}),
                            verify=verify)]}


# ── Voice-first batch 3: refunds, voids, deposits, casualty registration, theatre booking ──

async def _bill_for(read, who: str) -> list:
    """The patient's OPD invoices, by name or by today's token."""
    if who.startswith("bed:"):
        raise EmptyPlan("Say the patient's name or token for the OPD bill.")
    if not who.startswith("#"):
        return await _invoices(read, who)
    queue = _queue_of(await _get_or_refuse(read, "/admin/opd/queue", {"date": _now().date().isoformat()},
                                           "OPD Front Desk"))
    r = _only([q for q in queue if str(q.get("token_number")) == who[1:]], f"token {who[1:]}", "token today", _token)
    return [i for i in await _invoices(read, str(r.get("patient_name") or "").lower())
            if str(i.get("appointment_id")) == str(r.get("appointment_id"))]


async def refund_preview(read, params: dict) -> dict:
    """R3, typed rupees, OPD admin only (the route). Never more than was paid; the key comes from the invoice,
    what it holds as paid, and this refund, so a double confirm returns one refund."""
    who, mode, rupees = params["who"], params["mode"], int(Decimal(params["amount"]))
    if mode == "card":
        raise EmptyPlan("Card payments are refunded through the card machine; record it on the OPD Billing page.")
    paid = [i for i in await _bill_for(read, who) if int(i.get("paid_paise") or 0) > 0]
    if not paid:
        raise EmptyPlan(f'Nothing has been paid on "{who}"\'s bills, so there is nothing to refund.')
    inv = _only(paid, who, "paid invoice", _inv_line)
    iid = str(inv.get("id"))
    inv = await _get_or_refuse(read, f"{_INVOICES}/{iid}")
    held = int(inv.get("paid_paise") or 0)
    if rupees * 100 > held:
        raise EmptyPlan(f"{_inr(rupees)} is more than the {present.fmt(held, 'paise')} paid on "
                        f"{inv.get('invoice_number') or 'the bill'}.")
    ref = params.get("reference")
    key = hashlib.sha256(f"ketu-refund:{iid}:{held}:{rupees}:{mode}:{ref or ''}".encode()).hexdigest()[:40]
    body = {"mode": mode, "amount_paise": rupees * 100, "reason": params["reason"], "idempotency_key": key,
            **({"reference": ref} if ref else {})}
    return {"title": f"Refund {_inr(rupees)} to {_inv_name(inv)}",
            "bullets": [f"{inv.get('invoice_number') or 'Invoice'}: {present.fmt(held, 'paise')} paid.",
                        f"{mode.upper()} refund {_inr(rupees)}. Reason: {params['reason']}."],
            "warnings": ["Cash refunds come out of your open cash drawer shift."] if mode == "cash" else [],
            "affected": rupees, "snapshot": {"id": iid, "paid": held},
            "steps": [_step("Record the refund", _call("POST", f"{_INVOICES}/{iid}/refunds", json=body),
                            verify=_invoice_is(iid, lambda i: int(i.get("paid_paise") or 0) == held - rupees * 100,
                                               lambda i: f"Refunded; {present.fmt(i.get('paid_paise'), 'paise')} "
                                                         "still paid"))]}


async def void_preview(read, params: dict) -> dict:
    """R3: the typed number is the bill's total in rupees. Only an unpaid bill is voided (the server agrees)."""
    who = params["who"]
    rows = [i for i in await _bill_for(read, who) if i.get("status") in ("draft", "issued")]
    if not rows:
        raise EmptyPlan(f'No open bill for "{who}" to void.')
    inv = _only(rows, who, "open invoice", _inv_line)
    iid = str(inv.get("id"))
    inv = await _get_or_refuse(read, f"{_INVOICES}/{iid}")
    if int(inv.get("paid_paise") or 0) > 0:
        raise EmptyPlan(f"{inv.get('invoice_number') or 'The bill'} has {present.fmt(inv.get('paid_paise'), 'paise')} "
                        "paid: refund it first, then void.")
    total = int(inv.get("total_paise") or 0) // 100
    return {"title": f"Void {_inv_name(inv)}'s bill",
            "bullets": [f"{inv.get('invoice_number') or 'Draft'}: {present.fmt(inv.get('total_paise'), 'paise')}.",
                        f"Reason: {params['reason']}."],
            "warnings": ["A voided bill can't be used again; make a new one if the visit is billed later."],
            "affected": total, "snapshot": {"id": iid, "status": inv.get("status"), "updated_at": inv.get("updated_at")},
            "steps": [_step("Void the bill", _call("POST", f"{_INVOICES}/{iid}/void", json={"reason": params["reason"]}),
                            verify=_invoice_is(iid, lambda i: i.get("status") == "void", lambda i: "Voided"))]}


async def deposit_preview(read, params: dict) -> dict:
    """R3, typed rupees: an inpatient's advance. The key comes from the stay, its ledger so far and this deposit."""
    mode, ref, rupees = params["mode"], params.get("reference"), int(Decimal(params["amount"]))
    if mode in ("upi", "card") and not ref:
        raise EmptyPlan(f"A {mode.upper()} deposit needs its transaction reference (UTR / RRN).")
    b = await _inpatient_bed(read, params["target"])
    adm = str(b["occupant"]["admission_id"])
    ledger = await _get_or_refuse(read, f"{_IPD}/admissions/{adm}/deposits", need="IPD Billing")
    rows = [d for d in (ledger or {}).get("deposits") or [] if isinstance(d, dict)]
    held = sum(int(d.get("amount_paise") or 0) for d in rows if d.get("kind") == "deposit")
    key = hashlib.sha256(f"ketu-deposit:{adm}:{len(rows)}:{rupees}:{mode}:{ref or ''}".encode()).hexdigest()[:40]
    body = {"mode": mode, "amount_paise": rupees * 100, "idempotency_key": key, **({"reference": ref} if ref else {})}

    async def verify(read, data, call):
        did = data.get("id") if isinstance(data, dict) else None
        out = await read(_call("GET", f"{_IPD}/admissions/{adm}/deposits"))
        rows = (out.data or {}).get("deposits") if out.status == "ok" and isinstance(out.data, dict) else None
        if rows is None:
            return None, "Couldn't read the deposits back"
        ok = any(isinstance(d, dict) and (str(d.get("id")) == str(did) or d.get("idempotency_key") == key) for d in rows)
        return ok, "On the stay's deposits" if ok else "Not on the stay's deposits"

    return {"title": f"Take {_inr(rupees)} deposit for {_bed_line(b)}",
            "bullets": [f"{mode.upper()} {_inr(rupees)}" + (f", ref {ref}" if ref else "") + ".",
                        f"Deposits so far: {present.fmt(held, 'paise')}."],
            "warnings": ["Cash goes into your open cash drawer shift."] if mode == "cash" else [],
            "affected": rupees, "snapshot": {"admission_id": adm, "ledger": len(rows)},
            "steps": [_step("Record the deposit", _call("POST", f"{_IPD}/admissions/{adm}/deposits", json=body),
                            verify=verify)]}


_ER_VITALS = {"bp_systolic": "bp_sys", "bp_diastolic": "bp_dia", "pulse_bpm": "pulse", "spo2_pct": "spo2",
              "temperature_c": "temp", "resp_rate": "rr", "blood_glucose_mg_dl": "grbs"}


async def emergency_register_preview(read, params: dict) -> dict:
    """Rapid casualty registration. Unknown patients get a temporary name from the server. Black (dead on
    arrival) is never registered by voice."""
    triage = params["triage"]
    if triage == "black":
        raise EmptyPlan("A patient brought dead is registered on the Casualty page by the doctor.")
    body = {"triage_category": triage, "chief_complaints": params["complaint"], "arrival_mode": params["arrival"],
            "idempotency_key": hashlib.sha256(f"ketu-er:{params['said_at']}:{params['complaint']}".encode())
            .hexdigest()[:40]}
    if params.get("unknown") == "yes":
        body["is_unknown"] = True
        body.update({k: v for k, v in (("estimated_gender", params.get("gender")),
                                       ("estimated_age", params.get("age"))) if v is not None})
        who = "Unknown patient" + (f", {params['gender']}" if params.get("gender") else "") + \
            (f", about {params['age']}" if params.get("age") is not None else "")
    elif params.get("name") and params.get("phone"):
        body.update({"name": params["name"], "phone": params["phone"]})
        body.update({k: v for k, v in (("gender", params.get("gender")), ("age", params.get("age"))) if v is not None})
        who = f"{params['name']} ({params['phone']})"
    else:
        raise EmptyPlan("Say the patient's name and phone, or \"unknown patient\".")
    vitals = {_ER_VITALS[k]: v for k, v in (params.get("vitals") or {}).items() if k in _ER_VITALS}
    if vitals:
        body["initial_vitals"] = vitals

    async def verify(read, data, call):
        vid = data.get("id") if isinstance(data, dict) else None
        out = await read(_call("GET", "/admin/emergency/visits"))
        rows = (out.data or {}).get("visits") if out.status == "ok" and isinstance(out.data, dict) else None
        if rows is None or not vid:
            return None, "Couldn't read the casualty board back"
        row = next((v for v in rows if isinstance(v, dict) and str(v.get("id")) == str(vid)), None)
        return (row is not None), (f"On the casualty board: {row.get('visit_number') or ''}".strip() if row
                                   else "Not on the casualty board")

    return {"title": f"Register a {triage.upper()} casualty",
            "bullets": [who + ".", f"Complaint: {params['complaint']}.", f"Arrived: {params['arrival'].replace('_', ' ')}."]
            + _vital_lines({k: v for k, v in (params.get("vitals") or {}).items() if k in _ER_VITALS}),
            "warnings": ["Medico-legal details (police intimation) are recorded on the Casualty page."],
            "snapshot": None,
            "steps": [_step("Register the casualty", _call("POST", "/admin/emergency/register", json=body),
                            verify=verify)]}


async def ot_schedule_preview(read, params: dict) -> dict:
    """An admitted patient, a theatre, a surgeon and a time; the server refuses a theatre or patient already
    booked then. The key is the stay, theatre and start, so a double confirm books once."""
    opts = await _get_or_refuse(read, "/admin/ot/options", need="OT Schedule")
    target = params["target"]
    adms = [a for a in (opts or {}).get("admissions") or [] if isinstance(a, dict)]
    if target.startswith("bed:"):
        found = [a for a in adms if _same_code(a.get("bed"), _bed_ref(target[4:])[0])]
    elif target.startswith("#"):
        raise EmptyPlan("Surgery is booked for an admitted patient: say the bed or the patient's name.")
    else:
        found = _named(adms, target, lambda a: a.get("patient_name"))
    a = _only(found, target.replace("bed:", "bed "), "admitted patient",
              lambda a: f"{a.get('patient_name')} (bed {a.get('bed') or '—'})")
    theatres = [t for t in (opts or {}).get("theatres") or [] if isinstance(t, dict)]
    if params.get("theatre"):
        want = params["theatre"].lower()
        pick = [t for t in theatres if re.search(rf"(?<![a-z0-9]){re.escape(want)}(?![a-z0-9])", str(t.get("name")).lower())]
    else:
        pick = theatres if len(theatres) == 1 else []
    if len(pick) != 1:
        names = ", ".join(str(t.get("name")) for t in theatres[:8]) or "none"
        raise EmptyPlan(f"Say which theatre ({names}).")
    th = pick[0]
    doc = next((d for d in (opts or {}).get("doctors") or [] if isinstance(d, dict) and d.get("name") == params["surgeon"]),
               None)
    if not doc:
        raise EmptyPlan(f"{params['surgeon']} isn't an active doctor here.")
    day = date.fromisoformat(params["date"])
    if day < _now().date():
        raise EmptyPlan("That date has passed.")
    start, end = params["window"].split("-")
    start_at = datetime.combine(day, time.fromisoformat(start), IST)
    end_at = datetime.combine(day, time.fromisoformat(end), IST)
    if start_at <= _now():
        raise EmptyPlan("That time has passed.")
    key = hashlib.sha256(f"ketu-ot:{a['id']}:{th['id']}:{start_at.isoformat()}".encode()).hexdigest()[:40]
    body = {"admission_id": str(a["id"]), "theatre_id": str(th["id"]), "procedure_name": params["procedure"],
            "scheduled_start": start_at.isoformat(), "scheduled_end": end_at.isoformat(),
            "primary_surgeon_id": str(doc["id"]), "priority": params["priority"],
            "pre_op_diagnosis": params["indication"], "idempotency_key": key}
    proc = next((s for s in (opts or {}).get("services") or [] if isinstance(s, dict)
                 and str(s.get("name") or "").lower() == params["procedure"].lower()), None)
    if proc:
        body["service_id"] = str(proc["id"])

    async def verify(read, data, call):
        sid = data.get("id") if isinstance(data, dict) else None
        out = await read(_call("GET", "/admin/ot/schedule", {"date": day.isoformat()}))
        rows = (out.data or {}).get("surgeries") if out.status == "ok" and isinstance(out.data, dict) else None
        if rows is None or not sid:
            return None, "Couldn't read the theatre list back"
        row = next((s for s in rows if isinstance(s, dict) and str(s.get("id")) == str(sid)), None)
        return (row is not None), (f"On the theatre list: {row.get('surgery_number') or ''}".strip() if row
                                   else "Not on the theatre list")

    return {"title": f"Book {params['procedure']} for {a.get('patient_name')}",
            "bullets": [f"{a.get('patient_name')} (bed {a.get('bed') or '—'}), {th.get('name')}.",
                        f"{present.fmt(day.isoformat(), 'date')} {start}–{end} with {doc.get('name')}.",
                        f"Pre-op: {params['indication']}. Priority: {params['priority']}."]
            + ([f"Charge: {proc.get('name')} {present.fmt(proc.get('standard_price_paise'), 'paise')}."] if proc else []),
            "warnings": ["The WHO checklist, anaesthesia record and post-op note are done in the theatre."],
            "snapshot": {"admission": str(a["id"]), "theatre": str(th["id"])},
            "steps": [_step("Book the surgery", _call("POST", "/admin/ot/surgeries", json=body), verify=verify)]}


# ── Voice-first batch 4: admission details, discharge completion, OT status, staff logins, branches ──

_PATCH_LABELS = {"payer_type": "Payer", "payer_name": "Payer name", "policy_number": "Policy / card no.",
                 "attendant_name": "Attendant", "attendant_relation": "Relation", "attendant_phone": "Attendant phone",
                 "expected_discharge_on": "Expected discharge"}


async def admission_update_preview(read, params: dict) -> dict:
    """Administrative fields only (the trigger refuses clinical and state fields); old -> new from a fresh read."""
    b = await _inpatient_bed(read, params["target"])
    adm = str(b["occupant"]["admission_id"])
    row = await _ipd_admission(read, adm)
    if row is None:
        raise RuntimeError("read admission failed")
    patch = {k: v for k, v in params["changes"].items() if str(row.get(k) or "") != str(v)}
    if not patch:
        raise EmptyPlan(f"{_bed_line(b)} already has those details.")
    if patch.get("expected_discharge_on") and patch["expected_discharge_on"] < _now().date().isoformat():
        raise EmptyPlan("The expected discharge date has passed.")
    bullets = [f"{_PATCH_LABELS[k]}: {row.get(k) or 'none'} → {str(v).replace('_', ' ')}." for k, v in patch.items()]

    async def verify(read, data, call):
        now = await _ipd_admission(read, adm)
        if now is None:
            return None, "Couldn't read the admission back"
        ok = all(str(now.get(k) or "") == str(v) for k, v in patch.items())
        return ok, "Saved on the admission" if ok else "The admission shows other details"

    return {"title": f"Update {_bed_line(b)}'s admission", "bullets": bullets,
            "snapshot": {"admission_id": adm, **{k: row.get(k) for k in patch}},
            "steps": [_step("Save the admission details", _call("PATCH", f"{_IPD}/admissions/{adm}", json=patch),
                            verify=verify)]}


_CHECK_LABELS = {"discharge_initiated": "discharge started", "summary_signed": "discharge summary signed",
                 "orders_closed": "active orders closed", "charges_reviewed": "proposed charges reviewed",
                 "discount_decided": "discount requests decided", "final_bill_issued": "final bill issued",
                 "all_charges_billed": "every charge on the final bill", "deposits_cleared": "deposits applied or refunded",
                 "bill_settled": "bill paid"}


async def discharge_complete_preview(read, params: dict) -> dict:
    """Completes a discharge only when the stay's own checklist is all done; a settlement waiver is an IPD
    admin's decision on the chart, never by voice."""
    b = await _inpatient_bed(read, params["target"])
    adm = str(b["occupant"]["admission_id"])
    check = await _get_or_refuse(read, f"{_IPD}/admissions/{adm}/discharge/checklist", need="IPD")
    items = [i for i in (check or {}).get("items") or [] if isinstance(i, dict)]
    pending = [_CHECK_LABELS.get(i.get("key"), str(i.get("key")).replace("_", " ")) for i in items if not i.get("done")]
    if pending or not (check or {}).get("ready"):
        raise EmptyPlan(f"{_bed_line(b)} can't be discharged yet. Still to do: {', '.join(pending) or 'the checklist'}.")
    return {"title": f"Complete discharge for {_bed_line(b)}",
            "bullets": ["Every checklist item is done.", f"Bed {b.get('code')} goes for cleaning."],
            "warnings": ["This closes the stay: nothing more can be charged or charted on it."],
            "snapshot": {"admission_id": adm, "items": [i.get("key") for i in items]},
            "steps": [_step("Complete the discharge", _call("POST", f"{_IPD}/admissions/{adm}/discharge/complete",
                                                            json={}),
                            verify=_admission_is(adm, ("discharged",), "Discharged"))]}


_OT_TO = {"start": "in_theatre", "recovery": "recovery", "cancel": "cancelled", "postpone": "postponed"}
_OT_FROM = {"start": ("scheduled",), "recovery": ("in_theatre",), "cancel": ("scheduled", "in_theatre"),
            "postpone": ("scheduled",)}


async def ot_status_preview(read, params: dict) -> dict:
    """One of today's surgeries moves on (start / recovery) or is called off (cancel / postpone, reason said)."""
    action, day = params["action"], (params.get("date") or _now().date().isoformat())
    if action in ("cancel", "postpone") and not params.get("reason"):
        raise EmptyPlan(f"Say why the surgery is {'cancelled' if action == 'cancel' else 'postponed'}, "
                        "e.g. \"because the blood is not arranged\".")
    rows = [s for s in (await _get_or_refuse(read, "/admin/ot/schedule", {"date": day}, "OT") or {}).get("surgeries") or []
            if isinstance(s, dict) and s.get("status") in _OT_FROM[action]]
    target = params["target"]
    if target.startswith("bed:"):
        code, ward = _bed_ref(target[4:])
        beds = [x for x in _in_ward(_beds(await _get_or_refuse(read, f"{_IPD}/board", need="IPD")), ward)
                if _same_code(x.get("code"), code) and (x.get("occupant") or {}).get("admission_id")]
        adm = {str(x["occupant"]["admission_id"]) for x in beds}
        found = [s for s in rows if str(s.get("admission_id")) in adm]
    else:
        found = _named(rows, target, lambda s: s.get("patient_name"))
    s = _only(found, target.replace("bed:", "bed "), "surgery that can be " + {"start": "started", "recovery":
              "moved to recovery", "cancel": "cancelled", "postpone": "postponed"}[action],
              lambda s: f"{s.get('patient_name')}: {s.get('procedure_name')} at {present.fmt(s.get('scheduled_start'), 'time')}")
    sid, to = str(s["id"]), _OT_TO[action]
    body = {"action": action, **({"reason": params["reason"]} if action in ("cancel", "postpone") else {})}

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/ot/schedule", {"date": day}))
        rows = (out.data or {}).get("surgeries") if out.status == "ok" and isinstance(out.data, dict) else None
        row = next((r for r in rows or [] if isinstance(r, dict) and str(r.get("id")) == sid), None)
        if row is None:
            return None, "Couldn't read the theatre list back"
        return (row.get("status") == to), f"Now {str(row.get('status')).replace('_', ' ')}"

    warnings = {"start": ["The WHO sign-in should be done before the patient goes in."],
                "cancel": ["The theatre slot is freed; tell the patient's family."],
                "postpone": ["Book the new time with \"book … surgery\" when it is fixed."]}.get(action, [])
    return {"title": f"{action.capitalize()} {s.get('patient_name')}'s {s.get('procedure_name')}",
            "bullets": [f"{s.get('theatre_name') or 'Theatre'}, {present.fmt(s.get('scheduled_start'), 'time')}, "
                        f"{s.get('surgeon_name') or 'surgeon'}.",
                        f"{str(s.get('status')).replace('_', ' ')} → {to.replace('_', ' ')}."]
            + ([f"Reason: {params['reason']}."] if action in ("cancel", "postpone") else []),
            "warnings": warnings, "snapshot": {"id": sid, "status": s.get("status")},
            "steps": [_step(f"{action.capitalize()} the surgery", _call("POST", f"/admin/ot/surgeries/{sid}/status",
                                                                        json=body), verify=verify)]}


async def _staff_row(read, who: str) -> dict:
    rows = [r for r in (await _get_or_refuse(read, "/admin/staff", need="Staff View") or {}).get("staff") or []
            if isinstance(r, dict)]
    exact = [r for r in rows if who in (str(r.get("username") or "").lower(), str(r.get("full_name") or "").lower())]
    found = exact or _named(rows, who, lambda r: f"{r.get('full_name') or ''} {r.get('username') or ''}")
    return _only(found, who, "staff login",
                 lambda r: f"{r.get('full_name') or r.get('username')} ({r.get('username')}, {r.get('staff_role')})")


def _staff_name(r: dict) -> str:
    return str(r.get("full_name") or r.get("username"))


def _staff_after(sid: str, ok, said: str):
    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/staff"))
        rows = (out.data or {}).get("staff") if out.status == "ok" and isinstance(out.data, dict) else None
        row = next((r for r in rows or [] if isinstance(r, dict) and str(r.get("id")) == sid), None)
        if row is None:
            return None, "Couldn't read the staff list back"
        return (True, said) if ok(row) else (False, "The staff list shows otherwise")
    return verify


async def staff_active_preview(read, params: dict) -> dict:
    """Switching a login off ends the sessions it holds now and frees a phlebotomist's visits (the toggle route
    does both). The route toggles, so the preview's read of the current state is in the plan hash: a change
    made meanwhile means a new preview, never a flip the wrong way."""
    r = await _staff_row(read, params["who"])
    on = params["switch"] == "on"
    if bool(r.get("is_active")) == on:
        raise EmptyPlan(f"{_staff_name(r)}'s login is already {'on' if on else 'off'}.")
    sid = str(r["id"])
    warnings = (["They are signed out everywhere straight away."]
                + (["Their upcoming home visits go back to be reassigned."] if r.get("staff_role") == "PHLEBOTOMIST"
                   else [])) if not on else ["They can sign in again with their existing password."]
    return {"title": f"Switch {_staff_name(r)}'s login {'on' if on else 'off'}",
            "bullets": [f"{_staff_name(r)} ({r.get('username')}, {str(r.get('staff_role')).replace('_', ' ').lower()}): "
                        f"{'off → on' if on else 'on → off'}."],
            "warnings": warnings, "snapshot": {"id": sid, "active": r.get("is_active")},
            "steps": [_step("Switch the login", _call("PUT", f"/admin/staff/{sid}/toggle"),
                            verify=_staff_after(sid, lambda x: bool(x.get("is_active")) == on, "on" if on else "off"))]}


async def staff_role_preview(read, params: dict) -> dict:
    """The role's permission set replaces the old one (the route validates it and who may grant it); passwords
    and individual permissions stay on the Staff page."""
    r = await _staff_row(read, params["who"])
    role = params["role"]
    if r.get("staff_role") == role:
        raise EmptyPlan(f"{_staff_name(r)} is already {role.replace('_', ' ').lower()}.")
    sid = str(r["id"])
    return {"title": f"Make {_staff_name(r)} {role.replace('_', ' ').lower()}",
            "bullets": [f"Role: {str(r.get('staff_role')).replace('_', ' ').lower()} → {role.replace('_', ' ').lower()}."],
            "warnings": ["Their permissions change to the new role's, and they are signed out to pick them up."],
            "snapshot": {"id": sid, "role": r.get("staff_role")},
            "steps": [_step("Change the role", _call("PUT", f"/admin/staff/{sid}", json={"staff_role": role}),
                            verify=_staff_after(sid, lambda x: x.get("staff_role") == role, f"Now {role}"))]}


async def _branch_list(read) -> list:
    return branch_rows(await _get_or_refuse(read, "/admin/branches", need="Branches"))


def _branch_after(ok, said: str):
    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/branches"))
        if out.status != "ok":
            return None, "Couldn't read the branches back"
        return (True, said) if any(ok(b) for b in branch_rows(out.data)) else (False, "Not on the branch list")
    return verify


async def branch_create_preview(read, params: dict) -> dict:
    name = params["name"]
    branches = await _branch_list(read)
    same = [b for b in branches if name.lower() in (str(b.get("short_name") or "").lower(), str(b.get("name") or "").lower())]
    if same:
        raise EmptyPlan(f"{same[0].get('name')} is already a branch.")
    return {"title": f"Add the {name} branch",
            "bullets": [f"Locality: {name}. Address, phone and map link are added on the Branches page."],
            "warnings": ["Patients see the branch on WhatsApp once it has doctors assigned."],
            "snapshot": {"branches": sorted(str(b.get("id")) for b in branches)},
            "steps": [_step(f"Add {name}", _call("POST", "/admin/branches", json={"short_name": name}),
                            verify=_branch_after(lambda b: str(b.get("short_name") or "").lower() == name.lower(),
                                                 "On the branch list"))]}


async def branch_rename_preview(read, params: dict) -> dict:
    old, new = params["rename"]
    branches = await _branch_list(read)
    found = [b for b in branches if old in (str(b.get("short_name") or "").lower(), str(b.get("name") or "").lower())] \
        or [b for b in branches if old in str(b.get("name") or "").lower()]
    b = _only(found, old, "branch", lambda b: str(b.get("name")))
    if str(b.get("short_name") or "") == new:
        raise EmptyPlan(f"The branch is already called {new}.")
    bid = str(b["id"])
    return {"title": f"Rename {b.get('short_name') or b.get('name')} to {new}",
            "bullets": [f"Locality name: {b.get('short_name') or b.get('name')} → {new}."],
            "warnings": ["Patients see the new name on WhatsApp straight away."],
            "snapshot": {"id": bid, "short_name": b.get("short_name"), "name": b.get("name")},
            "steps": [_step("Rename the branch", _call("PUT", f"/admin/branches/{bid}", json={"short_name": new}),
                            verify=_branch_after(lambda x: str(x.get("id")) == bid and x.get("short_name") == new,
                                                 f"Now {new}"))]}


# ── Voice-first batch 5: doctor days / slot length, new services, credit notes, corporate companies ──

async def _upcoming(read, doc: dict) -> list:
    today = _now().date()
    return [a for a in await _bookings(read, doc["name"], today, today + timedelta(days=SCHEDULE_LOOKAHEAD_DAYS))
            if a.get("doctor_name") == doc["name"]]


async def doctor_days_preview(read, params: dict) -> dict:
    doc = await _doctor(read, params["doctor"])
    new, old = params["days"], str(doc.get("available_days") or "")
    if set(new.split(",")) == set(filter(None, old.split(","))):
        raise EmptyPlan(f"{doc.get('name')} already works {old}.")
    dropped = set(filter(None, old.split(","))) - set(new.split(","))
    outside = [a for a in await _upcoming(read, doc) if date.fromisoformat(str(a.get("appointment_date"))[:10])
               .strftime("%a") in dropped]
    warnings = ["Patients booking on WhatsApp see the new days straight away."]
    if outside:
        warnings.insert(0, f"{_plural(len(outside), 'booking')} in the next {SCHEDULE_LOOKAHEAD_DAYS} days "
                           f"{'falls' if len(outside) == 1 else 'fall'} on a day dropped. They are not moved: "
                           "cancel or call them from the Appointments page.")
    return {"title": f"Change {doc.get('name')}'s working days", "bullets": [f"Days: {old or 'none'} → {new}."],
            "warnings": warnings, "rows": _booking_rows(outside),
            "snapshot": {"id": str(doc["id"]), "days": old, "outside": sorted(str(a.get("id")) for a in outside)},
            "steps": [_step(f"Save {doc.get('name')}'s days",
                            _call("PUT", f"/admin/doctors/{doc['id']}", json={"available_days": new}),
                            verify=_doctor_after(str(doc["id"]), lambda r: set(str(r.get("available_days") or "")
                                                                               .split(",")) == set(new.split(",")),
                                                 f"days {new}"))]}


async def doctor_slot_preview(read, params: dict) -> dict:
    """The endpoint regenerates slots only when it is sent the shifts, so the body is the doctor's current
    shifts with the new length (D-34). Bookings at times the new grid no longer has are listed, not moved."""
    doc = await _doctor(read, params["doctor"])
    minutes = int(params["minutes"])
    old = int(doc.get("slot_duration_minutes") or 30)
    if minutes == old:
        raise EmptyPlan(f"{doc.get('name')}'s slots are already {minutes} minutes.")
    cur = {k: _hm(doc.get(k)) for k in _SHIFTS}
    keep, bullets = set(), [f"Slot length: {old} → {minutes} minutes."]
    for s in ("morning", "evening"):
        if cur[f"{s}_start"] and cur[f"{s}_end"]:
            grid = _slots(cur[f"{s}_start"], cur[f"{s}_end"], minutes)
            keep |= set(grid)
            bullets.append(f"{s.capitalize()} {cur[f'{s}_start']}–{cur[f'{s}_end']}: {len(grid)} slots.")
    if not keep:
        raise EmptyPlan(f"{doc.get('name')} has no hours set: set them first (\"Dr … morning 9 to 1\").")
    outside = [a for a in await _upcoming(read, doc) if str(a.get("appointment_time") or "")[:5] not in keep]
    warnings = ["Patients booking on WhatsApp see the new slots straight away."]
    if outside:
        warnings.insert(0, f"{_plural(len(outside), 'booking')} in the next {SCHEDULE_LOOKAHEAD_DAYS} days "
                           f"{'is' if len(outside) == 1 else 'are'} at a time the new slots don't have. They are kept "
                           "as booked.")
    body = {**{k: v for k, v in cur.items() if v}, "slot_duration_minutes": minutes}
    return {"title": f"Change {doc.get('name')}'s slot length", "bullets": bullets, "warnings": warnings,
            "rows": _booking_rows(outside),
            "snapshot": {"id": str(doc["id"]), "shifts": cur, "minutes": old,
                         "outside": sorted(str(a.get("id")) for a in outside)},
            "steps": [_step(f"Save {doc.get('name')}'s slots", _call("PUT", f"/admin/doctors/{doc['id']}", json=body),
                            verify=_doctor_after(str(doc["id"]),
                                                 lambda r: int(r.get("slot_duration_minutes") or 0) == minutes,
                                                 f"{minutes}-minute slots"))]}


def _service_code(name: str, taken: set) -> str:
    base = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:36] or "SVC"
    code, n = base, 2
    while code in taken:
        code, n = f"{base}-{n}", n + 1
    return code


async def service_create_preview(read, params: dict) -> dict:
    """A new tariff row from its name and price; the code is made from the name (unique), GST 0% (healthcare
    services are exempt) and SAC 999312 unless changed on the page afterwards."""
    name = params["name"]
    kind, value = params["price"][0], Decimal(params["price"][1])
    if kind != "amount" or not 0 < value <= 1_000_000:
        raise EmptyPlan("Say the price in rupees, e.g. \"at ₹300\".")
    rows = [s for s in (await _get_or_refuse(read, _SERVICES, None, "Services Manage") or {}).get("services") or []
            if isinstance(s, dict)]
    same = [s for s in rows if " ".join(str(s.get("name") or "").lower().split()) == name.lower()]
    if same:
        raise EmptyPlan(f"{same[0].get('name')} is already in the catalogue at "
                        f"{present.fmt(same[0].get('price_paise'), 'paise')}. Change its price instead.")
    code = _service_code(name, {str(s.get("code") or "") for s in rows})
    rupees = int(value)
    body = {"service_code": code, "name": name, "category": params["category"], "standard_price_paise": rupees * 100}

    async def verify(read, data, call):
        out = await read(_call("GET", _SERVICES))
        rows = (out.data or {}).get("services") if out.status == "ok" and isinstance(out.data, dict) else None
        if rows is None:
            return None, "Couldn't read the catalogue back"
        ok = any(isinstance(s, dict) and s.get("code") == code for s in rows)
        return ok, f"In the catalogue as {code}" if ok else "Not in the catalogue"

    return {"title": f"Add {name} to the service catalogue",
            "bullets": [f"{name} ({params['category'].replace('_', ' ')}): {_inr(rupees)}.", f"Code {code}, GST 0%."],
            "warnings": ["Change GST or SAC on the Service Catalogue page if this service is taxable."],
            "snapshot": {"codes": len(rows)},
            "steps": [_step(f"Add {name}", _call("POST", _SERVICES, json=body), verify=verify)]}


_CN_LABEL = {"billing_error": "billing error", "cancellation": "service not given", "discount_post_issue": "discount",
             "service_deficiency": "service deficiency", "other": "other"}


async def credit_note_preview(read, params: dict) -> dict:
    """R3, typed rupees. A credit note lowers an issued bill; when the patient has paid more than the bill
    now asks for, the excess is refunded in cash or UPI, which must be said."""
    who, rupees, code = params["who"], int(Decimal(params["amount"])), params["code"]
    rows = [i for i in await _bill_for(read, who) if i.get("status") in ("issued", "partially_paid", "paid")]
    if not rows:
        raise EmptyPlan(f'No issued bill for "{who}" to credit.')
    inv = _only(rows, who, "issued bill", _inv_line)
    iid = str(inv.get("id"))
    inv = await _get_or_refuse(read, f"{_INVOICES}/{iid}")
    credited = int(inv.get("credited_paise") or 0)
    due = int(inv.get("subtotal_paise") or 0) - int(inv.get("discount_paise") or 0) - credited
    if rupees * 100 > due:
        raise EmptyPlan(f"{_inr(rupees)} is more than the {present.fmt(due, 'paise')} left to credit on "
                        f"{inv.get('invoice_number') or 'the bill'}.")
    paid, after = int(inv.get("paid_paise") or 0), int(inv.get("total_paise") or 0) - credited - rupees * 100
    excess = max(paid - after, 0)
    mode, ref = params.get("mode"), params.get("reference")
    if excess and mode not in ("cash", "upi"):
        raise EmptyPlan(f"The patient has paid {present.fmt(excess, 'paise')} more than the bill will ask for: say "
                        "\"refund in cash\" or \"by UPI\" with the credit note.")
    if excess and mode == "upi" and not ref:
        raise EmptyPlan("A UPI refund needs its transaction reference (UTR).")
    notes = params.get("notes")
    if code == "other" and not notes:
        raise EmptyPlan("Say why, e.g. \"because the patient was charged twice\".")
    key = hashlib.sha256(f"ketu-cn:{iid}:{credited}:{rupees}:{code}".encode()).hexdigest()[:40]
    body = {"amount_paise": rupees * 100, "reason": code, "idempotency_key": key,
            **({"reason_notes": notes} if notes else {}),
            **({"refund_mode": mode, **({"refund_reference": ref} if ref else {})} if excess else {})}

    async def verify(read, data, call):
        out = await read(_call("GET", f"{_INVOICES}/{iid}/credit-notes"))
        items = (out.data or {}).get("items") if out.status == "ok" and isinstance(out.data, dict) else None
        if items is None:
            return None, "Couldn't read the credit notes back"
        hit = next((c for c in items if isinstance(c, dict) and int(c.get("amount_paise") or 0) == rupees * 100
                    and (not isinstance(data, dict) or not data.get("id") or str(c.get("id")) == str(data["id"]))), None)
        return (hit is not None), (f"Issued {hit.get('credit_note_number') or ''}".strip() if hit else "Not issued")

    return {"title": f"Credit note of {_inr(rupees)} on {_inv_name(inv)}'s bill",
            "bullets": [f"{inv.get('invoice_number')}: {present.fmt(due, 'paise')} can be credited.",
                        f"Reason: {_CN_LABEL[code]}" + (f" ({notes})" if notes else "") + "."]
            + ([f"Refund {present.fmt(excess, 'paise')} by {mode.upper()}."] if excess else []),
            "warnings": ["A credit note is a GST document: it is numbered and cannot be undone."],
            "affected": rupees, "snapshot": {"id": iid, "credited": credited, "paid": paid},
            "steps": [_step("Issue the credit note", _call("POST", f"{_INVOICES}/{iid}/credit-notes", json=body),
                            verify=verify)]}


async def company_create_preview(read, params: dict) -> dict:
    name = params["name"]
    rows = [c for c in (await _get_or_refuse(read, "/admin/corporate-health/companies", need="Corporate Health Manage")
                        or {}).get("companies") or [] if isinstance(c, dict)]
    same = next((c for c in rows if " ".join(str(c.get("name") or "").lower().split()) == name.lower()), None)
    if same:
        raise EmptyPlan(f"{same.get('name')} is already a corporate client.")

    async def verify(read, data, call):
        out = await read(_call("GET", "/admin/corporate-health/companies"))
        rows = (out.data or {}).get("companies") if out.status == "ok" and isinstance(out.data, dict) else None
        if rows is None:
            return None, "Couldn't read the companies back"
        ok = any(isinstance(c, dict) and c.get("name") == name for c in rows)
        return ok, "On the corporate client list" if ok else "Not on the list"

    return {"title": f"Add {name} as a corporate client", "bullets": [f"Company: {name}."],
            "warnings": ["Upload its employee reports and add viewer logins on the Corporate Health page."],
            "snapshot": {"companies": len(rows)},
            "steps": [_step(f"Add {name}", _call("POST", "/admin/corporate-health/companies", json={"name": name}),
                            verify=verify)]}


# ── Voice-first batch 6: IPD setup (wards, beds, bed tariffs); IPD admin only (the routes) ──

async def _ipd_setup(read) -> dict:
    return await _get_or_refuse(read, f"{_IPD}/setup", need="IPD Admin") or {}


def _code_from(name: str, taken: set, limit: int = 20) -> str:
    base = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:limit - 3] or "W"
    code, n = base, 2
    while code.lower() in taken:
        code, n = f"{base}-{n}", n + 1
    return code


async def ward_create_preview(read, params: dict) -> dict:
    name, kind = params["name"], params["ward_type"]
    wards = [w for w in (await _ipd_setup(read)).get("wards") or [] if isinstance(w, dict)]
    same = next((w for w in wards if str(w.get("name") or "").lower() == name.lower()), None)
    if same:
        raise EmptyPlan(f"{same.get('name')} is already a ward.")
    code = _code_from(name, {str(w.get("code") or "").lower() for w in wards})

    async def verify(read, data, call):
        out = await read(_call("GET", f"{_IPD}/setup"))
        rows = (out.data or {}).get("wards") if out.status == "ok" and isinstance(out.data, dict) else None
        if rows is None:
            return None, "Couldn't read the wards back"
        ok = any(isinstance(w, dict) and w.get("code") == code for w in rows)
        return ok, f"Ward {code} added" if ok else "Not on the ward list"

    return {"title": f"Add the {name} ward", "bullets": [f"{name}: {kind.replace('_', ' ')} ward, code {code}."],
            "warnings": ["Add its beds next, e.g. \"add beds 1 to 10 to the " + name + " ward\"."],
            "snapshot": {"wards": sorted(str(w.get("code")) for w in wards)},
            "steps": [_step(f"Add {name}", _call("POST", f"{_IPD}/wards", json={"code": code, "name": name,
                                                                                 "ward_type": kind}), verify=verify)]}


_WARD_CATEGORY = {"semi_private": "semi_private", "private": "private", "icu": "icu", "nicu": "nicu", "picu": "picu",
                  "hdu": "hdu", "isolation": "isolation", "maternity": "general", "daycare": "daycare"}


async def beds_create_preview(read, params: dict) -> dict:
    """R3, typed count of beds. Codes are the range said, or the next numbers after the ward's own; a code
    already in the ward is refused before anything is written."""
    setup = await _ipd_setup(read)
    wards = [w for w in setup.get("wards") or [] if isinstance(w, dict) and w.get("is_active", True)]
    term = params["ward"]
    found = [w for w in wards if term in (str(w.get("name") or "").lower(), str(w.get("code") or "").lower())] \
        or [w for w in wards if all(x in f"{w.get('name')} {w.get('code')}".lower() for x in term.split())]
    ward = _only(found, term, "ward", lambda w: f"{w.get('name')} ({w.get('code')})")
    board_beds = [b for b in _beds(await _get_or_refuse(read, f"{_IPD}/board", need="IPD"))
                  if str(b.get("_ward")) in (str(ward.get("name")), str(ward.get("code")))]
    have = {str(b.get("code") or "").upper() for b in board_beds}
    plan = params["plan"]
    if "count" in plan:
        nums = [(m.group(1), int(m.group(2))) for c in have if (m := re.fullmatch(r"([A-Z-]*?)(\d+)", c))]
        prefix = Counter(p for p, _ in nums).most_common(1)[0][0] if nums else ""
        start = max((n for p, n in nums if p == prefix), default=0) + 1
        codes = [f"{prefix}{n}" for n in range(start, start + plan["count"])]
    else:
        codes = [f"{plan['prefix']}{n}" for n in range(plan["from"], plan["to"] + 1)]
    clash = [c for c in codes if c in have]
    if clash:
        raise EmptyPlan(f"{ward.get('name')} already has bed {', '.join(clash[:5])}. Say a range that is free.")
    category = params.get("category") or _WARD_CATEGORY.get(str(ward.get("ward_type")), "general")
    tariff = (setup.get("settings") or {}).get("bed_tariffs", {}).get(category)
    body = {"beds": [{"code": c, "bed_category": category} for c in codes]}

    async def verify(read, data, call):
        out = await read(_call("GET", f"{_IPD}/board"))
        if out.status != "ok":
            return None, "Couldn't read the bed board back"
        now = {str(b.get("code")).upper() for b in _beds(out.data) if str(b.get("_ward")) == str(ward.get("name"))}
        missing = [c for c in codes if c not in now]
        return (not missing), "On the bed board" if not missing else f"{len(missing)} not on the bed board"

    return {"title": f"Add {_plural(len(codes), 'bed')} to {ward.get('name')}",
            "bullets": [f"Beds {codes[0]}" + (f" to {codes[-1]}" if len(codes) > 1 else "") + f", {_cat_label(category, lower=True)}.",
                        f"Bed tariff: {present.fmt(tariff, 'paise')} a day." if tariff is not None else
                        f"No tariff is set for {_cat_label(category, lower=True)} beds yet."],
            "warnings": ["New beds are free for admission straight away."], "affected": len(codes),
            "snapshot": {"ward": str(ward.get("id")), "have": sorted(have)},
            "steps": [_step("Add the beds", _call("POST", f"{_IPD}/wards/{ward['id']}/beds", json=body), verify=verify)]}


def _cat_label(cat: str, lower: bool = False) -> str:
    """"icu" -> "ICU", "semi_private" -> "Semi private" (or "semi private" mid-sentence)."""
    words = [w.upper() if w in ("icu", "nicu", "picu", "hdu", "ccu") else w for w in cat.split("_")]
    text = " ".join(words)
    return text if lower or text[:1].isupper() else text[:1].upper() + text[1:]


async def bed_tariff_preview(read, params: dict) -> dict:
    """The settings write replaces the whole tariff table, so the body is every current tariff with this one
    changed: setting ICU never drops the general ward's price."""
    cat = params["category"]
    kind, value = params["price"][0], Decimal(params["price"][1])
    tariffs = dict(((await _ipd_setup(read)).get("settings") or {}).get("bed_tariffs") or {})
    old = Decimal(int(tariffs.get(cat) or 0)) / 100
    new = new_price(old, params["price"]) if kind != "amount" else int(value)
    if not 0 < new <= 1_000_000:
        raise EmptyPlan(f"A bed tariff of {_inr(new)} a day is outside ₹1 to ₹10,00,000.")
    if cat in tariffs and new * 100 == int(tariffs[cat]):
        raise EmptyPlan(f"{_cat_label(cat)} beds are already {_inr(new)} a day.")
    body = {"bed_tariffs": {**tariffs, cat: new * 100}}

    async def verify(read, data, call):
        out = await read(_call("GET", f"{_IPD}/setup"))
        got = ((out.data or {}).get("settings") or {}).get("bed_tariffs") if out.status == "ok" and isinstance(out.data, dict) else None
        if got is None:
            return None, "Couldn't read the tariffs back"
        ok = int(got.get(cat) or -1) == new * 100 and all(int(got.get(k) or -1) == int(v) for k, v in tariffs.items() if k != cat)
        return ok, f"{_cat_label(cat)}: {_inr(new)} a day" if ok else "The tariffs read back differ"

    was = _inr(int(old)) if cat in tariffs else "not set"
    others = [f"{_cat_label(k, lower=True)} {present.fmt(v, 'paise')}" for k, v in tariffs.items() if k != cat]
    return {"title": f"Set the {_cat_label(cat, lower=True)} bed tariff",
            "bullets": [f"{_cat_label(cat)} beds: {was} → {_inr(new)} a day."]
            + ([f"Unchanged: {', '.join(others)}."] if others else []),
            # ipd_accrue_bed_charges prices each day when it is charged, at the tariff of that moment
            "warnings": ["Bed days not charged yet use the new tariff, including earlier days of patients admitted "
                         "now; days already charged keep their price."],
            "snapshot": {"tariffs": tariffs},
            "steps": [_step("Save the tariff", _call("PUT", f"{_IPD}/settings", json=body), verify=verify)]}


# ── Voice-first batch 7: OPD / IPD settings, insurance payers and claims ──

_OPD_SETUP = "/admin/opd/setup"
_OPD_LABELS = {"vitals_required": ("Vitals", {True: "required", False: "optional"}),
               "billing_after_consult": ("Billing", {True: "after the consultation", False: "before the consultation"}),
               "auto_open_shift": ("Open a cash shift automatically", {True: "on", False: "off"}),
               "after_checkin_stage": ("After check-in", {"vitals_pending": "vitals", "waiting": "waiting",
                                                          "registered": "registered"}),
               "payment_modes": ("Payment modes", None)}


def _opd_value(key, value) -> str:
    names = _OPD_LABELS[key][1]
    if names is None:
        return ", ".join(str(v).upper() for v in value or []) or "none"
    return names.get(value, str(value)) if value is not None else "not set"


async def opd_setting_preview(read, params: dict) -> dict:
    current = (await _get_or_refuse(read, _OPD_SETUP, need="OPD Admin") or {}).get("settings") or {}
    patch = {k: v for k, v in params["changes"].items() if current.get(k) != v}
    if not patch:
        raise EmptyPlan("OPD is already set that way.")

    async def verify(read, data, call):
        out = await read(_call("GET", _OPD_SETUP))
        got = (out.data or {}).get("settings") if out.status == "ok" and isinstance(out.data, dict) else None
        if got is None:
            return None, "Couldn't read the OPD settings back"
        ok = all(got.get(k) == v for k, v in patch.items())
        return ok, "Saved" if ok else "The OPD settings read back differ"

    return {"title": "Change OPD settings",
            "bullets": [f"{_OPD_LABELS[k][0]}: {_opd_value(k, current.get(k))} → {_opd_value(k, v)}." for k, v in patch.items()],
            "warnings": ["The front desk and queue use this straight away."],
            "snapshot": {k: current.get(k) for k in patch},
            "steps": [_step("Save OPD settings", _call("PUT", f"{_OPD_SETUP}/settings", json=patch), verify=verify)]}


async def opd_room_preview(read, params: dict) -> dict:
    """The settings write replaces the whole room table, so every doctor's current room is sent with this one."""
    doc = await _doctor(read, params["doctor"])
    rooms = dict(((await _get_or_refuse(read, _OPD_SETUP, need="OPD Admin") or {}).get("settings") or {}).get("rooms") or {})
    did, room = str(doc["id"]), params["room"]
    if rooms.get(did) == room:
        raise EmptyPlan(f"{doc.get('name')} is already in {room}.")
    taken = [d.get("name") for d in await _get(read, "/admin/doctors") if isinstance(d, dict)
             and rooms.get(str(d.get("id"))) == room and str(d.get("id")) != did]
    body = {"rooms": {**rooms, did: room}}

    async def verify(read, data, call):
        out = await read(_call("GET", _OPD_SETUP))
        got = ((out.data or {}).get("settings") or {}).get("rooms") if out.status == "ok" and isinstance(out.data, dict) else None
        if got is None:
            return None, "Couldn't read the rooms back"
        ok = got.get(did) == room and all(got.get(k) == v for k, v in rooms.items() if k != did)
        return ok, f"{doc.get('name')}: {room}" if ok else "The rooms read back differ"

    return {"title": f"Put {doc.get('name')} in {room}",
            "bullets": [f"{doc.get('name')}: {rooms.get(did) or 'no room'} → {room}."],
            "warnings": (["Patients are called to this room on WhatsApp and the queue screen."]
                         + ([f"{', '.join(taken)} also {'uses' if len(taken) == 1 else 'use'} {room}."] if taken else [])),
            "snapshot": {"rooms": rooms},
            "steps": [_step("Save the room", _call("PUT", f"{_OPD_SETUP}/settings", json=body), verify=verify)]}


_IPD_LABELS = {"discharge_cutoff_hour": "Discharge cut-off", "reservation_minutes": "A reserved bed is held for"}


def _ipd_value(key, value) -> str:
    if value is None:
        return "not set"
    return f"{int(value):02d}:00" if key == "discharge_cutoff_hour" else f"{value} minutes"


async def ipd_settings_preview(read, params: dict) -> dict:
    current = (await _ipd_setup(read)).get("settings") or {}
    patch = {k: v for k, v in params["changes"].items() if current.get(k) != v}
    if not patch:
        raise EmptyPlan("IPD is already set that way.")

    async def verify(read, data, call):
        out = await read(_call("GET", f"{_IPD}/setup"))
        got = (out.data or {}).get("settings") if out.status == "ok" and isinstance(out.data, dict) else None
        if got is None:
            return None, "Couldn't read the IPD settings back"
        ok = all(got.get(k) == v for k, v in patch.items())
        return ok, "Saved" if ok else "The IPD settings read back differ"

    warnings = ["The cut-off decides whether the discharge day is charged as a bed day."] \
        if "discharge_cutoff_hour" in patch else []
    return {"title": "Change IPD settings",
            "bullets": [f"{_IPD_LABELS[k]}: {_ipd_value(k, current.get(k))} → {_ipd_value(k, v)}." for k, v in patch.items()],
            "warnings": warnings, "snapshot": {k: current.get(k) for k in patch},
            "steps": [_step("Save IPD settings", _call("PUT", f"{_IPD}/settings", json=patch), verify=verify)]}


_INS = "/admin/insurance"
_PAYER_LABEL = {"tpa": "TPA", "private_insurance": "insurer", "government_scheme": "government scheme",
                "corporate": "corporate payer"}


async def payer_create_preview(read, params: dict) -> dict:
    name, kind = params["name"], params["kind"]
    rows = [p for p in (await _get_or_refuse(read, f"{_INS}/payers", need="Insurance") or {}).get("payers") or []
            if isinstance(p, dict)]
    same = next((p for p in rows if str(p.get("name") or "").lower() == name.lower()), None)
    if same:
        raise EmptyPlan(f"{same.get('name')} is already in the payer list.")

    async def verify(read, data, call):
        out = await read(_call("GET", f"{_INS}/payers"))
        got = (out.data or {}).get("payers") if out.status == "ok" and isinstance(out.data, dict) else None
        if got is None:
            return None, "Couldn't read the payers back"
        ok = any(isinstance(p, dict) and p.get("name") == name for p in got)
        return ok, "In the payer list" if ok else "Not in the payer list"

    return {"title": f"Add {name}", "bullets": [f"{name}: {_PAYER_LABEL[kind]}."],
            "warnings": ["Add its portal, email and phone on the Insurance page."], "snapshot": {"payers": len(rows)},
            "steps": [_step(f"Add {name}", _call("POST", f"{_INS}/payers", json={"name": name, "payer_type": kind}),
                            verify=verify)]}


async def _claim(read, target: str) -> dict:
    rows = [c for c in (await _get_or_refuse(read, f"{_INS}/claims", {"view": "open"}, "Insurance") or {}).get("items")
            or [] if isinstance(c, dict)]
    if target.startswith("claim:"):
        found = [c for c in rows if str(c.get("claim_number") or "").upper().replace("-", "")
                 == target[6:].replace("-", "")]
    elif target.startswith("bed:"):
        b = await _inpatient_bed(read, target)
        found = [c for c in rows if str(c.get("admission_id")) == str(b["occupant"]["admission_id"])]
    else:
        found = _named(rows, target, lambda c: c.get("patient_name"))
    return _only(found, target.replace("claim:", "claim ").replace("bed:", "bed "), "open claim",
                 lambda c: f"{c.get('claim_number')} ({c.get('patient_name')}, {c.get('payer_name') or 'payer'})")


def _claim_after(cid: str, ok, said):
    async def verify(read, data, call):
        out = await read(_call("GET", f"{_INS}/claims/{cid}"))
        if out.status != "ok" or not isinstance(out.data, dict):
            return None, "Couldn't read the claim back"
        return (True, said(out.data)) if ok(out.data) else (False, f"The claim is {out.data.get('status')}")
    return verify


async def settlement_preview(read, params: dict) -> dict:
    """R3, typed rupees received. Disallowances are reason-coded on the claim page; the key comes from the
    claim, what it has been paid, and this payment, so a double confirm records one settlement."""
    c = await _claim(read, params["target"])
    received, tds = int(Decimal(params["amount"])), int(Decimal(params.get("tds") or 0))
    if received and not params.get("utr"):
        raise EmptyPlan("Say the UTR / NEFT reference of the payment, e.g. \"UTR AXIS123456\".")
    split = c.get("split") or {}
    left = int(split.get("payer_payable_paise") or 0) - int(split.get("payer_received_paise") or 0) \
        - int(split.get("tds_paise") or 0) - int(split.get("disallowed_paise") or 0)
    if (received + tds) * 100 > left:
        raise EmptyPlan(f"{_inr(received + tds)} is more than the {present.fmt(left, 'paise')} still due on "
                        f"{c.get('claim_number')}.")
    cid, settled = str(c["id"]), int(c.get("settled_amount_paise") or 0)
    key = hashlib.sha256(f"ketu-settle:{cid}:{settled}:{received}:{tds}:{params.get('utr')}".encode()).hexdigest()[:40]
    body = {"amount_received_paise": received * 100, "tds_paise": tds * 100, "idempotency_key": key,
            **({"utr_number": params["utr"]} if params.get("utr") else {})}
    after = left - (received + tds) * 100
    return {"title": f"Record {_inr(received)} from {c.get('payer_name') or 'the insurer'}",
            "bullets": [f"{c.get('claim_number')}: {c.get('patient_name')}, {present.fmt(left, 'paise')} due.",
                        f"Received {_inr(received)}" + (f", TDS {_inr(tds)}" if tds else "")
                        + (f", UTR {params['utr']}" if params.get("utr") else "") + ".",
                        "Fully settled." if after == 0 else f"{present.fmt(after, 'paise')} still due after this."],
            "warnings": ["Record any disallowed amount with its reason on the Insurance page."],
            "affected": received, "snapshot": {"id": cid, "settled": settled, "left": left},
            "steps": [_step("Record the settlement", _call("POST", f"{_INS}/claims/{cid}/settlement", json=body),
                            verify=_claim_after(cid, lambda x: int(x.get("settled_amount_paise") or 0)
                                                >= settled + received * 100,
                                                lambda x: f"Claim {str(x.get('status')).replace('_', ' ')}"))]}


_CLAIM_TO = {"query": "query_raised", "in_process": "in_process", "withdraw": "withdrawn"}


async def claim_status_preview(read, params: dict) -> dict:
    action, note = params["action"], params.get("note")
    if action in ("query", "withdraw") and not note:
        raise EmptyPlan("Say the insurer's query, e.g. \"query: discharge summary missing\"." if action == "query"
                        else "Say why the claim is withdrawn, e.g. \"because the patient will pay\".")
    c = await _claim(read, params["target"])
    to = _CLAIM_TO[action]
    if c.get("status") == to and action != "query":   # a second query replaces the first one's details
        raise EmptyPlan(f"{c.get('claim_number')} is already {to.replace('_', ' ')}.")
    if action == "withdraw" and int(c.get("settled_amount_paise") or 0) + int(c.get("tds_deducted_paise") or 0) > 0:
        raise EmptyPlan(f"{c.get('claim_number')} has money recorded against it, so it can't be withdrawn.")
    cid = str(c["id"])
    return {"title": f"Mark {c.get('claim_number')} {to.replace('_', ' ')}",
            "bullets": [f"{c.get('claim_number')}: {c.get('patient_name')}, {c.get('payer_name') or 'payer'}.",
                        f"{str(c.get('status')).replace('_', ' ')} → {to.replace('_', ' ')}."]
            + ([f"{'Query' if action == 'query' else 'Reason'}: {note}."] if note else []),
            "warnings": ["A withdrawn claim can't be reopened."] if action == "withdraw" else [],
            "snapshot": {"id": cid, "status": c.get("status")},
            "steps": [_step("Update the claim", _call("POST", f"{_INS}/claims/{cid}/action",
                                                      json={"action": action, **({"note": note} if note else {})}),
                            verify=_claim_after(cid, lambda x: x.get("status") == to,
                                                lambda x: f"Now {str(x.get('status')).replace('_', ' ')}"))]}


# ── Voice-first batch 8: IPD discounts, packages, insurance pre-authorisations ──

async def _stay(read, target: str) -> dict:
    """The occupied bed of an inpatient named by bed or name (the same rules as every IPD action)."""
    return await _inpatient_bed(read, target)


async def _running_bill(read, adm: str) -> Optional[dict]:
    out = await read(_call("GET", f"{_IPD}/admissions/{adm}/running-bill"))
    return out.data if out.status == "ok" and isinstance(out.data, dict) else None


async def discount_request_preview(read, params: dict) -> dict:
    """R3, typed rupees. Within the requester's own limit it is approved at once, else it waits for an approver
    (the server decides; never the same person). The key is per stay, amount and reason."""
    rupees, reason = int(Decimal(params["amount"])), params["reason"]
    if len(reason) < 5:
        raise EmptyPlan("Say the reason in a few words, e.g. \"because of family hardship\".")
    b = await _stay(read, params["target"])
    adm = str(b["occupant"]["admission_id"])
    bill = await _running_bill(read, adm) or {}
    payable = int(bill.get("payable_paise") or 0)
    if bill and rupees * 100 > payable:
        raise EmptyPlan(f"{_inr(rupees)} is more than the {present.fmt(payable, 'paise')} the stay owes.")
    key = hashlib.sha256(f"ketu-discount:{adm}:{rupees}:{reason.lower()}".encode()).hexdigest()[:40]

    async def verify(read, data, call):
        status = data.get("status") if isinstance(data, dict) else None
        after = await _running_bill(read, adm)
        if status not in ("requested", "approved") or after is None:
            return None, "Couldn't read the discount back"
        seen = after.get("discount") if status == "approved" else after.get("pending_discount")
        return (bool(seen), "Approved" if status == "approved" else "Waiting for an approver") if seen else \
            (False, "Not on the running bill")

    return {"title": f"Discount {_inr(rupees)} for {_bed_line(b)}",
            "bullets": [f"Reason: {reason}."] + ([f"The stay owes {present.fmt(payable, 'paise')} now."] if bill else []),
            "warnings": ["Above your approval limit it waits for an IPD admin to approve."],
            "affected": rupees, "snapshot": {"admission_id": adm, "payable": payable},
            "steps": [_step("Request the discount", _call("POST", f"{_IPD}/admissions/{adm}/discounts",
                                                          json={"amount_paise": rupees * 100, "reason": reason,
                                                                "idempotency_key": key}), verify=verify)]}


async def discount_decision_preview(read, params: dict) -> dict:
    """R3, typed rupees of the request. Approve / reject by an IPD admin (the server also checks the approver's
    limit and that nobody approves their own request)."""
    decision = params["decision"]
    rows = [d for d in (await _get_or_refuse(read, f"{_IPD}/discounts/pending", need="IPD Admin") or {}).get("discounts")
            or [] if isinstance(d, dict)]
    target = params["target"]
    if target.startswith("bed:"):
        b = await _stay(read, target)
        found = [d for d in rows if str(d.get("admission_id")) == str(b["occupant"]["admission_id"])]
    else:
        found = _named(rows, target, lambda d: d.get("patient_name"))
    d = _only(found, target.replace("bed:", "bed "), "pending discount",
              lambda d: f"{d.get('patient_name')}: {present.fmt(d.get('amount_paise'), 'paise')}")
    rupees, did = int(d.get("amount_paise") or 0) // 100, str(d["id"])
    note = params.get("note")

    async def verify(read, data, call):
        status = data.get("status") if isinstance(data, dict) else None
        out = await read(_call("GET", f"{_IPD}/discounts/pending"))
        still = any(isinstance(x, dict) and str(x.get("id")) == did for x in (out.data or {}).get("discounts") or []) \
            if out.status == "ok" and isinstance(out.data, dict) else None
        if still is None:
            return None, "Couldn't read the discounts back"
        return (status == decision and not still), f"{decision.capitalize()}" if not still else "Still pending"

    return {"title": f"{'Approve' if decision == 'approved' else 'Reject'} {_inr(rupees)} discount for "
                     f"{d.get('patient_name')}",
            "bullets": [f"Requested by {d.get('requested_by_name') or 'staff'}: {d.get('reason') or ''}"] +
                       ([f"Note: {note}."] if note else []),
            "warnings": ["Approved discounts lower the final bill."] if decision == "approved" else [],
            "affected": rupees, "snapshot": {"id": did, "amount": d.get("amount_paise")},
            "steps": [_step(f"{decision.capitalize()} the discount", _call("POST", f"{_IPD}/discounts/{did}/decision",
                                                                         json={"decision": decision,
                                                                               **({"note": note} if note else {})}),
                            verify=verify)]}


async def package_apply_preview(read, params: dict) -> dict:
    b = await _stay(read, params["target"])
    adm = str(b["occupant"]["admission_id"])
    pkgs = [p for p in (await _get_or_refuse(read, f"{_IPD}/packages", {"active": True}, "IPD Billing") or {})
            .get("packages") or [] if isinstance(p, dict)]
    words = params["package"].split()
    found = [p for p in pkgs if all(w in f"{p.get('name')} {p.get('code')}".lower() for w in words)]
    pkg = _only(found, params["package"], "active package",
                lambda p: f"{p.get('name')} ({present.fmt(p.get('price_paise'), 'paise')})")
    bill = await _running_bill(read, adm) or {}
    if bill.get("package"):
        raise EmptyPlan(f"{_bed_line(b)} already has a package. Remove it on the chart first.")

    async def verify(read, data, call):
        after = await _running_bill(read, adm)
        if after is None:
            return None, "Couldn't read the running bill back"
        got = after.get("package") or {}
        ok = str(got.get("package_id") or got.get("id") or "") == str(pkg["id"]) or got.get("name") == pkg.get("name")
        return ok, f"On {pkg.get('name')}" if ok else "No package on the running bill"

    cats = ", ".join(str(c).replace("_", " ") for c in pkg.get("included_categories") or [])
    return {"title": f"Put {_bed_line(b)} on {pkg.get('name')}",
            "bullets": [f"{pkg.get('name')}: {present.fmt(pkg.get('price_paise'), 'paise')}"
                        + (f", up to {pkg['max_days']} days" if pkg.get("max_days") else "") + ".",
                        f"Covers: {cats or 'as set on the package'}."],
            "warnings": ["Charges in the covered categories move into the package price."],
            "snapshot": {"admission_id": adm, "package": str(pkg["id"])},
            "steps": [_step("Apply the package", _call("POST", f"{_IPD}/admissions/{adm}/package",
                                                       json={"package_id": str(pkg["id"])}), verify=verify)]}


async def _preauth(read, target: str, statuses: tuple) -> dict:
    rows = [p for p in (await _get_or_refuse(read, f"{_INS}/pre-auths", None, "Insurance") or {}).get("items") or []
            if isinstance(p, dict) and p.get("status") in statuses]
    if target.startswith("preauth:"):
        found = [p for p in rows if str(p.get("pre_auth_number") or "").upper().replace("-", "")
                 == target[8:].replace("-", "")]
    elif target.startswith("bed:"):
        b = await _stay(read, target)
        found = [p for p in rows if str(p.get("admission_id")) == str(b["occupant"]["admission_id"])]
    else:
        found = _named(rows, target, lambda p: p.get("patient_name"))
    return _only(found, target.split(":", 1)[-1], "pre-authorisation that can take this",
                 lambda p: f"{p.get('pre_auth_number')} ({p.get('patient_name')}, {str(p.get('status')).replace('_', ' ')})")


def _requested(p: dict) -> int:
    return int(p.get("initial_requested_paise") or 0) + int(p.get("enhanced_requested_paise") or 0)


def _preauth_after(pid: str, ok, said: str):
    async def verify(read, data, call):
        out = await read(_call("GET", f"{_INS}/pre-auths"))
        rows = (out.data or {}).get("items") if out.status == "ok" and isinstance(out.data, dict) else None
        if rows is None:
            return None, "Couldn't read the pre-authorisations back"
        row = next((r for r in rows if isinstance(r, dict) and str(r.get("id")) == pid), None)
        return (True, said) if row is not None and ok(row) else (False, "The pre-authorisation shows otherwise")
    return verify


async def preauth_approve_preview(read, params: dict) -> dict:
    """R3, typed rupees: the TOTAL the insurer's latest letter approves, with its approval reference."""
    p = await _preauth(read, params["target"], ("submitted", "query_raised", "enhancement_requested"))
    rupees, ref, pid = int(Decimal(params["amount"])), params["ref"], str(p["id"])
    before = int(p.get("total_approved_paise") or 0)
    if before and rupees * 100 < before:
        raise EmptyPlan(f"The insurer had approved {present.fmt(before, 'paise')} already. Say the new TOTAL from "
                        "their latest letter.")
    return {"title": f"Record {_inr(rupees)} approved for {p.get('patient_name')}",
            "bullets": [f"{p.get('pre_auth_number')}: {present.fmt(_requested(p), 'paise')} requested"
                        + (f", {present.fmt(before, 'paise')} approved before" if before else "") + ".",
                        f"Approved total {_inr(rupees)}, reference {ref}."],
            "warnings": ["This is the TOTAL approved, not the extra amount. Attach the approval letter on the "
                         "Insurance page."],
            "affected": rupees, "snapshot": {"id": pid, "status": p.get("status"), "approved": before},
            "steps": [_step("Record the approval", _call("PATCH", f"/admin/ipd/pre-auth/{pid}/decision",
                                                         json={"decision": "approved", "approved_amount_paise": rupees * 100,
                                                               "approval_ref": ref}),
                            verify=_preauth_after(pid, lambda r: int(r.get("total_approved_paise") or 0) == rupees * 100,
                                                  f"Approved {_inr(rupees)}"))]}


async def preauth_reply_preview(read, params: dict) -> dict:
    decision, note = params["decision"], params.get("note")
    if not note:
        raise EmptyPlan("Say what the insurer said, e.g. \"query: need the ultrasound report\" or "
                        "\"rejected because the policy lapsed\".")
    statuses = ("enhancement_requested",) if decision == "enhancement_denied" else ("submitted", "enhancement_requested",
                                                                                     "query_raised")
    p = await _preauth(read, params["target"], statuses)
    pid = str(p["id"])
    label = {"query_raised": "query raised", "rejected": "rejected", "enhancement_denied": "enhancement denied"}[decision]
    return {"title": f"Record the insurer's {label} for {p.get('patient_name')}",
            "bullets": [f"{p.get('pre_auth_number')}: {str(p.get('status')).replace('_', ' ')} → {label}.",
                        f"Insurer: {note}."],
            "warnings": ["A rejected pre-authorisation makes the stay self-pay unless a new one is approved."]
            if decision == "rejected" else [],
            "snapshot": {"id": pid, "status": p.get("status")},
            "steps": [_step("Record the reply", _call("PATCH", f"/admin/ipd/pre-auth/{pid}/decision",
                                                      json={"decision": decision, "note": note}),
                            verify=_preauth_after(pid, lambda r: r.get("status") == decision,
                                                  label.capitalize()))]}


async def preauth_action_preview(read, params: dict) -> dict:
    """Submit a draft to the insurer, or ask for more (an enhancement of the extra amount)."""
    action = params["action"]
    if action == "request_enhancement" and not params.get("amount"):
        raise EmptyPlan("Say how much more to ask for, e.g. \"request enhancement of 20,000\".")
    p = await _preauth(read, params["target"], ("draft",) if action == "submit" else ("approved",))
    pid = str(p["id"])
    body = {"action": action, **({"amount_paise": int(Decimal(params["amount"])) * 100} if action == "request_enhancement"
                                 else {})}
    to = "submitted" if action == "submit" else "enhancement_requested"
    return {"title": (f"Submit {p.get('patient_name')}'s pre-authorisation" if action == "submit"
                      else f"Ask {_inr(int(Decimal(params['amount'])))} more for {p.get('patient_name')}"),
            "bullets": [f"{p.get('pre_auth_number')}: {present.fmt(_requested(p), 'paise')} requested"
                        + (f", {present.fmt(p.get('total_approved_paise'), 'paise')} approved" if p.get("total_approved_paise")
                           else "") + "."],
            "warnings": ["Send the forms through the insurer's portal as usual; this records it here."],
            "snapshot": {"id": pid, "status": p.get("status")},
            "steps": [_step("Update the pre-authorisation", _call("POST", f"/admin/ipd/pre-auth/{pid}/action", json=body),
                            verify=_preauth_after(pid, lambda r: r.get("status") == to, to.replace("_", " ").capitalize()))]}


# ── Voice-first batch 9: package create, credit authorisation, new pre-auth, disallowances ──

async def package_create_preview(read, params: dict) -> dict:
    name, cover = params["name"], params["cover"]
    kind, value = params["price"][0], Decimal(params["price"][1])
    if kind != "amount" or not 0 < value <= 1_000_000:
        raise EmptyPlan("Say the package price in rupees, e.g. \"at 25,000\".")
    pkgs = [p for p in (await _get_or_refuse(read, f"{_IPD}/packages", None, "IPD Admin") or {}).get("packages") or []
            if isinstance(p, dict)]
    same = next((p for p in pkgs if str(p.get("name") or "").lower() == name.lower()), None)
    if same:
        raise EmptyPlan(f"{same.get('name')} is already a package at {present.fmt(same.get('price_paise'), 'paise')}.")
    code = _code_from(name, {str(p.get("code") or "").lower() for p in pkgs}, limit=40)
    rupees = int(value)
    body = {"code": code, "name": name, "price_paise": rupees * 100, "included_categories": cover,
            **({"max_days": params["days"]} if params.get("days") else {})}

    async def verify(read, data, call):
        out = await read(_call("GET", f"{_IPD}/packages"))
        rows = (out.data or {}).get("packages") if out.status == "ok" and isinstance(out.data, dict) else None
        if rows is None:
            return None, "Couldn't read the packages back"
        ok = any(isinstance(p, dict) and p.get("code") == code for p in rows)
        return ok, f"Package {code} added" if ok else "Not in the package list"

    return {"title": f"Create the {name} package",
            "bullets": [f"{name}: {_inr(rupees)}" + (f", up to {params['days']} days" if params.get("days") else "") + ".",
                        f"Covers: {', '.join(c.replace('_', ' ') for c in cover)}.", f"Code {code}."],
            "warnings": ["Charges in the covered categories are absorbed into the package price for stays put on it."],
            "snapshot": {"packages": len(pkgs)},
            "steps": [_step(f"Create {name}", _call("POST", f"{_IPD}/packages", json=body), verify=verify)]}


async def credit_preview(read, params: dict) -> dict:
    """R3, typed rupees: a corporate / payer's written credit for a stay, with its reference."""
    b = await _stay(read, params["target"])
    adm, rupees = str(b["occupant"]["admission_id"]), int(Decimal(params["amount"]))
    bill = await _running_bill(read, adm) or {}
    if bill.get("credit"):
        raise EmptyPlan(f"{_bed_line(b)} already has a credit authorisation. Revoke it on the chart first.")
    body = {"payer_name": params["payer"], "reference": params["ref"], "amount_paise": rupees * 100}

    async def verify(read, data, call):
        after = await _running_bill(read, adm)
        if after is None:
            return None, "Couldn't read the running bill back"
        return (bool(after.get("credit")), "On the running bill" if after.get("credit") else "Not on the running bill")

    return {"title": f"Authorise {_inr(rupees)} credit for {_bed_line(b)}",
            "bullets": [f"Payer: {params['payer']}, reference {params['ref']}."]
            + ([f"The stay owes {present.fmt(bill.get('payable_paise'), 'paise')} now."] if bill else []),
            "warnings": ["The patient is not asked for the covered amount at discharge; keep the payer's letter."],
            "affected": rupees, "snapshot": {"admission_id": adm},
            "steps": [_step("Authorise the credit", _call("POST", f"{_IPD}/admissions/{adm}/credit", json=body),
                            verify=verify)]}


async def preauth_create_preview(read, params: dict) -> dict:
    """The stay's patient's valid policy (the one whose insurer is named, if several); the server allows one live
    pre-authorisation per stay, so a repeat is refused there too."""
    b = await _stay(read, params["target"])
    adm = str(b["occupant"]["admission_id"])
    row = await _ipd_admission(read, adm)
    if row is None or not row.get("patient_id"):
        raise RuntimeError("read admission failed")
    pols = [p for p in (await _get_or_refuse(read, f"{_INS}/policies/{row['patient_id']}", None, "Insurance") or {})
            .get("policies") or [] if isinstance(p, dict) and p.get("is_valid_today")
            and str(p.get("family_member_id") or "") == str(row.get("family_member_id") or "")]
    if params.get("payer"):
        pols = [p for p in pols if params["payer"] in str((p.get("payer") or {}).get("name") or "").lower()] or pols
    if not pols:
        raise EmptyPlan(f"{_bed_line(b)} has no valid insurance policy on file. Add it on the Insurance page first.")
    if len(pols) > 1:
        raise EmptyPlan(f"{len(pols)} valid policies: " + "; ".join(
            f"{(p.get('payer') or {}).get('name')} {p.get('policy_number')}" for p in pols[:4])
            + ". Say the insurer too, e.g. \"under Star Health\".")
    pol, rupees = pols[0], int(Decimal(params["amount"]))
    body = {"policy_id": str(pol["id"]), "requested_paise": rupees * 100, "provisional_diagnosis": params["indication"],
            **({"expected_stay_days": params["days"]} if params.get("days") else {})}

    async def verify(read, data, call):
        pid = data.get("id") if isinstance(data, dict) else None
        out = await read(_call("GET", f"{_INS}/pre-auths"))
        rows = (out.data or {}).get("items") if out.status == "ok" and isinstance(out.data, dict) else None
        if rows is None or not pid:
            return None, "Couldn't read the pre-authorisations back"
        hit = next((r for r in rows if isinstance(r, dict) and str(r.get("id")) == str(pid)), None)
        return (hit is not None), (f"Submitted {hit.get('pre_auth_number')}" if hit else "Not in the pipeline")

    return {"title": f"Request {_inr(rupees)} pre-authorisation for {_bed_line(b)}",
            "bullets": [f"{(pol.get('payer') or {}).get('name')}, policy {pol.get('policy_number')}.",
                        f"For: {params['indication']}." + (f" Expected stay {params['days']} days." if params.get("days") else "")],
            "warnings": ["Send the insurer's forms through their portal as usual; this records it as submitted."],
            "snapshot": {"admission_id": adm, "policy": str(pol["id"])},
            "steps": [_step("Request the pre-authorisation", _call("POST", f"/admin/ipd/admissions/{adm}/pre-auth",
                                                                   json=body), verify=verify)]}


_DISALLOW_LABEL = {"non_payable_item": "non-payable items", "room_rent_proportionate": "room rent (proportionate)",
                   "copay": "co-pay", "policy_limit": "policy / sub-limit", "excess_over_tariff": "over the tariff",
                   "documentation_incomplete": "documents incomplete", "not_medically_necessary": "not medically necessary",
                   "repudiated": "repudiated", "other": "other"}


async def disallowance_preview(read, params: dict) -> dict:
    """R3, typed rupees: an insurer's deduction on a claim, reason-coded; recorded through the settlement call
    with nothing received, so the claim's own arithmetic decides what stays outstanding."""
    c = await _claim(read, params["target"])
    rupees, code, note = int(Decimal(params["amount"])), params["code"], params.get("note")
    if code == "other" and not note:
        raise EmptyPlan("Say why the insurer deducted it, e.g. \"because documents were missing\".")
    split = c.get("split") or {}
    left = int(split.get("payer_payable_paise") or 0) - int(split.get("payer_received_paise") or 0) \
        - int(split.get("tds_paise") or 0) - int(split.get("disallowed_paise") or 0)
    if rupees * 100 > left:
        raise EmptyPlan(f"{_inr(rupees)} is more than the {present.fmt(left, 'paise')} still due on {c.get('claim_number')}.")
    cid = str(c["id"])
    before = int(split.get("disallowed_paise") or 0)
    key = hashlib.sha256(f"ketu-disallow:{cid}:{before}:{rupees}:{code}".encode()).hexdigest()[:40]
    body = {"amount_received_paise": 0, "tds_paise": 0, "idempotency_key": key,
            "disallowances": [{"amount_paise": rupees * 100, "reason_code": code, **({"note": note} if note else {})}]}
    return {"title": f"Record {_inr(rupees)} disallowed on {c.get('claim_number')}",
            "bullets": [f"{c.get('patient_name')}, {c.get('payer_name') or 'payer'}: {present.fmt(left, 'paise')} due.",
                        f"Reason: {_DISALLOW_LABEL[code]}" + (f" ({note})" if note else "") + "."],
            "warnings": ["Decide later on the Insurance page whether it is written off or collected from the patient."],
            "affected": rupees, "snapshot": {"id": cid, "disallowed": before},
            "steps": [_step("Record the disallowance", _call("POST", f"{_INS}/claims/{cid}/settlement", json=body),
                            verify=_claim_after(cid, lambda x: int((x.get("split") or {}).get("disallowed_paise")
                                                                   or x.get("disallowed_amount_paise") or 0)
                                                >= before + rupees * 100, lambda x: "Recorded"))]}
