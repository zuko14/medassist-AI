"""KK-P3-04: the G-KK-ASR evaluation tool (scripts/assistant_stt_eval.py; speech spec B.4).

Hermetic: the provider is stt.FakeStt or a stub, never Sarvam. Checks the manifest rules
(60 clips, 20 per language, 10 code-mixed each), entity scoring through Ketu's own router,
the two pass bars (>= 90 % entities right with zero edits, >= 98 % routed after edits), the
transcribe-vs-codemix comparison for DP-03, file limits, and the report.
"""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from app.assistant import stt

SPEC = importlib.util.spec_from_file_location(
    "assistant_stt_eval", Path(__file__).resolve().parents[1] / "scripts" / "assistant_stt_eval.py")
ev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ev)

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
GOOD = "Mark Dr. Srinivas Rao on leave tomorrow"
DOCTORS = [{"id": "d1", "name": "Dr. Srinivas Rao"}, {"id": "d2", "name": "Dr. Anitha Reddy"}]
LEAVE = {"kind": "action", "action": "doctor_leave.create",
         "params": {"doctor": "Dr. Srinivas Rao", "date": "2026-10-16"}}


def _clip(i, lang, mixed=False, reference=GOOD, expect=None):
    return {"file": f"{lang}_{i:02d}.webm", "language": lang, "code_mixed": mixed, "duration_ms": 3000,
            "reference": reference, "expect": expect or LEAVE}


def _full_set():
    clips = []
    for lang in ("te", "hi", "en"):
        clips += [_clip(i, lang, mixed=i < 10) for i in range(20)]
    return clips


def _manifest(tmp_path, clips):
    for c in clips:
        (tmp_path / c["file"]).write_bytes(WEBM)
    m = {"now": "2026-10-15T09:00:00+05:30", "doctors": DOCTORS, "clips": clips}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(m), encoding="utf-8")
    return path


class Scripted:
    """Returns the next scripted transcript for the mode (or raises it); records every call."""

    def __init__(self, by_mode):
        self.by_mode, self.calls = by_mode, []

    async def transcribe(self, audio, mime, language_code, mode):
        self.calls.append((mime, language_code, mode))
        text = self.by_mode[mode].pop(0)
        if isinstance(text, Exception):
            raise text
        return stt.Heard(text, "te-IN")


# ── scoring ────────────────────────────────────────────────────────────────────

def test_route_match_checks_kind_action_and_every_expected_param():
    m = ev.load_manifest_dict({"now": "2026-10-15T09:00:00+05:30", "doctors": DOCTORS, "clips": []})
    assert ev.routes_as(GOOD, LEAVE, m) is True
    assert ev.routes_as("Mark Dr. Anitha Reddy on leave tomorrow", LEAVE, m) is False   # wrong doctor
    assert ev.routes_as("Mark Dr. Srinivas Rao on leave today", LEAVE, m) is False      # wrong date
    assert ev.routes_as("open leads", {"kind": "nav", "page": "leads"}, m) is True
    assert ev.routes_as("open doctors", {"kind": "nav", "page": "leads"}, m) is False


@pytest.mark.parametrize("ref, hyp, wer", [
    ("mark ravi on leave", "mark ravi on leave", 0.0),
    ("mark ravi on leave", "mark ravi leave", 0.25),
    ("mark ravi on leave", "", 1.0),
    ("Mark, Ravi!", "mark ravi", 0.0),          # punctuation and case don't count
])
def test_word_error_rate(ref, hyp, wer):
    assert ev.word_error_rate(ref, hyp) == pytest.approx(wer)


# ── set rules (B.4: 60 utterances, 20 per language, 10 of them code-mixed) ────

def test_complete_set_has_no_problems():
    assert ev.set_problems(_full_set()) == []


def test_incomplete_set_is_reported():
    clips = _full_set()[:-1]
    clips[0]["code_mixed"] = False
    problems = ev.set_problems(clips)
    assert any("60" in p for p in problems)
    assert any("en" in p and "20" in p for p in problems)
    assert any("te" in p and "code-mixed" in p for p in problems)


def test_unknown_language_or_missing_fields_are_rejected():
    with pytest.raises(ValueError):
        ev.load_manifest_dict({"now": "2026-10-15T09:00:00+05:30", "doctors": [], "clips": [_clip(0, "ta")]})
    bad = _clip(0, "te")
    del bad["expect"]
    with pytest.raises(ValueError):
        ev.load_manifest_dict({"now": "2026-10-15T09:00:00+05:30", "doctors": [], "clips": [bad]})


# ── a run: pass bars and mode comparison ──────────────────────────────────────

def test_perfect_run_passes_both_bars_for_each_mode(tmp_path):
    path = _manifest(tmp_path, _full_set())
    fake = stt.FakeStt(text=GOOD, language="te-IN")
    result = asyncio.run(ev.evaluate(path, fake, modes=("transcribe", "codemix"), hint="auto"))
    for mode in ("transcribe", "codemix"):
        r = result["modes"][mode]
        assert r["entities_ok"] == 60 and r["routed_after_edit"] == 60 and r["failed"] == 0
        assert r["passed"] is True
    assert result["passed"] is True
    assert len(fake.calls) == 120
    assert {c[2] for c in fake.calls} == {"unknown"}          # hint=auto: the widget default
    assert {c[1] for c in fake.calls} == {"audio/webm"}


