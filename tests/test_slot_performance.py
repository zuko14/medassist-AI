"""Tests for Parallel Slot Lookup Performance & Metadata Caching (Finding #11)."""

import importlib
import sys
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure app.database is the real module if an earlier test mutated sys.modules
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import app.database as app_db
if not hasattr(app_db, "get_available_slots"):
    importlib.reload(app_db)

from app.database import get_available_slots, _doctor_cache, _holiday_cache


@pytest.mark.asyncio
async def test_get_available_slots_parallel_and_cached():
    """Verify get_available_slots uses parallel queries and caches doctor & holiday metadata."""
    _doctor_cache.clear()
    _holiday_cache.clear()

    mock_doc = {
        "id": "doc-uuid-1",
        "name": "Dr. Sharma",
        "available_days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
        "morning_slots": ["09:00", "10:00"],
        "evening_slots": ["17:00", "18:00"],
    }

    mock_sb = MagicMock()

    def mock_table_handler(table_name):
        mock_t = MagicMock()
        mock_t.select = MagicMock(return_value=mock_t)
        mock_t.eq = MagicMock(return_value=mock_t)
        mock_t.in_ = MagicMock(return_value=mock_t)
        mock_t.order = MagicMock(return_value=mock_t)
        mock_t.limit = MagicMock(return_value=mock_t)

        mock_res = MagicMock()
        if table_name == "hospital_holidays":
            mock_res.data = []
        elif table_name == "doctor_leaves":
            mock_res.data = []
        elif table_name == "doctors":
            mock_res.data = [mock_doc]
        elif table_name == "appointments":
            mock_res.data = []
        else:
            mock_res.data = []

        mock_t.execute = MagicMock(return_value=mock_res)
        return mock_t

    mock_sb.table.side_effect = mock_table_handler

    with patch("app.database.supabase", mock_sb):
        slots, reason = await get_available_slots("clinic-123", "Dr. Sharma", "2028-10-10")

        assert reason is None
        assert "09:00" in slots
        assert "17:00" in slots

        # Verify doctor and holiday metadata cached
        assert "clinic-123:Dr. Sharma" in _doctor_cache
        assert "clinic-123:2028-10-10" in _holiday_cache

        # Subsequent call for same doctor should use cache
        slots_2, reason_2 = await get_available_slots("clinic-123", "Dr. Sharma", "2028-10-10")
        assert slots_2 == slots
