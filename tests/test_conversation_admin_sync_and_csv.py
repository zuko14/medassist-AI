"""Tests for Dynamic Service Discovery, Admin-to-Bot Synchronization,
Doctor Branch Presentation, and Atomic CSV Import.
"""

import io
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.database import (
    _doctor_cache,
    DOCTOR_CACHE_TTL_SECONDS,
    get_doctor_by_name,
    get_lab_test_by_id,
    invalidate_doctor_cache,
)
from app.routers.admin import (
    AdminUser,
    _sanitize_csv_cell,
    _normalize_csv_headers,
    CSV_MAX_FILE_BYTES,
    CSV_MAX_DATA_ROWS,
    import_lab_tests_csv,
    download_lab_test_csv_template,
)
from app.services.conversation import ConversationManager


# ─────────────────────────────────────────────────────────────
# 1. Dynamic Service Discovery & Unification Tests
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dynamic_services_from_doctors():
    """Verify _show_services delegates to _show_department_list and displays only active doctor departments."""
    service = ConversationManager()
    service.whatsapp = MagicMock()
    service.whatsapp.send_interactive_list = AsyncMock()
    service.update_state = AsyncMock()

    clinic = {"id": "clinic-1", "name": "Apollo Clinic"}

    # Mock supabase doctors table to return Cardiology and Dental
    mock_supabase = MagicMock()
    mock_query = MagicMock()
    mock_query.select.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.execute.return_value = MagicMock(
        data=[
            {"department": "Cardiology"},
            {"department": "Dental"},
            {"department": "Cardiology"},  # Duplicate should be deduplicated
        ]
    )
    mock_supabase.table.return_value = mock_query

    with patch("app.database.supabase", mock_supabase):
        await service._show_services(clinic, "919876543210", "en")

    # Verify interactive list was sent with Cardiology and Dental
    service.whatsapp.send_interactive_list.assert_called_once()
    call_kwargs = service.whatsapp.send_interactive_list.call_args[1]
    sections = call_kwargs["sections"]
    assert len(sections) == 1
    rows = sections[0]["rows"]
    dept_titles = [r["title"] for r in rows]
    assert dept_titles == ["Cardiology", "Dental"]
    assert "General Medicine" not in dept_titles


@pytest.mark.asyncio
async def test_no_active_doctors_no_services():
    """Verify that when no active doctors exist, bot sends friendly no-services message and does NOT fall back to General Medicine."""
    service = ConversationManager()
    service.whatsapp = MagicMock()
    service.whatsapp.send_text = AsyncMock()
    service._send_main_menu = AsyncMock()
    service.update_state = AsyncMock()

    clinic = {"id": "clinic-empty", "name": "Empty Clinic"}

    mock_supabase = MagicMock()
    mock_query = MagicMock()
    mock_query.select.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.execute.return_value = MagicMock(data=[])
    mock_supabase.table.return_value = mock_query

    with patch("app.database.supabase", mock_supabase):
        await service._show_services(clinic, "919876543210", "en")

    service.whatsapp.send_text.assert_called_once()
    sent_text = service.whatsapp.send_text.call_args[0][2]
    assert "No medical services or doctors are currently available" in sent_text
    service._send_main_menu.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_svc_button_compatibility():
    """Verify that legacy svc_* button IDs still resolve properly during the 30-day grace period."""
    service = ConversationManager()
    service._show_doctor_list = AsyncMock()
    service._show_department_list = AsyncMock()

    clinic = {"id": "clinic-1", "name": "Apollo Clinic"}
    context = {}

    mock_supabase = MagicMock()
    mock_query = MagicMock()
    mock_query.select.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.order.return_value = mock_query
    mock_query.execute.return_value = MagicMock(
        data=[{"id": "doc-1", "name": "Dr. Sharma", "department": "Orthopedics", "is_active": True}]
    )
    mock_supabase.table.return_value = mock_query

    with patch("app.database.supabase", mock_supabase):
        # Patient tapped legacy svc_ortho button
        await service._handle_selecting_department(
            clinic=clinic,
            phone="919876543210",
            message="",
            intent="",
            context=context,
            lang="en",
            interactive_data={"id": "svc_ortho"},
        )

    # Resolved to Orthopedics and displayed doctor list
    service._show_doctor_list.assert_called_once()
    assert service._show_doctor_list.call_args[0][2] == "Orthopedics"


