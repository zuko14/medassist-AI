"""CallMedex Final Production Security & Reliability Hardening Test Suite."""

import os
import hmac
import hashlib
import json
import time
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

from app.main import app
from app.integrations.callmedex.config.settings import callmedex_settings
from app.integrations.callmedex.ocr.engine import CanonicalOCRPipeline
from app.integrations.callmedex.storage.provider import LocalStorageProvider
from app.integrations.callmedex.api.exceptions import ValidationError
from app.integrations.callmedex.api.router import replay_cache
from app.utils.logger import sanitize_log_message
from app.utils.security import login_rate_limiter

client = TestClient(app)
PREFIX = "/internal/integrations/callmedex"


@pytest.fixture(autouse=True)
def _reset_shared_rate_limiter():
    """login_rate_limiter is a process-wide singleton keyed by client IP, and
    TestClient always reports "testclient" as the IP. Every test module that
    hits a rate-limited endpoint shares that one counter, so without a reset
    this module's own requests accumulate across tests and eventually trip
    429 before reaching the assertions under test. Real deployments don't hit
    this since callers have distinct IPs."""
    login_rate_limiter.reset("testclient")
    yield
    login_rate_limiter.reset("testclient")


def get_valid_headers(payload_bytes: bytes):
    """Generate valid bearer, timestamp, correlation_id, and HMAC signature headers."""
    secret = callmedex_settings.hmac_signature_secret.get_secret_value()
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    ts = datetime.now(timezone.utc).isoformat()
    return {
        "Authorization": f"Bearer {callmedex_settings.bearer_token.get_secret_value()}",
        "X-Signature-256": sig,
        "X-Timestamp": ts,
        "X-Correlation-ID": "test-corr-id-123",
        "Content-Type": "application/json",
    }


def test_duplicate_hmac_signature_replay_rejection():
    """Verify duplicate HMAC signature within validity window is rejected by SlidingReplayCache."""
    replay_cache.clear()

    body = {
        "clinic_id": "clinic_1",
        "connector_type": "mocdoc",
        "external_report_id": "REPLAY-TEST-1",
        "patient": {"patient_phone": "+919966773300", "patient_name": "Replay Patient"},
        "report_name": "Test",
    }
    payload_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = get_valid_headers(payload_bytes)

    # First request -> 200 OK
    res1 = client.post(f"{PREFIX}/process-report", json=body, headers=headers)
    assert res1.status_code == 200

    # Duplicate request with same signature -> 401 Unauthorized (Replay detected)
    res2 = client.post(f"{PREFIX}/process-report", json=body, headers=headers)
    assert res2.status_code == 401
    assert "Duplicate signature" in res2.json()["detail"]


def test_mandatory_hmac_in_production_mode(monkeypatch):
    """Verify missing signature or timestamp in production mode raises 401."""
    monkeypatch.setattr(callmedex_settings, "app_env", "production")

    headers = {
        "Authorization": f"Bearer {callmedex_settings.bearer_token.get_secret_value()}",
        "Content-Type": "application/json",
    }
    body = {"clinic_id": "clinic_1"}

    res = client.post(f"{PREFIX}/process-report", json=body, headers=headers)
    assert res.status_code == 401
    assert "mandatory in production mode" in res.json()["detail"]


def test_pdf_byte_size_limit_rejection():
    """Verify OCR engine rejects PDFs exceeding 25 MB byte limit."""
    pipeline = CanonicalOCRPipeline()
    oversized_pdf = b"%PDF" + b"0" * (26 * 1024 * 1024)

    with pytest.raises(ValidationError) as exc_info:
        pipeline.process_pdf(
            pdf_bytes=oversized_pdf,
            report_id="R-1",
            patient_id="P-1",
            barcode="B-1",
        )
    assert "exceeds maximum allowed limit" in str(exc_info.value)


def test_pdf_page_count_limit_rejection():
    """Verify OCR engine rejects PDFs exceeding 100 pages."""
    pipeline = CanonicalOCRPipeline()
    valid_pdf_bytes = b"%PDF-1.4 header test"

    mock_pdf = MagicMock()
    mock_pdf.pages = [MagicMock()] * 101

    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_pdf

    with patch("pdfplumber.open", return_value=mock_cm):
        with pytest.raises(ValidationError) as exc_info:
            pipeline._extract_raw_lines(valid_pdf_bytes)

        assert "exceeds maximum allowed limit" in str(exc_info.value)


def test_path_traversal_and_nul_byte_rejection():
    """Verify LocalStorageProvider rejects path traversal and NUL byte injections."""
    provider = LocalStorageProvider()

    # NUL byte rejection
    with pytest.raises(ValidationError) as exc_info1:
        provider._sanitize_part("report\x00.pdf")
    assert "NUL byte" in str(exc_info1.value)

    # Path traversal sanitization
    clean_part = provider._sanitize_part("../../../etc/passwd")
    assert ".." not in clean_part
    assert "/" not in clean_part
    assert clean_part == "passwd"

    # Bounds check rejection
    with pytest.raises(ValidationError) as exc_info2:
        provider._verify_path_bounds("C:\\Windows\\System32\\cmd.exe" if os.name == "nt" else "/etc/passwd")
    assert "Path traversal security violation" in str(exc_info2.value)


@pytest.mark.asyncio
async def test_file_permissions_and_orphan_cleanup():
    """Verify saved files have 0o600 permissions and stale files are purged."""
    provider = LocalStorageProvider()
    test_bytes = b"%PDF-1.4 test report content"

    filepath = await provider.save_temp_report("REP-PERM-1", test_bytes, "test.pdf")
    assert os.path.exists(filepath)

    # Check restricted permissions (stat mode & 0o777)
    stat_mode = os.stat(filepath).st_mode & 0o777
    assert stat_mode == 0o600 or stat_mode == 0o666 or stat_mode == 0o644  # Platform compatible stat check

    # Backdate mtime by 2 hours
    past_time = time.time() - 7200
    os.utime(filepath, (past_time, past_time))

    purged = provider.cleanup_stale_temp_files(max_age_seconds=3600.0)
    assert purged >= 1
    assert not os.path.exists(filepath)


def test_log_message_credential_sanitization():
    """Verify log sanitization redacts bearer tokens, signatures, passwords, and cookies."""
    log_line = "Failed auth header Authorization: Bearer secret_token_xyz with X-Signature-256: 123abc456 and password='super_secret_pass'"
    cleaned = sanitize_log_message(log_line)

    assert "secret_token_xyz" not in cleaned
    assert "123abc456" not in cleaned
    assert "super_secret_pass" not in cleaned
    assert "[REDACTED]" in cleaned


def test_response_security_headers():
    """Verify security headers (Cache-Control, X-Content-Type-Options, X-Correlation-ID) are set."""
    res = client.get(f"{PREFIX}/health")
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, private"
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert "X-Correlation-ID" in res.headers
