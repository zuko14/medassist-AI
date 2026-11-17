"""Kriya Pharmacy OS — foundation service (migration 134, RX-M1).

Settings (validated, CAS-versioned), stores, provisioning, the setup checklist
and the owner's deactivation preview.
Design: docs/pharmacy-os/DATA-MODEL.md §1, ADR-RX-03 / ADR-RX-09.

Zero-LLM: imports nothing from the AI, voice or assistant packages
(tests/test_rx_no_llm.py).
"""

import logging
import re
from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.database import sb, scoped_query, supabase

logger = logging.getLogger(__name__)

STORE_TYPES = ("central", "retail_counter", "ward_substore", "ot_tray", "emergency_tray")
STORE_CODE = r"^[A-Z0-9_-]{2,20}$"


class RxSettings(BaseModel):
    """clinics.rx_settings (DATA-MODEL §1). Defaults apply to any missing key."""

    model_config = ConfigDict(extra="forbid")

    max_discount_bps: int = Field(1000, ge=0, le=10000)          # counter discount ceiling without RX_ADMIN
    near_expiry_days: int = Field(90, ge=7, le=365)
    min_shelf_days_at_sale: int = Field(30, ge=0, le=365)
    ipd_autopost: bool = True
    ipd_price_basis: Literal["mrp", "cost_plus"] = "mrp"
    ipd_cost_plus_bps: int = Field(0, ge=0, le=10000)
    allow_loose_sale: bool = True
    round_off_to_rupee: bool = True
    refill_enabled: bool = False
    refill_lead_days: int = Field(5, ge=1, le=30)
    receipt_format: Literal["80mm", "a5"] = "80mm"
    fefo_override_requires_reason: bool = True
    rx_validity_days: int = Field(30, ge=1, le=365)
    return_days: int = Field(7, ge=0, le=90)
    audit_approval_paise: int = Field(500000, ge=0, le=100_000_000)
    version: int = Field(0, ge=0)


DEFAULT_SETTINGS = RxSettings().model_dump()

