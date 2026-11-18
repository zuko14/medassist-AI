"""Kriya Pharmacy OS — chronic refills (migration 142; RX-57, RX-58, RX-59; WORKFLOWS.md W10).

Zero-LLM. A plan exists only when the pharmacist ticked "Patient wants refill reminders" (X16), the item is chronic and
not H1 / X / narcotic / psychotropic, and the prescribed dosage + frequency fall inside the CLOSED map below — anything
else is skipped, never guessed. The daily reminder sends the clinic's own approved utility template (no dose text, no
advice; AGENTS.md §1.6: the clinic's credentials or nothing) only to patients who still accept engagement (global STOP).
The two button payloads are the only inbound text this module ever claims; typed text keeps flowing through the FSM.
"""

import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.database import sb, scoped_query, supabase
from app.services.rx import _rows, get_rx_settings
from app.services.rx_pos import mask_phone
from app.utils.helpers import today_ist

logger = logging.getLogger(__name__)


def _upd(table: str, clinic_id: str, patch: dict):
    """A clinic-scoped UPDATE (scoped_query only builds SELECTs)."""
    return supabase.table(table).update(patch).eq("clinic_id", clinic_id)

FREQ = {"OD": 1, "BD": 2, "TDS": 3, "QID": 4, "HS": 1}       # closed map: nothing else ever makes a plan
DOSE = re.compile(r"^\d+(\.5)?$")
NO_REFILL_SCHEDULES = ("H1", "X", "narcotic", "psychotropic")
OPEN_PLAN = ("scheduled", "sent", "requested")
PREFIX_ON, PREFIX_OFF = "rx_refill:", "rx_refill_off:"
PAYLOAD = re.compile(r"^(rx_refill|rx_refill_off):([0-9a-fA-F-]{36})$")
TEMPLATE = "rx_refill_due_v1"
MAX_FAILURES = 3
REPLY_REQUESTED = "Request received — the pharmacy will confirm."
REPLY_OFF = "Refill reminders are now off. The pharmacy can turn them on again at your next visit."
REPLY_IGNORED = "Thank you for your message."
REPLY_REJECTED = ("Your refill request could not be filled on the old prescription. "
                  "Please consult your doctor for a new prescription.")


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(..., min_length=3, max_length=300)
    notify_patient: bool = True


class PackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_id: str


RequestStatus = Literal["new", "verified", "packed", "completed", "rejected"]
PlanStatus = Literal["scheduled", "sent", "requested", "fulfilled", "expired", "opted_out", "cancelled"]


def units_per_day(dosage: Optional[str], frequency: Optional[str]) -> Optional[Decimal]:
    """Units a day from the closed map only; None (no plan) for anything it does not cover."""
    freq = FREQ.get((frequency or "").strip().upper())
    dose = (dosage or "").strip()
    if freq is None or not DOSE.fullmatch(dose) or Decimal(dose) <= 0:
        return None
    return Decimal(dose) * freq


def plan_dates(sale_day: date, units: int, per_day: Decimal, lead_days: int) -> tuple[date, date]:
    covered = sale_day + timedelta(days=math.floor(Decimal(units) / per_day))
    return covered, max(covered - timedelta(days=lead_days), sale_day)


async def _by_ids(table: str, clinic_id: str, ids, fields: str = "*") -> dict:
    ids = sorted({str(i) for i in ids if i})
    return {str(r["id"]): r for r in (await _rows(scoped_query(table, clinic_id, fields).in_("id", ids)) if ids else [])}


# ── plan creation at the counter (W10 step 1) ───────────────────────────────

