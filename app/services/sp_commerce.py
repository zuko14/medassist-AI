"""Specialty OS — S3 packages & instalments and S5 estimates (migration 152; GAP-ANALYSIS §2, VERIFICATION D15).

A package sale IS an ordinary OPD invoice (one line priced from the OPD service catalogue, so HSN / GST / receipts /
credit notes / GSTR-1 are unchanged for every clinic); each session used is a row in sp_package_consumptions — never a
zero-value invoice line. Instalments are a due schedule over one ordinary invoice; whether an instalment is paid is
derived from the invoice's paid amount (receipts stay the only money path). An estimate is accepted or declined on
WhatsApp with buttons — acceptance records the choice and the message id, and NO money moves. Zero-LLM.
"""

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services.sp import _db_error, _patient, actor_name, now_iso, rows
from app.utils.helpers import today_ist
from app.utils.validators import mask_phone

logger = logging.getLogger(__name__)
EST_PREFIX = "spest:"
LIVE_INVOICE = ("issued", "partially_paid", "paid")


# ── packages ─────────────────────────────────────────────────────────────────

async def _catalog_entry(clinic_id: str, code: str) -> dict:
    from app.services.masters import effective_catalog
    entry = next((c for c in await effective_catalog(clinic_id) if c.get("code") == code and c.get("active", True)), None)
    if not entry:
        raise HTTPException(status_code=422, detail="Pick an active service from the OPD service catalogue.")
    return entry


async def create_package(clinic_id: str, body: dict) -> dict:
    entry = await _catalog_entry(clinic_id, body["service_code"])
    try:
        # unscoped: insert_scoped_by_payload
        row = rows(await sb(supabase.table("sp_packages").insert({**body, "clinic_id": clinic_id})))[0]
    except Exception as e:
        if "sp_packages_code_key" in str(e):
            raise HTTPException(status_code=409, detail="A package with this code already exists.") from None
        _db_error(e)
    return {**row, "price_paise": int(entry.get("price_paise") or 0), "tax_rate_bps": entry.get("tax_rate_bps", 0)}


async def list_packages(clinic_id: str, active_only: bool = False) -> list:
    from app.services.masters import effective_catalog
    q = scoped_query("sp_packages", clinic_id)
    if active_only:
        q = q.eq("is_active", True)
    cat = {c.get("code"): c for c in await effective_catalog(clinic_id)}
    return [{**p, "price_paise": int((cat.get(p["service_code"]) or {}).get("price_paise") or 0),
             "tax_rate_bps": (cat.get(p["service_code"]) or {}).get("tax_rate_bps", 0)}
            for p in rows(await sb(q.order("name")))]


async def update_package(clinic_id: str, package_id: str, patch: dict) -> dict:
    if "service_code" in patch:
        await _catalog_entry(clinic_id, patch["service_code"])
    out = rows(await sb(supabase.table("sp_packages").update({**patch, "updated_at": now_iso()})
                        .eq("clinic_id", clinic_id).eq("id", package_id)))
    if not out:
        raise HTTPException(status_code=404, detail="Package not found.")
    return out[0]


async def sell_package(clinic_id: str, body: dict, user) -> dict:
    """Creates the ordinary draft invoice (one catalogue line, priced server-side) and the sale; staff issue and
    collect the invoice exactly as any other bill."""
    from app.services import opd_billing
    pkg = rows(await sb(scoped_query("sp_packages", clinic_id).eq("id", body["package_id"]).eq("is_active", True).limit(1)))
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found.")
    pkg = pkg[0]
    entry = await _catalog_entry(clinic_id, pkg["service_code"])
    inv = await opd_billing.create_invoice(
        clinic_id, body["patient_id"], [{"item_type": "procedure", "catalog_code": pkg["service_code"],
                                         "description": f"{pkg['name']} ({pkg['sessions_total']} sessions)",
                                         "quantity": 1, "unit_price_paise": int(entry.get("price_paise") or 0)}],
        family_member_id=body.get("family_member_id"), notes=f"Package {pkg['code']}", actor=user)
    try:
        # unscoped: insert_scoped_by_payload
        sale = rows(await sb(supabase.table("sp_package_sales").insert({
            "clinic_id": clinic_id, "package_id": pkg["id"], "patient_id": body["patient_id"],
            "family_member_id": body.get("family_member_id"), "invoice_id": inv["id"],
            "sessions_total": pkg["sessions_total"], "price_paise": int(inv.get("subtotal_paise") or entry["price_paise"]),
            "expires_on": (today_ist() + timedelta(days=int(pkg["validity_days"]))).isoformat(),
            "sold_by_name": actor_name(user)})))[0]
    except Exception as e:
        _db_error(e)
    return {**sale, "invoice": {"id": inv["id"], "status": inv.get("status")}}


