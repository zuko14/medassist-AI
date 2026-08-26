"""Regression guard: `status='pending_retry'` in failed_messages must be read back.

conversation.handle_message() parks lock-timeout messages there and the comment
promised "automatic retry", but nothing drained the table — every timed-out
patient message was lost until the 30-day purge deleted it.
"""

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scheduler import SchedulerService


@asynccontextmanager
async def _lock_granted(*args, **kwargs):
    yield True


def _payload(**kw):
    base = {"message": "Hi", "message_type": "text", "message_id": "wamid.1", "clinic_id": "clinic-1"}
    base.update(kw)
    return json.dumps(base)


def _supabase(rows):
    """failed_messages mock supporting both the select chain and update chain."""
    db = MagicMock()
    table = MagicMock()
    table.select.return_value.eq.return_value.execute.return_value = MagicMock(data=rows)
    db.table.return_value = table
    return db, table


def _updates(table):
    return [c.args[0] for c in table.update.call_args_list]


@pytest.mark.asyncio
async def test_fresh_pending_retry_message_is_replayed_and_resolved():
    now = datetime.now(timezone.utc).isoformat()
    db, table = _supabase([{"id": "row-1", "phone": "+919876543210", "payload": _payload(), "created_at": now}])
    handle = AsyncMock()

    with patch("app.services.distributed_lock.distributed_job_lock", _lock_granted), \
         patch("app.services.scheduler.supabase", db), \
         patch("app.services.conversation.conversation_manager.handle_message", handle), \
         patch("app.services.tenant.get_clinic_by_id", AsyncMock(return_value={"id": "clinic-1"})):
        await SchedulerService().drain_pending_retry_messages()

    handle.assert_awaited_once()
    assert handle.await_args.kwargs["message"] == "Hi"
    assert handle.await_args.kwargs["message_id"] == "wamid.1"
    assert any(u.get("status") == "resolved" for u in _updates(table))


@pytest.mark.asyncio
async def test_message_stuck_past_the_window_is_failed_loudly_not_replayed(caplog):
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    db, table = _supabase([{"id": "row-old", "phone": "+919876543210", "payload": _payload(), "created_at": old}])
    handle = AsyncMock()

    with caplog.at_level(logging.ERROR), \
         patch("app.services.distributed_lock.distributed_job_lock", _lock_granted), \
         patch("app.services.scheduler.supabase", db), \
         patch("app.services.conversation.conversation_manager.handle_message", handle):
        await SchedulerService().drain_pending_retry_messages()

    assert not handle.called  # giving up must not also deliver
    assert any(u.get("status") == "failed" for u in _updates(table))
    assert "ALERT dlq_drain" in "\n".join(r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_failed_replay_stays_queued_for_the_next_cycle():
    now = datetime.now(timezone.utc).isoformat()
    db, table = _supabase([{"id": "row-2", "phone": "+919876543210", "payload": _payload(), "created_at": now}])
    handle = AsyncMock(side_effect=Exception("Groq still down"))

    with patch("app.services.distributed_lock.distributed_job_lock", _lock_granted), \
         patch("app.services.scheduler.supabase", db), \
         patch("app.services.conversation.conversation_manager.handle_message", handle), \
         patch("app.services.tenant.get_clinic_by_id", AsyncMock(return_value={"id": "clinic-1"})):
        await SchedulerService().drain_pending_retry_messages()

    statuses = [u.get("status") for u in _updates(table)]
    assert "resolved" not in statuses and "failed" not in statuses
