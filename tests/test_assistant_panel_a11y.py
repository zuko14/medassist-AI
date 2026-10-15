"""KK-P2-04: Kriya Ketu accessibility, mobile sheet, reduced motion/transparency, light theme.

KK-NFR-A11Y-01/02, KK-NFR-MOB-01, ux-design-spec §2, §4.1, §6, §7. Static checks on the CSS
contract plus the widget's pure helpers run under node. The browser evidence (axe, measured
contrast, emulated media, 375x812) is recorded in docs/kriya-ketu/status.md.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "admin" / "index.html").read_text(encoding="utf-8")
CSS = HTML[HTML.index("/* ── Kriya Ketu"):HTML.index("/* ── end Kriya Ketu")]
JS = HTML[HTML.index("// ── Kriya Ketu"):HTML.index("// ── end Kriya Ketu")]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _fn(name):
    m = re.search(rf"function {name}\(.*?\n\}}", HTML, re.S)
    assert m, f"{name}() not found"
    return m.group(0)


def _node(src):
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _mobile_block():
    m = re.search(r"@media \(max-width: 640px\) \{(.*?)\n        \}", CSS, re.S)
    assert m, "no ≤640px block"
    return m.group(1)


# ── contrast fixes measured in the browser (all < 4.5:1 before) ───────────────

def test_links_use_the_stronger_accent():
    assert re.search(r"\.ketu-link \{[^}]*color: var\(--accent2\)", CSS)


def test_chip_and_warnings_carry_meaning_without_low_contrast_text():
    assert re.search(r"\.ketu-chip \{[^}]*color: var\(--text\)", CSS)
    assert re.search(r"\.ketu-chip \{[^}]*border: 1px solid var\(--green-border\)", CSS)
    assert re.search(r"\.ketu-warn \{[^}]*color: var\(--text\)[^}]*background: var\(--amber-bg\)", CSS)


def test_light_theme_star_is_not_white_on_white():
    assert re.search(r':root\[data-theme="light"\] #ketuStar stop[^{]*\{[^}]*stop-color', CSS)


# ── mobile (KK-NFR-MOB-01, ux §7: hit targets >= 44x44 on phones) ─────────────

def test_phone_hit_targets_are_44px():
    block = _mobile_block()
    for sel in (".ketu-actions .btn", ".ketu-link", ".ketu-card label.ketu-opt"):
        assert re.search(re.escape(sel) + r"[^{]*\{[^}]*min-height: 44px", block), sel


@needs_node
@pytest.mark.parametrize("vv, mobile, expected", [
    ({"height": 500, "offsetTop": 0}, True, {"height": 500, "top": 0}),       # keyboard open
    ({"height": 812, "offsetTop": 0}, True, {"height": 812, "top": 0}),
    ({"height": 430, "offsetTop": 120}, True, {"height": 430, "top": 120}),   # iOS scrolls the layout
    ({"height": 500, "offsetTop": 0}, False, None),                           # desktop: CSS geometry
    (None, True, None),                                                       # no visualViewport
])
def test_sheet_follows_the_visual_viewport(vv, mobile, expected):
    got = _node(_fn("ketuSheetBox") + f"\nconsole.log(JSON.stringify(ketuSheetBox({json.dumps(vv)}, {json.dumps(mobile)})));")
    assert got == expected


@needs_node
@pytest.mark.parametrize("scroll_height, scroll_top, client_height, pill", [
    (1000, 100, 520, True), (1000, 400, 520, True),            # 80 px or more above the bottom
    (1000, 401, 520, False), (1000, 440, 520, False),          # within 80 px: just auto-scroll (ux §6)
    (1000, 900, 100, False), (500, 0, 520, False),
])
def test_new_result_pill_only_when_scrolled_up(scroll_height, scroll_top, client_height, pill):
    got = _node(_fn("ketuNeedsPill") + f"\nconsole.log(JSON.stringify(ketuNeedsPill({scroll_height}, {scroll_top}, {client_height})));")
    assert got is pill


# ── stacking / modality (ux §4.1) and live region (ux §6, §7) ─────────────────

def test_panel_goes_inert_while_the_page_confirm_dialog_is_open():
    assert "confirmOverlay" in JS and "MutationObserver" in JS and ".inert =" in JS


def test_new_result_pill_is_a_labelled_button():
    assert 'id="ketuNewPill"' in JS and "New result" in JS


def test_reduced_motion_and_transparency_blocks_cover_the_widget():
    rm = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n        \}", CSS, re.S).group(1)
    assert "#ketuPanel" in rm and "transition: none" in rm and "transform: none" in rm
    rt = re.search(r"@media \(prefers-reduced-transparency: reduce\) \{(.*?)\n        \}", CSS, re.S).group(1)
    assert "--ketu-surface: var(--ketu-surface-solid)" in rt
