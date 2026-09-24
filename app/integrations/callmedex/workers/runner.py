"""CallMedex Background Worker Runner & DI Container (Phase 3 & Phase R2 Implementation)."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from uuid import uuid4
from app.integrations.callmedex.config.settings import callmedex_settings, CallMedexSettings
from app.integrations.callmedex.api.schemas import (
    ProcessReportRequest,
    ProcessReportResponse,
    CallMedexReportJobRequest,
)
from app.integrations.callmedex.connectors.factory import ConnectorFactory
from app.integrations.callmedex.ocr.engine import CanonicalOCRPipeline
from app.integrations.callmedex.ai.reasoning import ClinicalReasoningEngine
from app.integrations.callmedex.ai.generator import MultiAudienceSummaryGenerator
from app.integrations.callmedex.whatsapp.service import WhatsAppDeliveryService
from app.integrations.callmedex.whatsapp.schemas import WhatsAppDeliveryStatus
from app.integrations.callmedex.connectors.base.connector import JobCheckpoint
from app.integrations.callmedex.browser.session import PlaywrightBrowserSession
from app.integrations.callmedex.storage.provider import LocalStorageProvider
from app.integrations.callmedex.callbacks.handler import CallMedexCallbackHandler, build_analysis_payload
from app.integrations.callmedex.queue.drivers import InMemoryQueue
from app.integrations.callmedex.api.exceptions import ConfigurationError, CallMedexException, ValidationError
from app.integrations.callmedex.config.processing_centers import (
    resolve_processing_center,
    resolve_callmedex_clinic_id,
)
from app.database import sb  # T5.1: off-loop query execution
from app.utils.async_tasks import spawn_background_task
from app.services.distributed_lock import distributed_lock_manager

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _settle_callbacks(tasks: list, timeout: float = 15.0) -> None:
    """Let in-flight non-terminal callbacks finish (bounded), then drop the rest."""
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:
        t.cancel()


def _failure_reason(exc: Exception, checkpoint: JobCheckpoint) -> str:
    """Map a pipeline exception onto CallMedex's report-failed enum."""
    if isinstance(exc, ValidationError) or checkpoint == JobCheckpoint.PDF_DOWNLOADED:
        return "invalid_source_document"
    if "did not appear in MocDoc" in str(exc):  # connector wait_until_report_available timeout
        return "report_not_ready_timeout"
    return "download_automation_failed"


class CallMedexContainer:
    """Dependency Injection container resolving CallMedex subsystem services."""

    def __init__(self, settings: Optional[CallMedexSettings] = None):
        self.settings = settings or callmedex_settings
        self._validate_config_fail_fast()

        # Instantiate services
        self.browser_session = PlaywrightBrowserSession()
        self.storage_provider = LocalStorageProvider()
        self.callback_handler = CallMedexCallbackHandler(
            secret=self.settings.hmac_signature_secret.get_secret_value()
        )
        self.queue_engine = InMemoryQueue()
        self.ocr_pipeline = CanonicalOCRPipeline()
        self._connectors: Dict[str, Any] = {}

    def get_connector(self, connector_type: Any = "mocdoc"):
        """Resolve requested EMR connector instance dynamically via ConnectorFactory."""
        key = (connector_type.value if hasattr(connector_type, "value") else str(connector_type)).lower()
        if key not in self._connectors:
            self._connectors[key] = ConnectorFactory.create(
                connector_type=key,
                browser_session=self.browser_session,
            )
        return self._connectors[key]

    @property
    def mocdoc_connector(self):
        """Backward-compatibility accessor returning MocDoc connector instance."""
        return self.get_connector("mocdoc")

    @mocdoc_connector.setter
    def mocdoc_connector(self, val: Any) -> None:
        """Allow replacing MocDoc connector instance for testing/mocking."""
        self._connectors["mocdoc"] = val


    def _validate_config_fail_fast(self) -> None:
        """Fail fast if required configuration or secrets are invalid."""
        if not self.settings.integration_secret.get_secret_value():
            raise ConfigurationError("CallMedex integration secret is not set")
        if not self.settings.hmac_signature_secret.get_secret_value():
            raise ConfigurationError("CallMedex HMAC signature secret is not set")
        if not self.settings.bearer_token.get_secret_value():
            raise ConfigurationError("CallMedex bearer token is not set")

        if self.settings.app_env == "production":
            placeholders = []
            if "change_in_prod" in self.settings.integration_secret.get_secret_value().lower():
                placeholders.append("integration_secret")
            if "change_in_prod" in self.settings.hmac_signature_secret.get_secret_value().lower():
                placeholders.append("hmac_signature_secret")
            if "change_in_prod" in self.settings.bearer_token.get_secret_value().lower():
                placeholders.append("bearer_token")

            if placeholders:
                raise ConfigurationError(
                    f"Refusing to boot in production with default secrets: {', '.join(placeholders)}"
                )


