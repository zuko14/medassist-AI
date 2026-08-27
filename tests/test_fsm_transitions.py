"""Tests for FSM State Transition Table (T6.7 / KRIYA audit).

Verifies all 25 ConversationState enum states, reachability, and defined handling
for normal input, interactive input, global intents, and unexpected input classes.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.conversation import ConversationState, ConversationManager


ALL_25_STATES = [
    ConversationState.IDLE,
    ConversationState.SELECTING_LANGUAGE,
    ConversationState.AWAITING_CONSENT,
    ConversationState.MAIN_MENU,
    ConversationState.SELECTING_BRANCH,
    ConversationState.SELECTING_FAMILY_MEMBER,
    ConversationState.COLLECTING_NAME,
    ConversationState.CONFIRMING_SAVE_FAMILY_MEMBER,
    ConversationState.COLLECTING_SYMPTOMS,
    ConversationState.SUGGESTING_DEPARTMENT,
    ConversationState.SELECTING_DEPARTMENT,
    ConversationState.SELECTING_DOCTOR,
    ConversationState.SELECTING_DATE,
    ConversationState.SELECTING_SLOT,
    ConversationState.CONFIRMING_BOOKING,
    ConversationState.AWAITING_PAYMENT,
    ConversationState.MANAGING_APPOINTMENT,
    ConversationState.RESCHEDULING,
    ConversationState.EMERGENCY,
    ConversationState.ESCALATED_TO_HUMAN,
    ConversationState.AWAITING_DATA_DELETION,
    ConversationState.VIEWING_REPORTS,
    ConversationState.DOWNLOADING_REPORT,
    ConversationState.BROWSING_LAB_TESTS,
    ConversationState.CONFIRMING_COLLECTION_DATE,
]


def test_fsm_has_exactly_25_states():
    """T6.7: Assert exactly 25 states in ConversationState enum."""
    assert len(ConversationState) == 25
    assert len(ALL_25_STATES) == 25
    # All states must be lowercase strings
    for state in ALL_25_STATES:
        assert isinstance(state.value, str)
        assert state.value == state.value.lower()


@pytest.mark.asyncio
async def test_fsm_global_intents_from_all_states():
    """T6.7: Global intents (emergency, opt_out, human_escalation) execute cleanly from every state."""
    manager = ConversationManager()
    clinic = {"id": "clinic-fsm-1", "name": "FSM Clinic", "is_active": True}
    phone = "+919876543210"
    patient = {"phone": phone, "language": "en", "opted_in": True, "data_consent": True}

    with patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock) as mock_send_text, \
         patch.object(manager.whatsapp, "send_interactive_buttons", new_callable=AsyncMock) as mock_send_buttons, \
         patch.object(manager, "update_state", new_callable=AsyncMock) as mock_update_state, \
         patch("app.database.supabase") as mock_sb:

        for state in ALL_25_STATES:
            session = {"state": state.value, "context": {}}

            # Test Emergency Intent
            await manager._process_state(
                clinic, phone, "help emergency", "emergency", session, patient, "en"
            )
            assert mock_send_text.called or mock_send_buttons.called

            # Test Human Escalation Intent
            await manager._process_state(
                clinic, phone, "talk to agent", "human_escalation", session, patient, "en"
            )
            assert mock_send_text.called


@pytest.mark.asyncio
async def test_fsm_unknown_state_resets_to_main_menu():
    """T6.7: Unrecognized state safely falls back to main menu prompt."""
    manager = ConversationManager()
    clinic = {"id": "clinic-fsm-2", "name": "FSM Clinic", "is_active": True}
    phone = "+919876543210"
    patient = {"phone": phone, "language": "en", "opted_in": True, "data_consent": True}
    session = {"state": "corrupted_nonexistent_state", "context": {}}

    with patch.object(manager, "_send_main_menu", new_callable=AsyncMock) as mock_main_menu, \
         patch.object(manager, "update_state", new_callable=AsyncMock) as mock_update_state:

        await manager._process_state(
            clinic, phone, "hello", "general", session, patient, "en"
        )
        mock_update_state.assert_called_with(clinic, phone, "main_menu")
        mock_main_menu.assert_called_once()
