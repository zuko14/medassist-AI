"""Regressions from a live Accumx Diagnostics session (2026-09-19).

1. A lab test was booked without asking who it was for: the admin panel
   showed the literal "Patient".
2. "cancel" / "cancel booking" went unanswered for a patient holding a lab
   booking (doctor_name NULL -> TypeError building the list).
3. "delete my data" erased the data but the reply never arrived: the purge
   removed the conversation row the 24h-window check reads.
4. The guide's commands depended on the LLM ("cancel booking" fell back to
   book_appointment, "change language" to reschedule_appointment).
"""

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_engine import detect_intent, guide_command_intent
from app.services.conversation import ConversationManager

CLINIC = {"id": "clinic-1", "name": "Accumx Diagnostics", "whatsapp_number": "919999999999"}
PHONE = "919876543210"


def _ctx(**extra):
    ctx = {
        "lab_test_id": "t1",
        "lab_test_name": "(1,3)-BETA-D-GLUCAN",
        "lab_test_price_paise": 1200000,
        "branch_id": None,
    }
    ctx.update(extra)
    return ctx


def _booked():
    return {"success": True, "appointment": {"id": "a1", "booking_ref": "MC-2026-UKJQDVTZ"}}


@pytest.fixture(autouse=True)
def _no_family_db():
    """Never reach the real family_members table; tests that need saved
    members patch it again with their own list."""
    with patch("app.services.conversation.get_family_members", new_callable=AsyncMock, return_value=[]):
        yield


def _patches(manager):
    """Counter-payment centre, every outbound call captured."""
    return (
        patch("app.services.payment.resolve_payment_mode", return_value=("none", 100)),
        patch("app.services.conversation.book_appointment", new_callable=AsyncMock, return_value=_booked()),
        patch("app.services.conversation.update_patient", new_callable=AsyncMock),
        patch("app.database.get_lab_collection_window", new_callable=AsyncMock,
              return_value={"start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
                            "sunday_start": "07:00", "sunday_end": "14:00"}),
        patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock),
        patch.object(manager.whatsapp, "send_interactive_buttons", new_callable=AsyncMock),
        patch.object(manager, "_send_main_menu", new_callable=AsyncMock),
        patch.object(manager, "update_state", new_callable=AsyncMock),
    )


# ── 1. Lab booking asks who it is for ────────────────────────────────────────


@pytest.mark.asyncio
async def test_date_tap_asks_who_and_books_nothing():
    m = ConversationManager()
    ctx = _ctx()
    p = _patches(m)
    with p[0], p[1] as book, p[2], p[3], p[4], p[5] as buttons, p[6], p[7] as state:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "", "", ctx, {"name": "Ravi Kumar"}, "en",
            interactive_data={"id": "labdate_2026-09-20"},
        )
    book.assert_not_called()
    ids = [b["id"] for b in buttons.call_args.kwargs["buttons"]]
    assert ids == ["labfor_self", "labfor_other"]
    assert ctx["lab_collection_date"] == "2026-09-20" and ctx["lab_step"] == "who"
    assert state.call_args[0][2] == "confirming_collection_date"


@pytest.mark.asyncio
async def test_for_me_with_saved_name_books_under_that_name():
    m = ConversationManager()
    ctx = _ctx(lab_collection_date="2026-09-20", lab_step="who")
    p = _patches(m)
    with p[0], p[1] as book, p[2] as upd, p[3], p[4] as text, p[5], p[6], p[7] as state:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "For Me", "", ctx, {"name": "Ravi Kumar"}, "en",
            interactive_data={"id": "labfor_self"},
        )
    written = book.call_args[0][1]
    assert written["patient_name"] == "Ravi Kumar"
    assert written["appointment_date"] == "2026-09-20"
    upd.assert_not_called()
    confirmation = text.call_args[0][2]
    assert "Ravi Kumar" in confirmation and "MC-2026-UKJQDVTZ" in confirmation
    # 20 Sep 2026 is a Sunday: only Sunday hours are quoted.
    assert "07:00 - 14:00" in confirmation and "21:00" not in confirmation
    final = state.call_args[0]
    assert final[2] == "main_menu" and final[3]["lab_step"] is None


