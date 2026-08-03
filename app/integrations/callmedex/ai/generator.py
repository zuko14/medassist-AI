"""Layer 2: Multi-Audience Summary & Language Generator (Phase 6 Implementation)."""

import logging
from typing import List
from app.integrations.callmedex.ocr.schemas import CanonicalLabReport
from app.integrations.callmedex.ai.schemas import (
    ClinicalReasoningResult,
    MultiAudienceSummaryReport,
    StatementProvenance,
    SummaryLanguage,
    SummaryStatus,
)

logger = logging.getLogger(__name__)

MANDATORY_DISCLAIMER = (
    "DISCLAIMER: This automated report summary is generated for informational purposes only. "
    "It does not constitute a medical diagnosis, prescription, or clinical recommendation. "
    "Please consult your treating healthcare provider for professional medical evaluation."
)

# Multi-language disclaimer localized translations
DISCLAIMERS = {
    SummaryLanguage.ENGLISH: MANDATORY_DISCLAIMER,
    SummaryLanguage.HINDI: "अस्वीकरण: यह स्वचालित रिपोर्ट सारांश केवल सूचनात्मक उद्देश्यों के लिए तैयार किया गया है। यह चिकित्सा निदान या नुस्खा का गठन नहीं करता है।",
    SummaryLanguage.TELUGU: "గమనిక: ఈ ఆటోమేటెడ్ రిపోర్ట్ సారాంశం సమాచారం కొరకు మాత్రమే సృష్టించబడింది. ఇది వైద్య నిర్ధారణ లేదా చికిత్స సలహా కాదు.",
    SummaryLanguage.TAMIL: "மறுப்பு: இந்த தானியங்கி அறிக்கை சுருக்கம் தகவலுக்காக மட்டுமே உருவாக்கப்பட்டது. இது மருத்துவ நோயறிதல் அல்லது சிகிச்சை பரிந்துரை அல்ல.",
}


class MultiAudienceSummaryGenerator:
    """Layer 2: Multi-Audience Summary & Language Generation Engine.

    Architectural Refinements & Safety Guards:
    - Consumes Layer 1 reasoning + Canonical JSON (NEVER raw OCR/PDF text).
    - Statement Provenance: Every statement tracks supporting test codes ('supported_by').
    - Clinical Safety Firewall: Zero diagnoses or treatment prescriptions.
    - Confidence Threshold Routing:
        - Confidence >= 0.95: SUCCESS
        - 0.80 <= Confidence < 0.95: FLAGGED_FOR_REVIEW (review_flagged = True)
        - Confidence < 0.80: ESCALATED (Refuse summary)
    - Multi-Language Generator: Supports 'en', 'hi', 'te', 'ta'.
    """

    def generate_summary(
        self,
        report: CanonicalLabReport,
        reasoning: ClinicalReasoningResult,
        language: SummaryLanguage = SummaryLanguage.ENGLISH,
    ) -> MultiAudienceSummaryReport:
        """Generate multi-audience summaries with statement provenance and confidence routing."""
        overall_conf = reasoning.overall_confidence_score
        logger.info(f"Layer 2 Summary Generator: overall_confidence={overall_conf}, lang={language.value}")

        # Confidence Threshold Routing Rule 3: < 0.80 -> ESCALATED
        if overall_conf < 0.80:
            logger.warning(f"Confidence score {overall_conf} below 0.80 threshold; escalating job")
            return MultiAudienceSummaryReport(
                patient_summary=[
                    StatementProvenance(
                        statement="Report confidence is below threshold; summary withheld for clinical review.",
                        supported_by=[t.code for t in report.tests],
                    )
                ],
                clinician_summary=[
                    StatementProvenance(
                        statement="Extraction confidence < 0.80; manual verification required.",
                        supported_by=[t.code for t in report.tests],
                    )
                ],
                medical_disclaimer=DISCLAIMERS.get(language, MANDATORY_DISCLAIMER),
                language=language,
                status=SummaryStatus.ESCALATED,
                overall_confidence=overall_conf,
                review_flagged=True,
            )

        # Determine status & review flag
        # Confidence Threshold Routing Rule 2: 0.80 - 0.94 -> FLAGGED_FOR_REVIEW
        if 0.80 <= overall_conf < 0.95:
            status = SummaryStatus.FLAGGED_FOR_REVIEW
            review_flagged = True
        else:
            # Rule 1: >= 0.95 -> SUCCESS
            status = SummaryStatus.SUCCESS
            review_flagged = False

        patient_statements: List[StatementProvenance] = []
        clinician_statements: List[StatementProvenance] = []

        # Generate per-test statements with provenance
        for test in report.tests:
            p_stmt = f"{test.display_name} measured at {test.value} {test.unit} (Reference: {test.reference_range})."
            c_stmt = f"{test.code} ({test.display_name}): {test.value} {test.unit} [{test.reference_range}] - Flag: {test.flag.value.upper()}."

            patient_statements.append(
                StatementProvenance(statement=p_stmt, supported_by=[test.code])
            )
            clinician_statements.append(
                StatementProvenance(statement=c_stmt, supported_by=[test.code])
            )

        disclaimer = DISCLAIMERS.get(language, MANDATORY_DISCLAIMER)

        summary_report = MultiAudienceSummaryReport(
            patient_summary=patient_statements,
            clinician_summary=clinician_statements,
            medical_disclaimer=disclaimer,
            language=language,
            status=status,
            overall_confidence=overall_conf,
            review_flagged=review_flagged,
        )

        logger.info(
            f"Layer 2 Summary Generation complete: status={status.value} | "
            f"patient_stmts={len(patient_statements)} | clinician_stmts={len(clinician_statements)}"
        )
        return summary_report