class CallMedexWorkerRunner:
    """Background worker runner executing report processing jobs with recovery checkpoints."""

    def __init__(self, container: Optional[CallMedexContainer] = None):
        self.container = container or CallMedexContainer()
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            loop.create_task(
                self.container.queue_engine.register_handler("process_report", self.execute_report_job)
            )
        except RuntimeError:
            pass

    def _emit_event(
        self, event_name: str, report_job_id: str, correlation_id: str, extra: Optional[Dict[str, Any]] = None
    ) -> None:
        """Emit structured event log with correlation_id and report_job_id."""
        details = extra or {}
        logger.info(
            f"EVENT:{event_name} [ReportJob: {report_job_id} | Trace: {correlation_id}] details={details}"
        )

    async def execute_report_job(
        self, request: ProcessReportRequest, correlation_id: Optional[str] = None
    ) -> ProcessReportResponse:
        """Execute a report processing job following the connector-agnostic 9-step lifecycle with recovery checkpoints."""
        report_job_id = str(uuid4())
        corr_id = correlation_id or str(uuid4())
        connector_type = request.connector_type or "mocdoc"
        connector = self.container.get_connector(connector_type)
        # CallMedex's own job id — lifecycle callbacks only exist for jobs CallMedex created.
        cmx_job_id = getattr(request, "report_job_id", None)
        callbacks = self.container.callback_handler

        self._emit_event("ReportJobCreated", report_job_id, corr_id, {"barcode": request.external_report_id, "connector": connector_type})
        # Non-terminal callbacks run in the background so a slow/down CallMedex
        # never delays the patient's report; _settle_callbacks() drains them
        # before the terminal one so "processing" can never land after "delivered".
        cmx_tasks: list = []
        if cmx_job_id:
            cmx_tasks.append(spawn_background_task(
                callbacks.send_report_accepted(cmx_job_id, _utc_now_iso(), corr_id),
                name=f"cmx_accepted_{cmx_job_id}",
            ))

        # Step 1: Connector Created & Browser Session Initialized
        self._emit_event("ConnectorInitialized", report_job_id, corr_id, {"connector": connector_type})

        session_id = f"session_{report_job_id}"
        session_context = await self.container.browser_session.create_context(
            session_id, headless=callmedex_settings.browser_headless
        )
        if isinstance(session_context, dict) and session_context.get("page"):
            if hasattr(connector, "attach_page"):
                connector.attach_page(session_context["page"], session_id=session_id)

        temp_filepath = None
        checkpoint = JobCheckpoint.CREATED
        # Read by the step-9 callback even if step 8 never ran.
        patient_phone = getattr(request.patient, "patient_phone", None)
        summary_report = canonical_report = whatsapp_message_id = None
        whatsapp_sent = False

        try:
            # Step 2: Resolve processing center config & credentials (base_url, clinic_slug, username, password)
            # Uses processing_center_id from request, falling back to clinic_id
            center_lookup_id = (
                getattr(request, "processing_center_id", None) or request.clinic_id
            )
            center_config = None
            try:
                center_config = await resolve_processing_center(
                    clinic_id=center_lookup_id,
                    connector_type=str(connector_type.value if hasattr(connector_type, 'value') else connector_type),
                )
                if hasattr(connector, "configure_center"):
                    connector.configure_center(
                        base_url=center_config.base_url,
                        clinic_slug=center_config.clinic_slug,
                    )
                self._emit_event(
                    "CenterConfigResolved", report_job_id, corr_id,
                    {"center_id": center_lookup_id, "base_url": center_config.base_url},
                )
            except ValueError as cfg_err:
                logger.warning(
                    f"Processing center config resolution failed for '{center_lookup_id}': {cfg_err}. "
                    f"Falling back to environment settings."
                )
                self._emit_event(
                    "CenterConfigMissing", report_job_id, corr_id,
                    {"center_id": center_lookup_id, "error": str(cfg_err)},
                )

            # Step 2b: Credentials Validated
            # Priority: Per-center credentials in Supabase -> fallback to Render .env settings
            creds = {
                "username": (
                    (center_config.username if center_config and center_config.username else None)
                    or self.container.settings.mocdoc_username.get_secret_value()
                ),
                "password": (
                    (center_config.password if center_config and center_config.password else None)
                    or self.container.settings.mocdoc_password.get_secret_value()
                ),
            }

            # Step 3: Health Check
            health = await connector.health_check()
            if health.get("status") != "healthy":
                raise ConfigurationError(f"{connector_type} connector health check failed")

            # Step 4: Open Login Page & Login
            if checkpoint == JobCheckpoint.CREATED:
                if hasattr(connector, "open_login_page"):
                    await connector.open_login_page()
                await connector.login(creds)
                checkpoint = JobCheckpoint.AUTHENTICATED
                self._emit_event("LoginSucceeded", report_job_id, corr_id)

            # Step 5: Search by Barcode & Wait for Report Availability
            if checkpoint == JobCheckpoint.AUTHENTICATED:
                metadata = await connector.search_by_barcode(request.external_report_id)
                await connector.wait_until_report_available(request.external_report_id)
                checkpoint = JobCheckpoint.REPORT_LOCATED
                self._emit_event("BarcodeFound", report_job_id, corr_id, {"metadata": metadata.model_dump() if metadata else None})

            # Step 6: Download Report
            if checkpoint == JobCheckpoint.REPORT_LOCATED:
                pdf_bytes = await connector.download_report(
                    request.external_report_id, callmedex_settings.download_dir
                )
                if not pdf_bytes:
                    raise CallMedexException("Downloaded PDF bytes are empty")

                temp_filepath = await self.container.storage_provider.save_temp_report(
                    request.external_report_id, pdf_bytes, f"{request.external_report_id}.pdf"
                )
                checkpoint = JobCheckpoint.PDF_DOWNLOADED
                self._emit_event("ReportDownloaded", report_job_id, corr_id, {"bytes": len(pdf_bytes)})

            # Step 7: Validate Report
            if checkpoint == JobCheckpoint.PDF_DOWNLOADED:
                valid = await connector.validate_report(pdf_bytes, request.patient)
                if not valid:
                    raise CallMedexException("Report patient validation failed")
                checkpoint = JobCheckpoint.VALIDATED
                self._emit_event("ValidationSucceeded", report_job_id, corr_id)

            # Step 8: OCR Pipeline & Downstream AI/WhatsApp Delivery
            if checkpoint == JobCheckpoint.VALIDATED:
                # Re-check idempotency right before sending — the router-level check runs
                # before enqueueing, leaving a race window where the processing center's own
                # EMR connector could deliver the same external_report_id via /lab-report in
                # between. Closing it here (not just at enqueue time) prevents a double-send.
                # Fail-open: a broken check must never block a real report from sending.
                try:
                    from app.database import supabase as _supabase_precheck
                    already = (
                        await sb(_supabase_precheck.table("lab_reports")
                        .select("id")
                        .eq("clinic_id", request.clinic_id)
                        .eq("external_report_id", request.external_report_id))
                    )
                    if isinstance(already.data, list) and already.data:
                        logger.info(
                            f"Report {request.external_report_id} for clinic {request.clinic_id} "
                            f"already delivered by another intake path — skipping CallMedex send"
                        )
                        return ProcessReportResponse(
                            success=True,
                            task_id=report_job_id,
                            already_processed=True,
                            lab_report_id=str(already.data[0].get("id", "")),
                            message=f"Report {request.external_report_id} already processed",
                            callback_delivered=True,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )
                except Exception as precheck_err:
                    logger.warning(f"Cross-path idempotency pre-check failed (proceeding): {precheck_err}")

                patient_phone = getattr(request.patient, "patient_phone", None)
                patient_id = getattr(request.patient, "patient_mrn", None) or patient_phone or "unknown"
                patient_name = getattr(request.patient, "patient_name", "Patient")

                if cmx_job_id:
                    cmx_tasks.append(spawn_background_task(
                        callbacks.send_report_processing(cmx_job_id, _utc_now_iso(), corr_id),
                        name=f"cmx_processing_{cmx_job_id}",
                    ))

                # OCR Processing
                canonical_report = None
                try:
                    canonical_report = self.container.ocr_pipeline.process_pdf(
                        pdf_bytes=pdf_bytes,
                        report_id=report_job_id,
                        patient_id=patient_id,
                        barcode=request.external_report_id,
                        processing_center_id=getattr(request, "processing_center_id", "default") or "default",
                    )
                    self._emit_event("OCRExtracted", report_job_id, corr_id, {"extracted_tests": len(canonical_report.tests)})
                except Exception as ocr_err:
                    logger.warning(f"OCR Pipeline extraction warning for {report_job_id}: {ocr_err}")

                # AI Summary & Clinical Reasoning (Layer 1 reasoning -> Layer 2 multi-audience summary)
                summary_report = None
                if canonical_report is not None:
                    try:
                        reasoning = ClinicalReasoningEngine().analyze_report(canonical_report)
                        summary_report = MultiAudienceSummaryGenerator().generate_summary(canonical_report, reasoning)
                        self._emit_event("AISummarized", report_job_id, corr_id, {"status": summary_report.status.value})
                    except Exception as ai_err:
                        logger.warning(f"AI Summary generation warning for {report_job_id}: {ai_err}")

                # WhatsApp Delivery — Two strategies:
                # Strategy 1 (primary): CallMedex's own AI pipeline produced a summary_report →
                #   send via WhatsAppDeliveryService with the structured multi-audience summary.
                # Strategy 2 (fallback): OCR or AI failed (summary_report is None) →
                #   fall back to LabReportService.upload_and_send() which has its own independent
                #   ReportSummarizer pipeline (PDF text extraction → OpenRouter). This ensures
                #   patients ALWAYS receive the report + best-effort AI summary, never silence.
                whatsapp_sent = False
                whatsapp_message_id = None
                storage_path = None
                used_fallback_path = False
                if patient_phone:
                    try:
                        from app.database import supabase as _supabase
                        storage_path = f"callmedex/{request.clinic_id}/{report_job_id}.pdf"
                        # Blocking storage HTTP: run off the web event loop.
                        await asyncio.to_thread(
                            _supabase.storage.from_("lab-reports").upload,
                            storage_path, pdf_bytes, {"content-type": "application/pdf"},
                        )
                        signed = await asyncio.to_thread(
                            _supabase.storage.from_("lab-reports").create_signed_url, storage_path, 86400
                        )
                        pdf_url = signed.get("signedURL") or signed.get("signedUrl")

                        if summary_report is not None and pdf_url:
                            # Strategy 1: CallMedex AI summary available → send via template
                            delivery_service = WhatsAppDeliveryService(callback_handler=self.container.callback_handler)
                            delivery_result = await delivery_service.deliver_report_and_summary(
                                phone_number=patient_phone,
                                pdf_storage_url=pdf_url,
                                summary_report=summary_report,
                                report_job_id=report_job_id,
                                correlation_id=corr_id,
                            )
                            whatsapp_sent = delivery_result.status == WhatsAppDeliveryStatus.DELIVERED
                            if whatsapp_sent:
                                whatsapp_message_id = delivery_result.message_id
                        if not whatsapp_sent:
                            # Strategy 2: OCR/AI produced no summary, OR Strategy 1's
                            # send failed (e.g. template not approved on the CallMedex
                            # WABA) → LabReportService, which runs its own
                            # ReportSummarizer and sends from the clinic's own number.
                            # Fail-open: the patient must never get silence.
                            logger.warning(
                                f"CallMedex primary delivery unavailable for {report_job_id} "
                                f"(summary={'yes' if summary_report else 'no'}) "
                                f"— falling back to LabReportService.upload_and_send() for delivery"
                            )
                            try:
                                from app.services.lab_reports import LabReportService
                                fallback_result = await LabReportService().upload_and_send(
                                    clinic_id=request.clinic_id,
                                    file_bytes=pdf_bytes,
                                    filename=f"{request.external_report_id}.pdf",
                                    content_type="application/pdf",
                                    patient_phone=patient_phone,
                                    patient_name=patient_name,
                                    report_name=request.report_name,
                                    report_type=request.report_type.value,
                                    external_report_id=request.external_report_id,
                                    source="callmedex",
                                )
                                whatsapp_sent = fallback_result.get("status") == "sent"
                                whatsapp_message_id = fallback_result.get("whatsapp_message_id")
                                used_fallback_path = True
                                self._emit_event(
                                    "FallbackDelivery", report_job_id, corr_id,
                                    {"sent": whatsapp_sent, "fallback_result_status": fallback_result.get("status")},
                                )
                            except Exception as fallback_err:
                                logger.error(
                                    f"Fallback LabReportService delivery also failed for {report_job_id}: {fallback_err}"
                                )

                        self._emit_event(
                            "WhatsAppDelivered", report_job_id, corr_id,
                            {"phone": patient_phone[-4:], "sent": whatsapp_sent, "fallback": used_fallback_path},
                        )
                    except Exception as wa_err:
                        logger.warning(f"WhatsApp dispatch warning for {report_job_id}: {wa_err}")

                # Persist the processed report — gives the processing center's own dashboard/analytics
                # a record, and backs the /process-report idempotency check in api/router.py.
                # Skip DB insert if fallback path already persisted via LabReportService.upload_and_send()
                if not used_fallback_path:
                    try:
                        from app.database import supabase as _supabase
                        await sb(_supabase.table("lab_reports").insert(
                            {
                                "clinic_id": request.clinic_id,
                                "patient_phone": patient_phone or "",
                                "patient_name": patient_name,
                                "report_name": request.report_name,
                                "report_type": request.report_type.value,
                                "file_path": storage_path or "",
                                "ai_summary": (
                                    " ".join(s.statement for s in summary_report.patient_summary)
                                    if summary_report else None
                                ),
                                "has_abnormal_values": bool(summary_report and summary_report.status.value != "success"),
                                "status": "sent" if whatsapp_sent else "failed",
                                "error_message": None if whatsapp_sent else "CallMedex WhatsApp delivery did not complete",
                                "external_report_id": request.external_report_id,
                                "source": "callmedex",
                                "sent_at": datetime.now(timezone.utc).isoformat() if whatsapp_sent else None,
                            }
                        ))
                    except Exception as db_err:
                        logger.warning(f"Failed to persist lab_reports row for {report_job_id}: {db_err}")



            # Step 9: Signed lifecycle callback to CallMedex (only for CallMedex-created jobs)
            callback_delivered = False
            if cmx_job_id:
                await _settle_callbacks(cmx_tasks)
                if whatsapp_sent:
                    callback_delivered = await callbacks.send_report_delivered(
                        cmx_job_id,
                        _utc_now_iso(),
                        whatsapp_message_id,
                        build_analysis_payload(summary_report, canonical_report),
                        corr_id,
                    )
                else:
                    callback_delivered = await callbacks.send_report_failed(
                        cmx_job_id,
                        _utc_now_iso(),
                        "delivery_failed",
                        "No patient phone on the job" if not patient_phone
                        else "WhatsApp delivery did not complete on either the CallMedex or the clinic number",
                        corr_id,
                    )
            checkpoint = JobCheckpoint.CALLBACK_SENT
            self._emit_event(
                "CallbackDelivered",
                report_job_id,
                corr_id,
                {"callback_delivered": callback_delivered},
            )

            # Step 10: Logout & Dispose Resources
            await connector.logout()
            self._emit_event("Completed", report_job_id, corr_id)

            return ProcessReportResponse(
                success=True,
                task_id=report_job_id,
                message=f"Report {request.external_report_id} processed successfully",
                callback_delivered=callback_delivered,
            )

        except Exception as e:
            if self.container.settings.enable_screenshot_artifacts:
                try:
                    page_handle = getattr(connector, "_page", None)
                    await self.container.browser_session.capture_screenshot(
                        page_handle, f"failure_{report_job_id}"
                    )
                except Exception as ss_err:
                    logger.warning(f"Failed capturing failure screenshot: {ss_err}")

            logger.error(
                f"ReportJob {report_job_id} failed at Checkpoint {checkpoint.value}: {e}",
                extra={"correlation_id": corr_id, "report_job_id": report_job_id},
            )
            if cmx_job_id:
                await _settle_callbacks(cmx_tasks)
                # Deterministic idempotency key per job: queue retries re-send the
                # same failed event, which CallMedex replays instead of re-applying.
                await callbacks.send_report_failed(
                    cmx_job_id, _utc_now_iso(), _failure_reason(e, checkpoint), str(e), corr_id
                )
            raise

        finally:
            # Clean resource cleanup
            if temp_filepath:
                await self.container.storage_provider.cleanup_temp_report(temp_filepath)
            await self.container.browser_session.close_context(session_id)
            if hasattr(connector, "attach_page"):
                connector.attach_page(None)

    async def execute_callmedex_v1_job(
        self, request: CallMedexReportJobRequest, correlation_id: Optional[str] = None
    ) -> None:
        """Process a CallMedex report job from POST /api/v1/report-jobs. Never raises.

        Routing (fails closed on tenancy):
          * processing_center_id given -> must map to an enrolled Kriya clinic,
            else report-failed. Delivery: CallMedex number, then that clinic's
            own number (fail-open for the patient).
          * no processing_center_id (patient self-upload) -> CallMedex number
            only; no clinic's number or lab_reports is ever used.
          * barcode-only jobs (no source_document_url) are refused: scraping
            MocDoc means Chromium in the web container (the 2026-09-02 outage),
            and the centre's own MocDoc connector already delivers them.
        The route's scheduler_locks claim is renewed for 7 days on delivery and
        released on failure so CallMedex's retry worker can resubmit.
        """
        report_job_id = request.report_job_id
        corr_id = correlation_id or str(uuid4())
        callbacks = self.container.callback_handler
        lock_name = report_job_lock_name(report_job_id)

        set_job_status_record(report_job_id, "processing")
        self._emit_event("V1ReportJobStarted", report_job_id, corr_id, {"source_type": request.source_type})
        cmx_tasks: list = [
            spawn_background_task(
                callbacks.send_report_accepted(report_job_id, _utc_now_iso(), corr_id),
                name=f"cmx_v1_acc_{report_job_id}",
            ),
            spawn_background_task(
                callbacks.send_report_processing(report_job_id, _utc_now_iso(), corr_id),
                name=f"cmx_v1_proc_{report_job_id}",
            ),
        ]

        async def finish_failed(reason: str, details: str) -> None:
            logger.warning(f"CallMedex v1 job {report_job_id} failed ({reason}): {details}")
            set_job_status_record(report_job_id, "failed", failure_reason=reason)
            await _settle_callbacks(cmx_tasks)
            await callbacks.send_report_failed(report_job_id, _utc_now_iso(), reason, details, corr_id)
            await distributed_lock_manager.release(lock_name)

        async def finish_delivered(message_id: Optional[str], analysis: dict) -> None:
            set_job_status_record(report_job_id, "delivered")
            await distributed_lock_manager.renew(lock_name, lease_seconds=REPORT_JOB_DONE_LEASE_SECONDS)
            await _settle_callbacks(cmx_tasks)
            await callbacks.send_report_delivered(report_job_id, _utc_now_iso(), message_id, analysis, corr_id)

        try:
            from app.utils.validators import normalize_phone, validate_phone

            patient_phone = normalize_phone(request.patient.phone or "")
            if not validate_phone(patient_phone):
                return await finish_failed("delivery_failed", "Patient phone is missing or invalid")
            if not (request.source_document_url or "").strip():
                return await finish_failed(
                    "download_automation_failed",
                    "source_document_url is required: barcode-only jobs are not scraped by MediAssist "
                    "(the processing centre's own MocDoc connector delivers those reports)",
                )

            clinic_id: Optional[str] = None
            if request.processing_center_id:
                clinic_id = await resolve_callmedex_clinic_id(request.processing_center_id)
                if clinic_id is None:
                    return await finish_failed(
                        "delivery_failed",
                        f"processing_center_id '{request.processing_center_id}' is not mapped to an "
                        f"enrolled MediAssist clinic",
                    )

            from app.database import supabase as _supabase

            if clinic_id and await _prior_report_already_sent(clinic_id, report_job_id):
                # Delivered by an earlier run whose claim has since expired.
                return await finish_delivered(None, build_analysis_payload())

            pdf_bytes = await _download_source_document(request.source_document_url)
            patient_name = request.patient.name or "Patient"
            report_name = "Laboratory Report" if request.source_type == "lab_report" else (request.source_type or "Report")

            canonical_report = summary_report = None
            try:
                canonical_report = self.container.ocr_pipeline.process_pdf(
                    pdf_bytes=pdf_bytes, report_id=report_job_id, patient_id=request.patient.patient_id,
                    barcode=request.barcode or report_job_id, processing_center_id=clinic_id or "callmedex",
                )
                reasoning = ClinicalReasoningEngine().analyze_report(canonical_report)
                summary_report = MultiAudienceSummaryGenerator().generate_summary(canonical_report, reasoning)
            except Exception as ai_err:  # fail-open: the raw PDF still goes out
                logger.warning(f"CallMedex v1 job {report_job_id}: OCR/summary unavailable: {ai_err}")

            # Our own 24h signed link (CallMedex's expires in 1h).
            pdf_url = request.source_document_url
            storage_path = f"callmedex/{clinic_id or 'patient_uploads'}/{report_job_id}.pdf"
            try:
                await asyncio.to_thread(
                    _supabase.storage.from_("lab-reports").upload,
                    storage_path, pdf_bytes, {"content-type": "application/pdf", "upsert": "true"},
                )
                signed = await asyncio.to_thread(
                    _supabase.storage.from_("lab-reports").create_signed_url, storage_path, 86400
                )
                pdf_url = signed.get("signedURL") or signed.get("signedUrl") or pdf_url
            except Exception as stor_err:
                storage_path = ""
                logger.warning(f"CallMedex v1 job {report_job_id}: storage upload failed, using source URL: {stor_err}")

            whatsapp_sent, whatsapp_message_id, used_fallback = False, None, False
            if summary_report is not None:
                result = await WhatsAppDeliveryService(callback_handler=callbacks).deliver_report_and_summary(
                    phone_number=patient_phone, pdf_storage_url=pdf_url, summary_report=summary_report,
                    report_job_id=report_job_id, correlation_id=corr_id,
                )
                whatsapp_sent = result.status == WhatsAppDeliveryStatus.DELIVERED
                whatsapp_message_id = result.message_id if whatsapp_sent else None

            if not whatsapp_sent and clinic_id:
                try:
                    from app.services.lab_reports import LabReportService

                    fb = await LabReportService().upload_and_send(
                        clinic_id=clinic_id, file_bytes=pdf_bytes, filename=f"{report_job_id}.pdf",
                        content_type="application/pdf", patient_phone=patient_phone, patient_name=patient_name,
                        report_name=report_name, report_type="Laboratory",
                        external_report_id=report_job_id, source="callmedex",
                    )
                    used_fallback = True
                    whatsapp_sent = fb.get("status") == "sent"
                    whatsapp_message_id = fb.get("whatsapp_message_id")
                except Exception as fb_err:
                    logger.error(f"CallMedex v1 job {report_job_id}: clinic-number fallback failed: {fb_err}")

            if clinic_id and not used_fallback:
                try:
                    await sb(_supabase.table("lab_reports").insert({
                        "clinic_id": clinic_id,
                        "patient_phone": patient_phone,
                        "patient_name": patient_name,
                        "report_name": report_name,
                        "report_type": "Laboratory",
                        "file_path": storage_path,
                        "ai_summary": " ".join(s.statement for s in summary_report.patient_summary) if summary_report else None,
                        "has_abnormal_values": bool(summary_report and summary_report.status.value != "success"),
                        "status": "sent" if whatsapp_sent else "failed",
                        "error_message": None if whatsapp_sent else "CallMedex WhatsApp delivery did not complete",
                        "external_report_id": report_job_id,
                        "source": "callmedex",
                        "sent_at": _utc_now_iso() if whatsapp_sent else None,
                    }))
                except Exception as db_err:
                    logger.warning(f"CallMedex v1 job {report_job_id}: lab_reports insert failed: {db_err}")

            if whatsapp_sent:
                return await finish_delivered(whatsapp_message_id, build_analysis_payload(summary_report, canonical_report))
            return await finish_failed(
                "delivery_failed",
                "WhatsApp delivery did not complete" + ("" if clinic_id else " (CallMedex number only: no processing centre)"),
            )

        except ValidationError as e:
            await finish_failed("invalid_source_document", str(e))
        except Exception as e:
            logger.exception(f"CallMedex v1 job {report_job_id} crashed")
            await finish_failed("interpretation_failed", f"{type(e).__name__}: {str(e)[:300]}")


