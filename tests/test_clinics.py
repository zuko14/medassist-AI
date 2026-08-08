"""Tests for PATCH /admin/clinics/{id} payment_mode validation guard."""

import pytest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.routers.clinics import update_clinic


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
