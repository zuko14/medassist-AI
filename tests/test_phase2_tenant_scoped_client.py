"""Tests for TenantScopedClient and isolation backstop (W2)."""

import pytest
from unittest.mock import MagicMock
from app.services.tenant_scoped_client import (
    TenantScopedClient,
    TenantIsolationError,
    get_tenant_scoped_client,
)


def test_tenant_scoped_client_requires_non_empty_clinic():
    with pytest.raises(TenantIsolationError):
        TenantScopedClient("")

    with pytest.raises(TenantIsolationError):
        get_tenant_scoped_client(None)


def test_select_auto_injects_clinic_id():
    mock_raw = MagicMock()
    mock_table = MagicMock()
    mock_select = MagicMock()
    mock_eq = MagicMock()

    mock_raw.table.return_value = mock_table
    mock_table.select.return_value = mock_select
    mock_select.eq.return_value = mock_eq

    client = TenantScopedClient("clinic-123", raw_client=mock_raw)
    builder = client.table("appointments").select("*")

    mock_raw.table.assert_called_with("appointments")
    mock_table.select.assert_called_with("*")
    mock_select.eq.assert_called_with("clinic_id", "clinic-123")


def test_insert_rejects_cross_tenant_payload():
    mock_raw = MagicMock()
    client = TenantScopedClient("clinic-123", raw_client=mock_raw)

    with pytest.raises(TenantIsolationError) as exc:
        client.table("patients").insert({"name": "Eve", "clinic_id": "clinic-456"})
    assert "Cross-tenant INSERT attempted" in str(exc.value)


def test_insert_injects_client_clinic_id():
    mock_raw = MagicMock()
    mock_table = MagicMock()
    mock_raw.table.return_value = mock_table

    client = TenantScopedClient("clinic-123", raw_client=mock_raw)
    data = {"name": "Alice", "phone": "1234567890"}
    client.table("patients").insert(data)

    assert data["clinic_id"] == "clinic-123"
    mock_table.insert.assert_called_with(data)


def test_update_auto_injects_clinic_id_filter():
    mock_raw = MagicMock()
    mock_table = MagicMock()
    mock_update = MagicMock()
    mock_raw.table.return_value = mock_table
    mock_table.update.return_value = mock_update

    client = TenantScopedClient("clinic-123", raw_client=mock_raw)
    client.table("appointments").update({"status": "completed"})

    mock_table.update.assert_called_with({"status": "completed"})
    mock_update.eq.assert_called_with("clinic_id", "clinic-123")


def test_update_rejects_modifying_clinic_id():
    mock_raw = MagicMock()
    client = TenantScopedClient("clinic-123", raw_client=mock_raw)

    with pytest.raises(TenantIsolationError) as exc:
        client.table("appointments").update({"clinic_id": "clinic-999"})
    assert "Cross-tenant UPDATE attempted" in str(exc.value)


def test_delete_auto_injects_clinic_id_filter():
    mock_raw = MagicMock()
    mock_table = MagicMock()
    mock_delete = MagicMock()
    mock_raw.table.return_value = mock_table
    mock_table.delete.return_value = mock_delete

    client = TenantScopedClient("clinic-123", raw_client=mock_raw)
    client.table("lab_reports").delete()

    mock_delete.eq.assert_called_with("clinic_id", "clinic-123")
