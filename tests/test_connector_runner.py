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
    
    # CAS logic simulation: if currently locked within lease, update matches 0 rows.
    now = datetime.now(timezone.utc)
    is_locked = False
    if locked_at_iso is not None:
        dt = datetime.fromisoformat(locked_at_iso.replace("Z", "+00:00"))
        if (now - dt) < timedelta(minutes=5):
            is_locked = True
    update_data = [] if is_locked else [{"id": "conn-1"}]

    mock_exec = MagicMock(data=update_data)
    mock_table.update.return_value.eq.return_value.execute.return_value = mock_exec
    mock_table.update.return_value.eq.return_value.or_.return_value.execute.return_value = mock_exec
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
    mock_table.update.assert_called_once()


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


@pytest.mark.asyncio
async def test_run_connector_dry_run_includes_masked_sample_excluded_from_audit_log():
    from connectors.runner import run_connector, CONNECTOR_REGISTRY, _mask_sample_name, _mask_phone
    from connectors.base import ReportMetadata

    class _FakeConnector:
        def __init__(self, **kwargs):
            pass

        async def authenticate(self):
            return True

        async def fetch_new_reports(self):
            return [
                ReportMetadata(
                    patient_name=f"Patient {i}",
                    patient_phone="+919999999999",
                    report_name=f"CBC Report {i}",
                    report_type="lab",
                    external_report_id=f"ext-{i}",
                    vam_id=f"VAM-{i}",
                )
                for i in range(7)
            ]

        async def cleanup(self):
            pass

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.eq.return_value.is_.return_value.single.return_value.execute.return_value = MagicMock(
        data={
            "id": "conn-1",
            "clinic_id": "clinic-2",
            "is_enabled": True,
            "config": {"username": "labadmin", "password": "plaintext-dev-only"},
        }
    )

    with patch("connectors.runner.supabase", mock_sb), \
         patch.dict(CONNECTOR_REGISTRY, {"mocdoc": _FakeConnector}), \
         patch("connectors.runner.acquire_connector_lock", new_callable=AsyncMock, return_value=(True, 0)), \
         patch("connectors.runner.release_connector_lock", new_callable=AsyncMock):
        result = await run_connector(clinic_id="clinic-2", dry_run=True)

    assert result["run_status"] == "dry_run"
    assert len(result["sample"]) == 5
    assert result["sample"][0]["patient_name_masked"] == _mask_sample_name("Patient 0")
    assert result["sample"][0]["patient_phone_masked"] == _mask_phone("+919999999999")
    assert result["sample"][0]["vam_id"] == "VAM-0"
    assert result["sample"][0]["report_name"] == "CBC Report 0"

    insert_payload = mock_table.insert.call_args[0][0]
    assert "sample" not in insert_payload
    assert insert_payload["run_status"] == "dry_run"



def test_renew_lock_only_touches_locks_this_process_holds():
    """Heartbeat must not resurrect a lock another worker owns.

    A 17-report run takes ~6 min > LOCK_LEASE (5 min), so the lease is renewed
    per report. Renewing a lock we do not hold would let two workers keep each
    other's leases alive forever.
    """
    import asyncio
    from unittest.mock import MagicMock, patch
    from connectors.runner import renew_connector_lock, _locks_held_by_this_process

    mock_sb = MagicMock()
    with patch("connectors.runner.supabase", mock_sb):
        _locks_held_by_this_process.discard("not-ours")
        asyncio.run(renew_connector_lock("not-ours"))
        assert mock_sb.table.call_count == 0

        _locks_held_by_this_process.add("ours")
        try:
            asyncio.run(renew_connector_lock("ours"))
            assert mock_sb.table.call_count == 1
        finally:
            _locks_held_by_this_process.discard("ours")