# ─────────────────────────────────────────────────────────────
# 2. Doctor / Branch UX Tests
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_doctor_branch_grouping():
    """Verify _show_doctors groups canonical doctor with compact branch labels."""
    service = ConversationManager()
    service.whatsapp = MagicMock()
    service.whatsapp.send_interactive_list = AsyncMock()

    clinic = {"id": "clinic-1", "name": "Apollo Clinic"}

    mock_docs = [
        {
            "id": "doc-1",
            "name": "Dr. Rao",
            "department": "Cardiology",
            "specialization": "Cardiologist",
            "consultation_fee": 800,
            "is_active": True,
        }
    ]

    mock_doc_branches = [
        {
            "doctor_id": "doc-1",
            "branch_id": "b-1",
            "session": "morning",
            "branches": {"name": "Kukatpally Branch", "short_name": "Kukatpally"},
        },
        {
            "doctor_id": "doc-1",
            "branch_id": "b-2",
            "session": "both",
            "branches": {"name": "Hitec City Branch", "short_name": "Hitec"},
        },
    ]

    mock_supabase = MagicMock()
    
    def table_router(table_name):
        q = MagicMock()
        q.select.return_value = q
        q.eq.return_value = q
        q.in_.return_value = q
        q.order.return_value = q
        if table_name == "doctors":
            q.execute.return_value = MagicMock(data=mock_docs)
        elif table_name == "doctor_branches":
            q.execute.return_value = MagicMock(data=mock_doc_branches)
        return q

    mock_supabase.table.side_effect = table_router

    with patch("app.database.supabase", mock_supabase):
        await service._show_doctors(clinic, "919876543210", "en")

    service.whatsapp.send_interactive_list.assert_called_once()
    call_kwargs = service.whatsapp.send_interactive_list.call_args[1]
    row = call_kwargs["sections"][0]["rows"][0]
    assert row["id"] == "view_doc_doc-1"
    assert row["title"] == "Dr. Rao"
    assert "kukatpally(mor)" in row["description"].lower()
    assert "Hitec" in row["description"]
    assert "₹800" in row["description"]


@pytest.mark.asyncio
async def test_doctor_detail_shows_branches_and_prompts_branch_selection():
    """Verify view_doc_ displays hierarchical branch breakdown and prompts branch selection for multi-branch doctor."""
    service = ConversationManager()
    service.whatsapp = MagicMock()
    service.whatsapp.send_text = AsyncMock()
    service.whatsapp.send_interactive_buttons = AsyncMock()
    service.update_state = AsyncMock()

    clinic = {"id": "clinic-1", "name": "Apollo Clinic"}

    mock_doc = {
        "id": "doc-1",
        "name": "Dr. Rao",
        "department": "Cardiology",
        "specialization": "Interventional Cardiologist",
        "consultation_fee": 800,
        "rating": "4.9",
        "is_active": True,
    }

    mock_doc_branches = [
        {
            "doctor_id": "doc-1",
            "branch_id": "b-1",
            "session": "morning",
            "branches": {"name": "Kukatpally Branch", "short_name": "Kukatpally"},
        },
        {
            "doctor_id": "doc-1",
            "branch_id": "b-2",
            "session": "evening",
            "branches": {"name": "Hitec City Branch", "short_name": "Hitec"},
        },
    ]

    mock_supabase = MagicMock()
    def table_router(table_name):
        q = MagicMock()
        q.select.return_value = q
        q.eq.return_value = q
        if table_name == "doctors":
            q.execute.return_value = MagicMock(data=[mock_doc])
        elif table_name == "doctor_branches":
            q.execute.return_value = MagicMock(data=mock_doc_branches)
        return q

    mock_supabase.table.side_effect = table_router

    with patch("app.services.conversation.get_lang", AsyncMock(return_value="en")), \
         patch("app.database.supabase", mock_supabase), \
         patch("app.services.conversation.get_or_create_conversation", AsyncMock(return_value={"state": "main_menu", "context": {}})), \
         patch("app.services.conversation.get_patient_by_phone", AsyncMock(return_value={"id": "p-1", "language": "en"})):
        await service._handle_message_locked(
            clinic=clinic,
            phone="919876543210",
            message="",
            message_type="interactive",
            interactive_data={"id": "view_doc_doc-1", "title": "Dr. Rao"},
        )

    # 1. Detail text card sent
    service.whatsapp.send_text.assert_called_once()
    detail_card = service.whatsapp.send_text.call_args[0][2]
    assert "Dr. Rao" in detail_card
    assert "Kukatpally" in detail_card
    assert "Hitec" in detail_card
    assert "₹800" in detail_card

    # 2. Branch selection sent (since multi-branch)
    service.whatsapp.send_interactive_buttons.assert_called_once()
    service.update_state.assert_called_once()
    assert service.update_state.call_args[0][2] == "selecting_branch"


