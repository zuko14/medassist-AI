"""Tests for translating opaque Playwright browser-launch failures into
actionable operator-facing messages, and for the auto-install fallback."""
import pytest
from unittest.mock import patch, MagicMock


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


class TestIsMissingBrowserError:
    def test_detects_executable_doesnt_exist(self):
        from app.utils.browser_errors import is_missing_browser_error

        exc = Exception("Executable doesn't exist at /some/path")
        assert is_missing_browser_error(exc) is True

    def test_detects_playwright_install_hint(self):
        from app.utils.browser_errors import is_missing_browser_error

        exc = Exception("Run 'playwright install' to download the browser")
        assert is_missing_browser_error(exc) is True

    def test_returns_false_for_unrelated_error(self):
        from app.utils.browser_errors import is_missing_browser_error

        exc = Exception("net::ERR_CONNECTION_REFUSED")
        assert is_missing_browser_error(exc) is False


class TestTryInstallChromium:
    def test_runs_playwright_install_without_deps_first(self):
        import app.utils.browser_errors as mod

        # Reset the install-attempted flag
        mod._install_attempted = False

        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = mod._try_install_chromium()

        assert result is True
        # Should try without --with-deps first
        mock_run.assert_called_once_with(
            ["playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        # Reset after test
        mod._install_attempted = False

    def test_returns_false_on_nonzero_exit(self):
        import app.utils.browser_errors as mod

        mod._install_attempted = False

        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="Permission denied")
            result = mod._try_install_chromium()

        assert result is False
        mod._install_attempted = False

    def test_only_attempts_once_per_process(self):
        import app.utils.browser_errors as mod

        mod._install_attempted = False

        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="fail")
            mod._try_install_chromium()  # first attempt runs candidate commands
            first_call_count = mock_run.call_count
            assert first_call_count > 0

            result = mod._try_install_chromium()  # second attempt must be short-circuited

        assert result is False
        assert mock_run.call_count == first_call_count  # no additional subprocess calls
        mod._install_attempted = False

    def test_returns_false_when_playwright_not_on_path(self):
        import app.utils.browser_errors as mod

        mod._install_attempted = False

        with patch.object(mod.subprocess, "run", side_effect=FileNotFoundError):
            result = mod._try_install_chromium()

        assert result is False
        mod._install_attempted = False
