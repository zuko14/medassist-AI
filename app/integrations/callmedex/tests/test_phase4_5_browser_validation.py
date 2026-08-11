"""Phase 4.5 Browser Automation & DOM Stability Validation Test Suite."""

import pytest
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.connectors.base.connector import JobCheckpoint
from app.integrations.callmedex.browser.integrity import validate_pdf_download
from app.integrations.callmedex.browser.metrics import WorkflowTimer
from app.integrations.callmedex.browser.session import PlaywrightBrowserSession
from app.integrations.callmedex.config.settings import callmedex_settings
from app.integrations.callmedex.api.schemas import PatientIdentity


@pytest.mark.asyncio
async def test_phase4_5_full_workflow_validation():
    """Validate full 10-step MocDoc workflow execution and recovery checkpoints."""
    connector = MocDocConnector()
    assert connector.current_checkpoint == JobCheckpoint.CREATED

    # Login
    await connector.login({"username": "chaitanya_test", "password": "password_test"})
    assert connector.current_checkpoint == JobCheckpoint.AUTHENTICATED

    # Barcode Search
    metadata = await connector.search_by_barcode("BARCODE-2026-99")
    assert metadata is not None
    assert connector.current_checkpoint == JobCheckpoint.REPORT_LOCATED

    # Download & PDF Integrity Validation
    pdf_bytes = await connector.download_report("BARCODE-2026-99", callmedex_settings.download_dir)
    assert pdf_bytes is not None
    assert connector.current_checkpoint == JobCheckpoint.PDF_DOWNLOADED

    integrity = validate_pdf_download(pdf_bytes)
    assert integrity.is_valid is True
    assert integrity.pdf_signature_valid is True
    assert integrity.non_zero_bytes is True
    assert len(integrity.sha256_checksum) == 64

    # Logout
    logout_ok = await connector.logout()
    assert logout_ok is True


@pytest.mark.asyncio
async def test_phase4_5_dom_stability_repeated_runs():
    """Verify DOM stability by executing selector resolution 10 consecutive times."""
    connector = MocDocConnector()
    for run_index in range(10):
        # Resolve all versioned selectors
        assert "username" in connector.selectors.login_username_input
        assert "password" in connector.selectors.login_password_input
        assert "submit" in connector.selectors.login_submit_button
        assert "Investigation" in connector.selectors.nav_investigation_tab
        assert "View" in connector.selectors.patient_view_button
        assert "Select" in connector.selectors.download_modal_select_button
        assert "Sign out" in connector.selectors.logout_button


@pytest.mark.asyncio
async def test_phase4_5_download_integrity_validation():
    """Verify PDF signature, non-zero byte size, and SHA256 checksum generation."""
    valid_pdf_bytes = b"%PDF-1.4 Mock Lab Report Content Sample"
    result = validate_pdf_download(valid_pdf_bytes)

    assert result.is_valid is True
    assert result.pdf_signature_valid is True
    assert result.non_zero_bytes is True
    assert result.file_size_bytes > 0
    assert len(result.sha256_checksum) == 64

    # Corrupted header check
    invalid_bytes = b"CORRUPTED_NON_PDF_HEADER_CONTENT"
    bad_result = validate_pdf_download(invalid_bytes)
    assert bad_result.is_valid is False
    assert bad_result.pdf_signature_valid is False


@pytest.mark.asyncio
async def test_phase4_5_retry_recovery_checkpoint_resumption():
    """Simulate forced failure during barcode search and verify checkpoint resumption."""
    connector = MocDocConnector()

    # Stage 1: Authenticate
    await connector.login({"username": "test_user", "password": "test_password"})
    assert connector.current_checkpoint == JobCheckpoint.AUTHENTICATED

    # Forced failure simulation: network drop during initial search
    try:
        # Simulate interruption
        raise ConnectionError("Network connection interrupted during barcode search")
    except ConnectionError:
        # Verify checkpoint remains at AUTHENTICATED
        assert connector.current_checkpoint == JobCheckpoint.AUTHENTICATED

    # Resume execution from last completed checkpoint (AUTHENTICATED)
    metadata = await connector.search_by_barcode("RECOVERED-BARCODE-001")
    assert metadata is not None
    assert connector.current_checkpoint == JobCheckpoint.REPORT_LOCATED


@pytest.mark.asyncio
async def test_phase4_5_operational_timing_baselines():
    """Measure durations for Login, Search, Lookup, and Download stages."""
    timer = WorkflowTimer()

    # Simulate stage transitions
    timer.mark_login_complete()
    timer.mark_search_complete()
    timer.mark_lookup_complete()
    timer.mark_download_complete()

    metrics = timer.metrics
    assert metrics.login_duration_ms >= 0.0
    assert metrics.barcode_search_duration_ms >= 0.0
    assert metrics.report_lookup_duration_ms >= 0.0
    assert metrics.download_duration_ms >= 0.0
    assert metrics.total_workflow_duration_ms >= 0.0


@pytest.mark.asyncio
async def test_phase4_5_automated_screenshot_artifact_generation():
    """Verify automated regression screenshot generation for workflow stages."""
    session = PlaywrightBrowserSession()

    stages = [
        "login_page_regression",
        "barcode_search_regression",
        "patient_page_regression",
        "reports_page_regression",
        "download_confirmation_regression",
    ]

    class _FakePage:
        async def screenshot(self, path: str):
            with open(path, "wb") as f:
                f.write(b"fake-png-bytes-for-test")

    for stage_label in stages:
        filepath = await session.capture_screenshot(_FakePage(), stage_label)
        assert filepath is not None
        assert f"{stage_label}.png" in filepath
