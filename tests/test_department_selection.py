"""Tests for WhatsApp department-selection button routing."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_selecting_dynamic_department_resolves_correct_name():
    """Regression test: dept_* button IDs are generated from a clinic's real
    department names (up to 37 specialties), not the ~8 entries in the fixed
    DEPT_MAP. Selecting a department outside that fixed set must resolve to
    its real name (via context['dept_options']), not silently fall back to
    'General Medicine'."""
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
    context = {"dept_options": {"dept_neurology": "Neurology"}}

    mock_doctors_result = MagicMock()
    mock_doctors_result.data = []

    with patch("app.database.supabase") as mock_sb, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send, patch.object(
        manager, "_show_department_list", new_callable=AsyncMock
    ):
        mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value = (
            mock_doctors_result
        )

        await manager._handle_selecting_department(
            clinic,
            "+919876543210",
            "",
            "select_department",
            context,
            "en",
            interactive_data={"id": "dept_neurology"},
        )

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][2]
        assert "Neurology" in sent_text
        assert "General Medicine" not in sent_text


@pytest.mark.asyncio
async def test_selecting_fixed_service_menu_department_still_works():
    """svc_* ids come from the fixed 8-item quick-service menu and should
    keep resolving via DEPT_MAP even without dept_options in context."""
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
    context = {}

    mock_doctors_result = MagicMock()
    mock_doctors_result.data = []

    with patch("app.database.supabase") as mock_sb, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send, patch.object(
        manager, "_show_department_list", new_callable=AsyncMock
    ):
        mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value = (
            mock_doctors_result
        )

        await manager._handle_selecting_department(
            clinic,
            "+919876543210",
            "",
            "select_service",
            context,
            "en",
            interactive_data={"id": "svc_cardiology"},
        )

        sent_text = mock_send.call_args[0][2]
        assert "Cardiology" in sent_text


@pytest.mark.asyncio
async def test_no_doctors_single_department_clinic_does_not_loop():
    """Regression test: a clinic without multi_department (e.g. a
    diagnostics-only center with zero doctors) has exactly one department to
    offer. If that department has no doctors, retrying department selection
    calls straight back into the same no-doctors branch forever (this is
    the crash seen in production — CancelledError from an endless
    _show_department_list <-> _show_doctor_list recursion). It must fall
    back to the main menu instead of retrying."""
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
    context = {}

    with patch(
        "app.services.conversation.get_doctors", new_callable=AsyncMock
    ) as mock_get_doctors, patch(
        "app.services.tenant.has_feature", return_value=False
    ), patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ), patch.object(
        manager, "_show_department_list", new_callable=AsyncMock
    ) as mock_show_dept_list, patch.object(
        manager, "_send_main_menu", new_callable=AsyncMock
    ) as mock_main_menu, patch.object(
        manager, "update_state", new_callable=AsyncMock
    ):
        mock_get_doctors.return_value = []

        await manager._show_doctor_list(
            clinic, "+919876543210", "General Medicine", context, "en"
        )

        mock_main_menu.assert_called_once()
        mock_show_dept_list.assert_not_called()
