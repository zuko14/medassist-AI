"""Tests for translating opaque Playwright browser-launch failures into
actionable operator-facing messages."""
import pytest


class TestFriendlyBrowserLaunchError:
    def test_translates_missing_executable_error(self):
        from app.utils.browser_errors import friendly_browser_launch_error

        exc = Exception(
            "Executable doesn't exist at /opt/render/.cache/ms-playwright/"
            "chromium_headless_shell-1148/chrome-linux/headless_shell\n"
            "Looks like Playwright was just installed or updated."
        )
        msg = friendly_browser_launch_error(exc)
        assert "playwright install" in msg.lower()
        assert "render.yaml" in msg.lower() or "dockerfile" in msg.lower()

    def test_passes_through_unrelated_errors_unchanged(self):
        from app.utils.browser_errors import friendly_browser_launch_error

        exc = Exception("net::ERR_CONNECTION_REFUSED")
        assert friendly_browser_launch_error(exc) == "net::ERR_CONNECTION_REFUSED"
