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
        manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as mock_list:
        await manager._start_booking(clinic, "+919876543210", patient, "en")

        mock_list.assert_called_once()
        rows = mock_list.call_args[1]["sections"][0]["rows"]
        row_ids = [r["id"] for r in rows]
        assert "fam_self" in row_ids
        assert "fam_new" in row_ids
        assert "fam_0" in row_ids


@pytest.mark.asyncio
async def test_booking_lists_all_saved_family_members_not_just_first():
    """Regression test: previously only the first saved family member was
    ever shown, because the button UI hard-capped the list to saved_family[:1]."""
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
    patient = {"id": "p-1", "name": "Ramesh Sharma", "phone": "+919876543210", "language": "en"}

    saved_family = [
        {"id": "fam-1", "full_name": "Priya Sharma", "relationship": "Daughter"},
        {"id": "fam-2", "full_name": "Suresh Sharma", "relationship": "Son"},
        {"id": "fam-3", "full_name": "Lakshmi Sharma", "relationship": "Wife"},
    ]

    with patch(
        "app.services.conversation.get_family_members",
        new_callable=AsyncMock,
        return_value=saved_family,
    ), patch.object(
        manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as mock_list:
        await manager._start_booking(clinic, "+919876543210", patient, "en")

        rows = mock_list.call_args[1]["sections"][0]["rows"]
        row_titles = [r["title"] for r in rows]
        assert "Priya Sharma" in row_titles
        assert "Suresh Sharma" in row_titles
        assert "Lakshmi Sharma" in row_titles
        row_ids = [r["id"] for r in rows]
        assert "fam_0" in row_ids
        assert "fam_1" in row_ids
        assert "fam_2" in row_ids


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
