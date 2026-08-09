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
