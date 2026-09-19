"""conversation.py wiring: existing tenants see identical behaviour; specialty
clinics get the treatment flow; an abandoned treatment never tags a booking."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import specialty_flow
from app.services.conversation import ConversationManager, ConversationState
import sys
import app.database as _real_app_db

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "app" / "services" / "conversation.py").read_text(encoding="utf-8")
PHONE = "+919000000001"
T1 = "11111111-1111-1111-1111-111111111111"


def _manager():
    m = ConversationManager()
    m.whatsapp = MagicMock()
    m.whatsapp.send_text = AsyncMock()
    m.whatsapp.send_interactive_list = AsyncMock()
    m.whatsapp.send_interactive_buttons = AsyncMock()
    return m


def _menu_ids(m):
    call = m.whatsapp.send_interactive_list.await_args
    return [r["id"] for s in call.kwargs["sections"] for r in s["rows"]]


def test_states_exist():
    assert ConversationState.BROWSING_TREATMENTS == "browsing_treatments"
    assert ConversationState.SEARCHING_TREATMENTS == "searching_treatments"


# ── main menu ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["soloclinic", "essential", "polyclinic", "enterprise"])
async def test_existing_plans_keep_the_exact_same_menu(plan):
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.has_feature", return_value=False), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=True)) as active:
        await m._send_main_menu({"id": "c1", "plan": plan}, PHONE, "en")
    assert _menu_ids(m) == ["menu_book", "menu_services", "menu_doctors", "menu_emergency", "menu_human", "menu_help"]
    active.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_plan_with_lab_booking_keeps_its_lab_row():
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.has_feature", side_effect=lambda c, f: f == "lab_test_booking"):
        await m._send_main_menu({"id": "c1", "plan": "polyclinic"}, PHONE, "en")
    assert _menu_ids(m) == ["menu_book", "menu_services", "menu_doctors", "menu_lab_tests",
                            "menu_emergency", "menu_human", "menu_help"]


@pytest.mark.asyncio
async def test_specialty_clinic_with_treatments_gets_treatment_rows_and_no_services_row():
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.has_feature", return_value=False), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=True)):
        await m._send_main_menu({"id": "c1", "plan": "dental", "features": {}}, PHONE, "en")
    assert _menu_ids(m) == ["menu_treatments", "menu_concern", "menu_book", "menu_doctors",
                            "menu_emergency", "menu_human", "menu_help"]


@pytest.mark.asyncio
async def test_specialty_clinic_without_published_treatments_keeps_the_normal_menu():
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.has_feature", return_value=False), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=False)):
        await m._send_main_menu({"id": "c1", "plan": "eye", "features": {}}, PHONE, "en")
    assert _menu_ids(m) == ["menu_book", "menu_services", "menu_doctors", "menu_emergency", "menu_human", "menu_help"]


@pytest.mark.asyncio
async def test_override_enabled_general_clinic_keeps_services_alongside_treatments():
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.has_feature", return_value=False), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=True)):
        await m._send_main_menu({"id": "c1", "plan": "polyclinic", "features": {"specialty_treatments": True}}, PHONE, "en")
    assert _menu_ids(m) == ["menu_treatments", "menu_concern", "menu_book", "menu_services", "menu_doctors",
                            "menu_emergency", "menu_human", "menu_help"]


# ── context hygiene ──────────────────────────────────────────────────────────

async def _run_update_state(new_state):
    m = _manager()
    existing = MagicMock(data=[{"context": {"treatment_id": T1, "treatment_name": "IVF", "branch_id": "b1"},
                                "state": "selecting_doctor"}])
    fake_supabase = MagicMock()
    with patch("app.database.supabase", fake_supabase), \
         patch("app.services.conversation.sb", AsyncMock(side_effect=[existing, MagicMock(data=[])])):
        await m.update_state({"id": "c1"}, PHONE, new_state, {"x": 1})
    return fake_supabase.table.return_value.update.call_args.args[0]["context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["main_menu", "idle", "selecting_department", "suggesting_department",
                                   "collecting_symptoms"])
async def test_leaving_the_treatment_flow_drops_the_tag(state):
    ctx = await _run_update_state(state)
    assert "treatment_id" not in ctx and "treatment_name" not in ctx
    assert ctx["branch_id"] == "b1" and ctx["x"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["selecting_doctor", "selecting_date", "selecting_slot", "confirming_booking",
                                   "selecting_branch", "collecting_name", "awaiting_payment"])
async def test_booking_steps_keep_the_tag(state):
    ctx = await _run_update_state(state)
    assert ctx["treatment_id"] == T1


# ── booking seed and the symptom step ────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("seed, expected", [(None, {}), ({"treatment_id": T1, "treatment_name": "IVF"},
                                                          {"treatment_id": T1, "treatment_name": "IVF"})])
async def test_start_booking_seed_is_carried_or_empty(seed, expected):
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.get_clinic_branches", AsyncMock(return_value=[])), \
         patch("app.services.tenant.has_branches", return_value=False), \
         patch.object(m, "update_state", AsyncMock()) as upd, \
         patch.object(m, "_continue_booking_after_branch", AsyncMock()) as cont:
        await m._start_booking({"id": "c1"}, PHONE, {"language": "en", "name": "Asha Rao"}, "en", seed_context=seed)
    assert upd.await_args.args[2] == "selecting_family_member"
    assert upd.await_args.args[3] == expected and upd.await_args.kwargs["reset_context"] is True
    assert cont.await_args.args[4] == expected


@pytest.mark.asyncio
async def test_name_step_routes_treatment_bookings_to_specialists():
    m = _manager()
    ctx = {"treatment_id": T1, "treatment_name": "Hair PRP Therapy", "for_self": True}
    with patch("app.services.conversation.update_patient", AsyncMock()), \
         patch.object(specialty_flow, "show_treatment_doctors", AsyncMock()) as show, \
         patch.object(m, "update_state", AsyncMock()) as upd:
        await m._handle_collecting_name({"id": "c1"}, PHONE, "Asha Rao", ctx, {"name": None}, "en")
    show.assert_awaited_once()
    m.whatsapp.send_text.assert_not_awaited()
    assert all(c.args[2] != "collecting_symptoms" for c in upd.await_args_list)


@pytest.mark.asyncio
async def test_name_step_without_treatment_still_asks_for_symptoms():
    m = _manager()
    with patch("app.services.conversation.update_patient", AsyncMock()), \
         patch.object(specialty_flow, "show_treatment_doctors", AsyncMock()) as show, \
         patch.object(m, "update_state", AsyncMock()) as upd:
        await m._handle_collecting_name({"id": "c1"}, PHONE, "Asha Rao", {"for_self": True}, {"name": None}, "en")
    show.assert_not_awaited()
    m.whatsapp.send_text.assert_awaited_once()
    assert upd.await_args.args[2] == "collecting_symptoms"


@pytest.mark.asyncio
async def test_family_member_pick_routes_treatment_bookings():
    m = _manager()
    ctx = {"treatment_id": T1, "family_members": [{"full_name": "Ravi Rao", "relationship": "spouse"}]}
    with patch.object(specialty_flow, "show_treatment_doctors", AsyncMock()) as show, \
         patch.object(m, "update_state", AsyncMock()) as upd:
        await m._handle_selecting_family_member({"id": "c1"}, PHONE, "fam_0", ctx, "en", {"name": "Asha"})
    show.assert_awaited_once()
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_more_doctors_in_treatment_flow_pages_the_treatment_list():
    m = _manager()
    ctx = {"treatment_id": T1, "doctor_page": 0}
    with patch.object(specialty_flow, "show_treatment_doctors", AsyncMock()) as show, \
         patch.object(m, "_show_doctor_list", AsyncMock()) as dept_list:
        await m._handle_selecting_doctor({"id": "c1"}, PHONE, "More options", "button_click", ctx, "en",
                                         {"id": "doc_more"})
    assert show.await_args.kwargs["page"] == 1
    dept_list.assert_not_awaited()


# ── hooks present exactly where required ─────────────────────────────────────

def test_routing_hooks_are_wired():
    assert "from app.services import specialty_flow" in SRC
    assert "specialty_flow.handle_treatment_button(" in SRC
    assert "specialty_flow.offer_treatment_browse(" in SRC
    assert "specialty_flow.handle_treatment_search_text(" in SRC
    assert "specialty_flow.handle_treatment_state(" in SRC
    assert "specialty_flow.TREATMENT_RESET_STATES" in SRC
    # The typed-name paths share _continue_after_patient_name (which also
    # follows the save-family prompt), so two former call sites are one.
    assert SRC.count("specialty_flow.route_to_treatment_doctors(") == 6
    assert "await self._continue_after_patient_name(" in SRC
    view_doctor_block = SRC.split('if intent == "view_doctor":')[1][:3500]
    assert "specialty_flow.clear_treatment_context(context)" in view_doctor_block


def test_no_new_booking_type_is_introduced():
    assert "treatment_procedure" not in SRC
