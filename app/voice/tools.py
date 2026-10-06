"""Typed, tenant-scoped tools: the ONLY way the voice receptionist touches
hospital state. Implements dialog.Tools.

Rules every method follows:
  * clinic_id, branch_id and the caller's phone come from CallContext (the
    authenticated call), never from anything the caller said or an LLM produced;
  * reads return None when the underlying system failed (=> degraded speech,
    never a guess); an empty list means "really nothing";
  * writes reuse the existing WhatsApp-proven services (payment_service,
    book_appointment, ConversationManager._cancel_with_refund,
    lab_report_service.resend_report) and are VERIFIED by reading the row back;
    the dict they return carries {"status", "verified"} from that read-back;
  * every call is written to voice_call_events (kind="tool") with its duration,
    status, and for writes a proof record (expected vs read-back);
  * mode == "test": writes are simulated and labelled simulated=True in the trace.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from app.config import settings
from app.database import sb, supabase

from . import store
from .dates import slot_period, today_ist
from .phone import mask

logger = logging.getLogger(__name__)

TOOLS_VERSION = "tools-2026.10.06"
_ACTIVE = ("confirmed", "pending_payment", "pending_review")
_MAX_DOCTORS = 6


@dataclass
class CallContext:
    call_id: str
    call_ref: str
    clinic: dict
    branch_id: Optional[str]
    caller_phone: str
    mode: str = "live"                     # live | shadow | test
    reception_number: Optional[str] = None
    lang: str = "te-IN"
    correlation_id: Optional[str] = None
    summary: dict = field(default_factory=dict)   # filled by session for handoff packets

    @property
    def clinic_id(self) -> str:
        return self.clinic["id"]


def _speakable(text: str) -> str:
    """WhatsApp-formatted text -> something TTS can read: no markdown, emoji or URLs."""
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"[*_~`]", "", t)
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" .")
    if len(t) > 320:
        cut = t[:320]
        t = cut[: cut.rfind(".") + 1] or cut
    return t


class KriyaTools:
    def __init__(self, ctx: CallContext):
        self.ctx = ctx
        self._profile = None
        self._profile_loaded = False

    # ---- plumbing ----

    async def _trace(self, name: str, status: str, started: float, data: dict) -> None:
        await store.add_event(self.ctx.clinic_id, self.ctx.call_id, "tool", name, status=status, data=data,
                              duration_ms=int((time.monotonic() - started) * 1000),
                              correlation_id=self.ctx.correlation_id)

    @property
    def _simulated(self) -> bool:
        return self.ctx.mode == "test"

    async def _window_open(self) -> bool:
        from app.services.whatsapp import whatsapp_service
        try:
            return await whatsapp_service._can_send_freeform(self.ctx.clinic, self.ctx.caller_phone)
        except Exception:
            return False

    # ---- reads ----

    async def find_doctors(self, department, doctor_ids):
        from app.database import get_doctors
        t0 = time.monotonic()
        try:
            rows = await get_doctors(self.ctx.clinic_id, department=None if doctor_ids else department,
                                     branch_id=self.ctx.branch_id)
        except Exception as e:
            await self._trace("DOCTOR_SEARCH", "fail", t0, {"error": str(e)[:200]})
            return None
        if doctor_ids:
            rows = [r for r in rows if r.get("id") in set(doctor_ids)]
        docs = [{"id": r["id"], "name": r.get("name") or "", "department": r.get("department") or "",
                 "fee_paise": int(r["consultation_fee"]) * 100 if r.get("consultation_fee") else None,
                 "branch_session": r.get("branch_session")}
                for r in rows if r.get("is_active", True)][:_MAX_DOCTORS]
        await self._trace("DOCTOR_SEARCH", "ok", t0, {"department": department, "found": len(docs)})
        return docs

    async def find_slots(self, doctors, date, period, clock):
        from app.database import get_available_slots
        t0 = time.monotonic()
        opts, errors = [], 0
        for d in doctors:
            slots, reason = await get_available_slots(self.ctx.clinic_id, d["name"], date,
                                                      branch_id=self.ctx.branch_id,
                                                      branch_session=d.get("branch_session"))
            if reason == "error":
                errors += 1
                continue
            for s in slots:
                hhmm = str(s)[:5]
                if clock and hhmm != clock:
                    continue
                if period and not clock and slot_period(hhmm) != period:
                    continue
                opts.append({"doctor_id": d["id"], "doctor_name": d["name"], "department": d["department"],
                             "date": date, "time": hhmm, "fee_paise": d.get("fee_paise")})
                break  # earliest matching slot per doctor: options span doctors
        if doctors and errors == len(doctors):
            await self._trace("SLOT_LOOKUP", "fail", t0, {"date": date, "error": "all lookups failed"})
            return None
        opts.sort(key=lambda o: o["time"])
        await self._trace("SLOT_LOOKUP", "ok", t0, {"date": date, "period": period, "clock": clock,
                                                    "options": len(opts)})
        return opts

    async def next_available(self, doctors, after_date, period):
        from datetime import date as _date
        t0 = time.monotonic()
        start = _date.fromisoformat(after_date)
        for i in range(1, 15):
            day = (start + timedelta(days=i)).isoformat()
            opts = await self.find_slots(doctors, day, period, None)
            if opts is None:
                return None
            if opts:
                await self._trace("NEXT_AVAILABLE", "ok", t0, {"date": day})
                return opts
        await self._trace("NEXT_AVAILABLE", "ok", t0, {"date": None})
        return []

    async def caller_profile(self):
        from app.database import get_patient_by_phone
        if not self._profile_loaded:
            self._profile = await get_patient_by_phone(self.ctx.clinic_id, self.ctx.caller_phone)
            self._profile_loaded = True
        return {"id": self._profile.get("id"), "name": self._profile.get("name")} if self._profile else None

    async def upcoming_appointments(self):
        t0 = time.monotonic()
        try:
            res = await sb(supabase.table("appointments")
                           .select("id, booking_ref, status, payment_id, doctor_id, doctor_name, booking_type, "
                                   "lab_test_name, appointment_date, appointment_time")
                           .eq("clinic_id", self.ctx.clinic_id).eq("patient_phone", self.ctx.caller_phone)
                           .in_("status", list(_ACTIVE))
                           .gte("appointment_date", today_ist().isoformat())
                           .order("appointment_date").limit(10))
        except Exception as e:
            await self._trace("APPOINTMENT_LOOKUP", "fail", t0, {"error": str(e)[:200]})
            return None
        rows = [{"id": r["id"], "booking_ref": r.get("booking_ref"), "status": r["status"],
                 "paid": bool(r.get("payment_id")), "doctor_id": r.get("doctor_id"),
                 "doctor_name": r.get("doctor_name"), "booking_type": r.get("booking_type"),
                 "lab_test_name": r.get("lab_test_name"), "date": str(r["appointment_date"]),
                 "time": str(r["appointment_time"])[:5] if r.get("appointment_time") else None}
                for r in res.data or []]
        await self._trace("APPOINTMENT_LOOKUP", "ok", t0, {"found": len(rows)})
        return rows

    async def reports(self):
        t0 = time.monotonic()
        since = (today_ist() - timedelta(days=60)).isoformat()
        try:
            res = await sb(supabase.table("lab_reports")
                           .select("id, report_name, status, file_path, uploaded_at")
                           .eq("clinic_id", self.ctx.clinic_id).eq("patient_phone", self.ctx.caller_phone)
                           .gte("uploaded_at", since).order("uploaded_at", desc=True).limit(5))
        except Exception as e:
            await self._trace("REPORT_LOOKUP", "fail", t0, {"error": str(e)[:200]})
            return None
        rows = [{"id": r["id"], "test_name": r.get("report_name") or "",
                 # needs_review / dismissed are held by the lab: never offered to a caller
                 "status": "ready" if r.get("file_path") and r.get("status") not in ("needs_review", "dismissed")
                 else "processing"}
                for r in res.data or []]
        await self._trace("REPORT_LOOKUP", "ok", t0, {"found": len(rows)})
        return rows

    async def doctor_fees(self, department, doctor_ids):
        return await self.find_doctors(department, doctor_ids)

    async def lab_tests(self, query):
        from app.database import get_lab_tests
        from app.services.conversation import ConversationManager
        from app.services.hybrid_search import strip_query_filler
        t0 = time.monotonic()
        q = query or ""
        for e in await store.load_lexicon(self.ctx.clinic_id):
            if e.get("kind") == "test_alias" and e.get("phrase", "").lower() in q.lower():
                q = e["canonical"]
                break
        q = strip_query_filler(q) or q
        try:
            tests = await get_lab_tests(self.ctx.clinic_id, branch_id=self.ctx.branch_id)
            hits = ConversationManager._match_lab_tests(tests, q) if q.strip() else []
        except Exception as e:
            await self._trace("LAB_TEST_SEARCH", "fail", t0, {"error": str(e)[:200]})
            return None
        out = [{"id": t["id"], "name": t.get("name") or "", "price_paise": t.get("price_paise")} for t in hits[:3]]
        await self._trace("LAB_TEST_SEARCH", "ok", t0, {"query": q[:60], "found": len(out)})
        return out

    async def info(self, topic):
        from app.services.faq_engine import answer
        t0 = time.monotonic()
        lang = self.ctx.lang.split("-")[0]
        try:
            text = await answer(self.ctx.clinic, topic, lang if lang in ("te", "hi", "en") else "en")
        except Exception as e:
            await self._trace("HOSPITAL_INFO", "fail", t0, {"topic": topic, "error": str(e)[:200]})
            return None
        if text and await self._window_open():
            from app.services.whatsapp import whatsapp_service
            await whatsapp_service.send_text(self.ctx.clinic, self.ctx.caller_phone, text, _source="voice_info")
        await self._trace("HOSPITAL_INFO", "ok" if text else "skipped", t0, {"topic": topic})
        return _speakable(text) if text else None

    async def queue_status(self):
        from app.database import get_patient_queue_status
        q = await get_patient_queue_status(self.ctx.clinic_id, self.ctx.caller_phone, today_ist().isoformat())
        if q and q.get("checked_in"):
            return {"token": q["token_number"], "ahead": q["patients_ahead"]}
        return None

    # ---- writes (verified) ----

    async def _read_back(self, booking_id: str) -> Optional[dict]:
        res = await sb(supabase.table("appointments")
                       .select("id, status, booking_ref, doctor_id, lab_test_id, appointment_date, "
                               "appointment_time, amount_paise, patient_phone")
                       .eq("clinic_id", self.ctx.clinic_id).eq("id", booking_id).limit(1))
        return (res.data or [None])[0]

    async def _proof(self, name: str, t0: float, expected: dict, row: Optional[dict], ok: bool) -> None:
        actual = {k: (str(row.get(k))[:8] if k == "appointment_time" and row and row.get(k) else (row or {}).get(k))
                  for k in expected}
        await self._trace(name, "ok" if ok else "fail", t0, {"expected": expected, "read_back": actual,
                                                            "verified": ok, "system": "kriya"})

    async def _send_payment_link(self, booking: dict, link: str, what: str, date: str, time_: Optional[str],
                                 patient_name: str) -> bool:
        from app.services.whatsapp import whatsapp_service
        hold = settings.booking_hold_minutes
        rupees = f"{(booking.get('amount_paise') or 0) / 100:.0f}"
        hospital = self.ctx.clinic.get("name") or ""
        when = f"{date} {time_ or ''}".strip()
        if await self._window_open():
            msg = (f"Payment link for {what}, {when}: Rs {rupees}. The slot is held for {hold} minutes; "
                   f"it is confirmed automatically once paid.\n{link}")
            return bool(await whatsapp_service.send_text(self.ctx.clinic, self.ctx.caller_phone, msg,
                                                         _source="voice_payment_link"))
        params = [patient_name or "Patient", what, date, time_ or "-", str(hold), rupees, link, hospital]
        return bool(await whatsapp_service.send_template(
            self.ctx.clinic, self.ctx.caller_phone, settings.voice_payment_link_template_name,
            language="en", components=[{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}],
            _source="voice_payment_link"))

    async def _send_confirmation(self, row: dict, what: str, dept: str, patient_name: str) -> bool:
        from app.services.whatsapp import whatsapp_service
        from app.templates.whatsapp_templates import TEMPLATES
        date, time_ = str(row["appointment_date"]), (str(row["appointment_time"])[:5] if row.get("appointment_time") else "-")
        if await self._window_open():
            msg = (f"Your booking is confirmed: {what}, {date} {time_ if time_ != '-' else ''}. "
                   f"Booking number {row.get('booking_ref')}.")
            return bool(await whatsapp_service.send_text(self.ctx.clinic, self.ctx.caller_phone, msg,
                                                         _source="booking_confirmation"))
        tpl = TEMPLATES["appointment_confirmation"]
        return bool(await whatsapp_service.send_template(
            self.ctx.clinic, self.ctx.caller_phone, tpl["name"], language=tpl["language"],
            components=tpl["components_builder"](what, dept, date, time_, self.ctx.clinic.get("name") or ""),
            _source="booking_confirmation"))

    async def _create(self, *, kind: str, patient_name: str, department: str, doctor_id=None, doctor_name=None,
                      date: str, time_: Optional[str], lab_test: Optional[dict] = None) -> dict:
        """Shared consultation / lab-test write. Mirrors conversation.py's two paths:
        payment_mode full|partial -> create_booking_with_payment (pending_payment + link);
        none -> book_appointment (confirmed)."""
        from app.database import book_appointment
        from app.services.payment import payment_service, resolve_payment_mode
        t0 = time.monotonic()
        name = "LAB_TEST_CREATE" if kind == "lab_test" else "APPOINTMENT_CREATE"
        bare = re.sub(r"^(dr\.?\s*)", "", doctor_name or "", flags=re.I)
        what = (lab_test or {}).get("name") if kind == "lab_test" else "Dr. " + bare
        profile = await self.caller_profile()
        if self._simulated:
            await self._trace(name, "skipped", t0, {"simulated": True, "date": date, "time": time_})
            return {"status": "confirmed", "verified": True, "simulated": True, "booking_ref": "TEST-0000",
                    "whatsapp_sent": False}
        mode, deposit = resolve_payment_mode(self.ctx.clinic)
        branch_name = None
        try:
            if mode in ("full", "partial"):
                res = await payment_service.create_booking_with_payment(
                    clinic_id=self.ctx.clinic_id, patient_phone=self.ctx.caller_phone, patient_name=patient_name,
                    department=department if kind != "lab_test" else "Lab Test",
                    doctor_name=doctor_name if kind != "lab_test" else None,
                    appointment_date=date, appointment_time=time_ if kind != "lab_test" else None,
                    patient_id=(profile or {}).get("id"), clinic=self.ctx.clinic, branch_id=self.ctx.branch_id,
                    branch_name=branch_name, deposit_percent=deposit,
                    booking_type="lab_test" if kind == "lab_test" else "consultation",
                    lab_test_id=(lab_test or {}).get("id"), lab_test_name=(lab_test or {}).get("name"),
                    doctor_id=doctor_id if kind != "lab_test" else None)
                if not res.get("success"):
                    reason = res.get("reason")
                    await self._trace(name, "fail", t0, {"reason": reason})
                    return {"status": "slot_taken" if reason == "slot_taken" else
                            "unavailable" if reason == "doctor_unavailable" else "failed", "verified": False}
                row = await self._read_back(res["booking_id"])
                ok = bool(row) and row["status"] == "pending_payment" and str(row["appointment_date"]) == date \
                    and (kind == "lab_test" or str(row.get("doctor_id")) == str(doctor_id))
                await self._proof(name.replace("CREATE", "VERIFY"), t0,
                                  {"status": "pending_payment", "appointment_date": date, "doctor_id": doctor_id}, row, ok)
                link_sent = False
                if ok:
                    t1 = time.monotonic()
                    link_sent = await self._send_payment_link(row, res["payment_link"], what, date, time_, patient_name)
                    await self._trace("PAYMENT_LINK_SEND", "ok" if link_sent else "fail", t1,
                                      {"channel": "whatsapp", "to": mask(self.ctx.caller_phone)})
                return {"status": "payment_pending", "verified": ok, "booking_id": res["booking_id"],
                        "booking_ref": res.get("booking_ref"), "amount_paise": res.get("amount_paise"),
                        "link_sent": link_sent, "hold_minutes": settings.booking_hold_minutes}

            data = {"patient_id": (profile or {}).get("id"), "patient_phone": self.ctx.caller_phone,
                    "patient_name": patient_name, "appointment_date": date, "status": "confirmed"}
            if kind == "lab_test":
                data.update({"department": "Lab Test", "doctor_name": None, "appointment_time": None,
                             "booking_type": "lab_test", "lab_test_id": lab_test["id"],
                             "lab_test_name": lab_test["name"], "amount_paise": lab_test.get("price_paise")})
            else:
                data.update({"department": department, "doctor_name": doctor_name, "doctor_id": doctor_id,
                             "appointment_time": time_})
            if self.ctx.branch_id:
                data["branch_id"] = self.ctx.branch_id
            res = await book_appointment(self.ctx.clinic_id, data)
            if not res.get("success"):
                reason = res.get("reason")
                await self._trace(name, "fail", t0, {"reason": reason})
                return {"status": "slot_taken" if reason == "slot_taken" else
                        "unavailable" if reason == "doctor_unavailable" else "failed", "verified": False}
            row = await self._read_back(res["appointment"]["id"])
            ok = bool(row) and row["status"] == "confirmed" and str(row["appointment_date"]) == date \
                and (kind == "lab_test" or str(row.get("doctor_id")) == str(doctor_id))
            await self._proof(name.replace("CREATE", "VERIFY"), t0,
                              {"status": "confirmed", "appointment_date": date, "doctor_id": doctor_id}, row, ok)
            wa = False
            if ok:
                t1 = time.monotonic()
                wa = await self._send_confirmation(row, what, department, patient_name)
                await self._trace("WHATSAPP_CONFIRMATION", "ok" if wa else "fail", t1, {})
            return {"status": "confirmed", "verified": ok, "booking_id": row["id"] if row else None,
                    "booking_ref": (row or {}).get("booking_ref"), "whatsapp_sent": wa}
        except Exception as e:
            logger.error(f"VOICE_BOOKING_ERROR call={self.ctx.call_ref}: {type(e).__name__}: {e}")
            await self._trace(name, "fail", t0, {"error": type(e).__name__})
            return {"status": "failed", "verified": False}

    async def book(self, option, patient_name, relation):
        return await self._create(kind="consultation", patient_name=patient_name,
                                  department=option.get("department") or "", doctor_id=option["doctor_id"],
                                  doctor_name=option["doctor_name"], date=option["date"], time_=option["time"])

    async def book_lab(self, test, date, patient_name):
        from datetime import date as _date
        from app.database import get_lab_collection_window
        try:
            window = await get_lab_collection_window(self.ctx.clinic, self.ctx.branch_id)
            days = [d.strip()[:3].title() for d in str(window.get("days") or "").split(",") if d.strip()]
            if days and _date.fromisoformat(date).strftime("%a") not in days:
                return {"status": "unavailable", "verified": False}
        except Exception as e:
            logger.warning(f"VOICE_LAB_WINDOW_READ_FAILED: {e}")
        return await self._create(kind="lab_test", patient_name=patient_name, department="Lab Test",
                                  date=date, time_=None, lab_test=test)

    async def cancel(self, appointment):
        from app.services.conversation import conversation_manager
        t0 = time.monotonic()
        appt_id = appointment["id"]
        # Ownership re-check: only a booking on the CALLER's own number, in this clinic.
        res = await sb(supabase.table("appointments").select("id, status, patient_phone")
                       .eq("clinic_id", self.ctx.clinic_id).eq("id", appt_id).limit(1))
        row = (res.data or [None])[0]
        if not row or row.get("patient_phone") != self.ctx.caller_phone:
            await self._trace("APPOINTMENT_CANCEL", "fail", t0, {"reason": "not_callers_booking"})
            return {"status": "failed", "verified": False}
        if self._simulated:
            await self._trace("APPOINTMENT_CANCEL", "skipped", t0, {"simulated": True})
            return {"status": "cancelled", "verified": True, "simulated": True}
        try:
            cancelled, refund = await conversation_manager._cancel_with_refund(
                self.ctx.clinic, self.ctx.caller_phone, appt_id)
        except Exception as e:
            await self._trace("APPOINTMENT_CANCEL", "fail", t0, {"error": type(e).__name__})
            return {"status": "failed", "verified": False}
        after = await self._read_back(appt_id)
        ok = bool(cancelled) and bool(after) and after["status"] in ("cancelled", "refunded")
        if refund is None:
            status = "cancelled"
        elif refund.get("success"):
            status = "refunded"
        elif "refund_window_closed" in str(refund.get("reason") or ""):
            status = "cancelled_late"
        else:
            status = "refund_failed"
        await self._proof("APPOINTMENT_CANCEL_VERIFY", t0, {"status": "cancelled|refunded"}, after, ok)
        return {"status": status, "verified": ok}

    async def resend_report(self, report):
        from app.services.lab_reports import LabReportService
        t0 = time.monotonic()
        if self._simulated:
            await self._trace("REPORT_SEND", "skipped", t0, {"simulated": True})
            return {"status": "sent", "simulated": True}
        try:
            # No new_phone: a report only ever goes to the number it was issued to.
            row = await LabReportService().resend_report(report["id"], clinic_id=self.ctx.clinic_id)
            ok = (row or {}).get("status") == "sent"
        except Exception as e:
            await self._trace("REPORT_SEND", "fail", t0, {"error": str(e)[:200]})
            return {"status": "failed"}
        await self._trace("REPORT_SEND", "ok" if ok else "fail", t0, {"report_id": report["id"]})
        return {"status": "sent" if ok else "failed"}

    async def create_callback(self, reason):
        t0 = time.monotonic()
        if not self._simulated:
            try:
                # unscoped: insert_scoped_by_payload
                await sb(supabase.table("admin_notifications").insert({
                    "clinic_id": self.ctx.clinic_id, "admin_id": None,
                    "title": f"Call back {self.ctx.caller_phone} (AI receptionist)"[:255],
                    "message": f"Call {self.ctx.call_ref}: {reason}. Open AI Receptionist > Calls for the transcript.",
                    "is_read": False}))
            except Exception as e:
                await self._trace("CALLBACK_CREATE", "fail", t0, {"error": str(e)[:200]})
                return {"ok": False}
        await store.update_call(self.ctx.clinic_id, self.ctx.call_id, {"outcome": "callback_requested"})
        await self._trace("CALLBACK_CREATE", "ok" if not self._simulated else "skipped", t0, {"reason": reason})
        return {"ok": True}

    async def handoff(self, reason):
        t0 = time.monotonic()
        cfg = store.voice_config(self.ctx.clinic)
        can_transfer = bool(self.ctx.reception_number) and store.within_hours(cfg["reception_hours"])
        if reason == "emergency" and self.ctx.reception_number:
            can_transfer = True     # an emergency is transferred whatever the hours
        mode = "transfer" if can_transfer else "callback"
        packet = {"call_ref": self.ctx.call_ref, "phone": self.ctx.caller_phone, "language": self.ctx.lang,
                  "reason": reason, **self.ctx.summary}
        await store.update_call(self.ctx.clinic_id, self.ctx.call_id,
                                {"handoff_reason": reason, "handoff_packet": packet})
        if mode == "callback":
            await self.create_callback(f"handoff: {reason}")
        await store.add_event(self.ctx.clinic_id, self.ctx.call_id, "handoff", "HANDOFF_TRIGGERED", status="ok",
                              data={"mode": mode, "reason": reason},
                              duration_ms=int((time.monotonic() - t0) * 1000))
        return {"mode": mode}
