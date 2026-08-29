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


# ── Button/list selections must survive the park-and-replay round trip ───────
# The park stored only the reply's *title*, never its ID. A doctor pick whose
# ID is a UUID replayed as the doctor's display name and resolved to nothing —
# the patient's tap was silently dropped after the 15s lock timeout.


@pytest.mark.asyncio
async def test_park_preserves_the_interactive_reply_id():
    from app.services.conversation import conversation_manager

    db = MagicMock()
    interactive = {"id": "8f14e45f-ceea-467a-9d2b-1c1f2c3d4e5f", "type": "list_reply"}

    with patch("app.services.conversation.acquire_phone_lock_with_timeout",
               AsyncMock(return_value=False)),          patch("app.database.supabase", db):
        await conversation_manager.handle_message(
            clinic={"id": "clinic-1", "phone": "+919999999999"},
            phone="+919876543210",
            message="Dr. Anand Rao",           # the list_reply title
            message_type="interactive",
            message_id="wamid.park1",
            interactive_data=interactive,
        )

    parked = db.table.return_value.insert.call_args.args[0]
    assert json.loads(parked["payload"])["interactive_data"] == interactive


@pytest.mark.asyncio
async def test_replay_hands_the_interactive_reply_id_back_to_the_state_machine():
    now = datetime.now(timezone.utc).isoformat()
    interactive = {"id": "8f14e45f-ceea-467a-9d2b-1c1f2c3d4e5f", "type": "list_reply"}
    db, _table = _supabase([{
        "id": "row-i", "phone": "+919876543210", "created_at": now,
        "payload": _payload(message="Dr. Anand Rao", message_type="interactive",
                            interactive_data=interactive),
    }])
    handle = AsyncMock()

    with patch("app.services.distributed_lock.distributed_job_lock", _lock_granted),          patch("app.services.scheduler.supabase", db),          patch("app.services.conversation.conversation_manager.handle_message", handle),          patch("app.services.tenant.get_clinic_by_id", AsyncMock(return_value={"id": "clinic-1"})):
        await SchedulerService().drain_pending_retry_messages()

    assert handle.await_args.kwargs["interactive_data"] == interactive


@pytest.mark.asyncio
async def test_replay_of_a_plain_text_park_passes_no_interactive_data():
    """Older rows parked before interactive_data was stored must still replay."""
    now = datetime.now(timezone.utc).isoformat()
    db, _table = _supabase([{
        "id": "row-legacy", "phone": "+919876543210", "created_at": now,
        "payload": _payload(),  # no interactive_data key at all
    }])
    handle = AsyncMock()

    with patch("app.services.distributed_lock.distributed_job_lock", _lock_granted),          patch("app.services.scheduler.supabase", db),          patch("app.services.conversation.conversation_manager.handle_message", handle),          patch("app.services.tenant.get_clinic_by_id", AsyncMock(return_value={"id": "clinic-1"})):
        await SchedulerService().drain_pending_retry_messages()

    assert handle.await_args.kwargs["interactive_data"] is None
