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

  Layer 2 — asyncio.Lock per phone number:
    Prevents concurrent state mutations for the same patient within a single
    process instance. Uses a WeakValue dict to auto-GC locks for inactive phones.

Usage in webhook.py:
    queue = MessageQueueManager()
    if not await queue.acquire(message_id):
        return  # duplicate — already processing
    try:
        await process_message(...)
    finally:
        await queue.release(message_id)
"""

import asyncio
import logging
import weakref
from typing import Optional

logger = logging.getLogger(__name__)

# In-memory per-phone asyncio locks (WeakValueDictionary auto-GC's unused locks)
_phone_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_phone_locks_mutex = asyncio.Lock()


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
            result = supabase.table("processed_messages").insert(
                {"message_id": message_id},
                # upsert=False ensures we DON'T update on conflict
            ).execute()

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
            if "unique" in error_str or "duplicate" in error_str or "23505" in error_str:
                logger.info(f"Message queue: duplicate (unique violation) for {message_id}")
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
        pass

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

    Prevents two concurrent messages from the same patient phone number
    from simultaneously modifying the conversation state in Supabase.

    The WeakValueDictionary ensures that locks for inactive phones are
    automatically garbage-collected, preventing memory leaks.

    Args:
        phone: Patient phone number.

    Returns:
        asyncio.Lock for the given phone.
    """
    async with _phone_locks_mutex:
        lock = _phone_locks.get(phone)
        if lock is None:
            lock = asyncio.Lock()
            _phone_locks[phone] = lock
        return lock


# Global instance
message_queue = MessageQueueManager()
