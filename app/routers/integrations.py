"""Internal Integration API — receives lab reports from connectors.

This endpoint is NOT public. It is called only by the connector worker
process using a shared secret (X-Integration-Secret header).

The key design principle: this endpoint does ZERO business logic. It just
validates the request and calls LabReportService.upload_and_send() — the
exact same function that the admin panel uses for manual uploads.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File, Form
from pydantic import BaseModel

from app.config import settings
from app.database import supabase
from app.services.lab_reports import LabReportService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/integrations",
    tags=["integrations"],
    include_in_schema=False,  # Hide from public OpenAPI docs
)


async def verify_integration_secret(
    x_integration_secret: Optional[str] = Header(None),
):
    """Verify the X-Integration-Secret header for machine-to-machine auth."""
    if not settings.integration_secret:
        raise HTTPException(
            status_code=503,
            detail="Integration API not configured (INTEGRATION_SECRET not set)",
        )

    if not x_integration_secret or x_integration_secret != settings.integration_secret:
        logger.warning("Integration API: invalid or missing secret")
        raise HTTPException(
            status_code=401,
            detail="Invalid integration secret",
        )


class LabReportResponse(BaseModel):
    success: bool
    already_processed: bool = False
    lab_report_id: Optional[str] = None
    message: str = ""


@router.post(
    "/lab-report",
    response_model=LabReportResponse,
    dependencies=[Depends(verify_integration_secret)],
)
async def receive_lab_report(
    clinic_id: str = Form(...),
    patient_phone: str = Form(...),
    patient_name: str = Form(...),
    report_name: str = Form(...),
    report_type: str = Form(default="Laboratory"),
    external_report_id: str = Form(...),
    connector_type: str = Form(default="mocdoc"),
    file: UploadFile = File(...),
):
    """Receive a lab report from a connector and process it.

    This endpoint:
    1. Checks idempotency (has this external_report_id been processed?)
    2. Calls LabReportService.upload_and_send() — the SAME pipeline as admin panel
    3. Records the processed report for idempotency tracking

    The connector only needs to POST the PDF + metadata. All intelligence
    (AI summary, WhatsApp delivery, storage, audit) is handled by the
    existing LabReportService.
    """

    # Step 1: Idempotency check
    try:
        existing = (
            supabase.table("integration_processed_reports")
            .select("id")
            .eq("clinic_id", clinic_id)
            .eq("connector_type", connector_type)
            .eq("external_report_id", external_report_id)
            .execute()
        )

        if existing.data:
            logger.info(
                f"Already processed: {external_report_id} "
                f"(connector={connector_type})"
            )
            return LabReportResponse(
                success=True,
                already_processed=True,
                message=f"Report {external_report_id} already processed",
            )
    except Exception as e:
        logger.warning(f"Idempotency check failed (proceeding): {e}")

    # Step 2: Read file bytes
    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file uploaded")

    filename = file.filename or f"{external_report_id}.pdf"
    content_type = file.content_type or "application/pdf"

    logger.info(
        f"Received report from {connector_type}: "
        f"{external_report_id} | {report_name} | "
        f"{len(file_bytes)} bytes | patient=***{patient_phone[-4:]}"
    )

    # Step 3: Call the SAME LabReportService pipeline as admin panel
    # external_report_id/source are passed through so upload_and_send() can check
    # lab_reports for a duplicate delivered by another intake path (e.g. CallMedex)
    # before sending — this is what actually prevents the double-send.
    try:
        lab_service = LabReportService()
        saved_record = await lab_service.upload_and_send(
            clinic_id=clinic_id,
            file_bytes=file_bytes,
            filename=filename,
            content_type=content_type,
            patient_phone=patient_phone,
            patient_name=patient_name,
            report_name=report_name,
            report_type=report_type,
            external_report_id=external_report_id,
            source=connector_type,
        )
    except Exception as e:
        logger.error(f"LabReportService failed for {external_report_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process report: {type(e).__name__}: {str(e)[:200]}",
        )

    if saved_record.get("already_processed"):
        logger.info(
            f"Report {external_report_id} already delivered via another intake "
            f"(source={saved_record.get('source')}) — skipped duplicate send"
        )
        return LabReportResponse(
            success=True,
            already_processed=True,
            lab_report_id=str(saved_record.get("id")) if saved_record.get("id") else None,
            message=f"Report {external_report_id} already processed",
        )

    # Step 4: Record the processed report (idempotency)
    lab_report_id = saved_record.get("id")
    try:
        supabase.table("integration_processed_reports").insert(
            {
                "clinic_id": clinic_id,
                "connector_type": connector_type,
                "external_report_id": external_report_id,
                "patient_phone": patient_phone,
                "patient_name": patient_name,
                "report_name": report_name,
                "lab_report_id": lab_report_id,
            }
        ).execute()
    except Exception as e:
        # Don't fail the whole request — the report is already sent
        logger.error(f"Failed to record processed report: {e}")

    return LabReportResponse(
        success=True,
        lab_report_id=str(lab_report_id) if lab_report_id else None,
        message=f"Report {external_report_id} processed successfully",
    )


@router.get("/health")
async def integration_health():
    """Health check for the integration API."""
    configured = bool(settings.integration_secret)
    return {
        "status": "ok" if configured else "unconfigured",
        "integration_api": configured,
    }
