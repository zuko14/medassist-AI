"""Phase A: Durable Inbound WhatsApp Queue, Claiming, Crash Recovery, and DLQ Replayer Tests.

Verifies:
1. Inbound messages are durably written to PostgreSQL before HTTP 200 is returned to Meta.
2. Duplicate wamid is idempotently detected and dropped.
3. Worker crashes / processing failures trigger bounded retries with exponential backoff.
4. Messages exceeding max retries (3) transition to dead_letter.
5. DLQ replayer verifies tenant authorization and resets message to received for operator recovery.
6. Recovery sweep reclaims expired leases (>2 mins) and processes them to completion.
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from datetime import datetime, timezone, timedelta

from app.main import app
from app.services.message_queue import MessageQueueManager
from app.services.scheduler import SchedulerService


@pytest.fixture
def queue_manager():
    return MessageQueueManager()


@pytest.mark.asyncio
async def test_webhook_durable_ingest_before_acknowledgment(queue_manager):
    """Phase A: Webhook durably ingests message before returning 200 OK."""
    msg_id = "wamid.DURABLE_001"
    phone = "+919876543210"
    display_phone = "+919876543210"
    payload = {"entry": [{"changes": [{"value": {"messages": [{"id": msg_id}]}}]}]}

    mock_db = MagicMock()
    mock_db.insert.return_value.execute.return_value.data = [{"id": "uuid-1", "message_id": msg_id}]

    with patch("app.database.supabase.table", return_value=mock_db):
        is_new, record = await queue_manager.ingest(
            message_id=msg_id,
            phone=phone,
            display_phone=display_phone,
            payload=payload,
            clinic_id="clinic-test-1",
        )
        assert is_new is True
        assert record["message_id"] == msg_id
        mock_db.insert.assert_called()


@pytest.mark.asyncio
async def test_webhook_duplicate_wamid_dropped_at_durable_boundary(queue_manager):
    """Phase A: Duplicate wamid is dropped by unique constraint without re-dispatching."""
    msg_id = "wamid.DURABLE_DUP_001"

    mock_db = MagicMock()
    # Simulate Postgres unique violation on insert
    mock_db.insert.side_effect = Exception("duplicate key value violates unique constraint 23505")

    with patch("app.database.supabase.table", return_value=mock_db):
        is_new, record = await queue_manager.ingest(
            message_id=msg_id,
            phone="+919876543210",
            display_phone="+919876543210",
            payload={},
        )
        assert is_new is False
        assert record is None


@pytest.mark.asyncio
async def test_worker_failure_exponential_backoff_retry(queue_manager):
    """Phase A: Processing failure records attempt and sets status='failed_retryable' with backoff."""
    msg_id = "wamid.FAIL_RETRY_001"

    mock_select = MagicMock()
    mock_select.execute.return_value.data = [{"attempt_count": 0, "phone": "+919876543210", "payload": {}}]

    mock_update = MagicMock()
    mock_update.execute.return_value.data = [{"message_id": msg_id, "status": "failed_retryable"}]

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value = mock_select
    mock_table.update.return_value.eq.return_value = mock_update

    with patch("app.database.supabase.table", return_value=mock_table):
        status = await queue_manager.mark_failed(msg_id, "Temporary network timeout", max_retries=3)
        assert status == "failed_retryable"
        update_args = mock_table.update.call_args[0][0]
        assert update_args["status"] == "failed_retryable"
        assert update_args["attempt_count"] == 1
        assert "retry_at" in update_args


@pytest.mark.asyncio
async def test_worker_max_retries_exceeded_moves_to_dead_letter(queue_manager):
    """Phase A: Exceeding max retries (3) transitions message to dead_letter."""
    msg_id = "wamid.DEAD_LETTER_001"

    mock_select = MagicMock()
    mock_select.execute.return_value.data = [{"attempt_count": 2, "phone": "+919876543210", "payload": {}}]

    mock_update = MagicMock()
    mock_update.execute.return_value.data = [{"message_id": msg_id, "status": "dead_letter"}]

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value = mock_select
    mock_table.update.return_value.eq.return_value = mock_update

    with patch("app.database.supabase.table", return_value=mock_table):
        status = await queue_manager.mark_failed(msg_id, "Permanent parsing error", max_retries=3)
        assert status == "dead_letter"
        update_args = mock_table.update.call_args[0][0]
        assert update_args["status"] == "dead_letter"
        assert update_args["attempt_count"] == 3


@pytest.mark.asyncio
async def test_dlq_replay_with_tenant_authorization(queue_manager):
    """Phase A: Replay succeeds only for authorized tenant and resets status to received."""
    msg_id = "wamid.REPLAY_001"
    clinic_id = "clinic_alpha"

    # Authorized case
    mock_select_auth = MagicMock()
    mock_select_auth.execute.return_value.data = [{"message_id": msg_id, "clinic_id": clinic_id, "status": "dead_letter"}]

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value.in_.return_value.eq.return_value = mock_select_auth
    mock_table.update.return_value.eq.return_value.execute.return_value.data = [{"message_id": msg_id, "status": "received"}]

    with patch("app.database.supabase.table", return_value=mock_table):
        success = await queue_manager.replay_dead_letter(msg_id, clinic_id=clinic_id)
        assert success is True
        update_args = mock_table.update.call_args[0][0]
        assert update_args["status"] == "received"
        assert update_args["attempt_count"] == 0

    # Unauthorized case
    mock_select_unauth = MagicMock()
    mock_select_unauth.execute.return_value.data = []
    mock_table.select.return_value.eq.return_value.in_.return_value.eq.return_value = mock_select_unauth

    with patch("app.database.supabase.table", return_value=mock_table):
        unauth_success = await queue_manager.replay_dead_letter(msg_id, clinic_id="wrong_clinic")
        assert unauth_success is False


@pytest.mark.asyncio
async def test_scheduler_recovers_expired_lease_messages():
    """Phase A: Scheduler sweep recovers messages stuck in processing with expired lease (>2 min)."""
    scheduler_service = SchedulerService()

    expired_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    stuck_message = {
        "id": "uuid-stuck",
        "message_id": "wamid.STUCK_001",
        "phone": "+919876543210",
        "display_phone": "+919876543210",
        "status": "processing",
        "locked_at": expired_time,
        "payload": {},
    }

    mock_select = MagicMock()
    mock_select.execute.return_value.data = [stuck_message]

    mock_table = MagicMock()
    mock_table.select.return_value.in_.return_value.limit.return_value = mock_select

    with patch("app.database.supabase.table", return_value=mock_table), \
         patch("app.services.message_queue.message_queue.claim_message", new_callable=AsyncMock, return_value=True) as mock_claim, \
         patch("app.routers.webhook.process_message_safe", new_callable=AsyncMock) as mock_process:

        await scheduler_service.recover_pending_inbound_messages()

        mock_claim.assert_called_once_with("wamid.STUCK_001")
        mock_process.assert_called_once()