async def _sale(clinic_id: str, sale_id: str) -> dict:
    s = rows(await sb(scoped_query("sp_package_sales", clinic_id).eq("id", sale_id).limit(1)))
    if not s:
        raise HTTPException(status_code=404, detail="Package sale not found.")
    return s[0]


async def _used(clinic_id: str, sale_id: str) -> list:
    return rows(await sb(scoped_query("sp_package_consumptions", clinic_id).eq("sale_id", sale_id)
                         .is_("reversed_at", "null").order("session_no")))


async def sale_status(clinic_id: str, sale: dict) -> dict:
    used = await _used(clinic_id, sale["id"])
    inv = rows(await sb(scoped_query("opd_invoices", clinic_id, "id, status, total_paise, paid_paise")
                        .eq("id", sale["invoice_id"]).limit(1)))
    status = sale["status"]
    if status == "active" and date.fromisoformat(str(sale["expires_on"])[:10]) < today_ist():
        status = "expired"
    return {**sale, "status": status, "sessions_used": len(used), "sessions_left": sale["sessions_total"] - len(used),
            "consumptions": used, "invoice": inv[0] if inv else None}


async def list_sales(clinic_id: str, patient_id: str) -> list:
    return [await sale_status(clinic_id, s) for s in rows(await sb(scoped_query("sp_package_sales", clinic_id)
                                                                   .eq("patient_id", patient_id)
                                                                   .order("created_at", desc=True).limit(50)))]


async def consume(clinic_id: str, sale_id: str, body: dict, user) -> dict:
    """One session used. The sale's invoice must be issued (a draft bill is not a sale yet); the session number is
    the next free one, and the DB refuses a session beyond the count, an expired or inactive package, and a double tap."""
    sale = await _sale(clinic_id, sale_id)
    inv = rows(await sb(scoped_query("opd_invoices", clinic_id, "status").eq("id", sale["invoice_id"]).limit(1)))
    if not inv or inv[0]["status"] not in LIVE_INVOICE:
        raise HTTPException(status_code=409, detail="Issue the package bill first.")
    for _ in range(2):
        used = await _used(clinic_id, sale_id)
        taken = {u["session_no"] for u in used}
        nxt = next(i for i in range(1, sale["sessions_total"] + 2) if i not in taken)   # a reversed number is reused
        try:
            # unscoped: insert_scoped_by_payload
            row = rows(await sb(supabase.table("sp_package_consumptions").insert({
                "clinic_id": clinic_id, "sale_id": sale_id, "session_no": nxt, "appointment_id": body.get("appointment_id"),
                "encounter_id": body.get("encounter_id"), "consumed_by_name": actor_name(user)})))[0]
            break
        except Exception as e:
            if "uq_sp_package_consumption_live" in str(e):
                continue                              # a parallel tap took this number: take the next one
            _db_error(e)
    else:
        raise HTTPException(status_code=409, detail="That session was just recorded by someone else — try again.")
    left = sale["sessions_total"] - len(await _used(clinic_id, sale_id))
    if left <= 0:
        await sb(supabase.table("sp_package_sales").update({"status": "completed", "updated_at": now_iso()})
                 .eq("clinic_id", clinic_id).eq("id", sale_id).eq("status", "active"))
    return {**row, "sessions_left": left}


async def reverse_consumption(clinic_id: str, consumption_id: str, reason: str) -> dict:
    c = rows(await sb(scoped_query("sp_package_consumptions", clinic_id).eq("id", consumption_id)
                      .is_("reversed_at", "null").limit(1)))
    if not c:
        raise HTTPException(status_code=404, detail="Session not found or already reversed.")
    out = rows(await sb(supabase.table("sp_package_consumptions").update({"reversed_at": now_iso(),
                                                                           "reverse_reason": reason.strip()})
                        .eq("clinic_id", clinic_id).eq("id", consumption_id).is_("reversed_at", "null")))
    await sb(supabase.table("sp_package_sales").update({"status": "active", "updated_at": now_iso()})
             .eq("clinic_id", clinic_id).eq("id", c[0]["sale_id"]).eq("status", "completed"))
    return out[0]


