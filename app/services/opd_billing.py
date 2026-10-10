"""Kriya OPD OS Billing & Cashier Service (Phase 1.4).

Manages invoices, invoice items, receipts, cashier shifts, payment links,
and daily drawer reconciliation.
Zero-LLM clinical safety compliant.
"""

import asyncio
import logging
import secrets
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.database import (
    sb,
    scoped_query,
    supabase,
)
from app.services.tenant import get_clinic_by_id
from app.utils.helpers import IST, doctor_title, today_ist

logger = logging.getLogger(__name__)

# In-memory TTL cache for receipt idempotency keys (clinic_id:key -> (timestamp, receipt))
_IDEMPOTENCY_CACHE: Dict[str, Tuple[float, dict]] = {}
_IDEMPOTENCY_TTL_SECONDS = 600.0  # 10 minutes


def _clean_idempotency_cache() -> None:
    """Evict expired idempotency keys."""
    now = time.time()
    expired = [k for k, (ts, _) in _IDEMPOTENCY_CACHE.items() if now - ts > _IDEMPOTENCY_TTL_SECONDS]
    for k in expired:
        _IDEMPOTENCY_CACHE.pop(k, None)


def _format_money_inr(paise: int) -> str:
    """Format paise as ₹ INR string."""
    rupees = paise / 100.0
    return f"₹{rupees:,.2f}"


async def get_catalog(clinic_id: str) -> dict:
    """Load doctor consultation fees, clinic service catalog, and lab tests menu."""
    # Active doctors and consultation fees
    docs_res = await sb(
        scoped_query("doctors", clinic_id)
        .eq("is_active", True)
        .select("id, name, department, consultation_fee")
        .order("name", desc=False)
    )
    doctors = []
    for d in (docs_res.data or []):
        fee = d.get("consultation_fee")
        fee_paise = int(fee * 100) if fee is not None else 0
        doctors.append({
            "id": str(d["id"]),
            "name": d.get("name") or "Doctor",
            "department": d.get("department") or "",
            "consultation_fee_paise": fee_paise,
        })

    # Service catalog from clinic opd_settings
    clinic = await get_clinic_by_id(clinic_id)
    opd_settings = (clinic or {}).get("opd_settings") or {}
    service_catalog = opd_settings.get("service_catalog") or []

    # Active lab tests
    lab_res = await sb(
        scoped_query("lab_tests", clinic_id)
        .eq("is_active", True)
        .select("id, name, sample_type, price_paise")
        .order("name", desc=False)
    )
    lab_tests = []
    for t in (lab_res.data or []):
        lab_tests.append({
            "id": str(t["id"]),
            "name": t.get("name") or "Lab Test",
            "sample_type": t.get("sample_type"),
            "price_paise": t.get("price_paise") or 0,
        })

    return {
        "doctors": doctors,
        "service_catalog": service_catalog,
        "lab_tests": lab_tests,
    }


async def get_or_create_appointment_invoice(
    clinic_id: str,
    appointment_id: str,
    actor: Any,
) -> dict:
    """Create or retrieve the single live invoice for an appointment visit (§3.7)."""
    # Verify appointment
    appt_res = await sb(
        scoped_query("appointments", clinic_id)
        .eq("id", appointment_id)
        .limit(1)
    )
    if not appt_res.data:
        raise HTTPException(status_code=404, detail="Appointment not found")
    appt = appt_res.data[0]

    # Check for existing non-void invoice for this visit
    existing = await sb(
        scoped_query("opd_invoices", clinic_id)
        .eq("appointment_id", appointment_id)
        .neq("status", "void")
        .limit(1)
    )
    if existing.data:
        inv_id = str(existing.data[0]["id"])
        return await get_invoice(clinic_id, inv_id)

    # Fetch patient row for demographic snapshot
    pat_res = await sb(
        scoped_query("patients", clinic_id)
        .eq("id", appt["patient_id"])
        .limit(1)
    )
    patient = pat_res.data[0] if pat_res.data else {}
    patient_snapshot = {
        "name": appt.get("patient_name") or patient.get("name"),
        "phone": patient.get("phone"),
        "mrn": appt.get("mrn") or patient.get("mrn"),
        "gender": patient.get("gender"),
        "age_years": patient.get("age_years"),
        "doctor_name": appt.get("doctor_name"),
    }

    # Determine doctor consultation fee
    fee_paise = 0
    doc_name = appt.get("doctor_name") or "Doctor"
    doc_id = appt.get("doctor_id")
    if doc_id:
        doc_res = await sb(
            scoped_query("doctors", clinic_id)
            .eq("id", doc_id)
            .limit(1)
        )
        if doc_res.data:
            doc_row = doc_res.data[0]
            doc_name = doc_row.get("name") or doc_name
            fee = doc_row.get("consultation_fee")
            if fee is not None:
                fee_paise = int(fee * 100)

    # Create draft invoice
    invoice_payload = {
        "clinic_id": clinic_id,
        "branch_id": appt.get("branch_id"),
        "appointment_id": appointment_id,
        "encounter_id": None,
        "patient_id": appt["patient_id"],
        "family_member_id": appt.get("family_member_id"),
        "status": "draft",
        "subtotal_paise": fee_paise,
        "discount_paise": 0,
        "paid_paise": 0,
        "patient_snapshot": patient_snapshot,
        "created_by": getattr(actor, "user_id", None),
    }
    # unscoped: insert_scoped_by_payload
    inv_ins = await sb(supabase.table("opd_invoices").insert(invoice_payload))
    if not inv_ins.data:
        raise HTTPException(status_code=500, detail="Failed to initialize visit invoice.")
    new_inv = inv_ins.data[0]
    inv_id = str(new_inv["id"])

    # Insert consultation line item
    item_payload = {
        "clinic_id": clinic_id,
        "invoice_id": inv_id,
        "line_no": 1,
        "item_type": "consultation",
        "catalog_code": None,
        "doctor_id": doc_id,
        "lab_test_id": None,
        "description": f"Consultation - {doctor_title(doc_name)}",
        "quantity": 1,
        "unit_price_paise": fee_paise,
        "discount_paise": 0,
    }
    # unscoped: insert_scoped_by_payload
    await sb(supabase.table("opd_invoice_items").insert(item_payload))

    return await get_invoice(clinic_id, inv_id)


