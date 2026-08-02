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


class MessageQueueManager:
    """Supabase-native atomic message deduplication + per-phone asyncio locks."""

    async def acquire(self, message_id: str) -> bool:
        """Attempt to atomically claim processing rights for a message.

        Uses PostgreSQL UNIQUE constraint on message_id. If two identical
        webhooks arrive simultaneously, only one INSERT succeeds — the other
        gets a unique violation and returns False, dropping the duplicate.

        Args:
            message_id: WhatsApp message ID (unique per message globally).

        Returns:
            True  → This process owns this message. Proceed with processing.
            False → Duplicate. Another process already handling it. Drop.
        """
        from app.database import supabase

        try:
            # Atomic INSERT ON CONFLICT DO NOTHING
            # If message_id already exists, Supabase returns empty data (no error)
            result = (
                supabase.table("processed_messages")
                .insert(
                    {"message_id": message_id},
                    # upsert=False ensures we DON'T update on conflict
                )
                .execute()
            )

            # If insert succeeded, result.data will have the new row
            if result.data:
                logger.debug(f"Message queue: acquired lock for {message_id}")
                return True
            else:
                # ON CONFLICT: row already existed
                logger.info(f"Message queue: duplicate dropped for {message_id}")
                return False

        except Exception as e:
            error_str = str(e).lower()
            # PostgreSQL unique violation codes
            if (
                "unique" in error_str
                or "duplicate" in error_str
                or "23505" in error_str
            ):
                logger.info(
                    f"Message queue: duplicate (unique violation) for {message_id}"
                )
                return False
            # On any other error, fail open (allow processing) to avoid dropping real messages
            logger.warning(f"Message queue: acquire error (failing open): {e}")
            return True

    async def release(self, message_id: str) -> None:
        """Mark message processing as complete (no-op — Supabase row persists).

        The processed_messages table row serves as the permanent dedup record.
        No action needed on release — the row stays as the idempotency proof.

        This method exists for API symmetry in try/finally blocks.
        """
        # The Supabase row was already inserted on acquire() — nothing to do.

    async def is_processed(self, message_id: str) -> bool:
        """Check if a message has already been processed.

        Useful for pre-flight checks before expensive operations.

        Args:
            message_id: WhatsApp message ID to check.

        Returns:
            True if already processed, False if new.
        """
        from app.database import supabase

        try:
            result = (
                supabase.table("processed_messages")
                .select("id")
                .eq("message_id", message_id)
                .execute()
            )
            return bool(result.data)
        except Exception as e:
            logger.warning(f"Message queue: is_processed check error: {e}")
            return False  # Fail open


async def get_phone_lock(phone: str) -> asyncio.Lock:
    """Get (or create) a per-phone asyncio lock for concurrent state protection.

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
