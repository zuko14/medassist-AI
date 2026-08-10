# tests/test_admin_prescriptions_feature_gate.py
"""Prescriptions require the 'booking' feature — diagstream (lab-only) clinics
have no doctor consultations and must be rejected with 403, matching the
gating pattern already used for payments_razorpay and lab_reports."""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from datetime import date

from app.routers.admin import AdminUser, PrescriptionCreate, add_prescription, get_prescriptions


@pytest.mark.asyncio
async def test_add_prescription_rejects_diagstream_plan():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-9", user_id="user-9")
    fake_clinic = {"id": "clinic-9", "plan": "diagstream", "whatsapp_number": "+911111111111"}
    body = PrescriptionCreate(
        patient_phone="919876543210",
        patient_name="Test Patient",
        medicine_name="Paracetamol",
        dosage="500mg",
        frequency="Twice daily",
        reminder_times=["09:00", "21:00"],
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 10),
    )

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        with pytest.raises(HTTPException) as exc:
            await add_prescription(body=body, clinic_id="default", user=admin)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_prescriptions_rejects_diagstream_plan():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-9", user_id="user-9")
    fake_clinic = {"id": "clinic-9", "plan": "diagstream", "whatsapp_number": "+911111111111"}

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        with pytest.raises(HTTPException) as exc:
            await get_prescriptions(clinic_id="default", user=admin)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_prescriptions_allows_soloclinic_plan():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {"id": "clinic-1", "plan": "soloclinic", "whatsapp_number": "+911111111111"}

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ), patch(
        "app.routers.admin.PrescriptionService.get_all_prescriptions",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await get_prescriptions(clinic_id="default", user=admin)
    assert result == {"prescriptions": []}
