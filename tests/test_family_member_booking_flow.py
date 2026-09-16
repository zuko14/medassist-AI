"""Tests for booking for a family member / dependent."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager


@pytest.mark.asyncio
async def test_booking_shows_family_options_when_saved_members_exist():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
    patient = {"id": "p-1", "name": "Ramesh Sharma", "phone": "+919876543210", "language": "en"}

    saved_family = [
        {"id": "fam-1", "full_name": "Priya Sharma", "relationship": "Daughter"},
    ]

    with patch(
        "app.services.conversation.get_family_members",
        new_callable=AsyncMock,
        return_value=saved_family,
    ), patch.object(
        manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as mock_list:
        await manager._start_booking(clinic, "+919876543210", patient, "en")

        mock_list.assert_called_once()
        rows = mock_list.call_args[1]["sections"][0]["rows"]
        row_ids = [r["id"] for r in rows]
        assert "fam_self" in row_ids
        assert "fam_new" in row_ids
        assert "fam_0" in row_ids


@pytest.mark.asyncio
async def test_booking_lists_all_saved_family_members_not_just_first():
    """Regression test: previously only the first saved family member was
    ever shown, because the button UI hard-capped the list to saved_family[:1]."""
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
    patient = {"id": "p-1", "name": "Ramesh Sharma", "phone": "+919876543210", "language": "en"}

    saved_family = [
        {"id": "fam-1", "full_name": "Priya Sharma", "relationship": "Daughter"},
        {"id": "fam-2", "full_name": "Suresh Sharma", "relationship": "Son"},
        {"id": "fam-3", "full_name": "Lakshmi Sharma", "relationship": "Wife"},
    ]

    with patch(
        "app.services.conversation.get_family_members",
        new_callable=AsyncMock,
        return_value=saved_family,
    ), patch.object(
        manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as mock_list:
        await manager._start_booking(clinic, "+919876543210", patient, "en")

        rows = mock_list.call_args[1]["sections"][0]["rows"]
        row_titles = [r["title"] for r in rows]
        assert "Priya Sharma" in row_titles
        assert "Suresh Sharma" in row_titles
        assert "Lakshmi Sharma" in row_titles
        row_ids = [r["id"] for r in rows]
        assert "fam_0" in row_ids
        assert "fam_1" in row_ids
        assert "fam_2" in row_ids


@pytest.mark.asyncio
async def test_selecting_family_member_sets_patient_name_in_context():
    manager = ConversationManager()
    clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
    context = {
        "family_members": [{"full_name": "Priya Sharma", "relationship": "Daughter"}]
    }

    with patch.object(
        manager, "update_state", new_callable=AsyncMock
    ) as mock_update, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ):
        await manager._handle_selecting_family_member(
            clinic, "+919876543210", "1", context, "en"
        )

        mock_update.assert_called_once()
        new_ctx = mock_update.call_args[0][3]
        assert new_ctx["patient_name"] == "Priya Sharma"
        assert mock_update.call_args[0][2] in ("collecting_symptoms", "asking_symptoms")


# ── The booking must carry the name of the person it is for ──────────────────
# Regression: the "who is this for" list stored the chosen person only as
# context["patient_name"], but the confirmation screen and both booking writers
# read context["booking_name"], so every "For Me" / saved-family booking was
# saved and confirmed as "Patient".

from unittest.mock import MagicMock

from app.services.conversation import resolve_booking_name

CLINIC = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
PHONE = "+919876543210"


async def _pick(message, context, patient):
    manager = ConversationManager()
    with patch.object(manager, "update_state", new_callable=AsyncMock) as upd, \
         patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock) as txt:
        await manager._handle_selecting_family_member(CLINIC, PHONE, message, context, "en", patient)
    return upd, txt


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["fam_0", "1", "priya sharma"])
async def test_saved_family_member_becomes_the_booking_name(message):
    ctx = {"family_members": [{"full_name": "Priya Sharma", "relationship": "Daughter"}]}
    upd, _ = await _pick(message, ctx, {"name": "Ramesh Sharma"})
    new_ctx = upd.call_args[0][3]
    assert new_ctx["booking_name"] == "Priya Sharma"


@pytest.mark.asyncio
async def test_typed_new_family_name_becomes_the_booking_name():
    upd, _ = await _pick("Anil Kumar Sharma", {"family_members": []}, {"name": "Ramesh Sharma"})
    assert upd.call_args[0][3]["booking_name"] == "Anil Kumar Sharma"


@pytest.mark.asyncio
async def test_for_me_uses_the_account_holders_name():
    upd, _ = await _pick("fam_self", {"family_members": []}, {"name": "Ramesh Sharma"})
    new_ctx = upd.call_args[0][3]
    assert new_ctx["booking_name"] == "Ramesh Sharma"
    assert upd.call_args[0][2] == "collecting_symptoms"


@pytest.mark.asyncio
async def test_for_me_without_a_saved_name_asks_for_it_instead_of_booking_as_there():
    upd, txt = await _pick("fam_self", {"family_members": []}, {"name": None})
    assert upd.call_args[0][2] == "collecting_name"
    assert upd.call_args[0][3].get("booking_name") in (None, "")
    txt.assert_awaited_once()


@pytest.mark.asyncio
async def test_naming_a_new_family_member_never_renames_the_account_holder():
    manager = ConversationManager()
    ctx = {"is_family": True}  # what "+ Someone Else" leaves in the context
    with patch("app.services.conversation.update_patient", new_callable=AsyncMock) as rename, \
         patch.object(manager, "update_state", new_callable=AsyncMock) as upd, \
         patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock):
        await manager._handle_collecting_name(CLINIC, PHONE, "Anil Kumar", ctx, {"name": "Ramesh Sharma"}, "en")
    rename.assert_not_awaited()
    assert upd.call_args[0][3]["booking_name"] == "Anil Kumar"


@pytest.mark.asyncio
async def test_naming_yourself_still_saves_your_name():
    manager = ConversationManager()
    with patch("app.services.conversation.update_patient", new_callable=AsyncMock) as rename, \
         patch.object(manager, "update_state", new_callable=AsyncMock), \
         patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock):
        await manager._handle_collecting_name(CLINIC, PHONE, "Ramesh Sharma", {"for_self": True}, {"name": None}, "en")
    rename.assert_awaited_once_with("clinic-1", PHONE, {"name": "Ramesh Sharma"})


@pytest.mark.parametrize("context, patient, expected", [
    ({"booking_name": "Priya Sharma", "patient_name": "Other"}, {"name": "Ramesh"}, "Priya Sharma"),
    # Sessions already in flight when this fix deploys carry only patient_name.
    ({"patient_name": "Priya Sharma"}, {"name": "Ramesh"}, "Priya Sharma"),
    ({"patient_name": "there"}, {"name": "Ramesh Sharma"}, "Ramesh Sharma"),
    ({"booking_name": "  "}, {"name": "Ramesh Sharma"}, "Ramesh Sharma"),
    ({}, None, "Patient"),
    ({"booking_name": None}, {"name": None}, "Patient"),
])
def test_resolve_booking_name(context, patient, expected):
    assert resolve_booking_name(context, patient) == expected


def _confirm_ctx(**extra):
    return {"doctor_name": "Dr. Mehta", "doctor_id": "11111111-1111-1111-1111-111111111111",
            "department": "Dermatology", "appointment_date": "2026-10-01",
            "appointment_time": "10:00", **extra}


@pytest.mark.asyncio
async def test_confirmation_screen_shows_the_family_members_name():
    manager = ConversationManager()
    with patch.object(manager, "update_state", new_callable=AsyncMock), \
         patch.object(manager.whatsapp, "send_interactive_buttons", new_callable=AsyncMock) as btns:
        await manager._show_booking_confirmation(CLINIC, PHONE, _confirm_ctx(patient_name="Priya Sharma"), "en")
    body = btns.call_args.kwargs["body"]
    assert "Priya Sharma" in body and "Patient" not in body


@pytest.mark.asyncio
async def test_unpaid_booking_is_saved_under_the_family_members_name():
    manager = ConversationManager()
    booked = AsyncMock(return_value={"success": False, "reason": "error"})
    with patch("app.services.payment.resolve_payment_mode", return_value=("none", 100)), \
         patch("app.database.get_doctor_by_name", AsyncMock(return_value={"is_active": True})), \
         patch("app.services.conversation.book_appointment", booked), \
         patch.object(manager, "update_state", new_callable=AsyncMock), \
         patch.object(manager, "_send_main_menu", new_callable=AsyncMock), \
         patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock):
        await manager._handle_confirming_booking(
            CLINIC, PHONE, "Confirm", "confirm_booking",
            _confirm_ctx(booking_name="Priya Sharma"), {"id": "p-1", "name": "Ramesh Sharma"}, "en")
    assert booked.call_args.args[1]["patient_name"] == "Priya Sharma"


@pytest.mark.asyncio
async def test_paid_booking_is_saved_under_the_family_members_name():
    manager = ConversationManager()
    create = AsyncMock(return_value={"success": False, "reason": "error"})
    payment_service = MagicMock(create_booking_with_payment=create)
    with patch("app.services.payment.resolve_payment_mode", return_value=("full", 100)), \
         patch("app.services.payment.payment_service", payment_service), \
         patch("app.database.get_doctor_by_name", AsyncMock(return_value={"is_active": True})), \
         patch.object(manager, "update_state", new_callable=AsyncMock), \
         patch.object(manager, "_send_main_menu", new_callable=AsyncMock), \
         patch.object(manager.whatsapp, "send_text", new_callable=AsyncMock):
        await manager._handle_confirming_booking(
            CLINIC, PHONE, "Confirm", "confirm_booking",
            _confirm_ctx(patient_name="Priya Sharma"), {"id": "p-1", "name": "Ramesh Sharma"}, "en")
    assert create.call_args.kwargs["patient_name"] == "Priya Sharma"
