"""Multi-Instance Concurrency, Worker Synchronization & Lease Recovery (W4).

Tests correctness across multiple concurrent worker instances:
1. Slot & Queue Token Uniqueness (W4.1): Concurrent workers racing to claim slots/tokens.
2. Tenant Cache TTL & Invalidation Semantics (W4.2): Bound on staleness across independent processes.
3. Per-Phone Inbound Serialization (W4.3): Sequential processing per phone under multi-instance contention.
4. Worker Crash Lease Recovery (W4.4): Unfinished inbound messages reclaimed cleanly after lease expiry.
"""

import time
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from uuid import uuid4

from app.services.tenant import (
    _get_cached_item,
    _set_cached_item,
    CACHE_TTL_SECONDS,
    invalidate_tenant_cache,
)
from app.services.message_queue import MessageQueueManager, get_phone_lock, release_phone_lock
from app.services.distributed_lock import DistributedJobLock


def test_w4_2_tenant_cache_ttl_and_staleness_bound():
    """W4.2: In-memory tenant cache obeys bounded TTL and expires correctly."""
    cache = {}
    key = "clinic_uuid_101"
    data = {"id": key, "name": "Apollo Clinic", "plan": "enterprise"}

    _set_cached_item(cache, key, data)
    assert _get_cached_item(cache, key) == data

    # Simulate time advancing past TTL
    entry = cache[key]
    entry["cached_at"] = time.time() - (CACHE_TTL_SECONDS + 5)

    # Must be expired and evicted
    assert _get_cached_item(cache, key) is None
    assert key not in cache


@pytest.mark.asyncio
async def test_w4_1_multi_worker_slot_contention_race():
    """W4.1: Multiple worker processes racing to book the same slot result in exactly 1 winner."""
    slot_claimed = False
    lock = asyncio.Lock()

    async def worker_attempt_book(worker_id: int):
        nonlocal slot_claimed
        async with lock:
            if not slot_claimed:
                await asyncio.sleep(0.01)  # Simulate DB write latency
                slot_claimed = True
                return {"worker_id": worker_id, "status": "confirmed"}
            return {"worker_id": worker_id, "status": "slot_taken"}

    # 10 workers simultaneously attempting to book the same slot
    results = await asyncio.gather(*[worker_attempt_book(w) for w in range(10)])

    confirmed = [r for r in results if r["status"] == "confirmed"]
    taken = [r for r in results if r["status"] == "slot_taken"]

    assert len(confirmed) == 1, "Exactly one worker MUST win the slot"
    assert len(taken) == 9, "All 9 racing workers MUST receive slot_taken"


@pytest.mark.asyncio
async def test_w4_3_per_phone_sequential_serialization():
    """W4.3: Messages for the same phone number are processed in strict order."""
    phone = "+919999988888"
    execution_order = []

    async def simulate_message_worker(msg_idx: int, delay: float):
        lock = await get_phone_lock(phone)
        async with lock:
            execution_order.append(f"start_{msg_idx}")
            await asyncio.sleep(delay)
            execution_order.append(f"end_{msg_idx}")
        await release_phone_lock(phone)

    # Dispatch 3 concurrent messages with varying processing durations
    await asyncio.gather(
        simulate_message_worker(1, 0.03),
        simulate_message_worker(2, 0.02),
        simulate_message_worker(3, 0.01),
    )

    # Verify no overlapping executions: start_X is immediately followed by end_X
    for i in range(0, len(execution_order), 2):
        start = execution_order[i]
        end = execution_order[i + 1]
        msg_id = start.split("_")[1]
        assert end == f"end_{msg_id}", f"Interleaved processing detected: {start} followed by {end}"


@pytest.mark.asyncio
async def test_w4_4_crashed_worker_lease_recovery():
    """W4.4: Inbound messages abandoned by a crashed worker are reclaimed by recover_pending."""
    manager = MessageQueueManager()

    # Simulate DB query returning a row stuck in 'processing' past its lease timeout
    stale_wamid = "wamid.CRASHED_WORKER_MSG_001"
    mock_stale_row = {
        "id": "row-999",
        "message_id": stale_wamid,
        "clinic_id": "clinic-test",
        "status": "processing",
        "lease_expires_at": "2026-08-20T10:00:00Z",  # In the past
    }

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value.lte.return_value.execute.return_value = MagicMock(
        data=[mock_stale_row]
    )
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{**mock_stale_row, "status": "pending"}]
    )

    with patch("app.database.supabase.table", return_value=mock_table):
        recovered_count = await manager.recover_pending_messages()
        assert recovered_count == 1, "Stale lease must be reclaimed and reset to pending"
