"""No white text on a bright fill (docs/design/A11Y-CONTRAST-PLAN.md A11Y-1, A11Y-4).

1. A CSS rule or inline style that sets white text must not paint it on a colour that
   fails WCAG AA with white: --accent, --accent2, --red, --green, --brand-grad, #ef4444,
   #10B981. Use --action-grad / --accent-fill / --red-fill / --green-fill instead.
2. In the clinic panel (which has a light theme) an inline style may not set white text
   without its own background: it would land on a light card in light theme (A11Y-4).
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILES = ["admin/index.html", "admin/platform.html", "admin/queue-display.html"]
WHITE = re.compile(r"(?<![-\w])color:\s*(#fff\b|#ffffff\b|white\b)", re.I)
BAD_FILL = re.compile(r"var\(--(accent|accent2|red|green|brand-grad)\s*[,)]|#ef4444\b|#10b981\b", re.I)
BACKGROUND = re.compile(r"background(-color)?\s*:\s*([^;\"']+)", re.I)


def _chunks(src: str):
    """CSS rule bodies and inline style attributes, with their line numbers."""
    for m in re.finditer(r"\{([^{}]*)\}", src):
        yield src.count("\n", 0, m.start()) + 1, m.group(1)
    for m in re.finditer(r"style=\"([^\"]*)\"", src):
        yield src.count("\n", 0, m.start()) + 1, m.group(1)


@pytest.mark.parametrize("path", FILES)
def test_no_white_text_on_a_bright_fill(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    bad = []
    for line, body in _chunks(src):
        if not WHITE.search(body):
            continue
        for bg in BACKGROUND.finditer(body):
            if BAD_FILL.search(bg.group(2)):
                bad.append(f"{path}:{line}: {bg.group(0).strip()[:90]}")
    assert not bad, "white text on a fill below 4.5:1:\n" + "\n".join(bad)


def test_clinic_panel_inline_white_text_has_its_own_background():
    src = (ROOT / "admin/index.html").read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(r"style=\"([^\"]*)\"", src):
        style = m.group(1)
        if WHITE.search(style) and not BACKGROUND.search(style):
            bad.append(f"admin/index.html:{src.count(chr(10), 0, m.start()) + 1}: {style[:90]}")
    assert not bad, "inline white text without a background (invisible in light theme):\n" + "\n".join(bad)
