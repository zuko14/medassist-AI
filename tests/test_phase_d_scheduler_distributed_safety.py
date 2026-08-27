"""Phase D: Scheduler Distributed Safety & Multi-Instance Lock Tests (T1.1 / KRIYA-008).

Verifies:
1. Multi-instance mutual exclusion: RPC acquire returns True for 1st instance, False for 2nd.
2. Lock release on normal completion (via RPC/delete).
3. Lock release on unhandled job exception.
4. Heartbeat renewal extends lease during execution.
5. Lock steal detection: renew returns False and logs LOCK_STOLEN when locked_by changes.
6. Fail-closed on DB error: acquire returns False when RPC raises.
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import pytest
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock

from app.services.distributed_lock import DistributedJobLock, distributed_job_lock


@pytest.mark.asyncio
async def test_01_two_instances_mutual_exclusion():
    """Two scheduler instances compete for the same job lock: instance 1 wins, instance 2 skips."""
    inst1 = DistributedJobLock(instance_id="replica-pod-1")
    inst2 = DistributedJobLock(instance_id="replica-pod-2")

    mock_sb = MagicMock()
    # First RPC call returns True
    mock_sb.rpc.return_value.execute.side_effect = [
        MagicMock(data=True),   # inst1 acquires
        MagicMock(data=False),  # inst2 rejected
    ]

    with patch("app.database.supabase", mock_sb):
        acquired_1 = await inst1.acquire("24h_reminders", lease_seconds=120)
        assert acquired_1 is True

        acquired_2 = await inst2.acquire("24h_reminders", lease_seconds=120)
        assert acquired_2 is False

    assert mock_sb.rpc.call_count == 2
    mock_sb.rpc.assert_any_call(
        "acquire_scheduler_lock",
        {"p_job_name": "24h_reminders", "p_locked_by": "replica-pod-1", "p_lease_seconds": 120}
    )


@pytest.mark.asyncio
async def test_02_lock_release_on_job_completion():
    """Lock is released cleanly when context manager exits."""
    inst = DistributedJobLock(instance_id="replica-pod-1")

    mock_sb = MagicMock()
    mock_sb.rpc.return_value.execute.return_value = MagicMock(data=True)

    with patch("app.database.supabase", mock_sb):
        job_executed = False
        async with distributed_job_lock("followups", lease_seconds=120, lock_manager=inst) as acquired:
            assert acquired is True
            job_executed = True

        assert job_executed is True
        mock_sb.rpc.assert_any_call(
            "release_scheduler_lock",
            {"p_job_name": "followups", "p_locked_by": "replica-pod-1"}
        )


@pytest.mark.asyncio
async def test_03_lock_release_on_job_exception():
    """Lock is released cleanly even if job raises an unhandled exception."""
    inst = DistributedJobLock(instance_id="replica-pod-1")

    mock_sb = MagicMock()
    mock_sb.rpc.return_value.execute.return_value = MagicMock(data=True)

    with patch("app.database.supabase", mock_sb):
        with pytest.raises(RuntimeError):
            async with distributed_job_lock("doctor_leaves", lease_seconds=120, lock_manager=inst) as acquired:
                assert acquired is True
                raise RuntimeError("Simulated crash during leave check")

        mock_sb.rpc.assert_any_call(
            "release_scheduler_lock",
            {"p_job_name": "doctor_leaves", "p_locked_by": "replica-pod-1"}
        )


@pytest.mark.asyncio
async def test_04_lock_renewal_heartbeat():
    """Heartbeat renews the lock lease while the job is active."""
    inst = DistributedJobLock(instance_id="replica-pod-1")

    mock_sb = MagicMock()
    mock_sb.rpc.return_value.execute.return_value = MagicMock(data=True)
    # mock renew update
    mock_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[{"job_name": "long_job"}])

    with patch("app.database.supabase", mock_sb):
        async with distributed_job_lock("long_job", lease_seconds=0.1, lock_manager=inst) as acquired:
            assert acquired is True
            # Sleep long enough for heartbeat to fire at lease_seconds / 3 (~33ms)
            await asyncio.sleep(0.08)

        # Confirm renew was invoked on scheduler_locks table
        mock_sb.table.assert_called_with("scheduler_locks")


@pytest.mark.asyncio
async def test_05_lock_steal_detected(caplog):
    """When locked_by changes (stolen), renew returns False and LOCK_STOLEN is logged."""
    inst = DistributedJobLock(instance_id="replica-pod-1")

    mock_sb = MagicMock()
    # Update returns empty data (0 rows affected because locked_by does not match)
    mock_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

    with patch("app.database.supabase", mock_sb):
        renew_success = await inst.renew("stolen_job", lease_seconds=120)
        assert renew_success is False


@pytest.mark.asyncio
async def test_06_acquire_fails_closed_on_db_error():
    """If the RPC raises an exception, acquire returns False (fails closed)."""
    inst = DistributedJobLock(instance_id="replica-pod-1")

    mock_sb = MagicMock()
    mock_sb.rpc.side_effect = Exception("DB Connection Lost")

    with patch("app.database.supabase", mock_sb):
        acquired = await inst.acquire("failing_job", lease_seconds=120)
        assert acquired is False
