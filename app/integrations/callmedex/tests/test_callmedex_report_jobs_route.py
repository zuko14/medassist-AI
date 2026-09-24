"""Test suite for CallMedex external v1 routes:
- POST /api/v1/report-jobs
- GET  /api/v1/report-jobs/{report_job_id}
- POST /api/v1/notifications
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.integrations.callmedex.config.settings import callmedex_settings
from app.integrations.callmedex.config.processing_centers import resolve_callmedex_clinic_id
from app.integrations.callmedex.workers.runner import (
    set_job_status_record,
    get_job_status_record,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def bypass_rate_limiter_for_tests():
    """Bypass rate limiter so consecutive test requests don't hit 429."""
    with patch("app.utils.security.login_rate_limiter.check_and_record", return_value=False):
        yield


def sign_callmedex_request(
    body: dict,
    secret: str,
    token: str,
    timestamp: Optional[str] = None,
    query: str = "",
) -> tuple[dict, bytes]:
    """Generate headers and raw body bytes matching CallMedex mediassist_client format."""
    raw_body = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ts = timestamp or str(int(time.time()))
    message = f"{ts}.".encode("utf-8") + (query.encode("utf-8") if query else b"") + raw_body
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Timestamp": ts,
        "X-Signature": sig,
        "X-Idempotency-Key": "test-idem-key-123",
        "X-Correlation-Id": "test-corr-id-123",
        "Content-Type": "application/json",
    }
    return headers, raw_body



@pytest.fixture
def auth_credentials():
    secret = callmedex_settings.hmac_signature_secret.get_secret_value()
    token = callmedex_settings.bearer_token.get_secret_value()
    return secret, token


def test_v1_report_jobs_unauthorized_missing_token():
    """Verify 401 when Authorization header is absent."""
    body = {
        "report_job_id": "job-test-1",
        "source_type": "lab_report",
        "patient": {"patient_id": "p-1", "phone": "+919876543210"},
    }
    resp = client.post("/api/v1/report-jobs", json=body)
    assert resp.status_code == 401
    assert "Invalid or missing authorization" in resp.json()["detail"]


def test_v1_report_jobs_unauthorized_invalid_signature(auth_credentials):
    """Verify 401 when HMAC signature does not match payload."""
    secret, token = auth_credentials
    body = {
        "report_job_id": "job-test-2",
        "source_type": "lab_report",
        "patient": {"patient_id": "p-2", "phone": "+919876543210"},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Timestamp": str(int(time.time())),
        "X-Signature": "sha256=0000000000000000000000000000000000000000000000000000000000000000",
        "Content-Type": "application/json",
    }
    resp = client.post("/api/v1/report-jobs", json=body, headers=headers)
    assert resp.status_code == 401
    assert "Invalid HMAC-SHA256 signature" in resp.json()["detail"]


def test_v1_report_jobs_unauthorized_stale_timestamp(auth_credentials):
    """Verify 401 when request timestamp is older than 5 minutes."""
    secret, token = auth_credentials
    body = {
        "report_job_id": "job-test-3",
        "source_type": "lab_report",
        "patient": {"patient_id": "p-3", "phone": "+919876543210"},
    }
    stale_ts = str(int(time.time()) - 600)  # 10 minutes ago
    headers, raw_body = sign_callmedex_request(body, secret, token, timestamp=stale_ts)
    resp = client.post("/api/v1/report-jobs", content=raw_body, headers=headers)
    assert resp.status_code == 401
    assert "5-minute replay window" in resp.json()["detail"]


