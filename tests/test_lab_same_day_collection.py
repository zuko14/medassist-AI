"""Same-day lab collection (Accumax Diagnostics, 2026-09-23).

A patient at 1:51pm with a 07:00-21:00 window was offered Thu/Fri/Sat only:
collection dates started tomorrow regardless of the admin-set hours. Today is
now offered while its window is open, and a date button that has gone stale
(today after closing, or an old message) is refused instead of booked.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.conversation import ConversationManager

IST = ZoneInfo("Asia/Kolkata")
ALL_DAYS = "Mon,Tue,Wed,Thu,Fri,Sat,Sun"
WINDOW = {"start": "07:00", "end": "21:00", "days": ALL_DAYS,
          "sunday_start": "07:00", "sunday_end": "14:00"}
CLINIC = {"id": "clinic-1", "name": "Accumax Diagnostics"}
PHONE = "919876543210"
WED = datetime(2026, 9, 23, 13, 51, tzinfo=IST)  # the reported moment
SUN = datetime(2026, 9, 27, 13, 0, tzinfo=IST)

m = ConversationManager()


def test_today_offered_while_window_open():
    assert m._next_collection_dates(WINDOW, now=WED) == ["2026-09-23", "2026-09-24", "2026-09-25"]


def test_today_dropped_once_window_closed():
    assert m._next_collection_dates(WINDOW, now=WED.replace(hour=21, minute=0))[0] == "2026-09-24"


def test_early_morning_before_opening_still_offers_today():
    assert m._next_collection_dates(WINDOW, now=WED.replace(hour=5))[0] == "2026-09-23"


def test_sunday_uses_sunday_closing_time():
    assert m._next_collection_dates(WINDOW, now=SUN)[0] == "2026-09-27"
    assert m._next_collection_dates(WINDOW, now=SUN.replace(hour=14))[0] == "2026-09-28"
    # Sunday hours need both ends set; otherwise the weekday end applies.
    half = {**WINDOW, "sunday_start": None}
    assert m._next_collection_dates(half, now=SUN.replace(hour=20))[0] == "2026-09-27"


def test_today_skipped_when_not_a_collection_day():
    assert m._next_collection_dates({**WINDOW, "days": "Mon,Tue"}, now=WED) == [
        "2026-09-28", "2026-09-29", "2026-10-05"]


def test_unreadable_days_or_hours_never_hang_or_offer_closed_today():
    assert len(m._next_collection_dates({"end": "21:00", "days": ""}, now=WED)) == 3
    assert len(m._next_collection_dates({"end": "21:00", "days": "monday"}, now=WED)) == 3
    assert m._next_collection_dates({"end": "late", "days": ALL_DAYS}, now=WED)[0] == "2026-09-24"


async def _tap(date_str, window=WINDOW):
    ctx = {"lab_test_id": "t1", "lab_test_name": "HbA1c", "branch_id": None}
    with patch("app.database.get_lab_collection_window", new_callable=AsyncMock, return_value=window), \
         patch.object(m, "_ask_lab_test_patient", new_callable=AsyncMock) as ask, \
         patch.object(m.whatsapp, "send_interactive_buttons", new_callable=AsyncMock) as buttons, \
         patch.object(m.whatsapp, "send_text", new_callable=AsyncMock), \
         patch.object(m, "update_state", new_callable=AsyncMock):
        await m._handle_confirming_collection_date(
            CLINIC, PHONE, "", "", ctx, {"name": "Ravi"}, "en",
            interactive_data={"id": f"labdate_{date_str}"},
        )
    return ctx, ask, buttons


@pytest.mark.asyncio
async def test_stale_past_date_tap_is_refused_and_dates_reoffered():
    yesterday = (datetime.now(IST).date() - timedelta(days=1)).isoformat()
    ctx, ask, buttons = await _tap(yesterday)
    ask.assert_not_called()
    assert ctx["lab_collection_date"] is None
    offered = [b["id"] for b in buttons.call_args.kwargs["buttons"]]
    assert len(offered) == 3 and f"labdate_{yesterday}" not in offered


@pytest.mark.asyncio
async def test_valid_future_tap_proceeds():
    tomorrow = (datetime.now(IST).date() + timedelta(days=1)).isoformat()
    ctx, ask, _ = await _tap(tomorrow)
    ask.assert_awaited_once()
    assert ctx["lab_collection_date"] == tomorrow


@pytest.mark.asyncio
async def test_date_buttons_include_today_when_open():
    """Through the test-selection handler, clock pinned to the reported moment."""
    test = {"id": "t1", "name": "HbA1c", "price_paise": 50000, "is_active": True,
            "turnaround_hours": 24}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return WED

    with patch("app.database.get_lab_test_by_id", new_callable=AsyncMock, return_value=test), \
         patch("app.database.get_lab_collection_window", new_callable=AsyncMock, return_value=WINDOW), \
         patch("app.services.conversation.log_analytics_event", new_callable=AsyncMock), \
         patch("app.services.conversation.datetime", _Clock), \
         patch.object(m.whatsapp, "send_interactive_buttons", new_callable=AsyncMock) as buttons, \
         patch.object(m, "update_state", new_callable=AsyncMock):
        await m._handle_browsing_lab_tests(
            CLINIC, PHONE, "", "", {}, "en", interactive_data={"id": "labtest_t1"}
        )
    sent = buttons.call_args.kwargs["buttons"]
    assert [b["id"] for b in sent] == [
        "labdate_2026-09-23", "labdate_2026-09-24", "labdate_2026-09-25"]
    assert sent[0]["title"] == "Wed, 23 Sep"
