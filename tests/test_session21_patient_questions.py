"""Session 21: four production reports from 23 Sep 2026.

1. "I need the menu" / "Main menu" answered with the language picker: the
   patient had typed "change my language" the day before and never tapped a
   language. selecting_language never expires and rejected all typed text.
2. "Do you provide new born checkup" answered with the department picker,
   although the clinic lists "Newborn Check-up" under What We Treat.
3. A treatment booking could reach a doctor the admin had not mapped to the
   treatment (typed name, department name, older list, Edit booking,
   "select another doctor").
4. Weekly summary: "generated successfully" over an empty card (see
   test_session21_real_postgres.py and the endpoint tests below).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import specialty_flow as sf
from app.services.conversation import ConversationManager, is_menu_request

PHONE = "+919999999999"
CLINIC = {"id": "clinic-1", "name": "Aura", "whatsapp_number": "+911111111111",
          "plan": "derma", "features": {}, "config": {}}
CONSENTED = {"language": "en", "data_consent": True}

NEWBORN = {"id": "t-newborn", "name": "Newborn Check-up", "category": "Paediatrics",
           "concerns": "feeding, weight, jaundice", "display_order": 2}
CHILD = {"id": "t-child", "name": "Child Consultation", "category": "Paediatrics",
         "concerns": "fever, cough, feeding", "display_order": 1}
VACCINE = {"id": "t-vacc", "name": "Child Vaccination", "category": "Paediatrics",
           "concerns": "vaccines, immunisation", "display_order": 3}
CATALOGUE = [CHILD, NEWBORN, VACCINE]


def _manager():
    m = ConversationManager()
    m.whatsapp = MagicMock()
    m.whatsapp.send_text = AsyncMock(return_value=True)
    m.whatsapp.send_interactive_list = AsyncMock(return_value=True)
    m.whatsapp.send_interactive_buttons = AsyncMock(return_value=True)
    m.update_state = AsyncMock()
    return m


def _lang():
    return patch("app.services.conversation.get_lang", new_callable=AsyncMock, return_value="en")


# ── 1. Menu requests and the abandoned language picker ──────────────────────


@pytest.mark.parametrize("typed", [
    "menu", "Main menu", "I need the menu", "show me the main menu please",
    "menu please", "go back to menu", "मुझे मेनू चाहिए", "మెనూ కావాలి",
])
def test_menu_requests_are_recognised(typed):
    assert is_menu_request(typed)


@pytest.mark.parametrize("typed", [
    "", "hi", "menu card for the canteen", "what is on the canteen menu",
    "book appointment", "change my language", "the doctor",
])
def test_other_messages_are_not_menu_requests(typed):
    assert not is_menu_request(typed)


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["I need the menu", "Main menu"])
async def test_menu_from_an_abandoned_language_picker_shows_the_menu(typed):
    """The screenshot: picker opened yesterday, never answered."""
    m = _manager()
    session = {"state": "selecting_language", "context": {}}
    with _lang(), \
         patch.object(m, "_send_main_menu", new_callable=AsyncMock) as menu, \
         patch.object(m, "_send_language_selection", new_callable=AsyncMock) as picker:
        await m._process_state(CLINIC, PHONE, typed, "unknown", session, CONSENTED, "en")
    picker.assert_not_called()
    menu.assert_awaited_once()
    assert m.update_state.call_args.args[2] == "main_menu"


@pytest.mark.asyncio
async def test_typed_language_on_the_picker_is_applied_for_a_consented_patient():
    m = _manager()
    session = {"state": "selecting_language", "context": {}}
    with _lang(), patch.object(m, "_handle_selecting_language", new_callable=AsyncMock) as apply:
        await m._process_state(CLINIC, PHONE, "Telugu", "unknown", session, CONSENTED, "en")
    assert apply.await_args.args[4] == {"id": "lang_te"}


@pytest.mark.asyncio
@pytest.mark.parametrize("patient", [
    {"language": None, "data_consent": None},     # first contact
    {"language": "en", "data_consent": None},     # consent not given yet
    {"language": "en", "data_consent": False},    # declined
])
async def test_picker_still_required_before_language_and_consent(patient):
    """First-time patients must still pick; the consent step follows it."""
    m = _manager()
    session = {"state": "selecting_language", "context": {}}
    with _lang(), \
         patch.object(m, "_send_main_menu", new_callable=AsyncMock) as menu, \
         patch.object(m, "_send_language_selection", new_callable=AsyncMock) as picker:
        await m._process_state(CLINIC, PHONE, "I need the menu", "unknown", session, patient, "en")
    menu.assert_not_called()
    picker.assert_awaited_once()


@pytest.mark.asyncio
async def test_change_language_typed_on_the_picker_keeps_the_picker():
    m = _manager()
    session = {"state": "selecting_language", "context": {}}
    with _lang(), \
         patch.object(m, "_send_main_menu", new_callable=AsyncMock) as menu, \
         patch.object(m, "_send_language_selection", new_callable=AsyncMock) as picker:
        await m._process_state(CLINIC, PHONE, "change my language", "change_language",
                               session, CONSENTED, "en")
    menu.assert_not_called()
    picker.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_text_on_an_abandoned_picker_is_handled_from_the_menu():
    m = _manager()
    session = {"state": "selecting_language", "context": {}}
    with _lang(), \
         patch.object(m, "_handle_main_menu", new_callable=AsyncMock) as main, \
         patch.object(m, "_send_language_selection", new_callable=AsyncMock) as picker, \
         patch.object(sf, "answer_named_treatment", AsyncMock(return_value=False)):
        await m._process_state(CLINIC, PHONE, "hello there", "unknown",
                               session, CONSENTED, "en")
    picker.assert_not_called()
    main.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_phrase_leaves_the_lab_search_instead_of_being_searched():
    m = _manager()
    with patch.object(m, "_send_main_menu", new_callable=AsyncMock) as menu, \
         patch.object(m, "_show_lab_test_list", new_callable=AsyncMock) as search:
        await m._handle_browsing_lab_tests(CLINIC, PHONE, "I need the menu", "view_services", {}, "en")
    search.assert_not_called()
    menu.assert_awaited_once()


# ── 2. "Do you provide newborn checkup" ─────────────────────────────────────


@pytest.mark.parametrize("typed,expected", [
    ("Do you provide new born checkup", "new born checkup"),
    ("What are your services", ""),
    ("What treatments you provide", ""),
    ("do you have newborn check up facility", "newborn check up"),
])
def test_service_question_terms(typed, expected):
    assert sf.service_question_terms(typed) == expected


@pytest.mark.parametrize("terms", ["new born checkup", "newborn check up", "newborn", "Newborn Check-up"])
def test_named_treatments_finds_newborn_checkup(terms):
    assert sf.named_treatments(CATALOGUE, terms)[0]["id"] == "t-newborn"


def test_named_treatments_is_strict():
    # Every word must be found: one stray word is not a match.
    assert sf.named_treatments(CATALOGUE, "newborn surgery") == []
    assert sf.named_treatments(CATALOGUE, "") == []
    # Concerns count for a services question, not for names_only.
    assert [t["id"] for t in sf.named_treatments(CATALOGUE, "fever")] == ["t-child"]
    assert sf.named_treatments(CATALOGUE, "fever", names_only=True) == []
    # Several named: title hits first, admin order kept.
    assert [t["id"] for t in sf.named_treatments(CATALOGUE, "child")] == ["t-child", "t-vacc"]


@pytest.mark.asyncio
async def test_newborn_question_shows_the_newborn_card():
    """The screenshot: asked in selecting_department, got the department list."""
    m = _manager()
    session = {"state": "selecting_department", "context": {}}
    with _lang(), \
         patch.object(sf, "treatment_menu_active", AsyncMock(return_value=True)), \
         patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=CATALOGUE)), \
         patch.object(sf, "show_treatment_card", AsyncMock()) as card, \
         patch.object(m, "_show_services", new_callable=AsyncMock) as services:
        await m._process_state(CLINIC, PHONE, "Do you provide new born checkup", "view_services",
                               session, CONSENTED, "en")
    services.assert_not_called()
    assert card.await_args.args[3] == "t-newborn"
    assert "Yes" in card.await_args.kwargs["intro"]


@pytest.mark.asyncio
async def test_several_named_treatments_are_listed():
    m = _manager()
    with patch.object(sf, "treatment_menu_active", AsyncMock(return_value=True)), \
         patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=CATALOGUE)):
        assert await sf.answer_named_treatment(m, CLINIC, PHONE, "do you treat children", "en") is False
        assert await sf.answer_named_treatment(m, CLINIC, PHONE, "child services", "en") is True
    rows = m.whatsapp.send_interactive_list.await_args.kwargs["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["trt_t-child", "trt_t-vacc"]


@pytest.mark.asyncio
async def test_generic_services_question_keeps_the_department_list():
    m = _manager()
    session = {"state": "main_menu", "context": {}}
    with _lang(), \
         patch.object(sf, "treatment_menu_active", AsyncMock(return_value=True)), \
         patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=CATALOGUE)) as fetch, \
         patch.object(m, "_is_diagnostics_only", new_callable=AsyncMock, return_value=False), \
         patch.object(m, "_show_services", new_callable=AsyncMock) as services:
        await m._process_state(CLINIC, PHONE, "What are your services", "view_services",
                               session, CONSENTED, "en")
    services.assert_awaited_once()
    fetch.assert_not_called()  # nothing named: no catalogue read at all


@pytest.mark.asyncio
async def test_symptom_booking_is_not_hijacked_by_a_concern_match():
    """"I have fever" matches Child Consultation's concerns, but a booking
    only yields to a treatment it NAMES."""
    m = _manager()
    with patch.object(sf, "treatment_menu_active", AsyncMock(return_value=True)), \
         patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=CATALOGUE)), \
         patch.object(sf, "show_treatment_card", AsyncMock()) as card:
        answered = await sf.answer_named_treatment(m, CLINIC, PHONE, "I have fever", "en", names_only=True)
    assert answered is False
    card.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["collecting_symptoms", "collecting_name", "selecting_slot"])
async def test_named_question_skipped_mid_booking_and_while_answering_our_question(state):
    m = _manager()
    with _lang(), \
         patch.object(sf, "answer_named_treatment", AsyncMock(return_value=True)) as named, \
         patch.object(m, "_handle_collecting_symptoms", new_callable=AsyncMock, create=True), \
         patch.object(m, "_handle_collecting_name", new_callable=AsyncMock, create=True), \
         patch.object(m, "_handle_selecting_slot", new_callable=AsyncMock, create=True):
        await m._process_state(CLINIC, PHONE, "newborn checkup", "unknown",
                               {"state": state, "context": {}}, CONSENTED, "en")
    named.assert_not_called()


@pytest.mark.asyncio
async def test_non_specialty_clinic_never_reads_the_catalogue():
    m = _manager()
    with patch.object(sf, "has_active_treatments", AsyncMock(return_value=True)) as active, \
         patch.object(sf, "get_specialty_treatments", AsyncMock()) as fetch:
        answered = await sf.answer_named_treatment(
            m, {"id": "c", "plan": "enterprise"}, PHONE, "newborn checkup", "en")
    assert answered is False
    active.assert_not_called()
    fetch.assert_not_called()


# ── 3. Treatment bookings only reach the mapped doctors ─────────────────────

RAO = {"id": "11111111-1111-1111-1111-111111111111", "name": "Dr. Rao", "department": "Paediatrics"}
SEN = {"id": "22222222-2222-2222-2222-222222222222", "name": "Dr. Sen", "department": "Paediatrics"}


class _Res:
    def __init__(self, data):
        self.data = data


@pytest.mark.asyncio
async def test_treatment_mapping_filters_doctors_and_defaults_to_all():
    with patch.object(sf, "get_doctors", AsyncMock(return_value=[RAO, SEN])), \
         patch.object(sf, "get_treatment_doctor_ids", AsyncMock(return_value={SEN["id"]})):
        assert [d["name"] for d in await sf._treatment_doctors("c", "t")] == ["Dr. Sen"]
    with patch.object(sf, "get_doctors", AsyncMock(return_value=[SEN, RAO])), \
         patch.object(sf, "get_treatment_doctor_ids", AsyncMock(return_value=set())):
        assert [d["name"] for d in await sf._treatment_doctors("c", "t")] == ["Dr. Rao", "Dr. Sen"]


@pytest.mark.asyncio
async def test_unmapped_doctor_from_an_older_list_is_refused_on_a_treatment_booking():
    m = _manager()
    context = {"treatment_id": "t-newborn"}
    with patch("app.services.conversation.sb", AsyncMock(return_value=_Res([RAO]))), \
         patch.object(sf, "_treatment_doctors", AsyncMock(return_value=[SEN])), \
         patch.object(sf, "show_treatment_doctors", AsyncMock()) as relist:
        await m._handle_selecting_doctor(CLINIC, PHONE, "Dr. Rao", "select_doctor", context, "en",
                                         {"id": f"doc_{RAO['id']}"})
    relist.assert_awaited_once()
    assert "doctor_id" not in context


@pytest.mark.asyncio
async def test_typed_department_does_not_reopen_the_department_on_a_treatment_booking():
    m = _manager()
    context = {"treatment_id": "t-newborn"}
    with patch("app.services.conversation.sb", AsyncMock(side_effect=[
            _Res([{"department": "Paediatrics"}]), _Res([RAO, SEN])])), \
         patch.object(sf, "_treatment_doctors", AsyncMock(return_value=[SEN])), \
         patch.object(sf, "show_treatment_doctors", AsyncMock()) as relist, \
         patch.object(m, "_show_doctor_list", new_callable=AsyncMock) as dept_list:
        await m._handle_selecting_doctor(CLINIC, PHONE, "paediatrics", "unknown", context, "en")
    dept_list.assert_not_called()
    relist.assert_awaited_once()


@pytest.mark.asyncio
async def test_mapped_doctor_is_accepted_on_a_treatment_booking():
    m = _manager()
    context = {"treatment_id": "t-newborn"}
    with patch("app.services.conversation.sb", AsyncMock(return_value=_Res([SEN]))), \
         patch.object(sf, "_treatment_doctors", AsyncMock(return_value=[SEN])), \
         patch.object(sf, "show_treatment_doctors", AsyncMock()) as relist:
        try:
            await m._handle_selecting_doctor(CLINIC, PHONE, "Dr. Sen", "select_doctor", context, "en",
                                             {"id": f"doc_{SEN['id']}"})
        except Exception:
            pass  # the steps after choosing a doctor are not under test here
    relist.assert_not_called()
    assert context["doctor_id"] == SEN["id"]


@pytest.mark.asyncio
async def test_suggestion_row_id_is_not_queried_as_a_uuid():
    """"Select another doctor" rows are doc_{i}_{name}: that id used to be
    sent to the database as a UUID. It is now matched by the name."""
    m = _manager()
    context = {"department": "Paediatrics"}
    queries = []

    async def fake_sb(q):
        queries.append(q)
        return _Res([{"department": "Paediatrics"}]) if len(queries) == 1 else _Res([RAO, SEN])

    with patch("app.services.conversation.sb", side_effect=fake_sb):
        try:
            await m._handle_selecting_doctor(CLINIC, PHONE, "Dr. Sen", "select_doctor", context, "en",
                                             {"id": "doc_1_Dr. Sen"})
        except Exception:
            pass
    assert context.get("doctor_name") == "Dr. Sen"


@pytest.mark.asyncio
async def test_edit_booking_on_a_treatment_booking_lists_treatment_doctors():
    m = _manager()
    with patch.object(sf, "show_treatment_doctors", AsyncMock()) as relist, \
         patch.object(m, "_show_doctor_list", new_callable=AsyncMock) as dept_list:
        await m._handle_confirming_booking(CLINIC, PHONE, "edit", "edit_booking",
                                           {"treatment_id": "t-newborn", "department": "Paediatrics"},
                                           CONSENTED, "en")
    relist.assert_awaited_once()
    dept_list.assert_not_called()


@pytest.mark.asyncio
async def test_other_doctor_suggestions_stay_within_the_treatment():
    m = _manager()
    context = {"treatment_id": "t-newborn", "department": "Paediatrics", "doctor_name": "Dr. Sen"}
    with patch.object(sf, "_treatment_doctors", AsyncMock(return_value=[SEN])) as mapped, \
         patch("app.services.conversation.get_doctors", AsyncMock(return_value=[RAO, SEN])) as dept, \
         patch("app.services.conversation.get_available_slots", AsyncMock(return_value=(["10:00"], None))), \
         patch.object(m, "_send_main_menu", new_callable=AsyncMock):
        await m._suggest_other_doctors(CLINIC, PHONE, context, "en")
    mapped.assert_awaited_once()
    dept.assert_not_called()
    m.whatsapp.send_interactive_list.assert_not_called()  # only Sen, who is excluded