def test_v1_report_jobs_accepted_success(auth_credentials):
    """Verify valid report job submission returns HTTP 202 Accepted with status 'queued'."""
    secret, token = auth_credentials
    body = {
        "report_job_id": "job-success-test-1",
        "source_type": "lab_report",
        "source_document_url": "https://storage.callmedex.example/report.pdf",
        "booking_id": "bk-100",
        "sample_id": "smp-100",
        "processing_center_id": "e204185b-fd1c-4753-9243-58715d76b51c",
        "barcode": "CMX-BC-100",
        "connector_type": "patient_upload",
        "patient": {
            "patient_id": "pat-100",
            "phone": "+919876543210",
            "preferred_language": "en",
            "name": "Jane Doe",
        },
        "delivery": {"channels": ["whatsapp"]},
        "callback_base_url": "https://api.callmedex.example/callbacks",
    }

    headers, raw_body = sign_callmedex_request(body, secret, token)

    # In test mode, runner tries to execute; mock the runner execution to focus on HTTP surface
    with patch(
        "app.integrations.callmedex.api.v1_router.global_runner.execute_callmedex_v1_job",
        new=AsyncMock(),
    ) as mock_exec, patch(
        "app.integrations.callmedex.api.v1_router.distributed_lock_manager.acquire",
        new=AsyncMock(return_value=True),
    ):
        resp = client.post("/api/v1/report-jobs", content=raw_body, headers=headers)
        assert resp.status_code == 202
        data = resp.json()
        assert data["report_job_id"] == "job-success-test-1"
        assert data["status"] == "queued"
        mock_exec.assert_awaited_once()


def test_v1_report_job_status_polling(auth_credentials):
    """Verify GET /api/v1/report-jobs/{report_job_id} returns execution status."""
    secret, token = auth_credentials

    def _make_get_headers(seed: str) -> dict:
        ts = str(int(time.time()))
        # Sign with unique message per request to avoid replay cache
        msg = f"{ts}.seed={seed}".encode("utf-8")
        sig = "sha256=" + hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        return {
            "Authorization": f"Bearer {token}",
            "X-Timestamp": ts,
            "X-Signature": sig,
        }

    # 1. Unknown job -> 404
    resp = client.get("/api/v1/report-jobs/unknown-job-id-999?seed=1", headers=_make_get_headers("1"))
    assert resp.status_code == 404

    # 2. Known in-memory job -> 200
    set_job_status_record("test-poll-job-1", "delivered")
    resp2 = client.get("/api/v1/report-jobs/test-poll-job-1?seed=2", headers=_make_get_headers("2"))
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["report_job_id"] == "test-poll-job-1"
    assert data2["status"] == "delivered"


