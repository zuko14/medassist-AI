# tests/test_clinic_settings.py
"""Tests for GET /admin/me and GET/PUT /admin/settings/payment."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException, Request

from app.routers.admin import (
    AdminUser,
    get_current_admin,
    get_payment_settings,
    update_payment_settings,
    PaymentSettingsUpdate,
    get_clinic_profile,
    update_clinic_profile,
    ClinicProfileUpdate,
)


def _mock_request() -> Request:
    req = MagicMock()
    req.client.host = "127.0.0.1"
    return req


@pytest.mark.asyncio
async def test_me_super_admin_gets_no_plan_restriction():
    owner = AdminUser("owner", role="super_admin", clinic_id=None, user_id="super_admin_env")
    result = await get_current_admin(user=owner)
    assert result["role"] == "super_admin"
    assert result.get("plan") is None
    assert result.get("features") is None


@pytest.mark.asyncio
async def test_me_soloclinic_admin_gets_soloclinic_features():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {"id": "clinic-1", "plan": "soloclinic", "whatsapp_number": "+911111111111"}

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        result = await get_current_admin(user=admin)

    assert result["plan"] == "soloclinic"
    assert "booking" in result["features"]
    assert "lab_reports" not in result["features"]


@pytest.mark.asyncio
async def test_me_diagstream_admin_gets_diagstream_features():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    fake_clinic = {"id": "clinic-2", "plan": "diagstream", "whatsapp_number": "+912222222222"}

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        result = await get_current_admin(user=admin)

    assert result["plan"] == "diagstream"
    assert "lab_reports" in result["features"]
    assert "booking" not in result["features"]
    assert "payments_razorpay" not in result["features"]


@pytest.mark.asyncio
async def test_get_payment_settings_masks_secret():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {
        "id": "clinic-1",
        "plan": "soloclinic",
        "whatsapp_number": "+911111111111",
        "config": {
            "razorpay_key_id": "rzp_live_abc123",
            "razorpay_key_secret": "supersecretvalue",
            "payment_mode": "full",
        },
    }

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        result = await get_payment_settings(clinic_id="default", user=admin)

    assert result["razorpay_key_id"] == "rzp_live_abc123"
    assert result["razorpay_key_secret_masked"].endswith("alue")
    assert "supersecretvalue" not in result["razorpay_key_secret_masked"]
    assert result["payment_mode"] == "full"


@pytest.mark.asyncio
async def test_update_payment_settings_clinic_admin_updates_own_clinic():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {
        "id": "clinic-1",
        "plan": "soloclinic",
        "whatsapp_number": "+911111111111",
        "config": {},
    }
    updated_clinic = {
        "id": "clinic-1",
        "whatsapp_number": "+911111111111",
        "config": {
            "razorpay_key_id": "rzp_live_new",
            "razorpay_key_secret": "newsecret",
            "payment_mode": "partial",
            "payment_deposit_percent": 25,
        },
    }
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[updated_clinic]
    )

    body = PaymentSettingsUpdate(
        razorpay_key_id="rzp_live_new",
        razorpay_key_secret="newsecret",
        payment_mode="partial",
        payment_deposit_percent=25,
    )

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ), patch("app.routers.admin.supabase", mock_sb), patch(
        "app.routers.admin.invalidate_tenant_cache"
    ):
        result = await update_payment_settings(
            body=body, request=_mock_request(), clinic_id="default", user=admin
        )

    assert result["success"] is True
    sent_config = mock_table.update.call_args[0][0]["config"]
    assert sent_config["payment_mode"] == "partial"
    assert sent_config["payment_deposit_percent"] == 25


@pytest.mark.asyncio
async def test_update_payment_settings_cross_tenant_forbidden():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    body = PaymentSettingsUpdate(payment_mode="none")

    with pytest.raises(HTTPException) as exc:
        await update_payment_settings(
            body=body, request=_mock_request(), clinic_id="clinic-999", user=admin
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_payment_settings_rejects_diagstream_clinic():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    fake_clinic = {"id": "clinic-2", "plan": "diagstream", "whatsapp_number": "+912222222222", "config": {}}
    body = PaymentSettingsUpdate(payment_mode="full")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        with pytest.raises(HTTPException) as exc:
            await update_payment_settings(
                body=body, request=_mock_request(), clinic_id="default", user=admin
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_payment_settings_partial_without_percent_rejected():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {"id": "clinic-1", "plan": "soloclinic", "whatsapp_number": "+911111111111", "config": {}}
    body = PaymentSettingsUpdate(payment_mode="partial")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        with pytest.raises(HTTPException) as exc:
            await update_payment_settings(
                body=body, request=_mock_request(), clinic_id="default", user=admin
            )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_payment_settings_empty_secret_does_not_clobber_stored():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {
        "id": "clinic-1",
        "plan": "soloclinic",
        "whatsapp_number": "+911111111111",
        "config": {"razorpay_key_id": "rzp_live_existing", "razorpay_key_secret": "existingsecret"},
    }
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "clinic-1", "whatsapp_number": "+911111111111", "config": fake_clinic["config"]}]
    )

    body = PaymentSettingsUpdate(razorpay_key_secret="")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ), patch("app.routers.admin.supabase", mock_sb), patch(
        "app.routers.admin.invalidate_tenant_cache"
    ):
        await update_payment_settings(
            body=body, request=_mock_request(), clinic_id="default", user=admin
        )

    sent_config = mock_table.update.call_args[0][0]["config"]
    assert sent_config["razorpay_key_secret"] == "existingsecret"


def test_payment_settings_update_rejects_out_of_range_percent():
    with pytest.raises(ValueError):
        PaymentSettingsUpdate(payment_mode="partial", payment_deposit_percent=150)


@pytest.mark.asyncio
async def test_get_clinic_profile_falls_back_to_global_defaults():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {"id": "clinic-1", "plan": "soloclinic", "whatsapp_number": "+911111111111", "config": {}}

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        result = await get_clinic_profile(clinic_id="default", user=admin)

    assert result["name"]  # falls back to settings.hospital_name
    assert "hospital_address" in result
    assert "hospital_maps_link" in result
    assert "hospital_emergency_number" in result


@pytest.mark.asyncio
async def test_update_clinic_profile_sets_name_address_maps_link_and_emergency_number():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {"id": "clinic-1", "plan": "soloclinic", "whatsapp_number": "+911111111111", "config": {}}
    updated_clinic = {
        "id": "clinic-1",
        "name": "City Care Hospital",
        "whatsapp_number": "+911111111111",
        "config": {
            "address": "123 Main St",
            "maps_link": "https://maps.google.com/xyz",
            "emergency_number": "+919999999999",
        },
    }
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[updated_clinic]
    )

    body = ClinicProfileUpdate(
        name="City Care Hospital",
        hospital_address="123 Main St",
        hospital_maps_link="https://maps.google.com/xyz",
        hospital_emergency_number="+919999999999",
    )

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ), patch("app.routers.admin.supabase", mock_sb), patch(
        "app.routers.admin.invalidate_tenant_cache"
    ):
        result = await update_clinic_profile(
            body=body, request=_mock_request(), clinic_id="default", user=admin
        )

    assert result["success"] is True
    sent_updates = mock_table.update.call_args[0][0]
    assert sent_updates["name"] == "City Care Hospital"
    assert sent_updates["config"]["address"] == "123 Main St"
    assert sent_updates["config"]["maps_link"] == "https://maps.google.com/xyz"
    assert sent_updates["config"]["emergency_number"] == "+919999999999"


@pytest.mark.asyncio
async def test_update_clinic_profile_cross_tenant_forbidden():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    body = ClinicProfileUpdate(name="Other Hospital")

    with pytest.raises(HTTPException) as exc:
        await update_clinic_profile(
            body=body, request=_mock_request(), clinic_id="clinic-999", user=admin
        )
    assert exc.value.status_code == 403


def test_clinic_profile_update_rejects_blank_name():
    with pytest.raises(ValueError):
        ClinicProfileUpdate(name="   ")