# ── In-Memory Job Status Cache for Fast Polling ─────────────────────────────

_REPORT_JOB_STATUS_STORE: Dict[str, Dict[str, Any]] = {}


def get_job_status_record(job_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve in-memory job status."""
    return _REPORT_JOB_STATUS_STORE.get(job_id)


def set_job_status_record(job_id: str, status: str, failure_reason: Optional[str] = None):
    """Update in-memory job status."""
    _REPORT_JOB_STATUS_STORE[job_id] = {
        "report_job_id": job_id,
        "status": status,
        "failure_reason": failure_reason,
        "updated_at": datetime.now(timezone.utc),
    }

# ── CallMedex v1 report-job helpers ─────────────────────────────────────────

REPORT_JOB_CLAIM_LEASE_SECONDS = 30 * 60        # in-flight claim; expires if the process dies
REPORT_JOB_DONE_LEASE_SECONDS = 7 * 24 * 3600   # delivered: swallow resubmissions for a week
_MAX_SOURCE_PDF_BYTES = 25 * 1024 * 1024


def report_job_lock_name(report_job_id: str) -> str:
    return f"callmedex_report_job:{report_job_id}"


def _allowed_document_host(url: str) -> bool:
    """CallMedex hands out Supabase Storage signed URLs. Anything else (plain
    http, internal hosts, arbitrary sites) is refused: the URL comes from an
    external system and is fetched from inside our network."""
    from urllib.parse import urlparse

    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    cmx_host = (urlparse(callmedex_settings.callmedex_base_url or "").hostname or "").lower()
    return host.endswith(".supabase.co") or (bool(cmx_host) and host == cmx_host)


async def _download_source_document(url: str) -> bytes:
    import httpx

    if not _allowed_document_host(url):
        raise ValidationError("source_document_url must be an https Supabase Storage (or CallMedex) URL")
    buf = bytearray()
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise ValidationError(f"Could not download source document: HTTP {resp.status_code}")
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > _MAX_SOURCE_PDF_BYTES:
                        raise ValidationError("Source document exceeds 25 MB")
    except httpx.HTTPError as e:
        raise ValidationError(f"Could not download source document ({type(e).__name__})") from e
    if not bytes(buf[:5]).startswith(b"%PDF"):
        raise ValidationError("Source document is not a PDF")
    return bytes(buf)


async def _prior_report_already_sent(clinic_id: str, report_job_id: str) -> bool:
    """True if this CallMedex job was already delivered for this clinic.

    Otherwise clears any earlier failed/crashed attempt's row: CallMedex's
    retry worker resubmits the same report_job_id, and a stale row makes
    upload_and_send report "already processed" and our insert collide, so the
    retry could never succeed. Safe: the route's lease guarantees no other
    attempt for this job is in flight.
    """
    from app.database import supabase

    prior = await sb(supabase.table("lab_reports").select("status")
                     .eq("clinic_id", clinic_id).eq("external_report_id", report_job_id))
    if prior.data and prior.data[0].get("status") == "sent":
        return True
    if prior.data:
        await sb(supabase.table("lab_reports").delete()
                 .eq("clinic_id", clinic_id).eq("external_report_id", report_job_id)
                 .neq("status", "sent"))
    return False
