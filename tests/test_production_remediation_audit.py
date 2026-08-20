"""Tests verifying complete remediation of production issues:
1. Multi-tenant conversation resolution and idempotency (Issue A)
2. Branch form reset in admin UI (Issue B)
3. Safe patient creation concurrency
4. Multi-tenant database constraint semantics
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from app.database import (
    get_or_create_conversation,
    create_conversation,
    get_conversation,
    create_patient,
    get_patient_by_phone,
)


@pytest.mark.asyncio
async def test_get_or_create_conversation_existing():
    """Verify get_or_create_conversation returns existing conversation without re-creating."""
    import app.database
    mock_conv = {
        "id": "conv-123",
        "clinic_id": "clinic-uuid-1",
        "phone": "+919876543210",
        "state": "idle",
        "context": {},
    }

    mock_sb = MagicMock()
    mock_t = MagicMock()
    mock_t.select.return_value = mock_t
    mock_t.eq.return_value = mock_t
    mock_res = MagicMock()
    mock_res.data = [mock_conv]
    mock_t.execute.return_value = mock_res
    mock_sb.table.return_value = mock_t

    with patch("app.database.supabase", mock_sb):
        conv = await app.database.get_or_create_conversation("clinic-uuid-1", "+919876543210")
        assert conv["id"] == "conv-123"
        assert conv["phone"] == "+919876543210"
        assert conv["clinic_id"] == "clinic-uuid-1"
        mock_t.insert.assert_not_called()


@pytest.mark.asyncio
async def test_get_or_create_conversation_concurrency_race_recovery():
    """Verify get_or_create_conversation recovers cleanly when concurrent request inserts first (Issue A root cause)."""
    import app.database
    mock_conv_existing = {
        "id": "conv-concurrent-created",
        "clinic_id": "clinic-uuid-1",
        "phone": "+919876543210",
        "state": "idle",
    }

    mock_sb = MagicMock()
    mock_t = MagicMock()
    mock_t.select.return_value = mock_t
    mock_t.eq.return_value = mock_t
    
    # First select returns empty, insert raises unique constraint error, recovery select returns row
    res_empty = MagicMock(data=[])
    res_existing = MagicMock(data=[mock_conv_existing])
    mock_t.execute.side_effect = [
        res_empty,     # 1st get_conversation: None
        res_existing,  # Recovery get_conversation: returns conv
    ]
    mock_t.insert.return_value.execute.side_effect = Exception(
        'duplicate key value violates unique constraint "conversations_clinic_phone_key"'
    )
    mock_sb.table.return_value = mock_t

    with patch("app.database.supabase", mock_sb):
        conv = await app.database.get_or_create_conversation("clinic-uuid-1", "+919876543210")
        assert conv["id"] == "conv-concurrent-created"
        assert conv["phone"] == "+919876543210"


@pytest.mark.asyncio
async def test_create_patient_concurrency_race_recovery():
    """Verify create_patient handles duplicate key conflicts gracefully and returns existing patient."""
    import app.database
    mock_patient = {
        "id": "patient-123",
        "clinic_id": "clinic-uuid-1",
        "phone": "+919876543210",
        "name": "Ravi Kumar",
    }

    mock_sb = MagicMock()
    mock_t = MagicMock()
    mock_t.select.return_value = mock_t
    mock_t.eq.return_value = mock_t

    res_patient = MagicMock(data=[mock_patient])
    mock_t.execute.return_value = res_patient
    mock_t.insert.return_value.execute.side_effect = Exception(
        'duplicate key value violates unique constraint "patients_clinic_phone_key"'
    )
    mock_sb.table.return_value = mock_t

    with patch("app.database.supabase", mock_sb):
        patient = await app.database.create_patient("clinic-uuid-1", "+919876543210", "Ravi Kumar")
        assert patient["id"] == "patient-123"
        assert patient["name"] == "Ravi Kumar"


def test_admin_index_html_branch_elements_exist():
    """Verify admin/index.html contains the actual DOM element IDs targeted by resetBranchForm and saveBranch."""
    import re
    from pathlib import Path

    html_path = Path(__file__).parent.parent / "admin" / "index.html"
    assert html_path.exists()

    content = html_path.read_text(encoding="utf-8")

    # Form fields that MUST exist in HTML
    required_ids = [
        "f-branchId",
        "f-branchLocality",
        "f-branchAddr",
        "f-branchLandmark",
        "f-branchMaps",
        "f-branchPhone",
        "f-branchDiag",
        "f-branchOrder",
        "branchFormTitle",
    ]

    for element_id in required_ids:
        assert f'id="{element_id}"' in content, f"Missing required element ID: {element_id}"

    # Stale IDs that MUST NOT be referenced in resetBranchForm without null checks
    # Specifically ensure the resetBranchForm function body doesn't do document.getElementById('f-branchName').value
    assert "document.getElementById('f-branchName').value" not in content
    assert "document.getElementById('f-branchShort').value" not in content


def test_migration_035_syntax_and_structure():
    """Verify migration 035 exists and contains proper composite unique constraints for multi-tenancy."""
    from pathlib import Path

    mig_path = Path(__file__).parent.parent / "migrations" / "035_fix_multi_tenant_unique_constraints.sql"
    assert mig_path.exists()

    sql = mig_path.read_text(encoding="utf-8")

    # Check for dropping old single-column constraint and adding composite constraints
    assert "conversations_phone_key" in sql
    assert "conversations_clinic_phone_key" in sql
    assert "UNIQUE (clinic_id, phone)" in sql or "UNIQUE(clinic_id, phone)" in sql
    assert "patients_phone_key" in sql
    assert "patients_clinic_phone_key" in sql
