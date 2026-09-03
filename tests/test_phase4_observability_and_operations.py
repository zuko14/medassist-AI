"""Phase 4 Observability & Operational Readiness Tests (W5, W6.3-W6.6).

Verifies:
1. W5.1: Request Correlation ID middleware generation and propagation.
2. W5.2: Prometheus /metrics export endpoint structure.
3. W5.3: Metric registry increments on inbound events, DLQ depth, and slot contention.
4. W6.3-W6.4: CI/CD deployment configuration in render.yaml (preDeployCommand, autoDeploy=False).
"""

import pytest
import yaml
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import app
from app.services.metrics import metrics
from app.utils.correlation import get_correlation_id, set_correlation_id


@pytest.fixture
def client():
    return TestClient(app)


def test_w5_1_correlation_id_middleware_and_header(client):
    """W5.1: Requests receive and propagate X-Correlation-ID headers."""
    # 1. Custom incoming header is preserved
    custom_cid = "cid_test_custom_12345"
    res1 = client.get("/health/live", headers={"X-Correlation-ID": custom_cid})
    assert res1.status_code == 200
    assert res1.headers.get("X-Correlation-ID") == custom_cid

    # 2. Requests without header have one auto-generated
    res2 = client.get("/health/live")
    assert res2.status_code == 200
    assert "X-Correlation-ID" in res2.headers
    assert res2.headers["X-Correlation-ID"].startswith("cid_")


def test_w5_2_metrics_endpoint_rejects_unauthenticated_scrapes(client):
    """T3.1/KRIYA-009: /metrics is not public — it exposes clinic traffic volumes."""
    res = client.get("/metrics")
    assert res.status_code == 401


def test_w5_2_prometheus_metrics_endpoint(client, monkeypatch):
    """W5.2: /metrics returns valid Prometheus formatted plain text."""
    from app.config import settings

    monkeypatch.setattr(settings, "metrics_token", "scrape_token_for_test")
    res = client.get(
        "/metrics", headers={"Authorization": "Bearer scrape_token_for_test"}
    )
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    text = res.text

    assert "# TYPE kriya_inbound_messages_total counter" in text
    assert "# TYPE kriya_dlq_depth gauge" in text
    assert "kriya_inbound_messages_total" in text


def test_w5_3_metrics_registry_recording():
    """W5.3: Metrics registry properly records and increments counters and gauges."""
    metrics.inc_counter("kriya_slot_taken_total", 2)
    metrics.inc_counter("kriya_refund_failures_total", 1)
    metrics.set_gauge("kriya_dlq_depth", 5)

    exported = metrics.export_prometheus()
    assert "kriya_slot_taken_total 2.0" in exported
    assert "kriya_refund_failures_total 1.0" in exported
    assert "kriya_dlq_depth 5.0" in exported


def test_w6_3_and_w6_4_render_deployment_config():
    """W6.3 & W6.4: render.yaml specifies preDeployCommand migrations and disables un-gated autoDeploy."""
    render_yaml_path = Path(__file__).parent.parent / "render.yaml"
    config = yaml.safe_load(render_yaml_path.read_text())

    services = {s["name"]: s for s in config.get("services", [])}
    assert "mediassist-ai" in services
    web = services["mediassist-ai"]

    assert web.get("autoDeploy") is False, "autoDeploy must be false to prevent un-gated red builds reaching prod"
    assert web.get("preDeployCommand") == "python scripts/migrate.py", "preDeployCommand must run schema migrations"
