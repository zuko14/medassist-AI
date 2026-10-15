"""D-29: Kriya Ketu's date resolution takes a fully written date literally.

The voice receptionist's dates.parse_date (reused read-only) rolls a past day forward, which is right
for a patient booking, but it also dropped an explicit year ("1 oct 2026" -> 2027-10-01) and misread
ISO dates ("2026-10-01" -> 2027-01-10). Ketu now reads ISO, "<day> <month> <year>", "<month> <day>,
<year>" and dd/mm/yyyy literally first; a date without a year keeps the voice behaviour. A literal past
date reaches the actions' own refusals (holiday: already; leave: added here).
"""

import asyncio
from datetime import date, datetime
from unittest.mock import patch

import pytest

from app.assistant import playbooks, resolve
from app.assistant.errors import EmptyPlan
from app.voice.dates import IST

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=IST)


@pytest.mark.parametrize("text, expected", [
    ("add a holiday on 2026-11-02", date(2026, 11, 2)),
    ("Dr Rao on leave on 2026-10-01", date(2026, 10, 1)),       # literal, even in the past
    ("holiday on 1 oct 2026", date(2026, 10, 1)),
    ("holiday on 1st October, 2027", date(2027, 10, 1)),
    ("holiday on Oct 20, 2027", date(2027, 10, 20)),
    ("holiday on 20/10/2026", date(2026, 10, 20)),               # Indian order: day/month/year
    ("holiday on 2026-02-30", None),                             # impossible date: asked, never guessed
    ("holiday on 1 oct", date(2027, 10, 1)),                     # no year: voice behaviour kept
    ("Dr Rao on leave tomorrow", date(2026, 10, 15)),
])
def test_resolve_date_reads_a_full_date_literally(text, expected):
    assert resolve.resolve_date(text, now=NOW) == expected


@pytest.mark.parametrize("text, expected", [
    ("Dr Rao leave 2026-10-20 to 2026-10-22", (date(2026, 10, 20), date(2026, 10, 22))),
    ("leave 20 to 22 oct 2027", (date(2027, 10, 20), date(2027, 10, 22))),
    ("leave 14 oct 2026 to 16 oct 2026", (date(2026, 10, 14), date(2026, 10, 16))),
    ("Dr Rao leave 14 to 16 Oct", (date(2026, 10, 14), date(2026, 10, 16))),   # unchanged
])
def test_resolve_range_reads_full_dates_literally(text, expected):
    assert resolve.resolve_range(text, now=NOW) == expected


def test_a_leave_starting_in_the_past_is_refused_before_any_read():
    async def read(call):
        raise AssertionError("no read for a past leave")

    with patch.object(playbooks, "_now", lambda: NOW), pytest.raises(EmptyPlan, match="past"):
        asyncio.run(playbooks.leave_create_preview(read, {"doctor": "Dr. Rao", "date": "2026-10-01"}))
