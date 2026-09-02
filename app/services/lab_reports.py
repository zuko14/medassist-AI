"""Lab Report Delivery Service."""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4


from app.config import settings
from app.database import supabase, log_analytics_event, is_valid_clinic_scope
from app.utils.pdf_reader import extract_text_from_pdf
from app.services.report_summarizer import ReportSummarizer
from app.services.whatsapp import whatsapp_service
from app.services.tenant import get_clinic_by_id
from app.services.subscription import next_ist_midnight, report_dispatch_allowed
from app.utils.validators import mask_phone
from app.database import sb  # T5.1: off-loop query execution

logger = logging.getLogger(__name__)


def template_name_for(clinic: Optional[dict]) -> str:
    """The report template to send for this clinic.

    Templates live on a WABA, not globally, so two clinics can be approved under
    different names — Accumx's WABA has no approved `lab_report_delivery`, while
    TestHospital's does. A per-clinic override lets one clinic move to its own
    approved name without breaking delivery for every other tenant.
    """
    cfg = (clinic or {}).get("config") or {}
    return cfg.get("lab_report_template_name") or settings.lab_report_template_name


def summary_template_name_for(clinic: Optional[dict]) -> Optional[str]:
    """The report template that also carries the AI summary, if this clinic has one.

    A business-initiated template does NOT open the 24h customer-service
    window, so outside that window the summary cannot be sent as a follow-up
    text — it has to travel inside the template itself. That needs a Meta
    template with a third body variable, which is per-WABA and must be
    approved before it exists. Until a clinic sets this, delivery falls back
    to the 2-variable template and the summary is recorded as NOT delivered
    rather than silently claimed.

    Expected body variables: {{1}} patient name, {{2}} report name, {{3}} summary.
    """
    cfg = (clinic or {}).get("config") or {}
    return cfg.get("lab_report_summary_template_name") or settings.lab_report_summary_template_name or None


def flatten_for_template_param(text: str, limit: int = 700) -> str:
    """Meta rejects newlines, tabs and 4+ consecutive spaces inside a template
    parameter (error 132000), and caps each at 1024 chars."""
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def lab_booking_cta(clinic) -> str:
    """A one-line "you can book tests here too" note for a report caption.

    Patients who only ever receive automated reports never learn the bot can
    also book their next test — the menu that offers it is only reachable if
    they message in first. Riding along on the document caption costs no extra
    WhatsApp message and cannot become spam; the report template's quick-reply
    button (payload "book_lab_test") is what carries this outside the 24h
    window, where no caption is possible.

    Returns "" for clinics that do not sell lab tests.
    """
    from app.services.tenant import has_feature

    if not clinic or not has_feature(clinic, "lab_test_booking"):
        return ""
    return "\n\n🧪 Need another test? Just reply *BOOK TEST* here — no calls, no queue."


def report_template_and_params(
    clinic: Optional[dict],
    patient_name: str,
    report_name: str,
    summary_text: Optional[str],
) -> tuple[Optional[str], list, bool]:
    """Pick the document template and its body parameters for one report send.

    Returns (template_name, body_params, carries_summary). Shared by the first
    send, the admin resend, and the retry worker so all three agree on how many
    body variables the chosen template takes — a mismatch is rejected by Meta.
    """
    summary_text = flatten_for_template_param(summary_text or "")
    summary_template = summary_template_name_for(clinic)
    params = [
        {"type": "text", "text": patient_name},
        {"type": "text", "text": report_name},
    ]
    if summary_template and summary_text:
        return summary_template, params + [{"type": "text", "text": summary_text}], True
    return template_name_for(clinic), params, False


class ReportDispatchDeferred(Exception):
    """The send was deliberately held back, not attempted and not failed.

    Raised when a clinic has hit its daily report limit or its subscription is
    suspended. The PDF is already in storage by the time this fires, so the
    report is parked as `pending_retry` and the existing retry worker
    redelivers it once the Asia/Kolkata day rolls over or the clinic renews.
    Distinct from a Meta failure so the ops dashboard does not read a policy
    hold as an outage.
    """


