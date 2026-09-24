"""CallMedex External Integration API Router (v1).

Endpoints CallMedex's MediAssistClient calls (callmedex/backend/app/
integrations/mediassist_client.py; contract mediassist-ai.openapi.yaml):
- POST /api/v1/report-jobs
- GET  /api/v1/report-jobs/{report_job_id}
- POST /api/v1/notifications   (not supported yet -> 422, never a silent drop)
"""

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from app.integrations.callmedex.api.schemas import (
    CallMedexReportJobRequest,
    CallMedexReportJobAccepted,
    CallMedexReportJobStatus,
    CallMedexNotificationRequest,
)
from app.integrations.callmedex.api.router import (
    verify_callmedex_auth_and_hmac,
    set_security_headers,
    global_runner,
)
from app.integrations.callmedex.workers.runner import (
    REPORT_JOB_CLAIM_LEASE_SECONDS,
    get_job_status_record,
    report_job_lock_name,
    set_job_status_record,
)
from app.database import sb
from app.services.distributed_lock import distributed_lock_manager
from app.utils.async_tasks import spawn_background_task

logger = logging.getLogger(__name__)


async def require_timestamped_signature(request: Request) -> None:
    """Runs after verify_callmedex_auth_and_hmac. These routes act on patients,
    so only CallMedex's timestamp-bound signature is accepted (the bare-body
    legacy scheme can be replayed with a fresh X-Timestamp)."""
    if not getattr(request.state, "hmac_timestamped", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Signature must be sha256=HMAC(X-Timestamp + '.' + query + body)",
        )


v1_router = APIRouter(
    prefix="/api/v1",
    tags=["CallMedex External Integration API (v1)"],
    include_in_schema=False,
    dependencies=[Depends(verify_callmedex_auth_and_hmac), Depends(require_timestamped_signature)],
)


async def _lock_is_held(job_name: str) -> bool:
    """True if an unexpired scheduler_locks row exists. Raises on DB error."""
    from app.database import supabase

    # unscoped: unique_row_key
    res = await sb(supabase.table("scheduler_locks").select("expires_at").eq("job_name", job_name))
    if not res.data:
        return False
    expires = datetime.fromisoformat(str(res.data[0]["expires_at"]).replace("Z", "+00:00"))
    return expires > datetime.now(timezone.utc)


@v1_router.post(
    "/report-jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CallMedexReportJobAccepted,
)
async def submit_report_job_endpoint(
    request: CallMedexReportJobRequest,
    req_obj: Request,
    response: Response,
):
    """Accept a report job (202) and process it in the background.

    Duplicate protection is cross-worker and survives restarts: a
    scheduler_locks lease named after the report_job_id is taken here, renewed
    for 7 days on delivery, released on failure (so CallMedex's retry worker
    can resubmit), and simply expires if the process dies mid-job.
    """
    corr_id = getattr(req_obj.state, "correlation_id", None) or str(uuid4())
    set_security_headers(response, corr_id)

    lock_name = report_job_lock_name(request.report_job_id)
    if not await distributed_lock_manager.acquire(lock_name, lease_seconds=REPORT_JOB_CLAIM_LEASE_SECONDS):
        try:
            held = await _lock_is_held(lock_name)
        except Exception as e:
            logger.error(f"CallMedex report job {request.report_job_id}: claim check failed: {e}")
            held = False
        if not held:
            # Could not claim and cannot prove a duplicate: let CallMedex retry
            # rather than silently dropping or double-sending a patient report.
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Report job intake temporarily unavailable")
        logger.info(f"CallMedex report job {request.report_job_id} already accepted — duplicate submission ignored")
        return CallMedexReportJobAccepted(report_job_id=request.report_job_id, status="queued")

    set_job_status_record(request.report_job_id, "queued")

    from app.config import settings as app_settings

    job = global_runner.execute_callmedex_v1_job(request, correlation_id=corr_id)
    if app_settings.app_env == "production":
        # Never run OCR/downloads/WhatsApp inside CallMedex's request: its
        # client times out after 20s and retries.
        spawn_background_task(job, name=f"cmx_v1_{request.report_job_id}")
    else:
        await job  # dev/test: deterministic

    return CallMedexReportJobAccepted(report_job_id=request.report_job_id, status="queued")


@v1_router.get("/report-jobs/{report_job_id}", response_model=CallMedexReportJobStatus)
async def get_report_job_status_endpoint(report_job_id: str, req_obj: Request, response: Response):
    """Poll a report job's status. In-memory first (this worker), then lab_reports."""
    corr_id = getattr(req_obj.state, "correlation_id", None) or str(uuid4())
    set_security_headers(response, corr_id)

    record = get_job_status_record(report_job_id)
    if record:
        return CallMedexReportJobStatus(
            report_job_id=report_job_id,
            status=record.get("status", "processing"),
            failure_reason=record.get("failure_reason"),
            updated_at=record.get("updated_at") or datetime.now(timezone.utc),
        )

    try:
        from app.database import supabase

        # unscoped: unique_row_key
        res = await sb(
            supabase.table("lab_reports")
            .select("status, error_message, sent_at, uploaded_at")
            .eq("source", "callmedex")
            .eq("external_report_id", report_job_id)
            .limit(1)
        )
        if res.data:
            row = res.data[0]
            st = {"sent": "delivered", "failed": "failed"}.get(row.get("status"), "processing")
            return CallMedexReportJobStatus(
                report_job_id=report_job_id,
                status=st,
                failure_reason=row.get("error_message") if st == "failed" else None,
                updated_at=row.get("sent_at") or row.get("uploaded_at") or datetime.now(timezone.utc),
            )
    except Exception as db_err:
        logger.warning(f"Error querying lab_reports for report job {report_job_id}: {db_err}")

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Report job '{report_job_id}' not found")


@v1_router.post("/notifications")
async def send_notification_endpoint(request: CallMedexNotificationRequest, req_obj: Request, response: Response):
    """Not implemented yet. Returns the contract's 422 instead of a 202 that
    would make CallMedex believe the patient was notified."""
    corr_id = getattr(req_obj.state, "correlation_id", None) or str(uuid4())
    logger.warning(f"CallMedex notification '{request.template}' rejected: templated notifications not supported yet")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": {"code": "template_not_supported",
                           "message": f"Template '{request.template}' is not supported by MediAssist yet"}},
        headers={"X-Correlation-ID": corr_id, "Cache-Control": "no-store"},
    )