async def send_balance(clinic: dict, sale_id: str) -> dict:
    """"3 of 6 sessions left — book next" on WhatsApp (inside the 24-hour window; no clinical content)."""
    from app.services.whatsapp import whatsapp_service
    st = await sale_status(clinic["id"], await _sale(clinic["id"], sale_id))
    p = await _patient(clinic["id"], st["patient_id"])
    if not p.get("opted_in") or not p.get("phone"):
        raise HTTPException(status_code=409, detail="This patient has not opted in to WhatsApp.")
    pkg = rows(await sb(scoped_query("sp_packages", clinic["id"], "name").eq("id", st["package_id"]).limit(1)))
    text = (f"{clinic.get('name', '')}: {st['sessions_left']} of {st['sessions_total']} sessions left in your "
            f"{(pkg[0]['name'] if pkg else 'package')} (valid till {st['expires_on']}). Reply to book your next session.")
    if not await whatsapp_service.send_text(clinic, p["phone"], text, _source="sp_package"):
        raise HTTPException(status_code=409, detail="The patient's WhatsApp window is closed: tell them at the counter.")
    return {"sent": True, "to": mask_phone(p["phone"]), "sessions_left": st["sessions_left"]}


# ── instalments ──────────────────────────────────────────────────────────────

async def _invoice(clinic_id: str, invoice_id: str) -> dict:
    inv = rows(await sb(scoped_query("opd_invoices", clinic_id, "id, status, total_paise, paid_paise, patient_id")
                        .eq("id", invoice_id).limit(1)))
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    return inv[0]


async def set_instalments(clinic_id: str, invoice_id: str, plan: list, user) -> list:
    inv = await _invoice(clinic_id, invoice_id)
    if inv["status"] == "void":
        raise HTTPException(status_code=409, detail="This invoice is void.")
    total = sum(int(x["amount_paise"]) for x in plan)
    if total != int(inv["total_paise"] or 0):
        raise HTTPException(status_code=422, detail="The instalments must add up to the invoice total.")
    dues = [str(x["due_on"]) for x in plan]
    if dues != sorted(dues) or len(set(dues)) != len(dues):
        raise HTTPException(status_code=422, detail="Due dates must be distinct and in order.")
    await sb(supabase.table("sp_instalments").delete().eq("clinic_id", clinic_id).eq("invoice_id", invoice_id))
    # unscoped: insert_scoped_by_payload
    await sb(supabase.table("sp_instalments").insert([
        {"clinic_id": clinic_id, "invoice_id": invoice_id, "seq": i, "due_on": str(x["due_on"]),
         "amount_paise": int(x["amount_paise"]), "created_by_name": actor_name(user)} for i, x in enumerate(plan, start=1)]))
    return await instalments(clinic_id, invoice_id)


async def instalments(clinic_id: str, invoice_id: str) -> list:
    """Each instalment is paid once the invoice's cumulative paid amount covers it (receipts are the money path)."""
    inv = await _invoice(clinic_id, invoice_id)
    paid, cum, today, out = int(inv.get("paid_paise") or 0), 0, today_ist(), []
    for r in rows(await sb(scoped_query("sp_instalments", clinic_id).eq("invoice_id", invoice_id).order("seq"))):
        cum += int(r["amount_paise"])
        status = "paid" if paid >= cum else ("overdue" if date.fromisoformat(str(r["due_on"])[:10]) < today else "due")
        out.append({**r, "status": status, "outstanding_paise": max(0, min(int(r["amount_paise"]), cum - paid))})
    return out


async def instalments_due(clinic_id: str, within_days: int = 7) -> list:
    """Every unpaid instalment due within the window (or overdue) — the desk's call / payment-link list."""
    limit = (today_ist() + timedelta(days=within_days)).isoformat()
    inv_ids = {r["invoice_id"] for r in await fetch_all(lambda: scoped_query("sp_instalments", clinic_id, "invoice_id")
                                                        .lte("due_on", limit), label="sp:inst")}
    out = []
    for iid in inv_ids:
        out += [x for x in await instalments(clinic_id, iid) if x["status"] != "paid" and str(x["due_on"]) <= limit]
    return sorted(out, key=lambda x: str(x["due_on"]))


# ── S5 estimates ─────────────────────────────────────────────────────────────

def _clean_options(options: list) -> list:
    out, codes = [], set()
    for o in options:
        code = o["code"]
        if code in codes:
            raise HTTPException(status_code=422, detail="Option codes must be unique.")
        codes.add(code)
        lines = [{"label": ln["label"].strip(), "amount_paise": int(ln["amount_paise"])} for ln in o["lines"]]
        out.append({"code": code, "label": o["label"].strip(), "lines": lines,
                    "total_paise": sum(ln["amount_paise"] for ln in lines)})
    return out


