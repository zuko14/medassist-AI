"""Regression guard: an admin-entered clinic slug containing a space (e.g.
copy-pasting the clinic's display name instead of its URL slug) must not
produce a malformed navigation URL that silently strands the Pending Print
tab lookup — the slug must be URL-encoded before use."""

import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from connectors.mocdoc.worker import MocDocConnector


@pytest.mark.asyncio
async def test_fetch_new_reports_url_encodes_clinic_slug_with_space():
    worker = MocDocConnector(
        clinic_id="clinic-1",
        config={"username": "u", "password": "p", "clinic_slug": "ACCUMAX DIAGNOSTICS"},
        medassist_url="http://localhost:8000",
        integration_secret="secret",
        session_dir=tempfile.mkdtemp(),
    )

    mock_page = AsyncMock()
    mock_locator = AsyncMock()
    mock_locator.is_visible = AsyncMock(return_value=False)
    mock_locator.first = AsyncMock()
    mock_locator.first.is_visible = AsyncMock(return_value=False)
    mock_locator.first.click = AsyncMock(side_effect=Exception("no such element"))
    mock_page.locator = MagicMock(return_value=mock_locator)
    mock_page.evaluate = AsyncMock(return_value=False)  # JS tab click fails
    mock_page.wait_for_timeout = AsyncMock(return_value=None)
    mock_page.goto = AsyncMock(return_value=None)
    worker._page = mock_page

    # The tab never opens, so the run must fail loudly rather than report 0.
    with pytest.raises(RuntimeError, match="Pending Print"):
        await worker.fetch_new_reports()

    goto_url = mock_page.goto.call_args_list[0].args[0]
    assert " " not in goto_url
    assert "ACCUMAX%20DIAGNOSTICS" in goto_url


@pytest.mark.asyncio
async def test_tab_click_survives_overlay_banner_and_never_uses_bare_return():
    """Oct 2026 outage: MocDoc's fixed "subscription ends" banner covered the
    Pending Print tab, so the Playwright click was intercepted — and the JS
    click fallback had a top-level `return` (a SyntaxError), so it never ran.
    Every run reported "Success, 0 found" for four days."""
    worker = MocDocConnector(
        clinic_id="clinic-1",
        config={"username": "u", "password": "p", "clinic_slug": "s"},
        medassist_url="http://localhost:8000",
        integration_secret="secret",
        session_dir=tempfile.mkdtemp(),
    )
    scripts = []

    async def _evaluate(script, arg=None):
        scripts.append(script)
        return arg == "pendingprint"  # only the JS tab click succeeds

    mock_page = AsyncMock()
    mock_locator = MagicMock()
    mock_locator.is_visible = AsyncMock(return_value=False)
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.first.click = AsyncMock(side_effect=Exception("intercepts pointer events"))
    mock_page.locator = MagicMock(return_value=mock_locator)
    mock_page.evaluate = AsyncMock(side_effect=_evaluate)
    worker._page = mock_page

    assert await worker.fetch_new_reports() == []  # tab opened, empty table
    assert any("#dr_body_notice" == c.args[1] for c in mock_page.evaluate.call_args_list
               if len(c.args) > 1)
    for s in scripts:
        if "return" in s:  # page.evaluate only allows `return` inside a function
            assert s.lstrip().startswith("("), s[:80]


def test_parse_test_details_scoped_to_single_row():
    from connectors.mocdoc.worker import _parse_test_details
    assert _parse_test_details(
        "COMPLETE BLOOD COUNT - 3P No: 22222 SampleID: 260700002222"
    )["report_no"] == "22222"


def test_bare_ten_digit_mobile_gets_country_code():
    from connectors.mocdoc.worker import _parse_patient_cell
    assert _parse_patient_cell(
        "Mr.Ramesh\nID: VAM-40011 Mobile: 9876543210"
    )["phone"] == "+919876543210"
