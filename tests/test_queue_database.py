"""Tests for queue/token database helpers."""

import importlib
import sys
import pytest
from unittest.mock import MagicMock, patch

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import app.database as app_db
if not hasattr(app_db, "check_in_appointment"):
    importlib.reload(app_db)


@pytest.fixture(autouse=True)
def restore_real_database_module():
    if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
        del sys.modules["app.database"]
        importlib.import_module("app.database")


@pytest.mark.asyncio
async def test_check_in_appointment_assigns_next_token():
    import app.database as db
    mock_sb = MagicMock()
    # First select: appt lookup
    mock_appt = [{"doctor_name": "Dr. Rao", "appointment_date": "2026-08-09"}]
    # Second select: max token query returns max 3
    mock_max = [{"token_number": 3}]
    # Third: update return
    mock_updated = [{"id": "appt-1", "token_number": 4, "queue_status": "waiting"}]

    mock_select = mock_sb.table.return_value.select.return_value
    # Make .eq chainable
    mock_select.eq.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.side_effect = [
        MagicMock(data=mock_appt),
        MagicMock(data=mock_max),
    ]

    mock_update = mock_sb.table.return_value.update.return_value
    mock_update.eq.return_value = mock_update
    mock_update.execute.return_value = MagicMock(data=mock_updated)

    with patch.object(db, "supabase", mock_sb):
        result = await db.check_in_appointment("clinic-1", "appt-1")

    assert result["token_number"] == 4
    assert result["queue_status"] == "waiting"


@pytest.mark.asyncio
async def test_check_in_appointment_first_token_of_day_is_1():
    import app.database as db
    mock_sb = MagicMock()
    mock_appt = [{"doctor_name": "Dr. Rao", "appointment_date": "2026-08-09"}]
    mock_max = []
    mock_updated = [{"id": "appt-1", "token_number": 1, "queue_status": "waiting"}]

    mock_select = mock_sb.table.return_value.select.return_value
    mock_select.eq.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.side_effect = [
        MagicMock(data=mock_appt),
        MagicMock(data=mock_max),
    ]

    mock_update = mock_sb.table.return_value.update.return_value
    mock_update.eq.return_value = mock_update
    mock_update.execute.return_value = MagicMock(data=mock_updated)

    with patch.object(db, "supabase", mock_sb):
        result = await db.check_in_appointment("clinic-1", "appt-1")

    assert result["token_number"] == 1


@pytest.mark.asyncio
async def test_get_patient_queue_status_not_checked_in():
    import app.database as db
    mock_sb = MagicMock()
    mock_select = mock_sb.table.return_value.select.return_value
    mock_select.eq.return_value = mock_select
    mock_select.execute.return_value = MagicMock(
        data=[{"id": "appt-1", "token_number": None, "doctor_name": "Dr. Rao"}]
    )

    with patch.object(db, "supabase", mock_sb):
        result = await db.get_patient_queue_status("clinic-1", "+919876543210", "2026-08-09")

    assert result["checked_in"] is False


@pytest.mark.asyncio
async def test_call_next_patient_advances_queue():
    import app.database as db
    mock_sb = MagicMock()
    mock_update = mock_sb.table.return_value.update.return_value
    mock_update.eq.return_value = mock_update
    mock_update.execute.return_value = MagicMock(
        data=[{"id": "appt-2", "token_number": 2, "queue_status": "in_consultation"}]
    )

    mock_waiting = [{"id": "appt-2", "token_number": 2, "queue_status": "waiting"}]
    mock_select = mock_sb.table.return_value.select.return_value
    mock_select.eq.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.return_value = MagicMock(data=mock_waiting)

    with patch.object(db, "supabase", mock_sb):
        result = await db.call_next_patient("clinic-1", "Dr. Rao", "2026-08-09")

    assert result["id"] == "appt-2"


# ── Lab-test check-in ────────────────────────────────────────────────────────
# A lab-test booking carries doctor_name = NULL by design (migration 039).
# `.eq("doctor_name", None)` never matches a NULL row in PostgREST, so the
# max-token lookup found nothing and every sample-collection walk-in was
# handed token #1. The queue key for these rows is branch+date instead.


def _lab_mocks(appt_row, max_rows, updated_row):
    mock_sb = MagicMock()
    mock_select = mock_sb.table.return_value.select.return_value
    for chained in ("eq", "is_", "order", "limit"):
        getattr(mock_select, chained).return_value = mock_select
    mock_select.execute.side_effect = [
        MagicMock(data=appt_row),
        MagicMock(data=max_rows),
    ]
    mock_update = mock_sb.table.return_value.update.return_value
    mock_update.eq.return_value = mock_update
    mock_update.execute.return_value = MagicMock(data=updated_row)
    return mock_sb, mock_select


@pytest.mark.asyncio
async def test_lab_check_in_continues_the_branch_queue_not_restarting_at_one():
    import app.database as db

    mock_sb, mock_select = _lab_mocks(
        [{"doctor_name": None, "branch_id": "branch-1", "appointment_date": "2026-09-08"}],
        [{"token_number": 7}],
        [{"id": "lab-1", "token_number": 8, "queue_status": "waiting"}],
    )

    with patch.object(db, "supabase", mock_sb):
        result = await db.check_in_appointment("clinic-1", "lab-1")

    assert result["token_number"] == 8, "lab tokens must continue the branch queue"
    # The NULL doctor must be matched with is_(), never eq(None).
    assert ("doctor_name", "null") in [c.args for c in mock_select.is_.call_args_list]
    assert ("doctor_name", None) not in [c.args for c in mock_select.eq.call_args_list]
    assert ("branch_id", "branch-1") in [c.args for c in mock_select.eq.call_args_list]


@pytest.mark.asyncio
async def test_lab_check_in_single_branch_clinic_matches_null_branch():
    """A single-centre clinic leaves branch_id NULL; the queue lookup has to
    match those rows too or it degrades to the same token-#1 bug."""
    import app.database as db

    mock_sb, mock_select = _lab_mocks(
        [{"doctor_name": None, "branch_id": None, "appointment_date": "2026-09-08"}],
        [{"token_number": 2}],
        [{"id": "lab-2", "token_number": 3, "queue_status": "waiting"}],
    )

    with patch.object(db, "supabase", mock_sb):
        result = await db.check_in_appointment("clinic-1", "lab-2")

    assert result["token_number"] == 3
    is_calls = [c.args for c in mock_select.is_.call_args_list]
    assert ("doctor_name", "null") in is_calls
    assert ("branch_id", "null") in is_calls


@pytest.mark.asyncio
async def test_consultation_check_in_still_keys_on_the_doctor():
    """Regression guard: the lab branch must not change consultation queues."""
    import app.database as db

    mock_sb, mock_select = _lab_mocks(
        [{"doctor_name": "Dr. Rao", "branch_id": "branch-1", "appointment_date": "2026-09-08"}],
        [{"token_number": 4}],
        [{"id": "appt-9", "token_number": 5, "queue_status": "waiting"}],
    )

    with patch.object(db, "supabase", mock_sb):
        result = await db.check_in_appointment("clinic-1", "appt-9")

    assert result["token_number"] == 5
    eq_calls = [c.args for c in mock_select.eq.call_args_list]
    assert ("doctor_name", "Dr. Rao") in eq_calls
    # branch_id must NOT narrow a doctor queue — a doctor's token run is per
    # doctor per day regardless of which room they sit in.
    assert ("branch_id", "branch-1") not in eq_calls
    assert mock_select.is_.call_args_list == []
