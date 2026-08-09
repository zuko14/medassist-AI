"""Tests for family member database helpers."""

import importlib
import sys
import pytest
from unittest.mock import MagicMock, patch

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import app.database as app_db
if not hasattr(app_db, "get_family_members"):
    importlib.reload(app_db)


@pytest.fixture(autouse=True)
def restore_real_database_module():
    if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
        del sys.modules["app.database"]
        importlib.import_module("app.database")


@pytest.mark.asyncio
async def test_get_family_members_returns_list():
    import app.database as db
    mock_sb = MagicMock()
    mock_select = mock_sb.table.return_value.select.return_value
    mock_select.eq.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.execute.return_value = MagicMock(
        data=[
            {"id": "fam-1", "full_name": "Priya Sharma", "relationship": "Daughter"},
            {"id": "fam-2", "full_name": "Ramesh Sharma", "relationship": "Father"},
        ]
    )

    with patch.object(db, "supabase", mock_sb):
        members = await db.get_family_members("clinic-1", "+919876543210")

    assert len(members) == 2
    assert members[0]["full_name"] == "Priya Sharma"


@pytest.mark.asyncio
async def test_add_family_member_inserts_record():
    import app.database as db
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "fam-3", "full_name": "Aarav Sharma", "relationship": "Son"}]
    )

    with patch.object(db, "supabase", mock_sb):
        result = await db.add_family_member(
            "clinic-1",
            "+919876543210",
            full_name="Aarav Sharma",
            relationship="Son",
        )

    assert result["full_name"] == "Aarav Sharma"
