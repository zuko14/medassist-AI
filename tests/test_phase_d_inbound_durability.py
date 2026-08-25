"""Phase D: Durable Inbound Processing and Replay Protection.

Verifies:
1. last_processed_message_id is recorded only after successful message processing.
2. If message processing fails or raises an error, last_processed_message_id is NOT updated.
3. Replaying the same message_id is idempotently dropped at the conversation layer.
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.conversation import ConversationManager


@pytest.fixture
def conversation_service():
    service = ConversationManager()
    service.whatsapp = MagicMock()
    service.whatsapp.mark_as_read = AsyncMock()
    service.whatsapp.send_message = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_last_processed_message_id_persisted_after_success(conversation_service):
    """P1-4: Successful processing persists last_processed_message_id."""
    clinic = {"id": "clinic_test_01", "name": "Test Clinic"}
    phone = "+919999999991"
    msg_id = "wamid.HBgLMTIzNDU2Nzg5MA=="

    fake_session = {"state": "main_menu", "context": {}, "last_processed_message_id": None}

    with patch("app.services.conversation.acquire_phone_lock_with_timeout", new_callable=AsyncMock, return_value=True), \
         patch("app.services.conversation.get_phone_lock", new_callable=AsyncMock) as mock_get_lock, \
         patch("app.services.conversation.release_phone_lock", new_callable=AsyncMock), \
         patch("app.services.conversation.get_or_create_conversation", new_callable=AsyncMock, return_value=fake_session), \
         patch("app.services.conversation.update_conversation", new_callable=AsyncMock) as mock_update_conv, \
         patch("app.services.conversation.get_patient_by_phone", new_callable=AsyncMock, return_value={"id": "pat_1", "language": "en"}), \
         patch.object(conversation_service, "_handle_message_locked", new_callable=AsyncMock) as mock_inner:

        mock_lock = MagicMock()
        mock_get_lock.return_value = mock_lock

        await conversation_service.handle_message(
            clinic=clinic,
            phone=phone,
            message="Hi",
            message_type="text",
            message_id=msg_id,
        )

        mock_inner.assert_called_once()
        mock_update_conv.assert_called_once_with(
            "clinic_test_01", phone, {"last_processed_message_id": msg_id}
        )


@pytest.mark.asyncio
async def test_last_processed_message_id_not_persisted_on_failure(conversation_service):
    """P1-4: Failed processing does NOT mark message_id as processed, allowing retry/replay."""
    clinic = {"id": "clinic_test_01", "name": "Test Clinic"}
    phone = "+919999999991"
    msg_id = "wamid.FAIL123"

    with patch("app.services.conversation.acquire_phone_lock_with_timeout", new_callable=AsyncMock, return_value=True), \
         patch("app.services.conversation.get_phone_lock", new_callable=AsyncMock) as mock_get_lock, \
         patch("app.services.conversation.release_phone_lock", new_callable=AsyncMock), \
         patch("app.services.conversation.update_conversation", new_callable=AsyncMock) as mock_update_conv, \
         patch.object(conversation_service, "_handle_message_locked", new_callable=AsyncMock, side_effect=RuntimeError("Database timeout")):

        mock_lock = MagicMock()
        mock_get_lock.return_value = mock_lock

        with pytest.raises(RuntimeError, match="Database timeout"):
            await conversation_service.handle_message(
                clinic=clinic,
                phone=phone,
                message="Hi",
                message_type="text",
                message_id=msg_id,
            )

        # Ensure update_conversation with last_processed_message_id was NEVER called
        for call_args in mock_update_conv.call_args_list:
            assert "last_processed_message_id" not in call_args[0][2]


@pytest.mark.asyncio
async def test_duplicate_message_id_dropped_idempotently(conversation_service):
    """P1-4: Duplicate webhook message_id is dropped without re-executing state machine."""
    clinic = {"id": "clinic_test_01", "name": "Test Clinic"}
    phone = "+919999999991"
    msg_id = "wamid.ALREADY_PROCESSED"

    fake_session = {"state": "main_menu", "context": {}, "last_processed_message_id": msg_id}

    with patch("app.services.conversation.get_or_create_conversation", new_callable=AsyncMock, return_value=fake_session), \
         patch("app.services.conversation.get_patient_by_phone", new_callable=AsyncMock) as mock_get_pat:

        await conversation_service._handle_message_locked(
            clinic=clinic,
            phone=phone,
            message="Hi again",
            message_type="text",
            message_id=msg_id,
        )

        mock_get_pat.assert_not_called()
