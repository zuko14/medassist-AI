# tests/test_connector_runner.py
"""Tests for connectors/runner.py's distributed advisory lock: acquire,
deny-with-remaining-TTL, expiry, and release."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_supabase_with_locked_at(locked_at_iso):
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "conn-1", "locked_at": locked_at_iso}]
    )
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    return mock_sb, mock_table


@pytest.mark.asyncio
async def test_acquire_lock_granted_when_no_existing_lock():
    from connectors.runner import acquire_connector_lock

    mock_sb, mock_table = _mock_supabase_with_locked_at(None)
    with patch("connectors.runner.supabase", mock_sb):
        acquired, remaining = await acquire_connector_lock("conn-1")

    assert acquired is True
    assert remaining == 0
    mock_table.update.assert_called_once()


@pytest.mark.asyncio
async def test_acquire_lock_denied_with_remaining_ttl_when_recently_locked():
    from connectors.runner import acquire_connector_lock

    locked_two_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    mock_sb, mock_table = _mock_supabase_with_locked_at(locked_two_min_ago)
    with patch("connectors.runner.supabase", mock_sb):
        acquired, remaining = await acquire_connector_lock("conn-1")

    assert acquired is False
    assert remaining == 3  # 5-minute lease minus ~2 elapsed, rounded up
    mock_table.update.assert_not_called()


@pytest.mark.asyncio
async def test_acquire_lock_granted_after_ttl_expires():
    from connectors.runner import acquire_connector_lock

    locked_six_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    mock_sb, mock_table = _mock_supabase_with_locked_at(locked_six_min_ago)
    with patch("connectors.runner.supabase", mock_sb):
        acquired, remaining = await acquire_connector_lock("conn-1")

    assert acquired is True
    assert remaining == 0


@pytest.mark.asyncio
async def test_release_connector_lock_clears_fields_and_untracks():
    from connectors.runner import acquire_connector_lock, release_connector_lock, _locks_held_by_this_process

    mock_sb, mock_table = _mock_supabase_with_locked_at(None)
    with patch("connectors.runner.supabase", mock_sb):
        await acquire_connector_lock("conn-1")
        assert "conn-1" in _locks_held_by_this_process

        await release_connector_lock("conn-1")

    update_call = mock_table.update.call_args_list[-1][0][0]
    assert update_call == {"locked_at": None, "locked_by": None}
    assert "conn-1" not in _locks_held_by_this_process


def test_lifespan_shutdown_calls_release_all_locks_held():
    """Regression guard: FastAPI's shutdown path must release any connector
    lock this process holds, or a killed web worker leaves a stale lock."""
    import inspect
    from app import main

    source = inspect.getsource(main.lifespan)
    shutdown_section = source.split("# Shutdown", 1)[1]
    assert "release_all_locks_held" in shutdown_section


def test_scheduled_mode_releases_locks_on_sigterm():
    """Regression guard: the connector worker's scheduled mode must release
    its locks on SIGTERM (the signal Render sends on redeploy/stop), not
    just on KeyboardInterrupt."""
    import inspect
    from connectors import runner

    source = inspect.getsource(runner.start_scheduled_mode)
    assert "signal.signal(signal.SIGTERM" in source
    assert "release_all_locks_held" in source