async def create_estimate(clinic_id: str, body: dict, user) -> dict:
    await _patient(clinic_id, body["patient_id"])
    if body["valid_until"] < today_ist():
        raise HTTPException(status_code=422, detail="The validity date has passed.")
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("sp_estimates").insert({
            "clinic_id": clinic_id, "patient_id": body["patient_id"], "family_member_id": body.get("family_member_id"),
            "specialty": body["specialty"], "title": body["title"].strip(), "options": _clean_options(body["options"]),
            "valid_until": body["valid_until"].isoformat(), "dental_plan_id": body.get("dental_plan_id"),
            "created_by_name": actor_name(user)})))[0]
    except HTTPException:
        raise
    except Exception as e:
        _db_error(e)


async def _estimate(clinic_id: str, estimate_id: str) -> dict:
    e = rows(await sb(scoped_query("sp_estimates", clinic_id).eq("id", estimate_id).limit(1)))
    if not e:
        raise HTTPException(status_code=404, detail="Estimate not found.")
    return e[0]


async def list_estimates(clinic_id: str, patient_id: Optional[str] = None, status: Optional[str] = None) -> list:
    q = scoped_query("sp_estimates", clinic_id)
    if patient_id:
        q = q.eq("patient_id", patient_id)
    if status:
        q = q.eq("status", status)
    return rows(await sb(q.order("created_at", desc=True).limit(200)))


def _rs(paise: int) -> str:
    return f"₹{paise / 100:,.0f}"


async def send_estimate(clinic: dict, estimate_id: str) -> dict:
    """Interactive buttons inside the 24-hour window: "Accept <option>" for up to two options + "Not now"."""
    from app.services.whatsapp import whatsapp_service
    cid = clinic["id"]
    e = await _estimate(cid, estimate_id)
    if e["status"] not in ("draft", "sent"):
        raise HTTPException(status_code=409, detail="This estimate has already been answered.")
    if date.fromisoformat(str(e["valid_until"])[:10]) < today_ist():
        raise HTTPException(status_code=409, detail="This estimate has expired.")
    if len(e["options"]) > 2:
        raise HTTPException(status_code=422, detail="WhatsApp buttons fit two options: share a three- or four-option "
                                                    "estimate at the counter.")
    p = await _patient(cid, e["patient_id"])
    if not p.get("opted_in") or not p.get("phone"):
        raise HTTPException(status_code=409, detail="This patient has not opted in to WhatsApp.")
    body = "\n".join([f"{clinic.get('name', '')} — {e['title']}", *[f"• {o['label']}: {_rs(o['total_paise'])}"
                                                                   for o in e["options"]],
                      f"Valid till {e['valid_until']}. This is an estimate; nothing is charged when you reply."])
    buttons = [{"id": f"{EST_PREFIX}{e['id']}:{o['code']}", "title": f"Accept {o['label']}"[:20]} for o in e["options"]]
    buttons.append({"id": f"{EST_PREFIX}{e['id']}:no", "title": "Not now"})
    if not await whatsapp_service.send_interactive_buttons(clinic, p["phone"], body[:1024], buttons, _source="sp_estimate"):
        raise HTTPException(status_code=409, detail="The patient's WhatsApp window is closed: share the estimate at the "
                                                    "counter or after they message the clinic.")
    out = rows(await sb(supabase.table("sp_estimates").update({"status": "sent", "sent_at": now_iso(),
                                                                "updated_at": now_iso()})
                        .eq("clinic_id", cid).eq("id", estimate_id)))
    return {**out[0], "to": mask_phone(p["phone"])}