def test_v1_notifications_endpoint(auth_credentials):
    """Not implemented: must be the contract's 422, never a 202 that CallMedex
    would read as "patient notified"."""
    secret, token = auth_credentials
    body = {
        "channel": "whatsapp",
        "recipient": {"phone": "+919876543210", "patient_id": "pat-1"},
        "template": "booking_confirmed",
        "template_data": {"patient_name": "Asha Rao", "service_name": "Blood Test"},
    }
    headers, raw_body = sign_callmedex_request(body, secret, token)

    resp = client.post("/api/v1/notifications", content=raw_body, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "template_not_supported"


@pytest.mark.asyncio
async def test_resolve_callmedex_clinic_id_mapping():
    """Verify CallMedex processing center resolution logic."""
    # 1. Accumax diagnostic center static mapping
    accumax_cmx_id = "e204185b-fd1c-4753-9243-58715d76b51c"
    resolved_accumax = await resolve_callmedex_clinic_id(accumax_cmx_id)
    assert resolved_accumax == "c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c"

    # 2. Absent / unknown / lookup error -> None (fail closed, never a default clinic)
    assert await resolve_callmedex_clinic_id(None) is None
    with patch("app.integrations.callmedex.config.processing_centers.sb", new=AsyncMock(return_value=MagicMock(data=[]))):
        assert await resolve_callmedex_clinic_id("pc_014") is None
    with patch("app.integrations.callmedex.config.processing_centers.sb", new=AsyncMock(side_effect=RuntimeError("db down"))):
        assert await resolve_callmedex_clinic_id("pc_014") is None
    # 3. An enrolled Kriya clinic id resolves to itself
    with patch("app.integrations.callmedex.config.processing_centers.sb",
               new=AsyncMock(return_value=MagicMock(data=[{"clinic_id": "f13ea1b8-ec12-4d15-82a8-82668b74bd29"}]))):
        assert await resolve_callmedex_clinic_id("F13EA1B8-EC12-4D15-82A8-82668B74BD29") == "f13ea1b8-ec12-4d15-82a8-82668b74bd29"


# ── Production scenarios (2026-09-24 audit of commit 8f68f0d) ────────────────

def _job_body(job_id, **over):
    body = {
        "report_job_id": job_id,
        "source_type": "lab_report",
        "source_document_url": "https://abc.supabase.co/storage/v1/object/sign/reports/x.pdf?token=t",
        "patient": {"patient_id": "pat-1", "phone": "+919876543210", "name": "Asha"},
    }
    body.update(over)
    return body


def test_duplicate_submission_is_idempotent_and_not_reprocessed(auth_credentials):
    secret, token = auth_credentials
    headers, raw = sign_callmedex_request(_job_body("job-dup-1"), secret, token)
    with patch("app.integrations.callmedex.api.v1_router.global_runner.execute_callmedex_v1_job", new=AsyncMock()) as ex, \
         patch("app.integrations.callmedex.api.v1_router.distributed_lock_manager.acquire", new=AsyncMock(return_value=False)), \
         patch("app.integrations.callmedex.api.v1_router._lock_is_held", new=AsyncMock(return_value=True)):
        resp = client.post("/api/v1/report-jobs", content=raw, headers=headers)
    assert resp.status_code == 202 and resp.json()["status"] == "queued"
    ex.assert_not_called()


def test_claim_store_error_returns_503_so_callmedex_retries(auth_credentials):
    secret, token = auth_credentials
    headers, raw = sign_callmedex_request(_job_body("job-503-1"), secret, token)
    with patch("app.integrations.callmedex.api.v1_router.global_runner.execute_callmedex_v1_job", new=AsyncMock()) as ex, \
         patch("app.integrations.callmedex.api.v1_router.distributed_lock_manager.acquire", new=AsyncMock(return_value=False)), \
         patch("app.integrations.callmedex.api.v1_router._lock_is_held", new=AsyncMock(side_effect=RuntimeError("db down"))):
        resp = client.post("/api/v1/report-jobs", content=raw, headers=headers)
    assert resp.status_code == 503
    ex.assert_not_called()


def test_bare_body_signature_is_refused_on_v1(auth_credentials):
    """The legacy scheme does not bind X-Timestamp, so it is replayable."""
    secret, token = auth_credentials
    raw = json.dumps(_job_body("job-bare-1"), separators=(",", ":")).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Timestamp": str(int(time.time())),
        "X-Signature": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
        "Content-Type": "application/json",
    }
    resp = client.post("/api/v1/report-jobs", content=raw, headers=headers)
    assert resp.status_code == 401


def test_production_runs_job_in_background(auth_credentials, monkeypatch):
    from app.config import settings as app_settings

    secret, token = auth_credentials
    headers, raw = sign_callmedex_request(_job_body("job-bg-1"), secret, token)
    monkeypatch.setattr(app_settings, "app_env", "production")
    with patch("app.integrations.callmedex.api.v1_router.global_runner.execute_callmedex_v1_job", new=MagicMock()), \
         patch("app.integrations.callmedex.api.v1_router.distributed_lock_manager.acquire", new=AsyncMock(return_value=True)), \
         patch("app.integrations.callmedex.api.v1_router.spawn_background_task") as spawn:
        resp = client.post("/api/v1/report-jobs", content=raw, headers=headers)
    assert resp.status_code == 202
    spawn.assert_called_once()


# ── Runner: execute_callmedex_v1_job ─────────────────────────────────────────

