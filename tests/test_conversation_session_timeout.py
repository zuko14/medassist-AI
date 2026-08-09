"""Regression test: stale booking_context_expires_at must not falsely
time out a fresh booking started after an old abandoned one."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_update_state_to_main_menu_clears_booking_expiry():
    manager = ConversationManager()
    clinic = {"id": "clinic-1"}

    # Simulate an old, already-expired stale value from a prior abandoned booking
    existing_session = {
        "state": "collecting_symptoms",
        "context": {},
        "booking_context_expires_at": "2020-01-01T00:00:00+00:00",
    }

    with patch(
        "app.database.get_conversation", new_callable=AsyncMock
    ) as mock_get_conv, patch("app.database.supabase") as mock_supabase:
        mock_get_conv.return_value = existing_session
        mock_table = mock_supabase.table.return_value
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = None

        await manager.update_state(clinic, "+919876543210", "main_menu", {})

        # Assert the update payload sent to Supabase clears the stale timestamp
        update_call_kwargs = mock_table.update.call_args[0][0]
        assert update_call_kwargs["booking_context_expires_at"] is None
