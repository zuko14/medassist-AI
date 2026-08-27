"""Distributed Locking for multi-instance scheduler and worker clusters."""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

# Unique ID per running process instance
INSTANCE_ID = f"inst_{os.getpid()}_{uuid.uuid4().hex[:8]}"


class DistributedJobLock:
    """PostgreSQL-backed distributed lock manager for multi-instance scheduling."""

    def __init__(self, instance_id: str = INSTANCE_ID):
        self.instance_id = instance_id
        self.worker_id = instance_id

    async def acquire(self, job_name: str, lease_seconds: int = 300) -> bool:
        """Atomic acquire via the RPC defined in migration 048.

        The previous Python read-modify-write compared expiry as an ISO STRING
        and had no lease renewal, so a job outliving its lease could be taken
        over and run concurrently across the 4 production processes (KRIYA-008).
        """
        from app.database import supabase

        try:
            res = supabase.rpc(
                "acquire_scheduler_lock",
                {
                    "p_job_name": job_name,
                    "p_locked_by": self.worker_id,
                    "p_lease_seconds": lease_seconds,
                },
            ).execute()
            return bool(res.data)
        except Exception as e:
            # Fail CLOSED: not acquiring means the job is skipped this tick,
            # which is safe. Acquiring on error would allow concurrent runs.
            logger.error(f"Lock acquire failed for {job_name}: {e}")
            return False

    async def renew(self, job_name: str, lease_seconds: int = 300) -> bool:
        """Extend the lease. Returns False if the lock was stolen.

        The locked_by predicate is what makes theft detectable.
        """
        from app.database import supabase

        new_expiry = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat()
        try:
            res = (
                supabase.table("scheduler_locks")
                .update({"expires_at": new_expiry})
                .eq("job_name", job_name)
                .eq("locked_by", self.worker_id)
                .execute()
            )
            return bool(res.data)
        except Exception as e:
            logger.error(f"Lock renew failed for {job_name}: {e}")
            return False

    async def release(self, job_name: str) -> bool:
        """Release distributed lock if owned by this instance."""
        try:
            from app.database import supabase
            try:
                supabase.rpc(
                    "release_scheduler_lock",
                    {
                        "p_job_name": job_name,
                        "p_locked_by": self.worker_id,
                    },
                ).execute()
            except Exception:
                # Fallback to direct delete if RPC fails
                supabase.table("scheduler_locks").delete().eq("job_name", job_name).eq(
                    "locked_by", self.worker_id
                ).execute()
            logger.debug(f"Released distributed lock for '{job_name}'")
            return True
        except Exception as e:
            logger.warning(f"Error releasing distributed lock for '{job_name}': {e}")
            return False


distributed_lock_manager = DistributedJobLock()


@asynccontextmanager
async def distributed_job_lock(
    job_name: str,
    lease_seconds: int = 300,
    lock_manager: DistributedJobLock = distributed_lock_manager,
) -> AsyncGenerator[bool, None]:
    """Async context manager for safely executing a distributed singleton job with heartbeat lease renewal."""
    acquired = await lock_manager.acquire(job_name, lease_seconds=lease_seconds)
    if not acquired:
        logger.info(f"Distributed job '{job_name}' skipped: lock currently held by another instance")
        yield False
        return

    stop = asyncio.Event()

    async def _heartbeat():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=lease_seconds / 3)
                return
            except asyncio.TimeoutError:
                if not await lock_manager.renew(job_name, lease_seconds):
                    logger.error(
                        f"LOCK_STOLEN job={job_name} — another process took the "
                        f"lease; aborting job body"
                    )
                    stop.set()
                    return

    hb = asyncio.create_task(_heartbeat())
    try:
        yield True
    finally:
        stop.set()
        await hb
        await lock_manager.release(job_name)
