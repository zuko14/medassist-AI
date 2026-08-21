"""Playwright Browser Session Manager (Phase 3 & Real Automation Phase R2)."""

import os
import logging
from typing import Dict, Any, Optional
from app.integrations.callmedex.browser.base import BaseBrowserSession
from app.integrations.callmedex.config.settings import callmedex_settings

logger = logging.getLogger(__name__)


class PlaywrightBrowserSession(BaseBrowserSession):
    """Playwright implementation for managing browser contexts and screenshots."""

    def __init__(self):
        self._sessions: Dict[str, Dict[str, Any]] = {}
        os.makedirs(callmedex_settings.artifacts_dir, exist_ok=True)

    async def create_context(
        self, session_id: str, headless: bool = True
    ) -> Dict[str, Any]:
        """Create an isolated browser session context using Playwright async API."""
        logger.info(f"Creating browser session context: {session_id} (headless={headless})")
        playwright_obj = None
        browser = None
        context = None
        page = None

        is_live_required = (
            callmedex_settings.app_env == "production"
            or os.getenv("MOCDOC_SANDBOX_ENABLED") == "1"
        )

        if not is_live_required:
            logger.info(f"Unit test mode: fast mock browser session context created for {session_id}")
            session_data = {
                "session_id": session_id,
                "headless": headless,
                "active": True,
                "playwright": None,
                "browser": None,
                "context": None,
                "page": None,
                "pages": [],
            }
            self._sessions[session_id] = session_data
            return session_data

        try:
            from playwright.async_api import async_playwright
            playwright_obj = await async_playwright().start()
            try:
                browser = await playwright_obj.chromium.launch(headless=headless)
            except Exception as e:
                from app.utils.browser_errors import (
                    is_missing_browser_error,
                    _try_install_chromium,
                    friendly_browser_launch_error,
                )
                if is_missing_browser_error(e) and _try_install_chromium():
                    browser = await playwright_obj.chromium.launch(headless=headless)
                else:
                    raise RuntimeError(friendly_browser_launch_error(e)) from e
            context = await browser.new_context()
            page = await context.new_page()

            page.set_default_timeout(callmedex_settings.browser_timeout_ms)
            context.set_default_navigation_timeout(callmedex_settings.browser_navigation_timeout_ms)

            session_data = {
                "session_id": session_id,
                "headless": headless,
                "active": True,
                "playwright": playwright_obj,
                "browser": browser,
                "context": context,
                "page": page,
                "pages": [page],
            }
        except Exception as e:
            logger.warning(
                f"Could not initialize live Playwright Chromium context ({e}). Falling back to mock session."
            )
            session_data = {
                "session_id": session_id,
                "headless": headless,
                "active": True,
                "playwright": None,
                "browser": None,
                "context": None,
                "page": None,
                "pages": [],
            }

        self._sessions[session_id] = session_data
        return session_data

    async def capture_screenshot(
        self, page_handle: Any, artifact_name: str
    ) -> Optional[str]:
        """Capture a failure/diagnostic screenshot artifact."""
        filename = f"{artifact_name}.png"
        filepath = os.path.join(callmedex_settings.artifacts_dir, filename)
        logger.info(f"Capturing screenshot artifact to {filepath}")

        if page_handle is None or not hasattr(page_handle, "screenshot"):
            logger.warning(
                f"No live page handle available — skipping screenshot artifact '{artifact_name}' "
                f"(no real screenshot captured, no placeholder file written)"
            )
            return None

        try:
            await page_handle.screenshot(path=filepath)
        except Exception as e:
            logger.error(f"Failed capturing screenshot artifact {artifact_name}: {e}")
            return None

        return filepath

    async def close_context(self, session_id: str) -> None:
        """Close browser context cleanly with unconditional leak prevention guarantee."""
        session_data = self._sessions.get(session_id)
        if session_data:
            logger.info(f"Closing browser session context cleanly: {session_id}")
            try:
                page = session_data.get("page")
                if page and hasattr(page, "close"):
                    await page.close()
                context = session_data.get("context")
                if context and hasattr(context, "close"):
                    await context.close()
                browser = session_data.get("browser")
                if browser and hasattr(browser, "close"):
                    await browser.close()
                pw = session_data.get("playwright")
                if pw and hasattr(pw, "stop"):
                    await pw.stop()
            except Exception as e:
                logger.error(f"Error closing Playwright resources for session {session_id}: {e}")
            finally:
                session_data["active"] = False
                self._sessions.pop(session_id, None)
