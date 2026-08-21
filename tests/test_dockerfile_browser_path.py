# tests/test_dockerfile_browser_path.py
"""Regression guard for the Playwright Chromium cache-path bug: the browser
must be installed to a path that's the same for the root build-time user
and the appuser runtime user, or every cold boot re-downloads Chromium."""

from pathlib import Path

DOCKERFILE = Path(__file__).parent.parent / "Dockerfile"


def test_playwright_browsers_path_set_before_install_and_absolute():
    lines = DOCKERFILE.read_text().splitlines()

    env_line_idx = next(
        (i for i, l in enumerate(lines) if "PLAYWRIGHT_BROWSERS_PATH" in l and l.strip().startswith("ENV")),
        None,
    )
    install_line_idx = next(
        (i for i, l in enumerate(lines) if "playwright install" in l),
        None,
    )
    user_line_idx = next(
        (i for i, l in enumerate(lines) if l.strip().startswith("USER ")),
        None,
    )

    assert env_line_idx is not None, "Dockerfile must set ENV PLAYWRIGHT_BROWSERS_PATH"
    assert install_line_idx is not None, "Dockerfile must run playwright install"
    assert user_line_idx is not None, "Dockerfile must switch to a non-root USER"

    # Must be set before both the install (build-time/root) and the USER
    # switch (so appuser's runtime env matches what root installed to).
    assert env_line_idx < install_line_idx
    assert env_line_idx < user_line_idx

    path_value = lines[env_line_idx].split("PLAYWRIGHT_BROWSERS_PATH", 1)[1].strip().lstrip("=").strip()
    assert path_value.startswith("/"), "Path must be absolute, not $HOME-relative"
    assert "$HOME" not in path_value and "~" not in path_value
