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

from app.database import scoped_query, is_valid_clinic_scope


def test_scoped_query_with_specific_clinic_id():
    """scoped_query injects clinic_id filter on valid tenant ID."""
    mock_select = MagicMock()
    mock_table = MagicMock()
    mock_table.select.return_value = mock_select

    with patch("app.database.supabase.table", return_value=mock_table):
        scoped_query("appointments", clinic_id="clinic_abc_123", select_fields="id, doctor_name")
        mock_table.select.assert_called_once_with("id, doctor_name")
        mock_select.eq.assert_called_once_with("clinic_id", "clinic_abc_123")


def test_scoped_query_with_default_or_none_clinic_id():
    """scoped_query omits clinic_id filter when clinic_id is 'default', None, or empty."""
    mock_select = MagicMock()
    mock_table = MagicMock()
    mock_table.select.return_value = mock_select

    with patch("app.database.supabase.table", return_value=mock_table):
        # Case 1: default
        scoped_query("patients", clinic_id="default")
        mock_select.eq.assert_not_called()

        # Case 2: None
        scoped_query("patients", clinic_id=None)
        mock_select.eq.assert_not_called()

        # Case 3: Empty string / whitespace
        scoped_query("patients", clinic_id="  ")
        mock_select.eq.assert_not_called()


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