# rx_* codes raised by migration 134 and the constraint names behind user mistakes.
# Matched longest-first against the database message; the raw text is never returned.
_ERRORS = {
    "rx_settings_conflict": (409, "These settings were changed by someone else. Reload and try again."),
    "rx_settings_invalid": (422, "Settings must be an object."),
    "rx_clinic_not_found": (404, "Clinic not found."),
    "rx_store_delete_refused": (409, "Stores are never deleted. Deactivate the store instead."),
    "rx_store_field_locked": (409, "A store's code and type never change. Create a new store instead."),
    # 135 catalogue
    "rx_item_not_found": (404, "Item not found."),
    "rx_item_delete_refused": (409, "Items are never deleted. Deactivate the item instead."),
    "rx_item_field_locked": (409, "This field cannot change (item code, base unit, a schedule downgrade, or the "
                                  "generic / pack size once stock exists). Create a new item instead."),
    "rx_item_reason_required": (422, "Changing the GST rate or HSN code needs a reason (e.g. the notification)."),
    "rx_barcode_taken": (409, "This barcode is already on another item."),
    "rx_append_only": (409, "History rows are never edited or deleted."),
    # 136 stock ledger
    "rx_insufficient_stock": (409, "Not enough stock in this batch at this store."),
    "rx_batch_expired": (409, "This batch has expired and cannot be sold, dispensed or used as opening stock."),
    "rx_batch_blocked": (409, "This batch is blocked (recall / quarantine)."),
    "rx_batch_not_found": (404, "Batch not found."),
    "rx_batch_field_locked": (409, "A batch's number, expiry, MRP and cost never change."),
    "rx_batch_delete_refused": (409, "Batches are never deleted."),
    "rx_qty_invalid": (422, "Quantity must be a positive whole number of units."),
    "rx_writeoff_kind_invalid": (422, "A write-off is either expiry or damage."),
    "rx_opening_closed": (409, "This store has already traded: opening stock can no longer be loaded. "
                               "Use a stock audit instead."),
    "rx_store_not_found": (404, "Store not found."),
    "rx_movement_via_function_only": (500, "The pharmacy operation failed. Nothing was changed; please retry."),
    "rx_balance_via_ledger_only": (500, "The pharmacy operation failed. Nothing was changed; please retry."),
    # 137 purchase
    "rx_document_locked": (409, "This document is no longer a draft and cannot be changed."),
    "rx_po_not_found": (404, "Purchase order not found."),
    "rx_po_empty": (422, "Add at least one line before sending the purchase order."),
    "rx_grn_not_found": (404, "Goods receipt not found."),
    "rx_grn_empty": (422, "Add at least one line before posting the goods receipt."),
    "rx_grn_total_mismatch": (409, "The computed total does not match the supplier's invoice total (difference "
                                   "above ₹0.99). Check the lines, freight and invoice total."),
    "rx_grn_expired_batch": (422, "This batch has already expired and cannot be received."),
    "rx_grn_po_line_mismatch": (422, "This line does not belong to the purchase order of this receipt."),
    "rx_grn_stock_consumed": (409, "Some of this receipt's stock has already been used. Return it to the supplier "
                                   "instead of voiding the receipt."),
    "rx_store_cannot_receive": (409, "This store does not receive goods from suppliers."),
    "rx_reason_required": (422, "A reason is required (3+ characters)."),
    "rx_return_not_found": (404, "Supplier return not found."),
    "rx_return_empty": (422, "Add at least one batch before dispatching the return."),
    # 138 counter sales
    "rx_sale_not_found": (404, "Sale not found."),
    "rx_sale_item_not_found": (404, "Bill line not found."),
    "rx_sale_empty": (422, "Add at least one medicine before completing the bill."),
    "rx_store_cannot_sell": (409, "This store does not sell."),
    "rx_prescription_superseded": (409, "The doctor changed this prescription. Reload it before billing."),
    "rx_discount_over_limit": (403, "This discount is above the counter limit. Ask a manager to approve it."),
    "rx_fulfilment_exceeds_rx": (409, "This is more than the prescription allows (already dispensed elsewhere?)."),
    "rx_schedule_needs_rx": (422, "A scheduled medicine needs a prescription with the prescriber's registration number."),
    "rx_schedule_needs_patient": (422, "A scheduled medicine needs the patient's name."),
    "rx_controlled_permission": (403, "Selling Schedule X / narcotic medicines needs the RX_CONTROLLED permission."),
    "rx_controlled_requires_inhouse_rx": (422, "Schedule X / narcotic medicines are never sold over the counter: "
                                               "link the clinic's prescription or capture the outside one."),
    "rx_external_rx_reused": (409, "This outside prescription has already been used for a controlled medicine."),
    "rx_payment_mismatch": (422, "The payments do not add up to the bill total."),
    "rx_payment_mode_invalid": (422, "Payment mode must be cash, UPI, card or wallet."),
    "rx_payment_invalid": (422, "Each payment must be a positive amount."),
    "rx_shift_not_open": (409, "No open cashier shift for cash. Open your shift first."),
    "rx_whole_packs_only": (422, "This medicine is sold in whole packs only."),
    "rx_return_exceeds_sold": (409, "You cannot return more than was sold (and not already returned)."),
    "rx_return_window_closed": (409, "The return window for this bill has closed. A manager can approve it."),
    "rx_restock_needs_admin": (403, "Controlled or Schedule H1 returns go back to stock only with a manager."),
    "rx_refund_exceeds_tender": (409, "Refund is more than was paid in this mode. Use the original mode or a credit note."),
    "rx_void_not_same_day": (409, "Only today's bills can be voided. Use a return instead."),
    "rx_void_has_returns": (409, "This bill has returns; it can no longer be voided."),
    "rx_write_via_function_only": (500, "The pharmacy operation failed. Nothing was changed; please retry."),
    # 139 inpatient pharmacy
    "rx_admission_not_found": (404, "Admission not found."),
    "rx_admission_not_active": (409, "This patient is no longer admitted."),
    "rx_order_not_found": (404, "This medication order is not on this patient's chart."),
    "rx_order_not_medication": (422, "Only medication orders can be indented."),
    "rx_order_not_active": (409, "The doctor has stopped this order. Close the line instead of dispensing it."),
    "rx_indent_not_found": (404, "Indent not found."),
    "rx_indent_not_open": (409, "This indent is no longer open."),
    "rx_indent_empty": (422, "Add at least one line."),
    "rx_indent_line_not_found": (404, "Indent line not found."),
    "rx_indent_line_closed": (409, "This indent line is already settled."),
    "rx_substitute_not_same_generic": (422, "A substitute must be the same generic (molecule, strength and form)."),
    "rx_substitution_reason_required": (422, "A substitution needs a reason."),
    "rx_dispense_not_found": (404, "Dispense not found."),
    "rx_dispense_line_not_found": (404, "Dispensed line not found."),
    "rx_return_exceeds_dispensed": (409, "You cannot return more than was dispensed (and not already returned)."),
    "rx_mar_not_found": (404, "Dose record not found."),
    "rx_mar_line_mismatch": (409, "This batch was dispensed for another order or patient."),
    "rx_mar_already_linked": (409, "This dose is already linked to another batch."),
    "ipd_charge_billed": (409, "This charge is on a final bill. Void and regenerate the bill, or issue it first."),
    "opd_credit_note_invoice_state": (409, "The final bill is still a draft. Issue it, or void and regenerate it, "
                                           "before returning billed medicines."),
    "opd_credit_note_refund_required": (409, "The patient has paid more than the bill after this return. Record the "
                                             "refund from IPD billing."),
    # 140 transfers and audits
    "rx_transfer_not_found": (404, "Transfer not found."),
    "rx_transfer_same_store": (422, "A transfer goes from one store to a different store."),
    "rx_transfer_empty": (422, "Add at least one line (at most 200)."),
    "rx_transfer_not_dispatched": (409, "Only a dispatched transfer can be received."),
    "rx_transfer_lines_incomplete": (422, "Enter the received quantity for every line, once each."),
    "rx_received_out_of_range": (422, "Received units must be between 0 and the units sent."),
    "rx_discrepancy_reason_required": (422, "A short receipt needs a reason (3+ characters)."),
    "rx_controlled_two_person": (409, "A controlled transfer must be received by someone other than the dispatcher."),
    "rx_store_frozen_for_audit": (409, "This store is frozen for a stock audit: nothing can be sold or dispensed "
                                       "from it until the audit is posted or cancelled."),
    "rx_audit_not_found": (404, "Audit not found."),
    "rx_audit_already_open": (409, "This store already has an audit in progress."),
    "rx_audit_not_counting": (409, "Counting has finished for this audit."),
    "rx_audit_not_in_review": (409, "Move the audit to review before posting it."),
    "rx_audit_uncounted": (409, "Some lines are not counted yet. Enter 0 for an empty shelf."),
    "rx_audit_out_of_scope": (422, "This batch's item is outside the audit's scope."),
    "rx_audit_line_not_found": (404, "Audit line not found."),
    "rx_variance_reason_required": (422, "Every line with a variance needs a reason."),
    "rx_audit_needs_approver": (403, "This variance is above the approval limit: a second pharmacy manager must "
                                     "approve it."),
}
# constraints whose clients branch on a stable code (API.md §4)
_CONSTRAINT_CODES = {"rx_grns_invoice_key": "rx_grn_duplicate_invoice"}
_CONSTRAINTS = (
    ("rx_stores_code_key", 409, "Another store already uses this code."),
    ("uq_rx_stores_one_central", 409, "This branch already has an active central store."),
    ("rx_stores_branch_fk", 404, "Branch not found."),
    ("rx_stores_parent_fk", 404, "Parent store not found."),
    ("rx_transfers_distinct_stores", 422, "A transfer goes from one store to a different store."),
    ("rx_transfers_controlled_two_person", 409, "A controlled transfer must be received by someone other than the "
                                                "dispatcher."),
    ("rx_transfer_lines_batch_key", 422, "Each batch appears once per transfer."),
    ("uq_rx_stock_audits_open", 409, "This store already has an audit in progress."),
    ("rx_stock_audits_scope_shape", 422, "A rack audit names its rack; an item-list audit names its items."),
    ("rx_par_levels_item_fk", 404, "Item not found."),
    ("rx_stores_ward_shape", 422, "A ward sub-store needs its ward; other stores have none."),
    ("rx_stores_sell_shape", 422, "Only a retail counter can sell."),
    ("rx_stores_no_self_parent", 422, "A store cannot be its own parent."),
    ("rx_stores_code_check", 422, "A store code is 2–20 capital letters, digits, _ or -."),
    ("rx_stores_name_check", 422, "A store name is 2–100 characters."),
    ("rx_items_code_key", 409, "Another item already uses this item code."),
    ("rx_item_barcodes_barcode_key", 409, "This barcode is already on another item."),
    ("uq_rx_manufacturers_name", 409, "This manufacturer already exists."),
    ("rx_generics_class_key", 409, "This generic (name + strength + form) already exists."),
    ("rx_items_generic_fk", 404, "Generic not found."),
    ("rx_items_mfr_fk", 404, "Manufacturer not found."),
    ("rx_item_barcodes_item_fk", 404, "Item not found."),
    ("rx_items_drug_has_generic", 422, "A drug needs its generic (name, strength, form)."),
    ("rx_items_controlled_no_loose", 422, "Schedule X and narcotic items must allow per-unit dispensing."),
    ("rx_sm_reason_for_adjust", 422, "A write-off or adjustment needs a reason (3+ characters)."),
    ("rx_batches_block_reason", 422, "Blocking a batch needs a reason (3+ characters)."),
    ("rx_batches_batch_no_check", 422, "A batch number is 1–30 letters, digits or / . _ -."),
    ("rx_batches_mrp_pack_paise_check", 422, "MRP must be between ₹0.01 and ₹10,00,000 per pack."),
    ("rx_grns_invoice_key", 409, "This supplier invoice has already been entered."),
    ("uq_rx_suppliers_gstin", 409, "Another supplier already has this GSTIN."),
    ("rx_suppliers_state_matches_gstin", 422, "The supplier's state must match the first two digits of its GSTIN."),
    ("rx_po_cancel_reason", 422, "A cancellation needs a reason (3+ characters)."),
    ("rx_srn_lines_batch_key", 422, "Each batch appears once per return."),
    ("rx_srn_credit_after_dispatch", 409, "Record the supplier's credit after the return is dispatched."),
    ("rx_grn_lines_dates", 422, "The manufacture date must be before the expiry date."),
    ("rx_po_store_fk", 404, "Store not found."), ("rx_grns_store_fk", 404, "Store not found."),
    ("rx_po_supplier_fk", 404, "Supplier not found."), ("rx_grns_supplier_fk", 404, "Supplier not found."),
    ("rx_srn_supplier_fk", 404, "Supplier not found."), ("rx_grn_lines_item_fk", 404, "Item not found."),
    ("rx_po_lines_item_fk", 404, "Item not found."), ("rx_srn_lines_batch_fk", 404, "Batch not found."),
    ("uq_rx_sales_idem", 409, "This bill was already completed with another request."),
    ("rx_sales_has_person", 422, "Enter the patient's name."),
    ("rx_sale_items_le_mrp", 422, "A line cannot be priced above MRP."),
    ("rx_sale_items_returned_le_qty", 409, "You cannot return more than was sold."),
    ("rx_sales_giveback_le_net", 409, "Refunds cannot exceed what was paid for this bill."),
    ("rx_sale_payments_cash_needs_shift", 409, "Cash needs an open cashier shift."),
    ("rx_indent_lines_dispensed_le_requested", 409, "That is more than the nurse asked for on this line."),
    ("rx_dispense_lines_returned_le_qty", 409, "You cannot return more than was dispensed."),
    ("uq_rx_ipd_indents_idem", 409, "This indent was already raised with another request."),
    ("uq_rx_dispenses_idem", 409, "This dispense was already recorded with another request."),
    ("rx_ipd_indents_store_fk", 404, "Store not found."),
    ("rx_indent_lines_item_fk", 404, "Item not found."),
)