async def create_invoice(
    clinic_id: str,
    patient_id: str,
    items: list[dict],
    family_member_id: Optional[str] = None,
    appointment_id: Optional[str] = None,
    discount_paise: int = 0,
    discount_reason: Optional[str] = None,
    notes: Optional[str] = None,
    branch_id: Optional[str] = None,
    actor: Any = None,
) -> dict:
    """Create an ad-hoc or direct draft invoice."""
    # Check if appointment already has a live invoice
    if appointment_id:
        existing = await sb(
            scoped_query("opd_invoices", clinic_id)
            .eq("appointment_id", appointment_id)
            .neq("status", "void")
            .limit(1)
        )
        if existing.data:
            raise HTTPException(status_code=409, detail="A live invoice already exists for this visit.")

    # Validate patient
    pat_res = await sb(scoped_query("patients", clinic_id).eq("id", patient_id).limit(1))
    if not pat_res.data:
        raise HTTPException(status_code=404, detail="Patient not found")
    patient = pat_res.data[0]

    if discount_paise > 0 and not discount_reason:
        raise HTTPException(status_code=422, detail="Discount reason is required when discount is applied.")

    patient_snapshot = {
        "name": patient.get("name"),
        "phone": patient.get("phone"),
        "mrn": patient.get("mrn"),
        "gender": patient.get("gender"),
        "age_years": patient.get("age_years"),
    }

    # Validate items and calculate subtotal
    validated_items, subtotal_paise = await _validate_and_price_items(
        clinic_id, items, actor=actor
    )

    if discount_paise > subtotal_paise:
        raise HTTPException(status_code=422, detail="Discount cannot exceed invoice subtotal.")

    inv_payload = {
        "clinic_id": clinic_id,
        "branch_id": branch_id or getattr(actor, "branch_id", None),
        "appointment_id": appointment_id,
        "encounter_id": None,
        "patient_id": patient_id,
        "family_member_id": family_member_id,
        "status": "draft",
        "subtotal_paise": subtotal_paise,
        "discount_paise": discount_paise,
        "discount_reason": discount_reason if discount_paise > 0 else None,
        "notes": notes,
        "paid_paise": 0,
        "patient_snapshot": patient_snapshot,
        "created_by": getattr(actor, "user_id", None),
    }
    # unscoped: insert_scoped_by_payload
    inv_ins = await sb(supabase.table("opd_invoices").insert(inv_payload))
    if not inv_ins.data:
        raise HTTPException(status_code=500, detail="Failed to create invoice.")
    inv_id = str(inv_ins.data[0]["id"])

    # Insert items
    for line_idx, item in enumerate(validated_items, start=1):
        item_row = {
            "clinic_id": clinic_id,
            "invoice_id": inv_id,
            "line_no": line_idx,
            "item_type": item["item_type"],
            "catalog_code": item.get("catalog_code"),
            "doctor_id": item.get("doctor_id"),
            "lab_test_id": item.get("lab_test_id"),
            "description": item["description"],
            "quantity": item["quantity"],
            "unit_price_paise": item["unit_price_paise"],
            "discount_paise": item.get("discount_paise", 0),
        }
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("opd_invoice_items").insert(item_row))

    return await get_invoice(clinic_id, inv_id)


