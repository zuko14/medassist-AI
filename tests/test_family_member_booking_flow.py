"""Tests for booking for a family member / dependent."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_booking_shows_family_options_when_saved_members_exist():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
    patient = {"id": "p-1", "name": "Ramesh Sharma", "phone": "+919876543210", "language": "en"}

    saved_family = [
        {"id": "fam-1", "full_name": "Priya Sharma", "relationship": "Daughter"},
    ]

    with patch(
        "app.services.conversation.get_family_members",
        new_callable=AsyncMock,
        return_value=saved_family,
    ), patch.object(
        manager.whatsapp, "send_interactive_buttons", new_callable=AsyncMock
    ) as mock_buttons:
        await manager._start_booking(clinic, "+919876543210", patient, "en")

        mock_buttons.assert_called_once()
        buttons = mock_buttons.call_args[1]["buttons"]
        button_ids = [b["id"] for b in buttons]
        assert "fam_self" in button_ids
        assert "fam_new" in button_ids
        assert "fam_0" in button_ids


@pytest.mark.asyncio
async def test_selecting_family_member_sets_patient_name_in_context():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
    context = {
        "family_members": [{"full_name": "Priya Sharma", "relationship": "Daughter"}]
    }

    with patch.object(
        manager, "update_state", new_callable=AsyncMock
    ) as mock_update, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ):
        await manager._handle_selecting_family_member(
            clinic, "+919876543210", "1", context, "en"
        )

        mock_update.assert_called_once()
        new_ctx = mock_update.call_args[0][3]
        assert new_ctx["patient_name"] == "Priya Sharma"
        assert mock_update.call_args[0][2] == "asking_symptoms"