async def respond(clinic_id: str, estimate_id: str, option: Optional[str], message_id: Optional[str],
                  phone: Optional[str] = None) -> Optional[dict]:
    """Record the patient's answer once (a replayed webhook is ignored). option None = declined. No money moves."""
    e = rows(await sb(scoped_query("sp_estimates", clinic_id).eq("id", estimate_id).limit(1)))
    if not e:
        return None
    e = e[0]
    if phone:                                         # only the estimate's own patient can answer it
        p = rows(await sb(scoped_query("patients", clinic_id, "phone").eq("id", e["patient_id"]).limit(1)))
        digits = lambda x: "".join(ch for ch in str(x or "") if ch.isdigit())[-10:]   # noqa: E731
        if not p or digits(p[0]["phone"]) != digits(phone):
            return None
    if e["status"] != "sent" or date.fromisoformat(str(e["valid_until"])[:10]) < today_ist():
        return None
    if option is not None and option not in {o["code"] for o in e["options"]}:
        return None
    patch = {"status": "accepted" if option else "declined", "accepted_option": option, "responded_at": now_iso(),
             "response_message_id": message_id, "updated_at": now_iso()}
    try:
        out = rows(await sb(supabase.table("sp_estimates").update(patch).eq("clinic_id", clinic_id).eq("id", estimate_id)
                            .eq("status", "sent")))
    except Exception as ex:                            # duplicate webhook delivery of the same reply
        if "uq_sp_estimates_response" in str(ex):
            return None
        raise
    return out[0] if out else None


async def staff_respond(clinic_id: str, estimate_id: str, option: Optional[str]) -> dict:
    """The patient answered at the counter or on the phone."""
    e = await _estimate(clinic_id, estimate_id)
    if e["status"] == "draft":
        await sb(supabase.table("sp_estimates").update({"status": "sent", "sent_at": now_iso()})
                 .eq("clinic_id", clinic_id).eq("id", estimate_id).eq("status", "draft"))
    out = await respond(clinic_id, estimate_id, option, None)
    if not out:
        raise HTTPException(status_code=409, detail="This estimate cannot be answered (expired, answered, or unknown option).")
    return out


async def convert_estimate(clinic_id: str, estimate_id: str, package_id: str, user) -> dict:
    """Accepted estimate → package sale (an ordinary invoice). Still no money moves until the bill is paid."""
    e = await _estimate(clinic_id, estimate_id)
    if e["status"] != "accepted":
        raise HTTPException(status_code=409, detail="Only an accepted estimate can be converted.")
    sale = await sell_package(clinic_id, {"package_id": package_id, "patient_id": e["patient_id"],
                                          "family_member_id": e.get("family_member_id")}, user)
    await sb(supabase.table("sp_estimates").update({"status": "converted", "converted_sale_id": sale["id"],
                                                    "updated_at": now_iso()})
             .eq("clinic_id", clinic_id).eq("id", estimate_id).eq("status", "accepted"))
    return sale


async def convert_estimate_to_ipd(clinic: dict, estimate_id: str, admission_id: Optional[str], user) -> dict:
    """SP-EYE-05: an accepted surgery estimate (e.g. cataract + chosen IOL) becomes an IPD package of migration 116 at the
    accepted option's total, and — when the admission is given — is applied to that stay through the existing IPD path
    (one package charge; billing, receipts and GST unchanged). No money moves here either."""
    from app.services import ipd_finance
    from app.services.tenant import ipd_access
    cid = clinic["id"]
    if ipd_access(clinic) != "ACTIVE":
        raise HTTPException(status_code=409, detail="IPD OS is not active for this clinic.")
    e = await _estimate(cid, estimate_id)
    if e["status"] != "accepted":
        raise HTTPException(status_code=409, detail="Only an accepted estimate can be converted.")
    opt = next((o for o in e["options"] if o["code"] == e["accepted_option"]), None)
    if not opt or opt["total_paise"] < 1:
        raise HTTPException(status_code=422, detail="The accepted option has no amount.")
    if admission_id:
        adm = rows(await sb(scoped_query("ipd_admissions", cid, "id, patient_id, status").eq("id", admission_id).limit(1)))
        if not adm or adm[0]["patient_id"] != e["patient_id"]:
            raise HTTPException(status_code=404, detail="Admission of this patient not found.")
    pkg = await ipd_finance.create_package(cid, {
        "code": f"EST-{str(estimate_id).replace('-', '')[:10].upper()}", "name": f"{e['title']} — {opt['label']}"[:200],
        "description": "; ".join(f"{ln['label']} ₹{ln['amount_paise'] / 100:,.0f}" for ln in opt["lines"])[:1000],
        "price_paise": int(opt["total_paise"]), "included_categories": ["procedure", "consumable", "pharmacy"],
        "max_days": 3, "is_active": True}, user)
    await sb(supabase.table("sp_estimates").update({"status": "converted", "converted_ipd_package_id": pkg["id"],
                                                    "updated_at": now_iso()})
             .eq("clinic_id", cid).eq("id", estimate_id).eq("status", "accepted"))
    applied = await ipd_finance.apply_package(cid, admission_id, pkg["id"], user) if admission_id else None
    return {"ipd_package": pkg, "applied": applied}
