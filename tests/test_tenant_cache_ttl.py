"""Tests for Tenant and Branch In-Memory Cache TTL Expiration (Finding #10)."""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.tenant import (
    resolve_tenant,
    get_clinic_branches,
    invalidate_tenant_cache,
    invalidate_branch_cache,
    _tenant_cache,
    _branch_cache,
    _set_cached_item,
    CACHE_TTL_SECONDS,
)


@pytest.mark.asyncio
async def test_tenant_cache_ttl_expiration():
    """Verify tenant cache expires after TTL and re-queries DB."""
    _tenant_cache.clear()

    mock_clinic_1 = {"id": "c1", "name": "Clinic V1", "whatsapp_number": "+919999900000", "is_active": True}
    mock_clinic_2 = {"id": "c1", "name": "Clinic V2", "whatsapp_number": "+919999900000", "is_active": True}

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    # DB returns V1 first, then V2
    mock_table.select.return_value.eq.return_value.eq.return_value.execute.side_effect = [
        MagicMock(data=[mock_clinic_1]),
        MagicMock(data=[mock_clinic_2]),
    ]

    with patch("app.services.tenant.supabase", mock_sb):
        # 1st call: DB query -> caches V1
        c1 = await resolve_tenant("+919999900000")
        assert c1["name"] == "Clinic V1"
        assert mock_table.select.call_count == 1

        # 2nd call (immediate): returned from cache without DB query
        c2 = await resolve_tenant("+919999900000")
        assert c2["name"] == "Clinic V1"
        assert mock_table.select.call_count == 1

        # Simulate TTL expiration by backdating cached_at by CACHE_TTL_SECONDS + 1
        _tenant_cache["+919999900000"]["cached_at"] -= (CACHE_TTL_SECONDS + 10)

        # 3rd call (after TTL): cache miss -> re-queries DB -> returns V2
        c3 = await resolve_tenant("+919999900000")
        assert c3["name"] == "Clinic V2"
        assert mock_table.select.call_count == 2


@pytest.mark.asyncio
async def test_branch_cache_ttl_expiration():
    """Verify branch cache expires after TTL and re-queries DB."""
    _branch_cache.clear()

    mock_branches_v1 = [{"id": "b1", "name": "Branch 1"}]
    mock_branches_v2 = [{"id": "b1", "name": "Branch 1"}, {"id": "b2", "name": "Branch 2"}]

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    mock_table.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.side_effect = [
        MagicMock(data=mock_branches_v1),
        MagicMock(data=mock_branches_v2),
    ]

    with patch("app.services.tenant.supabase", mock_sb):
        b1 = await get_clinic_branches("clinic-uuid")
        assert len(b1) == 1

        # Immediate repeat call uses cache
        b2 = await get_clinic_branches("clinic-uuid")
        assert len(b2) == 1

        # Backdate cached_at to simulate TTL expiration
        _branch_cache["clinic-uuid"]["cached_at"] -= (CACHE_TTL_SECONDS + 10)

        b3 = await get_clinic_branches("clinic-uuid")
        assert len(b3) == 2
