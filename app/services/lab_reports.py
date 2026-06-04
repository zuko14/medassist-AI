"""Lab Report Delivery Service."""

import logging
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from app.config import settings
from app.database import supabase
from app.utils.pdf_reader import extract_text_from_pdf
from app.services.report_summarizer import ReportSummarizer
from app.services.whatsapp import whatsapp_service
from app.services.tenant import get_clinic_by_id

logger = logging.getLogger(__name__)

class LabReportService:
    """Service for uploading and sending lab reports to patients via WhatsApp."""

    # Removed hardcoded WhatsApp API methods in favor of whatsapp_service

    async def upload_and_send(
        self,
        clinic_id: str,
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
        storage_ok = False
        try:
            upload_result = supabase.storage.from_("lab-reports").upload(
                storage_path, file_bytes, {"content-type": content_type}
            )
            logger.info(f"Uploaded report to storage: {storage_path} -> {upload_result}")
            storage_ok = True
        except Exception as e:
            logger.error(f"Supabase Storage upload FAILED (file will not be retrievable for bot resend): {type(e).__name__}: {e}")
            # Continue — still send via WhatsApp even if storage failed

        # Steps D, E, F — WhatsApp delivery
        sent_ok = False
        error_message = None
        try:
            clinic = await get_clinic_by_id(clinic_id)
            
            # Step D — Upload PDF to WhatsApp media
            media_id = await whatsapp_service.upload_media(clinic, file_bytes, filename, content_type)

            if not media_id:
                raise ValueError("Failed to upload media to WhatsApp")

            # Step E — Send AI summary message to patient
            if not ai_result["fallback"]:
                summary_message = (
                    f"🏥 *{clinic['name']} — Lab Report Ready*\n\n"
                    f"Dear {patient_name},\n\n"
                    f"{ai_result['patient_message']}"
                )
                if ai_result["has_abnormal"]:
                    summary_message += "\n\n⚠️ *Some values may need attention. Please consult your doctor.*"
                summary_message += "\n\n📄 Your full report is attached below."
                await whatsapp_service.send_text(clinic, patient_phone, summary_message)
            else:
                fallback_text = (
                    f"🏥 *{clinic['name']}*\n\n"
                    f"Dear {patient_name}, your *{report_type}* report is ready. "
                    f"Please find the full report attached below. "
                    f"Consult your doctor for interpretation."
                )
                await whatsapp_service.send_text(clinic, patient_phone, fallback_text)

            # Step F — Send the actual PDF document
            caption = f"📋 {report_name} | {report_type} | {clinic['name']}"
            await whatsapp_service.send_document(clinic, patient_phone, media_id, filename, caption)

            sent_ok = True
            logger.info(f"Report sent successfully to {patient_phone}")
        except Exception as e:
            logger.error(f"WhatsApp send failed for {patient_phone}: {e}")
            error_message = str(e)

        # Step G — Save to database
        row = {
            "clinic_id": clinic["id"],
            "patient_phone": patient_phone,
            "patient_name": patient_name,
            "report_name": report_name,
            "report_type": report_type,
            "file_path": storage_path,
            "ai_summary": ai_result.get("patient_message"),
            "has_abnormal_values": ai_result.get("has_abnormal", False),
            "status": "sent" if sent_ok else "failed",
            "error_message": error_message if not sent_ok else (None if storage_ok else "Storage upload failed — bot resend unavailable"),
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

    async def get_all_reports(self, clinic_id: str = "default", limit: int = 100) -> list:
        """Get all lab reports ordered by upload date."""
        query = supabase.table("lab_reports").select("*").order("uploaded_at", desc=True).limit(limit)
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        result = query.execute()
        return result.data or []

    async def get_reports_by_phone(self, phone: str, clinic_id: str = "default") -> list:
        """Get sent lab reports for a specific patient phone."""
        # Normalize: strip + prefix to match admin-uploaded records
        clean_phone = phone.lstrip("+")
        
        query = supabase.table("lab_reports").select("*").ilike("patient_phone", f"%{clean_phone}%").eq("status", "sent").order("uploaded_at", desc=True)
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
            
        result = query.execute()
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

            clinic = await get_clinic_by_id(report.get("clinic_id", "default"))
            filename = report["file_path"].split("/")[-1]
            media_id = await whatsapp_service.upload_media(clinic, file_bytes, filename, "application/pdf")
            
            if not media_id:
                raise ValueError("Failed to upload media to WhatsApp")

            # Send summary or fallback text
            patient_name = report.get("patient_name", "Patient")
            report_type = report.get("report_type", "General")
            ai_summary = report.get("ai_summary")

            if ai_summary:
                summary_message = (
                    f"🏥 *{clinic['name']} — Lab Report Ready*\n\n"
                    f"Dear {patient_name},\n\n"
                    f"{ai_summary}"
                )
                if report.get("has_abnormal_values"):
                    summary_message += "\n\n⚠️ *Some values may need attention. Please consult your doctor.*"
                summary_message += "\n\n📄 Your full report is attached below."
                await whatsapp_service.send_text(clinic, report["patient_phone"], summary_message)
            else:
                fallback_text = (
                    f"🏥 *{clinic['name']}*\n\n"
                    f"Dear {patient_name}, your *{report_type}* report is ready. "
                    f"Please find the full report attached below. "
                    f"Consult your doctor for interpretation."
                )
                await whatsapp_service.send_text(clinic, report["patient_phone"], fallback_text)

            caption = f"📋 {report['report_name']} | {report_type} | {clinic['name']}"
            await whatsapp_service.send_document(clinic, report["patient_phone"], media_id, filename, caption)

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