# ─────────────────────────────────────────────────────────────
# 3. Atomic CSV Import Tests
# ─────────────────────────────────────────────────────────────

def test_sanitize_csv_cell():
    """Verify spreadsheet formula injection prefixes are sanitized with a leading single quote."""
    assert _sanitize_csv_cell("=SUM(A1:A10)") == "'=SUM(A1:A10)"
    assert _sanitize_csv_cell("+12345") == "'+12345"
    assert _sanitize_csv_cell("-12345") == "'-12345"
    assert _sanitize_csv_cell("@cmd") == "'@cmd"
    assert _sanitize_csv_cell("\tTab") == "'\tTab"
    assert _sanitize_csv_cell("Normal Text") == "Normal Text"
    assert _sanitize_csv_cell("") == ""


def test_normalize_csv_headers():
    """Verify header normalization aliases."""
    headers = ["Test Name", "Price (Rs)", "Specimen", "TAT", "Fasting Required", "Instructions"]
    mapping = _normalize_csv_headers(headers)
    assert mapping["Test Name"] == "name"
    assert mapping["Price (Rs)"] == "price_rupees"
    assert mapping["Specimen"] == "sample_type"
    assert mapping["TAT"] == "turnaround_hours"
    assert mapping["Fasting Required"] == "fasting_required"
    assert mapping["Instructions"] == "prep_instructions"


@pytest.mark.asyncio
async def test_csv_atomic_reject_invalid_row():
    """Verify that a single invalid row rejects the entire CSV with 422 and mutates zero database rows."""
    csv_content = (
        "name,price_rupees,sample_type\n"
        "Complete Blood Count,350,Blood\n"
        ",500,Blood\n"  # Invalid: missing name
        "Lipid Profile,not_a_number,Blood\n"  # Invalid: invalid price
    )

    upload_file = MagicMock()
    upload_file.read = AsyncMock(return_value=csv_content.encode("utf-8"))

    user = AdminUser(
        username="admin", role="admin", clinic_id="clinic-1", permissions=["LAB_TESTS_MANAGE"]
    )

    mock_supabase = MagicMock()

    with patch("app.routers.admin.supabase", mock_supabase), \
         patch("app.routers.admin.log_admin_action", AsyncMock()):
        response = await import_lab_tests_csv(file=upload_file, clinic_id="clinic-1", user=user)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 422
    content = response.body.decode("utf-8")
    assert "Import rejected" in content
    assert "Missing test name" in content
    assert "Price must be a positive number" in content
    
    # Assert ZERO database insert/update calls occurred
    mock_supabase.table().insert.assert_not_called()
    mock_supabase.table().update.assert_not_called()


@pytest.mark.asyncio
async def test_csv_size_limit():
    """Verify files larger than 5 MB are rejected with 400."""
    large_payload = b"name,price_rupees\nTest," + b"100\n" * (CSV_MAX_FILE_BYTES // 4)

    upload_file = MagicMock()
    upload_file.read = AsyncMock(return_value=large_payload)

    user = AdminUser(
        username="admin", role="admin", clinic_id="clinic-1", permissions=["LAB_TESTS_MANAGE"]
    )

    with pytest.raises(HTTPException) as exc_info:
        await import_lab_tests_csv(file=upload_file, clinic_id="clinic-1", user=user)

    assert exc_info.value.status_code == 400
    assert "exceeds maximum size limit" in exc_info.value.detail


@pytest.mark.asyncio
async def test_csv_duplicate_within_file():
    """Verify duplicate test names within the same CSV are rejected."""
    csv_content = (
        "name,price_rupees\n"
        "Thyroid Profile,750\n"
        "thyroid profile,800\n"  # Duplicate case-insensitive
    )

    upload_file = MagicMock()
    upload_file.read = AsyncMock(return_value=csv_content.encode("utf-8"))

    user = AdminUser(
        username="admin", role="admin", clinic_id="clinic-1", permissions=["LAB_TESTS_MANAGE"]
    )

    response = await import_lab_tests_csv(file=upload_file, clinic_id="clinic-1", user=user)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 422
    assert "Duplicate test name in CSV" in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_csv_upsert_existing():
    """Verify valid CSV correctly upserts existing and inserts new tests."""
    csv_content = (
        "name,price_rupees,sample_type,fasting_required\n"
        "CBC,400,Blood,false\n"
        "New Test,1200,Serum,true\n"
    )

    upload_file = MagicMock()
    upload_file.read = AsyncMock(return_value=csv_content.encode("utf-8"))

    user = AdminUser(
        username="admin", role="admin", clinic_id="clinic-1", permissions=["LAB_TESTS_MANAGE"]
    )

    mock_supabase = MagicMock()
    mock_table = MagicMock()
    
    # Mock existing CBC
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "test-uuid-1", "name": "CBC"}]
    )
    mock_supabase.table.return_value = mock_table

    with patch("app.routers.admin.supabase", mock_supabase), \
         patch("app.routers.admin.log_admin_action", AsyncMock()):
        result = await import_lab_tests_csv(file=upload_file, clinic_id="clinic-1", user=user)

    assert result["created"] == 1
    assert result["updated"] == 1
    assert result["total_imported"] == 2
    assert len(result["errors"]) == 0


