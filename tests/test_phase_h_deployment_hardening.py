"""Phase H: Deployment and Network Hardening Verification.

Verifies:
1. Dockerfile contains --proxy-headers and --forwarded-allow-ips for reverse proxy operation.
2. SecurityHeadersMiddleware emits complete HTTP security headers on all responses.
3. Health and readiness endpoints respond with valid diagnostic payloads.
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app


def test_dockerfile_proxy_headers_configured():
    """Phase H: Dockerfile must configure --proxy-headers for trusted reverse proxy operation."""
    dockerfile_path = Path("Dockerfile")
    assert dockerfile_path.exists()
    content = dockerfile_path.read_text(encoding="utf-8")
    assert "--proxy-headers" in content
    assert "--forwarded-allow-ips" in content


def test_http_security_headers_enforced():
    """Phase H: Every HTTP response must enforce nosniff, DENY, CSP, HSTS headers."""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200

    headers = response.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert "max-age" in headers.get("Strict-Transport-Security", "")
    assert "default-src 'self'" in headers.get("Content-Security-Policy", "")


def test_health_and_readiness_endpoints():
    """Phase H: Health and readiness endpoints are active and return JSON status."""
    client = TestClient(app)
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert "status" in health_resp.json()

    ready_resp = client.get("/ready")
    assert ready_resp.status_code == 200
    assert "status" in ready_resp.json()
