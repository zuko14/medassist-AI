# tests/test_render_yaml.py
"""Regression guard: connector polling must be scheduled in production.

Architecture change (2026-08-26): connector polling was moved from a
standalone Render worker process into the main FastAPI scheduler
(app/services/scheduler.py → connector_polling job).  This test now
verifies that the scheduler wires the polling job, rather than checking
for a dedicated worker entry in render.yaml.
"""

from pathlib import Path
import yaml

RENDER_YAML = Path(__file__).parent.parent / "render.yaml"


def test_render_yaml_web_service_exists():
    """The production web service must still be declared in render.yaml."""
    config = yaml.safe_load(RENDER_YAML.read_text())
    services = config.get("services", [])
    web = next((s for s in services if s.get("type") == "web" and s.get("name") == "mediassist-ai"), None)
    assert web is not None, "render.yaml must declare the mediassist-ai web service"
    assert web.get("healthCheckPath") == "/health"


def test_connector_polling_integrated_in_scheduler():
    """Connector polling must be registered in the main FastAPI scheduler."""
    scheduler_path = Path(__file__).parent.parent / "app" / "services" / "scheduler.py"
    scheduler_src = scheduler_path.read_text(encoding="utf-8")

    assert "run_all_connectors" in scheduler_src, (
        "scheduler.py must import run_all_connectors from connectors.runner"
    )
    assert "connector_polling" in scheduler_src, (
        "scheduler.py must register a 'connector_polling' job"
    )
    assert "cleanup_expired_storage" in scheduler_src, (
        "scheduler.py must register the connector storage cleanup job"
    )
