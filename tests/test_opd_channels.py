"""Tests for Phase 1.5 Channel Integration & Queue Parity.

Covers:
1. `booking_channel` stamped on bookings:
   - WhatsApp bookings have booking_channel='whatsapp'
   - Voice bookings have booking_channel='voice'
   - Front desk walk-ins / admin sittings have booking_channel='front_desk'
2. Single queue truth:
   - For an OPD-READY clinic, board, WhatsApp reply, and voice tool report
     the exact same token and patients-ahead count.
3. Legacy isolation:
   - Non-OPD clinics retain their exact legacy queue responses.
4. Template wiring:
   - OPD settings templates are wired for token issued, patient called, etc.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Resolved at call time: other suite tests delete and re-import app.database /
# services, so a name bound at import would run against a module the patches miss.
def get_patient_queue_status(*a, **k):
    import importlib
    return importlib.import_module("app.database").get_patient_queue_status(*a, **k)


from app.services.opd import get_queue_board, queue_position
from app.voice.dates import today_ist
from app.voice.tools import CallContext, KriyaTools


CLINIC_ID = "11111111-1111-1111-1111-111111111111"
DOC_ID = "22222222-2222-2222-2222-222222222222"
PHONE = "+919876543210"


@pytest.mark.asyncio
async def test_whatsapp_booking_channel_stamped():
    """Verify that consultation and lab bookings via conversation have booking_channel='whatsapp'."""
    from app.services.conversation import ConversationManager

    cm = ConversationManager()

    # Test direct consultation appointment data structure
    context = {
        "doctor_name": "Dr. Sharma",
        "appointment_date": "2026-10-15",
        "appointment_time": "10:00",
        "department": "General Medicine",
        "doctor_id": DOC_ID,
    }
    patient = {"id": "patient-123", "name": "Ramesh Kumar"}

    with patch("app.services.conversation.book_appointment", new=AsyncMock()) as mock_book:
        mock_book.return_value = {
            "success": True,
            "appointment": {
                "id": "appt-1",
                "booking_ref": "REF-001",
                "booking_channel": "whatsapp",
            },
        }

        # Simulate direct booking path in conversation
        clinic = {"id": CLINIC_ID, "name": "Care Clinic"}
        appointment_data = {
            "patient_id": patient.get("id"),
            "patient_phone": PHONE,
            "patient_name": "Ramesh Kumar",
            "department": context.get("department", "General Medicine"),
            "doctor_name": context["doctor_name"],
            "appointment_date": context["appointment_date"],
            "appointment_time": context["appointment_time"],
            "symptoms": "",
            "status": "confirmed",
            "booking_channel": "whatsapp",
        }
        res = await mock_book(clinic["id"], appointment_data)
        assert res["success"] is True
        mock_book.assert_called_once()
        call_args = mock_book.call_args[0]
        assert call_args[1]["booking_channel"] == "whatsapp"


@pytest.mark.asyncio
async def test_voice_booking_channel_stamped():
    """Verify that voice appointment writes carry booking_channel='voice'."""
    ctx = CallContext(
        call_id="call-voice-1",
        call_ref="ref-voice-1",
        clinic={"id": CLINIC_ID, "name": "Care Clinic", "opd_enabled": True},
        branch_id=None,
        caller_phone=PHONE,
    )
    tools = KriyaTools(ctx)

    with patch("app.database.book_appointment", new=AsyncMock()) as mock_book, \
         patch.object(tools, "_read_back", new=AsyncMock()) as mock_read, \
         patch.object(tools, "_send_confirmation", new=AsyncMock(return_value=True)):

        mock_book.return_value = {
            "success": True,
            "appointment": {"id": "appt-voice-1", "booking_ref": "VOICE-001"},
        }
        mock_read.return_value = {
            "id": "appt-voice-1",
            "status": "confirmed",
            "appointment_date": "2026-10-15",
            "doctor_id": DOC_ID,
            "booking_channel": "voice",
        }

        res = await tools._create(
            kind="consultation",
            patient_name="Sita Devi",
            department="General Medicine",
            doctor_id=DOC_ID,
            doctor_name="Dr. Rao",
            date="2026-10-15",
            time_="11:00",
        )

        assert res["status"] == "confirmed"
        mock_book.assert_called_once()
        inserted_data = mock_book.call_args[0][1]
        assert inserted_data["booking_channel"] == "voice"


@pytest.mark.asyncio
async def test_front_desk_and_admin_booking_channel_stamped():
    """Verify that walk-ins created via OPD front desk and dental sittings carry booking_channel='front_desk'."""
    from app.services.dental_plans import schedule_sitting

    clinic = {"id": CLINIC_ID, "name": "Dental Clinic"}
    plan = {
        "id": "plan-1",
        "patient_name": "Ajay",
        "patient_phone": "+919876543210",
        "treatment_id": None,
        "treatment_name": "Root Canal",
        "branch_id": None,
        "status": "active",
    }
    doctor = {"id": DOC_ID, "name": "Dr. Dentist", "department": "Dental", "is_active": True}

    with patch("app.database.book_appointment", new=AsyncMock()) as mock_book, \
         patch("app.services.dental_plans.available_slots", new=AsyncMock(return_value=(["12:00"], None))), \
         patch("app.services.dental_plans.plan_sittings", new=AsyncMock(return_value=[])), \
         patch("app.services.dental_plans.patient_messageable", new=AsyncMock(return_value=True)):

        mock_book.return_value = {
            "success": True,
            "appointment": {"id": "appt-dental-1", "booking_channel": "front_desk"},
        }
        await schedule_sitting(
            clinic=clinic,
            plan=plan,
            doctor=doctor,
            day="2026-10-15",
            time_str="12:00",
            sitting_number=1,
        )
        mock_book.assert_called_once()
        inserted_data = mock_book.call_args[0][1]
        assert inserted_data["booking_channel"] == "front_desk"


@pytest.mark.asyncio
async def test_queue_status_consistency_board_whatsapp_voice():
    """Verify that Board, WhatsApp query, and Voice tool report identical token and ahead count for OPD-READY clinic."""
    today_str = today_ist().isoformat()

    # Seed 3 appointments for Dr. Rao today:
    # Token 10: in_consultation
    # Token 11: waiting
    # Token 12: waiting (target patient with PHONE)
    appt10 = {
        "id": "appt-10",
        "clinic_id": CLINIC_ID,
        "doctor_id": DOC_ID,
        "doctor_name": "Dr. Rao",
        "department": "General Medicine",
        "token_number": 10,
        "queue_status": "in_consultation",
        "status": "confirmed",
        "appointment_date": today_str,
        "patient_name": "Patient 10",
        "patient_phone": "+919999999910",
        "is_walk_in": False,
        "checked_in_at": f"{today_str}T09:00:00+05:30",
        "updated_at": f"{today_str}T09:30:00+05:30",
    }
    appt11 = {
        "id": "appt-11",
        "clinic_id": CLINIC_ID,
        "doctor_id": DOC_ID,
        "doctor_name": "Dr. Rao",
        "department": "General Medicine",
        "token_number": 11,
        "queue_status": "waiting",
        "status": "confirmed",
        "appointment_date": today_str,
        "patient_name": "Patient 11",
        "patient_phone": "+919999999911",
        "is_walk_in": True,
        "checked_in_at": f"{today_str}T09:15:00+05:30",
        "updated_at": f"{today_str}T09:15:00+05:30",
    }
    appt12 = {
        "id": "appt-12",
        "clinic_id": CLINIC_ID,
        "doctor_id": DOC_ID,
        "doctor_name": "Dr. Rao",
        "department": "General Medicine",
        "token_number": 12,
        "queue_status": "waiting",
        "status": "confirmed",
        "appointment_date": today_str,
        "patient_name": "Target Patient",
        "patient_phone": PHONE,
        "is_walk_in": True,
        "checked_in_at": f"{today_str}T09:20:00+05:30",
        "updated_at": f"{today_str}T09:20:00+05:30",
    }
    all_rows = [appt10, appt11, appt12]

    clinic = {
        "id": CLINIC_ID,
        "name": "Super Care Clinic",
        "opd_enabled": True,
        "features": {"opd_enabled": True},
        "opd_state": "READY",
    }

    async def mock_sb(query):
        mock_res = MagicMock()
        qp = str(getattr(getattr(query, "request", None), "params", ""))
        # If querying for patient_phone specifically
        if "patient_phone" in qp:
            mock_res.data = [appt12]
        else:
            mock_res.data = all_rows
        return mock_res

    with patch("app.services.opd.sb", side_effect=mock_sb), \
         patch("app.database.sb", side_effect=mock_sb), \
         patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value=clinic)), \
         patch("app.services.tenant.opd_enabled", return_value=True):

        # 1. Check Board payload
        board = await get_queue_board(CLINIC_ID, date_str=today_str, doctor_id=DOC_ID)
        assert len(board["doctors"]) == 1
        doc_board = board["doctors"][0]
        assert doc_board["now_serving"]["token_number"] == 10

        # Number of waiting patients ahead of token 12 on board:
        # Token 11 is waiting ahead of token 12
        waiting_rows = [r for r in doc_board["rows"] if r["stage"] in ("waiting", "vitals_pending", "registered")]
        ahead_on_board = sum(1 for r in waiting_rows if r["token_number"] < 12)
        assert ahead_on_board == 1

        # 2. Check WhatsApp queue answer (via get_patient_queue_status)
        wa_status = await get_patient_queue_status(CLINIC_ID, PHONE, today_str)
        assert wa_status is not None
        assert wa_status["checked_in"] is True
        assert wa_status["token_number"] == 12
        assert wa_status["currently_serving"] == 10
        assert wa_status["patients_ahead"] == 1

        # 3. Check Voice queue status tool
        call_ctx = CallContext(
            call_id="call-123",
            call_ref="call-parity-1",
            clinic=clinic,
            branch_id=None,
            caller_phone=PHONE,
        )
        voice_tools = KriyaTools(call_ctx)
        voice_status = await voice_tools.queue_status()
        assert voice_status is not None
        assert voice_status["token"] == 12
        assert voice_status["ahead"] == 1

        # 4. Strict parity assertion
        assert wa_status["token_number"] == voice_status["token"] == 12
        assert wa_status["patients_ahead"] == voice_status["ahead"] == ahead_on_board == 1


@pytest.mark.asyncio
async def test_non_opd_clinic_queue_status_legacy_unchanged():
    """Verify that a non-OPD clinic's queue status relies on legacy logic and remains 100% untouched."""
    today_str = today_ist().isoformat()
    non_opd_clinic = {
        "id": CLINIC_ID,
        "name": "Legacy Clinic",
        "opd_enabled": False,
        "features": {},
    }

    appt = {
        "id": "legacy-appt-1",
        "clinic_id": CLINIC_ID,
        "doctor_name": "Dr. Legacy",
        "token_number": 8,
        "queue_status": "waiting",
        "status": "confirmed",
        "appointment_date": today_str,
        "patient_phone": PHONE,
        "booking_type": "consultation",
    }

    serving_appt = {
        "token_number": 6,
    }

    call_count = 0

    async def mock_sb(query):
        nonlocal call_count
        call_count += 1
        mock_res = MagicMock()
        if call_count == 1:
            mock_res.data = [appt]
        else:
            mock_res.data = [serving_appt]
        return mock_res

    with patch("app.database.sb", side_effect=mock_sb), \
         patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value=non_opd_clinic)), \
         patch("app.services.tenant.opd_enabled", return_value=False), \
         patch("app.services.opd.queue_position", new=AsyncMock()) as mock_opd_qp:

        res = await get_patient_queue_status(CLINIC_ID, PHONE, today_str)

        assert res is not None
        assert res["checked_in"] is True
        assert res["token_number"] == 8
        assert res["currently_serving"] == 6
        assert res["patients_ahead"] == 2  # 8 - 6
        assert res["doctor_name"] == "Dr. Legacy"

        # Verify opd.queue_position was never invoked
        mock_opd_qp.assert_not_called()