@pytest.mark.asyncio
async def test_csv_template_download():
    """Verify template download endpoint returns valid CSV file."""
    user = AdminUser(username="admin", role="admin", clinic_id="clinic-1", permissions=[])
    response = await download_lab_test_csv_template(user=user)
    assert response.media_type == "text/csv"
    assert "Content-Disposition" in response.headers
    assert b"name,price_rupees,sample_type" in response.body


# ─────────────────────────────────────────────────────────────
# 4. Security & Bug Fix Verification Tests
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lab_test_by_id_tenant_scoped():
    """Verify get_lab_test_by_id enforces clinic_id scoping."""
    import app.database
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value = mock_table
    mock_table.eq.return_value = mock_table
    mock_table.execute.return_value = MagicMock(data=[{"id": "t-1", "clinic_id": "clinic-A", "is_active": True}])

    with patch.object(app.database, "supabase", mock_sb):
        test = await app.database.get_lab_test_by_id("clinic-A", "t-1")
        mock_sb.table.assert_called_once_with("lab_tests")
        mock_table.eq.assert_any_call("clinic_id", "clinic-A")
        mock_table.eq.assert_any_call("id", "t-1")
        assert test["id"] == "t-1"


@pytest.mark.asyncio
async def test_booking_revalidates_doctor_active():
    """Verify that if a doctor is deactivated while patient is on confirmation screen, booking is safely rejected."""
    service = ConversationManager()
    service.whatsapp = MagicMock()
    service.whatsapp.send_text = AsyncMock()
    service._send_main_menu = AsyncMock()
    service.update_state = AsyncMock()

    clinic = {"id": "clinic-1", "name": "Apollo Clinic"}
    context = {
        "doctor_name": "Dr. Inactive",
        "appointment_date": "2026-08-30",
        "appointment_time": "10:00 AM",
    }

    with patch("app.database.get_doctor_by_name", AsyncMock(return_value={"name": "Dr. Inactive", "is_active": False})):
        await service._handle_confirming_booking(
            clinic=clinic,
            phone="919876543210",
            message="yes",
            intent="confirm_booking",
            context=context,
            patient={"id": "p-1"},
            lang="en",
        )

    service.whatsapp.send_text.assert_called_once()
    err_text = service.whatsapp.send_text.call_args[0][2]
    assert "no longer available for online bookings" in err_text
    service._send_main_menu.assert_called_once()


def test_doctor_cache_invalidation():
    """Verify invalidate_doctor_cache evicts targeted clinic/doctor entries."""
    _doctor_cache.clear()
    _doctor_cache["clinic-1:Dr. Smith"] = {"data": {"name": "Dr. Smith"}}
    _doctor_cache["clinic-1:Dr. Jones"] = {"data": {"name": "Dr. Jones"}}
    _doctor_cache["clinic-2:Dr. White"] = {"data": {"name": "Dr. White"}}

    # Evict single doctor
    invalidate_doctor_cache("clinic-1", "Dr. Smith")
    assert "clinic-1:Dr. Smith" not in _doctor_cache
    assert "clinic-1:Dr. Jones" in _doctor_cache

    # Evict whole clinic
    invalidate_doctor_cache("clinic-1")
    assert "clinic-1:Dr. Jones" not in _doctor_cache
    assert "clinic-2:Dr. White" in _doctor_cache

    # Evict all
    invalidate_doctor_cache()
    assert len(_doctor_cache) == 0
