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
    ConnectorType,
    PatientIdentity,
    ReportType,
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
        """Process an inbound CallMedex report job from POST /api/v1/report-jobs.

        Handles:
        1. Ingest via direct pre-signed URL (source_document_url) or LIS barcode.
        2. Status progression & callbacks: accepted -> processing -> delivered / failed.
        3. Canonical OCR pipeline -> Clinical AI reasoning -> MultiAudience summary.
        4. WhatsApp delivery: Strategy 1 (CallMedex number) with fallback to Strategy 2 (Clinic number).
        5. lab_reports table persistence (scoped to resolved clinic_id).
        """
        report_job_id = request.report_job_id
        corr_id = correlation_id or str(uuid4())
        callbacks = self.container.callback_handler

        set_job_status_record(report_job_id, "processing")
        self._emit_event("V1ReportJobStarted", report_job_id, corr_id, {"source_type": request.source_type})

        # Non-terminal callbacks
        cmx_tasks: list = []
        cmx_tasks.append(spawn_background_task(
            callbacks.send_report_accepted(report_job_id, _utc_now_iso(), corr_id),
            name=f"cmx_v1_acc_{report_job_id}",
        ))
        cmx_tasks.append(spawn_background_task(
            callbacks.send_report_processing(report_job_id, _utc_now_iso(), corr_id),
            name=f"cmx_v1_proc_{report_job_id}",
        ))

        temp_filepath = None
        patient_phone = request.patient.phone
        patient_name = request.patient.name or "Patient"
        patient_id = request.patient.patient_id
        barcode = request.barcode or report_job_id
        summary_report = None
        canonical_report = None

        try:
            # 1. Resolve clinic_id
            clinic_id = await resolve_callmedex_clinic_id(request.processing_center_id)

            # 2. Check DB idempotency
            try:
                from app.database import supabase as _sb_precheck
                already = (
                    await sb(_sb_precheck.table("lab_reports")
                    .select("id, status")
                    .eq("clinic_id", clinic_id)
                    .eq("external_report_id", barcode))
                )
                if isinstance(already.data, list) and already.data:
                    logger.info(
                        f"Report {barcode} for clinic {clinic_id} already exists in lab_reports. Marking delivered."
                    )
                    set_job_status_record(report_job_id, "delivered")
                    await _settle_callbacks(cmx_tasks)
                    await callbacks.send_report_delivered(
                        report_job_id,
                        _utc_now_iso(),
                        None,
                        {
                            "plain_language_summary": "Report already delivered",
                            "doctor_clinical_summary": "Report already processed and delivered",
                            "health_score": None,
                            "abnormal_flags": [],
                            "recommendations": [],
                        },
                        corr_id,
                    )
                    return
            except Exception as idemp_err:
                logger.debug(f"Idempotency check skipped: {idemp_err}")

            # 3. Acquire PDF document
            pdf_bytes: bytes = b""
            if request.source_document_url and request.source_document_url.strip():
                # Direct download path
                self._emit_event("DownloadingSourceDocument", report_job_id, corr_id, {"url": request.source_document_url[:60]})
                import httpx
                async with httpx.AsyncClient(timeout=30.0) as http_client:
                    dl_resp = await http_client.get(request.source_document_url)
                    if dl_resp.status_code != 200:
                        raise ValidationError(f"Failed to download report PDF from URL: HTTP {dl_resp.status_code}")
                    pdf_bytes = dl_resp.content

                if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
                    raise ValidationError("Downloaded document is empty or not a valid PDF header (%PDF missing)")
                temp_filepath = await self.container.storage_provider.save_temp_report(
                    barcode, pdf_bytes, f"{barcode}.pdf"
                )
            elif request.barcode:
                # EMR Barcode automation path: delegate to MocDoc runner flow
                proc_req = ProcessReportRequest(
                    clinic_id=clinic_id,
                    connector_type=ConnectorType.MOCDOC,
                    external_report_id=request.barcode,
                    patient=PatientIdentity(
                        patient_phone=patient_phone,
                        patient_name=patient_name,
                        patient_mrn=patient_id,
                    ),
                    report_name=request.source_type or "Laboratory Report",
                    report_type=ReportType.LABORATORY,
                    processing_center_id=request.processing_center_id or clinic_id,
                    report_job_id=report_job_id,
                )
                await self.execute_report_job(proc_req, correlation_id=corr_id)
                set_job_status_record(report_job_id, "delivered")
                return
            else:
                raise ValidationError("Neither source_document_url nor barcode was provided in the report job")

            # 4. Canonical OCR
            try:
                canonical_report = self.container.ocr_pipeline.process_pdf(
                    pdf_bytes=pdf_bytes,
                    report_id=report_job_id,
                    patient_id=patient_id,
                    barcode=barcode,
                    processing_center_id=clinic_id,
                )
                self._emit_event("OCRExtracted", report_job_id, corr_id, {"extracted_tests": len(canonical_report.tests)})
            except Exception as ocr_err:
                logger.warning(f"OCR Pipeline extraction warning for {report_job_id}: {ocr_err}")

            # 5. AI Summary & Clinical Reasoning
            if canonical_report is not None:
                try:
                    reasoning = ClinicalReasoningEngine().analyze_report(canonical_report)
                    summary_report = MultiAudienceSummaryGenerator().generate_summary(canonical_report, reasoning)
                    self._emit_event("AISummarized", report_job_id, corr_id, {"status": summary_report.status.value})
                except Exception as ai_err:
                    logger.warning(f"AI Summary generation warning for {report_job_id}: {ai_err}")

            # 6. Delivery via WhatsApp
            whatsapp_sent = False
            whatsapp_message_id = None
            storage_path = None
            used_fallback = False
            pdf_url = request.source_document_url

            if patient_phone:
                try:
                    from app.database import supabase as _supabase
                    storage_path = f"callmedex/{clinic_id}/{report_job_id}.pdf"
                    try:
                        await asyncio.to_thread(
                            _supabase.storage.from_("lab-reports").upload,
                            storage_path, pdf_bytes, {"content-type": "application/pdf"},
                        )
                        signed = await asyncio.to_thread(
                            _supabase.storage.from_("lab-reports").create_signed_url, storage_path, 86400
                        )
                        if signed and (signed.get("signedURL") or signed.get("signedUrl")):
                            pdf_url = signed.get("signedURL") or signed.get("signedUrl")
                    except Exception as stor_err:
                        logger.warning(f"Supabase storage upload warning (using source url): {stor_err}")

                    if summary_report is not None and pdf_url:
                        delivery_service = WhatsAppDeliveryService(callback_handler=callbacks)
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
                        # Strategy 2 fallback
                        try:
                            from app.services.lab_reports import LabReportService
                            fallback_result = await LabReportService().upload_and_send(
                                clinic_id=clinic_id,
                                file_bytes=pdf_bytes,
                                filename=f"{barcode}.pdf",
                                content_type="application/pdf",
                                patient_phone=patient_phone,
                                patient_name=patient_name,
                                report_name=request.source_type or "Laboratory Report",
                                report_type="Laboratory",
                                external_report_id=barcode,
                                source="callmedex",
                            )
                            whatsapp_sent = fallback_result.get("status") == "sent"
                            whatsapp_message_id = fallback_result.get("whatsapp_message_id")
                            used_fallback = True
                        except Exception as fb_err:
                            logger.error(f"Fallback delivery failed for {report_job_id}: {fb_err}")

                except Exception as wa_err:
                    logger.warning(f"WhatsApp delivery flow warning for {report_job_id}: {wa_err}")

            # 7. Persist in lab_reports
            if not used_fallback:
                try:
                    from app.database import supabase as _supabase
                    await sb(_supabase.table("lab_reports").insert(
                        {
                            "clinic_id": clinic_id,
                            "patient_phone": patient_phone or "",
                            "patient_name": patient_name,
                            "report_name": request.source_type or "Laboratory Report",
                            "report_type": "Laboratory",
                            "file_path": storage_path or (request.source_document_url or ""),
                            "ai_summary": (
                                " ".join(s.statement for s in summary_report.patient_summary)
                                if summary_report else None
                            ),
                            "has_abnormal_values": bool(summary_report and summary_report.status.value != "success"),
                            "status": "sent" if whatsapp_sent else "failed",
                            "error_message": None if whatsapp_sent else "CallMedex WhatsApp delivery did not complete",
                            "external_report_id": barcode,
                            "source": "callmedex",
                            "sent_at": _utc_now_iso() if whatsapp_sent else None,
                        }
                    ))
                except Exception as db_err:
                    logger.warning(f"Failed to persist lab_reports row for v1 job {report_job_id}: {db_err}")

            # 8. Terminal Callback & Status Update
            await _settle_callbacks(cmx_tasks)
            if whatsapp_sent:
                set_job_status_record(report_job_id, "delivered")
                await callbacks.send_report_delivered(
                    report_job_id,
                    _utc_now_iso(),
                    whatsapp_message_id,
                    build_analysis_payload(summary_report, canonical_report),
                    corr_id,
                )
            else:
                set_job_status_record(report_job_id, "failed", failure_reason="delivery_failed")
                await callbacks.send_report_failed(
                    report_job_id,
                    _utc_now_iso(),
                    "delivery_failed",
                    "WhatsApp delivery could not be completed on primary or fallback channels",
                    corr_id,
                )

        except Exception as e:
            logger.error(f"V1 ReportJob {report_job_id} failed: {e}", exc_info=True)
            await _settle_callbacks(cmx_tasks)
            reason = "invalid_source_document" if isinstance(e, ValidationError) else "download_automation_failed"
            set_job_status_record(report_job_id, "failed", failure_reason=reason)
            await callbacks.send_report_failed(
                report_job_id, _utc_now_iso(), reason, str(e), corr_id
            )
            raise
        finally:
            if temp_filepath:
                try:
                    await self.container.storage_provider.cleanup_temp_report(temp_filepath)
                except Exception:
                    pass


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


