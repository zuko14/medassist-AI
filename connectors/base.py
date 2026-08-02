"""Abstract base class for all hospital system connectors.

Every connector (MocDoc, Practo, Birlamedisoft, FHIR, etc.) implements this
interface. MedAssist AI never knows which HMIS the report came from — it just
receives a PDF + metadata via the internal API.
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class ReportMetadata:
    """Metadata extracted from a hospital system for a single lab report."""

    def __init__(
        self,
        patient_name: str,
        patient_phone: str,
        report_name: str,
        report_type: str,
        external_report_id: str,
        vam_id: Optional[str] = None,
        report_no: Optional[str] = None,
        sample_id: Optional[str] = None,
    ):
        self.patient_name = patient_name
        self.patient_phone = patient_phone
        self.report_name = report_name
        self.report_type = report_type
        self.external_report_id = external_report_id
        self.vam_id = vam_id
        self.report_no = report_no
        self.sample_id = sample_id

    def __repr__(self) -> str:
        return (
            f"ReportMetadata(name={self.patient_name!r}, "
            f"phone=***{self.patient_phone[-4:]}, "
            f"report={self.report_name!r}, "
            f"ext_id={self.external_report_id!r})"
        )


class HospitalConnector(ABC):
    """Abstract base for all hospital system connectors.

    Subclasses implement authenticate(), fetch_new_reports(), and
    download_report(). The submit_to_medassist() method is shared and
    identical for all connectors.
    """

    def __init__(
        self,
        clinic_id: str,
        connector_type: str,
        config: dict,
        medassist_url: str,
        integration_secret: str,
    ):
        self.clinic_id = clinic_id
        self.connector_type = connector_type
        self.config = config
        self.medassist_url = medassist_url.rstrip("/")
        self.integration_secret = integration_secret

    @abstractmethod
    async def authenticate(self) -> bool:
        """Login to the hospital system. Return True on success."""
        ...

    @abstractmethod
    async def fetch_new_reports(self) -> list[ReportMetadata]:
        """Discover reports ready for download. Does NOT download files."""
        ...

    @abstractmethod
    async def download_report(self, meta: ReportMetadata) -> Optional[bytes]:
        """Download a single report's PDF bytes. Return None on failure."""
        ...

    @abstractmethod
    async def cleanup(self) -> None:
        """Release resources (close browser, delete temp files, etc.)."""
        ...

    async def submit_to_medassist(
        self, pdf_bytes: bytes, meta: ReportMetadata
    ) -> dict:
        """POST a downloaded report to MedAssist AI's internal API.

        This method is the SAME for ALL connectors. It is the universal
        handoff point from any hospital system into MedAssist AI.
        """
        url = f"{self.medassist_url}/internal/integrations/lab-report"
        filename = f"{meta.external_report_id}.pdf"

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                url,
                headers={"X-Integration-Secret": self.integration_secret},
                data={
                    "clinic_id": self.clinic_id,
                    "patient_phone": meta.patient_phone,
                    "patient_name": meta.patient_name,
                    "report_name": meta.report_name,
                    "report_type": meta.report_type,
                    "external_report_id": meta.external_report_id,
                    "connector_type": self.connector_type,
                },
                files={"file": (filename, pdf_bytes, "application/pdf")},
            )

        if response.status_code == 200:
            return response.json()
        else:
            logger.error(
                f"MedAssist API returned {response.status_code}: "
                f"{response.text[:200]}"
            )
            raise RuntimeError(
                f"API upload failed ({response.status_code}): {response.text[:200]}"
            )

    async def run(self) -> dict:
        """Full cycle: authenticate → fetch → download → submit → audit.

        Returns a summary dict with counts.
        """
        summary = {
            "reports_found": 0,
            "reports_new": 0,
            "reports_uploaded": 0,
            "reports_failed": 0,
            "errors": [],
        }

        try:
            # Step 1: Authenticate
            if not await self.authenticate():
                summary["errors"].append("Authentication failed")
                return summary

            # Step 2: Fetch available reports
            reports = await self.fetch_new_reports()
            summary["reports_found"] = len(reports)

            if not reports:
                logger.info(f"[{self.connector_type}] No new reports found")
                return summary

            summary["reports_new"] = len(reports)

            # Step 3: Download and submit each report
            for meta in reports:
                try:
                    pdf_bytes = await self.download_report(meta)
                    if not pdf_bytes:
                        logger.warning(
                            f"[{self.connector_type}] Empty PDF for {meta}"
                        )
                        summary["reports_failed"] += 1
                        summary["errors"].append(
                            f"Empty PDF: {meta.external_report_id}"
                        )
                        continue

                    result = await self.submit_to_medassist(pdf_bytes, meta)

                    if result.get("already_processed"):
                        logger.info(
                            f"[{self.connector_type}] Already processed: "
                            f"{meta.external_report_id}"
                        )
                    else:
                        summary["reports_uploaded"] += 1
                        logger.info(
                            f"[{self.connector_type}] Uploaded: {meta}"
                        )

                except Exception as e:
                    summary["reports_failed"] += 1
                    summary["errors"].append(
                        f"{meta.external_report_id}: {type(e).__name__}: {e}"
                    )
                    logger.error(
                        f"[{self.connector_type}] Failed to process "
                        f"{meta.external_report_id}: {e}"
                    )

        finally:
            await self.cleanup()

        return summary
