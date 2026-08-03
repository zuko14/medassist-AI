"""Phase 6 AI Medical Summary & Multi-Audience Insights Test Suite."""

import pytest
from app.integrations.callmedex.ocr.schemas import (
    CanonicalLabReport,
    CanonicalReportMetadata,
    ExtractedLabTest,
    ExtractionSource,
    LabFlag,
)
from app.integrations.callmedex.ai.schemas import (
    SummaryLanguage,
    SummaryStatus,
    MultiAudienceSummaryReport,
)
from app.integrations.callmedex.ai.reasoning import ClinicalReasoningEngine
from app.integrations.callmedex.ai.generator import MultiAudienceSummaryGenerator


@pytest.fixture
def sample_canonical_report() -> CanonicalLabReport:
    """Fixture providing sample CanonicalLabReport JSON."""
    return CanonicalLabReport(
        report_metadata=CanonicalReportMetadata(
            report_id="REP-2026-999",
            patient_id="PAT-50380",
            barcode="260700009225",
            processing_center_id="visakha-multispeciality-clinics",
            generated_at="2026-08-02T13:03:30Z",
        ),
        tests=[
            ExtractedLabTest(
                code="HB",
                display_name="Hemoglobin",
                value=13.6,
                unit="g/dL",
                reference_range="13.0-17.0",
                flag=LabFlag.NORMAL,
                confidence=0.998,
                source=ExtractionSource.PDF_TEXT,
                page_number=1,
            ),
            ExtractedLabTest(
                code="WBC",
                display_name="White Blood Cell Count",
                value=7500.0,
                unit="/uL",
                reference_range="4000-11000",
                flag=LabFlag.NORMAL,
                confidence=0.985,
                source=ExtractionSource.PDF_TEXT,
                page_number=1,
            ),
            ExtractedLabTest(
                code="MANTOUX",
                display_name="Mantoux Test",
                value=12.0,
                unit="mm",
                reference_range="0-5",
                flag=LabFlag.HIGH,
                confidence=0.960,
                source=ExtractionSource.PDF_TEXT,
                page_number=1,
            ),
        ],
    )


def test_layer1_clinical_reasoning_engine(sample_canonical_report):
    """Verify Layer 1 Clinical Reasoning Engine categorization without prose text."""
    engine = ClinicalReasoningEngine()
    reasoning = engine.analyze_report(sample_canonical_report)

    assert reasoning.overall_confidence_score > 0.95
    assert "MANTOUX" in reasoning.abnormal_tests
    assert "HB" not in reasoning.abnormal_tests
    assert isinstance(reasoning.missing_reference_ranges, list)


def test_layer2_summary_generator_provenance_and_safety(sample_canonical_report):
    """Verify Layer 2 summary generation, statement provenance, medical disclaimer, and safety rules."""
    engine = ClinicalReasoningEngine()
    generator = MultiAudienceSummaryGenerator()

    reasoning = engine.analyze_report(sample_canonical_report)
    summary_report = generator.generate_summary(sample_canonical_report, reasoning)

    assert isinstance(summary_report, MultiAudienceSummaryReport)
    assert summary_report.status == SummaryStatus.SUCCESS
    assert summary_report.review_flagged is False

    # Check Patient Summary & Provenance
    assert len(summary_report.patient_summary) == 3
    hb_stmt = summary_report.patient_summary[0]
    assert "Hemoglobin" in hb_stmt.statement
    assert hb_stmt.supported_by == ["HB"]

    # Check Clinician Summary & Provenance
    assert len(summary_report.clinician_summary) == 3
    mantoux_stmt = summary_report.clinician_summary[2]
    assert "MANTOUX" in mantoux_stmt.statement
    assert "HIGH" in mantoux_stmt.statement
    assert mantoux_stmt.supported_by == ["MANTOUX"]

    # Check Medical Disclaimer
    assert "DISCLAIMER:" in summary_report.medical_disclaimer
    assert "not constitute a medical diagnosis" in summary_report.medical_disclaimer


def test_confidence_threshold_routing_high_medium_low(sample_canonical_report):
    """Verify confidence routing logic for high (>=0.95), medium (0.80-0.94), and low (<0.80)."""
    engine = ClinicalReasoningEngine()
    generator = MultiAudienceSummaryGenerator()

    reasoning = engine.analyze_report(sample_canonical_report)

    # High Confidence (>= 0.95) -> SUCCESS
    reasoning.overall_confidence_score = 0.98
    res_high = generator.generate_summary(sample_canonical_report, reasoning)
    assert res_high.status == SummaryStatus.SUCCESS
    assert res_high.review_flagged is False

    # Medium Confidence (0.80 - 0.94) -> FLAGGED_FOR_REVIEW
    reasoning.overall_confidence_score = 0.88
    res_med = generator.generate_summary(sample_canonical_report, reasoning)
    assert res_med.status == SummaryStatus.FLAGGED_FOR_REVIEW
    assert res_med.review_flagged is True

    # Low Confidence (< 0.80) -> ESCALATED
    reasoning.overall_confidence_score = 0.72
    res_low = generator.generate_summary(sample_canonical_report, reasoning)
    assert res_low.status == SummaryStatus.ESCALATED
    assert res_low.review_flagged is True
    assert "withheld" in res_low.patient_summary[0].statement


def test_multi_language_summary_generation(sample_canonical_report):
    """Verify multi-language summary generation (en, hi, te, ta)."""
    engine = ClinicalReasoningEngine()
    generator = MultiAudienceSummaryGenerator()
    reasoning = engine.analyze_report(sample_canonical_report)

    languages = [SummaryLanguage.ENGLISH, SummaryLanguage.HINDI, SummaryLanguage.TELUGU, SummaryLanguage.TAMIL]
    for lang in languages:
        report = generator.generate_summary(sample_canonical_report, reasoning, language=lang)
        assert report.language == lang
        assert report.medical_disclaimer is not None
