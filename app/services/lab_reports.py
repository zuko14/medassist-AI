"""Lab Report Delivery Service."""

import logging
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from app.config import settings
from app.database import supabase

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com/v18.0"


class LabReportService:
    """Service for uploading and sending lab reports to patients via WhatsApp."""

    def __init__(self):
        self.phone_number_id = settings.whatsapp_phone_number_id
        self.token = settings.whatsapp_token
        self.base_url = f"{WHATSAPP_API_BASE}/{self.phone_number_id}"

    async def upload_and_send(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str,
        patient_phone: str,
        patient_name: str,
        report_name: str,
        report_type: str,
    ) -> dict:
        """Upload file to Supabase Storage, send via WhatsApp, and save record."""

        # Step A — Upload to Supabase Storage
        storage_path = f"{patient_phone}/{uuid4()}_{filename}"
        supabase.storage.from_("lab-reports").upload(
            storage_path, file_bytes, {"content-type": content_type}
        )

        # Step B — Send PDF via WhatsApp as a document
        sent_ok = False
        error_message = None
        try:
            media_id = await self._upload_media(file_bytes, filename, content_type)
            await self._send_document(
                patient_phone, media_id, report_name,
                f"Dear {patient_name}, your {report_type} report from TestHospital is ready. "
                f"Please download and save it. For queries call 108."
            )
            sent_ok = True
        except Exception as e:
            logger.error(f"WhatsApp send failed for {patient_phone}: {e}")
            error_message = str(e)

        # Step C — Save record to lab_reports table
        row = {
            "patient_phone": patient_phone,
            "patient_name": patient_name,
            "report_name": report_name,
            "report_type": report_type,
            "file_path": storage_path,
            "status": "sent" if sent_ok else "failed",
            "uploaded_by": "admin",
            "error_message": error_message,
        }
        if sent_ok:
            row["sent_at"] = datetime.now(timezone.utc).isoformat()

        result = supabase.table("lab_reports").insert(row).execute()
        return result.data[0]

    async def get_all_reports(self, limit: int = 100) -> list:
        """Get all lab reports ordered by upload date."""
        result = (
            supabase.table("lab_reports")
            .select("*")
            .order("uploaded_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []

    async def resend_report(self, report_id: str) -> dict:
        """Resend a previously uploaded lab report."""
        # Fetch the report row
        report = (
            supabase.table("lab_reports")
            .select("*")
            .eq("id", report_id)
            .execute()
        )
        if not report.data:
            raise ValueError("Report not found")
        report = report.data[0]

        # Download file from Supabase Storage
        file_bytes = supabase.storage.from_("lab-reports").download(report["file_path"])

        # Re-send via WhatsApp
        try:
            filename = report["file_path"].split("/")[-1]
            media_id = await self._upload_media(file_bytes, filename, "application/pdf")
            await self._send_document(
                report["patient_phone"], media_id, report["report_name"],
                f"Dear {report['patient_name']}, your {report['report_type']} report from TestHospital is ready. "
                f"Please download and save it. For queries call 108."
            )
            supabase.table("lab_reports").update({
                "status": "sent",
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "error_message": None,
            }).eq("id", report_id).execute()
        except Exception as e:
            supabase.table("lab_reports").update({
                "status": "failed",
                "error_message": str(e),
            }).eq("id", report_id).execute()
            raise

        updated = supabase.table("lab_reports").select("*").eq("id", report_id).execute()
        return updated.data[0]

    async def get_reports_by_phone(self, phone: str) -> list:
        """Get lab reports for a specific patient phone."""
        result = (
            supabase.table("lab_reports")
            .select("*")
            .eq("patient_phone", phone)
            .order("uploaded_at", desc=True)
            .execute()
        )
        return result.data or []

    # ── Internal helpers ──

    async def _upload_media(self, file_bytes: bytes, filename: str, content_type: str) -> str:
        """Upload media to WhatsApp and return media_id."""
        url = f"{self.base_url}/media"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                files={"file": (filename, file_bytes, content_type)},
                data={"messaging_product": "whatsapp"},
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()["id"]

    async def _send_document(self, phone: str, media_id: str, filename: str, caption: str):
        """Send a document message via WhatsApp."""
        url = f"{self.base_url}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "document",
            "document": {
                "id": media_id,
                "filename": filename,
                "caption": caption,
            },
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