def _dry_run_tables():
    """supabase mock that keeps each table's calls separate."""
    tables = {}

    def table(name):
        if name not in tables:
            t = MagicMock()
            t.select.return_value.eq.return_value.eq.return_value.is_.return_value.single.return_value.execute.return_value = MagicMock(
                data={
                    "id": "conn-1",
                    "clinic_id": "clinic-2",
                    "is_enabled": True,
                    "config": {"username": "labadmin", "password": "plaintext-dev-only"},
                }
            )
            tables[name] = t
        return tables[name]

    sb = MagicMock()
    sb.table.side_effect = table
    return sb, tables


async def _run(dry_run):
    from connectors.runner import run_connector, CONNECTOR_REGISTRY

    class _FakeConnector:
        def __init__(self, **kwargs):
            pass

        async def authenticate(self):
            return True

        async def fetch_new_reports(self):
            return []

        async def cleanup(self):
            pass

    sb, tables = _dry_run_tables()
    with patch("connectors.runner.supabase", sb), \
         patch.dict(CONNECTOR_REGISTRY, {"mocdoc": _FakeConnector}), \
         patch("connectors.runner.acquire_connector_lock", new_callable=AsyncMock, return_value=(True, 0)), \
         patch("connectors.runner.release_connector_lock", new_callable=AsyncMock):
        result = await run_connector(clinic_id="clinic-2", dry_run=dry_run)
    return result, tables


@pytest.mark.asyncio
async def test_dry_run_does_not_stamp_last_run_at():
    """A Test Connection is not a poll.

    Stamping last_run_at made the dashboard show "Disabled - Last run: 13:02"
    and pushed the next real poll a full interval into the future.
    """
    result, tables = await _run(dry_run=True)
    assert result["run_status"] == "dry_run"
    assert tables["integration_connectors"].update.call_count == 0
    assert tables["connector_audit_log"].insert.call_count == 1


@pytest.mark.asyncio
async def test_real_run_still_stamps_last_run_at():
    result, tables = await _run(dry_run=False)
    assert result["run_status"] == "success"
    payload = tables["integration_connectors"].update.call_args[0][0]
    assert "last_run_at" in payload and "last_success_at" in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {"username": "your_mocdoc_username", "password": "your_password"},
        {"username": "CHANGEME", "password": "real-looking-secret"},
        {"username": "labadmin", "password": "changeme"},
    ],
)
async def test_placeholder_credentials_are_skipped_not_alerted(config):
    """Seed rows with template credentials must not page anyone.

    A left-over demo connector would otherwise fail authentication and fire an
    admin WhatsApp alert on every single poll.
    """
    from connectors.runner import run_connector, CONNECTOR_REGISTRY

    class _NeverReached:
        def __init__(self, **kwargs):
            raise AssertionError("connector was constructed with placeholder credentials")

    sb, tables = _dry_run_tables()
    tables_row = {
        "id": "conn-seed",
        "clinic_id": "clinic-seed",
        "is_enabled": True,
        "config": config,
    }
    sb.table("integration_connectors").select.return_value.eq.return_value.eq.return_value.is_.return_value.single.return_value.execute.return_value = MagicMock(
        data=tables_row
    )

    alert = AsyncMock()
    with patch("connectors.runner.supabase", sb), \
         patch.dict(CONNECTOR_REGISTRY, {"mocdoc": _NeverReached}), \
         patch("connectors.runner.send_admin_alert", alert), \
         patch("connectors.runner.acquire_connector_lock", new_callable=AsyncMock, return_value=(True, 0)), \
         patch("connectors.runner.release_connector_lock", new_callable=AsyncMock):
        result = await run_connector(clinic_id="clinic-seed")

    assert result["run_status"] == "skipped"
    assert "placeholder" in (result["error_message"] or "").lower()
    assert alert.call_count == 0
    assert tables["integration_connectors"].update.call_count == 0


@pytest.mark.asyncio
async def test_real_credentials_are_not_mistaken_for_placeholders():
    result, _ = await _run(dry_run=False)
    assert result["run_status"] == "success"