class LabReportService:
    """Service for uploading and sending lab reports to patients via WhatsApp."""

    # Removed hardcoded WhatsApp API methods in favor of whatsapp_service

    async def _store_pdf(
        self,
        clinic_id: str,
        patient_phone: str,
        filename: str,
        file_bytes: bytes,
        content_type: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Upload a report PDF to Supabase Storage.

        Returns (storage_path, signed_url); (None, None) if the upload failed.
        Shared by the delivery path and by store_for_review so that a report
        held for staff review is stored under a real path and can actually be
        sent later — a review row pointing at "pending_review/..." is not
        recoverable by resend_report.
        """
        storage_path = f"{clinic_id}/{patient_phone}/{uuid4()}_{filename}"
        try:
            upload_result = supabase.storage.from_("lab-reports").upload(
                storage_path, file_bytes, {"content-type": content_type}
            )
            logger.info(f"Uploaded report to storage: {storage_path} -> {upload_result}")
        except Exception as e:
            logger.error(
                f"Supabase Storage upload FAILED (file will not be retrievable for bot resend): "
                f"{type(e).__name__}: {e}"
            )
            return None, None

        pdf_signed_url = None
        try:
            signed = supabase.storage.from_("lab-reports").create_signed_url(
                storage_path, 604800
            )
            raw_signed_url = signed.get("signedURL") or signed.get("signedUrl")
            if raw_signed_url:
                if "/+" in raw_signed_url:
                    prefix, sep, suffix = raw_signed_url.partition("?")
                    prefix = prefix.replace("+", "%2B")
                    pdf_signed_url = f"{prefix}?{suffix}" if suffix else prefix
                else:
                    pdf_signed_url = raw_signed_url
        except Exception as sign_err:
            logger.warning(f"Failed to generate signed URL from storage: {sign_err}")

        return storage_path, pdf_signed_url

    async def store_for_review(
        self,
        clinic_id: str,
        patient_phone: str,
        patient_name: str,
        report_name: str,
        report_type: str,
        review_reason: str,
        file_bytes: Optional[bytes] = None,
        filename: Optional[str] = None,
        content_type: str = "application/pdf",
        external_report_id: Optional[str] = None,
        source: str = "connector",
        match_confidence: Optional[float] = None,
        match_source: Optional[str] = None,
    ) -> Optional[str]:
        """Record a report as needs_review WITHOUT delivering it.

        Stores the PDF first when the bytes are available, so that clearing the
        item from the review queue with send_now actually delivers something.
        Returns the new lab_reports row id, or None if the insert failed.
        """
        storage_path = None
        if file_bytes:
            storage_path, _ = await self._store_pdf(
                clinic_id,
                patient_phone or "unknown",
                filename or f"{(external_report_id or uuid4().hex)}.pdf",
                file_bytes,
                content_type,
            )

        row = {
            "clinic_id": clinic_id,
            "patient_phone": patient_phone or "MISSING",
            "patient_name": patient_name or "Unknown",
            "report_name": report_name or "Lab Report",
            "report_type": report_type or "Laboratory",
            # No stored PDF -> keep the legacy sentinel so resolve_report_match
            # still refuses to claim it sent something it cannot send.
            "file_path": storage_path or f"pending_review/{external_report_id or uuid4().hex[:12]}",
            "status": "needs_review",
            "external_report_id": external_report_id,
            "source": source,
            "match_confidence": match_confidence,
            "match_source": match_source,
            "error_message": review_reason,
        }
        try:
            # unscoped: insert_scoped_by_payload
            res = await sb(supabase.table("lab_reports").insert(row))
            if res.data and isinstance(res.data, list) and isinstance(res.data[0], dict):
                return str(res.data[0].get("id"))
        except Exception as e:
            error_str = str(e).lower()
            if "unique" in error_str or "duplicate" in error_str or "23505" in error_str:
                logger.info(
                    f"Report {external_report_id} already recorded — skipping duplicate review row"
                )
                return None
            logger.error(f"Failed to record needs_review row for {external_report_id}: {e}")
        return None

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
                # unscoped: insert_scoped_by_payload
                claim_result = await sb(supabase.table("lab_reports").insert(claim_row))
                if claim_result.data:
                    claim_id = claim_result.data[0]["id"]
            except Exception as e:
                error_str = str(e).lower()
                is_dup = (
                    "unique" in error_str or "duplicate" in error_str or "23505" in error_str
                )
                # A held (needs_review) row is a placeholder for a report that
                # was NEVER sent. Claim it by CAS instead of colliding with it,
                # so a re-offered report can finally be delivered. The
                # .eq("status", "needs_review") predicate is what makes this
                # safe under concurrency: exactly one worker can win the row.
                if is_dup:
                    try:
                        takeover = await sb(
                            supabase.table("lab_reports")
                            .update({"status": "processing", "delivery_status": "processing"})
                            .eq("clinic_id", clinic_id)
                            .eq("external_report_id", external_report_id)
                            .eq("status", "needs_review")
                        )
                        if takeover.data:
                            claim_id = takeover.data[0]["id"]
                            logger.info(
                                f"Claimed previously-held report {external_report_id} "
                                f"for delivery (was needs_review)"
                            )
                            is_dup = False
                    except Exception as takeover_err:
                        logger.warning(
                            f"Could not claim held report {external_report_id}: {takeover_err}"
                        )
                if is_dup:
                    logger.info(
                        f"Report {external_report_id} for clinic {clinic_id} already claimed/delivered — skipping duplicate send"
                    )
                    try:
                        existing = (
                            await sb(supabase.table("lab_reports")
                            .select("*")
                            .eq("clinic_id", clinic_id)
                            .eq("external_report_id", external_report_id))
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

        if ai_result.get("fallback"):
            logger.warning(
                f"AI summarizer returned fallback for report '{report_name}' "
                f"(type={report_type}, patient={patient_name[:3]}***, "
                f"text_len={len(pdf_text) if pdf_text else 0}, "
                f"external_id={external_report_id}, source={source})"
            )

        # Step C — Upload to Supabase Storage
        storage_path, pdf_signed_url = await self._store_pdf(
            clinic_id, patient_phone, filename, file_bytes, content_type
        )
        storage_ok = storage_path is not None
        if not storage_ok:
            # Continue — still send via WhatsApp even if storage failed
            storage_path = f"{clinic_id}/{patient_phone}/{uuid4()}_{filename}"

        # Steps D, E, F — WhatsApp delivery
        sent_ok = False
        summary_sent_ok = False   # did the AI summary text actually reach the patient?
        error_message = None
        capture = {}
        clinic = None
        deferred_reason = None
        try:
            clinic = await get_clinic_by_id(clinic_id)

            # Daily report limit / subscription gate. Checked AFTER storage so a
            # held-back report is fully recoverable, and BEFORE the phone lock so
            # a blocked clinic never serialises behind one.
            dispatch_ok, gate_reason = await report_dispatch_allowed(clinic)
            if not dispatch_ok:
                raise ReportDispatchDeferred(gate_reason)

            from app.services.message_queue import (
                acquire_phone_lock_with_timeout,
                release_phone_lock_acquired,
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
                    # A template send does not open the 24h window, so the summary
                    # cannot follow as a freeform text — it must ride inside the
                    # template. Use the 3-variable summary template when the clinic
                    # has one approved and we actually produced a summary; otherwise
                    # fall back to the 2-variable document template and record
                    # honestly that the patient received no summary.
                    template, body_params, use_summary_template = report_template_and_params(
                        clinic,
                        patient_name,
                        report_name,
                        None if ai_result.get("fallback") else ai_result.get("patient_message"),
                    )
                    if not template:
                        raise ValueError(
                            "Outside 24h window and LAB_REPORT_TEMPLATE_NAME unset — "
                            "cannot deliver to this patient"
                        )

                    if not use_summary_template:
                        logger.info(
                            f"Report {external_report_id or report_name} delivered outside the 24h "
                            f"window without its AI summary — set "
                            f"LAB_REPORT_SUMMARY_TEMPLATE_NAME (or the clinic's "
                            f"lab_report_summary_template_name) to an approved "
                            f"3-variable template to include it"
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
                                    "parameters": body_params,
                                },
                            ],
                            _source="lab_reports",
                            _capture=capture,
                        )
                        if sent_ok and use_summary_template:
                            summary_sent_ok = True
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
                                        "parameters": body_params,
                                    },
                                ],
                                _source="lab_reports",
                                _capture=capture,
                            )
                            if sent_ok and use_summary_template:
                                summary_sent_ok = True
                        else:
                            raise template_err
                    if not sent_ok:
                        raise ValueError(
                            f"WhatsApp rejected template '{template}': "
                            f"{capture.get('error') or 'no detail returned by Meta'}"
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
                        summary_sent_ok = bool(text_sent)
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
                    caption = (
                        f"📋 {report_name} | {report_type} | {clinic['name']}"
                        + lab_booking_cta(clinic)
                    )
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
                await release_phone_lock_acquired(patient_phone)
            logger.info(f"Report sent successfully to {mask_phone(patient_phone)}")
        except ReportDispatchDeferred as e:
            deferred_reason = str(e)
            logger.warning(
                f"Report delivery held for clinic {clinic_id} ({deferred_reason}) — "
                f"queued for {mask_phone(patient_phone)}, will redeliver automatically"
            )
            error_message = (
                "Daily report limit reached — queued until the next day (Asia/Kolkata)"
                if deferred_reason == "daily_limit_reached"
                else "Subscription suspended — queued until the clinic is renewed"
            )
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

            # Permanent non-retryable client errors or configuration gaps.
            # Template-state errors are deliberately NOT listed: Meta returns the
            # same code for "template awaiting approval" as for "no such template",
            # and approval flips to APPROVED on its own. Burning the report as
            # permanently failed means it never delivers once approval lands.
            # Unclassified errors default to transient below and are capped by
            # MAX_RETRIES, so the worst case is a bounded set of retries.
            permanent_indicators = [
                "session expired", "outside 24h window and lab_report_template_name unset",
                "allowlist", "credentials",
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
            "ai_summary_sent": bool(summary_sent_ok and sent_ok),
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
        elif deferred_reason:
            # A policy hold is not a delivery attempt: leave retry_count alone
            # so the 12-attempt Meta budget is not spent waiting for midnight.
            row["next_retry_at"] = (
                next_ist_midnight()
                if deferred_reason == "daily_limit_reached"
                else datetime.now(timezone.utc) + timedelta(hours=6)
            ).isoformat()
            logger.info(
                f"Report queued ({deferred_reason}) — next attempt at {row['next_retry_at']}"
            )
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
                # unscoped: unique_row_key
                result = await sb(supabase.table("lab_reports").update(row).eq("id", claim_id))
                saved_record = result.data[0] if result.data else row
            else:
                # unscoped: insert_scoped_by_payload
                result = await sb(supabase.table("lab_reports").insert(row))
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
                    await sb(supabase.table("lab_reports")
                    .select("*")
                    .eq("clinic_id", clinic_id)
                    .eq("external_report_id", external_report_id))
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
        query = query.eq("clinic_id", clinic_id)
        result = await sb(query)
        return result.data or []

    async def get_reports_by_phone(
        self, phone: str, clinic_id: str = ""
    ) -> list:
        """Get sent lab reports for a specific patient phone.

        KA-16: Uses exact match on normalized phone instead of substring.
        clinic_id is now required — no 'default' sentinel bypass.
        """
        if not is_valid_clinic_scope(clinic_id):
            # Phone numbers are NOT unique across tenants. Unscoped, a patient
            # asking for 'my reports' could be shown another clinic's reports
            # for the same number.
            raise ValueError("clinic_id is required to list reports by phone")

        # Normalize to last 10 digits (Indian mobile) for consistent matching
        clean_phone = phone.lstrip("+").lstrip("0")
        if len(clean_phone) > 10:
            clean_phone = clean_phone[-10:]

        # Try exact match first (normalized), then with country code prefix
        query = (
            supabase.table("lab_reports")
            .select("*")
            .or_(f"patient_phone.eq.{clean_phone},patient_phone.eq.91{clean_phone},patient_phone.eq.+91{clean_phone}")
            .eq("status", "sent")
            .order("uploaded_at", desc=True)
        )
        query = query.eq("clinic_id", clinic_id)
        result = await sb(query)
        return result.data or []

    async def resend_report(
        self,
        report_id: str,
        new_phone: Optional[str] = None,
        clinic_id: Optional[str] = None,
    ) -> dict:
        """Resend a previously uploaded lab report, scoped by clinic_id when provided."""
        if not is_valid_clinic_scope(clinic_id):
            # A report id alone is not an authorization. Resending without a
            # tenant predicate would let one clinic's admin push another
            # clinic's report to that other clinic's patient.
            raise ValueError("clinic_id is required to resend a lab report")
        query = (
            supabase.table("lab_reports")
            .select("*")
            .eq("id", report_id)
            .eq("clinic_id", clinic_id)
        )
        report = await sb(query)
        if not report.data:
            raise ValueError("Report not found")
        report = report.data[0]
        if new_phone:
            report["patient_phone"] = new_phone
            update_query = (
                supabase.table("lab_reports")
                .update({"patient_phone": new_phone})
                .eq("id", report_id)
                .eq("clinic_id", clinic_id)
            )
            await sb(update_query)

        try:
            # Check if PDF has been cleaned up by storage retention policy
            if not report.get("file_path"):
                raise ValueError(
                    "PDF has been removed after the 90-day retention period. "
                    "Metadata and AI summary are still available. "
                    "Please re-upload the report from MocDoc or the admin panel."
                )

            file_path = report["file_path"]
            summary_sent_ok = False  # did the AI summary text actually reach the patient?
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
                release_phone_lock_acquired,
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
                    template, body_params, use_summary_template = report_template_and_params(
                        clinic, patient_name, report_name, report.get("ai_summary")
                    )
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
                                "parameters": body_params,
                            },
                        ],
                        _source="lab_reports",
                        _capture=capture,
                    )
                    if sent_ok and use_summary_template:
                        summary_sent_ok = True
                    if not sent_ok:
                        raise ValueError(
                            f"WhatsApp rejected template '{template}': "
                            f"{capture.get('error') or 'no detail returned by Meta'}"
                        )
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
                        summary_sent_ok = bool(text_sent)
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

                    caption = (
                        f"📋 {report_name} | {report_type} | {clinic['name']}"
                        + lab_booking_cta(clinic)
                    )
                    doc_sent = await whatsapp_service.send_document(
                        clinic, patient_phone, media_handle, filename, caption,
                        _source="lab_reports", _capture=capture,
                    )

                    if not doc_sent:
                        raise ValueError(
                            "WhatsApp API rejected the document send — check recipient allowlist and 24h session window"
                        )
            finally:
                await release_phone_lock_acquired(patient_phone)

            # unscoped: unique_row_key
            await sb(supabase.table("lab_reports").update(
                {
                    "status": "sent",
                    "ai_summary_sent": summary_sent_ok,
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "whatsapp_message_id": capture.get("meta_message_id"),
                    "delivery_status": "sent",
                    "delivery_error": None,
                    "error_message": None,
                }
            ).eq("id", report_id))
        except Exception as e:
            # unscoped: unique_row_key
            await sb(supabase.table("lab_reports").update(
                {
                    "status": "failed",
                    "delivery_status": "failed",
                    "error_message": str(e),
                }
            ).eq("id", report_id))
            raise

        updated = (
            # unscoped: unique_row_key
            await sb(supabase.table("lab_reports").select("*").eq("id", report_id))
        )
        return updated.data[0]

    async def retry_pending_deliveries(self) -> int:
        """Retry reports stuck in 'pending_retry' status.

        Called by the scheduler every 5 minutes. Finds reports where
        next_retry_at <= now and retry_count < 3, re-attempts WhatsApp
        delivery using the file already in Supabase Storage.

        Returns the count of reports processed (success + final fail).
        """
        # Meta outages and operator-fixable faults (expired token, wrong phone id)
        # routinely last hours. At 3 attempts / 42 min the queue used to give up
        # long before a human could rotate a token, silently dropping reports that
        # were already sitting in storage. Cap the backoff instead and keep trying
        # for ~5h, which covers any realistic response window.
        MAX_RETRIES = 12
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            pending = (
                # unscoped: platform_sweep
                await sb(supabase.table("lab_reports")
                .select("*")
                .eq("status", "pending_retry")
                .lt("next_retry_at", now_iso)
                .order("next_retry_at")
                .limit(10))
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
                # unscoped: unique_row_key
                await sb(supabase.table("lab_reports").update({
                    "status": "failed",
                    "delivery_status": "failed",
                    "error_message": "No file_path available for retry",
                    "next_retry_at": None,
                }).eq("id", report_id))
                processed += 1
                continue

            retry_capture: dict = {}
            try:
                clinic = await get_clinic_by_id(report.get("clinic_id", "default"))

                # Re-check the policy gate BEFORE downloading the PDF. At the
                # Asia/Kolkata reset a whole backlog becomes due at once and
                # would otherwise blow straight past the new day's limit.
                dispatch_ok, gate_reason = await report_dispatch_allowed(clinic)
                if not dispatch_ok:
                    # A policy hold is not a delivery attempt — retry_count is
                    # left alone so the MAX_RETRIES budget is not spent waiting.
                    # unscoped: unique_row_key
                    await sb(supabase.table("lab_reports").update({
                        "next_retry_at": (
                            next_ist_midnight()
                            if gate_reason == "daily_limit_reached"
                            else datetime.now(timezone.utc) + timedelta(hours=6)
                        ).isoformat(),
                    }).eq("id", report_id))
                    logger.info(
                        f"Retry worker: report {report_id} still held ({gate_reason})"
                    )
                    continue

                logger.info(
                    f"Retry worker: attempting delivery {retry_count}/{MAX_RETRIES} "
                    f"for report {report_id} to {mask_phone(patient_phone)}"
                )

                # Download PDF from Supabase Storage
                file_bytes = supabase.storage.from_("lab-reports").download(file_path)
                filename = file_path.split("/")[-1]

                # Generate fresh signed URL
                pdf_signed_url = None
                try:
                    signed = supabase.storage.from_("lab-reports").create_signed_url(
                        file_path, 604800
                    )
                    raw_signed_url = signed.get("signedURL") or signed.get("signedUrl")
                    if raw_signed_url:
                        if "/+" in raw_signed_url:
                            prefix, sep, suffix = raw_signed_url.partition("?")
                            prefix = prefix.replace("+", "%2B")
                            pdf_signed_url = f"{prefix}?{suffix}" if suffix else prefix
                        else:
                            pdf_signed_url = raw_signed_url
                except Exception as sign_err:
                    logger.warning(f"Retry worker: signed URL generation failed: {sign_err}")

                # Resolve media handle strategy:
                # - Templates (outside 24h): upload to Meta directly (document.id) for highest reliability
                # - Freeform (inside 24h): prefer signed URL (document.link) with automatic fallback
                is_template_path = not await whatsapp_service._can_send_freeform(clinic, patient_phone)
                summary_sent_ok = False  # did the AI summary text actually reach the patient?

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
                    release_phone_lock_acquired,
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
                        template, body_params, use_summary_template = report_template_and_params(
                            clinic,
                            report.get("patient_name", "Patient"),
                            report.get("report_name", "Lab Report"),
                            report.get("ai_summary"),
                        )
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
                                        "parameters": body_params,
                                    },
                                ],
                                _source="lab_reports_retry",
                                _capture=retry_capture,
                            )
                            if sent_ok and use_summary_template:
                                summary_sent_ok = True
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
                                            "parameters": body_params,
                                        },
                                    ],
                                    _source="lab_reports_retry",
                                    _capture=retry_capture,
                                )
                                if sent_ok and use_summary_template:
                                    summary_sent_ok = True
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
                        summary_sent_ok = bool(text_sent and summary)

                        caption = (
                            f"📋 {report_name} | {report_type} | {clinic['name']}"
                            + lab_booking_cta(clinic)
                        )
                        sent_ok = await whatsapp_service.send_document(
                            clinic, patient_phone, media_handle, filename, caption,
                            _source="lab_reports_retry",
                            _fallback_file_bytes=file_bytes,
                            _fallback_content_type="application/pdf",
                        )
                finally:
                    await release_phone_lock_acquired(patient_phone)

                if sent_ok:
                    # unscoped: unique_row_key
                    await sb(supabase.table("lab_reports").update({
                        "status": "sent",
                        "ai_summary_sent": summary_sent_ok,
                        "delivery_status": "sent",
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                        "error_message": None,
                        "retry_count": retry_count,
                        "next_retry_at": None,
                    }).eq("id", report_id))
                    logger.info(
                        f"Retry worker: successfully delivered report {report_id} "
                        f"on attempt {retry_count} to {mask_phone(patient_phone)}"
                    )
                else:
                    raise ValueError(
                        "WhatsApp delivery returned False: "
                        f"{retry_capture.get('error') or 'no detail returned by Meta'}"
                    )

            except Exception as e:
                logger.error(
                    f"Retry worker: attempt {retry_count}/{MAX_RETRIES} failed "
                    f"for report {report_id}: {e}"
                )
                if retry_count >= MAX_RETRIES:
                    # unscoped: unique_row_key
                    await sb(supabase.table("lab_reports").update({
                        "status": "failed",
                        "delivery_status": "failed",
                        "error_message": f"All {MAX_RETRIES} delivery attempts failed. Last error: {e}",
                        "retry_count": retry_count,
                        "next_retry_at": None,
                    }).eq("id", report_id))
                    logger.error(
                        f"Retry worker: report {report_id} permanently failed after {MAX_RETRIES} attempts"
                    )
                else:
                    backoff_seconds = min(120 * (4 ** (retry_count - 1)), 1800)
                    next_retry = (datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)).isoformat()
                    # unscoped: unique_row_key
                    await sb(supabase.table("lab_reports").update({
                        "status": "pending_retry",
                        "retry_count": retry_count,
                        "next_retry_at": next_retry,
                        "error_message": str(e),
                    }).eq("id", report_id))
                    logger.info(
                        f"Retry worker: report {report_id} re-queued — "
                        f"next retry in {backoff_seconds}s (attempt {retry_count + 1}/{MAX_RETRIES})"
                    )

            processed += 1

        return processed