from app.integrations.callmedex.api.schemas import CallMedexReportJobRequest  # noqa: E402
from app.integrations.callmedex.api.exceptions import ValidationError  # noqa: E402
from app.integrations.callmedex.whatsapp.schemas import WhatsAppDeliveryStatus  # noqa: E402
from app.integrations.callmedex.workers import runner as runner_mod  # noqa: E402


@pytest.fixture
def v1(monkeypatch):
    r = runner_mod.CallMedexWorkerRunner()
    cb = r.container.callback_handler
    for name in ("send_report_accepted", "send_report_processing", "send_report_delivered", "send_report_failed"):
        monkeypatch.setattr(cb, name, AsyncMock(return_value=True))
    lock = MagicMock(release=AsyncMock(return_value=True), renew=AsyncMock(return_value=True))
    monkeypatch.setattr(runner_mod, "distributed_lock_manager", lock)
    monkeypatch.setattr(runner_mod, "_download_source_document", AsyncMock(return_value=b"%PDF-1.4 x"))
    monkeypatch.setattr(r.container.ocr_pipeline, "process_pdf", MagicMock(side_effect=RuntimeError("no text")))
    fake_sb = MagicMock()
    fake_sb.storage.from_.return_value.create_signed_url.return_value = {"signedURL": "https://s/x.pdf"}
    monkeypatch.setattr("app.database.supabase", fake_sb)
    db_calls = []

    async def fake_sb_exec(q):
        db_calls.append(q)
        return MagicMock(data=[])

    monkeypatch.setattr(runner_mod, "sb", fake_sb_exec)
    return r, cb, lock, fake_sb, db_calls


def _req(**over):
    return CallMedexReportJobRequest(**_job_body("job-r-1", **over))


def _with_summary(r, monkeypatch):
    summary = MagicMock()
    summary.status.value = "success"
    summary.patient_summary = []
    summary.clinician_summary = []
    monkeypatch.setattr(r.container.ocr_pipeline, "process_pdf", MagicMock(return_value=MagicMock(tests=[])))
    monkeypatch.setattr(runner_mod, "ClinicalReasoningEngine", MagicMock())
    monkeypatch.setattr(runner_mod, "MultiAudienceSummaryGenerator",
                        MagicMock(return_value=MagicMock(generate_summary=MagicMock(return_value=summary))))


@pytest.mark.asyncio
async def test_patient_upload_never_touches_a_clinic(v1, monkeypatch):
    """No processing_center_id = patient self-upload: CallMedex number only.
    The old code defaulted these to Accumx (its number, its lab_reports)."""
    r, cb, lock, fake_sb, _ = v1
    upload = AsyncMock()
    monkeypatch.setattr("app.services.lab_reports.LabReportService.upload_and_send", upload)
    resolve = AsyncMock()
    monkeypatch.setattr(runner_mod, "resolve_callmedex_clinic_id", resolve)
    _with_summary(r, monkeypatch)
    delivered = MagicMock(status=WhatsAppDeliveryStatus.DELIVERED, message_id="wamid.CMX")
    with patch.object(runner_mod.WhatsAppDeliveryService, "deliver_report_and_summary", AsyncMock(return_value=delivered)):
        await r.execute_callmedex_v1_job(_req())
    resolve.assert_not_awaited()
    upload.assert_not_awaited()
    assert ("lab_reports",) not in [c.args for c in fake_sb.table.call_args_list]
    assert cb.send_report_delivered.await_args.args[2] == "wamid.CMX"
    lock.renew.assert_awaited_once()
    lock.release.assert_not_awaited()


def _primary(status_name, message_id="wamid.CMX"):
    result = MagicMock(status=getattr(WhatsAppDeliveryStatus, status_name), message_id=message_id)
    return patch.object(runner_mod.WhatsAppDeliveryService, "deliver_report_and_summary", AsyncMock(return_value=result))