async def create_plans(clinic_id: str, sale_id: str, opt_in_by: Optional[str], settings: dict) -> list[dict]:
    """After a completed sale the pharmacist marked "wants refill reminders": one plan per eligible line. A newer
    sale of the same item for the same patient supersedes the older open plan (→ cancelled)."""
    if not settings.get("refill_enabled") or not opt_in_by:
        return []
    sale = (await _rows(scoped_query("rx_sales", clinic_id).eq("id", sale_id).limit(1)) or [None])[0]
    if not sale or sale["status"] != "completed" or not sale.get("patient_id"):
        return []                                  # a walk-in without a patient record cannot be reminded
    lines = await _rows(scoped_query("rx_sale_items", clinic_id).eq("sale_id", sale_id))
    items = await _by_ids("rx_items", clinic_id, [ln["item_id"] for ln in lines], "id, is_chronic, schedule")
    rx_lines = await _by_ids("opd_prescription_items", clinic_id, [ln.get("opd_prescription_item_id") for ln in lines],
                             "id, dosage, frequency")
    now, today = datetime.now(timezone.utc).isoformat(), today_ist()
    out = []
    for ln in lines:
        item = items.get(str(ln["item_id"])) or {}
        rx_line = rx_lines.get(str(ln.get("opd_prescription_item_id")))
        if not item.get("is_chronic") or item.get("schedule") in NO_REFILL_SCHEDULES or not rx_line:
            continue
        per_day = units_per_day(rx_line.get("dosage"), rx_line.get("frequency"))
        if per_day is None:
            continue
        covered, remind = plan_dates(today, int(ln["qty_units"]), per_day, int(settings.get("refill_lead_days", 5)))
        await sb(_upd("rx_refill_plans", clinic_id, {"status": "cancelled", "updated_at": now})
                 .eq("patient_id", sale["patient_id"]).eq("item_id", ln["item_id"]).in_("status", list(OPEN_PLAN))
                 .neq("sale_item_id", ln["id"]))
        if await _rows(scoped_query("rx_refill_plans", clinic_id, "id").eq("sale_item_id", ln["id"]).limit(1)):
            continue                               # replay of the same completion
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("rx_refill_plans").insert({
            "clinic_id": clinic_id, "patient_id": sale["patient_id"], "family_member_id": sale.get("family_member_id"),
            "item_id": ln["item_id"], "sale_item_id": ln["id"], "opd_prescription_item_id": rx_line["id"],
            "units_per_day": str(per_day), "covered_until": covered.isoformat(), "remind_on": remind.isoformat(),
            "opt_in_by": opt_in_by, "opt_in_at": now}))
        out.extend(res.data or [])
    return out


# ── daily reminders (W10 step 2) ────────────────────────────────────────────

async def _refill_clinics() -> list[dict]:
    from app.services.tenant import rx_access
    # unscoped: platform_sweep
    res = await sb(supabase.table("clinics").select("*").eq("rx_state", "ACTIVE").eq("is_active", True))
    return [c for c in (res.data or []) if c.get("status") != "DELETED" and rx_access(c) == "ACTIVE"
            and get_rx_settings(c).get("refill_enabled")]


async def send_reminder(clinic: dict, plan: dict, phone: str, brand: str, sale_day: str) -> tuple[bool, Optional[str]]:
    from app.services.whatsapp import whatsapp_service
    body = [{"type": "body", "parameters": [{"type": "text", "text": str(x)[:120]} for x in (
        clinic.get("name") or "Pharmacy", brand, sale_day, plan["covered_until"])]}]
    buttons = [{"type": "button", "sub_type": "quick_reply", "index": str(i),
                "parameters": [{"type": "payload", "payload": f"{prefix}{plan['id']}"}]}
               for i, prefix in enumerate((PREFIX_ON, PREFIX_OFF))]
    cap: dict = {}
    ok = await whatsapp_service.send_template(clinic, phone, TEMPLATE, components=body + buttons,
                                              _source="rx_refill_due", _capture=cap)
    return bool(ok), cap.get("meta_message_id")