def test_bars_are_90_and_98_percent(tmp_path):
    path = _manifest(tmp_path, _full_set())
    bad = "Mark Dr. Anitha Reddy on leave"
    # transcribe: 54/60 right (90 %) -> pass; codemix: 53/60 -> fail
    prov = Scripted({"transcribe": [GOOD] * 54 + [bad] * 6, "codemix": [GOOD] * 53 + [bad] * 7})
    result = asyncio.run(ev.evaluate(path, prov, modes=("transcribe", "codemix"), hint="auto"))
    assert result["modes"]["transcribe"]["passed"] is True
    assert result["modes"]["codemix"]["passed"] is False
    assert result["recommended_mode"] == "transcribe"           # DP-03: better entity accuracy wins
    assert result["passed"] is True                             # the gate needs one passing mode


def test_after_edit_bar_fails_when_references_do_not_route(tmp_path):
    clips = _full_set()
    for c in clips[:2]:
        c["reference"] = "do something nobody understands"      # 58/60 = 96.7 % < 98 %
    path = _manifest(tmp_path, clips)
    fake = stt.FakeStt(text=GOOD)
    r = asyncio.run(ev.evaluate(path, fake, modes=("transcribe",), hint="auto"))["modes"]["transcribe"]
    assert r["routed_after_edit"] == 58 and r["passed"] is False


def test_provider_failures_count_as_wrong_and_are_listed(tmp_path):
    path = _manifest(tmp_path, _full_set())
    texts = [GOOD] * 59 + [stt.SttError("timeout")]
    r = asyncio.run(ev.evaluate(path, Scripted({"transcribe": texts}), modes=("transcribe",), hint="auto"))
    m = r["modes"]["transcribe"]
    assert m["failed"] == 1 and m["entities_ok"] == 59
    assert any(row["error"] == "timeout" for row in m["rows"])


def test_declared_hint_sends_the_clip_language(tmp_path):
    path = _manifest(tmp_path, _full_set())
    fake = stt.FakeStt(text=GOOD)
    asyncio.run(ev.evaluate(path, fake, modes=("transcribe",), hint="declared"))
    assert {c[2] for c in fake.calls} == {"te-IN", "hi-IN", "en-IN"}


def test_incomplete_set_fails_the_gate_even_if_every_clip_is_right(tmp_path):
    path = _manifest(tmp_path, _full_set()[:30])
    fake = stt.FakeStt(text=GOOD)
    result = asyncio.run(ev.evaluate(path, fake, modes=("transcribe",), hint="auto"))
    assert result["modes"]["transcribe"]["entities_ok"] == 30
    assert result["passed"] is False and result["set_problems"]


def test_files_over_the_endpoint_limits_are_not_sent(tmp_path):
    clips = _full_set()
    clips[0]["file"] = "big.webm"
    clips[1]["file"] = "clip.wav"
    clips[2]["duration_ms"] = 31000
    path = _manifest(tmp_path, clips)
    (tmp_path / "big.webm").write_bytes(WEBM + b"\x00" * stt.MAX_BYTES)
    fake = stt.FakeStt(text=GOOD)
    r = asyncio.run(ev.evaluate(path, fake, modes=("transcribe",), hint="auto"))["modes"]["transcribe"]
    assert len(fake.calls) == 57
    errors = sorted(row["error"] for row in r["rows"] if row["error"])
    assert errors == ["too_large", "too_long", "unsupported_format"]


def test_latency_and_cost_summary(tmp_path):
    path = _manifest(tmp_path, _full_set())
    fake = stt.FakeStt(text=GOOD)
    r = asyncio.run(ev.evaluate(path, fake, modes=("transcribe",), hint="auto", paise_per_minute=30))
    m = r["modes"]["transcribe"]
    assert m["audio_seconds"] == 180 and m["cost_paise"] == 90     # 60 x 3 s at 30 paise/min
    assert m["p95_ms"] >= 0


# ── report ─────────────────────────────────────────────────────────────────────

def test_report_states_the_verdict_bars_and_per_language_rows(tmp_path):
    path = _manifest(tmp_path, _full_set())
    fake = stt.FakeStt(text=GOOD)
    result = asyncio.run(ev.evaluate(path, fake, modes=("transcribe", "codemix"), hint="auto"))
    md = ev.render_report(result, provider_label="fake")
    for needle in ("G-KK-ASR", "PASS", "≥ 90 %", "≥ 98 %", "transcribe", "codemix", "| te |", "| hi |", "| en |",
                   "code-mixed", "Recommended mode (DP-03)", "fake"):
        assert needle in md, needle


def test_live_run_refuses_without_a_key(monkeypatch, capsys):
    from app.config import settings
    monkeypatch.setattr(settings, "sarvam_api_key", "")
    assert ev.main(["--manifest", "x.json", "--live"]) == 2
    assert "SARVAM_API_KEY" in capsys.readouterr().out


def test_fake_dry_run_end_to_end_writes_the_report(tmp_path):
    path = _manifest(tmp_path, _full_set())
    out = tmp_path / "report.md"
    assert ev.main(["--manifest", str(path), "--fake", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "dry run (echo)" in text and "**Verdict: PASS**" in text


def test_no_provider_flag_refuses():
    with pytest.raises(SystemExit):
        ev.main(["--manifest", "x.json"])   # neither --live nor --fake
