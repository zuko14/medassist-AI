"""Tests for family member database helpers."""

import pytest
from unittest.mock import MagicMock, patch

from app.database import get_family_members, add_family_member


@pytest.mark.asyncio
async def test_get_family_members_returns_list():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
        data=[
            {"id": "fam-1", "full_name": "Priya Sharma", "relationship": "Daughter"},
            {"id": "fam-2", "full_name": "Ramesh Sharma", "relationship": "Father"},
        ]
    )

    with patch("app.database.supabase", mock_sb):
        members = await get_family_members("clinic-1", "+919876543210")

    assert len(members) == 2
    assert members[0]["full_name"] == "Priya Sharma"


@pytest.mark.asyncio
async def test_add_family_member_inserts_record():
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "fam-3", "full_name": "Aarav Sharma", "relationship": "Son"}]
    )

    with patch("app.database.supabase", mock_sb):
        result = await add_family_member(
            "clinic-1",
            "+919876543210",
            full_name="Aarav Sharma",
            relationship="Son",
        )

    assert result["full_name"] == "Aarav Sharma"