async def run_reminders(today: Optional[date] = None) -> dict:
    """09:30 IST, under distributed_job_lock (scheduler.py). ACTIVE clinics with refill_enabled and their own
    WhatsApp credentials only; a patient who sent STOP is never reminded; three failed sends expire the plan."""
    from app.services.consent import consent_service
    from app.services.subscription import automated_outbound_allowed
    from app.services.whatsapp import whatsapp_service

    today = today or today_ist()
    stats = {"sent": 0, "skipped": 0, "failed": 0}
    for clinic in await _refill_clinics():
        cid = clinic["id"]
        if not automated_outbound_allowed(clinic):
            continue
        try:
            whatsapp_service._get_credentials(clinic)     # the clinic's own number, or nothing (no platform fallback)
        except ValueError:
            continue
        try:
            plans = await _rows(scoped_query("rx_refill_plans", cid).eq("status", "scheduled")
                                .lte("remind_on", today.isoformat()).order("remind_on").limit(500))
            pats = await _by_ids("patients", cid, [p["patient_id"] for p in plans], "id, phone")
            items = await _by_ids("rx_items", cid, [p["item_id"] for p in plans], "id, brand_name")
            recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            for plan in plans:
                phone = (pats.get(str(plan["patient_id"])) or {}).get("phone")
                if (not phone or (plan.get("last_sent_at") and str(plan["last_sent_at"]) > recent)
                        or not await consent_service.accepts_engagement(cid, phone)):
                    stats["skipped"] += 1
                    continue
                now = datetime.now(timezone.utc).isoformat()
                # compare-and-set: one sender only
                claim = await sb(_upd("rx_refill_plans", cid, {"status": "sent", "last_sent_at": now, "updated_at": now})
                                 .eq("id", plan["id"]).eq("status", "scheduled"))
                if not claim.data:
                    continue
                try:
                    ok, msg_id = await send_reminder(clinic, plan, phone, (items.get(str(plan["item_id"])) or {})
                                                     .get("brand_name") or "medicine", str(plan["created_at"])[:10])
                except Exception as e:                    # noqa: BLE001 - counted as a failed send below
                    logger.error(f"Refill reminder send error clinic={cid} phone={mask_phone(phone)}: {type(e).__name__}")
                    ok, msg_id = False, None
                if ok:
                    stats["sent"] += 1
                    await sb(_upd("rx_refill_plans", cid,
                             {"last_message_id": msg_id, "send_failures": 0}).eq("id", plan["id"]))
                    continue
                stats["failed"] += 1
                fails = int(plan.get("send_failures") or 0) + 1
                await sb(_upd("rx_refill_plans", cid, {
                    "send_failures": min(fails, MAX_FAILURES), "last_sent_at": plan.get("last_sent_at"),
                    "status": "expired" if fails >= MAX_FAILURES else "scheduled"}).eq("id", plan["id"]))
                logger.warning(f"Refill reminder failed clinic={cid} phone={mask_phone(phone)} failures={fails}")
        except Exception as e:                            # noqa: BLE001 - one clinic never stops the sweep
            logger.error(f"Refill reminders failed for clinic={cid}: {type(e).__name__}: {e}")
    logger.info(f"Refill reminders: {stats}")
    return stats


# ── the patient tapped a reminder button (W10 step 3) ───────────────────────

async def handle_button(manager, clinic: dict, phone: str, payload: str) -> bool:
    """True when the payload is one of ours (handled, whatever the outcome); False for anything else, which then
    flows on untouched. The plan must belong to THIS clinic and THIS sender's phone (loophole X17)."""
    m = PAYLOAD.fullmatch(payload or "")
    if not m:
        return False
    kind, plan_id = m.group(1), m.group(2)
    cid = clinic["id"]
    try:
        plan = (await _rows(scoped_query("rx_refill_plans", cid).eq("id", plan_id).limit(1)) or [None])[0]
        owner = (await _rows(scoped_query("patients", cid, "id, phone").eq("id", plan["patient_id"]).limit(1))
                 or [None])[0] if plan else None
        if not owner or owner.get("phone") != phone:
            logger.warning(f"Refill payload rejected clinic={cid} phone={mask_phone(phone)}")
            await manager.whatsapp.send_text(clinic, phone, REPLY_IGNORED)
            return True
        now = datetime.now(timezone.utc).isoformat()
        if kind == "rx_refill_off":
            await sb(_upd("rx_refill_plans", cid, {"status": "opted_out", "updated_at": now})
                     .eq("patient_id", plan["patient_id"]).in_("status", ["scheduled", "sent"]))
            await manager.whatsapp.send_text(clinic, phone, REPLY_OFF)
            return True
        if plan["status"] in ("scheduled", "sent"):
            try:
                # unscoped: insert_scoped_by_payload
                await sb(supabase.table("rx_refill_requests").insert({
                    "clinic_id": cid, "plan_id": plan["id"], "patient_id": plan["patient_id"], "channel": "whatsapp"}))
            except Exception as e:                        # noqa: BLE001 - uq_rx_refill_requests_open: already queued
                if "uq_rx_refill_requests_open" not in str(e):
                    raise
            await sb(_upd("rx_refill_plans", cid, {"status": "requested", "updated_at": now})
                     .eq("id", plan["id"]).in_("status", ["scheduled", "sent"]))
        if plan["status"] in ("scheduled", "sent", "requested"):
            await manager.whatsapp.send_text(clinic, phone, REPLY_REQUESTED)
        else:
            await manager.whatsapp.send_text(clinic, phone, REPLY_IGNORED)
    except Exception as e:                                # noqa: BLE001 - never surface to the patient
        logger.error(f"Refill button failed clinic={cid}: {type(e).__name__}: {e}")
    return True


