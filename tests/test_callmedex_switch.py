"""CallMedex owner master switch (migration 158): OFF by default, fails closed, and while OFF every connection between
CallMedex and Kriya stops — both inbound APIs, report jobs, outbound calls / callbacks and the WhatsApp number."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.integrations.callmedex import switch
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

pytestmark = pytest.mark.callmedex_switch_real


@pytest.fixture(autouse=True)
def fresh_cache():
    saved = dict(switch._cache)
    switch._cache.update(on=False, at=float("-inf"), row=None)
    yield
    switch._cache.clear()
    switch._cache.update(saved)


def _off():
    switch._cache.update(on=False, at=float("inf"))


def test_owner_switch_end_to_end_on_real_postgres(api):  # noqa: F811
    _q(api.conn, "UPDATE platform_switches SET enabled = false WHERE key = 'callmedex'")
    assert _q(api.conn, "SELECT column_default FROM information_schema.columns WHERE table_name = 'platform_switches' "
                        "AND column_name = 'enabled'") == "false"
    assert api.client.get("/platform/callmedex/status").json()["enabled"] is False
    for method, path in (("POST", "/api/v1/report-jobs"), ("GET", "/api/v1/report-jobs/x"),
                         ("POST", "/api/v1/notifications"), ("GET", "/internal/integrations/callmedex/health")):
        assert api.client.request(method, path).status_code == 503, path

    on = api.client.put("/platform/callmedex/status", json={"enabled": True})
    assert on.status_code == 200 and on.json()["enabled"] is True and on.json()["updated_by"] == "owner"
    assert api.client.post("/api/v1/report-jobs").status_code != 503          # now reaches CallMedex auth
    assert api.client.get("/internal/integrations/callmedex/health").status_code != 503

    off = api.client.put("/platform/callmedex/status", json={"enabled": False})
    assert off.json()["enabled"] is False
    assert api.client.post("/api/v1/report-jobs").status_code == 503
    assert _q(api.conn, "SELECT count(*) FROM admin_audit_logs WHERE action = 'set_callmedex_integration'") >= 2


@pytest.mark.asyncio
async def test_fails_closed_when_the_switch_cannot_be_read(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(switch, "sb", boom)
    assert await switch.callmedex_enabled() is False


@pytest.mark.asyncio
async def test_outbound_calls_and_callbacks_stop_when_off(monkeypatch):
    from app.integrations.callmedex.api import client
    from app.integrations.callmedex.config.settings import callmedex_settings
    _off()
    monkeypatch.setattr(callmedex_settings, "callmedex_base_url", "https://callmedex.example")
    assert await client.request("POST", "/callbacks/report-delivered", json_body={"report_job_id": "j"}) is None


@pytest.mark.asyncio
async def test_v1_job_queued_before_switch_off_does_not_run():
    from app.integrations.callmedex.api.router import global_runner
    from app.integrations.callmedex.workers.runner import get_job_status_record
    _off()
    req = SimpleNamespace(report_job_id="cmx-job-off-1")
    await global_runner.execute_callmedex_v1_job(req)
    assert get_job_status_record("cmx-job-off-1")["failure_reason"] == "integration_disabled"


@pytest.mark.asyncio
async def test_callmedex_whatsapp_number_is_ignored_when_off(monkeypatch):
    import app.routers.webhook as webhook
    _off()
    handler, acquire = AsyncMock(), AsyncMock(return_value=True)
    monkeypatch.setattr(webhook, "is_callmedex_number", AsyncMock(return_value=True))
    monkeypatch.setattr(webhook, "handle_callmedex_inbound", handler)
    monkeypatch.setattr(webhook, "resolve_tenant", AsyncMock())
    monkeypatch.setattr(webhook.message_queue, "acquire", acquire)
    msg = SimpleNamespace(id="wamid.off", from_="919876543210", type="text", text=SimpleNamespace(body="hi"))
    await webhook.process_message(msg, "+91 90000 00000", phone_number_id="CMX-PNID")
    handler.assert_not_awaited()
    acquire.assert_not_awaited()
    webhook.resolve_tenant.assert_not_awaited()                              # never falls through to a clinic
