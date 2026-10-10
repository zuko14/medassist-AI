"""Kriya OPD OS PDF Generation Engine (Phase 1.3).

Renders immutable clinical and financial documents using fpdf2 and uharfbuzz
with bundled Noto TTF fonts (Latin, Devanagari, Telugu).
Renders ONLY snapshots stored on the row for 100% deterministic reprints.
Zero-LLM clinical safety compliant.
"""

import os
import warnings
from datetime import datetime
from typing import Any, Optional

warnings.filterwarnings("ignore", category=UserWarning, module="fpdf")

from fastapi import HTTPException, status
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.utils.helpers import doctor_title

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

        # Set fallback fonts for Indic script rendering
        fallbacks = [f for f in ["NotoDevanagari", "NotoTelugu"] if f in self.fonts]
        if fallbacks:
            self.set_fallback_fonts(fallbacks)


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
    clinic_phone = (clinic or {}).get("phone") or (clinic or {}).get("whatsapp_number") or ""
    clinic_address = (clinic or {}).get("address") or ""

    pdf.set_font("NotoSans", "B", 14)
    pdf.set_text_color(24, 43, 73)  # Navy
    pdf.cell(110, 7, clinic_name, border=0, new_x=XPos.RIGHT, new_y=YPos.TOP)

    # Right side: Invoice title & badge
    pdf.set_font("NotoSans", "B", 12)
    pdf.set_text_color(16, 120, 70)  # Green
    status_str = (invoice.get("status") or "ISSUED").upper()
    pdf.cell(76, 7, f"TAX INVOICE [{status_str}]", border=0, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

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

    col_w = [10, 80, 24, 16, 26, 30]
    headers = ["#", "Item Description", "Type", "Qty", "Price (INR)", "Total (INR)"]
    for w, h in zip(col_w, headers):
        align = "R" if h in ("Qty", "Price (INR)", "Total (INR)") else "L"
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
    balance_paise = max(0, total_paise - paid_paise)

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

    # Footer note
    pdf.set_y(-15)
    pdf.set_font("NotoSans", "", 7)
    pdf.set_text_color(140, 140, 140)
    pdf.cell(0, 5, f"{doc_label} • Generated by Kriya OPD OS • Thank you", border=0, align="C")

    return bytes(pdf.output())

