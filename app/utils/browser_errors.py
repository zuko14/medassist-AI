"""Translates opaque Playwright browser-launch failures into an actionable
message, and provides auto-install fallback so the connector self-heals
when the Chromium binary is missing at runtime (e.g. Render native buildpack)."""

import logging
import subprocess

logger = logging.getLogger(__name__)

# Track whether we've already attempted an install this process
_install_attempted = False


def _try_install_chromium() -> bool:
    """Attempt to install Playwright Chromium at runtime.

    Returns True if installation succeeded, False otherwise.
    Only attempts once per process lifetime to avoid infinite retry loops.
    """
    global _install_attempted
    if _install_attempted:
        return False
    _install_attempted = True

    logger.warning(
        "Chromium browser binary not found — attempting runtime install "
        "via 'playwright install chromium'. This may take 1-2 minutes."
    )
    # Try without --with-deps first (works on Render free tier where
    # root/sudo is unavailable but OS deps are already present).
    # Fall back to --with-deps if the first attempt fails (e.g. Docker).
    for cmd in [
        ["playwright", "install", "chromium"],
        ["playwright", "install", "--with-deps", "chromium"],
    ]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
            )
            if result.returncode == 0:
                logger.info("Playwright Chromium installed successfully at runtime via: %s", " ".join(cmd))
                return True
            else:
                logger.warning(
                    "Command '%s' failed (exit %d): %s",
                    " ".join(cmd),
                    result.returncode,
                    result.stderr[:300],
                )
        except FileNotFoundError:
            logger.error("'playwright' CLI not found on PATH — cannot auto-install.")
            return False
        except subprocess.TimeoutExpired:
            logger.error("Playwright Chromium install timed out after 300s.")
            return False
        except Exception as e:
            logger.error("Unexpected error during Playwright install: %s", e)
            return False
    return False


def friendly_browser_launch_error(exc: Exception) -> str:
    text = str(exc)
    if "Executable doesn't exist" in text or "playwright install" in text.lower():
        return (
            "Chromium browser is not installed on this server — the deploy did not "
            "run 'playwright install --with-deps chromium' at build time. Check that "
            "the Render service builds from the Dockerfile (see render.yaml, env: docker), "
            "then redeploy."
        )
    return text


def is_missing_browser_error(exc: Exception) -> bool:
    """Check if an exception is a missing Chromium binary error."""
    text = str(exc)
    return "Executable doesn't exist" in text or "playwright install" in text.lower()
