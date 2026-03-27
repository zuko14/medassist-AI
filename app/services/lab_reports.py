"""Lab Report Delivery Service."""

import logging
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from app.config import settings
from app.database import supabase
from app.utils.pdf_reader import extract_text_from_pdf
from app.services.report_summarizer import ReportSummarizer

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com/v18.0"


class LabReportService:
    """Service for uploading and sending lab reports to patients via WhatsApp."""

    def __init__(self):
        self.phone_number_id = settings.whatsapp_phone_number_id
        self.token = settings.whatsapp_token
        self.base_url = f"{WHATSAPP_API_BASE}/{self.phone_number_id}"

    async def _upload_media_to_whatsapp(self, file_bytes: bytes, filename: str, content_type: str) -> str:
        """Upload file to Meta media endpoint and return media_id."""
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

    async def _send_whatsapp_text(self, to: str, message: str) -> None:
        """Send a text message via WhatsApp."""
        url = f"{self.base_url}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": message, "preview_url": False},
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

    async def _send_whatsapp_document(self, to: str, media_id: str, filename: str, caption: str) -> None:
        """Send a document message via WhatsApp."""
        url = f"{self.base_url}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
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
        """Full pipeline: extract text, AI summary, upload, send via WhatsApp, save record."""

        # Step A — Extract text from PDF
        pdf_text = extract_text_from_pdf(file_bytes)

        # Step B — AI summary
        summarizer = ReportSummarizer()
        ai_result = await summarizer.summarize(pdf_text, patient_name, report_type)

        # Step C — Upload to Supabase Storage
        storage_path = f"{patient_phone}/{uuid4()}_{filename}"
        try:
            supabase.storage.from_("lab-reports").upload(
                storage_path, file_bytes, {"content-type": content_type}
            )
            logger.info(f"Uploaded report to storage: {storage_path}")
        except Exception as e:
            logger.error(f"Supabase Storage upload failed: {e}")

        # Steps D, E, F — WhatsApp delivery
        sent_ok = False
        error_message = None
        try:
            # Step D — Upload PDF to WhatsApp media
            media_id = await self._upload_media_to_whatsapp(file_bytes, filename, content_type)

            # Step E — Send AI summary message to patient
            if not ai_result["fallback"]:
                summary_message = (
                    f"🏥 *{settings.hospital_name} — Lab Report Ready*\n\n"
                    f"Dear {patient_name},\n\n"
                    f"{ai_result['patient_message']}"
                )
                if ai_result["has_abnormal"]:
                    summary_message += "\n\n⚠️ *Some values may need attention. Please consult your doctor.*"
                summary_message += "\n\n📄 Your full report is attached below."
                await self._send_whatsapp_text(patient_phone, summary_message)
            else:
                fallback_text = (
                    f"🏥 *{settings.hospital_name}*\n\n"
                    f"Dear {patient_name}, your *{report_type}* report is ready. "
                    f"Please find the full report attached below. "
                    f"Consult your doctor for interpretation."
                )
                await self._send_whatsapp_text(patient_phone, fallback_text)

            # Step F — Send the actual PDF document
            caption = f"📋 {report_name} | {report_type} | {settings.hospital_name}"
            await self._send_whatsapp_document(patient_phone, media_id, filename, caption)

            sent_ok = True
            logger.info(f"Report sent successfully to {patient_phone}")
        except Exception as e:
            logger.error(f"WhatsApp send failed for {patient_phone}: {e}")
            error_message = str(e)

        # Step G — Save to database
        row = {
            "patient_phone": patient_phone,
            "patient_name": patient_name,
            "report_name": report_name,
            "report_type": report_type,
            "file_path": storage_path,
            "ai_summary": ai_result.get("patient_message"),
            "has_abnormal_values": ai_result.get("has_abnormal", False),
            "status": "sent" if sent_ok else "failed",
            "error_message": error_message,
        }
        if sent_ok:
            row["sent_at"] = datetime.now(timezone.utc).isoformat()

        try:
            result = supabase.table("lab_reports").insert(row).execute()
            return result.data[0]
        except Exception as e:
            logger.error(f"Failed to save lab report record to database: {e}")
            row["id"] = str(uuid4())
            row["_db_error"] = str(e)
            return row

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

    async def get_reports_by_phone(self, phone: str) -> list:
        """Get sent lab reports for a specific patient phone."""
        # Normalize: strip + prefix to match admin-uploaded records
        clean_phone = phone.lstrip("+")
        result = (
            supabase.table("lab_reports")
            .select("id, report_name, report_type, uploaded_at, status")
            .eq("patient_phone", clean_phone)
            .eq("status", "sent")
            .order("uploaded_at", desc=True)
            .execute()
        )
        return result.data or []

    async def resend_report(self, report_id: str) -> dict:
        """Resend a previously uploaded lab report."""
        report = (
            supabase.table("lab_reports")
            .select("*")
            .eq("id", report_id)
            .execute()
        )
        if not report.data:
            raise ValueError("Report not found")
        report = report.data[0]

        try:
            # Download file from Supabase Storage
            try:
                file_bytes = supabase.storage.from_("lab-reports").download(report["file_path"])
            except Exception as storage_err:
                logger.error(f"Storage download failed for {report['file_path']}: {storage_err}")
                raise ValueError(
                    f"Report file not found in storage. It may have been deleted. "
                    f"Please re-upload the report from the admin panel."
                )

            filename = report["file_path"].split("/")[-1]
            media_id = await self._upload_media_to_whatsapp(file_bytes, filename, "application/pdf")

            # Send summary or fallback text
            patient_name = report.get("patient_name", "Patient")
            report_type = report.get("report_type", "General")
            ai_summary = report.get("ai_summary")

            if ai_summary:
                summary_message = (
                    f"🏥 *{settings.hospital_name} — Lab Report Ready*\n\n"
                    f"Dear {patient_name},\n\n"
                    f"{ai_summary}"
                )
                if report.get("has_abnormal_values"):
                    summary_message += "\n\n⚠️ *Some values may need attention. Please consult your doctor.*"
                summary_message += "\n\n📄 Your full report is attached below."
                await self._send_whatsapp_text(report["patient_phone"], summary_message)
            else:
                fallback_text = (
                    f"🏥 *{settings.hospital_name}*\n\n"
                    f"Dear {patient_name}, your *{report_type}* report is ready. "
                    f"Please find the full report attached below. "
                    f"Consult your doctor for interpretation."
                )
                await self._send_whatsapp_text(report["patient_phone"], fallback_text)

            caption = f"📋 {report['report_name']} | {report_type} | {settings.hospital_name}"
            await self._send_whatsapp_document(report["patient_phone"], media_id, filename, caption)

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
