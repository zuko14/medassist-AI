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
from app.integrations.callmedex.config.processing_centers import (
    resolve_callmedex_clinic_id,
    CALLMEDEX_CENTER_TO_CLINIC,
    DEFAULT_CALLMEDEX_CLINIC_ID,
)
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
    ) as mock_exec:
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
    """Verify POST /api/v1/notifications returns HTTP 202 Accepted."""
    secret, token = auth_credentials
    body = {
        "channel": "whatsapp",
        "recipient": {"phone": "+919876543210", "patient_id": "pat-1"},
        "template": "booking_confirmed",
        "template_data": {"patient_name": "Asha Rao", "service_name": "Blood Test"},
    }
    headers, raw_body = sign_callmedex_request(body, secret, token)

    resp = client.post("/api/v1/notifications", content=raw_body, headers=headers)
    assert resp.status_code == 202
    data = resp.json()
    assert "notification_id" in data
    assert data["status"] == "queued"


@pytest.mark.asyncio
async def test_resolve_callmedex_clinic_id_mapping():
    """Verify CallMedex processing center resolution logic."""
    # 1. Accumax diagnostic center static mapping
    accumax_cmx_id = "e204185b-fd1c-4753-9243-58715d76b51c"
    resolved_accumax = await resolve_callmedex_clinic_id(accumax_cmx_id)
    assert resolved_accumax == "c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c"

    # 2. None / Empty fallback to default clinic
    resolved_default = await resolve_callmedex_clinic_id(None)
    assert resolved_default == DEFAULT_CALLMEDEX_CLINIC_ID