def explain(msg: str) -> Optional[tuple[int, Optional[dict], str]]:
    """(status, {code, reason} or None, message) for a known rx_* code / constraint in a DB message."""
    for code in sorted(_ERRORS, key=len, reverse=True):
        if code in msg:
            status, message = _ERRORS[code]
            m = re.search(re.escape(code) + r":(-?[A-Za-z0-9_]+)", msg)
            return status, {"code": code, "reason": m.group(1) if m else None}, message
    for constraint, status, message in _CONSTRAINTS:
        if constraint in msg:
            code = _CONSTRAINT_CODES.get(constraint)
            return status, {"code": code, "reason": None} if code else None, message
    return None


def raise_for(exc: Exception) -> None:
    """Map an rx_* database error; anything else is a fixed 500 (never raw DB text)."""
    if isinstance(exc, HTTPException):
        raise exc
    msg = str(getattr(exc, "message", None) or exc)
    known = explain(msg)
    if known and known[0] < 500:
        status, code, message = known
        raise HTTPException(status_code=status, detail={**code, "message": message} if code else message)
    if known:   # a ledger wall reached from Python means a code path bypassed the rx_* functions
        logger.critical(f"RX_LEDGER_BYPASS code={known[1] and known[1]['code']}")
    logger.error(f"RX_DB_ERROR type={type(exc).__name__}")
    raise HTTPException(status_code=500, detail="The pharmacy operation failed. Nothing was changed; please retry.")


