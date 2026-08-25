"""Phase 4: Global Multi-Tenant Query Scoping & Invariant Tests.

Verifies:
1. P0-5: scoped_query correctly injects .eq("clinic_id", ...) when a valid tenant ID is supplied.
2. scoped_query does not inject invalid filters when clinic_id is None, empty, or 'default'.
3. is_valid_clinic_scope accurately discriminates specific tenants vs global contexts.
4. Database queries across services properly reject cross-tenant leakage.
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import pytest
from unittest.mock import MagicMock, patch

from app.database import (
    scoped_query,
    is_valid_clinic_scope,
    TenantIsolationError,
    TENANT_OWNED_TABLES,
)


def test_scoped_query_with_specific_clinic_id():
    """scoped_query injects clinic_id filter on valid tenant ID."""
    mock_select = MagicMock()
    mock_table = MagicMock()
    mock_table.select.return_value = mock_select

    with patch("app.database.supabase.table", return_value=mock_table):
        scoped_query("appointments", clinic_id="clinic_abc_123", select_fields="id, doctor_name")
        mock_table.select.assert_called_once_with("id, doctor_name")
        mock_select.eq.assert_called_once_with("clinic_id", "clinic_abc_123")


def test_scoped_query_fails_closed_on_tenant_table_without_scope():
    """A tenant-owned table without a valid clinic_id must RAISE, not build unscoped.

    This previously asserted the opposite — that scoped_query silently returned an
    unscoped query for clinic_id in ('default', None, '  '). That behaviour was the
    vulnerability: because the app connects as service_role (BYPASSRLS), an unscoped
    builder returns every tenant's rows, and a forgotten clinic_id was
    indistinguishable from a deliberate global read.
    """
    mock_select = MagicMock()
    mock_table = MagicMock()
    mock_table.select.return_value = mock_select

    with patch("app.database.supabase.table", return_value=mock_table):
        for bad_scope in ("default", None, "  ", "", "null", "none"):
            with pytest.raises(TenantIsolationError):
                scoped_query("patients", clinic_id=bad_scope)


def test_scoped_query_allows_explicit_unscoped_cross_tenant_read():
    """allow_unscoped=True is the deliberate, greppable escape hatch."""
    mock_select = MagicMock()
    mock_table = MagicMock()
    mock_table.select.return_value = mock_select

    with patch("app.database.supabase.table", return_value=mock_table):
        scoped_query("patients", clinic_id=None, allow_unscoped=True)
        mock_select.eq.assert_not_called()


def test_scoped_query_non_tenant_table_needs_no_scope():
    """Global tables (e.g. clinics) are unaffected by the guard."""
    mock_select = MagicMock()
    mock_table = MagicMock()
    mock_table.select.return_value = mock_select

    with patch("app.database.supabase.table", return_value=mock_table):
        scoped_query("clinics", clinic_id=None)
        mock_select.eq.assert_not_called()


def test_every_tenant_owned_table_is_guarded():
    """Guard covers the full tenant-owned table set, not a sample."""
    mock_table = MagicMock()
    mock_table.select.return_value = MagicMock()

    with patch("app.database.supabase.table", return_value=mock_table):
        for table in TENANT_OWNED_TABLES:
            with pytest.raises(TenantIsolationError):
                scoped_query(table, clinic_id=None)


def test_is_valid_clinic_scope():
    """is_valid_clinic_scope returns True only for specific tenant IDs."""
    assert is_valid_clinic_scope("clinic_123") is True
    assert is_valid_clinic_scope("uuid-tenant-abc") is True
    assert is_valid_clinic_scope("default") is False
    assert is_valid_clinic_scope("None") is False
    assert is_valid_clinic_scope("null") is False
    assert is_valid_clinic_scope("") is False
    assert is_valid_clinic_scope(None) is False
    assert is_valid_clinic_scope("   ") is False
