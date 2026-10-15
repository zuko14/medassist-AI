"""KK-P2-03: Kriya Ketu clarify, preview, confirm and result cards (KK-FR-012, 022, 023, 033; ux §4.3, §6).

ketuRunHtml(run, pageLabel, nowMs) renders one run from its state; executed under node.
Interactive controls exist only while the run is waiting for them, and a tick only on
COMPLETED_VERIFIED ("Never render an unexecuted plan with a tick", ux §4.3).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "admin" / "index.html").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
NOW = 1_800_000_000_000
LABEL = "p => ({leaves: 'Leaves', leads: 'WhatsApp Leads'})[p] || p"


def _fn(name):
    m = re.search(rf"function {name}\(.*?\n\}}", HTML, re.S)
    assert m, f"{name}() not found"
    return m.group(0)


def _html(run):
    src = "\n".join(_fn(f) for f in ("esc", "ketuCardHtml", "ketuSecondsLeft", "ketuClock", "ketuRunHtml")) + \
        f"\nconsole.log(JSON.stringify(ketuRunHtml({json.dumps(run)}, {LABEL}, {NOW})));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _iso(ms_from_now):
    from datetime import datetime, timezone
    return datetime.fromtimestamp((NOW + ms_from_now) / 1000, tz=timezone.utc).isoformat()


def _run(state, cards, **kw):
    return {"run_id": "r1", "state": state, "cards": cards, "steps": [], "plan_hash": "h" * 64,
            "expires_at": _iso(120_000), "error_code": None, **kw}


# ── clarify (KK-FR-012) ───────────────────────────────────────────────────────

def test_clarify_form_offers_only_what_the_server_sent():
    card = {"type": "clarify", "missing": ["action", "doctor", "date"],
            "fields": [{"name": "action", "kind": "action"}, {"name": "doctor", "kind": "doctor"},
                       {"name": "date", "kind": "date"}],
            "actions": [{"id": "a.one", "summary": "<b>One</b>"}, {"id": "a.two", "summary": "Two"}],
            "candidates": [{"id": "d1", "name": "Dr. Srinivas Rao"}, {"id": "d3", "name": "Dr. Srinivas Kumar"}]}
    html = _html(_run("NEEDS_CLARIFICATION", [card]))
    assert 'data-ketu-clarify="r1"' in html
    assert html.count('type="radio" name="action"') == 2 and "&lt;b&gt;One" in html
    assert '<option value="d1">Dr. Srinivas Rao</option>' in html and '<option value="d3">' in html
    assert 'type="date" name="date"' in html and "Continue" in html


def test_a_lapsed_form_is_read_only():
    card = {"type": "clarify", "missing": ["date"], "fields": [{"name": "date", "kind": "date"}]}
    html = _html(_run("EXPIRED", [card]))
    assert "<form" not in html and "Expired" in html


# ── preview / confirm (KK-FR-022, 023) ────────────────────────────────────────

PREVIEW = {"type": "preview", "title": "Leave for <Dr Rao>", "bullets": ["3 bookings will be cancelled"],
           "rows": [["Sita", "10:00"]], "warnings": ["Patients are told by WhatsApp"], "affected": 3,
           "requires_ack": False}


def test_preview_has_confirm_cancel_and_a_countdown():
    html = _html(_run("NEEDS_CONFIRMATION", [PREVIEW]))
    assert 'data-ketu-confirm="r1"' in html and 'data-ketu-cancel="r1"' in html
    assert "data-ketu-exp=" in html and "2:00" in html
    assert "Leave for &lt;Dr Rao&gt;" in html and "3 bookings will be cancelled" in html
    assert "Patients are told by WhatsApp" in html and "Sita" in html
    assert "Verified" not in html and "✓" not in html


def test_typed_preview_asks_for_the_exact_count():
    card = {**PREVIEW, "type": "preview-typed", "typed_count": 12}
    html = _html(_run("NEEDS_CONFIRMATION", [card]))
    assert 'id="ketuTyped-r1"' in html and 'aria-describedby="ketuTypedHint-r1"' in html
    assert "Type <strong>12</strong> to confirm" in html and 'inputmode="numeric"' in html


def test_duplicate_risk_needs_a_tick_box():
    html = _html(_run("NEEDS_CONFIRMATION", [{**PREVIEW, "requires_ack": True}]))
    assert 'id="ketuAck-r1"' in html and 'type="checkbox"' in html


def test_the_tick_names_what_the_server_says_and_is_escaped():
    text = "I understand 2 bookings will be cancelled and the patients told now <b>"
    html = _html(_run("NEEDS_CONFIRMATION", [{**PREVIEW, "requires_ack": True, "ack_text": text}]))
    assert "I understand 2 bookings will be cancelled" in html and "&lt;b&gt;" in html and "<b>" not in html
    default = _html(_run("NEEDS_CONFIRMATION", [{**PREVIEW, "requires_ack": True}]))
    assert "I understand this may be sent twice" in default


def test_changed_plan_says_so():
    html = _html(_run("NEEDS_CONFIRMATION", [PREVIEW], error_code="plan_changed"))
    assert "changed" in html.lower()


@pytest.mark.parametrize("state, word", [("CANCELLED", "Cancelled"), ("EXPIRED", "Expired"),
                                         ("REJECTED", "Not done")])
def test_a_finished_preview_has_no_buttons(state, word):
    html = _html(_run(state, [PREVIEW]))
    assert "data-ketu-confirm" not in html and "data-ketu-cancel" not in html and word in html


def test_an_already_lapsed_countdown_shows_expired():
    html = _html(_run("NEEDS_CONFIRMATION", [PREVIEW], expires_at=_iso(-1000)))
    assert "Expired" in html


# ── results (KK-FR-031, 033) ──────────────────────────────────────────────────

STEPS = [{"label": "Add leave", "status": "done", "evidence": "read back from Leaves"},
         {"label": "Cancel Sita", "status": "failed", "evidence": None},
         {"label": "Cancel Ravi", "status": "skipped", "evidence": None}]


def test_verified_result_has_the_tick_and_evidence():
    card = {"type": "result-ok", "title": "Add leave", "evidence": ["read back from Leaves"], "page": "leaves"}
    html = _html(_run("COMPLETED_VERIFIED", [card], steps=STEPS[:1]))
    assert "Verified" in html and "read back from Leaves" in html and 'data-ketu-go="leaves"' in html
    assert 'tabindex="-1"' in html  # focus target after Confirm (ux §6)


def test_unknown_outcome_never_shows_a_tick():
    card = {"type": "result-unknown", "text": "Kriya couldn't confirm the result.", "page": "leaves"}
    html = _html(_run("UNKNOWN_OUTCOME", [card]))
    assert "Verified" not in html and "✓" not in html and "couldn" in html


def test_partial_lists_every_step_with_a_word():
    card = {"type": "result-partial", "text": "Some steps were completed.", "steps": STEPS}
    html = _html(_run("PARTIALLY_COMPLETED", [card], steps=STEPS))
    for word in ("Done", "Failed", "Skipped"):
        assert word in html
    assert "Verified" not in html


@pytest.mark.parametrize("expires_ms, expected", [(120_000, 120), (500, 1), (0, 0), (-5000, 0)])
def test_seconds_left(expires_ms, expected):
    src = _fn("ketuSecondsLeft") + f"\nconsole.log(ketuSecondsLeft({json.dumps(_iso(expires_ms))}, {NOW}));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert int(out.stdout) == expected


def test_widget_wires_confirm_cancel_and_answers():
    js = HTML[HTML.index("// ── Kriya Ketu"):HTML.index("// ── end Kriya Ketu")]
    for needle in ("/confirm'", "/cancel'", "reply_to_run_id", "slot_answers", "typed_count", "ack_duplicate",
                   "data-ketu-confirm", "setInterval"):
        assert needle in js, needle


# ── consent questions (KK-P4-07, REG-03): yes/no, no default, both required ──

def test_consent_questions_are_required_yes_no_radios_with_no_default():
    card = {"type": "clarify", "missing": ["data_consent", "whatsapp_opt_in"], "candidates": [], "actions": [],
            "fields": [{"name": "data_consent", "kind": "yesno", "label": "Patient agrees to <data> being stored"},
                       {"name": "whatsapp_opt_in", "kind": "yesno", "label": "Patient agrees to WhatsApp messages"}]}
    html = _html(_run("NEEDS_CLARIFICATION", [card]))
    for name in ("data_consent", "whatsapp_opt_in"):
        assert html.count(f'type="radio" name="{name}"') == 2
        assert f'name="{name}" value="yes" required' in html and f'name="{name}" value="no" required' in html
    assert "checked" not in html                                       # never pre-answered
    assert "<legend>Patient agrees to &lt;data&gt; being stored</legend>" in html
