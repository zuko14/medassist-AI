"""The CallMedex number's booking flow must never touch a clinic's message path,
and a clinic's messages must never reach the CallMedex flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.routers.webhook as webhook


def _msg(mid="wamid.1"):
    return SimpleNamespace(id=mid, from_="919876543210", type="text", text=SimpleNamespace(body="hi"))


@pytest.fixture
def routed(monkeypatch):
    mocks = {
        "resolve_tenant": AsyncMock(return_value={"id": "clinic-1", "name": "C"}),
        "handle_callmedex_inbound": AsyncMock(),
        "is_callmedex_number": AsyncMock(side_effect=lambda pid: pid == "CMX-PNID"),
    }
    for name, m in mocks.items():
        monkeypatch.setattr(webhook, name, m)
    monkeypatch.setattr(webhook.message_queue, "acquire", AsyncMock(return_value=True))
    monkeypatch.setattr(webhook.message_queue, "claim_message", AsyncMock())
    monkeypatch.setattr(webhook.conversation_manager, "handle_message", AsyncMock())
    monkeypatch.setattr(webhook.whatsapp_service, "mark_as_read", AsyncMock())
    return mocks


@pytest.mark.asyncio
async def test_callmedex_number_goes_to_booking_flow_only(routed):
    await webhook.process_message(_msg(), "+91 90000 00000", phone_number_id="CMX-PNID")
    routed["handle_callmedex_inbound"].assert_awaited_once()
    routed["resolve_tenant"].assert_not_awaited()
    webhook.conversation_manager.handle_message.assert_not_awaited()
    webhook.message_queue.acquire.assert_awaited_once_with("wamid.1", clinic_id=None)


@pytest.mark.asyncio
async def test_clinic_number_path_is_unchanged(routed):
    await webhook.process_message(_msg(), "+91 80000 00000", phone_number_id="CLINIC-PNID")
    routed["handle_callmedex_inbound"].assert_not_awaited()
    routed["resolve_tenant"].assert_awaited_once()
    webhook.conversation_manager.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_callmedex_message_is_dropped(routed):
    webhook.message_queue.acquire.return_value = False
    await webhook.process_message(_msg(), "+91 90000 00000", phone_number_id="CMX-PNID")
    routed["handle_callmedex_inbound"].assert_not_awaited()
