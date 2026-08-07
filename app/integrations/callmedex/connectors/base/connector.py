"""Base Laboratory Connector Abstract Interface (Phase 2 & 3 Contract)."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from app.integrations.callmedex.api.schemas import (
    PatientIdentity,
    ReportMetadata,
)


class JobCheckpoint(str, Enum):
    """Resumable task recovery checkpoints for long-running report jobs."""

    CREATED = "CHECKPOINT_1_CREATED"
    AUTHENTICATED = "CHECKPOINT_2_AUTHENTICATED"
    BARCODE_LOCATED = "CHECKPOINT_3_BARCODE_LOCATED"
    REPORT_LOCATED = "CHECKPOINT_4_REPORT_LOCATED"
    PDF_DOWNLOADED = "CHECKPOINT_5_PDF_DOWNLOADED"
    VALIDATED = "CHECKPOINT_6_VALIDATED"
    CALLBACK_SENT = "CHECKPOINT_7_CALLBACK_SENT"


class ConnectorCapabilities(BaseModel):
    """Capability declarations advertised by laboratory connectors."""

    browser_required: bool = Field(
        default=True, description="Indicates if headless Playwright browser is required"
    )
    supports_barcode_search: bool = Field(
        default=True, description="Supports searching report by barcode ID"
    )
    supports_incremental_downloads: bool = Field(
        default=False, description="Supports incremental report fetching"
    )
    supports_multi_report: bool = Field(
        default=False, description="Supports batch multi-report processing"
    )
    supports_pdf: bool = Field(
        default=True, description="Supports downloading PDF reports"
    )
    supports_images: bool = Field(
        default=False, description="Supports image-based reports"
    )
    supports_retry: bool = Field(
        default=True, description="Supports task retries on failure"
    )


class BaseLaboratoryConnector(ABC):
    """Abstract Base Class defining the contract for all laboratory EMR connectors.

    Every concrete connector (e.g. MocDoc, Crelio, CloudLIMS) MUST implement
    this uniform interface and declare its capabilities.
    """

    @property
    @abstractmethod
    def capabilities(self) -> ConnectorCapabilities:
        """Return the connector's capability declaration."""
        pass

    @abstractmethod
    async def login(self, credentials: Dict[str, Any]) -> bool:
        """Authenticate with target EMR portal."""
        pass

    @abstractmethod
    async def search_by_barcode(self, barcode_id: str) -> Optional[ReportMetadata]:
        """Search target EMR for lab report matching barcode ID."""
        pass

    @abstractmethod
    async def download_report(
        self, barcode_id: str, download_path: str
    ) -> Optional[bytes]:
        """Download raw PDF report bytes for the specified report ID."""
        pass

    @abstractmethod
    async def validate_report(
        self, file_bytes: bytes, expected_patient: PatientIdentity
    ) -> bool:
        """Validate downloaded PDF report against expected patient identity."""
        pass

    @abstractmethod
    async def logout(self) -> bool:
        """Terminate active browser/API session cleanly."""
        pass

    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """Perform diagnostic health check on connector."""
        pass

    @abstractmethod
    async def cleanup(self) -> bool:
        """Clean up active browser sessions and temporary artifacts."""
        pass

    @abstractmethod
    async def retry(self, report_job_id: str, checkpoint: JobCheckpoint = JobCheckpoint.CREATED) -> bool:
        """Retry a failed job from specified recovery checkpoint."""
        pass

    @abstractmethod
    async def checkpoint_resume(self, report_job_id: str, target_checkpoint: JobCheckpoint) -> bool:
        """Resume execution from target recovery checkpoint."""
        pass

    @abstractmethod
    async def wait_until_report_available(
        self, barcode_id: str, timeout_seconds: int = 300
    ) -> bool:
        """Poll laboratory portal lifecycle until report is verified, approved, and ready for download."""
        pass

