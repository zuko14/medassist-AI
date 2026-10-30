"""GST on bills, credit notes and the GSTR-1 month summary (migration 117).

The database owns every tax number: line tax and the CGST/SGST split are set by
the opd_invoice_items trigger, room-rent GST by the ipd_charges trigger, the
credit note's tax share and number by opd_issue_credit_note(). This module
validates input, moves the refund through the cashier's shift, and reads.
"""

import re
from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services.opd_billing import _format_money_inr, resolve_counter_shift
from app.utils.helpers import IST, actor_uuid

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
#: GST slabs in basis points the schema accepts (opd_invoice_items.tax_rate_bps).
GST_RATES_BPS = (0, 500, 1200, 1800, 2800, 4000)
CREDIT_NOTE_REASONS = ("cancellation", "discount_post_issue", "billing_error", "service_deficiency", "other")


_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def valid_gstin(gstin: str) -> bool:
    """Shape plus the 15th-character check digit (GSTN's mod-36 scheme): a typo
    would otherwise print a wrong registration on every legal invoice."""
    if not GSTIN_RE.match(gstin or ""):
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        v = _GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return gstin[14] == _GSTIN_CHARS[(36 - total % 36) % 36]


def gst_amount(value_paise: int, rate_bps: int) -> int:
    """Half-up tax on a non-negative value; the same integer formula as SQL opd_gst_amount()."""
    return (max(value_paise, 0) * rate_bps + 5000) // 10000


def calculate_line_gst(subtotal_paise: int, tax_rate_bps: int, is_interstate: bool = False) -> dict:
    """Tax on one line. Intra-state splits CGST = tax // 2, SGST = the rest."""
    tax = gst_amount(subtotal_paise, tax_rate_bps)
    if is_interstate:
        return {"cgst_paise": 0, "sgst_paise": 0, "igst_paise": tax, "total_tax_paise": tax}
    return {"cgst_paise": tax // 2, "sgst_paise": tax - tax // 2, "igst_paise": 0, "total_tax_paise": tax}


def _db_error(e: Exception) -> str:
    return str(getattr(e, "message", None) or e)


async def list_credit_notes(clinic_id: str, invoice_id: str) -> list[dict]:
    res = await sb(scoped_query("opd_credit_notes", clinic_id).eq("invoice_id", invoice_id)
                   .order("created_at", desc=False))
    return res.data or []


async def issue_credit_note(
    clinic_id: str,
    invoice_id: str,
    amount_paise: int,
    reason: str,
    reason_notes: Optional[str],
    actor: Any,
    idempotency_key: Optional[str] = None,
    refund_mode: Optional[str] = None,
    refund_reference: Optional[str] = None,
    shift_id: Optional[str] = None,
) -> dict:
    """Issue a CRN- credit note for `amount_paise` (tax-inclusive) on an issued invoice.

    If the patient has paid more than the invoice now asks for, the excess is refunded
    in the same transaction, in cash or UPI, into the caller's open shift."""
    if reason not in CREDIT_NOTE_REASONS:
        raise HTTPException(status_code=422, detail="Unknown credit note reason.")
    if amount_paise <= 0:
        raise HTTPException(status_code=422, detail="Credit amount must be more than zero.")

    async def _existing() -> Optional[dict]:
        res = await sb(scoped_query("opd_credit_notes", clinic_id).eq("idempotency_key", idempotency_key).limit(1))
        return res.data[0] if res.data else None

    if idempotency_key:
        replay = await _existing()
        if replay:
            if str(replay["invoice_id"]) != str(invoice_id):
                raise HTTPException(status_code=409, detail="This credit note key was already used for another bill.")
            return {**replay, "replayed": True}

    inv_res = await sb(scoped_query("opd_invoices", clinic_id).eq("id", invoice_id).limit(1))
    if not inv_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    inv = inv_res.data[0]
    if inv["status"] not in ("issued", "partially_paid", "paid"):
        raise HTTPException(status_code=409, detail=f"A credit note needs an issued bill, not a '{inv['status']}' one.")
    due = inv["subtotal_paise"] - inv["discount_paise"] - inv.get("credited_paise", 0)
    if amount_paise > due:
        raise HTTPException(status_code=422,
                            detail=f"Credit ({_format_money_inr(amount_paise)}) exceeds what is left on the bill "
                                   f"({_format_money_inr(due)}).")
    excess = inv["paid_paise"] - min(inv["paid_paise"], due - amount_paise)
    if excess > 0:
        if refund_mode not in ("cash", "upi"):
            raise HTTPException(status_code=422,
                                detail=f"The patient has paid {_format_money_inr(excess)} more than the reduced bill. "
                                       "Choose cash or UPI to refund it.")
        if refund_mode == "upi" and not (refund_reference or "").strip():
            raise HTTPException(status_code=422, detail="UPI refund needs the transaction reference (UTR).")
        shift_id = await resolve_counter_shift(clinic_id, actor, shift_id, require_drawer=True)
    else:
        shift_id = None

    try:
        res = await sb(supabase.rpc("opd_issue_credit_note", {
            "p_clinic_id": clinic_id, "p_invoice_id": invoice_id, "p_amount_paise": amount_paise,
            "p_reason": reason, "p_reason_notes": reason_notes, "p_idempotency_key": idempotency_key,
            "p_refund_mode": refund_mode if excess > 0 else None,
            "p_refund_reference": refund_reference if excess > 0 else None,
            "p_shift_id": shift_id, "p_actor_id": actor_uuid(actor),
            "p_actor_name": str(getattr(actor, "username", None) or "staff")[:100],
        }))
    except Exception as e:
        msg = _db_error(e)
        if idempotency_key and "uq_opd_credit_notes_idempotency" in msg:   # a concurrent retry won
            replay = await _existing()
            if replay:
                return {**replay, "replayed": True}
        if "opd_credit_note_key_reused" in msg:
            raise HTTPException(status_code=409, detail="This credit note key was already used for another bill.")
        if "opd_credit_note_exceeds_invoice" in msg or "opd_credit_note_refund_required" in msg \
                or "opd_credit_note_invoice_state" in msg:
            raise HTTPException(status_code=409, detail="The bill changed while you were working. Reload it and try again.")
        if "opd_shift_not_open" in msg or "opd_shift_not_yours" in msg:
            raise HTTPException(status_code=409, detail="Your cashier shift is closed. Open a shift to refund.")
        if "opd_not_found" in msg:
            raise HTTPException(status_code=404, detail="Invoice not found")
        raise
    return res.data


def _ist_month_bounds(month: int, year: int) -> tuple[str, str]:
    start = datetime(year, month, 1, tzinfo=IST)
    end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=IST)
    return start.isoformat(), end.isoformat()


