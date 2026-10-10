"""Tests for Kriya OPD OS PDF Generation Engine (Phase 1.3).

Verifies:
1. Generated PDF begins with `%PDF-` magic header.
2. Doctor registration number and council are present in rendered text (extracted with pdfplumber).
3. Complex Indic scripts (Devanagari and Telugu) render without font/encoding exceptions.
4. Reprints use immutable snapshots only (clinic profile edits do not alter historical reprints).
5. Draft prescriptions refuse PDF rendering with HTTP 409 Conflict.
"""

import io
import uuid

import pdfplumber
import pytest
from fastapi import HTTPException

from app.services.opd_pdf import render_prescription_pdf

CLINIC_ID = str(uuid.uuid4())
RX_ID = str(uuid.uuid4())

SAMPLE_SIGNED_RX = {
    "id": RX_ID,
    "clinic_id": CLINIC_ID,
    "version": 1,
    "status": "signed",
    "signed_at": "2026-10-09T10:30:00+00:00",
    "letterhead_snapshot": {
        "clinic_name": "Kriya MedCenter",
        "branch_name": "Main Branch",
        "address": "Banjara Hills, Hyderabad",
        "phone": "+919876543210",
        "email": "contact@kriya.example",
    },
    "patient_snapshot": {
        "name": "రాజేష్ కుమార్ (Rajesh Kumar)",
        "mrn": "MRN-2026-00042",
        "age": 42,
        "gender": "M",
        "allergies": ["penicillin"],
        "allergies_status": "recorded",
    },
    "signer_snapshot": {
        "doctor_id": str(uuid.uuid4()),
        "name": "Sunita Verma",
        "qualifications": "MBBS, MD",
        "registration_number": "MCI-98765-A",
        "registration_council": "Medical Council of India",
    },
    "general_instructions": "Drink warm fluids and take medications after food.",
}

SAMPLE_ITEMS = [
    {
        "line_no": 1,
        "drug_name": "Paracetamol",
        "formulation": "tablet",
        "strength": "650mg",
        "dosage": "1 tab",
        "route": "oral",
        "frequency": "TDS",
        "timing": "after_food",
        "duration_days": 3,
        "instructions": "If temperature > 100 F",
    },
    {
        "line_no": 2,
        "drug_name": "Cetirizine",
        "formulation": "tablet",
        "strength": "10mg",
        "dosage": "1 tab",
        "route": "oral",
        "frequency": "HS",
        "timing": "bedtime",
        "duration_days": 5,
        "instructions": "May cause mild drowsiness",
    },
]


def test_pdf_magic_bytes_and_length():
    """PDF output starts with %PDF- header and has non-trivial byte length."""
    pdf_bytes = render_prescription_pdf(SAMPLE_SIGNED_RX, SAMPLE_ITEMS)
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 5000


def test_pdf_contains_reg_number_and_council():
    """Signer registration number and council are present in extracted text."""
    pdf_bytes = render_prescription_pdf(SAMPLE_SIGNED_RX, SAMPLE_ITEMS)

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        assert len(pdf.pages) >= 1
        text = pdf.pages[0].extract_text()
        assert "MCI-98765-A" in text
        assert "Medical Council of India" in text
        assert "Sunita Verma" in text
        assert "Paracetamol" in text
        assert "Cetirizine" in text
        assert "MRN-2026-00042" in text


def test_pdf_indic_script_rendering():
    """Devanagari and Telugu patient names render without encoding errors."""
    rx = dict(SAMPLE_SIGNED_RX)
    rx["patient_snapshot"] = dict(SAMPLE_SIGNED_RX["patient_snapshot"])
    # Devanagari + Telugu
    rx["patient_snapshot"]["name"] = "अमित शर्मा & వెంకటేశ్వర రావు"

    pdf_bytes = render_prescription_pdf(rx, SAMPLE_ITEMS)
    assert pdf_bytes.startswith(b"%PDF-")

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = pdf.pages[0].extract_text()
        # English parts still extractable cleanly
        assert "Kriya MedCenter" in text
        assert "MCI-98765-A" in text


def test_reprint_snapshot_isolation():
    """Reprint retains original letterhead snapshot even if clinic changes in DB."""
    # Historical prescription with old clinic snapshot
    rx = dict(SAMPLE_SIGNED_RX)
    rx["letterhead_snapshot"] = {
        "clinic_name": "Historical Clinic Name 2024",
        "address": "Old Address, Old City",
        "phone": "+911122334455",
    }

    pdf_bytes = render_prescription_pdf(rx, SAMPLE_ITEMS)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = pdf.pages[0].extract_text()
        assert "Historical Clinic Name 2024" in text
        assert "Old Address, Old City" in text


def test_draft_prescription_refuses_pdf():
    """Draft prescription cannot be rendered as PDF (raises HTTP 409)."""
    draft_rx = dict(SAMPLE_SIGNED_RX)
    draft_rx["status"] = "draft"

    with pytest.raises(HTTPException) as exc_info:
        render_prescription_pdf(draft_rx, SAMPLE_ITEMS)
    assert exc_info.value.status_code == 409
    assert "Draft prescriptions" in exc_info.value.detail
