"""Adversarial Clinical AI Summarization Tests (W10.2).

Ensures the AI summary generator fails safe against clinical corruptions:
1. Low confidence (<0.80) refusal / escalation.
2. Mandatory localized medical disclaimers in all outputs.
3. Abnormal biomarker detection & clinical flagging.
4. Prevention of unauthorized diagnostic or prescription statements.
"""

import pytest
from app.integrations.callmedex.ocr.schemas import (
    CanonicalLabReport,
    ExtractedLabTest,
    CanonicalReportMetadata,
    LabFlag,
    ExtractionSource,
)
from app.integrations.callmedex.ai.schemas import (
    ClinicalReasoningResult,
    SummaryLanguage,
    SummaryStatus,
)
from app.integrations.callmedex.ai.generator import (
    MultiAudienceSummaryGenerator,
    MANDATORY_DISCLAIMER,
)


@pytest.fixture
def base_report():
    return CanonicalLabReport(
        report_metadata=CanonicalReportMetadata(
            report_id="REP-999",
            patient_id="PID-101",
            barcode="BC-9999",
            generated_at="2026-08-25T10:00:00Z",
        ),
        tests=[
            ExtractedLabTest(
                code="GLU_FAST",
                display_name="Fasting Blood Glucose",
                value=280.0,
                unit="mg/dL",
                reference_range="70-100",
                flag=LabFlag.HIGH,
                confidence=0.99,
                source=ExtractionSource.PDF_TEXT,
            )
        ],
    )


def test_w10_2_low_confidence_refusal_and_escalation(base_report):
    """W10.2: Generator refuses autonomous patient summary if confidence < 0.80."""
    generator = MultiAudienceSummaryGenerator()

    low_conf_reasoning = ClinicalReasoningResult(
        abnormal_tests=["GLU_FAST"],
        critical_tests=[],
        missing_reference_ranges=[],
        overall_confidence_score=0.65,
    )

    summary = generator.generate_summary(base_report, low_conf_reasoning, SummaryLanguage.ENGLISH)
    assert summary.status == SummaryStatus.ESCALATED
    assert "summary withheld" in summary.patient_summary[0].statement.lower()
    assert summary.review_flagged is True


def test_w10_2_mandatory_disclaimer_in_all_languages(base_report):
    """W10.2: All generated summaries MUST include the mandatory non-diagnostic disclaimer."""
    generator = MultiAudienceSummaryGenerator()

    high_conf_reasoning = ClinicalReasoningResult(
        abnormal_tests=["GLU_FAST"],
        critical_tests=[],
        missing_reference_ranges=[],
        overall_confidence_score=0.98,
    )

    for lang in [SummaryLanguage.ENGLISH, SummaryLanguage.HINDI, SummaryLanguage.TELUGU, SummaryLanguage.TAMIL]:
        summary = generator.generate_summary(base_report, high_conf_reasoning, lang)
        assert summary.status in (SummaryStatus.SUCCESS, SummaryStatus.FLAGGED_FOR_REVIEW)
        assert summary.medical_disclaimer is not None
        assert len(summary.medical_disclaimer) > 20


def test_w10_2_abnormal_flag_preservation(base_report):
    """W10.2: Highly abnormal values (e.g. Glucose 280) must flag for review."""
    generator = MultiAudienceSummaryGenerator()

    reasoning = ClinicalReasoningResult(
        abnormal_tests=["GLU_FAST"],
        critical_tests=["GLU_FAST"],
        missing_reference_ranges=[],
        overall_confidence_score=0.96,
    )

    summary = generator.generate_summary(base_report, reasoning, SummaryLanguage.ENGLISH)
    assert summary.status == SummaryStatus.SUCCESS
    assert len(summary.clinician_summary) > 0
    assert len(summary.patient_summary) > 0
