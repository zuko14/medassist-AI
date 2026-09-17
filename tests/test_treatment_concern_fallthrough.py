"""A mislabelled concern must not become a main menu.

Reported from a live derma clinic: the patient tapped "Find by Concern",
typed "Dark circles", and got the main menu back. They typed "Acne" next and
the bot started a DOCTOR booking ("Who is this appointment for?") instead of
showing acne treatments.

Cause: the intent classifier labels a bare concern `book_appointment`, and the
guard in conversation.py that routes typed text to the concern search steps
aside for exactly that intent so a literal "book appointment" still escapes.
The text then reached handle_treatment_state, whose only behaviour was
"return to main menu" -- which also parked the session in main_menu, so the
NEXT concern hit the booking flow.

These pin the fix and the escapes it must not break.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import specialty_flow as sf

PHONE = "+919000000001"
DERMA = {"id": "clinic-1", "plan": "derma", "features": {}}


def _manager():
    m = MagicMock()
    m.whatsapp.send_text = AsyncMock()
    m.whatsapp.send_interactive_list = AsyncMock()
    m.whatsapp.send_interactive_buttons = AsyncMock()
    m.update_state = AsyncMock()
    m._send_main_menu = AsyncMock()
    m._handle_emergency = AsyncMock()
    return m


class TestConcernSurvivesMisclassification:
    @pytest.mark.asyncio
    async def test_a_typed_concern_is_searched_not_bounced_to_the_menu(self):
        m = _manager()
        with patch.object(sf, "handle_treatment_search_text", AsyncMock()) as search:
            await sf.handle_treatment_state(m, DERMA, PHONE, "en", message="Dark circles")

        search.assert_awaited_once()
        assert search.await_args.args[3] == "Dark circles"
        m._send_main_menu.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_reported_two_message_sequence_never_reaches_booking(self):
        """"Dark circles" then "Acne": both are concerns, neither is a booking."""
        m = _manager()
        with patch.object(sf, "handle_treatment_search_text", AsyncMock()) as search:
            await sf.handle_treatment_state(m, DERMA, PHONE, "en", message="Dark circles")
            await sf.handle_treatment_state(m, DERMA, PHONE, "en", message="Acne")

        assert [c.args[3] for c in search.await_args_list] == ["Dark circles", "Acne"]
        m._send_main_menu.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_padding_is_stripped_before_searching(self):
        m = _manager()
        with patch.object(sf, "handle_treatment_search_text", AsyncMock()) as search:
            await sf.handle_treatment_state(m, DERMA, PHONE, "en", message="  hair fall  ")
        assert search.await_args.args[3] == "hair fall"


class TestEscapesStillWork:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "word",
        ["menu", "Main Menu", "HOME", "cancel", "back", "exit", "stop",
         "hi", "hello", "book", "book appointment"],
    )
    async def test_exit_words_still_leave_the_flow(self, word):
        m = _manager()
        with patch.object(sf, "handle_treatment_search_text", AsyncMock()) as search:
            await sf.handle_treatment_state(m, DERMA, PHONE, "en", message=word)

        search.assert_not_awaited()
        m._send_main_menu.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_exit_word_inside_a_sentence_is_still_a_concern(self):
        """Substring matching would read this as "stop" and lose the patient."""
        m = _manager()
        with patch.object(sf, "handle_treatment_search_text", AsyncMock()) as search:
            await sf.handle_treatment_state(
                m, DERMA, PHONE, "en", message="I want to stop my hair fall"
            )
        search.assert_awaited_once()
        m._send_main_menu.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_tapped_row_is_not_treated_as_a_concern(self):
        """A stale button carries its own title as `message`."""
        m = _manager()
        with patch.object(sf, "handle_treatment_search_text", AsyncMock()) as search:
            await sf.handle_treatment_state(m, DERMA, PHONE, "en")

        search.assert_not_awaited()
        m._send_main_menu.assert_awaited_once()


class TestCallSiteForwardsTheMessage:
    """The fix is worthless if conversation.py keeps dropping the text."""

    def test_conversation_passes_text_but_not_a_tapped_row(self):
        import inspect

        from app.services import conversation

        src = inspect.getsource(conversation)
        call = src.split("specialty_flow.handle_treatment_state(")[1][:220]
        assert 'message="" if interactive_data else message' in call
