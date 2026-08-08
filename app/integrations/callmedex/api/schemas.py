"""CallMedex Integration Data Models & Pydantic Schemas (Phase 2 Contract)."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4
from pydantic import BaseModel, Field, ConfigDict


# Enums
class ConnectorType(str, Enum):
    MOCDOC = "mocdoc"
    CRELIO = "crelio"
    CLOUDLIMS = "cloudlims"
    CUSTOM = "custom"


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


class ReportType(str, Enum):
    LABORATORY = "Laboratory"
    RADIOLOGY = "Radiology"
    PATHOLOGY = "Pathology"
    GENERAL = "General"


# Shared Domain Models
class PatientIdentity(BaseModel):
    """Patient identity model used across integration payloads."""

    patient_phone: str = Field(
        ..., description="Patient phone number (E.164 format e.g. +919876543210)"
    )
    patient_name: str = Field(..., description="Full patient name")
    patient_mrn: Optional[str] = Field(
        None, description="Medical Record Number / Hospital Patient ID"
    )

    model_config = ConfigDict(extra="ignore")


class ReportMetadata(BaseModel):
    """Laboratory report metadata contract."""

    report_id: str = Field(..., description="Unique external report identifier / barcode")
    report_name: str = Field(..., description="Display name of the laboratory report")
    report_type: ReportType = Field(
        default=ReportType.LABORATORY, description="Classification of laboratory report"
    )
    collected_at: Optional[datetime] = Field(
        None, description="Timestamp when sample was collected"
    )
    approved_at: Optional[datetime] = Field(
        None, description="Timestamp when report was approved by pathologist"
    )


# API Request & Response Schemas (Matching OpenAPI specifications 1-to-1)
class ProcessReportRequest(BaseModel):
    """Request model for enqueuing or processing a lab report."""

    clinic_id: str = Field(..., description="Tenant / Clinic identifier")
    connector_type: ConnectorType = Field(
        default=ConnectorType.MOCDOC, description="EMR connector software type"
    )
    external_report_id: str = Field(..., description="Barcode / Report ID in EMR system")
    patient: PatientIdentity = Field(..., description="Patient identification details")
    report_name: str = Field(..., description="Name of the test or panel")
    report_type: ReportType = Field(
        default=ReportType.LABORATORY, description="Type of lab report"
    )
    processing_center_id: Optional[str] = Field(
        None,
        description="Processing center identifier for MocDoc portal config resolution. "
        "If not provided, clinic_id is used as the lookup key.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "clinic_id": "clinic_123",
                "connector_type": "mocdoc",
                "external_report_id": "MOC-998822",
                "patient": {
                    "patient_phone": "+919876543210",
                    "patient_name": "Jane Doe",
                    "patient_mrn": "MRN-55441",
                },
                "report_name": "Complete Blood Count (CBC)",
                "report_type": "Laboratory",
                "processing_center_id": "visakha-multispeciality-clinics",
            }
        }
    )


class ProcessReportResponse(BaseModel):
    """Response model returned after enqueuing or processing a report."""

    success: bool = Field(..., description="Indicates if request was accepted/processed")
    task_id: str = Field(..., description="Task tracking ID for asynchronous execution")
    already_processed: bool = Field(
        default=False, description="True if report was previously processed (idempotent)"
    )
    lab_report_id: Optional[str] = Field(
        None, description="Internal MediAssist lab report record ID if created"
    )
    message: str = Field("", description="Status or descriptive result message")
    callback_delivered: Optional[bool] = Field(
        None, description="Indicates if webhook callback status update was successfully delivered"
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Response generation timestamp",
    )


class CallbackStatusPayload(BaseModel):
    """Webhook callback payload sent to CallMedex upon task completion/failure."""

    task_id: str = Field(..., description="Task tracking ID")
    clinic_id: str = Field(..., description="Clinic ID")
    connector_type: ConnectorType = Field(..., description="EMR connector type")
    external_report_id: str = Field(..., description="External EMR report ID")
    status: TaskStatus = Field(..., description="Final or updated execution status")
    error_message: Optional[str] = Field(
        None, description="Error detail if status is FAILED"
    )
    processed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Completion timestamp",
    )
    correlation_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Correlation ID for end-to-end trace tracking",
    )


class HealthCheckResponse(BaseModel):
    """Health check endpoint response model."""

    status: str = Field(..., description="Service status ('ok' or 'unconfigured')")
    integration_api: bool = Field(
        ..., description="True if integration API secret is configured"
    )
    queue_status: str = Field(
        default="healthy", description="Status of task queue backend"
    )
    version: str = Field(default="1.0.0", description="CallMedex subsystem contract version")
