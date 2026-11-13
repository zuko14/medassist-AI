"""Kriya Diagnostic OS — billing glue (DX-22, ADR-DX-04).

The patient share of a DX visit is billed on the OPD ledger: dx_bill_visit writes
the draft lines (the only writer, AUDIT A10); issue, receipts, refunds, void and
payment links stay the unchanged opd_billing / payment services. The invoice has
no appointment, so each linked WhatsApp / voice booking's online payment (full or
deposit) is applied once through opd_billing._apply_prepaid (AUDIT A9).
Zero-LLM (tests/test_dx_no_llm.py).
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase
from app.services import dx, opd_billing

OPEN = ("issued", "partially_paid")


async def _invoices(clinic_id: str, accession_id: str) -> list[dict]:
    res = await sb(scoped_query("opd_invoices", clinic_id).eq("accession_id", accession_id).order("created_at"))
    return list(res.data or [])


def _balance(inv: dict) -> int:
    return max(0, (inv.get("total_paise") or 0) - (inv.get("credited_paise") or 0) - (inv.get("paid_paise") or 0))


async def apply_prepaid(clinic_id: str, accession_id: str, invoice_id: str) -> int:
    """Each linked booking's online payment, once (idempotent on the gateway payment id)."""
    res = await sb(scoped_query("dx_accession_bookings", clinic_id).eq("accession_id", accession_id)
                   .is_("prepaid_applied_at", "null"))
    applied = 0
    for b in res.data or []:
        await opd_billing._apply_prepaid(clinic_id, invoice_id, b["appointment_id"])
        await sb(supabase.table("dx_accession_bookings").update(
            {"prepaid_applied_at": datetime.now(timezone.utc).isoformat()})
            .eq("clinic_id", clinic_id).eq("id", b["id"]).is_("prepaid_applied_at", "null"))
        applied += 1
    return applied


async def bill_visit(clinic_id: str, accession_id: str, user, issue: bool) -> dict:
    """Refresh the visit's draft bill (supplementary once a bill is issued); optionally issue it."""
    aid, name = dx._actor(user)
    out = await dx.rpc("dx_bill_visit", {"p_clinic_id": clinic_id, "p_accession_id": accession_id,
                                         "p_actor_id": aid, "p_actor_name": name})
    invoice_id = out.get("invoice_id")
    if issue:
        if invoice_id:
            await opd_billing.issue_invoice(clinic_id, invoice_id, user)
        else:   # nothing new to bill: a retry still finishes prepayments on the issued bill
            issued = [i for i in await _invoices(clinic_id, accession_id) if i["status"] not in ("draft", "void")]
            invoice_id = issued[-1]["id"] if issued else None
        if invoice_id:
            out["prepaid_applied"] = await apply_prepaid(clinic_id, accession_id, invoice_id)
    out["invoices"] = await _invoices(clinic_id, accession_id)
    return out


async def open_invoice(clinic_id: str, accession_id: str, invoice_id: Optional[str] = None) -> dict:
    """The visit's bill to take money on: the one named, else the oldest with a balance."""
    rows = await _invoices(clinic_id, accession_id)
    if invoice_id:
        rows = [i for i in rows if str(i["id"]) == str(invoice_id)]
        if not rows:
            raise HTTPException(status_code=404, detail="This bill does not belong to the visit.")
    rows = [i for i in rows if i["status"] in OPEN and _balance(i) > 0]
    if not rows:
        raise HTTPException(status_code=409, detail="This visit has no issued bill with a balance.")
    return rows[0]


async def take_receipt(clinic_id: str, accession_id: str, body: dict, user) -> dict:
    # a retry of a receipt that already settled the bill replays it (before "no balance" can refuse it)
    seen = await sb(scoped_query("opd_receipts", clinic_id).eq("idempotency_key", body["idempotency_key"]).limit(1))
    if seen.data and any(str(i["id"]) == str(seen.data[0]["invoice_id"]) for i in await _invoices(clinic_id, accession_id)):
        return seen.data[0]
    inv = await open_invoice(clinic_id, accession_id, body.get("invoice_id"))
    return await opd_billing.create_receipt(clinic_id, inv["id"], body["mode"], body["amount_paise"],
                                            body["idempotency_key"], body.get("reference"), actor=user,
                                            shift_id=body.get("shift_id"))


async def payment_link(clinic_id: str, clinic: dict, accession_id: str) -> dict:
    from app.services.payment import payment_service
    inv = await open_invoice(clinic_id, accession_id)
    pat = await sb(scoped_query("patients", clinic_id).eq("id", inv["patient_id"]).limit(1))
    link = await payment_service.create_opd_payment_link(clinic, inv, (pat.data or [{}])[0], _balance(inv))
    return {**link, "invoice_id": inv["id"], "amount_paise": _balance(inv)}
