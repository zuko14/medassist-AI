"""Regression: a worker that LOSES the connector lock race must not release it.

Production symptom (Accumx, 2026-09-02): seven concurrent runs of the same
MocDoc connector, each ~6 minutes, all scraping one third-party account at
once, with runs starting seconds apart despite poll_interval_minutes = 5.

    branch 18c9df25 started: 12:38:55 12:39:04 12:39:12 12:39:40 12:39:43
                             12:40:04 12:40:17   (durations 300-415s)

The advisory lock and its CAS predicate were both correct. The defect was
ownership on the way OUT:

  * _run_connector() bound `connector_id` BEFORE attempting the lock, and its
    finally-block released on every exit path — including the early return
    taken when acquisition FAILED. A loser therefore cleared the winner's
    locked_at, and the next 60s tick walked straight in.
  * release_connector_lock() cleared locked_at unconditionally, with no check
    that this process was the owner.
  * Every process used the same literal worker id ("worker-1"), so locked_by
    identified nobody and an ownership check was impossible.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import connectors.runner as runner


def test_each_process_has_a_distinct_lock_owner_id():
    """'worker-1' was hardcoded for every process, so locked_by named nobody."""
    assert runner.CONNECTOR_WORKER_ID.startswith("conn_")
    assert runner.CONNECTOR_WORKER_ID != "worker-1"
    sig = inspect.signature(runner.acquire_connector_lock)
    assert sig.parameters["worker_id"].default is None, (
        "A shared default owner id makes every lock look like ours"
    )


@pytest.mark.asyncio
async def test_release_is_a_noop_when_this_process_never_held_the_lock():
    """The exact cascade: a losing worker must not free the winner's lock."""
    runner._locks_held_by_this_process.discard("conn-1")
    sb_mock = AsyncMock()

    with patch("connectors.runner.sb", sb_mock):
        await runner.release_connector_lock("conn-1")

    sb_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_is_scoped_to_this_process_owner_id():
    """Even when we believe we hold it, the UPDATE must prove ownership."""
    runner._locks_held_by_this_process.add("conn-1")
    table = MagicMock()
    supa = MagicMock()
    supa.table.return_value = table

    with patch("connectors.runner.sb", AsyncMock()), patch(
        "connectors.runner.supabase", supa
    ):
        await runner.release_connector_lock("conn-1")

    eq_args = [c.args for c in table.update.return_value.eq.call_args_list]
    eq_args += [
        c.args for c in table.update.return_value.eq.return_value.eq.call_args_list
    ]
    assert ("locked_by", runner.CONNECTOR_WORKER_ID) in eq_args, (
        f"release must filter on locked_by; predicates seen: {eq_args}"
    )
    assert "conn-1" not in runner._locks_held_by_this_process


@pytest.mark.asyncio
async def test_renew_stops_when_the_lease_was_taken_over():
    """Renewing a lease we no longer own would hide the takeover from the TTL."""
    runner._locks_held_by_this_process.add("conn-2")
    with patch(
        "connectors.runner.sb", AsyncMock(return_value=MagicMock(data=[]))
    ), patch("connectors.runner.supabase", MagicMock()):
        await runner.renew_connector_lock("conn-2")
    assert "conn-2" not in runner._locks_held_by_this_process, (
        "A lost lease must be dropped, not silently renewed"
    )


def test_connector_id_is_bound_only_after_the_lock_is_won():
    """connector_id drives the finally-block release. Binding it before the
    acquisition is precisely what let a loser release the winner's lock."""
    src = inspect.getsource(runner._run_connector)
    assert 'candidate_id = connector_row.get("id")' in src, (
        "The row id must land in a candidate variable first"
    )
    acquire_at = src.index("acquire_connector_lock(candidate_id)")
    bind_at = src.index("connector_id = candidate_id")
    assert acquire_at < bind_at, (
        "connector_id must be assigned only AFTER a successful acquisition"
    )
    losing = src[acquire_at:bind_at]
    assert 'summary["run_status"] = "locked"' in losing and "return summary" in losing


@pytest.mark.asyncio
async def test_a_losing_run_leaves_the_winners_lock_intact_end_to_end():
    """Full path: acquisition fails -> run returns 'locked' -> no release."""
    row = {
        "id": "conn-9",
        "clinic_id": "clinic-1",
        "is_enabled": True,
        "config": {"username": "u", "password": "p"},
    }
    supa = MagicMock()
    supa.table.return_value.select.return_value.eq.return_value.eq.return_value.is_.return_value.single.return_value.execute.return_value = MagicMock(
        data=row
    )
    runner._locks_held_by_this_process.discard("conn-9")
    released = AsyncMock()

    with patch("connectors.runner.supabase", supa), patch(
        "connectors.runner.acquire_connector_lock", AsyncMock(return_value=(False, 3))
    ), patch("connectors.runner.release_connector_lock", released):
        result = await runner.run_connector(clinic_id="clinic-1")

    assert result["run_status"] == "locked"
    released.assert_not_awaited()