# ── pharmacist queue (W10 step 4) ───────────────────────────────────────────

async def _enrich(clinic_id: str, rows: list[dict], requests: bool) -> list[dict]:
    """Rows + their item and patient (name, MRN, masked phone). Requests also carry their plan."""
    plans = (await _by_ids("rx_refill_plans", clinic_id, [r["plan_id"] for r in rows]) if requests
             else {str(r["id"]): r for r in rows})
    items = await _by_ids("rx_items", clinic_id, [p["item_id"] for p in plans.values()],
                          "id, item_code, brand_name, schedule")
    pats = await _by_ids("patients", clinic_id, [r["patient_id"] for r in rows], "id, name, phone, mrn")
    out = []
    for r in rows:
        plan = plans.get(str(r["plan_id"] if requests else r["id"])) or {}
        pat = pats.get(str(r["patient_id"])) or {}
        out.append({**r, **({"plan": plan} if requests else {}), "item": items.get(str(plan.get("item_id"))),
                    "patient": {"id": pat.get("id"), "name": pat.get("name"), "mrn": pat.get("mrn"),
                                "phone_masked": mask_phone(pat.get("phone"))}})
    return out


async def list_requests(clinic_id: str, status: Optional[str], limit: int) -> list[dict]:
    q = scoped_query("rx_refill_requests", clinic_id)
    q = q.eq("status", status) if status else q.in_("status", ["new", "verified", "packed"])
    return await _enrich(clinic_id, await _rows(q.order("created_at").limit(limit)), True)


async def list_plans(clinic_id: str, patient_id: Optional[str], status: Optional[str], limit: int) -> list[dict]:
    q = scoped_query("rx_refill_plans", clinic_id)
    if patient_id:
        q = q.eq("patient_id", patient_id)
    q = q.eq("status", status) if status else q.in_("status", list(OPEN_PLAN))
    return await _enrich(clinic_id, await _rows(q.order("remind_on").limit(limit)), False)


