"""Phase 6 AI Medical Summary Schemas & Domain Models."""

from enum import Enum
from typing import List
from pydantic import BaseModel, Field


class SummaryLanguage(str, Enum):
    """Supported target languages for summary generation."""
    ENGLISH = "en"
    HINDI = "hi"
    TELUGU = "te"
    TAMIL = "ta"


class SummaryStatus(str, Enum):
    """Overall summary processing routing status."""
    SUCCESS = "success"
    FLAGGED_FOR_REVIEW = "flagged_for_review"
    ESCALATED = "escalated"


class StatementProvenance(BaseModel):
    """Generated summary statement linked to supporting canonical test codes."""

    statement: str = Field(..., description="Generated summary statement narrative")
    supported_by: List[str] = Field(..., description="Array of canonical test codes backing this statement (e.g. ['HB'])")


class ClinicalReasoningResult(BaseModel):
    """Layer 1: Clinical Reasoning Output.

    Strictly structured clinical categorization.
    Contains ZERO patient or clinician prose/language.
    """

    abnormal_tests: List[str] = Field(default_factory=list, description="Test codes flagged as high or low")
    critical_tests: List[str] = Field(default_factory=list, description="Test codes flagged as critical")
    missing_reference_ranges: List[str] = Field(default_factory=list, description="Test codes missing reference bounds")
    overall_confidence_score: float = Field(..., ge=0.0, le=1.0, description="Overall extraction & reasoning confidence score")


class MultiAudienceSummaryReport(BaseModel):
    """Layer 2: Multi-Audience Summary Output Contract.

    Includes Patient Summary, Clinician Summary, Provenance, Medical Disclaimer, and Confidence Routing.
    """

    patient_summary: List[StatementProvenance] = Field(..., description="Patient-accessible explanatory statements")
    clinician_summary: List[StatementProvenance] = Field(..., description="Concise technical statements for clinicians")
    medical_disclaimer: str = Field(..., description="Mandatory non-diagnostic informational disclaimer")
    language: SummaryLanguage = Field(default=SummaryLanguage.ENGLISH, description="Target language code")
    status: SummaryStatus = Field(..., description="Confidence routing status (success, flagged_for_review, escalated)")
    overall_confidence: float = Field(..., ge=0.0, le=1.0, description="Overall report confidence score")
    review_flagged: bool = Field(default=False, description="True if summary requires clinical review")
