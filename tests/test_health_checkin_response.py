"""Tests for patient response to the post-discharge health check-in."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_health_checkin_concern_sends_contact_and_logs_event():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

    with patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send, patch(
        "app.services.conversation.log_analytics_event", new_callable=AsyncMock
    ) as mock_log:
        await manager._handle_health_checkin_concern(clinic, "+919876543210", "en")

        mock_send.assert_called_once()
        mock_log.assert_called_once()
        assert mock_log.call_args[0][2] == "discharge_checkin_concern"


@pytest.mark.asyncio
async def test_health_checkin_ok_sends_acknowledgement():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

    with patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock) as mock_send:
        await manager._handle_health_checkin_ok(clinic, "+919876543210", "en")
        mock_send.assert_called_once()
