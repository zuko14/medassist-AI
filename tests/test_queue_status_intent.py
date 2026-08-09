"""Tests for queue status intent detection and conversation response."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.ai_engine import detect_intent
from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_queue_status_keywords_detected():
    assert await detect_intent("my token") == "queue_status"
    assert await detect_intent("what is my token number") == "queue_status"
    assert await detect_intent("queue status") == "queue_status"
    assert await detect_intent("how many patients ahead of me") == "queue_status"


@pytest.mark.asyncio
async def test_queue_status_when_checked_in_shows_token_and_count():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

    with patch(
        "app.services.conversation.get_patient_queue_status",
        new_callable=AsyncMock,
        return_value={
            "checked_in": True,
            "token_number": 12,
            "currently_serving": 9,
            "patients_ahead": 3,
            "doctor_name": "Dr. Rao",
        },
    ), patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send:
        await manager._handle_queue_status(clinic, "+919876543210", "en")

        mock_send.assert_called_once()
        body = mock_send.call_args[0][2]
        assert "12" in body
        assert "3" in body
        assert "Dr. Rao" in body


@pytest.mark.asyncio
async def test_queue_status_when_not_checked_in():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

    with patch(
        "app.services.conversation.get_patient_queue_status",
        new_callable=AsyncMock,
        return_value={"checked_in": False, "doctor_name": "Dr. Rao"},
    ), patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send:
        await manager._handle_queue_status(clinic, "+919876543210", "en")

        mock_send.assert_called_once()
        body = mock_send.call_args[0][2]
        assert "not checked in" in body.lower() or "reception" in body.lower()
