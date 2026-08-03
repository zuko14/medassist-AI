"""Canonical OCR Pipeline Pydantic Schemas & Domain Models (Phase 5 Implementation)."""

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class ExtractionSource(str, Enum):
    """Source of text extraction for a specific field."""
    PDF_TEXT = "pdf_text"
    OCR_ENGINE = "ocr_engine"
    FALLBACK = "fallback"


class LabFlag(str, Enum):
    """Canonical lab result flag indicator."""
    NORMAL = "normal"
    HIGH = "high"
    LOW = "low"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class ExtractedLabTest(BaseModel):
    """Canonical schema for an individual extracted laboratory test result."""

    code: str = Field(..., description="Standardized canonical test code (e.g., HB, WBC, CREATININE)")
    display_name: str = Field(..., description="Canonical display name of the laboratory test")
    raw_test_name: Optional[str] = Field(None, description="Original un-normalized test name text from PDF")
    value: float = Field(..., description="Extracted numerical or quantitative test value")
    unit: str = Field(..., description="Standardized unit of measurement (e.g., g/dL, mg/dL)")
    reference_range: str = Field(..., description="Reference interval or normal range text (e.g., 13.0-17.0)")
    flag: LabFlag = Field(default=LabFlag.NORMAL, description="Flag indicator (normal, high, low, critical)")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Per-field confidence score between 0.0 and 1.0")
    source: ExtractionSource = Field(..., description="Extraction source method (pdf_text or ocr_engine)")
    page_number: int = Field(default=1, ge=1, description="Page number in PDF where test was detected")


class CanonicalReportMetadata(BaseModel):
    """Metadata header for canonical laboratory report."""

    report_id: str = Field(..., description="Unique laboratory report tracking identifier")
    patient_id: str = Field(..., description="Patient identification number or EMR ID")
    barcode: str = Field(..., description="Sample or accession barcode identifier")
    processing_center_id: str = Field(default="default-lab", description="Processing laboratory center ID")
    generated_at: str = Field(..., description="Timestamp of report generation or processing")


class CanonicalLabReport(BaseModel):
    """Master Canonical JSON schema for structured laboratory extraction.

    Guarantees 0% medical advice or AI summary generation.
    Strictly contains structured quantitative laboratory test values.
    """

    report_metadata: CanonicalReportMetadata = Field(..., description="Header metadata for report")
    tests: List[ExtractedLabTest] = Field(default_factory=list, description="Array of extracted canonical lab tests")
