"""The "Our Doctors" browse list must reach every doctor.

Reported from production: a clinic added 14 doctors in the admin panel and the
WhatsApp bot showed 10, with no way to see the rest. _show_doctors built rows
under a hard `remaining_rows = 10` budget and simply stopped - no "More" row,
and nothing in the message to say anything had been left out. The doctors were
plainly there in the admin panel, so it read as the bot losing them.

Meta caps an interactive list at 10 rows across all sections, so the cap itself
is real; what was missing is the pager. _page_rows() already existed and was
used by the department, branch, booking-doctor and lab-test pickers - this list
was the one that never adopted it.
"""

import ast
import inspect
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.conversation import _MORE_DOCTORS_ID, ConversationManager

CLINIC = {"id": "11111111-1111-1111-1111-111111111111", "name": "Test Clinic"}


def _doctors(n):
    return [
        {
            "id": f"doc-{i}",
            "name": f"Dr. Number {i}",
            "specialization": "Physician",
            "department": "Cardiology" if i % 2 == 0 else "Orthopaedics",
            "consultation_fee": 500,
            "is_active": True,
        }
        for i in range(n)
    ]


def _supabase(doctors):
    """doctors query returns the roster; doctor_branches returns nothing."""
    chain = MagicMock()
    for m in ("select", "eq", "order", "in_", "limit"):
        getattr(chain, m).return_value = chain

    calls = {"n": 0}

    def _execute():
        calls["n"] += 1
        return MagicMock(data=doctors if calls["n"] == 1 else [])

    chain.execute.side_effect = _execute
    sb_mock = MagicMock()
    sb_mock.table.return_value = chain
    return sb_mock


async def _render(doctor_count, page=0):
    """Run _show_doctors and return the sections it handed to WhatsApp."""
    cm = ConversationManager()
    cm.whatsapp = MagicMock()
    cm.whatsapp.send_interactive_list = AsyncMock(return_value=True)
    cm.whatsapp.send_text = AsyncMock(return_value=True)

    with patch("app.database.supabase", _supabase(_doctors(doctor_count))):
        await cm._show_doctors(CLINIC, "+919876543210", "en", page=page)

    cm.whatsapp.send_interactive_list.assert_awaited_once()
    return cm.whatsapp.send_interactive_list.await_args.kwargs["sections"]


def _rows(sections):
    return [r for s in sections for r in s["rows"]]


def _more_row(sections):
    return next(
        (r for r in _rows(sections) if r["id"].startswith(_MORE_DOCTORS_ID)), None
    )


# -- The reported bug -------------------------------------------------------


@pytest.mark.asyncio
async def test_fourteen_doctors_are_all_reachable():
    """The exact reported shape: 14 in the panel, 10 shown, 4 unreachable."""
    page0 = await _render(14, page=0)
    more = _more_row(page0)
    assert more is not None, "no 'More' row - doctors 10-14 are unreachable"

    page1 = await _render(14, page=int(more["id"].rsplit("_", 1)[1]))

    seen = {
        r["id"]
        for r in _rows(page0) + _rows(page1)
        if not r["id"].startswith(_MORE_DOCTORS_ID)
    }
    assert seen == {
        f"view_doc_{d['id']}" for d in _doctors(14)
    }, "some doctors cannot be reached from any page"


@pytest.mark.asyncio
async def test_each_page_respects_metas_ten_row_cap():
    """Over 10 rows and Meta rejects the whole message (131009) - the patient
    receives nothing at all, mid-flow."""
    for page in (0, 1):
        sections = await _render(14, page=page)
        assert len(_rows(sections)) <= 10
        assert len(sections) <= 10


@pytest.mark.asyncio
async def test_last_page_has_no_more_row():
    """A pager that always offers 'More' loops the patient forever."""
    page0 = await _render(14, page=0)
    last = await _render(14, page=int(_more_row(page0)["id"].rsplit("_", 1)[1]))
    assert _more_row(last) is None


@pytest.mark.asyncio
async def test_more_row_carries_the_next_page_number():
    """Paging is stateless - the page rides on the button id, so browsing
    doctors cannot clobber a booking in progress."""
    more = _more_row(await _render(14, page=0))
    assert re.fullmatch(rf"{re.escape(_MORE_DOCTORS_ID)}_\d+", more["id"])
    assert more["id"].rsplit("_", 1)[1] == "1"


@pytest.mark.asyncio
async def test_a_list_that_already_fits_gains_no_extra_tap():
    """Clinics under the cap must not be given a pointless 'More' row."""
    for count in (1, 9, 10):
        sections = await _render(count, page=0)
        assert _more_row(sections) is None
        assert len(_rows(sections)) == count


