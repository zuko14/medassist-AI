"""Test Suite for Incremental 10-Step MocDoc Browser Automation Workflow."""

import pytest
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.browser.selectors.mocdoc.v1 import MocDocSelectorProviderV1
from app.integrations.callmedex.connectors.base.connector import JobCheckpoint
from app.integrations.callmedex.config.settings import callmedex_settings


@pytest.mark.asyncio
async def test_step1_open_login_page():
    """Step 1: Verify opening login page."""
    connector = MocDocConnector()
    res = await connector.open_login_page()
    assert res is True


@pytest.mark.asyncio
async def test_step2_to_4_login_sequence():
    """Steps 2-4: Verify username, password entry, and submit login."""
    connector = MocDocConnector()
    login_ok = await connector.login({"username": "chaitanya_mocdoc", "password": "secure_password_123"})
    assert login_ok is True
    assert connector.current_checkpoint == JobCheckpoint.AUTHENTICATED
    assert connector.selectors.login_username_input is not None
    assert connector.selectors.login_password_input is not None
    assert connector.selectors.login_submit_button is not None


@pytest.mark.asyncio
async def test_step5_navigate_to_barcode_search():
    """Step 5: Verify navigation to Investigation -> Lab Order search interface."""
    connector = MocDocConnector()
    await connector.login({"username": "user", "password": "pwd"})
    nav_ok = await connector.navigate_to_barcode_search()
    assert nav_ok is True
    assert "Investigation" in connector.selectors.nav_investigation_tab
    assert "Laboratory" in connector.selectors.nav_lab_order_link


@pytest.mark.asyncio
async def test_step6_paste_barcode():
    """Step 6: Verify pasting barcode and search submission."""
    connector = MocDocConnector()
    await connector.login({"username": "user", "password": "pwd"})
    metadata = await connector.search_by_barcode("260700009225")
    assert metadata is not None
    assert metadata.report_id == "260700009225"
    assert connector.current_checkpoint == JobCheckpoint.REPORT_LOCATED


@pytest.mark.asyncio
async def test_step7_open_patient():
    """Step 7: Verify opening patient view."""
    connector = MocDocConnector()
    await connector.login({"username": "user", "password": "pwd"})
    patient_ok = await connector.open_patient("260700009225")
    assert patient_ok is True
    assert "View" in connector.selectors.patient_view_button


@pytest.mark.asyncio
async def test_step8_open_reports():
    """Step 8: Verify expanding report details list."""
    connector = MocDocConnector()
    await connector.login({"username": "user", "password": "pwd"})
    reports_ok = await connector.open_reports("260700009225")
    assert reports_ok is True


@pytest.mark.asyncio
async def test_step9_download_latest_report():
    """Step 9: Verify opening download modal and downloading PDF report bytes."""
    connector = MocDocConnector()
    await connector.login({"username": "user", "password": "pwd"})
    pdf_bytes = await connector.download_report("260700009225", callmedex_settings.download_dir)
    assert pdf_bytes is not None
    assert b"%PDF" in pdf_bytes
    assert connector.current_checkpoint == JobCheckpoint.PDF_DOWNLOADED


@pytest.mark.asyncio
async def test_step10_logout():
    """Step 10: Verify opening user profile menu and clicking Sign out."""
    connector = MocDocConnector()
    await connector.login({"username": "user", "password": "pwd"})
    logout_ok = await connector.logout()
    assert logout_ok is True
    assert "Welcome" in connector.selectors.profile_dropdown_menu
    assert "Sign out" in connector.selectors.logout_button
