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


def test_w5_2_prometheus_metrics_endpoint(client):
    """W5.2: /metrics returns valid Prometheus formatted plain text."""
    res = client.get("/metrics")
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
    """W6.3 & W6.4: migrations run before traffic, and deploys are not silently skipped.

    This used to assert autoDeploy is False, on the reasoning that a red build
    should not reach production un-gated. KA-P0-B reversed that decision: with
    autoDeploy off, a push deployed NOTHING, so a fix could sit unshipped while
    everyone believed it was live, and the web service and the connector worker
    could run different commits against one database indefinitely. Deploying
    stale code is the more dangerous of the two failures.

    The migration gate is what actually protects a bad deploy, and it is still
    asserted here: preDeployCommand runs migrations before traffic shifts.
    """
    render_yaml_path = Path(__file__).parent.parent / "render.yaml"
    config = yaml.safe_load(render_yaml_path.read_text())

    services = {s["name"]: s for s in config.get("services", [])}
    assert "mediassist-ai" in services
    web = services["mediassist-ai"]

    assert web.get("autoDeploy") is True, (
        "autoDeploy must be true so a push actually ships and the web service "
        "cannot drift onto a different commit than the connector worker"
    )
    assert web.get("preDeployCommand") == "python scripts/migrate.py", "preDeployCommand must run schema migrations"
