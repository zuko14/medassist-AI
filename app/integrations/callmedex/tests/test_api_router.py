"""CallMedex HTTP API Surface Test Suite (Phase R4 Implementation)."""

import hmac
import hashlib
import time
from typing import Optional
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.integrations.callmedex.config.settings import callmedex_settings

client = TestClient(app)

PREFIX = "/internal/integrations/callmedex"


def get_auth_headers(payload_bytes: bytes = b"", timestamp: Optional[str] = None):
    """Generate valid bearer, timestamp, and HMAC signature headers."""
    secret_key = callmedex_settings.hmac_signature_secret.get_secret_value()
    sig = hmac.new(secret_key.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    return {
        "Authorization": f"Bearer {callmedex_settings.bearer_token.get_secret_value()}",
        "X-Signature-256": sig,
        "X-Timestamp": ts,
        "Content-Type": "application/json",
    }


def test_api_health_endpoint():
    """Verify GET /internal/integrations/callmedex/health endpoint."""
    response = client.get(f"{PREFIX}/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ["ok", "healthy"]
    assert data["integration_api"] is True
    assert data["queue_status"] == "healthy"


def test_api_process_report_unauthorized_missing_bearer():
    """Verify 401 Unauthorized when authorization token is missing or invalid."""
    response = client.post(
        f"{PREFIX}/process-report",
        json={"clinic_id": "test"},
        headers={"Authorization": "Bearer invalid_token_123"},
    )
    assert response.status_code == 401
    assert "Invalid or missing authorization" in response.json()["detail"]


def test_api_process_report_invalid_hmac_signature():
    """Verify 401 Unauthorized when X-Signature-256 header is invalid."""
    headers = {
        "Authorization": f"Bearer {callmedex_settings.bearer_token.get_secret_value()}",
        "X-Signature-256": "0000000000000000000000000000000000000000000000000000000000000000",
        "Content-Type": "application/json",
    }
    body = {"clinic_id": "clinic_1", "external_report_id": "REP-1"}
    response = client.post(f"{PREFIX}/process-report", json=body, headers=headers)
    assert response.status_code == 401
    assert "Invalid HMAC-SHA256 signature" in response.json()["detail"]


def test_api_process_report_stale_timestamp_replay_rejection():
    """Verify 401 Unauthorized when X-Timestamp is outside 5-minute replay window."""
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    headers = {
        "Authorization": f"Bearer {callmedex_settings.bearer_token.get_secret_value()}",
        "X-Timestamp": stale_ts,
        "Content-Type": "application/json",
    }
    body = {"clinic_id": "clinic_1"}
    response = client.post(f"{PREFIX}/process-report", json=body, headers=headers)
    assert response.status_code == 401
    assert "replay window" in response.json()["detail"]


def test_api_process_report_success():
    """Verify valid POST /process-report enqueues job and returns ProcessReportResponse."""
    request_body = {
        "clinic_id": "visakha-multispeciality-clinics",
        "connector_type": "mocdoc",
        "external_report_id": "MOC-API-TEST-99",
        "patient": {
            "patient_phone": "+919966773300",
            "patient_name": "API Test Patient",
            "patient_mrn": "MRN-API-99",
        },
        "report_name": "Complete Blood Count",
        "report_type": "Laboratory",
    }

    import json
    payload_bytes = json.dumps(request_body, separators=(",", ":")).encode("utf-8")
    headers = get_auth_headers(payload_bytes)

    response = client.post(f"{PREFIX}/process-report", json=request_body, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "task_id" in data
    assert data["task_id"] is not None

    # Test GET /jobs/{task_id} status lookup
    task_id = data["task_id"]
    job_response = client.get(f"{PREFIX}/jobs/{task_id}")
    assert job_response.status_code == 200
    job_data = job_response.json()
    assert job_data["task_id"] == task_id
    assert "status" in job_data


def test_api_jobs_status_not_found():
    """Verify GET /jobs/{task_id} returns 404 for unknown task ID."""
    response = client.get(f"{PREFIX}/jobs/non_existent_task_999")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]