@pytest.mark.asyncio
async def test_for_me_without_saved_name_asks_then_saves_it():
    m = ConversationManager()
    ctx = _ctx(lab_collection_date="2026-09-21", lab_step="who")
    p = _patches(m)
    with p[0], p[1] as book, p[2] as upd, p[3], p[4], p[5], p[6], p[7]:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "For Me", "", ctx, {"name": None}, "en",
            interactive_data={"id": "labfor_self"},
        )
        book.assert_not_called()
        assert ctx["lab_step"] == "name" and ctx["lab_for_self"] is True

        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "Chaitanya Kumar", "", ctx, {"name": None}, "en",
        )
    upd.assert_awaited_once_with(CLINIC["id"], PHONE, {"name": "Chaitanya Kumar"})
    assert book.call_args[0][1]["patient_name"] == "Chaitanya Kumar"


@pytest.mark.asyncio
async def test_someone_else_books_their_name_and_leaves_the_account_holders_alone():
    m = ConversationManager()
    ctx = _ctx(lab_collection_date="2026-09-21", lab_step="who")
    p = _patches(m)
    with p[0], p[1] as book, p[2] as upd, p[3], p[4], p[5], p[6], p[7]:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "Someone Else", "", ctx, {"name": "Ravi Kumar"}, "en",
            interactive_data={"id": "labfor_other"},
        )
        book.assert_not_called()
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "Lakshmi Devi", "", ctx, {"name": "Ravi Kumar"}, "en",
        )
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "", "", ctx, {"name": "Ravi Kumar"}, "en",
            interactive_data={"id": "labsave_no"},
        )
    upd.assert_not_called()
    assert book.call_args[0][1]["patient_name"] == "Lakshmi Devi"


@pytest.mark.asyncio
async def test_invalid_name_is_refused_and_nothing_is_booked():
    m = ConversationManager()
    ctx = _ctx(lab_collection_date="2026-09-21", lab_step="name", lab_for_self=False)
    p = _patches(m)
    with p[0], p[1] as book, p[2], p[3], p[4] as text, p[5], p[6], p[7]:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "12345", "", ctx, {"name": None}, "en",
        )
    book.assert_not_called()
    text.assert_called_once()


@pytest.mark.asyncio
async def test_typed_name_reaches_the_lab_handler_whatever_the_classifier_says():
    """At a diagnostics-only clinic doctor_availability restarts the lab flow;
    a name the classifier mislabels must still be taken as the name."""
    m = ConversationManager()
    session = {"state": "confirming_collection_date",
               "context": _ctx(lab_collection_date="2026-09-21", lab_step="name")}
    patient = {"language": "en", "data_consent": True}
    with patch("app.services.conversation.get_lang", new_callable=AsyncMock, return_value="en"), \
         patch.object(m, "_handle_confirming_collection_date", new_callable=AsyncMock) as handler, \
         patch.object(m, "_start_lab_booking", new_callable=AsyncMock) as restart:
        await m._process_state(CLINIC, PHONE, "Ravi Kumar", "doctor_availability", session, patient, "en")
    handler.assert_awaited_once()
    restart.assert_not_called()


# ── 2. Cancel list for a lab booking ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_lists_a_lab_booking_by_its_test_name():
    m = ConversationManager()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    lab = {"id": "a1", "doctor_name": None, "lab_test_name": "(1,3)-BETA-D-GLUCAN",
           "appointment_date": tomorrow, "appointment_time": None, "status": "confirmed"}

    async def appts(_cid, _phone, status=None, from_date=None):
        return [lab] if status == "confirmed" else []

    with patch("app.database.get_patient_appointments", side_effect=appts), \
         patch.object(m.whatsapp, "send_interactive_list", new_callable=AsyncMock) as lst:
        await m._handle_cancel_request(CLINIC, PHONE, {}, "en")
    row = lst.call_args.kwargs["sections"][0]["rows"][0]
    assert row["id"] == "cancel_a1"
    assert row["title"] == "(1,3)-BETA-D-GLUCAN"


# ── 3. Data deletion reply ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_my_data_reply_is_sent_after_the_conversation_row_is_gone():
    m = ConversationManager()
    with patch("app.database.delete_patient_data", new_callable=AsyncMock, return_value=True), \
         patch("app.services.conversation.log_analytics_event", new_callable=AsyncMock), \
         patch.object(m.whatsapp, "send_text", new_callable=AsyncMock) as text:
        await m._handle_data_deletion(CLINIC, PHONE, {}, "en")
    assert text.call_args.kwargs.get("_window_open") is True


