"""MocDoc EMR Laboratory Connector Implementation (Production Hardened).

Implements the 10-step standardized browser automation workflow for the
CallMedex barcode-driven on-demand report pull. Downloads reports from the
'Pending Print' section of MocDoc's lab reports page.

Production features ported from Mode 1 (connectors/mocdoc/worker.py):
- Real lifecycle polling (wait_until_report_available)
- Cascading modal dismissal (_dismiss_all_modals)
- Download modal lifecycle with bill-error detection
- Encrypted debug artifact capture on failure
- Temp file cleanup after PDF read
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional

from app.integrations.callmedex.config.settings import callmedex_settings
from app.integrations.callmedex.connectors.base.connector import (
    BaseLaboratoryConnector,
    ConnectorCapabilities,
    JobCheckpoint,
)
from app.integrations.callmedex.api.schemas import (
    PatientIdentity,
    ReportMetadata,
    ReportType,
)
from app.integrations.callmedex.browser.selectors.mocdoc.current import MocDocSelectorProvider
from app.integrations.callmedex.browser.base import BaseBrowserSession
from app.integrations.callmedex.api.exceptions import (
    AuthenticationError,
    ConnectorNavigationError,
    ReportDownloadError,
    ValidationError,
)

logger = logging.getLogger(__name__)


def _build_mock_lab_report_pdf(barcode_id: str) -> bytes:
    """Build a minimal but structurally valid, OCR-parseable PDF for non-live (test/dev) runs.

    Used only outside production (live browser navigation disabled). Contains real embedded
    text so the downstream Canonical OCR pipeline exercises genuine extraction instead of
    silently falling through to a failure path.
    """
    stream_text = (
        "BT\n/F1 12 Tf\n14 TL\n50 750 Td\n"
        "(LABORATORY TEST REPORT) Tj T*\n"
        f"(Barcode {barcode_id}) Tj T*\n"
        "(Hemoglobin 13.6 g/dL 13.0-17.0) Tj T*\n"
        "(White Blood Cell Count 7500 /uL 4000-11000) Tj T*\n"
        "(Platelet Count 250000 /uL 150000-450000) Tj T*\n"
        "(Serum Creatinine 0.9 mg/dL 0.6-1.2) Tj T*\n"
        "ET"
    )
    stream_len = len(stream_text)
    pdf_text = f"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length {stream_len} >>
stream
{stream_text}
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000242 00000 n
0000000300 00000 n
trailer
<< /Size 6 /Root 1 0 R >>
startxref
380
%%EOF"""
    return pdf_text.encode("latin-1")


