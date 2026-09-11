"""The patient-facing report archive is gone.

Kriya delivers each lab report the moment the lab releases it. It never offers
patients a list of past reports to browse and re-download, because that would
oblige us to hold every PDF for as long as any patient might ask for it. These
tests fail if the "My Reports" row, the report list, or the patient-triggered
resend comes back.
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager


CLINIC = {"id": "clinic-reports-1", "whatsapp_number": "+911111111111"}
PHONE = "+919876543210"


async def _menu_rows(manager, diagnostics_only=False):
    """Render the main menu and return its rows."""
    with patch.object(
        manager, "_is_diagnostics_only", new_callable=AsyncMock,
        return_value=diagnostics_only,
    ), patch(
        # Every feature on: if a reports row can appear at all, it appears here.
        "app.services.tenant.has_feature", return_value=True
    ), patch.object(
        manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as mock_list:
        await manager._send_main_menu(CLINIC, PHONE, "en")

    return mock_list.await_args.kwargs["sections"][0]["rows"]


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostics_only", [False, True])
async def test_main_menu_never_offers_a_reports_row(diagnostics_only):
    rows = await _menu_rows(ConversationManager(), diagnostics_only)

    assert all(r["id"] != "menu_reports" for r in rows), rows
    assert all("report" not in r["title"].lower() for r in rows), rows


@pytest.mark.asyncio
async def test_main_menu_still_offers_booking_and_escalation():
    """Guard the rows that must survive the removal."""
    rows = await _menu_rows(ConversationManager())
    ids = [r["id"] for r in rows]

    assert ids[0] == "menu_book"
    for required in ("menu_services", "menu_doctors", "menu_lab_tests",
                     "menu_emergency", "menu_human"):
        assert required in ids, ids
    # Meta rejects a list section with more than 10 rows.
    assert len(rows) <= 10


@pytest.mark.asyncio
async def test_report_request_is_answered_without_listing_reports():
    """A stale "My Reports" tap, or a typed "my reports", must land on the
    automatic-delivery answer — never on a lookup."""
    manager = ConversationManager()

    with patch("app.services.tenant.has_feature", return_value=True), \
         patch("app.services.lab_reports.LabReportService") as mock_service, \
         patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock) as mock_text, \
         patch.object(manager, "update_state", new_callable=AsyncMock) as mock_state, \
         patch.object(manager, "_send_main_menu", new_callable=AsyncMock) as mock_menu:
        await manager._handle_view_reports(CLINIC, PHONE, "en")

    mock_service.assert_not_called()
    body = mock_text.await_args.args[2]
    assert "automatically" in body
    assert "reception" in body
    # The patient may have been mid-booking when they asked.
    mock_state.assert_awaited_once_with(
        CLINIC, PHONE, "main_menu", {"menu_shown": False}
    )
    mock_menu.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "hi", "te", "xx"])
async def test_report_answer_is_non_empty_in_every_language(lang):
    manager = ConversationManager()

    with patch("app.services.tenant.has_feature", return_value=True), \
         patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock) as mock_text, \
         patch.object(manager, "update_state", new_callable=AsyncMock), \
         patch.object(manager, "_send_main_menu", new_callable=AsyncMock):
        await manager._handle_view_reports(CLINIC, PHONE, lang)

    assert mock_text.await_args.args[2].strip()


@pytest.mark.asyncio
async def test_clinic_without_report_delivery_is_sent_to_reception():
    """Pre-existing behaviour for clinics that never had report delivery."""
    manager = ConversationManager()

    with patch("app.services.tenant.has_feature", return_value=False), \
         patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock) as mock_text, \
         patch.object(manager, "update_state", new_callable=AsyncMock), \
         patch.object(manager, "_send_main_menu", new_callable=AsyncMock):
        await manager._handle_view_reports(CLINIC, PHONE, "en")

    assert "not available" in mock_text.await_args.args[2]


def test_no_patient_facing_report_archive_remains():
    """The list state and its handler are gone, so no conversation path can
    reach LabReportService.resend_report. Admin resend is unaffected."""
    assert not hasattr(ConversationManager, "_handle_viewing_reports")

    import inspect
    import app.services.conversation as conv

    source = inspect.getsource(conv)
    assert "resend_report" not in source
    assert "get_reports_by_phone" not in source


async def _route(manager, message, intent, *, lab_booking=True, state="main_menu"):
    """Run the from-any-state routing block and report where the message went."""
    with patch("app.services.tenant.has_feature", return_value=lab_booking), \
         patch("app.services.conversation.get_lang", new_callable=AsyncMock,
               return_value="en"), \
         patch.object(manager, "_start_lab_booking", new_callable=AsyncMock) as lab, \
         patch.object(manager, "_handle_view_reports", new_callable=AsyncMock) as reports, \
         patch.object(manager, "_start_booking", new_callable=AsyncMock) as booking, \
         patch.object(manager, "_handle_main_menu", new_callable=AsyncMock), \
         patch.object(manager, "_send_main_menu", new_callable=AsyncMock), \
         patch.object(manager, "update_state", new_callable=AsyncMock), \
         patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock):
        await manager._process_state(
            CLINIC, PHONE, message, intent,
            {"state": state, "context": {}},
            {"phone": PHONE, "language": "en"}, "en",
        )
    return {"lab": lab.await_count, "reports": reports.await_count,
            "booking": booking.await_count}


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["lab test", "Lab Test", "  LAB TESTS  "])
@pytest.mark.parametrize("intent", ["book_appointment", "view_reports", "unknown"])
async def test_lab_test_always_starts_lab_booking(message, intent):
    """Whatever the classifier decides, "lab test" books a test. It must never
    reach the doctor flow or the reports answer."""
    hit = await _route(ConversationManager(), message, intent)

    assert hit["lab"] == 1, hit
    assert hit["reports"] == 0, hit
    assert hit["booking"] == 0, hit


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["book test", "book lab test", "booktest"])
async def test_existing_book_test_words_still_start_lab_booking(message):
    """Regression: the report caption and template quick-reply say "BOOK TEST"."""
    assert (await _route(ConversationManager(), message, "book_appointment"))["lab"] == 1


@pytest.mark.asyncio
async def test_lab_test_falls_through_where_booking_is_not_sold():
    hit = await _route(ConversationManager(), "lab test", "unknown", lab_booking=False)

    assert hit["lab"] == 0, hit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message", ["cancel my lab test", "when is my lab test", "lab test results"]
)
async def test_lab_booking_words_match_the_whole_message_only(message):
    """Substring matching would turn a cancellation into a new booking."""
    assert (await _route(ConversationManager(), message, "unknown"))["lab"] == 0


def test_lab_test_is_not_a_reports_keyword():
    from app.services.ai_engine import keyword_intent_fallback

    assert keyword_intent_fallback("lab test") != "view_reports"
    # The report-reading phrases around it must survive.
    assert keyword_intent_fallback("lab reports") == "view_reports"
    assert keyword_intent_fallback("my reports") == "view_reports"
    assert keyword_intent_fallback("blood report") == "view_reports"


def test_system_prompt_does_not_offer_report_lookup():
    from app.services.ai_engine import build_system_prompt

    with patch("app.services.tenant.has_feature", return_value=True):
        prompt = build_system_prompt({"id": "clinic-reports-1", "name": "Test"})

    assert "CANNOT look up" in prompt
    assert "registered phone number to look up results" not in prompt