@pytest.mark.asyncio
async def test_send_text_with_window_open_skips_the_conversation_row_check():
    from app.services.whatsapp import WhatsAppService

    wa = WhatsAppService()
    with patch.object(wa, "_can_send_freeform", new_callable=AsyncMock, return_value=False) as window, \
         patch.object(wa, "_make_request", new_callable=AsyncMock, return_value={}) as req, \
         patch.object(wa, "_log_to_ledger", new_callable=AsyncMock):
        assert await wa.send_text(CLINIC, PHONE, "gone", _window_open=True) is True
        assert await wa.send_text(CLINIC, PHONE, "blocked") is False
    window.assert_awaited_once()
    req.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_deletion_says_so_instead_of_claiming_success():
    m = ConversationManager()
    with patch("app.database.delete_patient_data", new_callable=AsyncMock, return_value=False), \
         patch("app.services.conversation.log_analytics_event", new_callable=AsyncMock) as log, \
         patch.object(m.whatsapp, "send_text", new_callable=AsyncMock) as text:
        await m._handle_data_deletion(CLINIC, PHONE, {}, "en")
    assert "couldn't" in text.call_args[0][2]
    log.assert_not_called()


# ── 4. Guide commands never depend on the LLM ────────────────────────────────


@pytest.mark.parametrize("typed,intent", [
    ("cancel", "cancel_appointment"),
    ("Cancel booking", "cancel_appointment"),
    ("change language", "change_language"),
    ("Change Language.", "change_language"),
    ("talk to staff", "human_escalation"),
    ("emergency", "emergency"),
    ("reschedule", "reschedule_appointment"),
    ("book test", None),
    ("cancel my thyroid test tomorrow please", None),
])
def test_guide_command_intent(typed, intent):
    assert guide_command_intent(typed) == intent


@pytest.mark.asyncio
async def test_guide_commands_skip_the_llm():
    with patch("app.services.ai_engine.call_openrouter_with_backoff", new_callable=AsyncMock) as llm:
        assert await detect_intent("Cancel booking") == "cancel_appointment"
        assert await detect_intent("change language") == "change_language"
        assert await detect_intent("delete my data") == "data_deletion_request"
    llm.assert_not_called()


@pytest.mark.asyncio
async def test_stale_language_button_applies_directly_for_a_consented_patient():
    m = ConversationManager()
    session = {"state": "main_menu", "context": {}}
    patient = {"language": "en", "data_consent": True}
    with patch("app.services.conversation.get_lang", new_callable=AsyncMock, return_value="en"), \
         patch.object(m, "_handle_selecting_language", new_callable=AsyncMock) as apply, \
         patch.object(m, "_send_language_selection", new_callable=AsyncMock) as picker:
        await m._process_state(CLINIC, PHONE, "हिंदी", "select_language", session, patient, "en",
                               interactive_data={"id": "lang_hi"})
    apply.assert_awaited_once()
    picker.assert_not_called()


@pytest.mark.asyncio
async def test_typed_change_language_still_shows_the_picker():
    m = ConversationManager()
    session = {"state": "main_menu", "context": {}}
    patient = {"language": "en", "data_consent": True}
    with patch("app.services.conversation.get_lang", new_callable=AsyncMock, return_value="en"), \
         patch.object(m, "_send_language_selection", new_callable=AsyncMock) as picker, \
         patch.object(m, "update_state", new_callable=AsyncMock) as state:
        await m._process_state(CLINIC, PHONE, "change language", "change_language", session, patient, "en")
    picker.assert_awaited_once()
    assert state.call_args[0][2] == "selecting_language"


# ── 5. Paid lab bookings honour the centre's deposit setting ─────────────────


