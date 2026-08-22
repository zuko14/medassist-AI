"""Lab Report Delivery Service."""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4


from app.config import settings
from app.database import supabase, log_analytics_event
from app.utils.pdf_reader import extract_text_from_pdf
from app.services.report_summarizer import ReportSummarizer
from app.services.whatsapp import whatsapp_service
from app.services.tenant import get_clinic_by_id
from app.utils.validators import mask_phone

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
        external_report_id: Optional[str] = None,
        source: str = "admin",
        match_confidence: Optional[float] = None,
        match_source: Optional[str] = None,
        matched_patient_id: Optional[str] = None,
    ) -> dict:
        """Full pipeline: extract text, AI summary, upload, send via WhatsApp, save record.

        `external_report_id` is the shared dedup key across every intake path that can
        deliver a report for this clinic (admin upload, generic EMR connector, CallMedex).
        When set, an existing lab_reports row for (clinic_id, external_report_id) means
        this report was already delivered — possibly via a different WhatsApp number on a
        different intake path — so we skip re-sending instead of delivering it twice.
        """

        # Step 0 — Cross-path idempotency guard (skip if already delivered by another intake)
        # Fail-open: if the check itself errors (network hiccup etc.), proceed with the
        # send rather than block report delivery on a guard that's meant to prevent — not
        # cause — a missed report.
        if external_report_id:
            try:
                existing = (
                    supabase.table("lab_reports")
                    .select("*")
                    .eq("clinic_id", clinic_id)
                    .eq("external_report_id", external_report_id)
                    .execute()
                )
                if isinstance(existing.data, list) and existing.data:
                    logger.info(
                        f"Report {external_report_id} for clinic {clinic_id} already delivered "
                        f"(source={existing.data[0].get('source')}) — skipping duplicate send"
                    )
                    record = dict(existing.data[0])
                    record["already_processed"] = True
                    return record
            except Exception as e:
                logger.warning(f"Cross-path idempotency check failed (proceeding): {e}")

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
            logger.info(
                f"Uploaded report to storage: {storage_path} -> {upload_result}"
            )
            storage_ok = True
        except Exception as e:
            logger.error(
                f"Supabase Storage upload FAILED (file will not be retrievable for bot resend): {type(e).__name__}: {e}"
            )
            # Continue — still send via WhatsApp even if storage failed

        # Steps D, E, F — WhatsApp delivery
        sent_ok = False
        error_message = None
        capture = {}
        clinic = None
        try:
            clinic = await get_clinic_by_id(clinic_id)
            from app.services.message_queue import (
                acquire_phone_lock_with_timeout,
                get_phone_lock,
                release_phone_lock,
            )

            acquired = await acquire_phone_lock_with_timeout(patient_phone)
            if not acquired:
                raise ValueError(
                    f"Phone lock timeout for {mask_phone(patient_phone)} — "
                    f"another delivery in progress. Will retry next cycle."
                )
            try:
                if not await whatsapp_service._can_send_freeform(clinic, patient_phone):
                    template = settings.lab_report_template_name
                    if not template:
                        raise ValueError(
                            "Outside 24h window and LAB_REPORT_TEMPLATE_NAME unset — "
                            "cannot deliver to this patient"
                        )
                    media_id = await whatsapp_service.upload_media(
                        clinic, file_bytes, filename, content_type
                    )
                    if not media_id:
                        raise ValueError("Failed to upload media to WhatsApp")
                    sent_ok = await whatsapp_service.send_template(
                        clinic,
                        patient_phone,
                        template_name=template,
                        components=[
                            {
                                "type": "header",
                                "parameters": [
                                    {
                                        "type": "document",
                                        "document": {"id": media_id, "filename": filename},
                                    }
                                ],
                            },
                            {
                                "type": "body",
                                "parameters": [
                                    {"type": "text", "text": patient_name},
                                    {"type": "text", "text": report_name},
                                ],
                            },
                        ],
                        _source="lab_reports",
                        _capture=capture,
                    )
                    if not sent_ok:
                        raise ValueError("WhatsApp rejected the utility template send")
                else:
                    # Step D — Upload PDF to WhatsApp media
                    media_id = await whatsapp_service.upload_media(
                        clinic, file_bytes, filename, content_type
                    )

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
                        text_sent = await whatsapp_service.send_text(
                            clinic, patient_phone, summary_message, _source="lab_reports"
                        )
                    else:
                        fallback_text = (
                            f"🏥 *{clinic['name']}*\n\n"
                            f"Dear {patient_name}, your *{report_type}* report is ready. "
                            f"Please find the full report attached below. "
                            f"Consult your doctor for interpretation."
                        )
                        text_sent = await whatsapp_service.send_text(
                            clinic, patient_phone, fallback_text, _source="lab_reports"
                        )

                    if not text_sent:
                        raise ValueError(
                            "WhatsApp API rejected the summary message — check recipient allowlist and 24h session window"
                        )

                    # Step F — Send the actual PDF document
                    caption = f"📋 {report_name} | {report_type} | {clinic['name']}"
                    doc_sent = await whatsapp_service.send_document(
                        clinic, patient_phone, media_id, filename, caption,
                        _source="lab_reports", _capture=capture,
                    )

                    if not doc_sent:
                        raise ValueError(
                            "WhatsApp API rejected the document send — check recipient allowlist and 24h session window"
                        )

                    sent_ok = True
            finally:
                phone_lock = await get_phone_lock(patient_phone)
                phone_lock.release()
                await release_phone_lock(patient_phone)
            logger.info(f"Report sent successfully to {mask_phone(patient_phone)}")
        except Exception as e:
            logger.error(f"WhatsApp send failed for {mask_phone(patient_phone)}: {e}")
            error_message = str(e)

        # Step G — Save to database
        resolved_clinic_id = clinic["id"] if clinic else clinic_id
        row = {
            "clinic_id": resolved_clinic_id,
            "patient_phone": patient_phone,
            "patient_name": patient_name,
            "report_name": report_name,
            "report_type": report_type,
            "file_path": storage_path,
            "ai_summary": ai_result.get("patient_message"),
            "has_abnormal_values": ai_result.get("has_abnormal", False),
            "status": "sent" if sent_ok else "failed",
            "whatsapp_message_id": capture.get("meta_message_id"),
            "delivery_status": "sent" if sent_ok else "failed",
            "external_report_id": external_report_id,
            "source": source,
            "match_confidence": match_confidence,
            "match_source": match_source,
            "matched_patient_id": matched_patient_id,
            "error_message": (
                error_message
                if not sent_ok
                else (
                    None
                    if storage_ok
                    else "Storage upload failed — bot resend unavailable"
                )
            ),
        }
        if sent_ok:
            row["sent_at"] = datetime.now(timezone.utc).isoformat()

        try:
            result = supabase.table("lab_reports").insert(row).execute()
            saved_record = result.data[0] if result.data else row
        except Exception as e:
            # Unique violation on (clinic_id, external_report_id) means another intake
            # path won the race and already delivered this report — a WhatsApp message
            # may have just gone out twice, but at least we don't record it twice.
            if external_report_id and "23505" in str(e):
                logger.warning(
                    f"Race detected: {external_report_id} for clinic {clinic_id} was "
                    f"delivered concurrently by another intake path"
                )
                existing = (
                    supabase.table("lab_reports")
                    .select("*")
                    .eq("clinic_id", clinic_id)
                    .eq("external_report_id", external_report_id)
                    .execute()
                )
                saved_record = (
                    existing.data[0]
                    if isinstance(existing.data, list) and existing.data
                    else row
                )
            else:
                logger.error(f"Failed to save lab report record to database: {e}")
                row["id"] = str(uuid4())
                row["_db_error"] = str(e)
                saved_record = row

        # Audit logging for PII-safe AI summarization and delivery
        if sent_ok:
            await log_analytics_event(
                clinic_id=resolved_clinic_id,
                phone=patient_phone,
                event_type="report_delivered",
                metadata={
                    "report_type": report_type,
                    "has_abnormal": ai_result.get("has_abnormal", False),
                    "ai_fallback": ai_result.get("fallback", True),
                },
            )

        return saved_record

    async def get_all_reports(
        self, clinic_id: str = "default", limit: int = 100
    ) -> list:
        """Get all lab reports ordered by upload date."""
        query = (
            supabase.table("lab_reports")
            .select("*")
            .order("uploaded_at", desc=True)
            .limit(limit)
        )
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        result = query.execute()
        return result.data or []

    async def get_reports_by_phone(
        self, phone: str, clinic_id: str = "default"
    ) -> list:
        """Get sent lab reports for a specific patient phone."""
        # Normalize: strip + prefix to match admin-uploaded records
        clean_phone = phone.lstrip("+")

        query = (
            supabase.table("lab_reports")
            .select("*")
            .ilike("patient_phone", f"%{clean_phone}%")
            .eq("status", "sent")
            .order("uploaded_at", desc=True)
        )
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)

        result = query.execute()
        return result.data or []

    async def resend_report(self, report_id: str, new_phone: Optional[str] = None) -> dict:
        """Resend a previously uploaded lab report."""
        report = supabase.table("lab_reports").select("*").eq("id", report_id).execute()
        if not report.data:
            raise ValueError("Report not found")
        report = report.data[0]
        if new_phone:
            report["patient_phone"] = new_phone
            supabase.table("lab_reports").update({"patient_phone": new_phone}).eq("id", report_id).execute()

        try:
            # Check if PDF has been cleaned up by storage retention policy
            if not report.get("file_path"):
                raise ValueError(
                    "PDF has been removed after the 90-day retention period. "
                    "Metadata and AI summary are still available. "
                    "Please re-upload the report from MocDoc or the admin panel."
                )

            # Download file from Supabase Storage
            try:
                file_bytes = supabase.storage.from_("lab-reports").download(
                    report["file_path"]
                )
            except Exception as storage_err:
                logger.error(
                    f"Storage download failed for {report['file_path']}: {storage_err}"
                )
                raise ValueError(
                    f"Report file not found in storage. It may have been deleted. "
                    f"Please re-upload the report from the admin panel."
                )

            clinic = await get_clinic_by_id(report.get("clinic_id", "default"))
            filename = report["file_path"].split("/")[-1]
            patient_phone = report["patient_phone"]
            patient_name = report.get("patient_name", "Patient")
            report_name = report.get("report_name", "Lab Report")
            report_type = report.get("report_type", "General")
            capture = {}

            from app.services.message_queue import (
                acquire_phone_lock_with_timeout,
                get_phone_lock,
                release_phone_lock,
            )

            acquired = await acquire_phone_lock_with_timeout(patient_phone)
            if not acquired:
                raise ValueError(
                    f"Phone lock timeout for {mask_phone(patient_phone)} — "
                    f"another delivery in progress"
                )
            try:
                if not await whatsapp_service._can_send_freeform(clinic, patient_phone):
                    template = settings.lab_report_template_name
                    if not template:
                        raise ValueError(
                            "Outside 24h window and LAB_REPORT_TEMPLATE_NAME unset — "
                            "cannot deliver to this patient"
                        )
                    media_id = await whatsapp_service.upload_media(
                        clinic, file_bytes, filename, "application/pdf"
                    )
                    if not media_id:
                        raise ValueError("Failed to upload media to WhatsApp")
                    sent_ok = await whatsapp_service.send_template(
                        clinic,
                        patient_phone,
                        template_name=template,
                        components=[
                            {
                                "type": "header",
                                "parameters": [
                                    {
                                        "type": "document",
                                        "document": {"id": media_id, "filename": filename},
                                    }
                                ],
                            },
                            {
                                "type": "body",
                                "parameters": [
                                    {"type": "text", "text": patient_name},
                                    {"type": "text", "text": report_name},
                                ],
                            },
                        ],
                        _source="lab_reports",
                        _capture=capture,
                    )
                    if not sent_ok:
                        raise ValueError("WhatsApp rejected the utility template send")
                else:
                    media_id = await whatsapp_service.upload_media(
                        clinic, file_bytes, filename, "application/pdf"
                    )

                    if not media_id:
                        raise ValueError("Failed to upload media to WhatsApp")

                    # Send summary or fallback text
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
                        text_sent = await whatsapp_service.send_text(
                            clinic, patient_phone, summary_message, _source="lab_reports"
                        )
                    else:
                        fallback_text = (
                            f"🏥 *{clinic['name']}*\n\n"
                            f"Dear {patient_name}, your *{report_type}* report is ready. "
                            f"Please find the full report attached below. "
                            f"Consult your doctor for interpretation."
                        )
                        text_sent = await whatsapp_service.send_text(
                            clinic, patient_phone, fallback_text, _source="lab_reports"
                        )

                    if not text_sent:
                        raise ValueError(
                            "WhatsApp API rejected the summary message — check recipient allowlist and 24h session window"
                        )

                    caption = f"📋 {report_name} | {report_type} | {clinic['name']}"
                    doc_sent = await whatsapp_service.send_document(
                        clinic, patient_phone, media_id, filename, caption,
                        _source="lab_reports", _capture=capture,
                    )

                    if not doc_sent:
                        raise ValueError(
                            "WhatsApp API rejected the document send — check recipient allowlist and 24h session window"
                        )
            finally:
                phone_lock = await get_phone_lock(patient_phone)
                phone_lock.release()
                await release_phone_lock(patient_phone)

            supabase.table("lab_reports").update(
                {
                    "status": "sent",
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "whatsapp_message_id": capture.get("meta_message_id"),
                    "delivery_status": "sent",
                    "delivery_error": None,
                    "error_message": None,
                }
            ).eq("id", report_id).execute()
        except Exception as e:
            supabase.table("lab_reports").update(
                {
                    "status": "failed",
                    "delivery_status": "failed",
                    "error_message": str(e),
                }
            ).eq("id", report_id).execute()
            raise

        updated = (
            supabase.table("lab_reports").select("*").eq("id", report_id).execute()
        )
        return updated.data[0]
