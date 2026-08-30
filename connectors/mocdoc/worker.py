"""MocDoc Playwright Worker — Browser automation for lab report extraction.

This module logs into MocDoc's web interface, navigates to the lab reports
page, and downloads approved reports from the "Pending Print" tab. Each
downloaded PDF is POSTed to MedAssist AI's internal integration API.

Selectors are NOT hardcoded here — they live in selectors.py (single file
to update if MocDoc changes their UI).
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from connectors.base import HospitalConnector, ReportMetadata
from connectors.mocdoc import selectors as S
from app.config import settings
from app.utils.pii_sanitizer import sanitize_report_text
from app.database import sb  # T5.1: off-loop query execution

logger = logging.getLogger(__name__)


def _parse_patient_cell(cell_text: str) -> dict:
    """Extract patient name, VAM ID, and phone from a MocDoc table cell.

    Example input:
        "Mrs.C Varalakshmi
         Gender: F Age: 60 years
         ID: VAM-39927 Mobile: +918121363550"

    Returns:
        {"patient_name": "Mrs.C Varalakshmi",
         "vam_id": "VAM-39927",
         "phone": "+918121363550"}
    """
    lines = [line.strip() for line in cell_text.strip().split("\n") if line.strip()]
    patient_name = lines[0] if lines else ""

    # Extract VAM ID: "ID: VAM-39927"
    vam_match = re.search(r"ID:\s*(VAM-\d+)", cell_text)
    vam_id = vam_match.group(1) if vam_match else None

    # Extract phone: "Mobile: +918121363550"
    phone_match = re.search(r"Mobile:\s*(\+?\d{10,15})", cell_text)
    phone = phone_match.group(1) if phone_match else None

    # Normalize to E.164 format
    if phone:
        from app.utils.validators import normalize_phone
        phone = normalize_phone(phone.lstrip("+"))

    return {
        "patient_name": patient_name,
        "vam_id": vam_id,
        "phone": phone,
    }


def _parse_test_details(expanded_text: str) -> dict:
    """Extract test name, report number, and sample ID from an expanded row.

    Example input:
        "COMPLETE BLOOD COUNT - 3P    No: 29220    08/07/2026  Track Sample
         SampleID: 260700007335"

    Returns:
        {"report_no": "29220", "sample_id": "260700007335"}
    """
    # Report number: "No: 29220"
    no_match = re.search(r"No:\s*(\d+)", expanded_text)
    report_no = no_match.group(1) if no_match else None

    # Sample ID: "SampleID: 260700007335"
    sample_match = re.search(r"SampleID:\s*(\d+)", expanded_text)
    sample_id = sample_match.group(1) if sample_match else None

    return {
        "report_no": report_no,
        "sample_id": sample_id,
    }


class MocDocConnector(HospitalConnector):
    """Playwright-based browser automation for MocDoc HMIS.

    Flow:
        1. Login (or restore session cookies)
        2. Navigate to lab reports → "Pending Print" tab
        3. Parse patient table → extract name, phone, VAM ID
        4. For each new report: View → Download → POST to MedAssist
    """

    CONFIG_SCHEMA = [
        {"key": "username", "label": "Username", "type": "text", "placeholder": "MocDoc login ID", "required": True},
        {"key": "password", "label": "Password", "type": "password", "placeholder": "Leave blank to keep existing", "required": True},
        {"key": "clinic_slug", "label": "Clinic Slug", "type": "text", "placeholder": "e.g. visakha-multispeciality-clinics", "required": False},
        {"key": "base_url", "label": "Base URL", "type": "text", "placeholder": "https://mocdoc.com", "required": False},
    ]

    def __init__(self, clinic_id: str, config: dict, medassist_url: str,
                 integration_secret: str, session_dir: str, branch_id: str = None):
        super().__init__(
            clinic_id=clinic_id,
            connector_type="mocdoc",
            config=config,
            medassist_url=medassist_url,
            integration_secret=integration_secret,
        )
        raw_url = (config.get("base_url") or "https://mocdoc.com").strip()
        if raw_url and not raw_url.startswith(("http://", "https://")):
            raw_url = f"https://{raw_url}"
        from urllib.parse import urlparse
        parsed = urlparse(raw_url)
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc or parsed.path.split("/")[0]
        self.base_url = f"{scheme}://{netloc}".rstrip("/")
        self.username = config.get("username", "")
        self.password = config.get("password", "")  # Already decrypted by runner
        self.clinic_slug = config.get("clinic_slug", "")
        self.branch_id = branch_id
        self.session_dir = session_dir
        # Branch-scoped session file — two branches of the same clinic have
        # separate MocDoc logins and must never share cookies.
        session_key = f"{clinic_id}_{branch_id}" if branch_id else clinic_id
        self.session_file = os.path.join(session_dir, f"mocdoc_{session_key}.json")
        self.download_dir = tempfile.mkdtemp(prefix="mocdoc_downloads_")

        # Playwright objects (initialized in authenticate())
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

        # Track already-processed report IDs for this run
        self._processed_ids: set = set()

        # Track which table row indices we've already attempted (handles
        # duplicate VAM IDs — same patient with multiple tests)
        self._processed_row_indices: set = set()

    async def _init_browser(self) -> None:
        """Launch headless Chromium with download directory configured.

        If Chromium is not installed (common on Render's native Python
        buildpack), attempts a one-time runtime install before retrying.

        NOTE: The child watcher needed by asyncio subprocess transport is
        reinstalled by the runner before each connector invocation (see
        runner.py _ensure_subprocess_support).  This prevents the
        NotImplementedError that occurs when a previous Playwright stop()
        leaves the watcher in a stale state.
        """
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
        except Exception as e:
            from app.utils.browser_errors import (
                is_missing_browser_error,
                _try_install_chromium,
                friendly_browser_launch_error,
            )
            if is_missing_browser_error(e) and _try_install_chromium():
                # Retry after successful install
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ],
                )
            else:
                raise RuntimeError(friendly_browser_launch_error(e)) from e
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(30000)  # 30 seconds

        # Block non-MocDoc requests (security + speed)
        await self._page.route(
            "**/*",
            lambda route: (
                route.continue_()
                if "mocdoc.com" in route.request.url
                or route.request.resource_type in ("document", "script", "xhr", "fetch", "stylesheet")
                else route.abort()
            ),
        )

    async def _save_session(self) -> None:
        """Save browser cookies to an encrypted session file."""
        cookies = await self._context.cookies()
        session_data = {
            "cookies": cookies,
            "saved_at": time.time(),
        }
        os.makedirs(self.session_dir, exist_ok=True)
        raw_json = json.dumps(session_data)

        encryption_key = getattr(settings, "connector_encryption_key", "")
        if encryption_key:
            try:
                from cryptography.fernet import Fernet
                f = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
                encrypted = f.encrypt(raw_json.encode()).decode()
                payload = json.dumps({"encrypted": True, "data": encrypted})
                with open(self.session_file, "w") as file:
                    file.write(payload)
                logger.debug(f"Session saved (encrypted): {len(cookies)} cookies")
                return
            except Exception as e:
                logger.warning(f"Session encryption failed: {e}")

        with open(self.session_file, "w") as f:
            f.write(raw_json)
        logger.debug(f"Session saved (plain): {len(cookies)} cookies")

    async def _restore_session(self) -> bool:
        """Try to restore a previous session from cookies.

        Returns True if session is still valid (dashboard loads).
        Returns False if session expired or no session file exists.
        """
        if not os.path.exists(self.session_file):
            return False

        try:
            with open(self.session_file, "r") as f:
                content = f.read()

            try:
                parsed = json.loads(content)
            except Exception:
                return False

            if isinstance(parsed, dict) and parsed.get("encrypted") and "data" in parsed:
                encryption_key = getattr(settings, "connector_encryption_key", "")
                if not encryption_key:
                    logger.error("Session file is encrypted but no CONNECTOR_ENCRYPTION_KEY configured")
                    return False
                from cryptography.fernet import Fernet
                f = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
                decrypted_bytes = f.decrypt(parsed["data"].encode())
                session_data = json.loads(decrypted_bytes.decode())
            else:
                session_data = parsed

            # Check if session file is older than 12 hours
            saved_at = session_data.get("saved_at", 0)
            if time.time() - saved_at > 12 * 3600:
                logger.info("Session file older than 12 hours — forcing fresh login")
                return False

            cookies = session_data.get("cookies", [])
            if not cookies:
                return False

            await self._context.add_cookies(cookies)
            logger.info(f"Restored {len(cookies)} session cookies")

            # Test if session is still valid by navigating to dashboard
            await self._page.goto(
                f"{self.base_url}{S.DASHBOARD_URL_PATH}",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await self._page.wait_for_timeout(2000)

            # If we're on the dashboard, session is valid
            current_url = self._page.url
            if S.LOGIN_URL_PATH not in current_url:
                logger.info("Session restored successfully — skipping login")
                return True

            logger.info("Session expired — cookies invalid, will re-login")
            return False

        except Exception as e:
            logger.warning(f"Session restore failed: {e}")
            return False

    async def _login(self) -> bool:
        """Perform a full login using username/password.

        MocDoc's login page at /user/loginform generates a server-side session
        token.  On a cold browser (no cookies), the first form submission
        almost always returns "Your login screen session expired" because
        MocDoc needs a prior visit to seed its CSRF / session state.

        Strategy:
            1. Warm-up visit to /user/loginform (establishes server session)
            2. Reload the page (fresh token, now with valid session cookies)
            3. Fill and submit credentials
            4. Retry up to 3 times total for resilience
        """
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            logger.info(f"Login attempt {attempt}/{max_attempts}")

            # On the first attempt, do a warm-up visit before filling the form.
            # This seeds MocDoc's server session so the token is valid.
            if attempt == 1:
                warmup_url = f"{self.base_url}{S.LOGIN_URL_PATH}"
                logger.info(f"Warm-up visit to {warmup_url}")
                try:
                    await self._page.goto(warmup_url, wait_until="domcontentloaded", timeout=30000)
                    await self._page.wait_for_timeout(2000)
                except Exception as e:
                    logger.warning(f"Warm-up navigation failed: {e}")

            success = await self._try_login_once()
            if success:
                return True

            # Check for "session expired" text — a warm-up retry is likely to fix it
            if attempt < max_attempts:
                logger.info("Retrying login with a fresh page load...")
                await self._page.wait_for_timeout(1500)

        return False

    async def _try_login_once(self) -> bool:
        """Single login attempt: navigate, fill, submit, check result."""
        # Navigate directly to /user/loginform (the actual login page)
        login_url = f"{self.base_url}{S.LOGIN_URL_PATH}"
        logger.info(f"Navigating to login page: {login_url}")

        await self._page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        # Give MocDoc's JS time to set up the login form session token
        await self._page.wait_for_timeout(3000)

        # Wait for the username field to be visible and interactable
        try:
            await self._page.wait_for_selector(
                S.USERNAME_FIELD, state="visible", timeout=15000
            )
        except Exception:
            # Maybe the field has a different selector on /user/login
            # Try common alternatives
            alt_selectors = ["input[name='username']", "input[type='text']", "#username", "#email"]
            found = False
            for sel in alt_selectors:
                try:
                    await self._page.wait_for_selector(sel, state="visible", timeout=3000)
                    logger.info(f"Found username field with alt selector: {sel}")
                    found = True
                    break
                except Exception:
                    continue
            if not found:
                logger.error("LOGIN_SELECTOR_MISSING: no username field found on page")
                await self._capture_login_debug("no_username_field")
                return False

        # Small pause to let MocDoc's JS fully bind event handlers
        await self._page.wait_for_timeout(1000)

        # ── Fill credentials ──
        # MocDoc's jQuery listens for specific events to enable the login
        # button.  Neither Playwright's fill() nor type() reliably fires
        # the exact jQuery handlers.  We use a two-phase approach:
        #   Phase 1: Playwright type() for realistic keyboard input
        #   Phase 2: JS evaluate() to trigger jQuery events + force-enable btn
        username_field = self._page.locator(S.USERNAME_FIELD)
        password_field = self._page.locator(S.PASSWORD_FIELD)

        # Phase 1 — type into fields (provides realistic input events)
        await username_field.click()
        await username_field.fill("")
        await username_field.type(self.username, delay=30)
        await self._page.wait_for_timeout(200)

        await password_field.click()
        await password_field.fill("")
        await password_field.type(self.password, delay=30)
        await self._page.wait_for_timeout(300)

        # Phase 2 — ensure jQuery sees the values and enable the button.
        # MocDoc pages load jQuery, so we can trigger events directly.
        await self._page.evaluate("""([uSel, pSel, btnSel]) => {
            // Set values via DOM (in case type() missed anything)
            const uField = document.querySelector(uSel);
            const pField = document.querySelector(pSel);
            if (uField) {
                uField.dispatchEvent(new Event('input',  {bubbles: true}));
                uField.dispatchEvent(new Event('change', {bubbles: true}));
                uField.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }
            if (pField) {
                pField.dispatchEvent(new Event('input',  {bubbles: true}));
                pField.dispatchEvent(new Event('change', {bubbles: true}));
                pField.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }
            // Also try jQuery .trigger() if jQuery is loaded
            if (typeof $ !== 'undefined') {
                try { $(uSel).trigger('keyup').trigger('change'); } catch(e) {}
                try { $(pSel).trigger('keyup').trigger('change'); } catch(e) {}
            }
            // Force-remove disabled class from login button
            const btn = document.querySelector(btnSel);
            if (btn) {
                btn.classList.remove('btndisabled', 'disabled');
                btn.removeAttribute('disabled');
            }
        }""", [S.USERNAME_FIELD, S.PASSWORD_FIELD, S.LOGIN_BUTTON])
        await self._page.wait_for_timeout(300)

        # Click login button — page JS handles AES encryption and form submission
        login_btn = self._page.locator(S.LOGIN_BUTTON)
        await login_btn.click()

        # Wait for navigation or network to settle
        try:
            await self._page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await self._page.wait_for_timeout(2000)

        current_url = self._page.url

        # Success: navigated to dashboard or any non-login page
        login_paths = ("/user/login", "/user/loginform", "/ptuser/login")
        on_login_page = any(p in current_url for p in login_paths)
        if not on_login_page:
            logger.info(f"Login successful — navigated to {current_url}")
            await self._save_session()
            return True

        # Failed — capture diagnostics
        logger.error(f"Login failed: still on {current_url}")
        await self._capture_login_debug("login_failed")
        return False

    async def _capture_login_debug(self, reason: str) -> None:
        """Capture screenshot and page error text for login debugging."""
        try:
            screenshot_path = os.path.join(
                self.session_dir, f"login_debug_{reason}_{self.clinic_id}.png"
            )
            await self._page.screenshot(path=screenshot_path, full_page=True)
            logger.info(f"Saved debug screenshot to {screenshot_path}")

            # Extract any visible error text on the page
            error_text = ""
            for err_sel in [
                ".error", ".alert", ".alert-danger", "#errormsg",
                ".error-message", "p.error", ".text-danger"
            ]:
                try:
                    el = self._page.locator(err_sel).first
                    if await el.is_visible(timeout=1000):
                        error_text = await el.inner_text()
                        if error_text.strip():
                            logger.error(f"MocDoc page error: '{error_text.strip()}'")
                            break
                except Exception:
                    continue

            if not error_text:
                body_text = await self._page.locator("body").inner_text()
                lines = [l.strip() for l in body_text.split("\n") if l.strip()]
                summary = " | ".join(lines[:15])
                logger.error(f"Page content summary: {summary}")
        except Exception as e:
            logger.warning(f"Could not capture login debug info: {e}")

    async def _dismiss_all_modals(self) -> None:
        """Dismiss any modal dialogs that MocDoc may show.

        MocDoc uses multiple Bootstrap modals that can appear in sequence:
        1. #ms-loading-modal — loading spinner, must WAIT for it to disappear
        2. #md-info-modal — info/maintenance notice, must CLICK to dismiss
        3. Generic modals — try buttons or Escape

        We loop up to 5 times to handle cascading modals.
        """
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
        for attempt in range(5):
            any_visible = False

            # Try #md-info-modal first
            try:
                modal = self._page.locator("#md-info-modal.show, #md-info-modal.in")
                if await modal.is_visible(timeout=1500):
                    any_visible = True
                    logger.info(f"Detected #md-info-modal (attempt {attempt+1})")
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
                    # Try JS nuclear option — remove ALL modals and backdrops
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

    async def authenticate(self) -> bool:
        """Initialize browser and login (or restore session)."""
        await self._init_browser()

        # Try session restore first
        if await self._restore_session():
            await self._dismiss_all_modals()
            return True

        # Full login
        if not await self._login():
            return False

        await self._dismiss_all_modals()
        return True

    async def fetch_new_reports(self) -> list[ReportMetadata]:
        """Navigate to lab reports page, parse the Pending Print table.

        Returns a list of ReportMetadata for reports that have NOT been
        processed yet (based on local cache check).
        """
        reports = []

        # Navigate to lab reports page (with retry)
        lab_url = (
            f"{self.base_url}"
            f"{S.LAB_REPORTS_URL_TEMPLATE.format(clinic_slug=quote(self.clinic_slug, safe=''))}"
        )
        for nav_attempt in range(2):
            try:
                logger.info(f"Navigating to lab reports: {lab_url}")
                await self._page.goto(
                    lab_url, wait_until="domcontentloaded", timeout=120000
                )
                await self._page.wait_for_timeout(3000)
                break
            except Exception as e:
                if nav_attempt == 0:
                    logger.warning(f"Navigation timed out — retrying: {e}")
                    await self._dismiss_all_modals()
                else:
                    raise

        # Dismiss any modals that appear after navigation (e.g. #md-info-modal)
        await self._dismiss_all_modals()

        # Click "Pending Print" tab — use JS to bypass any modal overlay
        tab_clicked = False
        for attempt in range(3):
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
                clicked = await self._page.evaluate("""
                    const tab = document.getElementById('pendingprint');
                    if (tab) { tab.click(); return true; }
                    return false;
                """)
                if clicked:
                    logger.info(f"Clicked 'Pending Print' tab via JS (attempt {attempt+1})")
                    tab_clicked = True
                    await self._page.wait_for_timeout(3000)
                    break
            except Exception:
                pass

            try:
                # Fallback: Playwright click with short timeout
                tab = self._page.locator(S.PENDING_PRINT_TAB).first
                await tab.click(timeout=5000)
                logger.info(f"Clicked 'Pending Print' tab via Playwright (attempt {attempt+1})")
                tab_clicked = True
                await self._page.wait_for_timeout(3000)
                break
            except Exception:
                logger.warning(f"Tab click attempt {attempt+1} failed — retrying")
                await self._page.wait_for_timeout(1000)

        if not tab_clicked:
            logger.error("TAB_NOT_FOUND: Could not click Pending Print tab after 3 attempts")
            return reports

        # Dismiss modals that may appear after tab click
        await self._dismiss_all_modals()

        # Try to set "Show entries" to 100 (show all rows)
        try:
            # Force the dropdown to 100 via JS and trigger the change event
            changed = await self._page.evaluate("""
                const select = document.querySelector('select[name$="_length"], .dataTables_length select');
                if (select) {
                    select.value = '100';
                    select.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
                return false;
            """)
            if changed:
                await self._page.wait_for_timeout(3000)
                await self._dismiss_all_modals()
                logger.info("Set show entries to 100 via JS dropdown change")
            else:
                logger.debug("Dropdown not found in DOM")
        except Exception as e:
            logger.debug(f"Could not set entries to 100: {e}")

        # Preload processed IDs from database to eliminate duplicate processing
        try:
            from app.database import supabase
            processed_query = (
                supabase.table("integration_processed_reports")
                .select("external_report_id")
                .eq("clinic_id", self.clinic_id)
                .eq("connector_type", self.connector_type)
            )
            processed_res = await sb(processed_query)
            for r in (processed_res.data or []):
                if r.get("external_report_id"):
                    self._processed_ids.add(r["external_report_id"])
            logger.info(f"Preloaded {len(self._processed_ids)} processed report IDs from database")
        except Exception as e_p:
            logger.warning(f"Could not preload processed report IDs: {e_p}")

        # Parse table rows across pages (with pagination support)
        max_pages = 5
        for page_idx in range(max_pages):
            rows = self._page.locator(S.REPORT_ROWS)
            row_count = await rows.count()

            if row_count == 0 and page_idx == 0:
                # Wait and retry — MocDoc may still be loading
                logger.info("No rows yet — waiting 5s for table to load...")
                await self._page.wait_for_timeout(5000)
                await self._dismiss_all_modals()
                row_count = await rows.count()

            if row_count == 0:
                if page_idx == 0:
                    logger.info("No reports found in Pending Print tab")
                break

            logger.info(f"Page {page_idx + 1}: Found {row_count} rows in Pending Print table")

            for i in range(row_count):
                try:
                    row = rows.nth(i)
                    row_text = await row.inner_text()

                    # Debug: log first 3 rows of first page
                    if i < 3 and page_idx == 0:
                        compact = " | ".join(
                            part.strip() for part in row_text.split("\n") if part.strip()
                        )
                        logger.info(f"ROW[{i}] raw: {compact[:300]}")

                    # Skip empty or header rows
                    if not row_text.strip() or S.EMPTY_TABLE_TEXT in row_text:
                        continue

                    # Parse patient cell (first column)
                    cells = row.locator("td")
                    cell_count = await cells.count()

                    if cell_count == 0:
                        continue

                    first_cell_text = await cells.first.inner_text()
                    parsed = _parse_patient_cell(first_cell_text)

                    if not parsed["phone"]:
                        logger.warning(
                            f"No phone number for {parsed['patient_name']} "
                            f"(VAM: {parsed['vam_id']}) — skipping"
                        )
                        continue

                    if not parsed["vam_id"]:
                        logger.warning(
                            f"No VAM ID for {parsed['patient_name']} — skipping"
                        )
                        continue

                    meta = ReportMetadata(
                        patient_name=parsed["patient_name"],
                        patient_phone=parsed["phone"],
                        report_name="",       # filled during download
                        report_type="Laboratory",
                        external_report_id=parsed["vam_id"],  # preliminary
                        vam_id=parsed["vam_id"],
                    )
                    reports.append(meta)
                    logger.info(
                        f"Parsed: {parsed['patient_name']} | "
                        f"{parsed['vam_id']} | ***{parsed['phone'][-4:]}"
                    )

                except Exception as e:
                    logger.warning(f"Failed to parse row {i} on page {page_idx + 1}: {e}")
                    continue

            # Check if there is a next page
            has_next = await self._page.evaluate("""
                () => {
                    const nextBtn = document.querySelector('.paginate_button.next:not(.disabled), a#orders_next:not(.disabled)');
                    if (nextBtn && nextBtn.offsetParent !== null) {
                        nextBtn.click();
                        return true;
                    }
                    return false;
                }
            """)
            if has_next:
                logger.info(f"PAGINATION: Navigating to page {page_idx + 2}...")
                await self._page.wait_for_timeout(3000)
                await self._dismiss_all_modals()
            else:
                break

        logger.info(f"Total parseable reports across pages: {len(reports)}")
        return reports

    async def download_report(self, meta: ReportMetadata) -> Optional[bytes]:
        """Click View on a patient row, then download each test's PDF.

        This method handles:
        1. Finding the correct row by VAM ID
        2. Clicking "View" to expand
        3. Extracting test name and report number
        4. Building the full external_report_id
        5. Clicking "Download Result"
        6. Handling the download modal
        7. Reading the downloaded PDF bytes

        Returns PDF bytes or None on failure.
        """
        vam_id = meta.vam_id
        logger.info(f"Processing download for {vam_id}")

        # Find the row containing this VAM ID, skipping rows we've
        # already processed this run (handles duplicate VAM IDs —
        # e.g., same patient with multiple tests in Pending Print).
        rows = self._page.locator(S.REPORT_ROWS)
        target_row = None
        target_row_index = None
        row_count = await rows.count()

        for i in range(row_count):
            # Skip rows we've already attempted this run
            if i in self._processed_row_indices:
                continue

            row = rows.nth(i)

            # Skip expanded detail rows (tr.showorders) that MocDoc
            # inserts when a row is expanded — they're not patient rows
            try:
                row_class = await row.get_attribute("class") or ""
                if "showorders" in row_class:
                    continue
            except Exception:
                pass

            text = await row.inner_text()
            if vam_id and vam_id in text:
                target_row = row
                target_row_index = i
                break

        if not target_row:
            logger.error(f"ROW_NOT_FOUND: Could not find unprocessed row for {vam_id}")
            return None

        # Mark this row as attempted so the next call (for a duplicate
        # VAM ID) will skip it and find the next matching row
        self._processed_row_indices.add(target_row_index)
        logger.debug(f"Processing row index {target_row_index} for {vam_id}")

        # Click "View" button
        try:
            view_btn = target_row.locator(S.VIEW_BUTTON).first
            await view_btn.click()
            logger.debug(f"Clicked 'View' for {vam_id}")
            await self._page.wait_for_timeout(3000)
        except Exception as e:
            logger.error(f"ROW_EXPAND_FAILED: View button failed for {vam_id}: {e}")
            return None

        # After clicking View, the expanded section appears.
        # We need to find the expanded content. It usually appears as
        # a sibling row or a nested div below the current row.
        # Wait a moment for the expansion animation
        await self._page.wait_for_timeout(2000)

        # Scope every read to the row we just expanded — see PHI cross-delivery
        # defect. Never read inner_text("body") here.
        expanded_row = target_row.locator(
            "xpath=following-sibling::tr[contains(@class,'showorders')][1]"
        )
        try:
            await expanded_row.wait_for(state="attached", timeout=10000)
            row_text = await expanded_row.inner_text()
        except Exception as e:
            logger.error(f"EXPANDED_ROW_NOT_FOUND for {vam_id}: {e}")
            await self._click_hide(target_row)
            return None

        # Extract test details from expanded section
        test_details = _parse_test_details(row_text)
        report_no = test_details.get("report_no")

        if not report_no:
            logger.warning(f"No report number found for {vam_id}")
            # Use vam_id only as fallback
            full_id = vam_id
        else:
            full_id = f"{vam_id}_{report_no}"

        # Update the meta with full external_report_id
        meta.external_report_id = full_id
        meta.report_no = report_no
        meta.sample_id = test_details.get("sample_id")

        # Check if already processed (full ID check)
        if full_id in self._processed_ids:
            logger.info(f"Already processed this run: {full_id}")
            await self._click_hide(target_row)
            return None

        # Find and extract test name from the expanded content
        # Look for text that appears before the report number
        test_name_match = re.search(
            r"([A-Z][A-Z\s\-\d]+(?:\d+P)?)\s+No:\s*\d+",
            row_text,
        )
        if test_name_match:
            meta.report_name = test_name_match.group(1).strip()
        else:
            meta.report_name = "Lab Report"

        # Click "Download Result" icon directly from expanded_row (using JS to bypass hover/visibility restrictions)
        try:
            download_icons = expanded_row.locator("a.downloadresult")
            if await download_icons.count() == 0:
                logger.info(f"Report {full_id} has no download result icon yet (test in progress / pending lab entry)")
                await self._click_hide(target_row)
                return None

            download_icon = download_icons.first
            await download_icon.wait_for(state="attached", timeout=3000)
            await download_icon.evaluate("node => node.click()")
            logger.debug(f"Clicked 'Download Result' for {full_id} via JS")
            await self._page.wait_for_timeout(2000)
        except Exception as e:
            logger.warning(f"Could not open download modal for {full_id} (test may be in progress): {e}")
            await self._click_hide(target_row)
            return None

        # Handle download modal
        pdf_bytes = await self._handle_download_modal(full_id)

        if pdf_bytes:
            self._processed_ids.add(full_id)

        # Collapse the row
        await self._click_hide(target_row)

        # Throttle: wait 1 second before next row
        await self._page.wait_for_timeout(1000)

        return pdf_bytes

    async def _handle_download_modal(self, report_id: str) -> Optional[bytes]:
        """Handle the download modal: verify checkboxes, click Select, wait for download.

        Handles the "Download Failed - Patient Due Pending / Account Balance
        Exceed" error that MocDoc shows when the patient's bill is not paid.
        In that case, the download never starts and we skip gracefully.

        Returns PDF bytes or None.
        """
        # Wait for modal to appear
        try:
            select_btn = self._page.locator(S.DOWNLOAD_SELECT_BUTTON).first
            await select_btn.wait_for(state="visible", timeout=10000)
        except Exception as err:
            logger.error(f"DOWNLOAD_MODAL_MISSING for {report_id}: {err}")

            # Debug: save sanitized/encrypted HTML and conditional screenshot
            try:
                debug_dir = Path(".connector_sessions")
                debug_dir.mkdir(exist_ok=True)
                encryption_key = getattr(settings, "connector_encryption_key", "")

                # Dump main page HTML (sanitized / encrypted)
                raw_html = await self._page.content()
                sanitized_html, _ = sanitize_report_text(raw_html)

                if encryption_key:
                    from cryptography.fernet import Fernet
                    f = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
                    enc_bytes = f.encrypt(sanitized_html.encode())
                    enc_path = debug_dir / f"modal_missing_{report_id}.html.enc"
                    enc_path.write_bytes(enc_bytes)
                    logger.error(f"Saved encrypted debug HTML to {enc_path}")
                else:
                    html_path = debug_dir / f"modal_missing_{report_id}.html"
                    html_path.write_text(sanitized_html, encoding="utf-8")
                    logger.error(f"Saved sanitized page HTML to {html_path}")

                # Save screenshots ONLY in development environment to avoid writing raw PHI
                app_env = getattr(settings, "app_env", "development")
                if app_env == "development":
                    shot_path = debug_dir / f"modal_missing_{report_id}.png"
                    await self._page.screenshot(path=str(shot_path), full_page=True)
                    logger.error(f"Saved debug screenshot to {shot_path}")
                else:
                    logger.info("Skipped full-page PNG screenshot in production to protect PHI.")
            except Exception as e:
                logger.error(f"Could not save debug files: {e}")

            return None

        # Click "Select" to trigger download (or bill payment error)
        await select_btn.click()
        logger.debug(f"Clicked 'Select' for {report_id}")

        # Wait briefly for MocDoc to respond — it either starts the
        # download or shows a "Download Failed" error almost immediately
        await self._page.wait_for_timeout(3000)

        # ── Check for bill payment error FIRST ──
        # MocDoc shows "Download Failed - Patient Due Pending / Account
        # Balance Exceed" in red text inside the modal when bill is unpaid.
        bill_error = await self._check_bill_payment_error()
        if bill_error:
            logger.warning(
                f"BILL_UNPAID for {report_id}: {bill_error} — "
                f"skipping this report until bill payment is completed"
            )
            await self._close_download_modal()
            return None

        # ── No error — wait for the actual file download ──
        # Re-click Select since the first click may have been consumed
        # by MocDoc's error check. Some MocDoc versions need a second click.
        try:
            # Check if Select button is still visible (meaning download
            # didn't start yet and no error appeared)
            if await select_btn.is_visible(timeout=1000):
                # Set up download handler and click again
                async with self._page.expect_download(timeout=60000) as download_info:
                    await select_btn.click()
                    logger.debug(f"Re-clicked 'Select' for {report_id}")
            else:
                # Button disappeared — download might already be in progress
                # Wait for a download event that was triggered by the first click
                async with self._page.expect_download(timeout=60000) as download_info:
                    pass  # download already triggered

            download = await download_info.value
            download_path = os.path.join(
                self.download_dir, download.suggested_filename or f"{report_id}.pdf"
            )
            await download.save_as(download_path)
            logger.info(
                f"Downloaded: {download.suggested_filename} "
                f"({os.path.getsize(download_path)} bytes)"
            )

        except Exception as e:
            # Download failed — check one more time if it's a bill error
            # (it might have appeared after our initial check)
            bill_error = await self._check_bill_payment_error()
            if bill_error:
                logger.warning(
                    f"BILL_UNPAID for {report_id}: {bill_error} — "
                    f"skipping this report until bill payment is completed"
                )
            else:
                logger.error(f"DOWNLOAD_FAILED for {report_id}: {e}")

            # Try waiting for the "Download Completed" text as fallback
            try:
                await self._page.wait_for_selector(
                    f"text={S.DOWNLOAD_COMPLETED_TEXT}", timeout=5000
                )
            except Exception:
                pass

            # Try to close the modal
            await self._close_download_modal()
            return None

        # Wait for "Download Completed" text
        try:
            await self._page.wait_for_selector(
                f"text={S.DOWNLOAD_COMPLETED_TEXT}", timeout=30000
            )
            logger.debug("Download completed confirmation seen")
        except Exception:
            logger.warning(
                f"DOWNLOAD_COMPLETED_TEXT not seen for {report_id}, "
                f"but file was downloaded — continuing"
            )

        # Close the modal
        await self._close_download_modal()

        # Read the PDF bytes
        try:
            if not os.path.exists(download_path):
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
            logger.error(f"PDF_READ_FAILED for {report_id}: {e}")
            return None

        finally:
            # Always delete the temp file
            try:
                if os.path.exists(download_path):
                    os.remove(download_path)
            except Exception:
                pass

    async def _check_bill_payment_error(self) -> Optional[str]:
        """Check if MocDoc is showing a bill payment error in the download modal.

        Returns the error text if found, or None if no error is visible.
        """
        for keyword in S.BILL_PENDING_KEYWORDS:
            try:
                error_locator = self._page.locator(f"text=/{keyword}/i").first
                if await error_locator.is_visible(timeout=500):
                    # Get the full error message for logging
                    try:
                        full_text = await error_locator.inner_text(timeout=1000)
                        return full_text.strip()
                    except Exception:
                        return keyword
            except Exception:
                continue
        return None

    async def _close_download_modal(self) -> None:
        """Close the download modal by clicking Close button.

        Falls back to JavaScript force-close if the button click doesn't
        dismiss the modal (common after bill payment errors where MocDoc's
        JS leaves the modal in a stuck state).
        """
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

        # Force-close via JS if modal is still visible (handles stuck modals
        # after bill payment errors)
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
        try:
            hide_btn = row.locator(S.HIDE_BUTTON).first
            if await hide_btn.is_visible(timeout=2000):
                await hide_btn.click()
                await self._page.wait_for_timeout(500)
        except Exception:
            pass

    async def cleanup(self) -> None:
        """Close browser and delete temp download directory."""
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning(f"Browser cleanup warning: {e}")

        # Purge temp download directory
        try:
            if os.path.exists(self.download_dir):
                shutil.rmtree(self.download_dir, ignore_errors=True)
                logger.debug(f"Cleaned up download dir: {self.download_dir}")
        except Exception:
            pass
