"""KK-P2-01: Kriya Ketu widget shell in admin/index.html (ux-design-spec §1-4, ADR-005).

Static checks on the inlined panel plus the widget's pure helpers executed under
node (pattern: tests/test_admin_panel_scope_helper.py): the mark, the launcher
gate, the Ctrl/Cmd+K rule, the scope-banner offset, the bootstrap hooks and a
syntax check of the whole inline script.
"""

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

PANEL = Path(__file__).resolve().parents[1] / "admin" / "index.html"
HTML = PANEL.read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _fn(name: str) -> str:
    m = re.search(rf"function {name}\(.*?\n\}}", HTML, re.S)
    assert m, f"{name}() not found in admin/index.html"
    return m.group(0)


def _node(script: str):
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── the mark (ux §1) ──────────────────────────────────────────────────────────

def test_ketu_mark_is_in_the_sprite_with_prefixed_gradients():
    m = re.search(r'<symbol id="ketu-mark" viewBox="0 0 64 64">(.*?)</symbol>', HTML, re.S)
    assert m, "#ketu-mark symbol missing"
    ids = re.findall(r'id="([^"]+)"', m.group(1))
    assert ids and all(i.startswith("ketu") for i in ids), ids
    for i in ids:  # no collision anywhere else in the panel
        assert HTML.count(f'id="{i}"') == 1, i
    assert "hex" not in m.group(1).lower()  # a sub-brand: no Kriya hex outline


# ── pure helpers under node ───────────────────────────────────────────────────

@needs_node
def test_launcher_gate():
    cases = [(True, None, True), (True, "FRONT_DESK", True), (False, None, False),
             (True, "CORPORATE_VIEWER", False), (True, "PHLEBOTOMIST", False), (None, None, False)]
    got = _node(_fn("ketuAllowed") + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(c => ketuAllowed(c[0], c[1]))));")
    assert got == [c[2] for c in cases]


@needs_node
def test_ctrl_k_rule():
    def ev(key="k", ctrl=True, meta=False, alt=False, shift=False, tag="BODY", editable=False, composing=False):
        return {"key": key, "ctrlKey": ctrl, "metaKey": meta, "altKey": alt, "shiftKey": shift,
                "isComposing": composing, "target": {"tagName": tag, "isContentEditable": editable}}
    cases = [
        (ev(), False, True), (ev(key="K"), False, True), (ev(ctrl=False, meta=True), False, True),
        (ev(ctrl=False), False, False), (ev(key="j"), False, False), (ev(alt=True), False, False),
        (ev(shift=True), False, False), (ev(composing=True), False, False),
        (ev(tag="INPUT"), False, False), (ev(tag="TEXTAREA"), False, False), (ev(tag="SELECT"), False, False),
        (ev(editable=True), False, False),
        (ev(tag="TEXTAREA"), True, True),  # panel open: Ctrl+K from its own composer closes it
    ]
    script = _fn("ketuShortcut") + textwrap.dedent(f"""
        const cases = {json.dumps(cases)};
        console.log(JSON.stringify(cases.map(c => ketuShortcut(c[0], c[1]))));
    """)
    assert _node(script) == [c[2] for c in cases]


@needs_node
def test_launcher_moves_above_the_scope_banner():
    cases = [(0, False, 20), (0, True, 16), (30, False, 50), (44, True, 64)]
    got = _node(_fn("ketuLauncherBottom") +
                f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(c => ketuLauncherBottom(c[0], c[1]))));")
    assert got == [c[2] for c in cases]


# ── wiring (ADR-005: a widget failure can never block the panel) ─────────────

def test_bootstrap_hooks_are_wrapped():
    login = _fn("login")
    adopt = HTML[HTML.index("async function adoptClinicPlan()"):HTML.index("async function switchClinic()")]
    banner = _fn("renderClinicScopeBanner")
    logout = _fn("logout")
    assert "myKetu = !!me.assistant_enabled" in login
    assert "myKetu = !!scoped.assistant_enabled" in adopt
    # init runs before the corporate/phlebotomist early returns, so it can hide the launcher there
    assert login.index("ketuSafe('init')") < login.index("if (isCorpViewer())")
    assert "ketuSafe('init')" in adopt
    assert "ketuSafe('place')" in banner
    assert "ketuSafe('teardown')" in logout and "myKetu = false" in logout
    safe = _fn("ketuSafe")
    assert "try" in safe and "catch" in safe


def test_tokens_are_scoped_not_global():
    # A :root block that is not narrowed to the widget would make the tokens global.
    root_blocks = [b for b in re.findall(r":root[^{]*\{[^}]*\}", HTML) if "#ketu" not in b.split("{")[0]]
    assert not any("--ketu-" in b for b in root_blocks)
    assert re.search(r"#ketuPanel,\s*#ketuLauncher\s*\{[^}]*--ketu-surface:", HTML)


def test_stacking_and_accessibility_contract():
    css = HTML[HTML.index("/* ── Kriya Ketu"):HTML.index("/* ── end Kriya Ketu")]
    assert re.search(r"#ketuLauncher\s*\{[^}]*z-index:\s*540", css)
    assert re.search(r"#ketuPanel\s*\{[^}]*z-index:\s*560", css)
    assert "prefers-reduced-motion: reduce" in css
    assert "@supports not" in css and "backdrop-filter" in css
    js = HTML[HTML.index("// ── Kriya Ketu"):HTML.index("// ── end Kriya Ketu")]
    for needle in ('role="dialog"', 'aria-modal="false"', 'aria-labelledby="ketuTitle"', 'role="log"',
                   'aria-live="polite"', "aria-expanded", "aria-controls", "Open Kriya Ketu assistant"):
        assert needle in js, needle


def test_widget_size_budget_gzipped():
    """KK-NFR-PERF-02 (owner decision 2026-10-14): the widget adds <= 20 KB gzipped; raised to 24 KB on
    2026-10-29 for voice mode (orb overlay, WAV encoder, end-of-speech detection), then 28 KB the same day for
    clinical dictation and noise handling (adaptive speech detection, high-pass, silence trim)."""
    import gzip
    css = HTML[HTML.index("/* ── Kriya Ketu"):HTML.index("/* ── end Kriya Ketu")]
    js = HTML[HTML.index("// ── Kriya Ketu"):HTML.index("// ── end Kriya Ketu")]
    mark = re.search(r'<symbol id="ketu-mark".*?</symbol>', HTML, re.S).group(0)
    size = len(gzip.compress((css + js + mark).encode("utf-8"), compresslevel=9))
    assert size <= 28 * 1024, f"widget is {size} bytes gzipped"


@needs_node
def test_inline_script_still_parses():
    blocks = re.findall(r"<script>(.*?)</script>", HTML, re.S)
    main = max(blocks, key=len)
    out = subprocess.run(["node", "--check", "-"], input=main, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr[:2000]