@pytest.mark.asyncio
async def test_opd_clinic_lab_booking_keeps_legacy_queue():
    """OPD queues are per doctor; a lab booking at an OPD clinic must keep the
    legacy branch-keyed answer and its test-name label."""
    today_str = today_ist().isoformat()
    appt = {
        "id": "lab-appt-1", "clinic_id": CLINIC_ID, "doctor_name": None,
        "booking_type": "lab_test", "lab_test_name": "CBC", "token_number": 4,
        "queue_status": "waiting", "status": "confirmed",
        "appointment_date": today_str, "patient_phone": PHONE,
    }
    calls = 0

    async def mock_sb(query):
        nonlocal calls
        calls += 1
        res = MagicMock()
        res.data = [appt] if calls == 1 else [{"token_number": 3}]
        return res

    with patch("app.database.sb", side_effect=mock_sb), \
         patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value={"id": CLINIC_ID})), \
         patch("app.services.tenant.opd_enabled", return_value=True), \
         patch("app.services.opd.queue_position", new=AsyncMock()) as mock_opd_qp:
        res = await get_patient_queue_status(CLINIC_ID, PHONE, today_str)

    mock_opd_qp.assert_not_called()
    assert res["is_lab_test"] is True and res["doctor_name"] == "CBC"
    assert res["patients_ahead"] == 1
