"""Tests for staff alert on emergency detection."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_emergency_alerts_staff_when_configured():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

    with patch("app.services.conversation.settings") as mock_settings, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send, patch(
        "app.services.conversation.log_analytics_event", new_callable=AsyncMock
    ), patch.object(manager, "update_state", new_callable=AsyncMock):
        mock_settings.hospital_staff_alert_number = "+919999999999"
        mock_settings.hospital_emergency_number = "108"
        mock_settings.hospital_maps_link = ""
        mock_settings.hospital_address = ""

        await manager._handle_emergency(clinic, "+918888888888", "en")

        # Two sends: one to the patient, one to staff
        assert mock_send.call_count == 2
        staff_call = mock_send.call_args_list[1]
        assert staff_call.args[1] == "+919999999999"


@pytest.mark.asyncio
async def test_emergency_skips_staff_alert_when_not_configured():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

    with patch("app.services.conversation.settings") as mock_settings, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send, patch(
        "app.services.conversation.log_analytics_event", new_callable=AsyncMock
    ), patch.object(manager, "update_state", new_callable=AsyncMock):
        mock_settings.hospital_staff_alert_number = ""
        mock_settings.hospital_emergency_number = "108"
        mock_settings.hospital_maps_link = ""
        mock_settings.hospital_address = ""

        await manager._handle_emergency(clinic, "+918888888888", "en")

        # Only one send: to the patient
        assert mock_send.call_count == 1
