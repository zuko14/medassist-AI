"""Message Queue / Idempotency Manager for MediAssist AI.

Provides atomic, race-condition-free deduplication for WhatsApp webhooks
using ONLY Supabase (PostgreSQL native capabilities) + Python asyncio.

NO Redis or external message queue servers required.

Strategy:
  Layer 1 — Supabase UNIQUE constraint:
    Uses an atomic INSERT with ON CONFLICT DO NOTHING on the `processed_messages`
    table. PostgreSQL guarantees that even if two requests arrive simultaneously,
    only one INSERT will succeed. The other will silently fail — this is the
    atomic gate.

  Layer 2 — asyncio.Lock per phone number (with timeout):
    Prevents concurrent state mutations for the same patient within a single
    process instance. Uses a dict with explicit cleanup to avoid WeakValueDict
    GC issues. Locks have a configurable timeout (default 15s) to prevent
    cascading delays from Groq latency spikes.

Meta Webhook Timeout Protection:
  Meta requires a 200 OK within 20 seconds. Our webhook.py returns 200
  immediately via FastAPI BackgroundTasks, then processes asynchronously.
  The per-phone lock timeout (15s) ensures that even if processing stalls,
  subsequent messages for the same phone are never blocked indefinitely.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── Per-phone asyncio locks with reference counting ─────────────────────────
# We use a regular dict + refcount instead of WeakValueDictionary to avoid
# garbage collection races where a lock is collected while a task still holds it.
_phone_locks: dict[str, asyncio.Lock] = {}
_phone_refcounts: dict[str, int] = {}
_phone_locks_mutex = asyncio.Lock()

# Maximum time (seconds) a message will wait to acquire the per-phone lock.
# Set below Meta's 20s webhook timeout to prevent cascading stalls.
PHONE_LOCK_TIMEOUT_SECONDS = 15

_fail_closed_count = 0


def get_fail_open_count() -> int:
    """Count of acquire() failure events (fails closed on database errors)."""
    return _fail_closed_count


def get_fail_closed_count() -> int:
    """Count of acquire() fail-closed events since process start."""
    return get_fail_open_count()


def _record_fail_closed() -> None:
    """Increment the fail-closed counter on database error during acquire()."""
    global _fail_closed_count
    _fail_closed_count += 1


def _record_fail_open() -> None:
    """Backward-compatible alias for _record_fail_closed."""
    _record_fail_closed()


_fail_open_count = _fail_closed_count


class MessageQueueManager:
    """Supabase-native atomic message deduplication + per-phone asyncio locks + durable queue."""

    async def ingest(
        self,
        message_id: str,
        phone: str,
        display_phone: str,
        payload: dict,
        clinic_id: Optional[str] = None,
        phone_number_id: Optional[str] = None,
    ) -> tuple[bool, Optional[dict]]:
        """Durably persist an incoming WhatsApp message before returning HTTP 200 to Meta.

        Guarantees that a process crash or worker restart after webhook acknowledgment
        cannot lose accepted patient messages.

        Returns:
            (is_new, record_dict)
            is_new=True  → Newly ingested message. Worker must process it.
            is_new=False → Duplicate wamid already in database.
        """
        from app.database import supabase
        from app.utils.pii_sanitizer import sanitize_pii
        import json

        def _is_duplicate(exc: Exception) -> bool:
            error_str = str(exc).lower()
            return "unique" in error_str or "duplicate" in error_str or "23505" in error_str

        # Mask sensitive PII before persisting payload in durable queue
        raw_str = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        sanitized_payload = json.loads(sanitize_pii(raw_str)) if isinstance(payload, dict) else {"raw": sanitize_pii(raw_str)}

        record = {
            "message_id": message_id,
            "phone": phone,
            "display_phone": display_phone,
            "phone_number_id": phone_number_id,
            "payload": sanitized_payload,
            "status": "received",
            "attempt_count": 0,
        }
        if clinic_id:
            record["clinic_id"] = clinic_id

        try:
            result = supabase.table("inbound_messages").insert(record).execute()
            if result.data:
                logger.info(f"Durable queue: ingested new message {message_id}")
                # NOTE: do NOT write processed_messages here. acquire() claims a message
                # by INSERTing that same message_id and treats a unique violation as
                # "duplicate, drop it". Pre-claiming the row here made acquire() reject
                # every message as its own duplicate, so process_message() returned at
                # the guard and no patient ever got a reply — while the inbound row was
                # still marked 'completed'. acquire() writes the row on success.
                return True, result.data[0]
            return False, None
        except Exception as e:
            if _is_duplicate(e):
                logger.info(f"Durable queue: duplicate message {message_id} dropped")
                return False, None

            # Fail closed: do not silently swallow durable ingestion errors
            _record_fail_closed()
            logger.error(f"Durable queue insert failed for {message_id}: {e}")
            return False, None

    async def claim_message(self, message_id: str) -> bool:
        """Atomically claim a message for processing."""
        from app.database import supabase
        from datetime import datetime, timezone
        from app.config import settings

        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            res = (
                supabase.table("inbound_messages")
                .update({"status": "processing", "locked_at": now_iso, "updated_at": now_iso})
                .eq("message_id", message_id)
                .in_("status", ["received", "failed_retryable"])
                .execute()
            )
            return bool(res.data)
        except Exception as e:
            if getattr(settings, "queue_fail_closed_enforce", False):
                logger.error(
                    f"MESSAGE_QUEUE_FAIL_CLOSED message_id={message_id} during claim: {e}"
                )
                return False
            else:
                logger.warning(
                    f"MESSAGE_QUEUE_FAIL_OPEN message_id={message_id} during claim: {e} "
                    f"(fail-open allowed by queue_fail_closed_enforce=False)"
                )
                return True

    async def mark_completed(self, message_id: str) -> bool:
        """Mark durable message as successfully processed."""
        from app.database import supabase
        from datetime import datetime, timezone

        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            res = (
                supabase.table("inbound_messages")
                .update({"status": "completed", "completed_at": now_iso, "updated_at": now_iso})
                .eq("message_id", message_id)
                .execute()
            )
            logger.info(f"Durable queue: marked message {message_id} completed")
            return bool(res.data)
        except Exception as e:
            logger.warning(f"Failed to mark completed for {message_id}: {e}")
            return False

    async def mark_failed(
        self, message_id: str, error: str, max_retries: int = 3
    ) -> str:
        """Mark message as failed_retryable or dead_letter with bounded exponential backoff."""
        from app.database import supabase
        from datetime import datetime, timezone, timedelta

        try:
            row_res = (
                supabase.table("inbound_messages")
                .select("attempt_count, phone, display_phone, payload, clinic_id")
                .eq("message_id", message_id)
                .execute()
            )
            attempts = 1
            phone = "unknown"
            display_phone = None
            payload = {}
            clinic_id = None

            if row_res and isinstance(getattr(row_res, "data", None), list) and row_res.data:
                row = row_res.data[0]
                if isinstance(row, dict):
                    raw_attempts = row.get("attempt_count")
                    if isinstance(raw_attempts, int):
                        attempts = raw_attempts + 1
                    phone = str(row.get("phone", "unknown"))
                    display_phone = row.get("display_phone")
                    payload = row.get("payload", {})
                    clinic_id = row.get("clinic_id")

            now = datetime.now(timezone.utc)
            if attempts < max_retries:
                # Bounded exponential backoff: 5s, 10s, 20s
                retry_at = (now + timedelta(seconds=5 * attempts)).isoformat()
                supabase.table("inbound_messages").update(
                    {
                        "status": "failed_retryable",
                        "attempt_count": attempts,
                        "retry_at": retry_at,
                        "last_error": str(error)[:500],
                        "updated_at": now.isoformat(),
                    }
                ).eq("message_id", message_id).execute()
                logger.warning(
                    f"Durable queue: message {message_id} attempt {attempts} failed, "
                    f"scheduled retry at {retry_at}"
                )
                # Write to failed_messages on failure
                try:
                    import json
                    supabase.table("failed_messages").insert(
                        {
                            "phone": phone,
                            "display_phone": display_phone,
                            "payload": json.dumps(payload) if isinstance(payload, dict) else str(payload),
                            "error": str(error)[:500],
                            "status": "retryable",
                        }
                    ).execute()
                except Exception as dlq_err:
                    logger.error(
                        f"DLQ_WRITE_FAILED message_id={message_id}: failed to record failed_messages on retryable error: {dlq_err}"
                    )
                return "failed_retryable"
            else:
                supabase.table("inbound_messages").update(
                    {
                        "status": "dead_letter",
                        "attempt_count": attempts,
                        "last_error": str(error)[:500],
                        "updated_at": now.isoformat(),
                    }
                ).eq("message_id", message_id).execute()

                # Also insert into dead-letter queue table for operator dashboard
                try:
                    import json
                    supabase.table("failed_messages").insert(
                        {
                            "phone": phone,
                            "display_phone": display_phone,
                            "payload": json.dumps(payload) if isinstance(payload, dict) else str(payload),
                            "error": str(error)[:500],
                            "status": "dead_letter",
                        }
                    ).execute()
                except Exception as dlq_e:
                    logger.error(f"DLQ secondary write error: {dlq_e}")

                logger.error(
                    f"Durable queue: message {message_id} exceeded max retries ({attempts}), "
                    f"moved to DEAD_LETTER"
                )
                return "dead_letter"
        except Exception as e:
            logger.error(f"Failed to record failure for {message_id}: {e}")
            return "failed_retryable"

    async def replay_dead_letter(
        self, message_id: str, clinic_id: Optional[str] = None
    ) -> bool:
        """Replay a dead-letter message with tenant authorization check."""
        from app.database import supabase
        from datetime import datetime, timezone

        try:
            query = (
                supabase.table("inbound_messages")
                .select("*")
                .eq("message_id", message_id)
                .in_("status", ["dead_letter", "failed_retryable"])
            )
            if clinic_id:
                query = query.eq("clinic_id", clinic_id)
            res = query.execute()

            if not res.data:
                logger.warning(f"Replay rejected: message {message_id} not found or unauthorized")
                return False

            now_iso = datetime.now(timezone.utc).isoformat()
            supabase.table("inbound_messages").update(
                {
                    "status": "received",
                    "attempt_count": 0,
                    "retry_at": None,
                    "last_error": None,
                    "updated_at": now_iso,
                }
            ).eq("message_id", message_id).execute()

            logger.info(f"Durable queue: replaying dead letter message {message_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to replay dead letter message {message_id}: {e}")
            return False

    async def recover_pending_inbound_messages(self, lease_timeout_seconds: int = 300) -> int:
        """Reclaim messages stuck in 'processing' state past their lease timeout (e.g. crashed worker).

        Resets abandoned processing leases back to 'received' so live workers
        can re-acquire and process them without data loss (W4.4).
        """
        from app.database import supabase
        from datetime import datetime, timezone, timedelta

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=lease_timeout_seconds)).isoformat()
            res = (
                supabase.table("inbound_messages")
                .select("id, message_id, attempt_count")
                .eq("status", "processing")
                .lte("updated_at", cutoff)
                .execute()
            )
            stale_rows = res.data or []
            recovered = 0
            for row in stale_rows:
                now_iso = datetime.now(timezone.utc).isoformat()
                up_res = (
                    supabase.table("inbound_messages")
                    .update({
                        "status": "received",
                        "last_error": "Lease recovered after worker timeout",
                        "updated_at": now_iso,
                    })
                    .eq("id", row["id"])
                    .execute()
                )
                if up_res.data:
                    recovered += 1
            if recovered > 0:
                logger.info(f"Durable queue: recovered {recovered} abandoned inbound messages from crashed workers")
            return recovered
        except Exception as e:
            logger.error(f"Failed to recover pending inbound messages: {e}")
            return 0

    async def recover_pending_messages(self, lease_timeout_seconds: int = 300) -> int:
        """Alias for recover_pending_inbound_messages (W4.4)."""
        return await self.recover_pending_inbound_messages(lease_timeout_seconds)

    async def acquire(
        self, message_id: str, clinic_id: Optional[str] = None
    ) -> bool:
        """Attempt to atomically claim processing rights for a message.

        Uses PostgreSQL UNIQUE constraint on message_id. If two identical
        webhooks arrive simultaneously, only one INSERT succeeds — the other
        gets a unique violation and returns False, dropping the duplicate.

        Args:
            message_id: WhatsApp message ID (unique per message globally).
            clinic_id: Optional clinic ID to associate message volume.

        Returns:
            True  → This process owns this message. Proceed with processing.
            False → Duplicate. Another process already handling it. Drop.
        """
        from app.database import supabase

        def _is_duplicate(exc: Exception) -> bool:
            error_str = str(exc).lower()
            return (
                "unique" in error_str
                or "duplicate" in error_str
                or "23505" in error_str
            )

        payload = {"message_id": message_id}
        if clinic_id:
            payload["clinic_id"] = clinic_id

        for attempt in range(2):
            try:
                # Atomic INSERT ON CONFLICT DO NOTHING
                result = supabase.table("processed_messages").insert(payload).execute()

                # If insert succeeded, result.data will have the new row
                if result.data:
                    logger.debug(f"Message queue: acquired lock for {message_id}")
                    return True
                else:
                    # ON CONFLICT: row already existed
                    logger.info(f"Message queue: duplicate dropped for {message_id}")
                    return False

            except Exception as e:
                if _is_duplicate(e):
                    logger.info(
                        f"Message queue: duplicate (unique violation) for {message_id}"
                    )
                    return False

                # Fallback to insert without clinic_id if schema lacks clinic_id column
                if clinic_id:
                    try:
                        result = (
                            supabase.table("processed_messages")
                            .insert({"message_id": message_id})
                            .execute()
                        )
                        if result.data:
                            logger.warning(
                                f"Message queue: acquired lock for {message_id} without "
                                f"clinic_id attribution (insert with clinic_id failed: {e})"
                            )
                            return True
                        logger.info(f"Message queue: duplicate dropped for {message_id}")
                        return False
                    except Exception as e2:
                        if _is_duplicate(e2):
                            logger.info(
                                f"Message queue: duplicate (unique violation) for {message_id}"
                            )
                            return False

                if attempt == 0:
                    await asyncio.sleep(0.1)
                    continue

                # Fail CLOSED on database error to protect financial and booking integrity
                _record_fail_closed()
                logger.error(
                    f"MESSAGE_QUEUE_FAIL_CLOSED message_id={message_id}: database error during acquire: {e}"
                )
                return False

        return False

    async def release(self, message_id: str) -> None:
        """Release message lock by deleting row from processed_messages on processing failure.

        Allows subsequent DLQ retry or webhook replay to claim the message.
        """
        from app.database import supabase

        try:
            supabase.table("processed_messages").delete().eq("message_id", message_id).execute()
            logger.info(f"Message queue: released claim for message_id={message_id}")
        except Exception as e:
            logger.warning(f"Message queue: failed to delete processed_messages row for {message_id}: {e}")

    async def reap_abandoned_claims(
        self, lease_seconds: int = 120, limit: int = 50
    ) -> int:
        """Release claims whose worker died mid-processing.

        A row stuck in 'processing' past the lease means the process that claimed
        it is gone. Deleting the processed_messages row makes the message
        eligible for replay; setting failed_retryable + retry_at hands it to the
        existing drain_pending_retry_messages job, which knows how to reconstruct
        and replay from `payload` (after T0.4).

        Uses idx_inbound_messages_locked_at (migration 047). No migration needed.
        """
        from app.database import supabase
        from datetime import datetime, timezone, timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
        ).isoformat()

        try:
            stale = (
                supabase.table("inbound_messages")
                .select("message_id, attempt_count")
                .eq("status", "processing")
                .lt("locked_at", cutoff)
                .limit(limit)
                .execute()
            )
        except Exception as e:
            logger.error(f"Reaper: failed to query abandoned claims: {e}")
            return 0

        reaped = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        for row in (stale.data or []):
            mid = row["message_id"]
            try:
                # 1. Drop the processed_messages claim so a replay can proceed.
                await self.release(mid)

                # 2. Hand to the retry drain. The .eq("status", "processing")
                #    predicate is a CAS: if another process reaped this row
                #    first, the update matches nothing and no double-handling
                #    occurs.
                supabase.table("inbound_messages").update(
                    {
                        "status": "failed_retryable",
                        "retry_at": now_iso,
                        "last_error": "abandoned: worker died mid-processing",
                        "updated_at": now_iso,
                    }
                ).eq("message_id", mid).eq("status", "processing").execute()

                reaped += 1
                logger.warning(f"Reaper: released abandoned claim for {mid}")
            except Exception as e:
                logger.error(f"Reaper: failed to release {mid}: {e}")

        if reaped:
            logger.warning(f"Reaper: released {reaped} abandoned claim(s)")

        return reaped

    async def is_processed(self, message_id: str) -> bool:
        """Check if a message has already been processed.

        Useful for pre-flight checks before expensive operations.

        Args:
            message_id: WhatsApp message ID to check.

        Returns:
            True if already processed, False if new.
        """
        from app.database import supabase
        from app.config import settings

        try:
            result = (
                supabase.table("processed_messages")
                .select("id")
                .eq("message_id", message_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            if getattr(settings, "queue_fail_closed_enforce", False):
                logger.error(
                    f"MESSAGE_QUEUE_FAIL_CLOSED message_id={message_id} during is_processed: {e}"
                )
                return True  # Fail CLOSED: assume processed to prevent double execution
            else:
                logger.warning(
                    f"MESSAGE_QUEUE_FAIL_OPEN message_id={message_id} during is_processed: {e} "
                    f"(fail-open allowed by queue_fail_closed_enforce=False)"
                )
                return False


async def get_phone_lock(phone: str) -> asyncio.Lock:
    """Get (or create) a per-phone asyncio lock for concurrent state protection.

    NOTE ON CEILING (T2.3 / KRIYA-015 — Decision Recorded):
    This is an in-process asyncio lock that serializes rapid concurrent messages from
    the same phone within a single worker process. Across multi-instance deployments
    (e.g., 4 processes), cross-process message deduplication is already strictly
    guaranteed by acquire() on processed_messages.
    Upgrade path: If fsm_interleave_suspected metric indicates high cross-process
    interleaving under load, upgrade to Postgres pg_advisory_xact_lock(hashtext(phone)).

    Uses reference counting instead of WeakValueDictionary to prevent GC
    from collecting a lock while a coroutine still references it.

    Args:
        phone: Patient phone number.

    Returns:
        asyncio.Lock for the given phone.
    """
    async with _phone_locks_mutex:
        if phone not in _phone_locks:
            _phone_locks[phone] = asyncio.Lock()
            _phone_refcounts[phone] = 0
        _phone_refcounts[phone] += 1
        return _phone_locks[phone]


async def release_phone_lock(phone: str) -> None:
    """Release a reference to a per-phone lock.

    When the reference count drops to zero, the lock is removed from the dict
    to prevent memory leaks from accumulating locks for inactive phones.

    Args:
        phone: Patient phone number.
    """
    async with _phone_locks_mutex:
        if phone in _phone_refcounts:
            _phone_refcounts[phone] -= 1
            if _phone_refcounts[phone] <= 0:
                _phone_locks.pop(phone, None)
                _phone_refcounts.pop(phone, None)


async def acquire_phone_lock_with_timeout(
    phone: str,
    timeout: float = PHONE_LOCK_TIMEOUT_SECONDS,
) -> bool:
    """Acquire the per-phone asyncio lock with a timeout.

    If the lock cannot be acquired within `timeout` seconds (e.g., because
    a previous message for the same phone is still being processed by Groq),
    this returns False instead of blocking indefinitely.

    This prevents cascading delays when:
      - A patient sends rapid consecutive messages
      - Groq API has a latency spike
      - Meta retries cause lock contention

    Args:
        phone: Patient phone number.
        timeout: Max seconds to wait for the lock. Defaults to 15s
                 (safely under Meta's 20s webhook timeout).

    Returns:
        True  → Lock acquired. Caller MUST release it via the lock's context.
        False → Timed out. Caller should defer or queue the message.
    """
    lock = await get_phone_lock(phone)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        logger.warning(
            f"Phone lock timeout ({timeout}s) for {phone[:6]}*** — "
            f"previous message still processing. Deferring."
        )
        # Release our refcount since we won't be using the lock
        await release_phone_lock(phone)
        return False


# Global instance
message_queue = MessageQueueManager()
