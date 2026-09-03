"""Distributed Locking for multi-instance scheduler and worker clusters."""

import asyncio
import contextlib
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator
from app.database import sb  # T5.1: off-loop query execution

logger = logging.getLogger(__name__)

# Unique ID per running process instance
INSTANCE_ID = f"inst_{os.getpid()}_{uuid.uuid4().hex[:8]}"


class LockStolenError(RuntimeError):
    """Raised into a job body whose distributed lease was lost mid-flight.

    Distinct from asyncio.CancelledError so that a lease loss is not confused
    with interpreter shutdown in logs or in APScheduler's error handling.
    """


class DistributedJobLock:
    """PostgreSQL-backed distributed lock manager for multi-instance scheduling."""

    def __init__(self, instance_id: str = INSTANCE_ID):
        self.instance_id = instance_id
        self.worker_id = instance_id

    async def acquire(
        self,
        job_name: str,
        lease_seconds: int = 300,
        raise_on_error: bool = False,
    ) -> bool:
        """Atomic acquire via the RPC defined in migration 048.

        The previous Python read-modify-write compared expiry as an ISO STRING
        and had no lease renewal, so a job outliving its lease could be taken
        over and run concurrently across the 4 production processes (KRIYA-008).

        `raise_on_error` separates "someone else holds the lease" (False) from
        "we could not reach the database to find out" (raises). Collapsing both
        into False is right for scheduler jobs, which must fail CLOSED, but it
        silently disabled the fail-OPEN path the per-phone lock documents: a
        Supabase blip made every inbound patient message look like a genuine
        conflict and sent it to the dead-letter queue instead of answering it.
        Callers that can safely proceed without the lease pass True and handle
        the exception themselves.
        """
        from app.database import supabase

        try:
            res = await sb(supabase.rpc(
                "acquire_scheduler_lock",
                {
                    "p_job_name": job_name,
                    "p_locked_by": self.worker_id,
                    "p_lease_seconds": lease_seconds,
                },
            ))
            return bool(res.data)
        except Exception as e:
            # Fail CLOSED by default: not acquiring means the job is skipped
            # this tick, which is safe. Acquiring on error would allow
            # concurrent runs.
            logger.error(f"Lock acquire failed for {job_name}: {e}")
            if raise_on_error:
                raise
            return False

    async def renew(self, job_name: str, lease_seconds: int = 300) -> bool:
        """Extend the lease. Returns False only when the row is no longer ours.

        The locked_by predicate is what makes theft detectable: an UPDATE that
        matches zero rows means another instance owns the lease.

        A transport or database failure RAISES rather than returning False.
        That distinction is load-bearing now that the heartbeat cancels the
        running job on a False: a dropped packet is not evidence of theft, and
        swallowing it here would abort healthy jobs on the first timeout.
        """
        from app.database import supabase

        new_expiry = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat()
        res = (
            await sb(supabase.table("scheduler_locks")
            .update({"expires_at": new_expiry})
            .eq("job_name", job_name)
            .eq("locked_by", self.worker_id))
        )
        return bool(res.data)

    async def release(self, job_name: str) -> bool:
        """Release distributed lock if owned by this instance."""
        try:
            from app.database import supabase
            try:
                await sb(supabase.rpc(
                    "release_scheduler_lock",
                    {
                        "p_job_name": job_name,
                        "p_locked_by": self.worker_id,
                    },
                ))
            except Exception:
                # Fallback to direct delete if RPC fails
                await sb(supabase.table("scheduler_locks").delete().eq("job_name", job_name).eq(
                    "locked_by", self.worker_id
                ))
            logger.debug(f"Released distributed lock for '{job_name}'")
            return True
        except Exception as e:
            logger.warning(f"Error releasing distributed lock for '{job_name}': {e}")
            return False


distributed_lock_manager = DistributedJobLock()


async def _release_after(hb: asyncio.Task, lock_manager: "DistributedJobLock", job_name: str) -> None:
    """Drain the heartbeat task, then release the lease. Never raises.

    Runs as its own task so that a cancellation aimed at the job body cannot
    skip the release and strand the lock until its lease expires.
    """
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await hb
    with contextlib.suppress(Exception):
        await lock_manager.release(job_name)


@asynccontextmanager
async def distributed_job_lock(
    job_name: str,
    lease_seconds: int = 300,
    lock_manager: DistributedJobLock = distributed_lock_manager,
) -> AsyncGenerator[bool, None]:
    """Run a job body as a distributed singleton, with a heartbeat-renewed lease.

    On lease loss the body is CANCELLED, not merely flagged. Setting an event
    the body never reads left the losing instance running the job to completion
    alongside the instance that took the lease — the exact double-execution the
    lock exists to prevent (AUDIT-P0-2). The body sees LockStolenError.
    """
    acquired = await lock_manager.acquire(job_name, lease_seconds=lease_seconds)
    if not acquired:
        logger.info(f"Distributed job '{job_name}' skipped: lock currently held by another instance")
        yield False
        return

    owner = asyncio.current_task()
    stop = asyncio.Event()
    stolen = False
    # Wall-clock instant at which our lease lapses if no renewal lands. Renewal
    # failures are tolerated until this passes, because until it does no other
    # instance can legitimately acquire the lock.
    lease_deadline = time.monotonic() + lease_seconds

    async def _heartbeat() -> None:
        nonlocal stolen, lease_deadline
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=lease_seconds / 3)
                return
            except asyncio.TimeoutError:
                pass

            try:
                renewed = await lock_manager.renew(job_name, lease_seconds)
            except Exception as e:
                if time.monotonic() < lease_deadline:
                    logger.warning(
                        f"LOCK_RENEW_TRANSIENT job={job_name} — renewal failed but "
                        f"the lease has not lapsed yet, job continues: {e}"
                    )
                    continue
                logger.error(
                    f"LOCK_LEASE_EXPIRED job={job_name} — lease lapsed while renewals "
                    f"kept failing ({e}); aborting job body"
                )
            else:
                if renewed:
                    lease_deadline = time.monotonic() + lease_seconds
                    continue
                logger.error(
                    f"LOCK_STOLEN job={job_name} — another process took the lease; "
                    f"aborting job body"
                )

            stolen = True
            if owner is not None:
                owner.cancel()
            return

    hb = asyncio.create_task(_heartbeat())
    try:
        yield True
    except asyncio.CancelledError:
        if not stolen:
            raise
        # Convert our own abort into a named error so callers and APScheduler
        # can tell a lost lease from a shutdown.
        if owner is not None and hasattr(owner, "uncancel"):
            owner.uncancel()
        raise LockStolenError(
            f"Distributed lease for '{job_name}' was lost mid-flight; job aborted"
        ) from None
    finally:
        stop.set()
        hb.cancel()
        cleanup = asyncio.ensure_future(_release_after(hb, lock_manager, job_name))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Either a shutdown, or an abort we requested that arrived only now
            # because the body had no further await points to deliver it at.
            # The shielded cleanup still runs to completion either way.
            if not stolen:
                raise
            logger.error(
                f"LOCK_STOLEN job={job_name} — body had already finished when the "
                f"lease loss was detected; it ran concurrently with the new owner"
            )
