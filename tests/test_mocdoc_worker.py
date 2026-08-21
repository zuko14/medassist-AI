"""Regression guard: an admin-entered clinic slug containing a space (e.g.
copy-pasting the clinic's display name instead of its URL slug) must not
produce a malformed navigation URL that silently strands the Pending Print
tab lookup — the slug must be URL-encoded before use."""

import tempfile
from unittest.mock import AsyncMock

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
    mock_page.locator.return_value.is_visible = AsyncMock(return_value=False)
    mock_page.evaluate = AsyncMock(return_value=False)  # JS tab click fails
    mock_page.locator.return_value.first.click = AsyncMock(side_effect=Exception("no such element"))
    worker._page = mock_page

    await worker.fetch_new_reports()

    goto_url = mock_page.goto.call_args_list[0].args[0]
    assert " " not in goto_url
    assert "ACCUMAX%20DIAGNOSTICS" in goto_url
