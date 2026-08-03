"""Phase 5 Canonical OCR Pipeline & Structured Lab Value Extraction Test Suite (Phase R3)."""

import os
import pytest
from app.integrations.callmedex.ocr.schemas import (
    CanonicalLabReport,
    ExtractedLabTest,
    ExtractionSource,
    LabFlag,
)
from app.integrations.callmedex.ocr.normalizer import normalize_lab_test_name
from app.integrations.callmedex.ocr.validator import validate_and_deduplicate_tests
from app.integrations.callmedex.ocr.engine import CanonicalOCRPipeline
from app.integrations.callmedex.api.exceptions import ValidationError

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def test_lab_test_normalizer_variants():
    """Verify normalization of variant test names to canonical codes."""
    variants = ["Hb", "HEMOGLOBIN", "Hemoglobin", "Haemoglobin", "hgb"]
    for var in variants:
        code, display = normalize_lab_test_name(var)
        assert code == "HB"
        assert display == "Hemoglobin"

    wbc_code, wbc_disp = normalize_lab_test_name("Total Leukocyte Count")
    assert wbc_code == "WBC"

    creatinine_code, _ = normalize_lab_test_name("Serum Creatinine")
    assert creatinine_code == "CREATININE"


def test_validator_impossible_values_and_deduplication():
    """Verify validation flags impossible numeric values and deduplicates tests."""
    tests = [
        ExtractedLabTest(
            code="HB",
            display_name="Hemoglobin",
            value=13.6,
            unit="g/dL",
            reference_range="13.0-17.0",
            flag=LabFlag.NORMAL,
            confidence=0.99,
            source=ExtractionSource.PDF_TEXT,
            page_number=1,
        ),
        ExtractedLabTest(
            code="HB",
            display_name="Hemoglobin Duplicate",
            value=14.0,
            unit="g/dL",
            reference_range="13.0-17.0",
            flag=LabFlag.NORMAL,
            confidence=0.85,
            source=ExtractionSource.PDF_TEXT,
            page_number=1,
        ),
        ExtractedLabTest(
            code="HB",
            display_name="Hemoglobin Impossible",
            value=999.0,  # Impossible value > 30
            unit="g/dL",
            reference_range="13.0-17.0",
            flag=LabFlag.NORMAL,
            confidence=0.99,
            source=ExtractionSource.PDF_TEXT,
            page_number=1,
        ),
    ]

    validated, warnings = validate_and_deduplicate_tests(tests)
    assert len(validated) == 1
    assert validated[0].code == "HB"
    assert validated[0].value == 13.6
    assert len(warnings) >= 1


def test_canonical_ocr_pipeline_native_pdf_fixture():
    """Verify Canonical OCR pipeline processing against real native text PDF fixture using pdfplumber."""
    fixture_path = os.path.join(FIXTURES_DIR, "native_text_report.pdf")
    with open(fixture_path, "rb") as f:
        pdf_bytes = f.read()

    pipeline = CanonicalOCRPipeline()
    report = pipeline.process_pdf(
        pdf_bytes=pdf_bytes,
        report_id="REP-FIXTURE-001",
        patient_id="PAT-FIXTURE-500",
        barcode="260700009225",
    )

    assert isinstance(report, CanonicalLabReport)
    assert report.report_metadata.report_id == "REP-FIXTURE-001"
    assert report.report_metadata.patient_id == "PAT-FIXTURE-500"
    assert len(report.tests) >= 3

    hb_test = [t for t in report.tests if t.code == "HB"][0]
    assert hb_test.code == "HB"
    assert hb_test.value == 13.6
    assert hb_test.unit == "g/dL"
    assert hb_test.reference_range == "13.0-17.0"
    assert hb_test.flag == LabFlag.NORMAL
    assert hb_test.confidence == 0.99
    assert hb_test.source == ExtractionSource.PDF_TEXT

    # Verify ZERO AI summary fields in Canonical JSON Output
    report_dict = report.model_dump()
    assert "summary" not in report_dict
    assert "medical_advice" not in report_dict
    assert "interpretation" not in report_dict


def test_canonical_ocr_pipeline_empty_pdf_rejection():
    """Verify OCR pipeline rejects empty PDF bytes."""
    pipeline = CanonicalOCRPipeline()
    with pytest.raises(ValidationError) as exc:
        pipeline.process_pdf(b"", "R1", "P1", "B1")
    assert "empty PDF bytes" in str(exc.value)


def test_canonical_ocr_pipeline_corrupted_header_rejection():
    """Verify OCR pipeline rejects corrupted non-PDF bytes."""
    pipeline = CanonicalOCRPipeline()
    with pytest.raises(ValidationError) as exc:
        pipeline.process_pdf(b"NOT_A_REAL_PDF_HEADER", "R1", "P1", "B1")
    assert "Invalid PDF header signature" in str(exc.value)
