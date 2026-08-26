"""Tests for POST /admin/clinics creation and PATCH /admin/clinics/{id} payment_mode validation guard."""

import pytest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.routers.clinics import create_clinic, update_clinic, CreateClinicRequest


def _mock_supabase_for_create(clinic_row: dict, admin_row: dict):
    """Route supabase.table('clinics'|'clinic_admins') to separate mock tables."""
    mock_sb = MagicMock()
    clinics_table = MagicMock()
    admins_table = MagicMock()
    clinics_table.insert.return_value.execute.return_value = MagicMock(data=[clinic_row])
    admins_table.insert.return_value.execute.return_value = MagicMock(data=[admin_row])

    def table_router(name):
        return {"clinics": clinics_table, "clinic_admins": admins_table}[name]

    mock_sb.table.side_effect = table_router
    return mock_sb, clinics_table, admins_table


@pytest.mark.asyncio
async def test_create_clinic_auto_provisions_clinic_admin():
    clinic_row = {"id": "clinic-new", "name": "City Care", "whatsapp_number": "+911111111111"}
    admin_row = {"id": "admin-1", "clinic_id": "clinic-new", "username": "citycareabc123"}
    mock_sb, clinics_table, admins_table = _mock_supabase_for_create(clinic_row, admin_row)

    req = CreateClinicRequest(
        name="City Care",
        whatsapp_number="+911111111111",
        meta_phone_number_id="pid",
        meta_access_token="token",
        clinic_name="City Care",
        doctor_name="Dr. Admin",
    )

    with patch("app.routers.clinics.supabase", mock_sb):
        result = await create_clinic(req)

    assert result["success"] is True
    assert result["clinic_admin"]["username"]
    assert result["clinic_admin"]["password"]
    inserted = admins_table.insert.call_args[0][0]
    assert inserted["clinic_id"] == "clinic-new"
    assert inserted["role"] == "clinic_admin"
    assert inserted["is_active"] is True
    assert "password" not in inserted  # only the hash is stored
    assert inserted["password_hash"] != result["clinic_admin"]["password"]


@pytest.mark.asyncio
async def test_create_clinic_still_succeeds_if_admin_provisioning_fails():
    clinic_row = {"id": "clinic-new", "name": "City Care", "whatsapp_number": "+911111111111"}
    mock_sb = MagicMock()
    clinics_table = MagicMock()
    admins_table = MagicMock()
    clinics_table.insert.return_value.execute.return_value = MagicMock(data=[clinic_row])
    admins_table.insert.return_value.execute.side_effect = Exception("username collision")
    mock_sb.table.side_effect = lambda name: {"clinics": clinics_table, "clinic_admins": admins_table}[name]

    req = CreateClinicRequest(
        name="City Care",
        whatsapp_number="+911111111111",
        meta_phone_number_id="pid",
        meta_access_token="token",
        clinic_name="City Care",
        doctor_name="Dr. Admin",
    )

    with patch("app.routers.clinics.supabase", mock_sb):
        result = await create_clinic(req)

    assert result["success"] is True
    assert result["clinic_admin"] is None


@pytest.mark.asyncio
async def test_update_clinic_rejects_partial_mode_without_percent():
    with pytest.raises(HTTPException) as exc:
        await update_clinic(
            "clinic-1", {"config": {"payment_mode": "partial"}}
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_clinic_rejects_partial_mode_with_out_of_range_percent():
    with pytest.raises(HTTPException) as exc:
        await update_clinic(
            "clinic-1",
            {"config": {"payment_mode": "partial", "payment_deposit_percent": 150}},
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_clinic_allows_partial_mode_with_valid_percent():
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "clinic-1", "whatsapp_number": "+911111111111"}]
    )

    with patch("app.routers.clinics.supabase", mock_sb), patch(
        "app.routers.clinics.invalidate_tenant_cache"
    ):
        result = await update_clinic(
            "clinic-1",
            {"config": {"payment_mode": "partial", "payment_deposit_percent": 20}},
        )

    assert result["success"] is True


@pytest.mark.asyncio
async def test_update_clinic_unrelated_updates_unaffected():
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "clinic-1", "whatsapp_number": "+911111111111", "plan": "essential"}]
    )

    with patch("app.routers.clinics.supabase", mock_sb), patch(
        "app.routers.clinics.invalidate_tenant_cache"
    ):
        result = await update_clinic("clinic-1", {"plan": "essential"})

    assert result["success"] is True


@pytest.mark.asyncio
async def test_create_clinic_stores_meta_waba_id_in_config():
    """The WABA id is what lets whatsapp_doctor read template approval state."""
    clinic_row = {"id": "clinic-new", "name": "City Care", "whatsapp_number": "+911111111111"}
    admin_row = {"id": "admin-1", "clinic_id": "clinic-new", "username": "citycareabc123"}
    mock_sb, clinics_table, _ = _mock_supabase_for_create(clinic_row, admin_row)

    req = CreateClinicRequest(
        name="City Care",
        whatsapp_number="+911111111111",
        meta_phone_number_id="pid",
        meta_access_token="token",
        meta_waba_id="1702889104159864",
    )

    with patch("app.routers.clinics.supabase", mock_sb):
        await create_clinic(req)

    assert clinics_table.insert.call_args[0][0]["config"]["meta_waba_id"] == "1702889104159864"


@pytest.mark.asyncio
async def test_update_clinic_merges_meta_waba_id_into_existing_config():
    """meta_* are config keys, not columns — they must never reach the update payload."""
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "clinic-1", "whatsapp_number": "+911111111111"}]
    )

    async def _fake_get(clinic_id):
        return {"id": clinic_id, "config": {"meta_access_token": "tok", "language": "te"}}

    with patch("app.routers.clinics.supabase", mock_sb), patch(
        "app.routers.clinics.invalidate_tenant_cache"
    ), patch("app.routers.clinics.get_clinic_by_id", _fake_get):
        result = await update_clinic("clinic-1", {"meta_waba_id": "1702889104159864"})

    assert result["success"] is True
    payload = mock_table.update.call_args[0][0]
    assert "meta_waba_id" not in payload  # would be an unknown column
    assert payload["config"] == {
        "meta_access_token": "tok",
        "language": "te",
        "meta_waba_id": "1702889104159864",
    }
