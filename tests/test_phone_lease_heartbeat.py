"""The cross-process phone lease must outlive a slow handler.

Before this, acquire_phone_lock_with_timeout() called
distributed_lock_manager.acquire(lease_seconds=20) directly, bypassing the
heartbeat that distributed_job_lock() provides. A handler slower than 20s
(the OpenRouter retry budget alone is ~12s) let the lease lapse, and a second
message from the same patient on another worker acquired it and ran
concurrently — the FSM interleaving the lock exists to prevent.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services import message_queue as mq


@pytest.fixture
def fast_lease(monkeypatch):
    """Shrink the lease so the heartbeat's lease/3 interval is testable."""
    monkeypatch.setattr(mq, "PHONE_LEASE_SECONDS", 0.3)


@pytest.fixture
def lock_manager():
    manager = AsyncMock()
    manager.acquire.return_value = True
    manager.renew.return_value = True
    manager.release.return_value = True
    with patch("app.services.distributed_lock.distributed_lock_manager", manager):
        yield manager


@pytest.mark.asyncio
async def test_lease_is_renewed_while_the_handler_still_holds_it(fast_lease, lock_manager):
    phone = "+919000000001"
    assert await mq.acquire_phone_lock_with_timeout(phone, timeout=1) is True
    try:
        # Simulate a handler that outlives the raw 0.3s lease.
        await asyncio.sleep(0.75)
        assert lock_manager.renew.await_count >= 2, (
            "lease was never renewed — it would have lapsed mid-handler and "
            "another process could take this patient's phone"
        )
        renewed_name = lock_manager.renew.await_args[0][0]
        assert renewed_name == mq.phone_lock_name(phone)
    finally:
        await mq.release_phone_lock_acquired(phone)


@pytest.mark.asyncio
async def test_release_stops_the_heartbeat_so_it_cannot_resurrect_the_lease(
    fast_lease, lock_manager
):
    phone = "+919000000002"
    assert await mq.acquire_phone_lock_with_timeout(phone, timeout=1) is True
    await mq.release_phone_lock_acquired(phone)

    assert mq.phone_lock_name(phone) not in mq._phone_lease_renewals
    after_release = lock_manager.renew.await_count
    await asyncio.sleep(0.5)
    assert lock_manager.renew.await_count == after_release, (
        "heartbeat kept renewing after release — the lease would be resurrected "
        "and lock this patient out for a full window"
    )


@pytest.mark.asyncio
async def test_no_heartbeat_is_left_running_when_the_local_lock_times_out(
    fast_lease, lock_manager
):
    phone = "+919000000003"
    assert await mq.acquire_phone_lock_with_timeout(phone, timeout=1) is True
    try:
        # Second waiter on the same phone in this process cannot get the local lock.
        assert await mq.acquire_phone_lock_with_timeout(phone, timeout=0.2) is False
        # The loser must not have registered a competing renewal task.
        assert len(mq._phone_lease_renewals) == 1
    finally:
        await mq.release_phone_lock_acquired(phone)
        assert mq._phone_lease_renewals == {}
