"""Regression tests for WhatsApp interactive-list hard limits.

Meta rejects a list message outright if it carries more than 10 rows (across
all sections), more than 10 sections, or a section with no rows. The rejection
is total: the patient receives NOTHING.

Three builders in conversation.py were unbounded — doctors in a department
(x2) and saved family members — so the failure scaled with hospital size. The
busiest polyclinic, with 11 cardiologists, was the one where tapping
"Cardiology" produced silence.

The cap is enforced in send_interactive_list, the single function all 14 call
sites route through, so a new list builder cannot reintroduce it.
"""

import logging

import pytest
from unittest.mock import AsyncMock, patch

from app.services.whatsapp import MAX_LIST_ROWS, MAX_LIST_SECTIONS, WhatsAppService

CLINIC = {"id": "clinic-1", "name": "Apollo Clinic"}
PHONE = "+919876543210"


def _rows(n, prefix="doc"):
    return [
        {"id": f"{prefix}_{i}", "title": f"Dr. Name {i}", "description": "Cardiology"}
        for i in range(n)
    ]


async def _send(svc, sections):
    """Send with the 24h window open; return the payload handed to Meta."""
    with patch.object(svc, "_can_send_freeform", AsyncMock(return_value=True)), \
         patch.object(svc, "_make_request", AsyncMock(return_value={})) as req, \
         patch.object(svc, "_log_to_ledger", AsyncMock()):
        ok = await svc.send_interactive_list(
            CLINIC, PHONE, body="Choose", button_text="Select", sections=sections,
        )
    payload = req.call_args.args[2] if req.call_args else None
    return ok, payload


def _row_count(payload):
    return sum(len(s["rows"]) for s in payload["interactive"]["action"]["sections"])


@pytest.mark.asyncio
async def test_eleven_doctors_are_truncated_instead_of_rejected_by_meta():
    """The exact production shape: one department, 11 doctors."""
    svc = WhatsAppService()
    ok, payload = await _send(svc, [{"title": "Cardiology", "rows": _rows(11)}])

    assert ok is True
    assert _row_count(payload) == MAX_LIST_ROWS


@pytest.mark.asyncio
async def test_rows_are_capped_across_sections_not_per_section():
    """Meta's budget is 10 rows TOTAL — three 5-row sections is 15, not 5."""
    svc = WhatsAppService()
    sections = [
        {"title": "Morning", "rows": _rows(5, "am")},
        {"title": "Afternoon", "rows": _rows(5, "pm")},
        {"title": "Evening", "rows": _rows(5, "ev")},
    ]
    ok, payload = await _send(svc, sections)

    assert ok is True
    assert _row_count(payload) == MAX_LIST_ROWS


@pytest.mark.asyncio
async def test_truncation_keeps_the_first_options_in_order():
    """Truncation must drop the tail, never reshuffle what the patient sees."""
    svc = WhatsAppService()
    _ok, payload = await _send(svc, [{"title": "Cardiology", "rows": _rows(15)}])

    ids = [r["id"] for s in payload["interactive"]["action"]["sections"] for r in s["rows"]]
    assert ids == [f"doc_{i}" for i in range(MAX_LIST_ROWS)]


@pytest.mark.asyncio
async def test_truncation_is_logged_loudly_enough_to_alert_on(caplog):
    svc = WhatsAppService()
    with caplog.at_level(logging.ERROR):
        await _send(svc, [{"title": "Cardiology", "rows": _rows(14)}])

    log = "\n".join(r.message for r in caplog.records)
    assert "ALERT list_truncated" in log
    assert "14" in log  # the real number of options the clinic wanted to show


@pytest.mark.asyncio
async def test_a_list_within_the_limit_is_passed_through_untouched():
    svc = WhatsAppService()
    ok, payload = await _send(svc, [{"title": "Cardiology", "rows": _rows(4)}])

    assert ok is True
    assert _row_count(payload) == 4
    assert len(payload["interactive"]["action"]["sections"]) == 1


@pytest.mark.asyncio
async def test_sections_are_capped_too():
    svc = WhatsAppService()
    sections = [{"title": f"S{i}", "rows": _rows(1, f"s{i}")} for i in range(14)]
    ok, payload = await _send(svc, sections)

    assert ok is True
    assert len(payload["interactive"]["action"]["sections"]) <= MAX_LIST_SECTIONS


@pytest.mark.asyncio
async def test_empty_list_is_refused_before_the_api_call(caplog):
    """An all-empty list is also a Meta rejection — don't spend the call."""
    svc = WhatsAppService()
    with caplog.at_level(logging.ERROR):
        with patch.object(svc, "_can_send_freeform", AsyncMock(return_value=True)), \
             patch.object(svc, "_make_request", AsyncMock()) as req, \
             patch.object(svc, "_log_to_ledger", AsyncMock()):
            ok = await svc.send_interactive_list(
                CLINIC, PHONE, body="Choose", button_text="Select",
                sections=[{"title": "Empty", "rows": []}],
            )

    assert ok is False
    req.assert_not_awaited()
    assert "empty interactive list" in "\n".join(r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_empty_sections_are_dropped_but_a_full_one_still_sends():
    """A department with no doctors must not poison the whole message."""
    svc = WhatsAppService()
    sections = [
        {"title": "Dermatology", "rows": []},
        {"title": "Cardiology", "rows": _rows(3)},
    ]
    ok, payload = await _send(svc, sections)

    assert ok is True
    titles = [s["title"] for s in payload["interactive"]["action"]["sections"]]
    assert titles == ["Cardiology"]