@pytest.mark.asyncio
async def test_partial_payment_centre_charges_the_deposit_and_links_the_patient():
    m = ConversationManager()
    ctx = _ctx(lab_collection_date="2026-09-21", lab_step="who")
    paid = {"success": True, "booking_id": "b1", "booking_ref": "MC-1", "payment_link": "https://rzp.io/i/x",
            "amount_paise": 360000, "hold_expires_at": "2026-09-21T10:00:00Z"}
    with patch("app.services.payment.resolve_payment_mode", return_value=("partial", 30)), \
         patch("app.services.payment.payment_service.create_booking_with_payment",
               new_callable=AsyncMock, return_value=paid) as create, \
         patch.object(m.whatsapp, "send_text", new_callable=AsyncMock) as text, \
         patch.object(m, "update_state", new_callable=AsyncMock) as state:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "", "", ctx, {"id": "p1", "name": "Ravi Kumar"}, "en",
            interactive_data={"id": "labfor_self"},
        )
    kw = create.call_args.kwargs
    assert kw["deposit_percent"] == 30 and kw["patient_id"] == "p1"
    assert kw["patient_name"] == "Ravi Kumar"
    assert "30% deposit" in text.call_args[0][2]
    assert state.call_args[0][2] == "awaiting_payment"


# ── 6. Saved family members ──────────────────────────────────────────────────


FAMILY = [{"full_name": "Lakshmi Devi"}, {"full_name": "Suresh Kumar"}]


@pytest.mark.asyncio
async def test_saved_family_members_are_offered_as_a_list():
    m = ConversationManager()
    ctx = _ctx()
    p = _patches(m)
    with patch("app.services.conversation.get_family_members", new_callable=AsyncMock, return_value=FAMILY), \
         patch.object(m.whatsapp, "send_interactive_list", new_callable=AsyncMock) as lst, \
         p[0], p[1] as book, p[2], p[3], p[4], p[5] as buttons, p[6], p[7]:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "", "", ctx, {"name": "Ravi Kumar"}, "en",
            interactive_data={"id": "labdate_2026-09-21"},
        )
    book.assert_not_called()
    buttons.assert_not_called()
    rows = lst.call_args.kwargs["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["labfor_self", "labfor_fam_0", "labfor_fam_1", "labfor_other"]
    assert rows[1]["title"] == "Lakshmi Devi"
    assert ctx["lab_family"] == ["Lakshmi Devi", "Suresh Kumar"]


@pytest.mark.asyncio
async def test_picking_a_saved_member_books_under_their_name():
    m = ConversationManager()
    ctx = _ctx(lab_collection_date="2026-09-21", lab_step="who", lab_family=["Lakshmi Devi", "Suresh Kumar"])
    p = _patches(m)
    with p[0], p[1] as book, p[2] as upd, p[3], p[4], p[5], p[6], p[7]:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "Suresh Kumar", "", ctx, {"name": "Ravi Kumar"}, "en",
            interactive_data={"id": "labfor_fam_1"},
        )
    assert book.call_args[0][1]["patient_name"] == "Suresh Kumar"
    upd.assert_not_called()


@pytest.mark.asyncio
async def test_stale_family_index_asks_again_instead_of_booking():
    m = ConversationManager()
    ctx = _ctx(lab_collection_date="2026-09-21", lab_step="who", lab_family=["Lakshmi Devi"])
    p = _patches(m)
    with p[0], p[1] as book, p[2], p[3], p[4], p[5] as buttons, p[6], p[7]:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "", "", ctx, {"name": "Ravi Kumar"}, "en",
            interactive_data={"id": "labfor_fam_5"},
        )
    book.assert_not_called()
    buttons.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("tap,saves", [("labsave_yes", True), ("labsave_no", False)])
async def test_new_name_offers_save_and_both_answers_book(tap, saves):
    m = ConversationManager()
    ctx = _ctx(lab_collection_date="2026-09-21", lab_step="name", lab_for_self=False, lab_family=[])
    p = _patches(m)
    with patch("app.services.conversation.add_family_member", new_callable=AsyncMock) as add, \
         p[0], p[1] as book, p[2], p[3], p[4], p[5] as buttons, p[6], p[7]:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "Anita Sharma", "", ctx, {"name": "Ravi Kumar"}, "en",
        )
        book.assert_not_called()
        assert [b["id"] for b in buttons.call_args.kwargs["buttons"]] == ["labsave_yes", "labsave_no"]
        assert ctx["lab_step"] == "save"

        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "", "", ctx, {"name": "Ravi Kumar"}, "en", interactive_data={"id": tap},
        )
    assert book.call_args[0][1]["patient_name"] == "Anita Sharma"
    if saves:
        add.assert_awaited_once_with(CLINIC["id"], PHONE, full_name="Anita Sharma")
    else:
        add.assert_not_called()


