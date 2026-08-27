"""Tests for Tenant Resolution & Error Handling (Finding #8)."""

import pytest
from unittest.mock import MagicMock, patch

from app.services.tenant import resolve_tenant, _tenant_cache, TenantNotFound


@pytest.mark.asyncio
async def test_resolve_tenant_success():
    """Verify resolve_tenant returns clinic dict on successful DB match."""
    _tenant_cache.clear()

    mock_clinic = {
        "id": "clinic-uuid-999",
        "name": "Apollo Clinic",
        "whatsapp_number": "+919876543210",
        "is_active": True,
    }

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[mock_clinic]
    )

    with patch("app.services.tenant.supabase", mock_sb):
        clinic = await resolve_tenant("+919876543210")
        assert clinic["id"] == "clinic-uuid-999"
        assert clinic["name"] == "Apollo Clinic"


@pytest.mark.asyncio
async def test_resolve_tenant_db_error_raises_exception():
    """Verify DB query errors raise RuntimeError and DO NOT fall back to default clinic."""
    _tenant_cache.clear()

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    # Simulate DB connection timeout or error
    mock_table.select.return_value.eq.return_value.eq.return_value.execute.side_effect = Exception(
        "Supabase connection timeout"
    )

    with patch("app.services.tenant.supabase", mock_sb):
        with pytest.raises(RuntimeError) as exc_info:
            await resolve_tenant("+919876543210")

        assert "Database error during tenant resolution" in str(exc_info.value)
        assert "Supabase connection timeout" in str(exc_info.value)
        # Ensure default tenant fallback WAS NOT returned
        assert "+919876543210" not in _tenant_cache


@pytest.mark.asyncio
async def test_resolve_tenant_inactive_clinic_raises_tenant_not_found():
    """Verify cached inactive clinic raises TenantNotFound."""
    _tenant_cache.clear()
    _tenant_cache["+919999999999"] = {"id": "inactive-clinic", "is_active": False}

    with pytest.raises(TenantNotFound):
        await resolve_tenant("+919999999999")


@pytest.mark.asyncio
async def test_sandbox_fallback_disabled_in_production():
    """T2.1 / KRIYA-012: In production, unrecognized numbers NEVER route to sandbox clinic."""
    _tenant_cache.clear()

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    # Direct lookups find nothing
    mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    # Multi-tenant check finds 2 clinics
    mock_table.select.return_value.eq.return_value.neq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"id": "c1", "is_active": True}, {"id": "c2", "is_active": True}]
    )

    with patch("app.services.tenant.supabase", mock_sb), \
         patch("app.config.settings.app_env", "production"):
        with pytest.raises(TenantNotFound):
            await resolve_tenant("+919876500000")


@pytest.mark.asyncio
async def test_unresolvable_number_alerts_not_routes():
    """T2.1: An unresolvable number in multi-tenant raises TenantNotFound without routing."""
    _tenant_cache.clear()

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_table.select.return_value.eq.return_value.neq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"id": "c1"}, {"id": "c2"}]
    )

    with patch("app.services.tenant.supabase", mock_sb), \
         patch("app.config.settings.app_env", "production"):
        with pytest.raises(TenantNotFound) as exc_info:
            await resolve_tenant("+919999999999")
        assert "No clinic registered" in str(exc_info.value)


@pytest.mark.asyncio
async def test_sandbox_fallback_still_works_in_development():
    """T2.1: In development mode, sandbox fallback routes test numbers to sandbox clinic."""
    _tenant_cache.clear()

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    mock_sandbox = {
        "id": "sandbox-uuid",
        "name": "Sandbox Test Clinic",
        "is_sandbox": True,
        "is_active": True,
    }

    # Direct phone lookup returns empty
    mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    # Sandbox query returns sandbox clinic
    mock_table.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[mock_sandbox]
    )

    with patch("app.services.tenant.supabase", mock_sb), \
         patch("app.config.settings.app_env", "development"):
        clinic = await resolve_tenant("+919876500000")
        assert clinic["id"] == "sandbox-uuid"
        assert clinic["name"] == "Sandbox Test Clinic"
