"""Kriya OPD OS PDF Generation Engine (Phase 1.3).

Renders immutable clinical and financial documents using fpdf2 and uharfbuzz
with bundled Noto TTF fonts (Latin, Devanagari, Telugu).
Renders ONLY snapshots stored on the row for 100% deterministic reprints.
Zero-LLM clinical safety compliant.
"""

import os
import warnings
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

warnings.filterwarnings("ignore", category=UserWarning, module="fpdf")

from fastapi import HTTPException, status
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.utils.helpers import IST, doctor_title

FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts")


class OPDDocument(FPDF):
    """Custom FPDF subclass with page number footers."""

    def __init__(self, doc_label: str = "Kriya Rx", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.doc_label = doc_label
        self.setup_fonts()

    def setup_fonts(self):
        """Register Noto Sans regular/bold and Indic fallbacks."""
        reg_path = os.path.join(FONTS_DIR, "NotoSans-Regular.ttf")
        bold_path = os.path.join(FONTS_DIR, "NotoSans-Bold.ttf")
        deva_path = os.path.join(FONTS_DIR, "NotoSansDevanagari-Regular.ttf")
        tel_path = os.path.join(FONTS_DIR, "NotoSansTelugu-Regular.ttf")

        if os.path.exists(reg_path):
            self.add_font("NotoSans", "", reg_path)
        if os.path.exists(bold_path):
            self.add_font("NotoSans", "B", bold_path)
        if os.path.exists(deva_path):
            self.add_font("NotoDevanagari", "", deva_path)
        if os.path.exists(tel_path):
            self.add_font("NotoTelugu", "", tel_path)

        # Fallback fonts for Indic script. fpdf2 keys self.fonts in lower case, so the
        # old mixed-case check never matched and Hindi / Telugu text was dropped.
        # exact_match=False: bold text falls back to the regular Indic face.
        fallbacks = [f for f in ["NotoDevanagari", "NotoTelugu"] if f.lower() in self.fonts]
        if fallbacks:
            self.set_fallback_fonts(fallbacks, exact_match=False)
        try:
            self.set_text_shaping(True)   # conjuncts and vowel signs need HarfBuzz (uharfbuzz)
        except Exception:  # noqa: BLE001 - uharfbuzz missing: unshaped but still legible Latin
            pass


def render_prescription_pdf(rx: dict, items: list[dict]) -> bytes:
    """Render signed e-prescription as A5 portrait PDF from immutable snapshots.

    Draft prescriptions are rejected with HTTP 409.
    """
    if rx.get("status") == "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Draft prescriptions cannot be rendered as PDF",
        )

    short_id = str(rx.get("id", ""))[:8]
    version = rx.get("version", 1)
    doc_label = f"Kriya Rx {short_id} v{version}"

    pdf = OPDDocument(doc_label=doc_label, orientation="P", unit="mm", format="A5")
    pdf.set_margins(8, 8, 8)
    pdf.set_auto_page_break(auto=True, margin=10)
    pdf.add_page()

    # 1. Letterhead block (from letterhead_snapshot)
    lh = rx.get("letterhead_snapshot") or {}
    clinic_name = lh.get("clinic_name") or "Medical Clinic"
    branch_name = lh.get("branch_name")
    address = lh.get("branch_address") or lh.get("address") or ""
    phone = lh.get("phone") or ""
    email = lh.get("email") or ""

    pdf.set_font("NotoSans", "B", 14)
    pdf.cell(0, 7, clinic_name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("NotoSans", "", 8)
    subhead = []
    if branch_name:
        subhead.append(branch_name)
    if address:
        subhead.append(address)
    contact_parts = []
    if phone:
        contact_parts.append(f"Ph: {phone}")
    if email:
        contact_parts.append(f"Email: {email}")
    if contact_parts:
        subhead.append(" | ".join(contact_parts))

    for line in subhead:
        pdf.cell(0, 4, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(1)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(8, pdf.get_y(), 140, pdf.get_y())
    pdf.ln(2)

    # 2. Patient block (from patient_snapshot)
    pat = rx.get("patient_snapshot") or {}
    p_name = pat.get("name") or "Patient"
    p_mrn = pat.get("mrn") or "—"
    p_age = f"{pat.get('age')}y" if pat.get("age") is not None else "—"
    p_gender = pat.get("gender") or "—"

    signed_at = rx.get("signed_at") or rx.get("created_at") or ""
    try:
        dt = datetime.fromisoformat(str(signed_at).replace("Z", "+00:00"))
        date_str = dt.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        date_str = str(signed_at)[:16]

    pdf.set_font("NotoSans", "B", 8)
    pdf.cell(20, 5, "Patient:", border=0)
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(55, 5, f"{p_name} ({p_gender}, {p_age})", border=0)

    pdf.set_font("NotoSans", "B", 8)
    pdf.cell(15, 5, "MRN:", border=0)
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(0, 5, p_mrn, border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("NotoSans", "B", 8)
    pdf.cell(20, 5, "Date:", border=0)
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(55, 5, date_str, border=0)

    allergies = pat.get("allergies") or []
    al_status = pat.get("allergies_status") or "unknown"
    if al_status == "recorded" and allergies:
        al_text = ", ".join(allergies)
    elif al_status == "none_known":
        al_text = "No known drug allergies"
    else:
        al_text = "Unknown / not recorded"

    pdf.set_font("NotoSans", "B", 8)
    pdf.cell(15, 5, "Allergies:", border=0)
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(0, 5, al_text, border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(1)
    pdf.line(8, pdf.get_y(), 140, pdf.get_y())
    pdf.ln(3)

    # 3. Rx Symbol & Table Header
    pdf.set_font("NotoSans", "B", 11)
    pdf.cell(0, 6, "℞ Medical Prescription", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_fill_color(240, 243, 246)
    pdf.set_font("NotoSans", "B", 7)
    pdf.cell(8, 6, "#", border=1, align="C", fill=True)
    pdf.cell(50, 6, "Medicine / Formulation", border=1, align="L", fill=True)
    pdf.cell(22, 6, "Dose & Route", border=1, align="L", fill=True)
    pdf.cell(24, 6, "Frequency", border=1, align="L", fill=True)
    pdf.cell(14, 6, "Duration", border=1, align="C", fill=True)
    pdf.cell(0, 6, "Instructions", border=1, align="L", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # 4. Medication lines
    pdf.set_font("NotoSans", "", 7)
    for idx, item in enumerate(items, start=1):
        drug_name = item.get("drug_name") or ""
        form = item.get("formulation") or ""
        strength = f" ({item.get('strength')})" if item.get("strength") else ""
        med_desc = f"{drug_name}{strength} [{form}]"

        dose = item.get("dosage") or ""
        route = item.get("route") or "oral"
        dose_desc = f"{dose} ({route})"

        freq = item.get("frequency") or ""
        timing = (item.get("timing") or "any").replace("_", " ")
        freq_desc = f"{freq} • {timing}"

        days = f"{item.get('duration_days')}d" if item.get("duration_days") else "SOS"
        instr = item.get("instructions") or "—"

        pdf.cell(8, 6, str(idx), border=1, align="C")
        pdf.cell(50, 6, med_desc[:36], border=1, align="L")
        pdf.cell(22, 6, dose_desc[:16], border=1, align="L")
        pdf.cell(24, 6, freq_desc[:18], border=1, align="L")
        pdf.cell(14, 6, days, border=1, align="C")
        pdf.cell(0, 6, instr[:14], border=1, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # 5. General instructions
    gen_instr = rx.get("general_instructions")
    if gen_instr:
        pdf.ln(3)
        pdf.set_font("NotoSans", "B", 8)
        pdf.cell(0, 5, "Advice & General Instructions:", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("NotoSans", "", 7)
        pdf.multi_cell(0, 4, gen_instr)

    # 6. Clinician Signature block (from signer_snapshot)
    pdf.ln(6)
    signer = rx.get("signer_snapshot") or {}
    doc_name = signer.get("name") or "Treating Clinician"
    quals = signer.get("qualifications") or ""
    reg_no = signer.get("registration_number") or ""
    council = signer.get("registration_council") or ""

    pdf.set_font("NotoSans", "B", 9)
    pdf.cell(0, 5, doctor_title(doc_name), border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("NotoSans", "", 7)
    if quals:
        pdf.cell(0, 4, quals, border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.cell(0, 4, f"Reg. No: {reg_no} | Council: {council}", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 4, f"Digitally signed on {date_str} (IST)", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # 7. Document identifier footer
    pdf.set_y(-12)
    pdf.set_font("NotoSans", "", 6)
    pdf.set_text_color(130, 130, 130)
    pdf.cell(0, 4, f"{doc_label} • Generated by Kriya OPD OS", border=0, align="C")

    return bytes(pdf.output())


def render_invoice_pdf(
    invoice: dict,
    items: list[dict],
    receipts: list[dict],
    clinic: Optional[dict] = None,
) -> bytes:
    """Render invoice as A4 portrait PDF from immutable snapshots (Phase 1.4).

    Draft invoices are rejected with HTTP 409.
    """
    if invoice.get("status") == "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot generate PDF for draft invoice. Please issue the invoice first.",
        )

    inv_num = invoice.get("invoice_number") or f"INV-DRAFT-{str(invoice.get('id', ''))[:8]}"
    doc_label = f"Invoice {inv_num}"
    pdf = OPDDocument(doc_label=doc_label, orientation="P", unit="mm", format="A4")
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # 1. Header & Clinic branding
    clinic_name = (clinic or {}).get("name") or "Medical Center"
    from app.services.tenant import clinic_letterhead
    lh = clinic_letterhead(clinic)
    clinic_phone = lh["phone"]
    clinic_address = lh["address"]

    pdf.set_font("NotoSans", "B", 14)
    pdf.set_text_color(24, 43, 73)  # Navy
    pdf.cell(110, 7, clinic_name, border=0, new_x=XPos.RIGHT, new_y=YPos.TOP)

    # Right side: Invoice title & badge
    pdf.set_font("NotoSans", "B", 12)
    pdf.set_text_color(16, 120, 70)  # Green
    status_str = (invoice.get("status") or "ISSUED").upper()
    # A tax invoice only when GST was charged; exempt health care is a bill of supply (migration 117).
    is_tax = (invoice.get("total_tax_paise") or 0) > 0
    doc_title = "TAX INVOICE" if is_tax else ("BILL OF SUPPLY" if invoice.get("clinic_gstin") else "BILL")
    pdf.cell(76, 7, f"{doc_title} [{status_str}]", border=0, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("NotoSans", "", 8)
    pdf.set_text_color(90, 90, 90)
    clinic_sub = clinic_address + (f" | Tel: {clinic_phone}" if clinic_phone else "")
    if clinic_sub:
        pdf.cell(110, 4, clinic_sub, border=0, new_x=XPos.RIGHT, new_y=YPos.TOP)
    else:
        pdf.cell(110, 4, "", border=0, new_x=XPos.RIGHT, new_y=YPos.TOP)

    inv_date_str = ""
    if invoice.get("issued_at"):
        try:
            dt = datetime.fromisoformat(invoice["issued_at"].replace("Z", "+00:00"))
            inv_date_str = dt.strftime("%d %b %Y, %I:%M %p")
        except Exception:
            inv_date_str = str(invoice["issued_at"])[:16]

    pdf.cell(76, 4, f"Date: {inv_date_str}", border=0, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(110, 4, f"Invoice #: {inv_num}", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if invoice.get("clinic_gstin"):
        pdf.cell(110, 4, f"GSTIN: {invoice['clinic_gstin']}", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if invoice.get("patient_gstin"):
        pdf.cell(110, 4, f"Buyer GSTIN: {invoice['patient_gstin']}", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(3)
    pdf.set_draw_color(220, 220, 220)
    pdf.set_line_width(0.3)
    pdf.line(12, pdf.get_y(), 198, pdf.get_y())
    pdf.ln(3)

    # 2. Patient Demographics Box
    patient_snap = invoice.get("patient_snapshot") or {}
    pat_name = patient_snap.get("name") or "Patient"
    pat_phone = patient_snap.get("phone") or "—"
    pat_mrn = patient_snap.get("mrn") or "—"
    gender = patient_snap.get("gender") or ""
    age = patient_snap.get("age_years")
    demog_parts = []
    if age:
        demog_parts.append(f"{age}y")
    if gender:
        demog_parts.append(gender.upper())
    pat_demog = " / ".join(demog_parts) if demog_parts else "—"

    pdf.set_fill_color(248, 249, 250)
    pdf.rect(12, pdf.get_y(), 186, 16, style="F")

    curr_y = pdf.get_y() + 2
    pdf.set_xy(15, curr_y)
    pdf.set_font("NotoSans", "B", 8)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(22, 5, "Billed To:", border=0)
    pdf.set_font("NotoSans", "B", 9)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(70, 5, pat_name, border=0)

    pdf.set_font("NotoSans", "B", 8)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(15, 5, "MRN:", border=0)
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(30, 5, pat_mrn, border=0)

    pdf.set_font("NotoSans", "B", 8)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(15, 5, "Age/Sex:", border=0)
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(30, 5, pat_demog, border=0)

    pdf.set_xy(15, curr_y + 6)
    pdf.set_font("NotoSans", "B", 8)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(22, 5, "Contact:", border=0)
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(70, 5, pat_phone, border=0)

    pdf.set_xy(12, curr_y + 14)
    pdf.ln(2)

    # 3. Line Items Table
    pdf.set_font("NotoSans", "B", 8)
    pdf.set_fill_color(235, 240, 248)
    pdf.set_text_color(24, 43, 73)
    pdf.set_draw_color(210, 220, 235)

    if is_tax:
        col_w = [8, 66, 18, 12, 22, 24, 12, 24]
        headers = ["#", "Item Description", "HSN/SAC", "Qty", "Price (INR)", "Taxable (INR)", "GST %", "GST (INR)"]
    else:
        col_w = [10, 80, 24, 16, 26, 30]
        headers = ["#", "Item Description", "Type", "Qty", "Price (INR)", "Total (INR)"]
    for w, h in zip(col_w, headers):
        align = "L" if h in ("#", "Item Description", "Type", "HSN/SAC") else "R"
        pdf.cell(w, 6, h, border=1, align=align, fill=True)
    pdf.ln()

    pdf.set_font("NotoSans", "", 8)
    pdf.set_text_color(30, 30, 30)

    for it in items:
        line_no = str(it.get("line_no", ""))
        desc = it.get("description") or "Service"
        itype = (it.get("item_type") or "").title()
        qty = str(it.get("quantity", 1))
        unit_paise = it.get("unit_price_paise", 0)
        tot_paise = it.get("line_total_paise", unit_paise * int(qty))

        unit_str = f"{unit_paise / 100:.2f}"
        tot_str = f"{tot_paise / 100:.2f}"

        if is_tax:
            pdf.cell(col_w[0], 6, line_no, border="LRB", align="C")
            pdf.cell(col_w[1], 6, desc[:36], border="LRB", align="L")
            pdf.cell(col_w[2], 6, it.get("hsn_sac") or "", border="LRB", align="L")
            pdf.cell(col_w[3], 6, qty, border="LRB", align="R")
            pdf.cell(col_w[4], 6, unit_str, border="LRB", align="R")
            pdf.cell(col_w[5], 6, tot_str, border="LRB", align="R")
            pdf.cell(col_w[6], 6, f"{(it.get('tax_rate_bps') or 0) / 100:g}", border="LRB", align="R")
            pdf.cell(col_w[7], 6, f"{(it.get('total_tax_paise') or 0) / 100:.2f}", border="LRB", align="R")
            pdf.ln()
            continue
        pdf.cell(col_w[0], 6, line_no, border="LRB", align="C")
        pdf.cell(col_w[1], 6, desc[:42], border="LRB", align="L")
        pdf.cell(col_w[2], 6, itype, border="LRB", align="L")
        pdf.cell(col_w[3], 6, qty, border="LRB", align="R")
        pdf.cell(col_w[4], 6, unit_str, border="LRB", align="R")
        pdf.cell(col_w[5], 6, tot_str, border="LRB", align="R")
        pdf.ln()

    # 4. Totals Breakdown Block
    pdf.ln(3)
    subtotal_paise = invoice.get("subtotal_paise", 0)
    discount_paise = invoice.get("discount_paise", 0)
    total_paise = invoice.get("total_paise", subtotal_paise - discount_paise)
    paid_paise = invoice.get("paid_paise", 0)
    credited_paise = invoice.get("credited_paise", 0)
    balance_paise = max(0, total_paise - credited_paise - paid_paise)

    tot_x = 110
    tot_label_w = 46
    tot_val_w = 30

    def _render_total_line(label: str, paise_val: int, is_bold: bool = False, is_highlight: bool = False):
        pdf.set_x(tot_x)
        pdf.set_font("NotoSans", "B" if is_bold else "", 8)
        if is_highlight:
            pdf.set_text_color(180, 40, 40)
        else:
            pdf.set_text_color(20, 20, 20)
        pdf.cell(tot_label_w, 5, label, border=0, align="R")
        pdf.cell(tot_val_w, 5, f"INR {paise_val / 100:.2f}", border=0, align="R")
        pdf.ln()

    _render_total_line("Subtotal:", subtotal_paise)
    if discount_paise > 0:
        d_reason = f" ({invoice.get('discount_reason', '')})" if invoice.get("discount_reason") else ""
        _render_total_line(f"Discount{d_reason}:", -discount_paise)
    _render_total_line("Total Amount:", total_paise, is_bold=True)
    if is_tax:
        tax_paise = invoice["total_tax_paise"]
        _render_total_line("of which taxable value:", total_paise - tax_paise)
        _render_total_line("CGST:", tax_paise // 2)
        _render_total_line("SGST:", tax_paise - tax_paise // 2)
    if credited_paise:
        _render_total_line("Credit notes:", -credited_paise)
    _render_total_line("Amount Received:", paid_paise)
    _render_total_line("Balance Due:", balance_paise, is_bold=True, is_highlight=(balance_paise > 0))

    # 5. Receipts / Payments Table (if any)
    if receipts:
        pdf.ln(4)
        pdf.set_font("NotoSans", "B", 8)
        pdf.set_text_color(24, 43, 73)
        pdf.cell(0, 5, "Payment Receipts Ledger:", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_fill_color(240, 245, 250)
        r_cols = [32, 28, 22, 58, 24, 22]
        r_headers = ["Receipt #", "Date", "Mode", "Reference", "Cashier", "Amount"]
        for rw, rh in zip(r_cols, r_headers):
            pdf.cell(rw, 5, rh, border=1, fill=True, align="R" if rh == "Amount" else "L")
        pdf.ln()

        pdf.set_font("NotoSans", "", 7)
        pdf.set_text_color(40, 40, 40)
        for r in receipts:
            r_num = r.get("receipt_number") or f"RCT-{str(r.get('id', ''))[:8]}"
            r_date = str(r.get("created_at", ""))[:10]
            r_mode = (r.get("mode") or "").upper()
            r_kind = r.get("kind") or "payment"
            if r_kind == "refund":
                r_mode = f"REFUND ({r_mode})"
            r_ref = r.get("reference") or r.get("gateway_payment_id") or "—"
            r_cashier = r.get("received_by_name") or "System"
            r_amt_val = r.get("amount_paise", 0) / 100.0
            r_amt_str = f"-{r_amt_val:.2f}" if r_kind == "refund" else f"{r_amt_val:.2f}"

            pdf.cell(r_cols[0], 5, r_num, border="LRB")
            pdf.cell(r_cols[1], 5, r_date, border="LRB")
            pdf.cell(r_cols[2], 5, r_mode, border="LRB")
            pdf.cell(r_cols[3], 5, str(r_ref)[:28], border="LRB")
            pdf.cell(r_cols[4], 5, str(r_cashier)[:12], border="LRB")
            pdf.cell(r_cols[5], 5, r_amt_str, border="LRB", align="R")
            pdf.ln()

    credit_notes = invoice.get("credit_notes") or []
    if credit_notes:
        pdf.ln(4)
        pdf.set_font("NotoSans", "B", 8)
        pdf.set_text_color(24, 43, 73)
        pdf.cell(0, 5, "Credit Notes:", border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("NotoSans", "", 7)
        pdf.set_text_color(40, 40, 40)
        for c in credit_notes:
            gst = f", GST {c.get('tax_paise', 0) / 100:.2f}" if c.get("tax_paise") else ""
            pdf.cell(0, 5, f"{c.get('credit_note_number')}  {str(c.get('created_at', ''))[:10]}  "
                           f"{(c.get('reason') or '').replace('_', ' ')}  -{c.get('total_credit_paise', 0) / 100:.2f}{gst}",
                     border=0, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Footer note
    pdf.set_y(-15)
    pdf.set_font("NotoSans", "", 7)
    pdf.set_text_color(140, 140, 140)
    pdf.cell(0, 5, f"{doc_label} • Generated by Kriya OPD OS • Thank you", border=0, align="C")

    return bytes(pdf.output())



_LAB_FLAG_TEXT = {"low": "L", "high": "H", "critical_low": "CRITICAL LOW", "critical_high": "CRITICAL HIGH",
                  "abnormal": "ABNORMAL", "not_evaluated": "", "normal": ""}
_A4_RIGHT = 198
_RED = (176, 0, 32)


def _ist_text(value: Any) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return str(value)[:16]


def _ist_date(value: Any) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
    except Exception:
        return str(value)[:10]


# ── shared A4 building blocks (lab reports, discharge summaries, consents, certificates) ──

def _a4(doc_label: str) -> OPDDocument:
    pdf = OPDDocument(doc_label=doc_label, orientation="P", unit="mm", format="A4")
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    return pdf


def _rule(pdf: OPDDocument, after: float = 2) -> None:
    pdf.set_draw_color(160, 160, 160)
    pdf.line(12, pdf.get_y(), _A4_RIGHT, pdf.get_y())
    pdf.ln(after)


def _letterhead_block(pdf: OPDDocument, lh: dict, fallback: str) -> None:
    pdf.set_font("NotoSans", "B", 15)
    pdf.cell(0, 7, lh.get("clinic_name") or fallback, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("NotoSans", "", 8)
    contact = " | ".join(p for p in (f"Ph: {lh['phone']}" if lh.get("phone") else "",
                                     f"Email: {lh['email']}" if lh.get("email") else "") if p)
    for line in (lh.get("branch_name"), lh.get("branch_address") or lh.get("address"), contact):
        if line:
            pdf.cell(0, 4, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)
    _rule(pdf)


def _banner(pdf: OPDDocument, text: str) -> None:
    pdf.set_fill_color(253, 236, 236)
    pdf.set_text_color(*_RED)
    pdf.set_font("NotoSans", "B", 9)
    pdf.multi_cell(0, 7, text, border=1, align="C", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)


def _kv_rows(pdf: OPDDocument, rows: list[tuple[str, Any]]) -> None:
    """Label: value pairs, two per line."""
    for i in range(0, len(rows), 2):
        for label, val in rows[i:i + 2]:
            pdf.set_font("NotoSans", "B", 8)
            pdf.cell(24, 5, f"{label}:")
            pdf.set_font("NotoSans", "", 8)
            pdf.cell(69, 5, str(val if val not in (None, "") else "—")[:60])
        pdf.ln(5)
    pdf.ln(1)
    _rule(pdf, 3)


def _title(pdf: OPDDocument, text: str) -> None:
    pdf.set_font("NotoSans", "B", 12)
    pdf.cell(0, 7, text, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _section(pdf: OPDDocument, heading: str, body: Optional[str]) -> None:
    """A heading and its free text; skipped when there is no text."""
    if not body or not str(body).strip():
        return
    pdf.set_font("NotoSans", "B", 9)
    pdf.cell(0, 6, heading, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("NotoSans", "", 8)
    pdf.multi_cell(0, 4.2, str(body).strip(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)


def _table(pdf: OPDDocument, widths: tuple, headers: tuple, rows: list[tuple], styles: Optional[list] = None,
           emphasis: tuple = (1,)) -> None:
    """Bordered table; styles[i] = '' | 'bold' | 'critical' styles row i's `emphasis` columns."""
    pdf.set_fill_color(240, 243, 246)
    pdf.set_font("NotoSans", "B", 8)
    for w, h in zip(widths, headers):
        pdf.cell(w, 7, h, border=1, fill=True)
    pdf.ln(7)
    for i, row in enumerate(rows):
        style = (styles or [""] * len(rows))[i]
        for j, (w, cell) in enumerate(zip(widths, row)):
            emphasise = j in emphasis and style in ("bold", "critical")
            pdf.set_font("NotoSans", "B" if emphasise else "", 8)
            if emphasise and style == "critical":
                pdf.set_text_color(*_RED)
            pdf.cell(w, 6, str(cell if cell is not None else "")[: max(4, int(w / 1.6))], border=1)
            pdf.set_text_color(0, 0, 0)
        pdf.ln(6)
    pdf.ln(2)


def _signature(pdf: OPDDocument, signer: dict, signed_at: Any, fallback_name: str, verb: str = "Digitally signed",
               extra: Optional[list[str]] = None) -> None:
    pdf.ln(6)
    pdf.set_font("NotoSans", "B", 9)
    pdf.cell(0, 5, doctor_title(signer.get("name") or fallback_name), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("NotoSans", "", 7)
    if signer.get("qualifications"):
        pdf.cell(0, 4, signer["qualifications"], new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 4, f"Reg. No: {signer.get('registration_number') or '—'} | Council: "
                   f"{signer.get('registration_council') or '—'}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 4, f"{verb} on {_ist_text(signed_at)} (IST)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    for line in extra or []:
        pdf.cell(0, 4, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _footer(pdf: OPDDocument, doc_label: str) -> bytes:
    pdf.set_y(-14)
    pdf.set_font("NotoSans", "", 6)
    pdf.set_text_color(130, 130, 130)
    pdf.cell(0, 4, f"{doc_label} • Generated by Kriya Hospital OS • End of document", align="C")
    return bytes(pdf.output())


def _person(pat: dict) -> str:
    age = f"{pat['age_years']}y" if pat.get("age_years") is not None else "—"
    return f"{pat.get('name') or 'Patient'} ({(pat.get('gender') or '—').title()}, {age})"


def render_lab_report_pdf(result: dict, values: list[dict]) -> bytes:
    """Render a signed LIS report (migration 118) as A4 from the snapshots and
    values stored on the row, so a reprint is identical to what was signed.
    Flagged values are bold; critical ones bold red. A superseded version
    carries a banner pointing at its amendment."""
    if result.get("status") not in ("validated", "amended"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a signed lab report can be rendered")

    doc_label = f"Kriya Lab {str(result.get('id', ''))[:8]} v{result.get('version', 1)}"
    pdf = _a4(doc_label)
    _letterhead_block(pdf, result.get("letterhead_snapshot") or {}, "Laboratory")
    if result.get("status") == "amended":
        _banner(pdf, "SUPERSEDED - a corrected version of this report was issued. Do not use for care.")

    pat = result.get("patient_snapshot") or {}
    _kv_rows(pdf, [("Patient", _person(pat)), ("MRN", pat.get("mrn")),
                   ("Referred by", doctor_title(pat["referred_by"]) if pat.get("referred_by") else None),
                   ("Sample", f"{pat.get('sample_type') or '—'}  [{pat.get('specimen_barcode') or '—'}]"),
                   ("Collected", _ist_text(pat.get("sample_collected_at"))),
                   ("Reported", _ist_text(result.get("validated_at")))])

    _title(pdf, result.get("test_name") or "Laboratory Report")
    if (result.get("version") or 1) > 1:
        pdf.set_font("NotoSans", "", 8)
        pdf.multi_cell(0, 4, f"Amended report (version {result['version']}). Reason: {result.get('amendment_reason') or '—'}",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    widths = (62, 30, 22, 46, 26)
    pdf.set_fill_color(240, 243, 246)
    pdf.set_font("NotoSans", "B", 8)
    for w, h in zip(widths, ("Parameter", "Result", "Unit", "Reference range", "Flag")):
        pdf.cell(w, 7, h, border=1, fill=True)
    pdf.ln(7)
    for v in values:
        flag = v.get("flag") or "normal"
        if v.get("numeric_value") is not None:
            shown = format(Decimal(str(v["numeric_value"])).normalize(), "f")
        else:
            shown = v.get("text_value") or ""
        critical, flagged = flag.startswith("critical"), flag not in ("normal", "not_evaluated")
        pdf.set_font("NotoSans", "", 8)
        pdf.cell(widths[0], 6, (v.get("parameter_name") or "")[:42], border=1)
        pdf.set_font("NotoSans", "B" if flagged else "", 8)
        if critical:
            pdf.set_text_color(*_RED)
        pdf.cell(widths[1], 6, shown[:22], border=1)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("NotoSans", "", 8)
        pdf.cell(widths[2], 6, (v.get("unit") or "")[:14], border=1)
        pdf.cell(widths[3], 6, (v.get("reference_range_display") or "—")[:32], border=1)
        pdf.set_font("NotoSans", "B" if flagged else "", 8)
        if critical:
            pdf.set_text_color(*_RED)
        pdf.cell(widths[4], 6, _LAB_FLAG_TEXT.get(flag, ""), border=1)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(6)
        if v.get("remarks"):
            pdf.set_font("NotoSans", "", 7)
            pdf.multi_cell(0, 4, f"    Note: {v['remarks']}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    if result.get("has_critical_value"):
        pdf.ln(2)
        pdf.set_text_color(*_RED)
        pdf.set_font("NotoSans", "B", 8)
        pdf.multi_cell(0, 4, "This report contains CRITICAL values. Please contact your doctor immediately.",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)

    _signature(pdf, result.get("pathologist_snapshot") or {}, result.get("validated_at"), "Pathologist",
               extra=[f"Entered by: {result['technician_name']}"] if result.get("technician_name") else None)
    return _footer(pdf, doc_label)


_MODALITY_TEXT = {"x_ray": "X-Ray", "ultrasound": "Ultrasound", "ct": "CT", "mri": "MRI",
                  "mammography": "Mammography", "dexa": "DEXA", "fluoroscopy": "Fluoroscopy", "other": "Imaging"}
#: Printed on every obstetric report and Form F (PCPNDT Act, 1994).
PCPNDT_NOTICE = ("Disclosure of the sex of the foetus is prohibited under the PCPNDT Act, 1994. The sex of the foetus "
                 "has not been determined or disclosed, and this centre does not do so in any manner.")


def render_radiology_report_pdf(report: dict) -> bytes:
    """Render a finalized radiology report (migration 123) from the row and
    the snapshots frozen at signing, so a reprint is identical to what was
    signed. A superseded version carries a banner; an obstetric report carries
    the PCPNDT notice and its Form F number."""
    if report.get("status") not in ("finalized", "amended"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a finalized radiology report can be rendered")
    number = report.get("report_number") or ""
    doc_label = f"Kriya {number} v{report.get('version', 1)}"
    pdf = _a4(doc_label)
    _letterhead_block(pdf, report.get("letterhead_snapshot") or {}, "Radiology")
    if report.get("status") == "amended":
        _banner(pdf, "SUPERSEDED - a corrected version of this report was issued. Do not use for care.")
    pat = report.get("patient_snapshot") or {}
    _kv_rows(pdf, [("Patient", _person(pat)), ("MRN", pat.get("mrn")),
                   ("Report No", f"{number}" + (f" (v{report['version']})" if (report.get("version") or 1) > 1 else "")),
                   ("Modality", _MODALITY_TEXT.get(report.get("modality"), report.get("modality"))),
                   ("Referred by", doctor_title(pat["referred_by"]) if pat.get("referred_by") else None),
                   ("Reported", _ist_text(report.get("finalized_at"))),
                   ("Form F", pat.get("form_f_number")) if report.get("is_obstetric") else ("PACS UID", report.get("pacs_study_uid"))])
    _title(pdf, (report.get("procedure_name") or "Radiology Report").upper())
    if (report.get("version") or 1) > 1:
        pdf.set_font("NotoSans", "", 8)
        pdf.multi_cell(0, 4, f"Amended report (version {report['version']}). Reason: {report.get('amendment_reason') or '—'}",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    _section(pdf, "Clinical history", report.get("clinical_history"))
    _section(pdf, "Findings", report.get("findings"))
    _section(pdf, "Impression", report.get("impression"))
    _section(pdf, "Recommendations", report.get("recommendations"))
    if report.get("is_obstetric"):
        pdf.set_font("NotoSans", "B", 7)
        pdf.multi_cell(0, 4, PCPNDT_NOTICE, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    _signature(pdf, report.get("radiologist_snapshot") or {}, report.get("finalized_at"), "Radiologist",
               extra=[f"Prepared by: {report['drafted_by_name']}"] if report.get("drafted_by_name") else None)
    pdf.set_font("NotoSans", "", 7)
    pdf.ln(2)
    pdf.multi_cell(0, 4, "This report is an opinion on the images. Correlate clinically; discuss with your doctor.",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return _footer(pdf, doc_label)


FORM_F_INDICATIONS = {
    "dating_scan_early_pregnancy": "Dating / early pregnancy (confirm intrauterine pregnancy, gestational age)",
    "first_trimester_screening": "First-trimester screening (NT / NB)",
    "anomaly_scan_target": "Detection of fetal anomalies (targeted anomaly scan)",
    "fetal_growth_biometry": "Evaluation of fetal growth / biometry",
    "liquor_placental_evaluation": "Evaluation of liquor amnii / placental localisation",
    "suspected_ectopic_pregnancy": "Suspected ectopic pregnancy",
    "suspected_molar_pregnancy": "Suspected molar pregnancy",
    "fetal_viability_bleeding": "Fetal viability (bleeding per vaginum / pain)",
    "multiple_gestation_chorionicity": "Multiple gestation: number and chorionicity",
    "other_medical_indication": "Other medical indication",
}


def render_pcpndt_form_f_pdf(form: dict) -> bytes:
    """Render a recorded PCPNDT Form F (migration 123) from its locked row:
    the maternal history, the statutory indication and both declarations."""
    number = form.get("form_f_number") or ""
    doc_label = f"Kriya {number or 'Form F'}"
    pdf = _a4(doc_label)
    _letterhead_block(pdf, form.get("letterhead_snapshot") or {}, "Diagnostic Centre")
    _title(pdf, "FORM F - PCPNDT ACT, 1994 (Rule 9(4))")
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(0, 5, f"Form F No: {number}    Recorded and locked: {_ist_text(form.get('locked_at'))}", align="C",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pat = form.get("patient_snapshot") or {}
    pdf.set_font("NotoSans", "B", 9)
    pdf.cell(0, 6, "Section A - to be filled for all diagnostic procedures / tests", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    _kv_rows(pdf, [("Patient", pat.get("name") or "Patient"), ("MRN", pat.get("mrn")),
                   ("Age", f"{form.get('patient_age')} years"), ("Husband/Father", form.get("relative_name")),
                   ("Referred by", form.get("referred_by")), ("Doctor", doctor_title((form.get("doctor_snapshot") or {}).get("name") or "")),
                   ("Living children", form.get("num_living_children")),
                   ("Sons / daughters", f"{form.get('num_male_children')} / {form.get('num_female_children')}"),
                   ("LMP", _ist_date(form.get("last_menstrual_period"))),
                   ("Weeks", f"{form.get('gestational_age_weeks')} weeks of pregnancy")])
    pdf.set_font("NotoSans", "B", 9)
    pdf.cell(0, 6, "Section B - non-invasive procedure (ultrasonography)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    _section(pdf, "Indication", FORM_F_INDICATIONS.get(form.get("clinical_indication"), form.get("clinical_indication")))
    _section(pdf, "Indication notes", form.get("indication_notes"))
    _kv_rows(pdf, [("Invasive", "Yes (Section C maintained separately)" if form.get("invasive_procedure_done") else "No"),
                   ("Linked report", pat.get("report_number"))])
    pdf.set_font("NotoSans", "B", 9)
    pdf.cell(0, 6, "Declarations", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("NotoSans", "", 8)
    who = pat.get("name") or "the pregnant woman"
    doctor = doctor_title((form.get("doctor_snapshot") or {}).get("name") or "the doctor")
    pdf.multi_cell(0, 4.2, f"Declaration of the pregnant woman: I, {who}, declare that by undergoing this procedure I do "
                           f"not want to know the sex of my foetus. [{'Signed' if form.get('pregnant_woman_declaration_signed') else 'NOT SIGNED'}]",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)
    pdf.multi_cell(0, 4.2, f"Declaration of the doctor: I, {doctor}, declare that while conducting ultrasonography on "
                           f"{who}, I have neither detected nor disclosed the sex of her foetus to anybody in any manner. "
                           f"[{'Signed' if form.get('doctor_declaration_signed') else 'NOT SIGNED'}]",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    _signature(pdf, form.get("doctor_snapshot") or {}, form.get("locked_at"), "Doctor", verb="Declared and locked")
    pdf.ln(2)
    pdf.set_font("NotoSans", "B", 7)
    pdf.multi_cell(0, 4, PCPNDT_NOTICE, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return _footer(pdf, doc_label)


_VITAL_COLS = (("bp", "BP"), ("pulse_bpm", "Pulse"), ("resp_rate", "RR"), ("temperature_c", "Temp °C"),
               ("spo2_pct", "SpO2 %"), ("blood_glucose_mg_dl", "Glucose"))


def _vitals_row(label: str, v: Optional[dict]) -> tuple:
    v = v or {}
    bp = f"{v['bp_systolic']}/{v['bp_diastolic']}" if v.get("bp_systolic") and v.get("bp_diastolic") else None
    vals = {"bp": bp, **v}
    return (label, *[vals.get(k) if vals.get(k) is not None else "—" for k, _ in _VITAL_COLS],
            _ist_text(v.get("observed_at")) if v else "—")


def render_discharge_summary_pdf(summary: dict) -> bytes:
    """Render a signed IPD discharge summary (migrations 111 + 119) from the row
    alone: its text, its doctor_snapshot and the letterhead / clinical snapshots
    frozen at signing. A superseded version carries a banner."""
    if summary.get("status") not in ("signed", "superseded"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a signed discharge summary can be rendered")
    snap = summary.get("clinical_snapshot") or {}
    pat, stay = snap.get("patient") or {}, snap.get("admission") or {}
    doc_label = f"Kriya DS {stay.get('admission_number') or str(summary.get('id', ''))[:8]} v{summary.get('version', 1)}"
    pdf = _a4(doc_label)
    _letterhead_block(pdf, summary.get("letterhead_snapshot") or {}, "Hospital")
    if summary.get("status") == "superseded":
        _banner(pdf, "SUPERSEDED - an amended discharge summary was issued. Do not use for care.")
    _title(pdf, "DISCHARGE SUMMARY")
    pdf.ln(1)
    _kv_rows(pdf, [("Patient", _person(pat)), ("MRN", pat.get("mrn")),
                   ("IP number", stay.get("admission_number")), ("Bed", stay.get("bed")),
                   ("Admitted", _ist_text(stay.get("admitted_at"))), ("Discharged", _ist_text(stay.get("discharged_at"))),
                   ("Consultant", doctor_title(stay["attending_doctor"]) if stay.get("attending_doctor") else None),
                   ("Discharge", (stay.get("discharge_type") or "").replace("_", " ").upper() or None)])
    if (summary.get("version") or 1) > 1:
        pdf.set_font("NotoSans", "", 8)
        pdf.multi_cell(0, 4, f"Amended summary (version {summary['version']}). Reason: {summary.get('amend_reason') or '—'}",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)

    _section(pdf, "Final diagnosis", summary.get("final_diagnosis"))
    _section(pdf, "Reason for admission", stay.get("reason"))
    _section(pdf, "Presenting complaints", summary.get("presenting_complaints"))
    _section(pdf, "Course in hospital", summary.get("course_in_hospital"))
    _section(pdf, "Procedures performed", summary.get("procedures_performed"))
    for note in snap.get("procedure_notes") or []:
        _section(pdf, f"Procedure note — {_ist_text(note.get('authored_at'))} — {note.get('author_name') or ''}",
                 note.get("body"))

    vitals = snap.get("vitals") or {}
    if vitals.get("admission") or vitals.get("discharge"):
        pdf.set_font("NotoSans", "B", 9)
        pdf.cell(0, 6, "Vital signs", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        _table(pdf, (24, 22, 16, 12, 18, 18, 18, 58), ("", *[h for _, h in _VITAL_COLS], "Recorded"),
               [_vitals_row("Admission", vitals.get("admission")), _vitals_row("Discharge", vitals.get("discharge"))])

    labs = snap.get("lab_results") or []
    if labs:
        pdf.set_font("NotoSans", "B", 9)
        pdf.cell(0, 6, "Investigations (signed laboratory results)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        rows, styles = [], []
        for lab in labs:
            for v in lab.get("values") or []:
                flag = v.get("flag") or "normal"
                rows.append((f"{lab.get('test_name')}: {v.get('parameter_name')}", v.get("value"), v.get("unit") or "",
                             v.get("reference_range_display") or "—", _LAB_FLAG_TEXT.get(flag, ""),
                             _ist_date(lab.get("validated_at"))))
                styles.append("critical" if flag.startswith("critical")
                              else "bold" if flag not in ("normal", "not_evaluated") else "")
        _table(pdf, (62, 24, 18, 38, 22, 22), ("Test / parameter", "Result", "Unit", "Reference", "Flag", "Reported"),
               rows, styles, emphasis=(1, 4))
    other = snap.get("other_investigations") or []
    if other:
        _section(pdf, "Other investigations & procedures",
                 "\n".join(f"• {o.get('item_name')}: {o.get('result_summary') or o.get('status')}" for o in other))

    _section(pdf, "Condition at discharge", summary.get("condition_at_discharge"))
    _section(pdf, "Discharge medications", summary.get("discharge_medications"))
    _section(pdf, "Advice", summary.get("advice"))
    _section(pdf, "Follow-up", summary.get("follow_up"))
    pdf.set_font("NotoSans", "", 7)
    pdf.multi_cell(0, 4, "In an emergency, or if symptoms worsen, report to the hospital immediately.",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    _signature(pdf, summary.get("doctor_snapshot") or {}, summary.get("signed_at"), "Treating Consultant",
               extra=[f"Prepared by: {summary['prepared_by_name']}"] if summary.get("prepared_by_name") else None)
    return _footer(pdf, doc_label)


_CONSENT_TITLES = {"general_admission": "General consent for admission and treatment",
                   "surgical": "Informed consent for surgery", "anesthesia": "Informed consent for anaesthesia",
                   "high_risk_procedure": "Informed consent for a high-risk procedure",
                   "blood_transfusion": "Informed consent for blood transfusion",
                   "refusal_lama": "Refusal of treatment / leaving against medical advice"}


def render_consent_pdf(consent: dict) -> bytes:
    """Render a recorded informed consent from its row (text, signer, witness and
    the snapshots taken when it was recorded)."""
    if not consent.get("is_signed"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a recorded consent can be rendered")
    doc_label = f"Kriya Consent {str(consent.get('id', ''))[:8]}"
    pdf = _a4(doc_label)
    _letterhead_block(pdf, consent.get("letterhead_snapshot") or {}, "Hospital")
    pat = consent.get("patient_snapshot") or {}
    _title(pdf, (consent.get("title") or _CONSENT_TITLES.get(consent.get("consent_type"), "Consent")).upper())
    pdf.ln(1)
    _kv_rows(pdf, [("Patient", _person(pat)), ("MRN", pat.get("mrn")),
                   ("IP number", pat.get("admission_number")), ("Recorded", _ist_text(consent.get("signed_at"))),
                   ("Procedure", consent.get("procedure_name")),
                   ("Explained by", doctor_title((consent.get("doctor_snapshot") or {}).get("name"))
                    if consent.get("doctor_snapshot") else None)])
    _section(pdf, "Consent", consent.get("body_text"))
    _section(pdf, "Risks, benefits and alternatives explained", consent.get("risks_explained"))

    pdf.ln(4)
    rel = consent.get("signer_relationship") or "self"
    _kv_rows(pdf, [("Signed by", consent.get("signer_name")),
                   ("Relationship", "Patient (self)" if rel == "self" else rel.title()),
                   ("Witness", consent.get("witness_name")), ("Witness phone", consent.get("witness_phone"))])
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(93, 14, "Signature of patient / representative", border="B")
    pdf.cell(93, 14, "Signature of witness", border="B", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    pdf.set_font("NotoSans", "", 7)
    pdf.multi_cell(0, 4, f"Signature recorded by {consent.get('signed_by_name') or 'staff'} on "
                         f"{_ist_text(consent.get('signed_at'))} (IST). The signed paper / tablet form is kept on file.",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return _footer(pdf, doc_label)


_CERT_TITLES = {"medical_leave": "MEDICAL CERTIFICATE (LEAVE)", "fitness": "MEDICAL FITNESS CERTIFICATE",
                "travel_fitness": "FITNESS TO TRAVEL CERTIFICATE",
                "disability_assessment": "DISABILITY ASSESSMENT CERTIFICATE",
                "birth_intimation": "BIRTH INTIMATION", "death_intimation": "DEATH INTIMATION"}


def render_certificate_pdf(cert: dict) -> bytes:
    """Render an issued (or voided) certificate from its row and snapshots."""
    number = cert.get("certificate_number") or ""
    doc_label = f"Kriya {number or 'Certificate'}"
    pdf = _a4(doc_label)
    _letterhead_block(pdf, cert.get("letterhead_snapshot") or {}, "Clinic")
    if cert.get("status") == "void":
        _banner(pdf, f"VOID - this certificate was cancelled ({cert.get('void_reason') or 'no reason'}). It is not valid.")
    _title(pdf, _CERT_TITLES.get(cert.get("certificate_type"), "MEDICAL CERTIFICATE"))
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(0, 5, f"Certificate No: {number}    Date: {_ist_text(cert.get('created_at'))}", align="C",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pat = cert.get("patient_snapshot") or {}
    _kv_rows(pdf, [("Patient", _person(pat)), ("MRN", pat.get("mrn"))])

    pdf.set_font("NotoSans", "", 9)
    who = pat.get("name") or "the patient"
    kind = cert.get("certificate_type")
    if kind == "medical_leave":
        days = cert.get("rest_days_advised")
        text = (f"This is to certify that {who} was examined by me and is suffering from the condition stated below. "
                f"Rest is advised from {_ist_date(cert.get('start_date'))} to {_ist_date(cert.get('end_date'))}"
                f"{f' ({days} day' + ('s' if days != 1 else '') + ')' if days else ''}.")
    elif kind in ("fitness", "travel_fitness"):
        text = (f"This is to certify that I have examined {who} and, on the basis of my examination, find them "
                f"{'fit to travel' if kind == 'travel_fitness' else 'medically fit'}"
                f"{' from ' + _ist_date(cert.get('start_date')) if cert.get('start_date') else ''}.")
    else:
        text = f"This is to certify the following in respect of {who}, as recorded by me."
    pdf.multi_cell(0, 5, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    _section(pdf, "Diagnosis / findings", cert.get("diagnosis_text"))
    _section(pdf, "Remarks", cert.get("remarks"))
    _signature(pdf, cert.get("doctor_snapshot") or {}, cert.get("created_at"), "Doctor", verb="Issued and signed")
    pdf.set_font("NotoSans", "", 7)
    pdf.ln(2)
    pdf.multi_cell(0, 4, "Verify this certificate with the issuing clinic quoting its number.",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return _footer(pdf, doc_label)


_INJURY = {"rto": "Road traffic accident", "physical_assault": "Physical assault", "poisoning": "Poisoning",
           "burns": "Burns", "fall_from_height": "Fall from height", "workplace": "Workplace injury",
           "gunshot_stab": "Gunshot / stab injury", "hanging": "Hanging", "other": "Other"}


def render_mlc_intimation_pdf(mlc: dict) -> bytes:
    """Medico-legal case intimation to the police (migration 121), from the MLC
    row and the snapshots taken when it was recorded. An unlocked MLC prints
    as a DRAFT; only a locked one is the statutory document."""
    number = mlc.get("mlc_number") or ""
    doc_label = f"Kriya {number or 'MLC'}"
    pdf = _a4(doc_label)
    _letterhead_block(pdf, mlc.get("letterhead_snapshot") or {}, "Hospital")
    if not mlc.get("is_locked"):
        _banner(pdf, "DRAFT - not yet locked by the doctor. Not valid as a police intimation.")
    _title(pdf, "MEDICO-LEGAL CASE - INTIMATION TO POLICE")
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(0, 5, f"MLC No: {number}    ER No: {(mlc.get('patient_snapshot') or {}).get('visit_number') or '—'}",
             align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pat = mlc.get("patient_snapshot") or {}
    _kv_rows(pdf, [("To", f"Station House Officer, {mlc.get('police_station_name') or '—'}"),
                   ("Intimated", _ist_text(mlc.get("police_intimation_time"))),
                   ("Officer", " ".join(x for x in (mlc.get("officer_name"), f"(Belt/Badge {mlc['officer_badge']})"
                                                    if mlc.get("officer_badge") else None) if x) or None),
                   ("Patient", _person(pat) + (" - identity not established" if pat.get("is_unknown") else "")),
                   ("MRN", pat.get("mrn")), ("Arrived", _ist_text(pat.get("arrived_at"))),
                   ("Brought by", " ".join(x for x in (pat.get("brought_by_name"), pat.get("brought_by_phone")) if x) or None),
                   ("Arrival", (pat.get("arrival_mode") or "").replace("_", " ").title() or None)])
    pdf.set_font("NotoSans", "", 9)
    pdf.multi_cell(0, 5, f"Sir / Madam, this is to inform you that the above patient was brought to this hospital and "
                         f"examined by me. The case has been registered as a medico-legal case for necessary action.",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    _kv_rows(pdf, [("Nature", _INJURY.get(mlc.get("injury_type"), mlc.get("injury_type"))),
                   ("Incident at", mlc.get("incident_place")), ("Incident time", _ist_text(mlc.get("incident_time"))
                                                                if mlc.get("incident_time") else None),
                   ("Dying decl.", "Yes - to be recorded by the Magistrate" if mlc.get("is_dying_declaration") else "No")])
    _section(pdf, "Injuries / findings", mlc.get("injury_description"))
    _signature(pdf, mlc.get("doctor_snapshot") or {}, mlc.get("locked_at") or mlc.get("created_at"),
               "Casualty Medical Officer", verb="Locked and signed" if mlc.get("is_locked") else "Drafted")
    pdf.ln(4)
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(93, 12, "Received by (police): name, belt no., signature", border="B")
    pdf.cell(93, 12, "Date / time of receipt", border="B", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return _footer(pdf, doc_label)
