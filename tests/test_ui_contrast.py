"""WCAG AA contrast of the panel colour tokens (docs/design/A11Y-CONTRAST-PLAN.md).

Reads the theme blocks straight from the HTML files, composites the translucent
surfaces over the page background (as the browser renders them) and checks every
text/fill pair the panels use. Pure file parsing: hermetic, no browser.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = (ROOT / "admin/index.html").read_text(encoding="utf-8")
PLATFORM = (ROOT / "admin/platform.html").read_text(encoding="utf-8")
AA = 4.5


def _block(src: str, opener: str) -> dict:
    start = src.index(opener)
    body = src[start + len(opener): src.index("\n        }", start)]
    return {k: v.strip() for k, v in re.findall(r"--([a-z0-9-]+):\s*([^;]+);", body)}


DARK = _block(INDEX, ":root {")
LIGHT = {**DARK, **_block(INDEX, ':root[data-theme="light"] {')}
CONSOLE = _block(PLATFORM, ":root {")


def _rgba(value: str) -> tuple:
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (1.0,)
    m = re.match(r"rgba?\(([^)]+)\)", value)
    assert m, value
    parts = [p.strip() for p in m.group(1).split(",")]
    return tuple(int(float(p)) for p in parts[:3]) + (float(parts[3]) if len(parts) > 3 else 1.0,)


def _over(fg: tuple, bg: tuple) -> tuple:
    a = fg[3]
    return tuple(round(a * f + (1 - a) * b) for f, b in zip(fg[:3], bg[:3]))


def _lum(c: tuple) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a: tuple, b: tuple) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _surfaces(t: dict) -> dict:
    bg = _rgba(t["bg"])[:3]
    out = {"bg": bg}
    for name in ("surface", "surface2", "surface3"):
        if name in t:
            out[name] = _over(_rgba(t[name]), bg)
    return out


WHITE = (255, 255, 255)
THEMES = {"dark": DARK, "light": LIGHT, "console": CONSOLE}


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("text", ["text", "text2", "text3"])
def test_text_tokens_pass_on_every_surface(theme, text):
    t = THEMES[theme]
    for name, surface in _surfaces(t).items():
        r = ratio(_rgba(t[text])[:3], surface)
        assert r >= AA, f"{theme} --{text} on --{name}: {r:.2f}"


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("status", ["green", "red", "amber", "blue"])
def test_status_text_on_its_own_tint(theme, status):
    t = THEMES[theme]
    base = _rgba(t["surface-solid"])[:3]
    tint = _over(_rgba(t[f"{status}-bg"]), base)
    r = ratio(_rgba(t[status])[:3], tint)
    assert r >= AA, f"{theme} --{status} on --{status}-bg: {r:.2f}"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("fill", ["accent-fill", "red-fill", "green-fill"])
def test_white_text_on_fill_tokens(theme, fill):
    t = THEMES[theme]
    if theme == "console" and fill == "green-fill":
        pytest.skip("the owner console has no green fill with text")
    r = ratio(WHITE, _rgba(t[fill])[:3])
    assert r >= AA, f"{theme} white on --{fill}: {r:.2f}"


@pytest.mark.parametrize("theme", THEMES)
def test_white_text_on_every_action_gradient_stop(theme):
    stops = re.findall(r"#[0-9A-Fa-f]{6}", THEMES[theme]["action-grad"])
    assert len(stops) >= 2
    for stop in stops:
        r = ratio(WHITE, _rgba(stop)[:3])
        assert r >= AA, f"{theme} white on --action-grad stop {stop}: {r:.2f}"


OPD_STAGES = ["opd-registered", "opd-vitals", "opd-waiting", "opd-consult", "opd-billing", "opd-done", "opd-cancel"]


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("stage", OPD_STAGES)
def test_opd_stage_pill_text_on_its_tint(theme, stage):
    """.opd-pill: text in the stage colour on an 18 % tint of it (color-mix)."""
    t = THEMES[theme]
    colour = _rgba(t[stage])
    tint = _over(colour[:3] + (0.18,), _rgba(t["surface-solid"])[:3])
    r = ratio(colour[:3], tint)
    assert r >= AA, f"{theme} --{stage} pill: {r:.2f}"


def test_brand_gradient_is_unchanged():
    """The logo gradient stays; only text-bearing fills moved to --action-grad."""
    assert DARK["brand-grad"] == "linear-gradient(135deg, #10B981 0%, #2FA8D8 55%, #3D8BFD 100%)"
    assert LIGHT["brand-grad"] == "linear-gradient(135deg, #059669 0%, #1387C4 55%, #1668D6 100%)"
