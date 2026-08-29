"""Regression tests for what Kriya does with inbound messages it cannot read.

Meta delivers far more than text. Before READABLE_MESSAGE_TYPES existed, a
voice note, photo, PDF or shared location arrived with an empty body and fell
straight through the whole state machine as a blank text message:

  * mid-booking in collecting_name, a voice note answered "Name is too short"
  * every photo burned a paid LLM intent call classifying an empty string
  * a thumbs-up reaction on a booking confirmation re-opened the conversation

These tests pin the three behaviours that fix relies on:
  1. Unreadable media gets a localized "please type it" reply.
  2. Conversation state is left exactly where the patient left it.
  3. Reactions and system events get no reply at all.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.conversation import (
    IGNORED_MESSAGE_TYPES,
    READABLE_MESSAGE_TYPES,
    ConversationManager,
)
from app.templates.whatsapp_templates import get_message

CLINIC = {
    "id": "clinic-1",
    "name": "Apollo Clinic",
    "phone": "+919999999999",
    "config": {},
}
PHONE = "+919876543210"


def _patches(state="collecting_name", language="en"):
    """Patch everything _handle_message_locked touches before the media guard."""
    session = {
        "state": state,
        "context": {"booking_name": None},
        "last_processed_message_id": None,
        "booking_context_expires_at": None,
    }
    patient = {"id": "p1", "phone": PHONE, "language": language, "visit_count": 1}

    return [
        patch("app.services.conversation.get_or_create_conversation",
              new=AsyncMock(return_value=session)),
        patch("app.services.conversation.get_patient_by_phone",
              new=AsyncMock(return_value=patient)),
        patch("app.services.conversation.update_conversation", new=AsyncMock()),
        patch("app.services.conversation.detect_intent",
              new=AsyncMock(return_value="unknown")),
    ]


async def _run(manager, message_type, state="collecting_name", language="en"):
    patches = _patches(state, language)
    for p in patches:
        p.start()
    try:
        await manager._handle_message_locked(
            clinic=CLINIC,
            phone=PHONE,
            message="",
            message_type=message_type,
            message_id="wamid.TEST1",
        )
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media_type",
    ["audio", "image", "video", "document", "sticker", "location", "contacts"],
)
async def test_unreadable_media_gets_a_reply_instead_of_a_state_machine_error(media_type):
    manager = ConversationManager()
    manager.whatsapp = MagicMock()
    manager.whatsapp.send_text = AsyncMock()

    await _run(manager, media_type)

    manager.whatsapp.send_text.assert_awaited_once()
    sent = manager.whatsapp.send_text.await_args[0][2]
    assert sent == get_message("unsupported_media", "en")
    # The old behaviour in collecting_name: validate_name("") -> "Name is too short."
    assert "too short" not in sent.lower()


@pytest.mark.asyncio
async def test_unreadable_media_never_reaches_intent_detection():
    """An empty body must not cost a paid LLM classification call."""
    manager = ConversationManager()
    manager.whatsapp = MagicMock()
    manager.whatsapp.send_text = AsyncMock()

    patches = _patches("main_menu")
    detect = patches[-1].new
    for p in patches:
        p.start()
    try:
        await manager._handle_message_locked(
            clinic=CLINIC, phone=PHONE, message="",
            message_type="audio", message_id="wamid.TEST2",
        )
        detect.assert_not_awaited()
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_unreadable_media_preserves_booking_state():
    """A voice note mid-booking must not knock the patient out of their flow."""
    manager = ConversationManager()
    manager.whatsapp = MagicMock()
    manager.whatsapp.send_text = AsyncMock()
    manager.update_state = AsyncMock()

    await _run(manager, "audio", state="selecting_slot")

    manager.update_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["hi", "te"])
async def test_media_reply_is_localized(lang):
    manager = ConversationManager()
    manager.whatsapp = MagicMock()
    manager.whatsapp.send_text = AsyncMock()

    await _run(manager, "image", language=lang)

    sent = manager.whatsapp.send_text.await_args[0][2]
    assert sent == get_message("unsupported_media", lang)
    assert sent != get_message("unsupported_media", "en")


@pytest.mark.asyncio
@pytest.mark.parametrize("noise_type", sorted(IGNORED_MESSAGE_TYPES))
async def test_reactions_and_system_events_get_no_reply(noise_type):
    """A thumbs-up on a booking confirmation must not re-open the conversation."""
    manager = ConversationManager()
    manager.whatsapp = MagicMock()
    manager.whatsapp.send_text = AsyncMock()

    await _run(manager, noise_type, state="main_menu")

    manager.whatsapp.send_text.assert_not_awaited()


def test_readable_and_ignored_type_sets_do_not_overlap():
    assert not (READABLE_MESSAGE_TYPES & IGNORED_MESSAGE_TYPES)
    # Template quick-reply buttons arrive as type "button", not "interactive" —
    # they carry real patient intent and must stay readable.
    assert "button" in READABLE_MESSAGE_TYPES
    assert "interactive" in READABLE_MESSAGE_TYPES
    assert "text" in READABLE_MESSAGE_TYPES
