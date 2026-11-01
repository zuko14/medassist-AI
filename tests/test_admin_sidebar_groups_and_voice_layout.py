"""Session 52: admin panel sidebar groups and AI Receptionist layout on narrow screens (admin/index.html).

- Sidebar: OPD OS, IPD OS and Diagnostics fold into one row each. The links inside are unchanged (same
  class, data-page, gates, onclick); only a wrapper is added. A folded group hides its links with
  height + visibility, never display:none, because ketuVisiblePages() reads getClientRects() and Kriya
  Ketu would otherwise answer "not available" for every folded page.
- AI Receptionist: `.table-wrap` had no CSS, so its tables ran past the card's border at every width.
  The group helpers run under node; the browser behaviour was checked with Playwright at 390/412/980/1280 px.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "admin" / "index.html").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _nav() -> str:
    start = HTML.index('<div class="nav-links">')
    return HTML[start:HTML.index('<div class="nav-bottom"', start)]


def _group(name: str) -> str:
    nav = _nav()
    start = nav.index(f'<div class="nav-group" data-nav-group="{name}">')
    end = nav.find('<div class="nav-group" data-nav-group=', start + 10)
    return nav[start:end if end > 0 else len(nav)]


def _pages(block: str) -> list:
    return re.findall(r'<div class="nav-link[^"]*"[^>]*data-page="([a-z]+)"', block)


# ── sidebar groups ───────────────────────────────────────────────────────────

def test_groups_hold_the_expected_pages_in_order():
    assert _pages(_group("opd").split('<div class="nav-group" data-nav-group="ipd"')[0]) == [
        "opdsetup", "casualty", "tariffs", "opddesk", "opdqueue", "opdworkspace", "opdearnings", "opdbilling",
        "opdanalytics"]
    assert _pages(_group("ipd").split('<div class="nav-group" data-nav-group="dx"')[0]) == [
        "ipdboard", "ipdadmit", "ipdchart", "ot", "ipdfinance", "insurance", "ipdsetup"]
    dx = _group("dx")
    assert _pages(dx[:dx.index('data-page="leads"')]) == ["dxworklist", "lisqueue", "radiology"]


def test_no_sidebar_page_was_lost_or_duplicated():
    pages = _pages(_nav())
    assert len(pages) == len(set(pages))
    old = {"dashboard", "insights", "profile", "appointments", "doctors", "leaves", "holidays", "patients", "voice",
           "opdsetup", "casualty", "tariffs", "opddesk", "opdqueue", "opdworkspace", "opdearnings", "opdbilling",
           "opdanalytics", "ipdboard", "ipdadmit", "ipdchart", "ot", "ipdfinance", "insurance", "ipdsetup",
           "dxworklist", "lisqueue", "radiology", "leads", "diagreports", "labreports", "labtests", "homecollect",
           "corporate", "treatments", "dentalplans", "prescriptions", "payments", "paysettings", "branches", "staff"}
    assert old <= set(pages)


def test_each_group_head_is_an_accessible_button():
    for name, gid in (("opd", "Opd"), ("ipd", "Ipd"), ("dx", "Dx")):
        g = _group(name)
        assert f'<button type="button" class="nav-group-head" id="navGrp{gid}Head" aria-expanded="false" ' \
               f'aria-controls="navGrp{gid}" onclick="navGroupToggle(\'{name}\')">' in g
        assert f'id="navGrp{gid}" role="group" aria-labelledby="navGrp{gid}Head"' in g


def test_old_section_labels_are_gone_from_the_sidebar():
    assert "nav-section-label" not in _nav()


def test_folding_never_uses_display_none():
    css = re.search(r"/\* ─── Sidebar groups \(session 52\).*?@media \(prefers-reduced-motion: reduce\)", HTML, re.S)
    assert css, "sidebar group CSS block missing"
    folded = re.search(r"\.nav-group-inner \{([^}]*)\}", css.group(0)).group(1)
    assert "visibility: hidden" in folded and "overflow: hidden" in folded and "display" not in folded
    assert ".nav-group-body { display: grid; grid-template-rows: 0fr;" in css.group(0)


def test_go_reveals_the_pages_group():
    go = re.search(r"function go\(page, el\) \{.*?\n\}", HTML, re.S).group(0)
    assert "navGroupReveal(page);" in go


def test_ketu_visible_pages_still_reads_client_rects():
    fn = re.search(r"function ketuVisiblePages\(\) \{.*?\n\}", HTML, re.S).group(0)
    assert "getClientRects().length > 0" in fn


def _helpers() -> str:
    m = re.search(r"const NAV_GROUP_KEY = .*?\n\(function navGroupsWatch", HTML, re.S)
    assert m
    return m.group(0)[:-len("(function navGroupsWatch")]


@needs_node
def test_group_sync_flat_hidden_and_remembered_state():
    """One visible link = flat (no dropdown of one), none = hidden; first sync opens on the active page or the
    remembered choice; a toggle is remembered; reveal opens the page's group without remembering it."""
    script = r"""
    const store = {};
    global.localStorage = { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); } };
    function el(attrs) { const cls = new Set(attrs.cls || []); return Object.assign({ dataset: attrs.dataset || {},
        style: { display: attrs.display || '' }, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
        classList: { toggle: (c, on) => { (on === undefined ? !cls.has(c) : on) ? cls.add(c) : cls.delete(c); }, contains: c => cls.has(c) },
        cls }, attrs.extra || {}); }
    function group(name, links) {
        const head = el({}), count = el({ extra: { textContent: '' } });
        const g = el({ dataset: { navGroup: name } });
        g.hidden = false;
        g.querySelectorAll = sel => links;
        g.querySelector = sel => sel === '.nav-group-head' ? head : sel === '.nav-group-count' ? count
            : sel === '.nav-link.on' ? links.find(l => l.cls.has('on')) || null
            : links.find(l => sel.includes('"' + l.dataset.page + '"')) || null;
        g.head = head; g.count = count;
        return g;
    }
    const A = group('opd', [el({ dataset: { page: 'opddesk' } }), el({ dataset: { page: 'opdbilling' }, cls: ['on'] })]);
    const B = group('ipd', [el({ dataset: { page: 'ipdboard' }, display: 'none' })]);
    const C = group('dx', [el({ dataset: { page: 'dxworklist' } }), el({ dataset: { page: 'radiology' }, display: 'none' })]);
    const D = group('xx', [el({ dataset: { page: 'p1' } }), el({ dataset: { page: 'p2' } })]);
    const groups = [A, B, C, D];
    global.document = { querySelectorAll: () => groups, querySelector: s => groups.find(g => s.includes("'" + g.dataset.navGroup + "'") || s.includes('"' + g.dataset.navGroup + '"')) };
    global.getComputedStyle = l => ({ display: l.style.display || 'flex' });
    store['kriya.nav.groups'] = JSON.stringify({ xx: true });
    """ + _helpers() + r"""
    navGroupsSync();
    const s1 = groups.map(g => [g.dataset.navGroup, g.hidden, g.cls.has('is-flat'), g.cls.has('open'), g.count.textContent, g.head.attrs['aria-expanded'] || null]);
    navGroupToggle('opd');
    const afterToggle = [A.cls.has('open'), JSON.parse(store['kriya.nav.groups']).opd];
    navGroupReveal('p2');
    const afterReveal = [D.cls.has('open'), D.cls.has('has-active'), A.cls.has('has-active'), JSON.parse(store['kriya.nav.groups']).xx];
    console.log(JSON.stringify({ s1, afterToggle, afterReveal }));
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["s1"] == [["opd", False, False, True, 2, "true"],      # the open page is inside: opens
                         ["ipd", True, False, False, "", None],       # nothing visible: hidden, untouched
                         ["dx", False, True, False, 1, "false"],      # one link: flat
                         ["xx", False, False, True, 2, "true"]]       # remembered open
    assert got["afterToggle"] == [False, False]
    assert got["afterReveal"] == [True, True, False, True]


# ── AI Receptionist layout ───────────────────────────────────────────────────

def test_table_wrap_scrolls_inside_its_card():
    rule = re.search(r"\.tbl-wrap, \.table-wrap \{([^}]*)\}", HTML)
    assert rule and "overflow-x: auto" in rule.group(1) and "max-width: 100%" in rule.group(1)


def test_voice_tables_are_all_wrapped():
    body = HTML[HTML.index("async function vcAnalytics()"):HTML.index("async function vcOutbound()")]
    assert body.count("<table>") == body.count('<div class="tbl-wrap"><table>') == 3
    assert 'class="vc-grid"' in body and 'class="lead-grid"' not in body


def test_voice_call_and_queue_rows_stack_on_phones():
    rows = HTML[HTML.index("function vcCallRows("):HTML.index("async function vcLive()")]
    assert '<table class="vc-stack">' in rows
    for label in ("Caller", "Language", "Request", "Outcome", "Status", "Started"):
        assert f'data-label="{label}"' in rows
    out = HTML[HTML.index("async function vcOutbound()"):HTML.index("function vcBulkPanel()")]
    assert '<table class="vc-stack">' in out
    for label in ("Asked about", "Status", "Attempts", "Next try", "Note"):
        assert f'data-label="{label}"' in out
    assert re.search(r"@media \(max-width: 640px\) \{\s*\.vc-inline-file .*?\.vc-stack thead \{ display: none; \}", HTML, re.S)


def test_bulk_file_row_has_no_inline_columns():
    """The inline grid-template-columns beat the phone rule and pushed the file input past the card."""
    assert 'class="vc-inline vc-inline-file"' in HTML
    assert 'style="grid-template-columns:auto minmax(0,1fr)"' not in HTML


def test_widget_labels_a_corrected_run():
    assert "run.error_code === 'corrected' ? 'Replaced below.'" in HTML
    refresh = HTML[HTML.index("async refreshOpenForms(exceptId)"):]
    assert "r.state === 'NEEDS_CONFIRMATION'" in refresh[:400]
