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
    report_job_id: Optional[str] = Field(
        None,
        description="CallMedex's own report_job id. When present, lifecycle callbacks "
        "(report-accepted/processing/delivered/failed) are posted back to CallMedex; "
        "when absent (e.g. connector-driven jobs) no callback is sent.",
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


# ─── CallMedex v1 Contract Models (mediassist-ai.openapi.yaml) ───────────────


class CallMedexPatient(BaseModel):
    """Patient details provided in CallMedex ReportJob submissions."""

    patient_id: str = Field(..., description="CallMedex internal or EMR patient ID")
    phone: str = Field(..., description="Patient mobile number in E.164 format")
    preferred_language: str = Field(default="en", description="ISO 639-1 language code (e.g. en, te, hi)")
    abha_number: Optional[str] = Field(None, description="Ayushman Bharat Health Account number")
    name: Optional[str] = Field(None, description="Patient full name")
    gender: Optional[str] = Field(None, description="Patient gender")
    dob: Optional[str] = Field(None, description="Patient date of birth")

    model_config = ConfigDict(extra="ignore")


class CallMedexDelivery(BaseModel):
    """Delivery channels and routing instructions."""

    channels: list[str] = Field(default_factory=lambda: ["whatsapp"], description="List of delivery channels")
    deliver_summary_to_doctor_id: Optional[str] = Field(None, description="Optional doctor recipient ID")
    whatsapp_phone: Optional[str] = Field(None, description="Optional explicit WhatsApp recipient phone")

    model_config = ConfigDict(extra="ignore")


class CallMedexReportJobRequest(BaseModel):
    """Report job payload submitted by CallMedex to POST /api/v1/report-jobs."""

    report_job_id: str = Field(..., description="CallMedex unique report job UUID")
    source_type: str = Field(default="lab_report", description="Document type: lab_report, prescription, consultation_summary")
    source_document_url: Optional[str] = Field(default="", description="Pre-signed URL to source PDF document if pre-generated")
    booking_id: Optional[str] = Field(None, description="CallMedex booking reference")
    sample_id: Optional[str] = Field(None, description="Specimen sample ID")
    processing_center_id: Optional[str] = Field(None, description="CallMedex processing center UUID")
    barcode: Optional[str] = Field(None, description="Specimen barcode in processing center LIS/EMR")
    connector_type: Optional[str] = Field(default="patient_upload", description="Connector or intake type")
    patient: CallMedexPatient = Field(..., description="Patient demographic and contact details")
    delivery: CallMedexDelivery = Field(default_factory=CallMedexDelivery, description="Delivery preferences")
    callback_base_url: Optional[str] = Field(None, description="CallMedex callback base URL")

    model_config = ConfigDict(extra="ignore")


class CallMedexReportJobAccepted(BaseModel):
    """202 Accepted response for POST /api/v1/report-jobs."""

    report_job_id: str = Field(..., description="Acknowledged CallMedex report job ID")
    status: str = Field(default="queued", description="Initial lifecycle status")


class CallMedexReportJobStatus(BaseModel):
    """Status polling response for GET /api/v1/report-jobs/{report_job_id}."""

    report_job_id: str = Field(..., description="CallMedex report job ID")
    status: str = Field(..., description="Current status: queued, processing, delivered, failed, expired")
    failure_reason: Optional[str] = Field(None, description="Failure reason code if status is failed")
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp of last status update",
    )


class CallMedexNotificationRecipient(BaseModel):
    """Recipient for transactional notifications."""

    phone: str = Field(..., description="Recipient E.164 phone number")
    patient_id: Optional[str] = Field(None, description="Patient ID if recipient is a patient")
    provider_id: Optional[str] = Field(None, description="Provider ID if recipient is a healthcare worker")

    model_config = ConfigDict(extra="ignore")


class CallMedexNotificationRequest(BaseModel):
    """Notification dispatch payload for POST /api/v1/notifications."""

    channel: str = Field(default="whatsapp", description="Channel (whatsapp)")
    recipient: CallMedexNotificationRecipient = Field(..., description="Message recipient details")
    template: str = Field(..., description="Template name")
    template_data: dict = Field(default_factory=dict, description="Variables passed to template")
    callback_base_url: Optional[str] = Field(None, description="CallMedex callback endpoint")

    model_config = ConfigDict(extra="ignore")


class CallMedexNotificationAccepted(BaseModel):
    """202 Accepted response for POST /api/v1/notifications."""

    notification_id: str = Field(..., description="Unique notification ID")
    status: str = Field(default="queued", description="Initial lifecycle status")

