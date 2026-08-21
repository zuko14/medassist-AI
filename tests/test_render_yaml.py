# tests/test_render_yaml.py
"""Regression guard: a worker service must run scheduled connector polling
in production, or every connector run stays purely admin-triggered."""

from pathlib import Path
import yaml

RENDER_YAML = Path(__file__).parent.parent / "render.yaml"


def test_render_yaml_has_connector_polling_worker():
    config = yaml.safe_load(RENDER_YAML.read_text())
    services = config.get("services", [])

    worker = next((s for s in services if s.get("type") == "worker"), None)
    assert worker is not None, "render.yaml must declare a worker service for scheduled connector polling"
    assert worker.get("dockerCommand") == "python -m connectors.runner --all"
    assert worker.get("dockerfilePath") == "./Dockerfile"

    web = next((s for s in services if s.get("type") == "web"), None)
    assert web is not None, "existing web service must still be declared"
