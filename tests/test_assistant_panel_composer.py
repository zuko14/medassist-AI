"""KK-P2-02: Kriya Ketu composer, send, draft and R0 cards (KK-FR-004, 005, 034; ux-design-spec §4.3, §6).

The widget's pure helpers are extracted from admin/index.html and executed under
node (pattern: tests/test_admin_panel_scope_helper.py).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "admin" / "index.html").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _fn(name: str) -> str:
    m = re.search(rf"function {name}\(.*?\n\}}", HTML, re.S)
    assert m, f"{name}() not found in admin/index.html"
    return m.group(0)


def _run(fns, expr):
    src = "\n".join(_fn(f) for f in fns) + f"\nconsole.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── KK-FR-004: keys and the send button ───────────────────────────────────────

def _ev(key="Enter", shift=False, composing=False, ctrl=False):
    return {"key": key, "shiftKey": shift, "isComposing": composing, "ctrlKey": ctrl, "keyCode": 13}


@pytest.mark.parametrize("ev, in_flight, expected", [
    (_ev(), False, "send"),
    (_ev(shift=True), False, None),            # newline
    (_ev(composing=True), False, None),        # IME: Enter commits the composition
    ({**_ev(), "keyCode": 229}, False, None),  # Safari's IME signal
    (_ev(), True, "block"),                    # in flight: swallow, never a second request
    (_ev(key="a"), False, None),
])
def test_composer_keys(ev, in_flight, expected):
    assert _run(["ketuKeyAction"], f"ketuKeyAction({json.dumps(ev)}, {json.dumps(in_flight)})") == expected


@pytest.mark.parametrize("text, in_flight, ok", [
    ("open leads", False, True), ("   ", False, False), ("", False, False),
    ("x" * 1000, False, True), ("x" * 1001, False, False), ("open leads", True, False),
])
def test_can_send(text, in_flight, ok):
    assert _run(["ketuCanSend"], f"ketuCanSend({json.dumps(text)}, {json.dumps(in_flight)})") is ok


# ── KK-FR-005 / FR-003: the draft keeps its request id until the server accepts ──

def test_draft_keeps_the_request_id_for_a_retry_and_drops_it_on_edit():
    got = _run(["ketuDraftOnInput", "ketuDraftOnSend"], """(() => {
        const a = ketuDraftOnInput('open leads');
        const sent = ketuDraftOnSend(a, 'id-1');
        const retry = ketuDraftOnSend(sent, 'id-2');      // network failed: same text, same id
        const edited = ketuDraftOnInput('open doctors');  // user changed it: a new request
        const resent = ketuDraftOnSend(edited, 'id-3');
        return [a, sent, retry, edited, resent];
    })()""")
    assert got == [{"text": "open leads", "crid": None}, {"text": "open leads", "crid": "id-1"},
                   {"text": "open leads", "crid": "id-1"}, {"text": "open doctors", "crid": None},
                   {"text": "open doctors", "crid": "id-3"}]


# ── KK-FR-034: cards, escaping, never raw detail ──────────────────────────────

def _card(card):
    return _run(["esc", "ketuCardHtml"], f"ketuCardHtml({json.dumps(card)}, p => ({{leads: 'WhatsApp Leads'}})[p] || p)")


def test_text_and_error_cards_escape_server_text():
    html = _card({"type": "text", "text": "<img src=x onerror=alert(1)>"})
    assert "<img" not in html and "&lt;img" in html
    assert "ketu-card-error" in _card({"type": "error", "text": "You don't have permission"})


def test_help_card_offers_open_page():
    html = _card({"type": "help", "text": "Open Leads and press Add.", "page": "leads"})
    assert 'data-ketu-go="leads"' in html and "Open WhatsApp Leads" in html


def test_table_card_shows_at_most_8_rows_and_a_show_all_link():
    rows = [[f"Patient {i}", f"10:{i:02d}"] for i in range(12)]
    html = _card({"type": "table", "title": "Today", "columns": ["Name", "Time"], "rows": rows, "page": "leads"})
    assert html.count("<tr>") == 1 + 8  # header + 8 rows
    assert "Patient 7" in html and "Patient 8" not in html
    assert "Show all 12 in WhatsApp Leads" in html


def test_unknown_card_type_falls_back_to_its_text():
    assert "later" in _card({"type": "something-new", "text": "later"})


@pytest.mark.parametrize("status, body, expected", [
    (403, {"detail": {"error_code": "assistant_disabled", "message": "Kriya Ketu is not enabled for this clinic."}},
     "Kriya Ketu is not enabled for this clinic."),
    (429, {"detail": {"error_code": "rate_limited", "message": "Too many requests. Please wait a few minutes."}},
     "Too many requests. Please wait a few minutes."),
    (422, {"detail": [{"msg": "String should have at most 1000 characters", "loc": ["body", "text"]}]},
     "Kriya couldn't send that. Please shorten or rephrase it."),
    (500, {"detail": "Internal Server Error"}, "Kriya had a problem. Your message is kept; press Send to try again."),
    (0, None, "Couldn't reach Kriya. Your message is kept; press Send to try again."),
])
def test_error_text_is_fixed(status, body, expected):
    assert _run(["ketuErrorText"], f"ketuErrorText({status}, {json.dumps(body)})") == expected


def test_detail_text_reads_structured_details():
    """apiFail() used to show "[object Object]" for {error_code, message} details."""
    got = _run(["detailText"], """[detailText({detail: {error_code: 'x', message: 'Plain words.'}}),
                                    detailText({detail: 'str'}), detailText({detail: [{msg: 'a'}, {msg: 'b'}]}),
                                    detailText({detail: {foo: 1}}), detailText({})]""")
    assert got == ["Plain words.", "str", "a b", "", ""]


# ── wiring ────────────────────────────────────────────────────────────────────

def test_composer_contract_in_the_widget():
    js = HTML[HTML.index("// ── Kriya Ketu"):HTML.index("// ── end Kriya Ketu")]
    for needle in ("'/admin/assistant/requests'", "'/admin/assistant/runs?active=1&limit=30'", "visible_pages",
                   "AbortController", "sessionStorage", "ketu_draft", 'maxlength="1000"', "Enter to send",
                   "crypto.randomUUID", "go(page)"):
        assert needle in js, needle
    # the panel opens on the composer (ux §6 Focus)
    assert "#ketuInput').focus()" in js
