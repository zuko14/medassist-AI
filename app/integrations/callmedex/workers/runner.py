"""CallMedex Background Worker Runner & DI Container (Phase 3 & Phase R2 Implementation)."""

import logging
from typing import Dict, Any, Optional
from uuid import uuid4
from app.integrations.callmedex.config.settings import callmedex_settings, CallMedexSettings
from app.integrations.callmedex.api.schemas import (
    ProcessReportRequest,
    ProcessReportResponse,
    CallbackStatusPayload,
    TaskStatus,
)
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.connectors.base.connector import JobCheckpoint
from app.integrations.callmedex.browser.session import PlaywrightBrowserSession
from app.integrations.callmedex.storage.provider import LocalStorageProvider
from app.integrations.callmedex.callbacks.handler import CallMedexCallbackHandler
from app.integrations.callmedex.queue.drivers import InMemoryQueue
from app.integrations.callmedex.api.exceptions import ConfigurationError, CallMedexException

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
        self.mocdoc_connector = MocDocConnector(browser_session=self.browser_session)

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
        """Execute a report processing job following the 9-step lifecycle with recovery checkpoints."""
        report_job_id = str(uuid4())
        corr_id = correlation_id or str(uuid4())
        connector = self.container.mocdoc_connector

        self._emit_event("ReportJobCreated", report_job_id, corr_id, {"barcode": request.external_report_id})

        # Step 1: Connector Created & Browser Session Initialized
        self._emit_event("ConnectorInitialized", report_job_id, corr_id, {"connector": "MocDoc"})

        session_id = f"session_{report_job_id}"
        session_context = await self.container.browser_session.create_context(
            session_id, headless=callmedex_settings.browser_headless
        )
        if isinstance(session_context, dict) and session_context.get("page"):
            connector.attach_page(session_context["page"], session_id=session_id)

        temp_filepath = None
        checkpoint = JobCheckpoint.CREATED

        try:
            # Step 2: Configuration Validated
            creds = {
                "username": self.container.settings.mocdoc_username.get_secret_value(),
                "password": self.container.settings.mocdoc_password.get_secret_value(),
            }

            # Step 3: Health Check
            health = await connector.health_check()
            if health.get("status") != "healthy":
                raise ConfigurationError("MocDoc connector health check failed")

            # Step 4: Open Login Page & Login
            if checkpoint == JobCheckpoint.CREATED:
                await connector.open_login_page()
                await connector.login(creds)
                checkpoint = JobCheckpoint.AUTHENTICATED
                self._emit_event("LoginSucceeded", report_job_id, corr_id)

            # Step 5: Search by Barcode
            if checkpoint == JobCheckpoint.AUTHENTICATED:
                metadata = await connector.search_by_barcode(request.external_report_id)
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

            # Step 8: Send Callback
            callback_delivered = False
            if checkpoint == JobCheckpoint.VALIDATED:
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

            # Step 9: Logout & Dispose Resources
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
            connector.attach_page(None)