def _split_by_rate(inv: dict, lines: list[dict]) -> dict[int, list[int]]:
    """{rate_bps: [taxable_paise, tax_paise]} for one invoice, after its header discount.

    The header discount lowers every line pro rata (as migration 117 computes the
    header tax); rounding is pushed onto the largest bucket so the buckets add up
    exactly to the invoice's total and total_tax."""
    buckets: dict[int, list[int]] = {}
    for ln in lines:
        b = buckets.setdefault(ln["tax_rate_bps"], [0, 0])
        b[0] += ln["line_total_paise"]
        b[1] += ln["total_tax_paise"]
    total, sub = inv["total_paise"], inv["subtotal_paise"]
    if sub and total != sub:
        buckets = {r: [(v * total * 2 + sub) // (2 * sub), (t * total * 2 + sub) // (2 * sub)]
                   for r, (v, t) in buckets.items()}
    if buckets:
        top = max(buckets, key=lambda r: sum(buckets[r]))
        buckets[top][1] += inv["total_tax_paise"] - sum(t for _, t in buckets.values())
        buckets[top][0] += (total - inv["total_tax_paise"]) - sum(v for v, _ in buckets.values())
    return buckets


async def export_gstr1_report(clinic_id: str, month: int, year: int, actor: Any = None) -> dict:
    """GSTR-1 working summary for one IST calendar month.

    b2b: invoices to a buyer with a GSTIN, one row each; b2cs: the rest, by rate;
    nil_exempt: exempt (0%) supplies; credit_notes: CRN- issued in the month;
    doc_summary: the numbered series with cancellations (Table 13)."""
    if not 1 <= month <= 12 or not 2017 <= year <= 2100:
        raise HTTPException(status_code=422, detail="Pick a valid month and year.")
    start, end = _ist_month_bounds(month, year)
    cols = ("id, invoice_number, invoice_seq, issued_at, status, subtotal_paise, discount_paise, total_paise, "
            "total_tax_paise, credited_paise, clinic_gstin, patient_gstin, patient_snapshot")
    invoices = await fetch_all(lambda: scoped_query("opd_invoices", clinic_id, cols)
                               .gte("issued_at", start).lt("issued_at", end)
                               .order("invoice_seq"), label="gstr1:invoices")
    live = [i for i in invoices if i["status"] != "void"]

    lines_by_inv: dict[str, list[dict]] = {}
    taxed_ids = [i["id"] for i in live if i["total_tax_paise"] > 0]
    for k in range(0, len(taxed_ids), 100):   # batches keep the request URL short
        rows = (await sb(scoped_query("opd_invoice_items", clinic_id,
                                      "invoice_id, line_total_paise, total_tax_paise, tax_rate_bps")
                         .in_("invoice_id", taxed_ids[k:k + 100]))).data or []
        for r in rows:
            lines_by_inv.setdefault(r["invoice_id"], []).append(r)

    b2b, b2cs, nil_exempt = [], {}, 0
    for inv in live:
        buckets = (_split_by_rate(inv, lines_by_inv.get(inv["id"], []))
                   if inv["total_tax_paise"] > 0 else {0: [inv["total_paise"], 0]})
        nil_exempt += buckets.pop(0, [0, 0])[0]
        if not buckets:
            continue
        if inv.get("patient_gstin"):
            b2b.append({"invoice_number": inv["invoice_number"], "issued_at": inv["issued_at"],
                        "buyer_gstin": inv["patient_gstin"],
                        "buyer_name": (inv.get("patient_snapshot") or {}).get("name"),
                        "invoice_value_paise": inv["total_paise"],
                        "rates": [{"rate_bps": r, "taxable_paise": v, "cgst_paise": t // 2, "sgst_paise": t - t // 2}
                                  for r, (v, t) in sorted(buckets.items())]})
        else:
            for r, (v, t) in buckets.items():
                agg = b2cs.setdefault(r, [0, 0])
                agg[0] += v
                agg[1] += t

    cns = await fetch_all(lambda: scoped_query("opd_credit_notes", clinic_id,
                                               "id, invoice_id, credit_note_number, credit_note_seq, created_at, "
                                               "amount_paise, tax_paise, total_credit_paise, reason, status")
                          .gte("created_at", start).lt("created_at", end).order("credit_note_seq"),
                          label="gstr1:credit_notes")
    cn_inv: dict[str, dict] = {}
    cn_inv_ids = list({c["invoice_id"] for c in cns})
    for k in range(0, len(cn_inv_ids), 100):
        for r in (await sb(scoped_query("opd_invoices", clinic_id, "id, invoice_number, issued_at, patient_gstin")
                           .in_("id", cn_inv_ids[k:k + 100]))).data or []:
            cn_inv[r["id"]] = r
    credit_notes = [{
        "credit_note_number": c["credit_note_number"], "created_at": c["created_at"], "reason": c["reason"],
        "invoice_number": cn_inv.get(c["invoice_id"], {}).get("invoice_number"),
        "invoice_date": cn_inv.get(c["invoice_id"], {}).get("issued_at"),
        "buyer_gstin": cn_inv.get(c["invoice_id"], {}).get("patient_gstin"),
        "taxable_paise": c["amount_paise"], "cgst_paise": c["tax_paise"] // 2,
        "sgst_paise": c["tax_paise"] - c["tax_paise"] // 2, "total_paise": c["total_credit_paise"],
    } for c in cns if c["status"] == "issued"]

    def _series(numbers: list[str], cancelled: int) -> dict:
        return {"from": numbers[0] if numbers else None, "to": numbers[-1] if numbers else None,
                "total": len(numbers), "cancelled": cancelled}

    gstin = next((i["clinic_gstin"] for i in invoices if i.get("clinic_gstin")), None)
    return {
        "period": f"{year:04d}-{month:02d}",
        "clinic_gstin": gstin,
        "b2b": b2b,
        "b2cs": [{"rate_bps": r, "taxable_paise": v, "cgst_paise": t // 2, "sgst_paise": t - t // 2}
                 for r, (v, t) in sorted(b2cs.items())],
        "nil_exempt_paise": nil_exempt,
        "credit_notes": credit_notes,
        "doc_summary": {
            "invoices": _series([i["invoice_number"] for i in invoices],
                                sum(1 for i in invoices if i["status"] == "void")),
            "credit_notes": _series([c["credit_note_number"] for c in cns],
                                    sum(1 for c in cns if c["status"] == "void")),
        },
        "totals": {
            "invoice_value_paise": sum(i["total_paise"] for i in live),
            "tax_paise": sum(i["total_tax_paise"] for i in live),
            "credit_value_paise": sum(c["total_paise"] for c in credit_notes),
            "credit_tax_paise": sum(c["cgst_paise"] + c["sgst_paise"] for c in credit_notes),
        },
    }
