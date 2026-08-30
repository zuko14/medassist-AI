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
from app.database import supabase, scoped_query
from app.services.lab_reports import LabReportService
from app.database import sb  # T5.1: off-loop query execution

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
    match_confidence: Optional[float] = Form(default=None),
    match_source: Optional[str] = Form(default=None),
    matched_patient_id: Optional[str] = Form(default=None),
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

    # Step 1: Idempotency check (connector processed reports + lab_reports cross-path)
    try:
        existing = (
            # unscoped: checking connector idempotency by external_report_id with clinic scope
            await sb(supabase.table("integration_processed_reports")
            .select("id")
            .eq("clinic_id", clinic_id)
            .eq("connector_type", connector_type)
            .eq("external_report_id", external_report_id))
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

        existing_lr = (
            # unscoped: cross-path duplicate check for existing delivered lab report with clinic scope
            await sb(supabase.table("lab_reports")
            .select("id, status, source")
            .eq("clinic_id", clinic_id)
            .eq("external_report_id", external_report_id))
        )
        if existing_lr.data:
            lr_row = existing_lr.data[0]
            logger.info(
                f"Report {external_report_id} already delivered via another intake "
                f"(source={lr_row.get('source')}) — skipped duplicate send"
            )
            return LabReportResponse(
                success=True,
                already_processed=True,
                lab_report_id=str(lr_row.get("id")) if lr_row.get("id") else None,
                message=f"Report {external_report_id} already processed",
            )
    except Exception as e:
        logger.warning(f"Idempotency check failed (proceeding): {e}")

    # Step 2: Read file bytes
    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file uploaded")

    # A MocDoc session timeout / error page downloads as a valid-looking .pdf.
    # Without this it is summarised to "", sent to the patient as their report,
    # and recorded delivered — never retried. Reject so the connector records a
    # failure and retries next poll.
    if not file_bytes.startswith(b"%PDF"):
        logger.error(
            f"INVALID_PDF for {external_report_id}: missing %PDF header "
            f"({len(file_bytes)} bytes, starts {file_bytes[:16]!r})"
        )
        raise HTTPException(
            status_code=400,
            detail="Downloaded file is not a valid PDF (missing %PDF header)",
        )

    filename = file.filename or f"{external_report_id}.pdf"
    content_type = file.content_type or "application/pdf"

    logger.info(
        f"Received report from {connector_type}: "
        f"{external_report_id} | {report_name} | "
        f"{len(file_bytes)} bytes | patient=***{patient_phone[-4:]}"
    )

    # Step 3: Server-side Patient Matching Verification (P1-2)
    from app.services.patient_match import patient_match_service
    match_res = await patient_match_service.match(
        clinic_id=clinic_id,
        scraped_name=patient_name,
        scraped_phone=patient_phone,
    )
    effective_match_confidence = match_res.match_confidence
    effective_match_source = match_res.match_source
    effective_matched_patient_id = match_res.matched_patient_id
    if match_res.normalized_phone:
        patient_phone = match_res.normalized_phone

    # If matching fails safety gate, route to needs_review instead of dispatching
    if not match_res.is_safe_to_send:
        logger.warning(
            f"Intake held for review by patient match gate for {external_report_id}: {match_res.review_reason}"
        )
        try:
            # unscoped: recording held lab_report in needs_review state with explicit clinic_id
            nr_insert = await sb(supabase.table("lab_reports").insert({
                "clinic_id": clinic_id,
                "patient_phone": patient_phone,
                "patient_name": patient_name,
                "report_name": report_name,
                "report_type": report_type,
                "file_path": f"pending_review/{external_report_id}",
                "status": "needs_review",
                "external_report_id": external_report_id,
                "match_source": effective_match_source,
                "error_message": match_res.review_reason,
            }))
            raw_id = nr_insert.data[0].get("id") if (nr_insert.data and isinstance(nr_insert.data, list) and isinstance(nr_insert.data[0], dict)) else None
            nr_id = str(raw_id) if isinstance(raw_id, (str, int)) else None
        except Exception as e_nr:
            logger.error(f"Failed to record needs_review row: {e_nr}")
            nr_id = None

        return LabReportResponse(
            success=True,
            already_processed=False,
            lab_report_id=nr_id,
            message=f"Report held for review: {match_res.review_reason}",
        )

    # Step 4: Call the SAME LabReportService pipeline as admin panel
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
            match_confidence=effective_match_confidence,
            match_source=effective_match_source,
            matched_patient_id=effective_matched_patient_id,
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
        # unscoped: recording processed report in idempotency tracking log with explicit clinic_id
        await sb(supabase.table("integration_processed_reports").insert(
            {
                "clinic_id": clinic_id,
                "connector_type": connector_type,
                "external_report_id": external_report_id,
                "patient_phone": patient_phone,
                "patient_name": patient_name,
                "report_name": report_name,
                "lab_report_id": lab_report_id,
            }
        ))
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
