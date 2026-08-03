"""Credential-gated live MocDoc EMR sandbox integration test suite (Phase R2).

Skipped by default in standard CI unless environment variable MOCDOC_SANDBOX_ENABLED=1 is explicitly set.
"""

import os
import pytest
from app.integrations.callmedex.config.settings import callmedex_settings
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.browser.session import PlaywrightBrowserSession


@pytest.mark.skipif(
    os.getenv("MOCDOC_SANDBOX_ENABLED") != "1",
    reason="Credential-gated integration test requiring live MocDoc sandbox access (MOCDOC_SANDBOX_ENABLED=1)",
)
@pytest.mark.asyncio
async def test_mocdoc_live_sandbox_login_and_search():
    """Perform live login, navigation, and barcode search against MocDoc staging/sandbox portal."""
    session = PlaywrightBrowserSession()
    session_id = "live_sandbox_test_session"

    ctx = await session.create_context(session_id, headless=callmedex_settings.browser_headless)
    page = ctx.get("page")
    assert page is not None, "Playwright browser context failed to launch live page"

    connector = MocDocConnector(browser_session=session)
    connector.attach_page(page)

    try:
        # Step 1: Open Login Page
        await connector.open_login_page()

        # Step 2-4: Login Sequence
        creds = {
            "username": os.getenv("MOCDOC_SANDBOX_USER", callmedex_settings.mocdoc_username.get_secret_value()),
            "password": os.getenv("MOCDOC_SANDBOX_PASS", callmedex_settings.mocdoc_password.get_secret_value()),
        }
        login_success = await connector.login(creds)
        assert login_success is True

        # Step 5-6: Barcode Search
        test_barcode = os.getenv("MOCDOC_SANDBOX_BARCODE", "SANDBOX-BARCODE-001")
        metadata = await connector.search_by_barcode(test_barcode)
        assert metadata is not None
        assert metadata.report_id == test_barcode

        # Step 10: Logout
        await connector.logout()

    finally:
        await session.close_context(session_id)
        connector.attach_page(None)