async def _rows(query) -> list[dict]:
    res = await sb(query)
    return list(res.data or [])


# ── settings ────────────────────────────────────────────────────────────────

def get_rx_settings(clinic: dict) -> dict:
    return {**DEFAULT_SETTINGS, **(clinic.get("rx_settings") or {})}


def validate_settings(current: dict, changes: dict) -> dict:
    """Merge `changes` over the stored settings and validate the result (422 on error)."""
    merged = {**get_rx_settings({"rx_settings": current}), **changes}
    try:
        return RxSettings(**merged).model_dump()
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=[{"loc": err["loc"], "msg": err["msg"]} for err in e.errors()])


async def update_rx_settings(clinic_id: str, clinic: dict, changes: dict, expected_version: int) -> dict:
    """Validated write with compare-and-swap on settings.version (rx_update_settings)."""
    merged = validate_settings(clinic.get("rx_settings") or {}, changes)
    merged.pop("version", None)
    try:
        res = await sb(supabase.rpc("rx_update_settings", {"p_clinic_id": clinic_id, "p_settings": merged,
                                                           "p_expected_version": expected_version}))
    except Exception as e:
        raise_for(e)
    return {**DEFAULT_SETTINGS, **(res.data or {})}


# ── stores ──────────────────────────────────────────────────────────────────

