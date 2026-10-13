"""Several leave rows on one day (Kriya Ketu bug register D-11, D-19).

D-19  get_available_slots() and the OPD walk-in check read only the FIRST
      doctor_leaves row for a date, from an unordered query. A doctor with a
      half-day row and a later full-day row (or morning + evening halves) could
      be offered to patients for a day they are away.
D-11  Nothing stopped the same leave being saved twice (double click, overlapping
      ranges), which is how a day collects several rows in the first place.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import leave_blocked_sessions

BOTH = {"morning", "evening"}


@pytest.mark.parametrize("rows, expected", [
    ([], set()),
    ([{"leave_type": "full"}], BOTH),
    ([{"leave_type": "half_morning"}], {"morning"}),
    ([{"leave_type": "half_evening"}], {"evening"}),
    ([{"leave_type": "half_morning"}, {"leave_type": "full"}], BOTH),
    ([{"leave_type": "half_morning"}, {"leave_type": "half_evening"}], BOTH),
    ([{"leave_type": "half_morning"}, {"leave_type": "half_morning"}], {"morning"}),
    # A legacy value stored before leave_type was validated is treated as a full
    # day: showing the doctor as away is safer than sending patients to them.
    ([{"leave_type": "full_day"}], BOTH),
    ([{"leave_type": None}], BOTH),
])
def test_leave_rows_combine(rows, expected):
    assert leave_blocked_sessions(rows) == expected


class _Q:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return MagicMock(data=self.rows)


@pytest.mark.asyncio
async def test_slots_refused_when_any_row_is_a_full_day():
    from app import database

    tables = {"hospital_holidays": [], "appointments": [],
              "doctor_leaves": [{"leave_type": "half_morning"}, {"leave_type": "full"}]}
    db = MagicMock()
    db.table.side_effect = lambda name: MagicMock(select=lambda *_a, **_k: _Q(tables[name]))
    doc = {"name": "Dr. A", "available_days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
           "morning_slots": ["09:00"], "evening_slots": ["17:00"]}
    with patch.object(database, "supabase", db), \
         patch.object(database, "get_doctor_by_name", new_callable=AsyncMock, return_value=doc):
        slots, reason = await database.get_available_slots("c-1", "Dr. A", "2035-06-13")
    assert (slots, reason) == ([], "doctor_on_leave")


class _Rec:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return MagicMock(data=self.rows)


class _Upd:
    """update(...).eq(col, val)... recorder."""

    def __init__(self, payload, log):
        self.entry = {"payload": payload, "eq": {}}
        log.append(self.entry)

    def eq(self, col, val):
        self.entry["eq"][col] = val
        return self

    def execute(self):
        return MagicMock(data=[{}])


async def _create(existing, leave_type="full", insert_error=None):
    from app.routers.admin import AdminUser, LeaveCreate, create_leave

    inserted, updated = [], []
    db = MagicMock()

    def _insert(rows):
        if insert_error:
            raise insert_error
        inserted.extend(rows)
        return _Rec(rows)

    def _table(name):
        t = MagicMock()
        t.select.side_effect = lambda *_a, **_k: _Rec(existing)
        t.insert.side_effect = _insert
        t.update.side_effect = lambda payload: _Upd(payload, updated)
        return t

    db.table.side_effect = _table
    with patch("app.routers.admin.supabase", db), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock), \
         patch("app.routers.admin.spawn_background_task", MagicMock(side_effect=lambda c, **_k: c.close())), \
         patch("app.utils.helpers.today_ist", return_value=date(2035, 1, 1)):
        res = await create_leave(
            leave=LeaveCreate(doctor_name="Dr. A", leave_date=date(2035, 6, 10),
                              end_date=date(2035, 6, 12), leave_type=leave_type),
            request=None, clinic_id="c-1",
            user=AdminUser("admin", role="clinic_admin", clinic_id="c-1", user_id="u-1"),
        )
    return inserted, updated, res


@pytest.mark.asyncio
async def test_create_leave_skips_dates_already_covered():
    inserted, updated, _ = await _create([{"leave_date": "2035-06-11", "leave_type": "full"}])
    assert [r["leave_date"] for r in inserted] == ["2035-06-10", "2035-06-12"]
    assert updated == []


@pytest.mark.asyncio
async def test_full_day_over_an_existing_half_day_upgrades_that_row():
    # doctor_leaves may still carry the pre-tenancy UNIQUE(doctor_name, leave_date),
    # so a second row for the date would fail: the existing row becomes a full day.
    inserted, updated, _ = await _create([{"leave_date": "2035-06-11", "leave_type": "half_morning"}])
    assert [r["leave_date"] for r in inserted] == ["2035-06-10", "2035-06-12"]
    assert updated == [{"payload": {"leave_type": "full"},
                        "eq": {"clinic_id": "c-1", "doctor_name": "Dr. A", "leave_date": "2035-06-11"}}]


@pytest.mark.asyncio
async def test_other_half_of_the_day_makes_it_a_full_day():
    inserted, updated, _ = await _create([{"leave_date": "2035-06-11", "leave_type": "half_morning"}],
                                         leave_type="half_evening")
    assert updated[0]["payload"] == {"leave_type": "full"}
    assert "2035-06-11" not in [r["leave_date"] for r in inserted]


@pytest.mark.asyncio
async def test_create_leave_when_every_date_exists_inserts_nothing_and_says_so():
    existing = [{"leave_date": d, "leave_type": "full"} for d in ("2035-06-10", "2035-06-11", "2035-06-12")]
    inserted, updated, res = await _create(existing)
    assert inserted == [] and updated == []
    assert res["count"] == 0 and res["already_on_leave"] == ["2035-06-10", "2035-06-11", "2035-06-12"]


@pytest.mark.asyncio
async def test_same_doctor_name_in_another_clinic_gets_a_clear_409():
    """D-20: the pre-tenancy UNIQUE(doctor_name, leave_date) spans all clinics."""
    from fastapi import HTTPException

    err = Exception('duplicate key value violates unique constraint "doctor_leaves_doctor_name_leave_date_key"')
    with pytest.raises(HTTPException) as exc:
        await _create([], insert_error=err)
    assert exc.value.status_code == 409
    assert "another clinic" in exc.value.detail
