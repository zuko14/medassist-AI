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


def test_connector_polling_has_somewhere_to_run():
    """A web service that opts out of polling requires a worker to exist.

    KA-P2-20 moved Playwright/Chromium out of the web process by setting
    RUN_CONNECTORS_IN_WEB=false there and adding a dedicated worker. The
    failure mode of that split is silent: delete the worker (or never
    provision it) while the web services still opt out, and connector polling
    simply stops. No exception is raised, nothing is logged as an error —
    lab reports just quietly stop arriving.

    settings.run_connectors_in_web defaults to True, so a deployment that
    never sets the variable keeps polling and is unaffected. This test only
    fires when render.yaml has EXPLICITLY opted a web service out.
    """
    config = yaml.safe_load(RENDER_YAML.read_text())
    services = config.get("services", [])

    def opts_out(service):
        for env_var in service.get("envVars") or []:
            if env_var.get("key") == "RUN_CONNECTORS_IN_WEB":
                return str(env_var.get("value", "")).strip().lower() in ("false", "0", "no")
        return False  # absent == the default, which is True (still polling)

    opted_out = [
        s.get("name") for s in services if s.get("type") == "web" and opts_out(s)
    ]
    if not opted_out:
        return  # nothing opted out; in-process polling is still in effect

    workers = [
        s for s in services
        if s.get("type") == "worker" and "connectors.runner" in (s.get("dockerCommand") or "")
    ]
    assert workers, (
        f"web service(s) {opted_out} set RUN_CONNECTORS_IN_WEB=false but no "
        f"connector worker is declared. Connector polling would stop silently. "
        f"Either restore the worker or set RUN_CONNECTORS_IN_WEB=true."
    )


def test_main_branch_services_deploy_together():
    """Web and connector worker must never sit on different commits.

    KA-P0-B. Both were autoDeploy: false, so a push deployed neither and the
    two could drift apart indefinitely — the worker running stale connector
    code against a schema the web service had already migrated. They now share
    one setting; this test is what keeps them sharing it.
    """
    config = yaml.safe_load(RENDER_YAML.read_text())
    services = config.get("services", [])

    web = next(s for s in services if s.get("name") == "mediassist-ai")
    worker = next(
        s for s in services
        if s.get("type") == "worker" and "connectors.runner" in (s.get("dockerCommand") or "")
    )

    assert web.get("autoDeploy") == worker.get("autoDeploy"), (
        f"mediassist-ai autoDeploy={web.get('autoDeploy')} but the connector "
        f"worker is {worker.get('autoDeploy')}. Split settings let the two "
        f"services run different commits against one database."
    )