async def list_stores(clinic_id: str, branch_id: Optional[str] = None) -> list[dict]:
    q = scoped_query("rx_stores", clinic_id).order("code")
    return await _rows(q.eq("branch_id", branch_id) if branch_id else q)


async def get_store(clinic_id: str, store_id: str) -> dict:
    rows = await _rows(scoped_query("rx_stores", clinic_id).eq("id", store_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Store not found.")
    return rows[0]


async def create_or_update_store(clinic_id: str, body: dict, store_id: Optional[str] = None) -> dict:
    try:
        if store_id:
            res = await sb(supabase.table("rx_stores").update(body).eq("clinic_id", clinic_id).eq("id", store_id))
        else:
            # unscoped: insert_scoped_by_payload
            res = await sb(supabase.table("rx_stores").insert({**body, "clinic_id": clinic_id}))
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=404, detail="Store not found.")
    return res.data[0]


async def assert_ward(clinic_id: str, ward_id: str, branch_id: Optional[str]) -> None:
    """A ward sub-store sits on a ward of this clinic (and of its branch, when it has one). This is the only
    check: an rx_stores → ipd_wards FK would block the IPD owner purge (migration 139 leaves rx_stores alone)."""
    q = scoped_query("ipd_wards", clinic_id, "id, branch_id").eq("id", ward_id).limit(1)
    rows = await _rows(q)
    if not rows or (branch_id and rows[0].get("branch_id") and str(rows[0]["branch_id"]) != str(branch_id)):
        raise HTTPException(status_code=404, detail="Ward not found.")


# ── provisioning, checklist, deactivation ───────────────────────────────────

async def provision_defaults(clinic_id: str) -> None:
    """Owner enable: a clinic with no store gets one clinic-wide retail counter
    that can also receive goods ("Main Pharmacy"). Existing stores are never touched."""
    if await list_stores(clinic_id):
        return
    await create_or_update_store(clinic_id, {"code": "MAIN", "name": "Main Pharmacy", "store_type": "retail_counter",
                                             "can_sell": True, "can_receive_grn": True})


def setup_checklist(settings: dict, stores: list[dict], gstin: Optional[str], counts: Optional[dict] = None) -> list[dict]:
    """What the pharmacy still has to configure. `counts` carries later-phase masters
    (items: migration 135) and is empty until those tables exist."""
    active = [s for s in stores if s.get("is_active")]
    selling = [s for s in active if s.get("can_sell")]
    items = int((counts or {}).get("items") or 0)
    saved = int(settings.get("version") or 0) >= 1
    return [
        {"key": "stores", "label": "Stores", "done": bool(selling),
         "detail": f"{len(active)} active, {len(selling)} selling" if active else "Add a retail counter that can sell"},
        {"key": "items", "label": "Drug master", "done": items > 0,
         "detail": f"{items} items" if items else "Import or add medicines (Drug Master)"},
        {"key": "gstin", "label": "GST registration", "done": bool(gstin),
         "detail": "GSTIN on file: bills are tax invoices" if gstin else
                   "No GSTIN in OPD settings: bills will be bills of supply"},
        {"key": "settings", "label": "Settings", "done": saved,
         "detail": "Saved" if saved else "Review and save the pharmacy settings"},
    ]


async def deactivation_preview(clinic_id: str) -> dict:
    """What an owner disable would leave running (ADR-RX-09).
    Drafts are not counted: they hold no stock and no number."""
    from app.services.rx_audit import open_count
    from app.services.rx_ipd import open_indent_count
    from app.services.rx_transfers import open_counts
    if not await list_stores(clinic_id):
        return {"open_sales": 0, "open_indents": 0, "transfers_in_transit": 0, "open_audits": 0, "blocking": False}
    indents, transit, audits = (await open_indent_count(clinic_id), await open_counts(clinic_id),
                                await open_count(clinic_id))
    return {"open_sales": 0, "open_indents": indents, "transfers_in_transit": transit, "open_audits": audits,
            "blocking": bool(indents or transit or audits)}
