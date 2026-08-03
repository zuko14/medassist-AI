"""Layer 1: Clinical Reasoning Engine (Phase 6 Implementation)."""

import logging
from typing import List
from app.integrations.callmedex.ocr.schemas import CanonicalLabReport, LabFlag
from app.integrations.callmedex.ai.schemas import ClinicalReasoningResult

logger = logging.getLogger(__name__)


class ClinicalReasoningEngine:
    """Layer 1: Clinical Reasoning Layer.

    Consumes CanonicalLabReport structured JSON ONLY.
    Outputs structured ClinicalReasoningResult (abnormal tests, critical tests, confidence score).
    Strictly contains ZERO patient or doctor prose/language.
    """

    def analyze_report(self, report: CanonicalLabReport) -> ClinicalReasoningResult:
        """Perform structured clinical evaluation on canonical lab report JSON."""
        abnormal: List[str] = []
        critical: List[str] = []
        missing_ref: List[str] = []
        confidence_scores: List[float] = []

        for test in report.tests:
            confidence_scores.append(test.confidence)

            if not test.reference_range or test.reference_range == "":
                missing_ref.append(test.code)

            if test.flag == LabFlag.CRITICAL:
                critical.append(test.code)
                abnormal.append(test.code)
            elif test.flag in [LabFlag.HIGH, LabFlag.LOW]:
                abnormal.append(test.code)

        overall_confidence = (
            sum(confidence_scores) / len(confidence_scores)
            if confidence_scores
            else 0.0
        )

        result = ClinicalReasoningResult(
            abnormal_tests=abnormal,
            critical_tests=critical,
            missing_reference_ranges=missing_ref,
            overall_confidence_score=round(overall_confidence, 4),
        )

        logger.info(
            f"Layer 1 Clinical Reasoning Complete: abnormal={len(abnormal)} | "
            f"critical={len(critical)} | confidence={result.overall_confidence_score}"
        )
        return result