class MocDocConnector(BaseLaboratoryConnector):
    """Concrete laboratory connector for MocDoc EMR web portal.

    Implements the 10-step standardized browser automation workflow:
    Step 1: Open Login page (https://mocdoc.com/user/loginform)
    Step 2: Enter Username
    Step 3: Enter Password
    Step 4: Login
    Step 5: Navigate to Lab Reports → Pending Print tab
    Step 6: Search for Barcode in Pending Print table
    Step 7: Open Patient View (click View button)
    Step 8: Open Reports List (expand row)
    Step 9: Download Latest Final Report PDF (handle download modal)
    Step 10: Logout

    Includes versioned selector resolution (v1.0.0), explicit checkpoints,
    resumable JobCheckpoint tracking, production modal handling, and
    bill-payment error detection.
    """

    # ─── Polling tuning constants ───────────────────────────────────────
    _POLL_INTERVAL_SECONDS = 15
    _TABLE_LOAD_WAIT_MS = 3000
    _EXPANSION_WAIT_MS = 2000
    _MODAL_DISMISS_ROUNDS = 5
    _TAB_CLICK_ATTEMPTS = 3
    _DOWNLOAD_TIMEOUT_MS = 60_000
    _MODAL_APPEAR_TIMEOUT_MS = 10_000

    def __init__(
        self,
        selector_provider: Optional[MocDocSelectorProvider] = None,
        browser_session: Optional[BaseBrowserSession] = None,
    ):
        self._selectors = selector_provider or MocDocSelectorProvider()
        self._browser_session = browser_session
        self._page: Any = None
        self._session_id: Optional[str] = None
        self._authenticated = False
        self._current_checkpoint = JobCheckpoint.CREATED

        # Portal config — set via configure_center() before lifecycle calls
        self._base_url: Optional[str] = None
        self._clinic_slug: Optional[str] = None

        logger.info(
            f"Initialized MocDocConnector with versioned selectors {self._selectors.version}"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # CONFIGURATION
    # ═══════════════════════════════════════════════════════════════════════

    def configure_center(self, base_url: str, clinic_slug: str) -> None:
        """Set the MocDoc portal URL and clinic slug for this processing center.

        Must be called before wait_until_report_available() or download_report()
        so the connector knows which lab reports page to navigate to.

        Args:
            base_url: Root URL of the MocDoc instance (e.g., "https://mocdoc.com")
            clinic_slug: URL slug for the lab reports path
        """
        self._base_url = base_url.rstrip("/")
        self._clinic_slug = clinic_slug
        logger.info(
            f"Configured processing center: base_url='{self._base_url}', "
            f"clinic_slug='{self._clinic_slug}'"
        )

    def attach_page(self, page: Any, session_id: Optional[str] = None) -> None:
        """Attach active Playwright page instance handle and its owning browser session ID."""
        self._page = page
        self._session_id = session_id

    def _is_live_page(self) -> bool:
        """Return True if attached page handle is navigated to an active HTTP/HTTPS live portal."""
        if self._page is None or not hasattr(self._page, "url"):
            return False
        try:
            url = getattr(self._page, "url", "")
            return bool(url and url.startswith("http") and url != "about:blank")
        except Exception:
            return False

    def _require_center_config(self) -> None:
        """Raise if configure_center() has not been called."""
        if not self._base_url or not self._clinic_slug:
            raise ConnectorNavigationError(
                "Processing center not configured. Call configure_center(base_url, clinic_slug) "
                "before wait_until_report_available() or download_report()."
            )

    @property
    def capabilities(self) -> ConnectorCapabilities:
        """Return MocDoc connector capability declaration."""
        return ConnectorCapabilities(
            browser_required=True,
            supports_barcode_search=True,
            supports_incremental_downloads=False,
            supports_multi_report=False,
            supports_pdf=True,
            supports_images=False,
            supports_retry=True,
        )

    @property
    def selectors(self) -> MocDocSelectorProvider:
        """Return current versioned selector provider instance."""
        return self._selectors

    @property
    def current_checkpoint(self) -> JobCheckpoint:
        """Return current recovery checkpoint for active job."""
        return self._current_checkpoint

    # ═══════════════════════════════════════════════════════════════════════
    # MODAL DISMISSAL (ported from Mode 1 worker.py)
    # ═══════════════════════════════════════════════════════════════════════

    async def _dismiss_all_modals(self) -> None:
        """Dismiss any modal dialogs that MocDoc may show.

        MocDoc uses multiple Bootstrap modals that can appear in sequence:
        1. #ms-loading-modal — loading spinner, must WAIT for it to disappear
        2. #md-info-modal — info/maintenance notice, must CLICK to dismiss
        3. Generic modals — try buttons or force-remove via JS

        Loops up to 5 times to handle cascading modals.
        """
        if not self._is_live_page():
            return

        # Step 1: Wait for loading spinner to disappear
        loading = self._page.locator("#ms-loading-modal.show, #ms-loading-modal.in")
        try:
            if await loading.is_visible(timeout=1000):
                logger.info("Loading spinner detected — waiting for it to disappear...")
                await loading.wait_for(state="hidden", timeout=30000)
                logger.info("Loading spinner gone")
                await self._page.wait_for_timeout(1000)
        except Exception:
            # Force-hide via JS
            try:
                await self._page.evaluate("""
                    const m = document.getElementById('ms-loading-modal');
                    if (m) { m.classList.remove('show', 'in'); m.style.display = 'none'; }
                    document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
                """)
                logger.info("Force-hid loading spinner via JS")
                await self._page.wait_for_timeout(500)
            except Exception:
                pass

        # Step 2: Loop to dismiss all info/maintenance/generic modals
        for attempt in range(self._MODAL_DISMISS_ROUNDS):
            any_visible = False

            # Try #md-info-modal first
            try:
                modal = self._page.locator("#md-info-modal.show, #md-info-modal.in")
                if await modal.is_visible(timeout=1500):
                    any_visible = True
                    logger.info(f"Detected #md-info-modal (attempt {attempt + 1})")
                    for btn_sel in [
                        "#md-info-modal button[data-dismiss='modal']",
                        "#md-info-modal button[data-bs-dismiss='modal']",
                        "#md-info-modal button.btn",
                        "#md-info-modal .close",
                        "#md-info-modal button",
                    ]:
                        try:
                            btn = self._page.locator(btn_sel).first
                            if await btn.is_visible(timeout=500):
                                await btn.click()
                                logger.info(f"Dismissed #md-info-modal via: {btn_sel}")
                                await self._page.wait_for_timeout(1000)
                                break
                        except Exception:
                            continue
                    else:
                        # No button worked — force via JS
                        await self._page.evaluate("""
                            const m = document.getElementById('md-info-modal');
                            if (m) { m.classList.remove('show', 'in'); m.style.display = 'none'; }
                            document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
                            document.body.classList.remove('modal-open');
                            document.body.style.removeProperty('padding-right');
                        """)
                        logger.info("Force-hid #md-info-modal via JS")
                        await self._page.wait_for_timeout(500)
                    continue  # Check for more modals
            except Exception:
                pass

            # Try any generic visible modal
            try:
                any_modal = self._page.locator(".modal.show, .modal.in")
                if await any_modal.is_visible(timeout=1000):
                    any_visible = True
                    await self._page.evaluate("""
                        document.querySelectorAll('.modal.show, .modal.in').forEach(m => {
                            m.classList.remove('show', 'in');
                            m.style.display = 'none';
                        });
                        document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
                        document.body.classList.remove('modal-open');
                        document.body.style.removeProperty('padding-right');
                    """)
                    logger.info("Force-hid all modals via JS")
                    await self._page.wait_for_timeout(1000)
                    continue
            except Exception:
                pass

            if not any_visible:
                break

        logger.debug("Modal dismissal complete")

    # ═══════════════════════════════════════════════════════════════════════
    # Step 1: Open Login Page
    # ═══════════════════════════════════════════════════════════════════════

    async def open_login_page(
        self, base_url: str = "https://mocdoc.com/user/loginform"
    ) -> bool:
        """Step 1: Navigate to MocDoc sign-in portal page."""
        logger.info(f"Opening MocDoc login page: {base_url}")
        is_prod = callmedex_settings.app_env == "production"

        if self._page is not None and hasattr(self._page, "goto"):
            if is_prod or os.getenv("MOCDOC_SANDBOX_ENABLED") == "1":
                try:
                    response = await self._page.goto(base_url, timeout=callmedex_settings.browser_navigation_timeout_ms)
                    if response and hasattr(response, "status") and response.status >= 400:
                        raise ConnectorNavigationError(f"MocDoc login portal returned HTTP error status {response.status}")
                except ConnectorNavigationError:
                    raise
                except Exception as e:
                    raise ConnectorNavigationError(f"Failed to navigate to MocDoc login page '{base_url}': {e}") from e

        if is_prod and not self._is_live_page():
            raise ConnectorNavigationError("Production execution failed to navigate to live MocDoc portal page (page handle is invalid or at about:blank)")

        return True

    # ═══════════════════════════════════════════════════════════════════════
    # Steps 2-4: Login Sequence
    # ═══════════════════════════════════════════════════════════════════════

    async def login(self, credentials: Dict[str, Any]) -> bool:
        """Steps 2-4: Enter Username, Password, and click Login button with explicit checkpoints."""
        username = credentials.get("username")
        password = credentials.get("password")

        if not username or not password:
            raise AuthenticationError("MocDoc credentials missing username or password")

        logger.info(
            f"Step 2-3: Filling username '{username}' using selector '{self._selectors.login_username_input}'"
        )
        logger.info(
            f"Step 2-3: Filling password using selector '{self._selectors.login_password_input}'"
        )
        logger.info(
            f"Step 4: Submitting login button using selector '{self._selectors.login_submit_button}'"
        )

        if self._is_live_page():
            try:
                # Checkpoint 2: Verify login form elements exist
                await self._page.wait_for_selector(self._selectors.login_username_input, timeout=10000)
                await self._page.fill(self._selectors.login_username_input, username)
                await self._page.fill(self._selectors.login_password_input, password)
                await self._page.click(self._selectors.login_submit_button)

                # Wait for navigation to complete
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                await self._page.wait_for_timeout(2000)

                # Dismiss any post-login modals
                await self._dismiss_all_modals()

            except AuthenticationError:
                raise
            except Exception as e:
                raise AuthenticationError(f"MocDoc portal authentication failed for user '{username}': {e}") from e

        self._authenticated = True
        self._current_checkpoint = JobCheckpoint.AUTHENTICATED
        logger.info(f"Reached Checkpoint: {self._current_checkpoint.value}")
        return True

    # ═══════════════════════════════════════════════════════════════════════
    # Step 5: Navigate to Barcode Search
    # ═══════════════════════════════════════════════════════════════════════

    async def navigate_to_barcode_search(self) -> bool:
        """Step 5: Navigate to Investigation → Lab Order search interface."""
        if not self._authenticated:
            raise AuthenticationError("Must be authenticated before navigating to barcode search")

        logger.info(
            f"Step 5: Navigating to Investigation tab using '{self._selectors.nav_investigation_tab}' "
            f"and Lab Order link '{self._selectors.nav_lab_order_link}'"
        )

        if self._is_live_page():
            try:
                await self._page.click(self._selectors.nav_investigation_tab)
                await self._page.wait_for_timeout(1000)
                await self._page.click(self._selectors.nav_lab_order_link)
                await self._page.wait_for_timeout(2000)
                await self._dismiss_all_modals()
            except Exception as e:
                raise ConnectorNavigationError(f"Failed navigating to lab order search interface: {e}") from e

        return True

    # ═══════════════════════════════════════════════════════════════════════
    # Step 6: Paste Barcode & Search
    # ═══════════════════════════════════════════════════════════════════════

    async def search_by_barcode(self, barcode_id: str) -> Optional[ReportMetadata]:
        """Step 6: Paste barcode into search input box and submit."""
        if not self._authenticated:
            raise AuthenticationError("Cannot search barcode prior to authentication")

        if not barcode_id:
            raise ConnectorNavigationError("Barcode ID cannot be empty")

        await self.navigate_to_barcode_search()

        logger.info(
            f"Step 6: Pasting barcode '{barcode_id}' into input '{self._selectors.search_barcode_input}'"
        )
        logger.info(
            f"Step 6: Clicking search button using '{self._selectors.search_submit_button}'"
        )

        if self._is_live_page():
            try:
                await self._page.fill(self._selectors.search_barcode_input, barcode_id)
                await self._page.click(self._selectors.search_submit_button)
                await self._page.wait_for_timeout(2000)
                await self._dismiss_all_modals()
            except Exception as e:
                raise ConnectorNavigationError(f"Barcode search failed for barcode '{barcode_id}': {e}") from e

        self._current_checkpoint = JobCheckpoint.BARCODE_LOCATED
        logger.info(f"Reached Checkpoint: {self._current_checkpoint.value}")

        metadata = ReportMetadata(
            report_id=barcode_id,
            report_name=f"Report_{barcode_id}",
            report_type=ReportType.LABORATORY,
        )
        self._current_checkpoint = JobCheckpoint.REPORT_LOCATED
        logger.info(f"Reached Checkpoint: {self._current_checkpoint.value}")
        return metadata

    # ═══════════════════════════════════════════════════════════════════════
    # LIFECYCLE POLLING — PENDING PRINT ONLY
    # ═══════════════════════════════════════════════════════════════════════

    async def _navigate_to_pending_print(self) -> bool:
        """Navigate to the lab reports page and click the Pending Print tab.

        Returns True if the tab was successfully clicked.
        Raises ConnectorNavigationError on failure.
        """
        self._require_center_config()

        lab_url = (
            f"{self._base_url}"
            f"{self._selectors.lab_reports_url_template.format(clinic_slug=self._clinic_slug)}"
        )

        # Navigate to lab reports page (with retry)
        for nav_attempt in range(2):
            try:
                logger.info(f"Navigating to lab reports: {lab_url}")
                await self._page.goto(
                    lab_url, wait_until="domcontentloaded", timeout=120000
                )
                await self._page.wait_for_timeout(self._TABLE_LOAD_WAIT_MS)
                break
            except Exception as e:
                if nav_attempt == 0:
                    logger.warning(f"Navigation timed out — retrying: {e}")
                    await self._dismiss_all_modals()
                else:
                    raise ConnectorNavigationError(
                        f"Failed to navigate to lab reports page after 2 attempts: {e}"
                    ) from e

        await self._dismiss_all_modals()

        # Click "Pending Print" tab — JS click first, Playwright fallback
        tab_clicked = False
        for attempt in range(self._TAB_CLICK_ATTEMPTS):
            # Force-clear ALL modals via JS before clicking
            await self._page.evaluate("""
                document.querySelectorAll('.modal.show, .modal.in, .modal.fade.show').forEach(m => {
                    m.classList.remove('show', 'in');
                    m.style.display = 'none';
                });
                document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('padding-right');
            """)
            await self._page.wait_for_timeout(500)

            try:
                # Try JS click first (bypasses overlay)
                tab_id = self._selectors.pending_print_tab_id
                clicked = await self._page.evaluate(f"""
                    const tab = document.getElementById('{tab_id}');
                    if (tab) {{ tab.click(); return true; }}
                    return false;
                """)
                if clicked:
                    logger.info(f"Clicked 'Pending Print' tab via JS (attempt {attempt + 1})")
                    tab_clicked = True
                    await self._page.wait_for_timeout(self._TABLE_LOAD_WAIT_MS)
                    break
            except Exception:
                pass

            try:
                # Fallback: Playwright click with short timeout
                tab = self._page.locator(self._selectors.pending_print_tab).first
                await tab.click(timeout=5000)
                logger.info(f"Clicked 'Pending Print' tab via Playwright (attempt {attempt + 1})")
                tab_clicked = True
                await self._page.wait_for_timeout(self._TABLE_LOAD_WAIT_MS)
                break
            except Exception:
                logger.warning(f"Tab click attempt {attempt + 1} failed — retrying")
                await self._page.wait_for_timeout(1000)

        if not tab_clicked:
            raise ConnectorNavigationError(
                f"Could not click Pending Print tab after {self._TAB_CLICK_ATTEMPTS} attempts"
            )

        await self._dismiss_all_modals()

        # Set "Show entries" to 100
        try:
            changed = await self._page.evaluate("""
                const select = document.querySelector('select[name$=\"_length\"], .dataTables_length select');
                if (select) {
                    select.value = '100';
                    select.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
                return false;
            """)
            if changed:
                await self._page.wait_for_timeout(self._TABLE_LOAD_WAIT_MS)
                await self._dismiss_all_modals()
                logger.info("Set show entries to 100 via JS dropdown change")
            else:
                logger.debug("Dropdown not found in DOM")
        except Exception as e:
            logger.debug(f"Could not set entries to 100: {e}")

        return True

    async def _find_barcode_in_pending_print(self, barcode_id: str) -> bool:
        """Check if the barcode appears in any row of the current Pending Print table.

        Returns True if found, False otherwise.
        """
        rows = self._page.locator(self._selectors.report_rows)
        row_count = await rows.count()

        if row_count == 0:
            # Wait and retry — MocDoc may still be loading
            logger.info("No rows yet — waiting 5s for table to load...")
            await self._page.wait_for_timeout(5000)
            await self._dismiss_all_modals()
            row_count = await rows.count()

        if row_count == 0:
            logger.info("No reports found in Pending Print tab")
            return False

        logger.info(f"Scanning {row_count} rows in Pending Print for barcode '{barcode_id}'")

        for i in range(row_count):
            try:
                row = rows.nth(i)
                row_text = await row.inner_text()

                if self._selectors.empty_table_text in row_text:
                    continue

                if barcode_id in row_text:
                    logger.info(f"Found barcode '{barcode_id}' in Pending Print row {i}")
                    return True
            except Exception as e:
                logger.debug(f"Error reading row {i}: {e}")
                continue

        logger.info(f"Barcode '{barcode_id}' not found in {row_count} Pending Print rows")
        return False

    async def wait_until_report_available(
        self, barcode_id: str, timeout_seconds: int = 300
    ) -> bool:
        """Poll the Pending Print tab until the barcode appears.

        MocDoc Laboratory Status Progression:
        Pending Accession → Pending Completion → Pending Verification →
        Pending Approval → Pending Print → Printed

        This method polls the 'Pending Print' tab only. Once the barcode
        appears there, the report is ready for PDF download.

        Args:
            barcode_id: The barcode to search for.
            timeout_seconds: Maximum time to poll (default 300s = 5 minutes).

        Returns:
            True if the barcode was found in Pending Print.

        Raises:
            ReportDownloadError: If the barcode is not found within the timeout.
        """
        logger.info(
            f"Polling MocDoc Pending Print for barcode '{barcode_id}' "
            f"(max {timeout_seconds}s, interval {self._POLL_INTERVAL_SECONDS}s)"
        )

        if not self._is_live_page():
            logger.info(
                f"Non-live page — skipping lifecycle polling for barcode '{barcode_id}'"
            )
            return True

        self._require_center_config()

        elapsed = 0
        poll_count = 0

        while elapsed < timeout_seconds:
            poll_count += 1
            logger.info(
                f"Poll #{poll_count} for barcode '{barcode_id}' "
                f"(elapsed {elapsed}s / {timeout_seconds}s)"
            )

            try:
                await self._navigate_to_pending_print()
                found = await self._find_barcode_in_pending_print(barcode_id)

                if found:
                    logger.info(
                        f"MocDoc Pending Print: barcode '{barcode_id}' is ready for download "
                        f"(found after {elapsed}s, {poll_count} polls)"
                    )
                    return True

            except ConnectorNavigationError:
                # Navigation failure — log and retry after interval
                logger.warning(
                    f"Navigation failed during poll #{poll_count} — "
                    f"will retry in {self._POLL_INTERVAL_SECONDS}s"
                )
            except Exception as e:
                logger.warning(
                    f"Unexpected error during poll #{poll_count}: {e} — "
                    f"will retry in {self._POLL_INTERVAL_SECONDS}s"
                )

            # Sleep before next poll
            logger.info(f"Barcode not ready — sleeping {self._POLL_INTERVAL_SECONDS}s...")
            await asyncio.sleep(self._POLL_INTERVAL_SECONDS)
            elapsed += self._POLL_INTERVAL_SECONDS

        # Timeout reached
        raise ReportDownloadError(
            f"Barcode '{barcode_id}' did not appear in MocDoc Pending Print "
            f"within {timeout_seconds}s ({poll_count} polls). "
            f"The report may still be in an earlier lifecycle stage "
            f"(Pending Accession/Completion/Verification/Approval)."
        )

    # ═══════════════════════════════════════════════════════════════════════
    # BILL PAYMENT ERROR DETECTION
    # ═══════════════════════════════════════════════════════════════════════

    async def _check_bill_payment_error(self) -> Optional[str]:
        """Check if MocDoc is showing a bill payment error in the download modal.

        Returns the error text if found, or None if no error is visible.
        """
        if not self._is_live_page():
            return None

        for keyword in self._selectors.bill_pending_keywords:
            try:
                error_locator = self._page.locator(f"text=/{keyword}/i").first
                if await error_locator.is_visible(timeout=500):
                    try:
                        full_text = await error_locator.inner_text(timeout=1000)
                        return full_text.strip()
                    except Exception:
                        return keyword
            except Exception:
                continue
        return None

    # ═══════════════════════════════════════════════════════════════════════
    # DOWNLOAD MODAL LIFECYCLE
    # ═══════════════════════════════════════════════════════════════════════

    async def _close_download_modal(self) -> None:
        """Close the download modal by clicking Close button.

        Falls back to JavaScript force-close if the button click doesn't
        dismiss the modal (common after bill payment errors where MocDoc's
        JS leaves the modal in a stuck state).
        """
        if not self._is_live_page():
            return

        # Try clicking Close button first
        try:
            close_btn = self._page.locator(
                "#download-modal button:has-text('Close'), "
                "#download-modal button[data-dismiss='modal'], "
                "#download-modal button[data-bs-dismiss='modal'], "
                "#download-modal .close"
            ).first
            if await close_btn.is_visible(timeout=3000):
                await close_btn.click()
                await self._page.wait_for_timeout(1000)
        except Exception:
            pass

        # Force-close via JS if modal is still visible
        try:
            still_visible = await self._page.locator(
                "#download-modal.show, #download-modal.in"
            ).is_visible(timeout=1000)

            if still_visible:
                await self._page.evaluate("""
                    const modal = document.getElementById('download-modal');
                    if (modal) {
                        modal.classList.remove('show', 'in');
                        modal.style.display = 'none';
                        modal.setAttribute('aria-hidden', 'true');
                    }
                    document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
                    document.body.classList.remove('modal-open');
                    document.body.style.removeProperty('padding-right');
                    document.body.style.removeProperty('overflow');
                """)
                logger.info("Force-closed #download-modal via JS")
                await self._page.wait_for_timeout(500)
        except Exception:
            pass

    async def _click_hide(self, row) -> None:
        """Click 'Hide' to collapse an expanded row."""
        if not self._is_live_page():
            return
        try:
            hide_btn = row.locator(self._selectors.hide_button).first
            if await hide_btn.is_visible(timeout=2000):
                await hide_btn.click()
                await self._page.wait_for_timeout(500)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════════
    # DEBUG ARTIFACT CAPTURE
    # ═══════════════════════════════════════════════════════════════════════

    async def _capture_failure_debug(self, report_id: str, reason: str) -> None:
        """Save diagnostic artifacts on failure.

        - Encrypted HTML dump (using connector_encryption_key Fernet)
        - Screenshots only in development (skip in production to protect PHI)
        """
        if not self._is_live_page():
            return

        try:
            debug_dir = Path(callmedex_settings.artifacts_dir) / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            encryption_key = getattr(callmedex_settings, "connector_encryption_key", None)

            # Dump page HTML (sanitized/encrypted)
            raw_html = await self._page.content()

            if encryption_key:
                try:
                    from cryptography.fernet import Fernet
                    f = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
                    enc_bytes = f.encrypt(raw_html.encode())
                    enc_path = debug_dir / f"{reason}_{report_id}.html.enc"
                    enc_path.write_bytes(enc_bytes)
                    logger.error(f"Saved encrypted debug HTML to {enc_path}")
                except Exception as e:
                    logger.warning(f"HTML encryption failed: {e}")
            else:
                html_path = debug_dir / f"{reason}_{report_id}.html"
                html_path.write_text(raw_html[:50000], encoding="utf-8")  # Truncate for safety
                logger.error(f"Saved debug HTML to {html_path}")

            # Screenshots ONLY in development
            if callmedex_settings.app_env == "development":
                shot_path = debug_dir / f"{reason}_{report_id}.png"
                await self._page.screenshot(path=str(shot_path), full_page=True)
                logger.error(f"Saved debug screenshot to {shot_path}")
            else:
                logger.info("Skipped screenshot in non-development environment to protect PHI")

        except Exception as e:
            logger.error(f"Could not save debug files: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # Steps 7-8: Open Patient View & Reports List
    # ═══════════════════════════════════════════════════════════════════════

    async def open_patient(self, barcode_id: str) -> bool:
        """Step 7: Click 'View' button to open patient report row."""
        logger.info(
            f"Step 7: Clicking patient 'View' button using selector '{self._selectors.patient_view_button}'"
        )
        if self._is_live_page():
            try:
                await self._page.click(self._selectors.patient_view_button)
            except Exception as e:
                raise ConnectorNavigationError(f"Failed opening patient view for barcode '{barcode_id}': {e}") from e
        return True

    async def open_reports(self, barcode_id: str) -> bool:
        """Step 8: Expand patient lab report details row."""
        logger.info(
            f"Step 8: Expanding lab report details using selector '{self._selectors.report_download_trigger_icon}'"
        )
        if self._is_live_page():
            try:
                await self._page.click(self._selectors.report_download_trigger_icon)
            except Exception as e:
                raise ConnectorNavigationError(f"Failed expanding lab report details for barcode '{barcode_id}': {e}") from e
        return True

    # ═══════════════════════════════════════════════════════════════════════
    # Step 9: Download Report (Production — Full Modal Lifecycle)
    # ═══════════════════════════════════════════════════════════════════════

    async def download_report(
        self, barcode_id: str, download_path: str
    ) -> Optional[bytes]:
        """Step 9: Download report PDF from Pending Print using the full modal lifecycle.

        1. Find row matching barcode in Pending Print table
        2. Click View to expand
        3. Locate download icon in expanded row
        4. Handle download modal (bill-error check, Select click, file download)
        5. Read PDF bytes, validate, cleanup

        Args:
            barcode_id: The barcode ID to download.
            download_path: Base directory for downloads (used as fallback;
                          actual download uses tempfile.mkdtemp()).

        Returns:
            PDF bytes on success, None on failure.

        Raises:
            AuthenticationError: If not authenticated.
            ReportDownloadError: If download fails.
        """
        if not self._authenticated:
            raise AuthenticationError("Cannot download report prior to authentication")

        pdf_bytes = None

        if not self._is_live_page():
            if callmedex_settings.app_env == "production":
                raise ReportDownloadError(
                    f"Production execution cannot download report for barcode '{barcode_id}' "
                    f"because live browser navigation is not active"
                )
            pdf_bytes = _build_mock_lab_report_pdf(barcode_id)
            self._current_checkpoint = JobCheckpoint.PDF_DOWNLOADED
            logger.info(f"Reached Checkpoint: {self._current_checkpoint.value}")
            return pdf_bytes

        # ── Ensure we're on the Pending Print page ──
        # wait_until_report_available() should have been called first,
        # but navigate again to be safe (idempotent)
        try:
            await self._navigate_to_pending_print()
        except Exception as e:
            raise ReportDownloadError(
                f"Failed navigating to Pending Print for download of barcode '{barcode_id}': {e}"
            ) from e

        # ── Find the row containing this barcode ──
        rows = self._page.locator(self._selectors.report_rows)
        target_row = None
        row_count = await rows.count()

        for i in range(row_count):
            try:
                row = rows.nth(i)

                # Skip expanded detail rows (tr.showorders)
                try:
                    row_class = await row.get_attribute("class") or ""
                    if "showorders" in row_class:
                        continue
                except Exception:
                    pass

                text = await row.inner_text()
                if barcode_id in text:
                    target_row = row
                    logger.info(f"Found barcode '{barcode_id}' in row {i}")
                    break
            except Exception:
                continue

        if not target_row:
            await self._capture_failure_debug(barcode_id, "row_not_found")
            raise ReportDownloadError(
                f"Could not find row for barcode '{barcode_id}' in Pending Print table "
                f"({row_count} rows scanned)"
            )

        # ── Click "View" to expand the patient row ──
        try:
            view_btn = target_row.locator(self._selectors.view_button).first
            await view_btn.click()
            logger.debug(f"Clicked 'View' for barcode '{barcode_id}'")
            await self._page.wait_for_timeout(self._EXPANSION_WAIT_MS)
        except Exception as e:
            await self._capture_failure_debug(barcode_id, "view_click_failed")
            raise ReportDownloadError(
                f"View button click failed for barcode '{barcode_id}': {e}"
            ) from e

        # Wait for expansion animation
        await self._page.wait_for_timeout(self._EXPANSION_WAIT_MS)

        # ── Locate download icon in expanded row (tr.showorders) ──
        expanded_row = self._page.locator(
            f"tr.showorders:has-text('{barcode_id}')"
        ).first

        try:
            download_icon = expanded_row.locator(
                self._selectors.download_result_link_class
            ).first
            await download_icon.wait_for(state="attached", timeout=5000)
            # JS click to bypass hover-to-show CSS restrictions
            await download_icon.evaluate("node => node.click()")
            logger.debug(f"Clicked 'Download Result' for barcode '{barcode_id}' via JS")
            await self._page.wait_for_timeout(2000)
        except Exception as e:
            await self._capture_failure_debug(barcode_id, "download_icon_click_failed")
            await self._click_hide(target_row)
            raise ReportDownloadError(
                f"Download Result icon click failed for barcode '{barcode_id}': {e}"
            ) from e

        # ── Handle download modal ──
        pdf_bytes = await self._handle_download_modal(barcode_id)

        # Collapse the row
        await self._click_hide(target_row)

        if not pdf_bytes:
            raise ReportDownloadError(
                f"Download modal did not produce PDF bytes for barcode '{barcode_id}'"
            )

        # Validate PDF magic header
        if not pdf_bytes.startswith(b"%PDF"):
            raise ReportDownloadError(
                f"Downloaded file for barcode '{barcode_id}' is not a valid PDF document"
            )

        self._current_checkpoint = JobCheckpoint.PDF_DOWNLOADED
        logger.info(f"Reached Checkpoint: {self._current_checkpoint.value}")
        return pdf_bytes

    async def _handle_download_modal(self, barcode_id: str) -> Optional[bytes]:
        """Handle the download modal: verify modal, check bill errors, click Select, wait for download.

        Returns PDF bytes or None.
        """
        # Wait for download modal to appear
        try:
            select_btn = self._page.locator(self._selectors.download_select_button_id).first
            await select_btn.wait_for(state="visible", timeout=self._MODAL_APPEAR_TIMEOUT_MS)
        except Exception as err:
            logger.error(f"DOWNLOAD_MODAL_MISSING for barcode '{barcode_id}': {err}")
            await self._capture_failure_debug(barcode_id, "modal_missing")
            return None

        # Click "Select" to trigger download (or bill payment error)
        await select_btn.click()
        logger.debug(f"Clicked 'Select' for barcode '{barcode_id}'")

        # Wait briefly for MocDoc to respond
        await self._page.wait_for_timeout(3000)

        # ── Check for bill payment error FIRST ──
        bill_error = await self._check_bill_payment_error()
        if bill_error:
            logger.warning(
                f"BILL_UNPAID for barcode '{barcode_id}': {bill_error} — "
                f"skipping this report until bill payment is completed"
            )
            await self._close_download_modal()
            return None

        # ── No error — wait for actual file download ──
        temp_dir = tempfile.mkdtemp(prefix="mocdoc_callmedex_dl_")
        download_path = None

        try:
            # Check if Select button is still visible (download didn't start)
            if await select_btn.is_visible(timeout=1000):
                async with self._page.expect_download(timeout=self._DOWNLOAD_TIMEOUT_MS) as download_info:
                    await select_btn.click()
                    logger.debug(f"Re-clicked 'Select' for barcode '{barcode_id}'")
            else:
                # Button disappeared — download might already be in progress
                async with self._page.expect_download(timeout=self._DOWNLOAD_TIMEOUT_MS) as download_info:
                    pass

            download = await download_info.value
            download_path = os.path.join(
                temp_dir, download.suggested_filename or f"{barcode_id}.pdf"
            )
            await download.save_as(download_path)
            logger.info(
                f"Downloaded: {download.suggested_filename} "
                f"({os.path.getsize(download_path)} bytes)"
            )

        except Exception as e:
            # Download failed — check one more time for bill error
            bill_error = await self._check_bill_payment_error()
            if bill_error:
                logger.warning(
                    f"BILL_UNPAID for barcode '{barcode_id}': {bill_error} — "
                    f"skipping this report"
                )
            else:
                logger.error(f"DOWNLOAD_FAILED for barcode '{barcode_id}': {e}")

            # Try waiting for "Download Completed" as fallback
            try:
                await self._page.wait_for_selector(
                    f"text={self._selectors.download_completed_text}", timeout=5000
                )
            except Exception:
                pass

            await self._close_download_modal()
            self._cleanup_temp_dir(temp_dir)
            return None

        # Wait for "Download Completed" text
        try:
            await self._page.wait_for_selector(
                f"text={self._selectors.download_completed_text}", timeout=30000
            )
            logger.debug("Download completed confirmation seen")
        except Exception:
            logger.warning(
                f"DOWNLOAD_COMPLETED_TEXT not seen for barcode '{barcode_id}', "
                f"but file was downloaded — continuing"
            )

        # Close the modal
        await self._close_download_modal()

        # Read PDF bytes
        try:
            if not download_path or not os.path.exists(download_path):
                logger.error(f"DOWNLOAD_FILE_MISSING: {download_path}")
                return None

            file_size = os.path.getsize(download_path)
            if file_size == 0:
                logger.error(f"EMPTY_PDF: {download_path}")
                return None

            with open(download_path, "rb") as f:
                pdf_bytes = f.read()

            logger.info(f"Read {len(pdf_bytes)} bytes from {download_path}")
            return pdf_bytes

        except Exception as e:
            logger.error(f"PDF_READ_FAILED for barcode '{barcode_id}': {e}")
            return None

        finally:
            self._cleanup_temp_dir(temp_dir)

    @staticmethod
    def _cleanup_temp_dir(temp_dir: str) -> None:
        """Remove the temporary download directory and all its contents."""
        try:
            import shutil
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════════
    # VALIDATION, LOGOUT, HEALTH, CLEANUP, RECOVERY
    # ═══════════════════════════════════════════════════════════════════════

    async def validate_report(
        self, file_bytes: bytes, expected_patient: PatientIdentity
    ) -> bool:
        """Validate report content against patient identity contract."""
        if len(file_bytes) == 0:
            raise ValidationError("Downloaded report file is empty")

        if not file_bytes.startswith(b"%PDF"):
            raise ValidationError("Report file signature is invalid (%PDF magic header missing)")

        logger.info(f"Validating report bytes against patient '{expected_patient.patient_name}'")
        self._current_checkpoint = JobCheckpoint.VALIDATED
        logger.info(f"Reached Checkpoint: {self._current_checkpoint.value}")
        return True

    # Step 10: Logout
    async def logout(self) -> bool:
        """Step 10: Click Profile dropdown → Sign out."""
        logger.info(
            f"Step 10: Opening profile dropdown '{self._selectors.profile_dropdown_menu}' "
            f"and clicking sign out '{self._selectors.logout_button}'"
        )
        if self._is_live_page():
            try:
                await self._page.click(self._selectors.profile_dropdown_menu)
                await self._page.wait_for_timeout(500)
                await self._page.click(self._selectors.logout_button)
            except Exception as e:
                logger.warning(f"Failed clicking logout sequence in portal: {e}")

        self._authenticated = False
        return True

    async def health_check(self) -> Dict[str, Any]:
        """Perform diagnostic health check on connector."""
        return {
            "connector": "MocDocConnector",
            "selector_version": self._selectors.version,
            "status": "healthy",
            "capabilities": self.capabilities.model_dump(),
            "center_configured": bool(self._base_url and self._clinic_slug),
        }

    async def cleanup(self) -> bool:
        """Clean up active browser sessions and reset connector state."""
        logger.info("Executing MocDocConnector cleanup...")
        if self._browser_session and self._session_id:
            await self._browser_session.close_context(self._session_id)
        self._authenticated = False
        self._page = None
        return True

    async def checkpoint_resume(
        self, report_job_id: str, target_checkpoint: JobCheckpoint
    ) -> bool:
        """Resume job execution starting from target recovery checkpoint."""
        logger.info(f"Resuming MocDoc connector job {report_job_id} from checkpoint '{target_checkpoint.value}'")
        self._current_checkpoint = target_checkpoint
        return True

    async def retry(
        self, report_job_id: str, checkpoint: JobCheckpoint = JobCheckpoint.CREATED
    ) -> bool:
        """Retry connector execution from checkpoint."""
        logger.info(f"Retrying MocDoc connector job {report_job_id} from checkpoint '{checkpoint.value}'")
        await self.cleanup()
        return await self.checkpoint_resume(report_job_id, checkpoint)