async def get_request(clinic_id: str, request_id: str) -> dict:
    rows = await _rows(scoped_query("rx_refill_requests", clinic_id).eq("id", request_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Refill request not found.")
    return rows[0]


async def get_plan(clinic_id: str, plan_id: str) -> dict:
    rows = await _rows(scoped_query("rx_refill_plans", clinic_id).eq("id", plan_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Refill plan not found.")
    return rows[0]


def _conflict(code: str, message: str):
    raise HTTPException(status_code=409, detail={"code": code, "message": message})


async def _move(clinic_id: str, req: dict, frm: tuple, patch: dict) -> dict:
    """Compare-and-set on status: a double click or a second pharmacist never applies a step twice."""
    res = await sb(_upd("rx_refill_requests", clinic_id,
                   {**patch, "updated_at": datetime.now(timezone.utc).isoformat()})
                   .eq("id", req["id"]).in_("status", list(frm)))
    if not res.data:
        _conflict("rx_refill_request_state", "This request was already moved on. Reload the queue.")
    return res.data[0]


async def prescription_of(clinic_id: str, plan: dict) -> Optional[dict]:
    if not plan.get("opd_prescription_item_id"):
        return None
    line = (await _rows(scoped_query("opd_prescription_items", clinic_id, "id, prescription_id")
                        .eq("id", plan["opd_prescription_item_id"]).limit(1)) or [None])[0]
    if not line:
        return None
    return (await _rows(scoped_query("opd_prescriptions", clinic_id, "id, status, signed_at")
                        .eq("id", line["prescription_id"]).limit(1)) or [None])[0]


async def verify(clinic_id: str, req: dict, settings: dict, actor_id: Optional[str]) -> dict:
    """The original signed Rx must still be within rx_validity_days; otherwise the pharmacist rejects."""
    if req["status"] != "new":
        _conflict("rx_refill_request_state", "Only a new request can be verified.")
    rx = await prescription_of(clinic_id, await get_plan(clinic_id, req["plan_id"]))
    if not rx or rx.get("status") != "signed" or not rx.get("signed_at"):
        _conflict("rx_refill_rx_invalid", "The original prescription is no longer valid. Reject and ask the patient "
                                          "to see the doctor.")
    signed = datetime.fromisoformat(str(rx["signed_at"]).replace("Z", "+00:00"))
    if (datetime.now(timezone.utc) - signed).days > int(settings.get("rx_validity_days", 30)):
        _conflict("rx_refill_rx_expired", "The original prescription is older than its validity. Reject and ask the "
                                          "patient to see the doctor.")
    return await _move(clinic_id, req, ("new",), {"status": "verified", "verified_by": actor_id,
                                                   "verified_at": datetime.now(timezone.utc).isoformat()})


async def pack(clinic_id: str, req: dict, store: dict, actor: tuple, settings: dict) -> dict:
    """A DRAFT counter sale of the same item and quantity, FEFO batch, against the original prescription. Nothing is
    dispensed here: the pharmacist completes it at the counter (W2 step 5)."""
    from app.services import rx_pos
    if req["status"] != "verified":
        _conflict("rx_refill_request_state", "Verify the request before packing it.")
    plan = await get_plan(clinic_id, req["plan_id"])
    rx = await prescription_of(clinic_id, plan)
    if not rx:
        _conflict("rx_refill_rx_invalid", "The original prescription is no longer available.")
    sold = (await _rows(scoped_query("rx_sale_items", clinic_id, "qty_units").eq("id", plan["sale_item_id"]).limit(1))
            or [{}])[0]
    sale = await rx_pos.create_sale(clinic_id, store, rx_pos.SaleIn(
        store_id=str(store["id"]), sale_kind="opd_rx", opd_prescription_id=str(rx["id"])), actor, settings)
    sale_row = await rx_pos.get_sale_row(clinic_id, sale["sale_id"])
    await rx_pos.replace_lines(clinic_id, sale_row, [rx_pos.LineIn(
        item_id=str(plan["item_id"]), qty_units=int(sold.get("qty_units") or 1),
        opd_prescription_item_id=plan.get("opd_prescription_item_id"))], settings)
    return await _move(clinic_id, req, ("verified",), {"status": "packed", "sale_id": sale["sale_id"],
                                                        "packed_by": actor[0],
                                                        "packed_at": datetime.now(timezone.utc).isoformat()})


async def reject(clinic_id: str, clinic: dict, req: dict, body: RejectIn) -> dict:
    out = await _move(clinic_id, req, ("new", "verified"), {"status": "rejected", "rejected_reason": body.reason})
    now = datetime.now(timezone.utc).isoformat()
    await sb(_upd("rx_refill_plans", clinic_id, {"status": "cancelled", "updated_at": now})
             .eq("id", req["plan_id"]).eq("status", "requested"))
    if body.notify_patient:          # fixed text only; the patient wrote to us moments ago (24-hour session)
        from app.services.whatsapp import whatsapp_service
        phone = ((await _rows(scoped_query("patients", clinic_id, "phone").eq("id", req["patient_id"]).limit(1))
                  or [{}])[0]).get("phone")
        try:
            if phone:
                await whatsapp_service.send_text(clinic, phone, REPLY_REJECTED)
        except Exception as e:                            # noqa: BLE001 - the rejection stands without the message
            logger.warning(f"Refill reject notice failed clinic={clinic_id} phone={mask_phone(phone)}: {type(e).__name__}")
    return out


async def cancel_plan(clinic_id: str, plan: dict) -> dict:
    res = await sb(_upd("rx_refill_plans", clinic_id,
                   {"status": "cancelled", "updated_at": datetime.now(timezone.utc).isoformat()})
                   .eq("id", plan["id"]).in_("status", list(OPEN_PLAN)))
    if not res.data:
        _conflict("rx_refill_plan_closed", "This plan is already closed.")
    return res.data[0]


async def mark_fulfilled(clinic_id: str, sale_id: str) -> None:
    """A packed refill's draft was completed at the counter: request completed, plan fulfilled."""
    reqs = await _rows(scoped_query("rx_refill_requests", clinic_id, "id, plan_id").eq("sale_id", sale_id)
                       .eq("status", "packed"))
    now = datetime.now(timezone.utc).isoformat()
    for r in reqs:
        await sb(_upd("rx_refill_requests", clinic_id, {"status": "completed", "updated_at": now})
                 .eq("id", r["id"]).eq("status", "packed"))
        await sb(_upd("rx_refill_plans", clinic_id, {"status": "fulfilled", "updated_at": now})
                 .eq("id", r["plan_id"]).eq("status", "requested"))


async def open_count(clinic_id: str) -> int:
    return len(await _rows(scoped_query("rx_refill_requests", clinic_id, "id").in_("status", ["new", "verified"]).limit(1000)))