async def save_invoice_draft(
    clinic_id: str,
    invoice_id: str,
    expected_updated_at: datetime,
    items: list[dict],
    discount_paise: int = 0,
    discount_reason: Optional[str] = None,
    notes: Optional[str] = None,
    actor: Any = None,
) -> dict:
    """Update draft invoice items and discounts with CAS optimistic locking."""
    inv_res = await sb(
        scoped_query("opd_invoices", clinic_id)
        .eq("id", invoice_id)
        .limit(1)
    )
    if not inv_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice = inv_res.data[0]

    if invoice.get("status") != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot edit invoice in '{invoice.get('status')}' status. Only draft invoices can be edited.",
        )

    # CAS check
    actual_updated_str = invoice.get("updated_at")
    if actual_updated_str:
        actual_updated = datetime.fromisoformat(actual_updated_str.replace("Z", "+00:00"))
        expected_norm = expected_updated_at.astimezone(timezone.utc)
        actual_norm = actual_updated.astimezone(timezone.utc)
        # Compare seconds with 0.1s margin
        if abs((actual_norm - expected_norm).total_seconds()) > 0.5:
            raise HTTPException(
                status_code=409,
                detail="Invoice was modified concurrently. Please reload to review the latest changes.",
            )

    if discount_paise > 0 and not discount_reason:
        raise HTTPException(status_code=422, detail="Discount reason is required when discount is applied.")

    validated_items, subtotal_paise = await _validate_and_price_items(
        clinic_id, items, actor=actor
    )

    if discount_paise > subtotal_paise:
        raise HTTPException(status_code=422, detail="Discount cannot exceed invoice subtotal.")

    # Delete existing items
    await sb(
        supabase.table("opd_invoice_items")
        .delete()
        .eq("clinic_id", clinic_id)
        .eq("invoice_id", invoice_id)
    )

    # Insert updated items
    for line_idx, item in enumerate(validated_items, start=1):
        item_row = {
            "clinic_id": clinic_id,
            "invoice_id": invoice_id,
            "line_no": line_idx,
            "item_type": item["item_type"],
            "catalog_code": item.get("catalog_code"),
            "doctor_id": item.get("doctor_id"),
            "lab_test_id": item.get("lab_test_id"),
            "description": item["description"],
            "quantity": item["quantity"],
            "unit_price_paise": item["unit_price_paise"],
            "discount_paise": item.get("discount_paise", 0),
        }
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("opd_invoice_items").insert(item_row))

    # Update invoice fields
    updates = {
        "subtotal_paise": subtotal_paise,
        "discount_paise": discount_paise,
        "discount_reason": discount_reason if discount_paise > 0 else None,
        "notes": notes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await sb(
        supabase.table("opd_invoices")
        .update(updates)
        .eq("clinic_id", clinic_id)
        .eq("id", invoice_id)
    )

    return await get_invoice(clinic_id, invoice_id)


async def issue_invoice(
    clinic_id: str,
    invoice_id: str,
    actor: Any,
) -> dict:
    """Issue a draft invoice, triggering atomic number assignment and prepaid detection."""
    inv_res = await sb(
        scoped_query("opd_invoices", clinic_id)
        .eq("id", invoice_id)
        .limit(1)
    )
    if not inv_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice = inv_res.data[0]

    # Already issued? Idempotent return
    if invoice.get("status") in ("issued", "partially_paid", "paid"):
        return await get_invoice(clinic_id, invoice_id)

    if invoice.get("status") == "void":
        raise HTTPException(status_code=409, detail="Cannot issue a void invoice.")

    # Check that invoice has items
    items_res = await sb(
        scoped_query("opd_invoice_items", clinic_id)
        .eq("invoice_id", invoice_id)
    )
    if not items_res.data:
        raise HTTPException(status_code=422, detail="Cannot issue an invoice without line items.")

    # Mark as issued (trigger opd_guard_invoice assigns year, seq, number, and status 'paid' if total == 0)
    issue_update = {
        "status": "issued",
        "issued_by": getattr(actor, "user_id", None),
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await sb(
        supabase.table("opd_invoices")
        .update(issue_update)
        .eq("clinic_id", clinic_id)
        .eq("id", invoice_id)
    )

    # Prepaid online booking detection
    appt_id = invoice.get("appointment_id")
    if appt_id:
        appt_res = await sb(
            scoped_query("appointments", clinic_id)
            .eq("id", appt_id)
            .limit(1)
        )
        if appt_res.data:
            appt = appt_res.data[0]
            # appointments has no payment_status column: payment_id is set only
            # once the gateway captured an online (WhatsApp/voice) payment.
            if appt.get("payment_id") and appt.get("status") not in ("refunded", "cancelled"):
                fee_paise = appt.get("amount_paise") or (
                    invoice.get("subtotal_paise", 0) - invoice.get("discount_paise", 0)
                )

                prepaid_gw_id = appt.get("payment_id") or f"prepaid:{appt['id']}"
                gw = appt.get("payment_gateway") or "razorpay"

                # Check if receipt already created for this gateway payment
                existing_rct = await sb(
                    scoped_query("opd_receipts", clinic_id)
                    .eq("gateway", gw)
                    .eq("gateway_payment_id", prepaid_gw_id)
                    .limit(1)
                )
                if not existing_rct.data:
                    receipt_data = {
                        "clinic_id": clinic_id,
                        "invoice_id": invoice_id,
                        "shift_id": None,
                        "kind": "payment",
                        "mode": "prepaid_online",
                        "amount_paise": fee_paise,
                        "reference": appt.get("booking_ref") or str(appt["id"])[:8],
                        "gateway": gw,
                        "gateway_payment_id": prepaid_gw_id,
                        "received_by_admin_id": None,
                        "received_by_name": "online",
                    }
                    # unscoped: insert_scoped_by_payload
                    await sb(supabase.table("opd_receipts").insert(receipt_data))

    return await get_invoice(clinic_id, invoice_id)


async def create_receipt(
    clinic_id: str,
    invoice_id: str,
    mode: str,
    amount_paise: int,
    idempotency_key: str,
    reference: Optional[str] = None,
    actor: Any = None,
    shift_id: Optional[str] = None,
) -> dict:
    """Record an in-person counter receipt (cash, upi, card) against an issued invoice."""
    _clean_idempotency_cache()
    cache_key = f"{clinic_id}:{idempotency_key}"
    if cache_key in _IDEMPOTENCY_CACHE:
        return _IDEMPOTENCY_CACHE[cache_key][1]

    if mode not in ("cash", "upi", "card"):
        raise HTTPException(status_code=422, detail="Counter receipts accept only 'cash', 'upi', or 'card'.")

    if mode in ("upi", "card") and not reference:
        raise HTTPException(status_code=422, detail=f"Transaction reference (UTR/RRN) is required for {mode.upper()} payments.")

    inv_res = await sb(
        scoped_query("opd_invoices", clinic_id)
        .eq("id", invoice_id)
        .limit(1)
    )
    if not inv_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice = inv_res.data[0]

    if invoice.get("status") not in ("issued", "partially_paid"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot record payment for invoice in '{invoice.get('status')}' status.",
        )

    total_paise = invoice.get("total_paise") or (invoice.get("subtotal_paise", 0) - invoice.get("discount_paise", 0))
    paid_paise = invoice.get("paid_paise", 0)
    balance_due = total_paise - paid_paise

    if amount_paise > balance_due:
        raise HTTPException(
            status_code=422,
            detail=f"Payment amount ({_format_money_inr(amount_paise)}) exceeds balance due ({_format_money_inr(balance_due)}).",
        )

    # Cashier shift resolution
    admin_id = getattr(actor, "user_id", None)
    resolved_shift_id = None
    if shift_id:
        shift_res = await sb(
            scoped_query("opd_cashier_shifts", clinic_id)
            .eq("id", shift_id)
            .limit(1)
        )
        if not shift_res.data:
            raise HTTPException(status_code=404, detail="Cashier shift not found.")
        target_shift = shift_res.data[0]
        if str(target_shift.get("cashier_admin_id")) != str(admin_id):
            raise HTTPException(status_code=409, detail="Cannot record receipt into another cashier's shift.")
        if target_shift.get("status") != "open" or target_shift.get("closed_at") is not None:
            raise HTTPException(status_code=409, detail="Target cashier shift is closed.")
        resolved_shift_id = str(target_shift["id"])
    else:
        shift_res = await sb(
            scoped_query("opd_cashier_shifts", clinic_id)
            .eq("cashier_admin_id", admin_id)
            .eq("status", "open")
            .limit(1)
        )
        if shift_res.data:
            resolved_shift_id = str(shift_res.data[0]["id"])
        else:
            # Check if auto_open_shift is enabled
            clinic = await get_clinic_by_id(clinic_id)
            opd_settings = (clinic or {}).get("opd_settings") or {}
            if opd_settings.get("auto_open_shift", True):
                auto_shift = {
                    "clinic_id": clinic_id,
                    "branch_id": getattr(actor, "branch_id", None),
                    "cashier_admin_id": admin_id,
                    "cashier_name": getattr(actor, "username", "Cashier"),
                    "status": "open",
                    "opening_float_paise": 0,
                }
                # unscoped: insert_scoped_by_payload
                shift_ins = await sb(supabase.table("opd_cashier_shifts").insert(auto_shift))
                if shift_ins.data:
                    resolved_shift_id = str(shift_ins.data[0]["id"])
            else:
                raise HTTPException(
                    status_code=409,
                    detail="No open cashier shift found. Please open a shift before receiving counter payments.",
                )

    receipt_payload = {
        "clinic_id": clinic_id,
        "invoice_id": invoice_id,
        "shift_id": resolved_shift_id,
        "kind": "payment",
        "mode": mode,
        "amount_paise": amount_paise,
        "reference": reference,
        "gateway": None,
        "gateway_payment_id": None,
        "received_by_admin_id": admin_id,
        "received_by_name": getattr(actor, "username", "Cashier"),
        "reason": None,
    }
    # unscoped: insert_scoped_by_payload
    rct_ins = await sb(supabase.table("opd_receipts").insert(receipt_payload))
    if not rct_ins.data:
        raise HTTPException(status_code=500, detail="Failed to persist receipt.")
    receipt = rct_ins.data[0]

    # Cache for idempotency
    _IDEMPOTENCY_CACHE[cache_key] = (time.time(), receipt)

    # Dispatch receipt WhatsApp notification
    try:
        pat_res = await sb(scoped_query("patients", clinic_id).eq("id", invoice["patient_id"]).limit(1))
        patient_row = pat_res.data[0] if pat_res.data else None
        if patient_row and patient_row.get("phone"):
            from app.services.whatsapp import whatsapp_service
            clinic_row = await get_clinic_by_id(clinic_id)
            templates = ((clinic_row or {}).get("opd_settings") or {}).get("templates") or {}
            tmpl_name = templates.get("receipt") or templates.get("opd_receipt")
            amt_str = _format_money_inr(amount_paise)
            inv_num = invoice.get("invoice_number") or f"INV-{invoice_id[:8]}"
            if tmpl_name:
                asyncio.create_task(
                    whatsapp_service.send_template(
                        clinic_row,
                        patient_row["phone"],
                        template_name=tmpl_name,
                        components=[
                            {
                                "type": "body",
                                "parameters": [
                                    {"type": "text", "text": inv_num},
                                    {"type": "text", "text": amt_str},
                                    {"type": "text", "text": mode.upper()},
                                ],
                            }
                        ],
                        _source="opd_receipt",
                    )
                )
            else:
                asyncio.create_task(
                    whatsapp_service.send_text(
                        clinic_row,
                        patient_row["phone"],
                        f"Payment of {amt_str} received for invoice {inv_num}. Mode: {mode.upper()}.",
                        _source="opd_receipt",
                    )
                )
    except Exception as e:
        logger.debug(f"Failed to queue receipt notification: {e}")

    return receipt


async def create_refund(
    clinic_id: str,
    invoice_id: str,
    mode: str,
    amount_paise: int,
    reason: str,
    reference: Optional[str] = None,
    actor: Any = None,
) -> dict:
    """Process an admin refund against an invoice."""
    if not _is_opd_admin(actor):
        raise HTTPException(status_code=403, detail="Refunds require OPD_ADMIN permissions.")

    if len(reason.strip()) < 5:
        raise HTTPException(status_code=422, detail="Refund reason must be at least 5 characters.")

    inv_res = await sb(
        scoped_query("opd_invoices", clinic_id)
        .eq("id", invoice_id)
        .limit(1)
    )
    if not inv_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice = inv_res.data[0]

    paid_paise = invoice.get("paid_paise", 0)
    if amount_paise > paid_paise:
        raise HTTPException(
            status_code=422,
            detail=f"Refund amount ({_format_money_inr(amount_paise)}) exceeds total paid ({_format_money_inr(paid_paise)}).",
        )

    # Counter refund in cash requires open shift
    shift_id = None
    admin_id = getattr(actor, "user_id", None)
    if mode == "cash":
        shift_res = await sb(
            scoped_query("opd_cashier_shifts", clinic_id)
            .eq("cashier_admin_id", admin_id)
            .eq("status", "open")
            .limit(1)
        )
        if not shift_res.data:
            # Auto-open or 409
            clinic = await get_clinic_by_id(clinic_id)
            opd_settings = (clinic or {}).get("opd_settings") or {}
            if opd_settings.get("auto_open_shift", True):
                auto_shift = {
                    "clinic_id": clinic_id,
                    "branch_id": getattr(actor, "branch_id", None),
                    "cashier_admin_id": admin_id,
                    "cashier_name": getattr(actor, "username", "Admin"),
                    "status": "open",
                    "opening_float_paise": 0,
                }
                # unscoped: insert_scoped_by_payload
                shift_ins = await sb(supabase.table("opd_cashier_shifts").insert(auto_shift))
                if shift_ins.data:
                    shift_id = str(shift_ins.data[0]["id"])
            else:
                raise HTTPException(status_code=409, detail="An open cashier shift is required for cash refunds.")
        else:
            shift_id = str(shift_res.data[0]["id"])

    refund_ref = reference or f"REF-{secrets.token_hex(4).upper()}"
    refund_payload = {
        "clinic_id": clinic_id,
        "invoice_id": invoice_id,
        "shift_id": shift_id,
        "kind": "refund",
        "mode": mode,
        "amount_paise": amount_paise,
        "reference": refund_ref,
        "reason": reason.strip(),
        "received_by_admin_id": admin_id,
        "received_by_name": getattr(actor, "username", "Admin"),
    }
    # unscoped: insert_scoped_by_payload
    rct_ins = await sb(supabase.table("opd_receipts").insert(refund_payload))
    if not rct_ins.data:
        raise HTTPException(status_code=500, detail="Failed to record refund.")
    return rct_ins.data[0]


async def void_invoice(
    clinic_id: str,
    invoice_id: str,
    reason: str,
    actor: Any = None,
) -> dict:
    """Void an invoice (OPD_ADMIN only). Fails if payments exist."""
    if not _is_opd_admin(actor):
        raise HTTPException(status_code=403, detail="Voiding an invoice requires OPD_ADMIN permissions.")

    if len(reason.strip()) < 5:
        raise HTTPException(status_code=422, detail="Void reason must be at least 5 characters.")

    inv_res = await sb(
        scoped_query("opd_invoices", clinic_id)
        .eq("id", invoice_id)
        .limit(1)
    )
    if not inv_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice = inv_res.data[0]

    if invoice.get("paid_paise", 0) > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot void an invoice with payments ({_format_money_inr(invoice['paid_paise'])}). All payments must be refunded first.",
        )

    if invoice.get("status") == "void":
        raise HTTPException(status_code=409, detail="Invoice is already void.")

    updates = {
        "status": "void",
        "voided_at": datetime.now(timezone.utc).isoformat(),
        "voided_by": getattr(actor, "user_id", None),
        "void_reason": reason.strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await sb(
        supabase.table("opd_invoices")
        .update(updates)
        .eq("clinic_id", clinic_id)
        .eq("id", invoice_id)
    )

    return await get_invoice(clinic_id, invoice_id)


async def open_shift(
    clinic_id: str,
    opening_float_paise: int,
    branch_id: Optional[str] = None,
    actor: Any = None,
) -> dict:
    """Open a new cashier drawer shift."""
    admin_id = getattr(actor, "user_id", None)
    if not admin_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    existing = await sb(
        scoped_query("opd_cashier_shifts", clinic_id)
        .eq("cashier_admin_id", admin_id)
        .eq("status", "open")
        .limit(1)
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="You already have an open shift.")

    shift_payload = {
        "clinic_id": clinic_id,
        "branch_id": branch_id or getattr(actor, "branch_id", None),
        "cashier_admin_id": admin_id,
        "cashier_name": getattr(actor, "username", "Cashier"),
        "status": "open",
        "opening_float_paise": max(0, opening_float_paise),
    }
    # unscoped: insert_scoped_by_payload
    ins_res = await sb(supabase.table("opd_cashier_shifts").insert(shift_payload))
    if not ins_res.data:
        raise HTTPException(status_code=500, detail="Failed to open shift.")
    return ins_res.data[0]


async def get_current_shift(
    clinic_id: str,
    actor: Any,
) -> Optional[dict]:
    """Retrieve active open shift with live breakdown by payment mode."""
    admin_id = getattr(actor, "user_id", None)
    if not admin_id:
        return None

    shift_res = await sb(
        scoped_query("opd_cashier_shifts", clinic_id)
        .eq("cashier_admin_id", admin_id)
        .eq("status", "open")
        .limit(1)
    )
    if not shift_res.data:
        return None
    shift = shift_res.data[0]
    shift_id = str(shift["id"])

    # Aggregate receipts in this shift
    rcts_res = await sb(
        scoped_query("opd_receipts", clinic_id)
        .eq("shift_id", shift_id)
    )
    receipts = rcts_res.data or []

    modes = {"cash": 0, "upi": 0, "card": 0}
    cash_refunds = 0
    total_refunds = 0
    for r in receipts:
        amt = r.get("amount_paise", 0)
        m = r.get("mode")
        if r.get("kind") == "refund":
            total_refunds += amt
            if m == "cash":
                cash_refunds += amt
        else:
            if m in modes:
                modes[m] += amt

    opening_float = shift.get("opening_float_paise", 0)
    expected_cash = opening_float + modes["cash"] - cash_refunds

    shift["totals_by_mode"] = modes
    shift["refunds_total_paise"] = total_refunds
    shift["expected_cash_paise"] = expected_cash
    shift["receipts_count"] = len(receipts)
    return shift


async def close_shift(
    clinic_id: str,
    shift_id: str,
    declared_cash_paise: int,
    notes: Optional[str] = None,
    actor: Any = None,
) -> dict:
    """Close cashier shift using the atomic RPC opd_close_shift."""
    admin_id = getattr(actor, "user_id", None)
    if not admin_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Verify shift exists and belongs to caller
    shift_res = await sb(
        scoped_query("opd_cashier_shifts", clinic_id)
        .eq("id", shift_id)
        .limit(1)
    )
    if not shift_res.data:
        raise HTTPException(status_code=404, detail="Shift not found")
    shift = shift_res.data[0]

    if str(shift.get("cashier_admin_id")) != str(admin_id):
        raise HTTPException(status_code=403, detail="You can only close your own shift.")

    if shift.get("status") != "open":
        raise HTTPException(status_code=409, detail="Shift is already closed.")

    # Execute RPC
    rpc_res = await sb(
        supabase.rpc(
            "opd_close_shift",
            {
                "p_clinic_id": clinic_id,
                "p_shift_id": shift_id,
                "p_admin_id": admin_id,
                "p_declared_paise": max(0, declared_cash_paise),
                "p_notes": (notes or "").strip() or None,
            },
        )
    )
    if not rpc_res.data:
        raise HTTPException(status_code=500, detail="Failed to close shift.")
    return rpc_res.data[0]


async def list_shifts(
    clinic_id: str,
    date_str: Optional[str] = None,
    actor: Any = None,
) -> list[dict]:
    """List drawer shifts for admin reconciliation (§3.7)."""
    if not _is_opd_admin(actor):
        raise HTTPException(status_code=403, detail="Admin access required to view drawer shifts.")

    query = scoped_query("opd_cashier_shifts", clinic_id).order("opened_at", desc=True)
    if date_str:
        start_dt = f"{date_str}T00:00:00+05:30"
        end_dt = f"{date_str}T23:59:59+05:30"
        query = query.gte("opened_at", start_dt).lte("opened_at", end_dt)

    res = await sb(query.limit(100))
    return res.data or []


async def get_collections_summary(
    clinic_id: str,
    date_str: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> dict:
    """Compute financial summary for a business day."""
    target_date = date_str or today_ist().isoformat()
    start_dt = f"{target_date}T00:00:00+05:30"
    end_dt = f"{target_date}T23:59:59+05:30"

    # Fetch receipts for the day
    rct_query = (
        scoped_query("opd_receipts", clinic_id)
        .gte("created_at", start_dt)
        .lte("created_at", end_dt)
    )
    rcts_res = await sb(rct_query.limit(1000))
    receipts = rcts_res.data or []

    gross_by_mode = {
        "cash": 0,
        "upi": 0,
        "card": 0,
        "payment_link": 0,
        "prepaid_online": 0,
    }
    cashier_breakdown: dict[str, dict] = {}
    refunds_total = 0

    for r in receipts:
        amt = r.get("amount_paise", 0)
        mode = r.get("mode") or "other"
        cashier = r.get("received_by_name") or "System"

        if cashier not in cashier_breakdown:
            cashier_breakdown[cashier] = {"collections_paise": 0, "refunds_paise": 0, "count": 0}
        cashier_breakdown[cashier]["count"] += 1

        if r.get("kind") == "refund":
            refunds_total += amt
            cashier_breakdown[cashier]["refunds_paise"] += amt
        else:
            if mode in gross_by_mode:
                gross_by_mode[mode] += amt
            cashier_breakdown[cashier]["collections_paise"] += amt

    gross_total = sum(gross_by_mode.values())
    net_total = gross_total - refunds_total
    counter_total = gross_by_mode["cash"] + gross_by_mode["upi"] + gross_by_mode["card"]
    online_total = gross_by_mode["payment_link"] + gross_by_mode["prepaid_online"]

    # Invoices summary
    inv_query = (
        scoped_query("opd_invoices", clinic_id)
        .gte("created_at", start_dt)
        .lte("created_at", end_dt)
    )
    if branch_id:
        inv_query = inv_query.eq("branch_id", branch_id)
    inv_res = await sb(inv_query.limit(1000))
    invoices = inv_res.data or []

    invoices_by_status = {
        "draft": 0,
        "issued": 0,
        "partially_paid": 0,
        "paid": 0,
        "void": 0,
    }
    outstanding_dues_paise = 0
    for inv in invoices:
        st = inv.get("status")
        if st in invoices_by_status:
            invoices_by_status[st] += 1
        if st in ("issued", "partially_paid"):
            tot = inv.get("total_paise") or (inv.get("subtotal_paise", 0) - inv.get("discount_paise", 0))
            paid = inv.get("paid_paise", 0)
            outstanding_dues_paise += max(0, tot - paid)

    return {
        "date": target_date,
        "gross_paise": gross_total,
        "refunds_paise": refunds_total,
        "net_paise": net_total,
        "modes": gross_by_mode,
        "counter_paise": counter_total,
        "online_paise": online_total,
        "outstanding_dues_paise": outstanding_dues_paise,
        "cashiers": cashier_breakdown,
        "invoices_count": len(invoices),
        "invoices_by_status": invoices_by_status,
    }


async def list_invoices(
    clinic_id: str,
    date_str: Optional[str] = None,
    status_filter: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    branch_id: Optional[str] = None,
) -> list[dict]:
    """Search and list invoices with patient context."""
    query = scoped_query("opd_invoices", clinic_id).order("created_at", desc=True)
    if branch_id:
        query = query.eq("branch_id", branch_id)

    if date_str:
        start_dt = f"{date_str}T00:00:00+05:30"
        end_dt = f"{date_str}T23:59:59+05:30"
        query = query.gte("created_at", start_dt).lte("created_at", end_dt)

    if status_filter:
        query = query.eq("status", status_filter)

    res = await sb(query.limit(limit))
    rows = res.data or []

    if q:
        q_lower = q.strip().lower()
        filtered = []
        for r in rows:
            snap = r.get("patient_snapshot") or {}
            num = (r.get("invoice_number") or "").lower()
            name = (snap.get("name") or "").lower()
            phone = (snap.get("phone") or "").lower()
            mrn = (snap.get("mrn") or "").lower()
            if q_lower in num or q_lower in name or q_lower in phone or q_lower in mrn:
                filtered.append(r)
        return filtered

    return rows


async def get_invoice(clinic_id: str, invoice_id: str) -> dict:
    """Retrieve full invoice with line items, receipts, and calculated balance."""
    inv_res = await sb(
        scoped_query("opd_invoices", clinic_id)
        .eq("id", invoice_id)
        .limit(1)
    )
    if not inv_res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice = inv_res.data[0]

    # Fetch items
    items_res = await sb(
        scoped_query("opd_invoice_items", clinic_id)
        .eq("invoice_id", invoice_id)
        .order("line_no", desc=False)
    )
    invoice["items"] = items_res.data or []

    # Fetch receipts
    rcts_res = await sb(
        scoped_query("opd_receipts", clinic_id)
        .eq("invoice_id", invoice_id)
        .order("created_at", desc=False)
    )
    invoice["receipts"] = rcts_res.data or []

    total_paise = invoice.get("total_paise")
    if total_paise is None:
        total_paise = max(0, invoice.get("subtotal_paise", 0) - invoice.get("discount_paise", 0))
    invoice["total_paise"] = total_paise
    invoice["balance_due_paise"] = max(0, total_paise - invoice.get("paid_paise", 0))

    return invoice


async def settle_opd_payment_link(
    clinic_id: Optional[str] = None,
    gateway: str = "razorpay",
    payment_id: str = "",
    amount_paid: int = 0,
    payment_link_id: Optional[str] = None,
    opd_invoice_id: Optional[str] = None,
) -> dict:
    """Settle an online payment link payment into opd_receipts (webhook branch).

    Guarantees idempotency and handles amount/void mismatches gracefully.
    """
    # Look up invoice
    target_inv = None
    if opd_invoice_id:
        # unscoped: meta_callback_by_unique_id
        res = await sb(supabase.table("opd_invoices").select("*").eq("id", opd_invoice_id).limit(1))
        if res.data:
            target_inv = res.data[0]

    if not target_inv and payment_link_id:
        # unscoped: meta_callback_by_unique_id
        res = await sb(supabase.table("opd_invoices").select("*").eq("payment_link_id", payment_link_id).limit(1))
        if res.data:
            target_inv = res.data[0]

    if not target_inv:
        logger.warning(f"OPD settlement: invoice not found for link_id={payment_link_id}, inv_id={opd_invoice_id}")
        return {"status": "ignored", "code": 200, "reason": "invoice_not_found"}

    inv_clinic_id = str(target_inv["clinic_id"])
    if clinic_id and clinic_id != inv_clinic_id:
        logger.warning(f"OPD settlement: clinic mismatch. Event={clinic_id}, invoice={inv_clinic_id}")
        return {"status": "ignored", "code": 200, "reason": "clinic_mismatch"}

    if not payment_id or amount_paid <= 0:
        logger.error(f"OPD settlement: missing payment id or amount (id={payment_id!r}, amount={amount_paid})")
        return {"status": "ignored", "code": 200, "reason": "missing_fields"}

    # Idempotency: a gateway payment yields at most one receipt and one exception.
    rct_check = await sb(
        scoped_query("opd_receipts", inv_clinic_id)
        .eq("gateway", gateway)
        .eq("gateway_payment_id", payment_id)
        .limit(1)
    )
    exc_check = await sb(
        scoped_query("opd_payment_exceptions", inv_clinic_id)
        .eq("gateway", gateway)
        .eq("gateway_payment_id", payment_id)
        .limit(1)
    )
    if exc_check.data:
        logger.info(f"OPD settlement: payment {payment_id} already settled (exception recorded).")
        return {"status": "ok", "code": 200, "reason": "already_settled"}
    if rct_check.data:
        applied = int(rct_check.data[0].get("amount_paise") or 0)
        if applied >= amount_paid:
            logger.info(f"OPD settlement: payment {payment_id} already settled.")
            return {"status": "ok", "code": 200, "reason": "already_settled"}
        # Receipt written but the excess row was not (process died between the two writes).
        await _record_payment_exception(target_inv, gateway, payment_id, payment_link_id, amount_paid, applied, "overpaid")
        return {"status": "ok", "code": 200, "reason": "overpaid", "applied_paise": applied}

    applied = 0
    if target_inv.get("status") in ("issued", "partially_paid"):
        for attempt in range(2):
            total_paise = target_inv.get("total_paise") or (
                target_inv.get("subtotal_paise", 0) - target_inv.get("discount_paise", 0))
            applied = max(0, min(amount_paid, total_paise - (target_inv.get("paid_paise") or 0)))
            if applied == 0:
                break
            try:
                # unscoped: insert_scoped_by_payload
                await sb(supabase.table("opd_receipts").insert({
                    "clinic_id": inv_clinic_id,
                    "invoice_id": str(target_inv["id"]),
                    "shift_id": None,
                    "kind": "payment",
                    "mode": "payment_link",
                    "amount_paise": applied,
                    "reference": payment_id,
                    "gateway": gateway,
                    "gateway_payment_id": payment_id,
                    "received_by_admin_id": None,
                    "received_by_name": "online",
                }))
                break
            except Exception as ins_err:
                if _is_unique_violation(ins_err):
                    return {"status": "ok", "code": 200, "reason": "already_settled"}
                # A counter receipt (or void) landed between our read and insert and the
                # DB refused the over-payment. Re-read once; a second refusal parks it all.
                logger.warning(f"OPD settlement: receipt insert refused ({ins_err}); re-reading invoice")
                applied = 0
                if attempt == 1:
                    break
                res = await sb(scoped_query("opd_invoices", inv_clinic_id).eq("id", str(target_inv["id"])).limit(1))
                if not res.data:
                    break
                target_inv = res.data[0]
                if target_inv.get("status") not in ("issued", "partially_paid"):
                    break

    if applied >= amount_paid:
        logger.info(f"OPD settlement complete for invoice {target_inv['id']}, amount={amount_paid}")
        return {"status": "ok", "code": 200, "opd_settled": True}

    if target_inv.get("status") == "void":
        reason = "invoice_void"
    elif applied > 0:
        reason = "overpaid"
    else:
        reason = "invoice_settled"
    await _record_payment_exception(target_inv, gateway, payment_id, payment_link_id, amount_paid, applied, reason)
    return {"status": "ok", "code": 200, "reason": reason, "applied_paise": applied}


def _is_unique_violation(err: Exception) -> bool:
    text = str(err)
    return "23505" in text or "duplicate key" in text


_EXCEPTION_REASON_TEXT = {
    "overpaid": "paid more than the balance due",
    "invoice_settled": "invoice was already fully paid",
    "invoice_void": "invoice had been voided",
}


async def _record_payment_exception(
    invoice: dict,
    gateway: str,
    payment_id: str,
    order_id: Optional[str],
    paid_paise: int,
    applied_paise: int,
    reason: str,
) -> None:
    """Park the part of an online payment that could not be applied, then alert.

    A replay (unique index) is a no-op; any other failure propagates so the
    gateway retries the webhook instead of the money going unrecorded.
    """
    clinic_id = str(invoice["clinic_id"])
    try:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("opd_payment_exceptions").insert({
            "clinic_id": clinic_id,
            "invoice_id": str(invoice["id"]),
            "gateway": gateway,
            "gateway_payment_id": payment_id,
            "gateway_order_id": (order_id or None) and str(order_id)[:100],
            "reason": reason,
            "paid_paise": paid_paise,
            "applied_paise": applied_paise,
        }))
    except Exception as err:
        if _is_unique_violation(err):
            return
        logger.error(f"OPD settlement: failed to record payment exception for {payment_id}: {err}")
        raise

    excess = paid_paise - applied_paise
    logger.warning(
        f"OPD settlement: {reason} — invoice {invoice['id']} paid={paid_paise} applied={applied_paise} excess={excess}"
    )
    try:
        from connectors.runner import send_admin_alert
        await send_admin_alert(
            clinic_id,
            "Online OPD payment needs a refund\n\n"
            f"Invoice: {invoice.get('invoice_number') or invoice['id']}\n"
            f"Paid online: {_format_money_inr(paid_paise)}; applied to invoice: {_format_money_inr(applied_paise)}\n"
            f"Extra: {_format_money_inr(excess)} ({_EXCEPTION_REASON_TEXT.get(reason, reason)})\n"
            f"Payment ID: {payment_id}\n"
            "Refund it from Billing → Online payment exceptions.",
        )
    except Exception as alert_err:
        logger.error(f"Failed to alert on OPD payment exception: {alert_err}")


async def list_payment_exceptions(clinic_id: str, status: str = "open", limit: int = 50) -> list[dict]:
    """Online payments not applied in full, newest first, with invoice + patient labels."""
    q = scoped_query("opd_payment_exceptions", clinic_id).order("created_at", desc=True).limit(limit)
    if status != "all":
        q = q.eq("status", status)
    rows = (await sb(q)).data or []
    inv_ids = list({str(r["invoice_id"]) for r in rows})
    invoices: dict = {}
    if inv_ids:
        inv_res = await sb(
            scoped_query("opd_invoices", clinic_id, "id,invoice_number,patient_snapshot,status").in_("id", inv_ids)
        )
        invoices = {str(i["id"]): i for i in (inv_res.data or [])}
    for r in rows:
        inv = invoices.get(str(r["invoice_id"])) or {}
        r["invoice_number"] = inv.get("invoice_number")
        r["invoice_status"] = inv.get("status")
        r["patient_name"] = (inv.get("patient_snapshot") or {}).get("name")
        if r.get("excess_paise") is None:
            r["excess_paise"] = (r.get("paid_paise") or 0) - (r.get("applied_paise") or 0)
    return rows


async def resolve_payment_exception(
    clinic_id: str,
    exception_id: str,
    action: str,
    note: Optional[str] = None,
    reference: Optional[str] = None,
    actor: Any = None,
) -> dict:
    """Close an open exception: refund the excess through its gateway, or record an offline settlement."""
    if not _is_opd_admin(actor):
        raise HTTPException(status_code=403, detail="Resolving payment exceptions requires OPD_ADMIN permissions.")
    if action not in ("refund_gateway", "settled_offline"):
        raise HTTPException(status_code=422, detail="Unknown action.")

    res = await sb(scoped_query("opd_payment_exceptions", clinic_id).eq("id", exception_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Payment exception not found.")
    exc = res.data[0]
    if exc.get("status") != "open":
        raise HTTPException(status_code=409, detail="This payment has already been resolved.")
    excess = int(exc.get("excess_paise") or (exc["paid_paise"] - exc["applied_paise"]))

    if action == "settled_offline":
        note = (note or "").strip()
        if len(note) < 5:
            raise HTTPException(status_code=422, detail="Describe how it was settled (at least 5 characters).")
        updates = {"status": "settled_offline", "resolution_note": note[:300],
                   "resolution_reference": (reference or "").strip()[:100] or None}
    else:
        from app.services.payment import get_razorpay_creds, payment_service
        clinic = await get_clinic_by_id(clinic_id) or {}
        # Deterministic per exception: a double click or retry reuses the same gateway refund.
        idem = f"opdx-{exception_id}"
        try:
            if exc["gateway"] == "phonepe":
                if not exc.get("gateway_order_id"):
                    raise HTTPException(status_code=409, detail="PhonePe order id missing; refund from the PhonePe dashboard and mark it settled.")
                data = await payment_service._create_phonepe_refund(
                    clinic, merchant_order_id=exc["gateway_order_id"], amount_paise=excess, idempotency_key=idem,
                )
            else:
                key_id, key_secret, _ = get_razorpay_creds(clinic)
                if not key_id or not key_secret:
                    raise HTTPException(status_code=409, detail="Razorpay is not configured for this clinic; refund from the dashboard and mark it settled.")
                data = await payment_service._create_razorpay_refund(
                    exc["gateway_payment_id"], excess, f"OPD excess payment refund ({exc.get('reason')})", idem,
                    key_id=key_id, key_secret=key_secret,
                )
        except HTTPException:
            raise
        except Exception as err:
            logger.error(f"OPD exception refund failed for {exception_id}: {err}")
            raise HTTPException(status_code=502, detail="The payment gateway refused the refund. Nothing was changed; try again or refund from the gateway dashboard.")
        refund_id = str((data or {}).get("id") or (data or {}).get("refundId") or idem)
        updates = {"status": "refunded", "resolution_reference": refund_id[:100],
                   "resolution_note": (note or "").strip()[:300] or None}

    updates.update({
        "resolved_by_admin_id": getattr(actor, "user_id", None),
        "resolved_by_name": getattr(actor, "username", "Admin"),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    })
    # CAS on status=open: two admins resolving at once cannot both write.
    upd = await sb(
        supabase.table("opd_payment_exceptions")
        .update(updates)
        .eq("clinic_id", clinic_id)
        .eq("id", exception_id)
        .eq("status", "open")
    )
    if not upd.data:
        raise HTTPException(status_code=409, detail="This payment was resolved by someone else just now.")
    return upd.data[0]


async def _validate_and_price_items(
    clinic_id: str,
    items: list[dict],
    actor: Any = None,
) -> tuple[list[dict], int]:
    """Validate item types, apply server-side pricing rules, and compute subtotal."""
    if not items:
        raise HTTPException(status_code=422, detail="At least one line item is required.")

    is_admin = _is_opd_admin(actor)
    validated = []
    subtotal = 0

    clinic = await get_clinic_by_id(clinic_id)
    opd_settings = (clinic or {}).get("opd_settings") or {}
    service_catalog = {
        c.get("code"): c.get("price_paise", 0)
        for c in (opd_settings.get("service_catalog") or [])
        if c.get("active", True)
    }

    for idx, it in enumerate(items, start=1):
        item_type = it.get("item_type")
        if item_type not in ("consultation", "nursing", "diagnostic", "procedure", "other"):
            raise HTTPException(status_code=422, detail=f"Line {idx}: Invalid item type '{item_type}'.")

        desc = (it.get("description") or "").strip()
        if not desc:
            raise HTTPException(status_code=422, detail=f"Line {idx}: Description is required.")

        qty = it.get("quantity", 1)
        if qty < 1 or qty > 999:
            raise HTTPException(status_code=422, detail=f"Line {idx}: Quantity must be between 1 and 999.")

        client_unit_price = it.get("unit_price_paise", 0)
        item_discount = it.get("discount_paise", 0)

        # Server-side pricing enforcement
        unit_price = client_unit_price
        code = it.get("catalog_code")
        if code and code in service_catalog and not is_admin:
            unit_price = service_catalog[code]
        elif item_type == "consultation":
            doc_id = it.get("doctor_id")
            if doc_id:
                doc_res = await sb(
                    scoped_query("doctors", clinic_id)
                    .eq("id", doc_id)
                    .limit(1)
                )
                if doc_res.data:
                    fee = doc_res.data[0].get("consultation_fee")
                    doc_fee_paise = int(fee * 100) if fee is not None else 0
                    if not is_admin:
                        # Non-admins cannot alter doctor consultation fee
                        unit_price = doc_fee_paise
                elif not is_admin:
                    # An unknown/other-clinic doctor must not let the client price stand.
                    raise HTTPException(status_code=422, detail=f"Line {idx}: Doctor not found.")

        elif item_type == "diagnostic":
            if code and code in service_catalog:
                unit_price = service_catalog[code]
            else:
                test_id = it.get("lab_test_id")
                if test_id:
                    test_res = await sb(
                        scoped_query("lab_tests", clinic_id)
                        .eq("id", test_id)
                        .limit(1)
                    )
                    if test_res.data:
                        t_row = test_res.data[0]
                        price_val = t_row.get("price_paise")
                        if price_val is None and "price" in t_row and t_row["price"] is not None:
                            price_val = int(t_row["price"] * 100)
                        if price_val is not None and not is_admin:
                            unit_price = price_val

        if unit_price < 0 or unit_price > 100_000_000:
            raise HTTPException(status_code=422, detail=f"Line {idx}: Unit price exceeds permitted bounds.")

        line_gross = qty * unit_price
        if item_discount > line_gross:
            raise HTTPException(status_code=422, detail=f"Line {idx}: Line discount cannot exceed line total.")

        line_total = line_gross - item_discount
        subtotal += line_total

        validated.append({
            "item_type": item_type,
            "catalog_code": it.get("catalog_code"),
            "doctor_id": it.get("doctor_id"),
            "lab_test_id": it.get("lab_test_id"),
            "description": desc,
            "quantity": qty,
            "unit_price_paise": unit_price,
            "discount_paise": item_discount,
            "line_total_paise": line_total,
        })

    return validated, subtotal


def _is_opd_admin(actor: Any) -> bool:
    """Check if the actor holds administrative billing privileges."""
    if not actor:
        return False
    role = getattr(actor, "role", None)
    if role in ("super_admin", "clinic_admin", "admin"):
        return True
    perms = getattr(actor, "permissions", []) or []
    return "OPD_ADMIN" in perms


# ═══════════════════════════════════════════════════════════════════════════════
# DOCTOR CONSULTATION EARNINGS
# ═══════════════════════════════════════════════════════════════════════════════

_EARNINGS_STATUSES = ("issued", "partially_paid", "paid")
_EARNINGS_MAX_DAYS = 92
_EARNINGS_MAX_INVOICES = 20000


def _ratio(amount: int, part: int, whole: int) -> int:
    """amount * part / whole, rounded half up, in exact integers."""
    if whole <= 0 or part <= 0 or amount <= 0:
        return 0
    return (amount * part * 2 + whole) // (whole * 2)


def allocate_consultation(consult_paise: int, subtotal_paise: int, discount_paise: int, paid_paise: int) -> dict:
    """A doctor's share of one invoice.

    Receipts are recorded per invoice, not per line, so money is allocated to
    the doctor's consultation lines in proportion to their value: the
    invoice-level discount the same way, then what was paid (refunds already
    netted out by the receipt trigger) over what was due. On a fully paid
    invoice the share collected equals the fee net of its discount share;
    on a consultation-only invoice every figure is exact.
    """
    consult = max(0, int(consult_paise or 0))
    subtotal = max(0, int(subtotal_paise or 0))
    discount = max(0, int(discount_paise or 0))
    total = max(0, subtotal - discount)
    paid = min(max(0, int(paid_paise or 0)), total)
    net = consult - _ratio(discount, consult, subtotal)
    collected = net if paid == total else _ratio(paid, net, total)
    return {"fee_paise": net, "collected_paise": collected, "outstanding_paise": net - collected}


async def get_doctor_earnings(
    clinic_id: str,
    doctor_id: str,
    from_date: date,
    to_date: date,
    branch_id: Optional[str] = None,
) -> dict:
    """Consultation fees billed to one doctor on invoices issued in [from, to]
    (IST days), and how much of each has been collected. Drafts are not billed
    and void invoices are excluded."""
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'.")
    if (to_date - from_date).days + 1 > _EARNINGS_MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"Choose a range of at most {_EARNINGS_MAX_DAYS} days.")

    doc_res = await sb(
        scoped_query("doctors", clinic_id).select("id, name, department").eq("id", doctor_id).limit(1)
    )
    if not doc_res.data:
        raise HTTPException(status_code=404, detail="Doctor not found")
    doctor = doc_res.data[0]

    start_dt = f"{from_date.isoformat()}T00:00:00+05:30"
    end_dt = f"{to_date.isoformat()}T23:59:59.999999+05:30"
    page = 1000
    invoices: list[dict] = []
    offset = 0
    while True:
        q = (
            scoped_query("opd_invoices", clinic_id)
            .select("id, invoice_number, status, issued_at, subtotal_paise, discount_paise, "
                    "paid_paise, patient_snapshot, branch_id")
            .in_("status", list(_EARNINGS_STATUSES))
            .gte("issued_at", start_dt)
            .lte("issued_at", end_dt)
        )
        if branch_id:
            q = q.eq("branch_id", branch_id)
        batch = (await sb(q.order("issued_at", desc=True).order("id").range(offset, offset + page - 1))).data or []
        invoices.extend(batch)
        if len(invoices) > _EARNINGS_MAX_INVOICES:
            raise HTTPException(status_code=422, detail="Too many invoices in this range. Please narrow the dates.")
        if len(batch) < page:
            break
        offset += page

    consult_by_invoice: dict[str, int] = {}
    ids = [str(i["id"]) for i in invoices]
    for k in range(0, len(ids), 150):
        items = (await sb(
            scoped_query("opd_invoice_items", clinic_id)
            .select("invoice_id, line_total_paise")
            .eq("doctor_id", doctor_id)
            .eq("item_type", "consultation")
            .in_("invoice_id", ids[k:k + 150])
        )).data or []
        for it in items:
            iid = str(it["invoice_id"])
            consult_by_invoice[iid] = consult_by_invoice.get(iid, 0) + int(it.get("line_total_paise") or 0)

    rows = []
    totals = {"visits": 0, "fee_paise": 0, "collected_paise": 0, "outstanding_paise": 0}
    for inv in invoices:
        consult = consult_by_invoice.get(str(inv["id"]))
        if consult is None:
            continue
        share = allocate_consultation(consult, inv.get("subtotal_paise"), inv.get("discount_paise"), inv.get("paid_paise"))
        rows.append({
            "invoice_id": str(inv["id"]),
            "invoice_number": inv.get("invoice_number"),
            "issued_at": inv.get("issued_at"),
            "status": inv.get("status"),
            "patient_name": (inv.get("patient_snapshot") or {}).get("name") or "Patient",
            "shared_invoice": int(inv.get("subtotal_paise") or 0) != consult,
            **share,
        })
        totals["visits"] += 1
        for key in ("fee_paise", "collected_paise", "outstanding_paise"):
            totals[key] += share[key]

    return {
        "doctor": {"id": str(doctor["id"]), "name": doctor.get("name"), "department": doctor.get("department")},
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "totals": totals,
        "rows": rows,
    }


if __name__ == "__main__":
    a = allocate_consultation
    assert a(50000, 50000, 0, 50000) == {"fee_paise": 50000, "collected_paise": 50000, "outstanding_paise": 0}
    assert a(50000, 50000, 0, 0)["outstanding_paise"] == 50000
    assert a(50000, 50000, 10000, 40000) == {"fee_paise": 40000, "collected_paise": 40000, "outstanding_paise": 0}
    assert a(50000, 50000, 0, 20000)["collected_paise"] == 20000
    # 500 consult + 500 lab, 100 discount, fully paid: 450 to the doctor.
    assert a(50000, 100000, 10000, 90000) == {"fee_paise": 45000, "collected_paise": 45000, "outstanding_paise": 0}
    # same, half paid
    assert a(50000, 100000, 10000, 45000)["collected_paise"] == 22500
    # odd split: 333 of 1000, 1 paisa discount; shares never exceed the fee
    s = a(333, 1000, 1, 500)
    assert 0 <= s["collected_paise"] <= s["fee_paise"] <= 333
    assert a(0, 0, 0, 0) == {"fee_paise": 0, "collected_paise": 0, "outstanding_paise": 0}
    print("ok")
