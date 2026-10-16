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
        return (True, said) if ok(row) else (False, "The Doctors list shows other hours")
    return verify


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