@pytest.mark.asyncio
async def test_patient_upload_undeliverable_fails_without_clinic_fallback(v1, monkeypatch):
    r, cb, lock, *_ = v1
    upload = AsyncMock()
    monkeypatch.setattr("app.services.lab_reports.LabReportService.upload_and_send", upload)
    with _primary("FAILED"):
        await r.execute_callmedex_v1_job(_req())
    upload.assert_not_awaited()
    assert cb.send_report_failed.await_args.args[2] == "delivery_failed"
    lock.release.assert_awaited_once()  # CallMedex's retry worker may resubmit


@pytest.mark.asyncio
async def test_no_ai_summary_still_sends_pdf_from_callmedex_number(v1):
    """Fail-open on content: OCR failed, the PDF still goes out on the CallMedex
    number with a neutral line — never an invented interpretation."""
    r, cb, *_ = v1  # fixture's OCR raises
    deliver = AsyncMock(return_value=MagicMock(status=WhatsAppDeliveryStatus.DELIVERED, message_id="wamid.CMX"))
    with patch.object(runner_mod.WhatsAppDeliveryService, "deliver_report_and_summary", deliver):
        await r.execute_callmedex_v1_job(_req())
    summary = deliver.await_args.kwargs["summary_report"]
    assert summary.patient_summary[0].statement.startswith("Your lab report is attached")
    assert summary.review_flagged is True
    analysis = cb.send_report_delivered.await_args.args[3]
    assert analysis["plain_language_summary"] == "" and analysis["abnormal_flags"] == []


@pytest.mark.asyncio
async def test_unknown_center_fails_closed_before_download(v1, monkeypatch):
    r, cb, lock, *_ = v1
    monkeypatch.setattr(runner_mod, "resolve_callmedex_clinic_id", AsyncMock(return_value=None))
    await r.execute_callmedex_v1_job(_req(processing_center_id="pc_unknown"))
    runner_mod._download_source_document.assert_not_awaited()
    assert cb.send_report_failed.await_args.args[2] == "delivery_failed"
    lock.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_barcode_only_job_never_launches_a_browser(v1, monkeypatch):
    r, cb, *_ = v1
    exec_job = AsyncMock()
    monkeypatch.setattr(r, "execute_report_job", exec_job)
    await r.execute_callmedex_v1_job(_req(source_document_url="", barcode="BC-1", processing_center_id="x"))
    exec_job.assert_not_awaited()
    assert cb.send_report_failed.await_args.args[2] == "download_automation_failed"


@pytest.mark.asyncio
async def test_invalid_phone_fails(v1):
    r, cb, *_ = v1
    await r.execute_callmedex_v1_job(_req(patient={"patient_id": "p", "phone": "123"}))
    assert cb.send_report_failed.await_args.args[2] == "delivery_failed"


@pytest.mark.asyncio
async def test_bad_document_is_invalid_source_document(v1, monkeypatch):
    r, cb, *_ = v1
    monkeypatch.setattr(runner_mod, "_download_source_document", AsyncMock(side_effect=ValidationError("not a PDF")))
    await r.execute_callmedex_v1_job(_req())
    assert cb.send_report_failed.await_args.args[2] == "invalid_source_document"