@pytest.mark.asyncio
async def test_typing_an_already_saved_name_books_without_asking_to_save():
    m = ConversationManager()
    ctx = _ctx(lab_collection_date="2026-09-21", lab_step="name", lab_for_self=False,
               lab_family=["Lakshmi Devi"])
    p = _patches(m)
    with patch("app.services.conversation.add_family_member", new_callable=AsyncMock) as add, \
         p[0], p[1] as book, p[2], p[3], p[4], p[5] as buttons, p[6], p[7]:
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "lakshmi devi", "", ctx, {"name": "Ravi Kumar"}, "en",
        )
    buttons.assert_not_called()
    add.assert_not_called()
    book.assert_called_once()


# ── 7. Doctor booking: new family names can be saved ─────────────────────────


def _doctor_patches(manager, saved=()):
    return (
        patch("app.services.conversation.get_family_members", new_callable=AsyncMock, return_value=list(saved)),
        patch("app.services.conversation.add_family_member", new_callable=AsyncMock),
        patch("app.services.conversation.update_patient", new_callable=AsyncMock),
        patch("app.services.specialty_flow.route_to_treatment_doctors", new_callable=AsyncMock, return_value=False),
        patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock),
        patch.object(manager.whatsapp, "send_interactive_buttons", new_callable=AsyncMock),
        patch.object(manager, "update_state", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tap,saves", [("save_family_yes", True), ("save_family_no", False)])
async def test_doctor_booking_offers_to_save_a_family_name_then_asks_symptoms(tap, saves):
    m = ConversationManager()
    ctx = {"for_self": False}
    p = _doctor_patches(m)
    with p[0], p[1] as add, p[2] as upd, p[3], p[4] as text, p[5] as buttons, p[6] as state:
        await m._handle_collecting_name(CLINIC, PHONE, "Lakshmi Devi", ctx, {"name": "Ravi Kumar"}, "en")
        assert [b["id"] for b in buttons.call_args.kwargs["buttons"]] == ["save_family_yes", "save_family_no"]
        assert state.call_args[0][2] == "confirming_save_family_member"
        upd.assert_not_called()

        await m._handle_confirming_save_family_member(CLINIC, PHONE, tap, ctx, "en")
    if saves:
        add.assert_awaited_once_with(CLINIC["id"], PHONE, full_name="Lakshmi Devi", relationship=None)
    else:
        add.assert_not_called()
    # Booking carries on to symptoms, for the family member.
    assert state.call_args[0][2] == "collecting_symptoms"
    assert state.call_args[0][3]["booking_name"] == "Lakshmi Devi"


@pytest.mark.asyncio
async def test_doctor_booking_for_self_is_never_offered_a_family_save():
    m = ConversationManager()
    p = _doctor_patches(m)
    with p[0], p[1], p[2] as upd, p[3], p[4], p[5] as buttons, p[6] as state:
        await m._handle_collecting_name(CLINIC, PHONE, "Ravi Kumar", {"for_self": True}, {}, "en")
    buttons.assert_not_called()
    upd.assert_awaited_once()
    assert state.call_args[0][2] == "collecting_symptoms"


@pytest.mark.asyncio
async def test_doctor_booking_skips_the_offer_for_an_already_saved_name():
    m = ConversationManager()
    p = _doctor_patches(m, saved=[{"full_name": "Lakshmi Devi"}])
    with p[0], p[1], p[2], p[3], p[4], p[5] as buttons, p[6] as state:
        await m._handle_collecting_name(CLINIC, PHONE, "lakshmi devi", {"is_family": True}, {}, "en")
    buttons.assert_not_called()
    assert state.call_args[0][2] == "collecting_symptoms"


@pytest.mark.asyncio
async def test_doctor_save_prompt_repeats_on_unrelated_input():
    m = ConversationManager()
    ctx = {"pending_family_name": "Lakshmi Devi"}
    p = _doctor_patches(m)
    with p[0], p[1] as add, p[2], p[3], p[4], p[5] as buttons, p[6] as state:
        await m._handle_confirming_save_family_member(CLINIC, PHONE, "what?", ctx, "en")
    buttons.assert_awaited_once()
    add.assert_not_called()
    state.assert_not_called()
