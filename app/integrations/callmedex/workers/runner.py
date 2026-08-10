"""CallMedex Background Worker Runner & DI Container (Phase 3 & Phase R2 Implementation)."""

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from uuid import uuid4
from app.integrations.callmedex.config.settings import callmedex_settings, CallMedexSettings
from app.integrations.callmedex.api.schemas import (
    ProcessReportRequest,
    ProcessReportResponse,
    CallbackStatusPayload,
    TaskStatus,
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
from app.integrations.callmedex.callbacks.handler import CallMedexCallbackHandler
from app.integrations.callmedex.queue.drivers import InMemoryQueue
from app.integrations.callmedex.api.exceptions import ConfigurationError, CallMedexException
from app.integrations.callmedex.config.processing_centers import resolve_processing_center

logger = logging.getLogger(__name__)


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

        self._emit_event("ReportJobCreated", report_job_id, corr_id, {"barcode": request.external_report_id, "connector": connector_type})

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
                patient_phone = getattr(request.patient, "patient_phone", None)
                patient_id = getattr(request.patient, "patient_mrn", None) or patient_phone or "unknown"
                patient_name = getattr(request.patient, "patient_name", "Patient")

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

                # WhatsApp Delivery — CallMedex's own shared Meta identity (not the processing center's own number),
                # via a short-lived signed link to the report PDF buffered in the shared "lab-reports" bucket.
                whatsapp_sent = False
                storage_path = None
                if patient_phone:
                    try:
                        from app.database import supabase as _supabase
                        storage_path = f"callmedex/{request.clinic_id}/{report_job_id}.pdf"
                        _supabase.storage.from_("lab-reports").upload(
                            storage_path, pdf_bytes, {"content-type": "application/pdf"}
                        )
                        signed = _supabase.storage.from_("lab-reports").create_signed_url(storage_path, 86400)
                        pdf_url = signed.get("signedURL") or signed.get("signedUrl")

                        if summary_report is not None and pdf_url:
                            delivery_service = WhatsAppDeliveryService(callback_handler=self.container.callback_handler)
                            delivery_result = await delivery_service.deliver_report_and_summary(
                                phone_number=patient_phone,
                                pdf_storage_url=pdf_url,
                                summary_report=summary_report,
                                report_job_id=report_job_id,
                                correlation_id=corr_id,
                            )
                            whatsapp_sent = delivery_result.status == WhatsAppDeliveryStatus.DELIVERED
                        self._emit_event(
                            "WhatsAppDelivered", report_job_id, corr_id,
                            {"phone": patient_phone[-4:], "sent": whatsapp_sent},
                        )
                    except Exception as wa_err:
                        logger.warning(f"WhatsApp dispatch warning for {report_job_id}: {wa_err}")

                # Persist the processed report — gives the processing center's own dashboard/analytics
                # a record, and backs the /process-report idempotency check in api/router.py.
                try:
                    from app.database import supabase as _supabase
                    _supabase.table("lab_reports").insert(
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
                    ).execute()
                except Exception as db_err:
                    logger.warning(f"Failed to persist lab_reports row for {report_job_id}: {db_err}")


            # Step 9: Send Signed HMAC Callback
            callback_delivered = False
            callback_payload = CallbackStatusPayload(
                task_id=report_job_id,
                clinic_id=request.clinic_id,
                connector_type=request.connector_type,
                external_report_id=request.external_report_id,
                status=TaskStatus.COMPLETED,
                correlation_id=corr_id,
            )
            callback_delivered = await self.container.callback_handler.send_status_callback(
                callback_payload
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
            raise

        finally:
            # Clean resource cleanup
            if temp_filepath:
                await self.container.storage_provider.cleanup_temp_report(temp_filepath)
            await self.container.browser_session.close_context(session_id)
            if hasattr(connector, "attach_page"):
                connector.attach_page(None)