@pytest.mark.asyncio
async def test_center_job_never_uses_the_clinic_number(v1, monkeypatch):
    """Channel isolation: a CallMedex booking for Accumax is sent ONLY from the
    CallMedex number. If that fails, report-failed — never Accumax's number."""
    r, cb, lock, fake_sb, db_calls = v1
    monkeypatch.setattr(runner_mod, "resolve_callmedex_clinic_id", AsyncMock(return_value="clinic-A"))
    upload = AsyncMock()
    monkeypatch.setattr("app.services.lab_reports.LabReportService.upload_and_send", upload)
    with _primary("FAILED"):
        await r.execute_callmedex_v1_job(_req(processing_center_id="e204185b-fd1c-4753-9243-58715d76b51c"))
    upload.assert_not_awaited()
    assert cb.send_report_failed.await_args.args[2] == "delivery_failed"
    lock.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_center_job_clears_stale_retry_row_and_records_barcode(v1, monkeypatch):
    r, cb, lock, fake_sb, db_calls = v1
    monkeypatch.setattr(runner_mod, "resolve_callmedex_clinic_id", AsyncMock(return_value="clinic-A"))
    rows = iter([MagicMock(data=[{"status": "failed"}])])  # first query: prior attempt's row

    async def sb_exec(q):
        db_calls.append(q)
        return next(rows, MagicMock(data=[]))

    monkeypatch.setattr(runner_mod, "sb", sb_exec)
    with _primary("DELIVERED"):
        await r.execute_callmedex_v1_job(_req(processing_center_id="e204185b", barcode="260700007335"))
    lab = fake_sb.table.return_value
    lab.delete.return_value.eq.return_value.eq.return_value.neq.assert_called_once_with("status", "sent")
    row = lab.insert.call_args.args[0]
    assert row["clinic_id"] == "clinic-A" and row["source"] == "callmedex" and row["status"] == "sent"
    assert row["external_report_id"] == "job-r-1" and row["sample_barcode"] == "260700007335"
    assert cb.send_report_delivered.await_args.args[2] == "wamid.CMX"


@pytest.mark.asyncio
async def test_sample_already_delivered_by_centre_connector_is_not_resent(v1, monkeypatch):
    """Option C, reverse direction: Accumx's MocDoc connector delivered this
    specimen first -> CallMedex does not send a second copy."""
    r, cb, lock, fake_sb, _ = v1
    monkeypatch.setattr(runner_mod, "resolve_callmedex_clinic_id", AsyncMock(return_value="clinic-A"))
    monkeypatch.setattr(runner_mod, "_prior_report_already_sent", AsyncMock(return_value=False))
    monkeypatch.setattr(runner_mod, "_connector_already_sent_sample", AsyncMock(return_value=True))
    deliver = AsyncMock()
    with patch.object(runner_mod.WhatsAppDeliveryService, "deliver_report_and_summary", deliver):
        await r.execute_callmedex_v1_job(_req(processing_center_id="e204185b", barcode="260700007335"))
    deliver.assert_not_awaited()
    runner_mod._download_source_document.assert_not_awaited()
    runner_mod._connector_already_sent_sample.assert_awaited_once_with("clinic-A", "260700007335")
    cb.send_report_delivered.assert_awaited_once()
    lock.renew.assert_awaited_once()


@pytest.mark.asyncio
async def test_connector_sample_lookup_is_tenant_scoped_and_fails_open(monkeypatch):
    q = MagicMock()
    fake = MagicMock()
    fake.table.return_value.select.return_value = q
    q.eq.return_value = q
    q.neq.return_value = q
    q.limit.return_value = q
    monkeypatch.setattr("app.database.supabase", fake)
    monkeypatch.setattr(runner_mod, "sb", AsyncMock(return_value=MagicMock(data=[{"id": "x"}])))
    assert await runner_mod._connector_already_sent_sample("clinic-A", "BC1") is True
    assert ("clinic_id", "clinic-A") in [c.args for c in q.eq.call_args_list]
    q.neq.assert_called_once_with("source", "callmedex")
    monkeypatch.setattr(runner_mod, "sb", AsyncMock(side_effect=RuntimeError("db down")))
    assert await runner_mod._connector_already_sent_sample("clinic-A", "BC1") is False


def test_document_url_allowlist():
    ok = runner_mod._allowed_document_host
    assert ok("https://abc.supabase.co/storage/v1/object/sign/r/x.pdf?token=t")
    assert not ok("http://abc.supabase.co/x.pdf")
    assert not ok("https://169.254.169.254/latest/meta-data")
    assert not ok("https://localhost/x.pdf")
    assert not ok("https://evil.example/x.pdf")
    assert not ok("https://supabase.co.evil.example/x.pdf")
