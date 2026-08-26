"""Afternoon slots must not be labelled Evening, and the fully-booked
fallback must not probe availability one doctor-day at a time."""

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager

CLINIC = {"id": "c1", "whatsapp_number": "+911111111111"}
CTX = {"doctor_name": "Dr. Booked", "department": "General Medicine"}


async def _sections(slots, lang="en"):
    m = ConversationManager()
    with patch.object(
        m.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as sl, patch.object(m, "update_state", new_callable=AsyncMock):
        await m._show_slot_list(CLINIC, "+919876543210", slots, dict(CTX), lang)
    return sl.call_args.kwargs["sections"]


@pytest.mark.asyncio
async def test_afternoon_slots_get_their_own_section():
    sections = await _sections(["09:00", "13:00", "14:30", "17:00"])
    titles = [s["title"] for s in sections]
    assert len(sections) == 3, titles
    assert "Morning (1)" in titles[0]
    assert "Afternoon (2)" in titles[1]
    assert "Evening (1)" in titles[2]


@pytest.mark.asyncio
async def test_afternoon_only_doctor_is_not_called_evening():
    sections = await _sections(["12:00", "15:30"])
    assert len(sections) == 1
    assert "Afternoon (2)" in sections[0]["title"]


@pytest.mark.asyncio
async def test_row_budget_never_exceeds_whatsapp_limit():
    slots = [f"{h:02d}:{m:02d}" for h in range(8, 20) for m in (0, 30)]
    sections = await _sections(slots)
    assert sum(len(s["rows"]) for s in sections) <= 10
    # Every non-empty session still gets representation.
    assert len(sections) == 3


@pytest.mark.asyncio
async def test_two_session_doctor_keeps_five_and_five():
    sections = await _sections(
        ["09:00", "09:30", "10:00", "10:30", "11:00", "17:00", "17:30"]
    )
    assert [len(s["rows"]) for s in sections] == [5, 2]


@pytest.mark.asyncio
async def test_unparseable_slot_still_shown():
    sections = await _sections(["09:00", "garbage"])
    ids = {r["id"] for s in sections for r in s["rows"]}
    assert "slot_garbage" in ids


@pytest.mark.asyncio
async def test_fully_booked_fallback_scans_doctors_concurrently():
    """3 doctors x 7 days used to be 21 serial round-trips."""
    m = ConversationManager()
    doctors = [
        {"name": f"Dr. {n}", "specialization": "General"}
        for n in ("A", "B", "C")
    ] + [{"name": "Dr. Booked", "specialization": "General"}]

    from datetime import datetime, timedelta

    free_on = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")

    async def slow_slots(clinic_id, doctor_name, date_str, **kw):
        await asyncio.sleep(0.05)
        return (["09:00"], None) if date_str == free_on else ([], None)

    with patch(
        "app.services.conversation.get_doctors",
        new_callable=AsyncMock,
        return_value=doctors,
    ), patch(
        "app.services.conversation.get_available_slots", side_effect=slow_slots
    ) as probe, patch.object(
        m.whatsapp, "send_text", new_callable=AsyncMock
    ), patch.object(
        m.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as sl:
        started = time.perf_counter()
        await m._suggest_other_doctors(CLINIC, "+919876543210", dict(CTX), "en")
        elapsed = time.perf_counter() - started

    # 3 doctors x 3 day-rounds, then every doctor is placed and the scan stops.
    assert probe.call_count == 9, probe.call_count
    assert elapsed < 0.4, f"serial scan: {elapsed:.2f}s for {probe.call_count} probes"
    # Excluded doctor stays out; the rest are offered.
    names = {r["title"] for s in sl.call_args.kwargs["sections"] for r in s["rows"]}
    assert names == {"Dr. A", "Dr. B", "Dr. C"}


@pytest.mark.asyncio
async def test_fallback_offers_each_doctors_earliest_free_date():
    """Concurrency must not change which date a doctor is advertised as free."""
    from datetime import datetime, timedelta

    m = ConversationManager()
    # The scan probes tomorrow .. +7 days; free A on probe 2, B on probe 5.
    free_date = {
        "Dr. A": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"),
        "Dr. B": (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d"),
    }

    async def slots_for(clinic_id, doctor_name, date_str, **kw):
        return (["09:00"], None) if date_str == free_date[doctor_name] else ([], None)

    with patch(
        "app.services.conversation.get_doctors",
        new_callable=AsyncMock,
        return_value=[
            {"name": "Dr. A", "specialization": "General"},
            {"name": "Dr. B", "specialization": "General"},
        ],
    ), patch(
        "app.services.conversation.get_available_slots", side_effect=slots_for
    ), patch.object(
        m.whatsapp, "send_text", new_callable=AsyncMock
    ), patch.object(
        m.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as sl:
        await m._suggest_other_doctors(CLINIC, "+919876543210", dict(CTX), "en")

    descs = {
        r["title"]: r["description"]
        for s in sl.call_args.kwargs["sections"]
        for r in s["rows"]
    }
    for name, iso in free_date.items():
        expected = datetime.strptime(iso, "%Y-%m-%d").strftime("%d %b")
        assert expected in descs[name], (name, descs[name], expected)
