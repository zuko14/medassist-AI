"""Lab Report Delivery Service."""

import logging
from datetime import datetime, timedelta, timezone
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

        # Step 0 — Cross-path idempotency guard (atomic claim row before sending)
        claim_id = None
        if external_report_id:
            try:
                # Atomic INSERT with status='processing' to claim the slot before WhatsApp send.
                # file_path is intentionally omitted here — it is computed after storage upload
                # and backfilled via UPDATE at the end of this function.
                claim_row = {
                    "clinic_id": clinic_id,
                    "external_report_id": external_report_id,
                    "patient_phone": patient_phone,
                    "patient_name": patient_name,
                    "report_name": report_name,
                    "report_type": report_type,
                    "status": "processing",
                    "delivery_status": "processing",
                    "source": source,
                    "match_confidence": match_confidence,
                    "match_source": match_source,
                    "matched_patient_id": matched_patient_id,
                }
                claim_result = supabase.table("lab_reports").insert(claim_row).execute()
                if claim_result.data:
                    claim_id = claim_result.data[0]["id"]
            except Exception as e:
                error_str = str(e).lower()
                if "unique" in error_str or "duplicate" in error_str or "23505" in error_str:
                    logger.info(
                        f"Report {external_report_id} for clinic {clinic_id} already claimed/delivered — skipping duplicate send"
                    )
                    try:
                        existing = (
                            supabase.table("lab_reports")
                            .select("*")
                            .eq("clinic_id", clinic_id)
                            .eq("external_report_id", external_report_id)
                            .execute()
                        )
                        if (
                            existing
                            and existing.data
                            and isinstance(existing.data, list)
                            and len(existing.data) > 0
                            and isinstance(existing.data[0], dict)
                        ):
                            record = dict(existing.data[0])
                            record["status"] = record.get("status") or "skipped"
                            record["already_processed"] = True
                            record["reason"] = "duplicate_report_id"
                            return record
                    except Exception:
                        pass
                    return {
                        "id": None,
                        "status": "skipped",
                        "delivery_status": "already_claimed",
                        "already_processed": True,
                        "reason": "duplicate_report_id",
                    }
                # Non-unique DB errors (e.g. constraint violations, connection issues)
                # mean the idempotency guard is NOT active for this report.
                # Log at ERROR level so this is never silently ignored.
                logger.error(
                    f"Lab report claim insert FAILED — idempotency guard inactive "
                    f"for {external_report_id}: {e}"
                )

        # Step A — Extract text from PDF
        pdf_text = extract_text_from_pdf(file_bytes)

        # Step B — AI summary
        summarizer = ReportSummarizer()
        ai_result = await summarizer.summarize(pdf_text, patient_name, report_type)

        # Step C — Upload to Supabase Storage
        storage_path = f"{patient_phone}/{uuid4()}_{filename}"
        storage_ok = False
        pdf_signed_url = None
        try:
            upload_result = supabase.storage.from_("lab-reports").upload(
                storage_path, file_bytes, {"content-type": content_type}
            )
            logger.info(
                f"Uploaded report to storage: {storage_path} -> {upload_result}"
            )
            storage_ok = True
            try:
                signed = supabase.storage.from_("lab-reports").create_signed_url(
                    storage_path, 604800
                )
                pdf_signed_url = signed.get("signedURL") or signed.get("signedUrl")
            except Exception as sign_err:
                logger.warning(f"Failed to generate signed URL from storage: {sign_err}")
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
                # Resolve media handle strategy:
                # - Templates (outside 24h): prefer Meta upload (document.id) — more reliable
                #   since Meta doesn't need to fetch an external URL
                # - Freeform (inside 24h): prefer signed URL (document.link) — faster,
                #   and send_document has automatic link→upload fallback if Meta 500s
                is_template_path = not await whatsapp_service._can_send_freeform(clinic, patient_phone)

                if is_template_path:
                    # Upload to Meta first for reliability
                    try:
                        media_handle = await whatsapp_service.upload_media(
                            clinic, file_bytes, filename, content_type
                        )
                    except Exception as upload_err:
                        logger.warning(f"Media upload attempt failed (will try signed URL): {upload_err}")
                        media_handle = None

                    if not media_handle and pdf_signed_url:
                        media_handle = pdf_signed_url  # Fallback to signed URL
                else:
                    # Freeform: prefer signed URL (send_document handles fallback)
                    media_handle = pdf_signed_url
                    if not media_handle:
                        try:
                            media_handle = await whatsapp_service.upload_media(
                                clinic, file_bytes, filename, content_type
                            )
                        except Exception:
                            media_handle = None

                if not media_handle:
                    raise ValueError("Failed to obtain media handle (both upload and signed URL unavailable)")

                doc_header = {"filename": filename}
                if media_handle.startswith("http://") or media_handle.startswith("https://"):
                    doc_header["link"] = media_handle
                else:
                    doc_header["id"] = media_handle

                if is_template_path:
                    template = settings.lab_report_template_name
                    if not template:
                        raise ValueError(
                            "Outside 24h window and LAB_REPORT_TEMPLATE_NAME unset — "
                            "cannot deliver to this patient"
                        )
                    try:
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
                                            "document": doc_header,
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
                    except Exception as template_err:
                        # If sent with media ID and failed with 500, attempt with signed URL before giving up
                        if "id" in doc_header and pdf_signed_url and ("500" in str(template_err) or "Server Error" in str(template_err)):
                            logger.warning(
                                f"Template send with media ID failed (Meta 500) — retrying with signed URL link for {mask_phone(patient_phone)}"
                            )
                            alt_header = {"filename": filename, "link": pdf_signed_url}
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
                                                "document": alt_header,
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
                        else:
                            raise template_err
                    if not sent_ok:
                        raise ValueError(
                            "WhatsApp rejected the utility template send — "
                            "template may not be approved or parameters don't match"
                        )
                else:
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
                        clinic, patient_phone, media_handle, filename, caption,
                        _source="lab_reports", _capture=capture,
                        _fallback_file_bytes=file_bytes,
                        _fallback_content_type=content_type,
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

        # Determine if this is a transient Meta API error (retryable) vs permanent failure
        is_transient_meta_error = False
        if error_message and not sent_ok:
            # Explicit server error / network / transient indicators from Meta or storage
            transient_indicators = [
                "500", "502", "503", "504", "Server Error", "Meta 500",
                "upload fallback also failed", "OAuthException", "timeout",
                "Internal Server Error", "An unknown error has occurred",
            ]
            has_transient_indicator = any(ind.lower() in error_message.lower() for ind in transient_indicators)

            # Permanent non-retryable client errors or configuration gaps
            permanent_indicators = [
                "session expired", "outside 24h window and lab_report_template_name unset",
                "allowlist", "credentials", "template may not be approved",
                "template does not exist", "template name is invalid",
            ]
            has_permanent_indicator = any(ind.lower() in error_message.lower() for ind in permanent_indicators)

            if has_transient_indicator:
                is_transient_meta_error = True
            elif not has_permanent_indicator:
                # Default unclassified errors to transient to allow retry queue attempts
                is_transient_meta_error = True

        effective_status = "sent" if sent_ok else ("pending_retry" if is_transient_meta_error else "failed")

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
            "status": effective_status,
            "whatsapp_message_id": capture.get("meta_message_id"),
            "delivery_status": effective_status,
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
        elif is_transient_meta_error:
            # Schedule retry: exponential backoff — 2 min, 8 min, 32 min
            retry_count = 1  # This is the first attempt
            row["retry_count"] = retry_count
            backoff_seconds = 120 * (4 ** (retry_count - 1))  # 120s, 480s, 1920s
            row["next_retry_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)
            ).isoformat()
            logger.info(
                f"Report queued for retry (attempt {retry_count}) — "
                f"next retry in {backoff_seconds}s for {mask_phone(patient_phone)}"
            )

        try:
            if claim_id:
                result = supabase.table("lab_reports").update(row).eq("id", claim_id).execute()
                saved_record = result.data[0] if result.data else row
            else:
                result = supabase.table("lab_reports").insert(row).execute()
                saved_record = result.data[0] if result.data else row
        except Exception as e:
            # Unique violation on (clinic_id, external_report_id) means another intake
            # path won the race and already delivered this report.
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
                row["id"] = claim_id or str(uuid4())
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

            file_path = report["file_path"]
            # Download file from Supabase Storage
            try:
                file_bytes = supabase.storage.from_("lab-reports").download(
                    file_path
                )
            except Exception as storage_err:
                logger.error(
                    f"Storage download failed for {file_path}: {storage_err}"
                )
                raise ValueError(
                    "Report file not found in storage. It may have been deleted. "
                    "Please re-upload the report from the admin panel."
                )

            clinic = await get_clinic_by_id(report.get("clinic_id", "default"))
            filename = file_path.split("/")[-1]
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
                # Resolve media handle: prefer Supabase Storage signed URL (direct link), fallback to WhatsApp upload
                pdf_signed_url = None
                try:
                    signed = supabase.storage.from_("lab-reports").create_signed_url(
                        file_path, 604800
                    )
                    pdf_signed_url = signed.get("signedURL") or signed.get("signedUrl")
                except Exception as sign_err:
                    logger.warning(f"Failed to generate signed URL for resend: {sign_err}")

                media_handle = pdf_signed_url
                if not media_handle:
                    media_handle = await whatsapp_service.upload_media(
                        clinic, file_bytes, filename, "application/pdf"
                    )
                if not media_handle:
                    raise ValueError("Failed to obtain media handle (signed URL or WhatsApp upload)")

                doc_header = {"filename": filename}
                if media_handle.startswith("http://") or media_handle.startswith("https://"):
                    doc_header["link"] = media_handle
                else:
                    doc_header["id"] = media_handle

                if not await whatsapp_service._can_send_freeform(clinic, patient_phone):
                    template = settings.lab_report_template_name
                    if not template:
                        raise ValueError(
                            "Outside 24h window and LAB_REPORT_TEMPLATE_NAME unset — "
                            "cannot deliver to this patient"
                        )
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
                                        "document": doc_header,
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
                        clinic, patient_phone, media_handle, filename, caption,
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

    async def retry_pending_deliveries(self) -> int:
        """Retry reports stuck in 'pending_retry' status.

        Called by the scheduler every 5 minutes. Finds reports where
        next_retry_at <= now and retry_count < 3, re-attempts WhatsApp
        delivery using the file already in Supabase Storage.

        Returns the count of reports processed (success + final fail).
        """
        MAX_RETRIES = 3
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            pending = (
                supabase.table("lab_reports")
                .select("*")
                .eq("status", "pending_retry")
                .lt("next_retry_at", now_iso)
                .order("next_retry_at")
                .limit(10)  # Process at most 10 per cycle to avoid overload
                .execute()
            )
        except Exception as e:
            logger.error(f"Retry worker: failed to query pending_retry reports: {e}")
            return 0

        if not pending.data:
            return 0

        processed = 0
        for report in pending.data:
            report_id = report["id"]
            retry_count = (report.get("retry_count") or 0) + 1
            patient_phone = report["patient_phone"]
            file_path = report.get("file_path")

            if not file_path:
                logger.warning(f"Retry worker: report {report_id} has no file_path — marking failed")
                supabase.table("lab_reports").update({
                    "status": "failed",
                    "delivery_status": "failed",
                    "error_message": "No file_path available for retry",
                    "next_retry_at": None,
                }).eq("id", report_id).execute()
                processed += 1
                continue

            logger.info(
                f"Retry worker: attempting delivery {retry_count}/{MAX_RETRIES} "
                f"for report {report_id} to {mask_phone(patient_phone)}"
            )

            try:
                # Download PDF from Supabase Storage
                file_bytes = supabase.storage.from_("lab-reports").download(file_path)
                filename = file_path.split("/")[-1]
                clinic = await get_clinic_by_id(report.get("clinic_id", "default"))

                # Generate fresh signed URL
                pdf_signed_url = None
                try:
                    signed = supabase.storage.from_("lab-reports").create_signed_url(
                        file_path, 604800
                    )
                    pdf_signed_url = signed.get("signedURL") or signed.get("signedUrl")
                except Exception as sign_err:
                    logger.warning(f"Retry worker: signed URL generation failed: {sign_err}")

                # Resolve media handle strategy:
                # - Templates (outside 24h): upload to Meta directly (document.id) for highest reliability
                # - Freeform (inside 24h): prefer signed URL (document.link) with automatic fallback
                is_template_path = not await whatsapp_service._can_send_freeform(clinic, patient_phone)

                if is_template_path:
                    media_handle = await whatsapp_service.upload_media(
                        clinic, file_bytes, filename, "application/pdf"
                    )
                    if not media_handle and pdf_signed_url:
                        media_handle = pdf_signed_url
                else:
                    media_handle = pdf_signed_url
                    if not media_handle:
                        media_handle = await whatsapp_service.upload_media(
                            clinic, file_bytes, filename, "application/pdf"
                        )

                if not media_handle:
                    raise ValueError("Failed to obtain media handle for retry")

                from app.services.message_queue import (
                    acquire_phone_lock_with_timeout,
                    get_phone_lock,
                    release_phone_lock,
                )

                acquired = await acquire_phone_lock_with_timeout(patient_phone)
                if not acquired:
                    logger.warning(
                        f"Retry worker: phone lock timeout for {mask_phone(patient_phone)} — will retry next cycle"
                    )
                    continue  # Don't increment retry_count — not a real failure

                try:
                    # Check session window
                    if is_template_path:
                        # Outside 24h window — need template
                        from app.config import settings as app_settings
                        template = app_settings.lab_report_template_name
                        if not template:
                            raise ValueError("Outside 24h window and no template configured")

                        doc_header = {"filename": filename}
                        if media_handle.startswith("http://") or media_handle.startswith("https://"):
                            doc_header["link"] = media_handle
                        else:
                            doc_header["id"] = media_handle

                        try:
                            sent_ok = await whatsapp_service.send_template(
                                clinic,
                                patient_phone,
                                template_name=template,
                                components=[
                                    {
                                        "type": "header",
                                        "parameters": [
                                            {"type": "document", "document": doc_header}
                                        ],
                                    },
                                    {
                                        "type": "body",
                                        "parameters": [
                                            {"type": "text", "text": report.get("patient_name", "Patient")},
                                            {"type": "text", "text": report.get("report_name", "Lab Report")},
                                        ],
                                    },
                                ],
                                _source="lab_reports_retry",
                            )
                        except Exception as retry_tpl_err:
                            if "id" in doc_header and pdf_signed_url and ("500" in str(retry_tpl_err) or "Server Error" in str(retry_tpl_err)):
                                logger.warning(
                                    f"Retry worker: template send with media ID failed (Meta 500) — retrying with signed URL link for {mask_phone(patient_phone)}"
                                )
                                alt_header = {"filename": filename, "link": pdf_signed_url}
                                sent_ok = await whatsapp_service.send_template(
                                    clinic,
                                    patient_phone,
                                    template_name=template,
                                    components=[
                                        {
                                            "type": "header",
                                            "parameters": [
                                                {"type": "document", "document": alt_header}
                                            ],
                                        },
                                        {
                                            "type": "body",
                                            "parameters": [
                                                {"type": "text", "text": report.get("patient_name", "Patient")},
                                                {"type": "text", "text": report.get("report_name", "Lab Report")},
                                            ],
                                        },
                                    ],
                                    _source="lab_reports_retry",
                                )
                            else:
                                raise retry_tpl_err
                    else:
                        # Freeform send: summary + document
                        report_name = report.get("report_name", "Lab Report")
                        report_type = report.get("report_type", "General")
                        patient_name = report.get("patient_name", "Patient")

                        summary = report.get("ai_summary")
                        if summary:
                            summary_msg = (
                                f"🏥 *{clinic['name']} — Lab Report Ready*\n\n"
                                f"Dear {patient_name},\n\n{summary}"
                            )
                            if report.get("has_abnormal_values"):
                                summary_msg += "\n\n⚠️ *Some values may need attention. Please consult your doctor.*"
                            summary_msg += "\n\n📄 Your full report is attached below."
                        else:
                            summary_msg = (
                                f"🏥 *{clinic['name']}*\n\n"
                                f"Dear {patient_name}, your *{report_type}* report is ready. "
                                f"Please find the full report attached below."
                            )

                        text_sent = await whatsapp_service.send_text(
                            clinic, patient_phone, summary_msg, _source="lab_reports_retry"
                        )

                        caption = f"📋 {report_name} | {report_type} | {clinic['name']}"
                        sent_ok = await whatsapp_service.send_document(
                            clinic, patient_phone, media_handle, filename, caption,
                            _source="lab_reports_retry",
                            _fallback_file_bytes=file_bytes,
                            _fallback_content_type="application/pdf",
                        )
                finally:
                    phone_lock = await get_phone_lock(patient_phone)
                    phone_lock.release()
                    await release_phone_lock(patient_phone)

                if sent_ok:
                    supabase.table("lab_reports").update({
                        "status": "sent",
                        "delivery_status": "sent",
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                        "error_message": None,
                        "retry_count": retry_count,
                        "next_retry_at": None,
                    }).eq("id", report_id).execute()
                    logger.info(
                        f"Retry worker: successfully delivered report {report_id} "
                        f"on attempt {retry_count} to {mask_phone(patient_phone)}"
                    )
                else:
                    raise ValueError("WhatsApp delivery returned False")

            except Exception as e:
                logger.error(
                    f"Retry worker: attempt {retry_count}/{MAX_RETRIES} failed "
                    f"for report {report_id}: {e}"
                )
                if retry_count >= MAX_RETRIES:
                    supabase.table("lab_reports").update({
                        "status": "failed",
                        "delivery_status": "failed",
                        "error_message": f"All {MAX_RETRIES} delivery attempts failed. Last error: {e}",
                        "retry_count": retry_count,
                        "next_retry_at": None,
                    }).eq("id", report_id).execute()
                    logger.error(
                        f"Retry worker: report {report_id} permanently failed after {MAX_RETRIES} attempts"
                    )
                else:
                    backoff_seconds = 120 * (4 ** (retry_count - 1))
                    next_retry = (datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)).isoformat()
                    supabase.table("lab_reports").update({
                        "status": "pending_retry",
                        "retry_count": retry_count,
                        "next_retry_at": next_retry,
                        "error_message": str(e),
                    }).eq("id", report_id).execute()
                    logger.info(
                        f"Retry worker: report {report_id} re-queued — "
                        f"next retry in {backoff_seconds}s (attempt {retry_count + 1}/{MAX_RETRIES})"
                    )

            processed += 1

        return processed
