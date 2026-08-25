"""Regression: ingest() must not pre-claim the processed_messages row that acquire() needs.

Production outage 2026-08-25: message_queue.ingest() inserted into `processed_messages`
as a side effect. process_message() then called message_queue.acquire(), which claims a
message by INSERTing the same message_id into `processed_messages` and returns False on a
unique violation. Because ingest() had already written that row, acquire() saw every
message as its own duplicate and returned False, so process_message() returned early at
the guard and no reply was ever sent.

The inbound row was still marked 'completed' with attempt_count=0 and last_error=None,
so the failure was invisible: 100% of patient messages were silently dropped while every
metric reported success.

Invariant: a message that has just been ingested must still be acquirable exactly once.
"""

import pytest
from unittest.mock import MagicMock, patch

from app.services.message_queue import message_queue


class _FakeTables:
    """Minimal Supabase double enforcing UNIQUE(message_id) per table.

    The shared store is the point: the real bug only appears when ingest() and
    acquire() write to the same processed_messages table.
    """

    def __init__(self):
        self.rows = {"inbound_messages": set(), "processed_messages": set()}

    def table(self, name):
        store = self.rows.setdefault(name, set())

        class _Q:
            def insert(_self, payload):
                mid = payload.get("message_id")

                class _Exec:
                    def execute(_e):
                        if mid in store:
                            raise Exception(
                                'duplicate key value violates unique constraint "23505"'
                            )
                        store.add(mid)
                        res = MagicMock()
                        res.data = [dict(payload)]
                        return res

                return _Exec()

        return _Q()


@pytest.mark.asyncio
async def test_ingested_message_is_still_acquirable():
    """After ingest() accepts a new message, acquire() must grant the claim.

    Fails before the fix: ingest() wrote processed_messages, so acquire() saw a
    unique violation and returned False -> process_message() returned early ->
    the patient never got a reply.
    """
    fake = _FakeTables()
    message_id = "wamid.REGRESSION_TEST_001"

    with patch("app.database.supabase", fake):
        is_new, _row = await message_queue.ingest(
            message_id=message_id,
            phone="+919999999999",
            display_phone="+15551649189",
            payload={"object": "whatsapp_business_account"},
            clinic_id=None,
            phone_number_id="971342239407011",
        )
        assert is_new is True, "first ingest of a new message must be accepted"

        acquired = await message_queue.acquire(message_id, clinic_id=None)

    assert acquired is True, (
        "acquire() must grant the claim for a freshly ingested message. "
        "If this is False, ingest() has pre-claimed processed_messages and every "
        "inbound message will be silently dropped in production."
    )


@pytest.mark.asyncio
async def test_second_ingest_of_same_message_is_rejected():
    """The durable queue still deduplicates genuine Meta redeliveries."""
    fake = _FakeTables()
    message_id = "wamid.REGRESSION_TEST_002"

    with patch("app.database.supabase", fake):
        first, _ = await message_queue.ingest(
            message_id=message_id,
            phone="+919999999999",
            display_phone="+15551649189",
            payload={},
            clinic_id=None,
            phone_number_id="971342239407011",
        )
        second, _ = await message_queue.ingest(
            message_id=message_id,
            phone="+919999999999",
            display_phone="+15551649189",
            payload={},
            clinic_id=None,
            phone_number_id="971342239407011",
        )

    assert first is True
    assert second is False, "a redelivered wamid must not be ingested twice"