@pytest.mark.asyncio
async def test_department_grouping_survives_pagination():
    sections = await _render(14, page=0)
    doctor_sections = [
        s for s in sections if not s["rows"][0]["id"].startswith(_MORE_DOCTORS_ID)
    ]
    assert {s["title"] for s in doctor_sections} <= {"Cardiology", "Orthopaedics"}
    assert doctor_sections, "department headings were lost"


# -- The ordering hazard the pager id creates -------------------------------


def test_pager_id_is_dispatched_before_the_doctor_id_prefix():
    """`view_doc_more_2` also starts with `view_doc_`.

    If the generic prefix matched first, the bot would look up a doctor whose
    id is "more_2", find nothing, and re-show page 0 - an endless loop on the
    first ten doctors, which is a worse bug than the one being fixed. This is
    an ordering constraint in a long elif chain, so it is pinned structurally.
    """
    source = pathlib.Path(inspect.getfile(ConversationManager)).read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    pager_line = doctor_line = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Attribute) and node.func.attr == "startswith"
        ):
            continue
        arg = node.args[0] if node.args else None
        if isinstance(arg, ast.Constant) and arg.value == "view_doc_":
            doctor_line = node.lineno if doctor_line is None else doctor_line
        elif isinstance(arg, ast.BinOp):  # _MORE_DOCTORS_ID + "_"
            pager_line = node.lineno if pager_line is None else pager_line

    assert pager_line is not None, "the pager prefix branch is gone"
    assert doctor_line is not None, "the view_doc_ branch is gone"
    assert pager_line < doctor_line, (
        "the pager branch must be tested BEFORE the generic view_doc_ prefix, "
        "or 'view_doc_more_N' resolves as a doctor id and pages forever"
    )


# -- The send-path backstop -------------------------------------------------


async def _sent_payload(row_count):
    """Send a list with row_count rows and return the payload Meta would get."""
    from app.services.whatsapp import WhatsAppService

    svc = WhatsAppService()
    svc._can_send_freeform = AsyncMock(return_value=True)
    svc._make_request = AsyncMock(return_value={"messages": [{"id": "wamid.X"}]})
    svc._log_to_ledger = AsyncMock()

    rows = [
        {"id": f"r{i}", "title": f"Option {i}", "description": ""}
        for i in range(row_count)
    ]
    await svc.send_interactive_list(
        CLINIC,
        phone="+919876543210",
        body="Pick one:",
        button_text="Select",
        sections=[{"title": "All", "rows": rows}],
    )
    svc._make_request.assert_awaited_once()
    return svc._make_request.await_args[0][2]


@pytest.mark.asyncio
async def test_overflowing_list_tells_the_patient_it_was_truncated():
    """The 'list_truncated' ALERT only ever reached our logs.

    Any builder that hands over more than Meta's 10 rows now says so in the
    body, so a patient never again sees a silently shortened list and assumes
    that is everything the clinic has.
    """
    payload = await _sent_payload(14)
    body = payload["interactive"]["body"]["text"]
    assert "Showing 10 of 14" in body
    assert "4 more" in body
    assert len(payload["interactive"]["action"]["sections"][0]["rows"]) == 10


@pytest.mark.asyncio
async def test_a_list_that_fits_gets_no_notice():
    payload = await _sent_payload(10)
    assert payload["interactive"]["body"]["text"] == "Pick one:"


@pytest.mark.asyncio
async def test_notice_keeps_the_body_within_metas_limit():
    """Meta rejects an over-long body outright, and a rejected message means
    the patient receives nothing at all - worse than the truncation."""
    from app.services.whatsapp import MAX_LIST_BODY_CHARS, WhatsAppService

    svc = WhatsAppService()
    svc._can_send_freeform = AsyncMock(return_value=True)
    svc._make_request = AsyncMock(return_value={"messages": [{"id": "wamid.X"}]})
    svc._log_to_ledger = AsyncMock()

    await svc.send_interactive_list(
        CLINIC,
        phone="+919876543210",
        body="x" * MAX_LIST_BODY_CHARS,
        button_text="Select",
        sections=[
            {
                "title": "All",
                "rows": [
                    {"id": f"r{i}", "title": f"O{i}", "description": ""}
                    for i in range(30)
                ],
            }
        ],
    )
    body = svc._make_request.await_args[0][2]["interactive"]["body"]["text"]
    assert len(body) <= MAX_LIST_BODY_CHARS
    assert "Showing 10 of 30" in body
