"""Gate G-KK-ASR: evaluate Kriya Ketu voice input on real recordings (openrouter-and-speech-spec.md B.4).

    # 1. free dry run: checks the manifest and that every reference sentence routes
    python scripts/assistant_stt_eval.py --manifest path/to/manifest.json --fake
    # 2. live, paid (Sarvam, ~60 clips x 2 modes): the owner runs it with the production key
    SARVAM_API_KEY=... python scripts/assistant_stt_eval.py --manifest path/to/manifest.json --live

Each clip goes through the same provider, language mapping and limits as POST /admin/assistant/transcribe
(app/assistant/stt.py), once per mode (transcribe, codemix). A clip counts as "entities right" when its
raw transcript, with zero edits, routes through Ketu's own router (app/assistant/rules.route) to the
expected action/page with every expected parameter (doctor, date, ...). "Routed after edit" routes the
reference sentence, i.e. what the user would correct the text to.
Pass (spec B.4): the set is complete (60 clips; 20 per te/hi/en; 10 of each code-mixed) and at least one
mode has >= 90 % entities right and >= 98 % routed after edit. DP-03: the mode with more entities right
becomes the default. Never run in CI. Audio is read from disk and sent; nothing is stored.

manifest.json:
    {"now": "2026-10-15T09:00:00+05:30",            # the moment "today"/"tomorrow" are resolved against
     "doctors": [{"id": "d1", "name": "Dr. Srinivas Rao"}],
     "clips": [{"file": "te_01.webm", "language": "te", "code_mixed": false, "duration_ms": 3200,
                "reference": "Dr Srinivas Rao ni repu leave lo pettu",
                "expect": {"kind": "action", "action": "doctor_leave.create",
                           "params": {"doctor": "Dr. Srinivas Rao", "date": "2026-10-16"}}}]}
`file` is relative to the manifest; WebM or MP4 (what browsers record). `expect` for navigation:
{"kind": "nav", "page": "leads"}.
"""

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.assistant import rules, stt  # noqa: E402
from app.config import settings  # noqa: E402

LANGS = ("te", "hi", "en")
DECLARED = {"te": "te-IN", "hi": "hi-IN", "en": "en-IN"}
MIME_BY_EXT = {".webm": "audio/webm", ".mp4": "audio/mp4", ".m4a": "audio/mp4"}
ENTITY_BAR, EDIT_BAR = 0.90, 0.98
SET_SIZE, PER_LANG, MIXED_PER_LANG = 60, 20, 10


def load_manifest_dict(data: dict, base: Optional[Path] = None) -> dict:
    try:
        now = datetime.fromisoformat(data["now"])
        doctors, clips = list(data["doctors"]), list(data["clips"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"manifest needs now (ISO with offset), doctors and clips: {e}")
    if now.tzinfo is None:
        raise ValueError("manifest 'now' needs a UTC offset, e.g. +05:30")
    for i, c in enumerate(clips):
        missing = [k for k in ("file", "language", "reference", "expect") if k not in c]
        if missing:
            raise ValueError(f"clip {i}: missing {', '.join(missing)}")
        if c["language"] not in LANGS:
            raise ValueError(f"clip {i}: language must be one of {LANGS}, got {c['language']!r}")
        if c["expect"].get("kind") not in ("action", "nav"):
            raise ValueError(f"clip {i}: expect.kind must be 'action' or 'nav'")
    return {"now": now, "doctors": doctors, "clips": clips, "base": base or Path(".")}


def routes_as(text: str, expect: dict, manifest: dict) -> bool:
    r = rules.route(text, doctors=manifest["doctors"], now=manifest["now"])
    if r.kind != expect["kind"]:
        return False
    if expect["kind"] == "nav":
        return r.page == expect["page"]
    return r.action_id == expect["action"] and all(r.params.get(k) == v for k, v in expect.get("params", {}).items())


def _words(text: str) -> list:
    return re.sub(r"[^\w\s]", " ", (text or "").lower()).split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = _words(reference), _words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, rw in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, hw in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[-1] / len(ref)


def set_problems(clips: list) -> list:
    problems = []
    if len(clips) < SET_SIZE:
        problems.append(f"{len(clips)} clips; the gate needs {SET_SIZE}")
    for lang in LANGS:
        mine = [c for c in clips if c["language"] == lang]
        mixed = sum(1 for c in mine if c.get("code_mixed"))
        if len(mine) < PER_LANG:
            problems.append(f"{lang}: {len(mine)} clips; needs {PER_LANG}")
        if mixed < MIXED_PER_LANG:
            problems.append(f"{lang}: {mixed} code-mixed clips; needs {MIXED_PER_LANG}")
    return problems


def _check_file(clip: dict, base: Path):
    """(audio, mime, None) or (None, None, error) using the endpoint's limits."""
    path = base / clip["file"]
    mime = MIME_BY_EXT.get(path.suffix.lower())
    if not mime:
        return None, None, "unsupported_format"
    if (clip.get("duration_ms") or 0) > stt.MAX_MS:
        return None, None, "too_long"
    audio = path.read_bytes()
    if len(audio) > stt.MAX_BYTES:
        return None, None, "too_large"
    if not audio or not stt.looks_like(mime, audio):
        return None, None, "unsupported_format"
    return audio, mime, None


def _p95(values: list) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)]


async def _run_mode(manifest: dict, provider, mode: str, hint: str, echo: bool, paise_per_minute: int) -> dict:
    rows, latencies, audio_ms = [], [], 0
    for clip in manifest["clips"]:
        row = {"file": clip["file"], "language": clip["language"], "code_mixed": bool(clip.get("code_mixed")),
               "transcript": "", "detected": None, "entities_ok": False, "error": None, "wer": None,
               "routed_after_edit": routes_as(clip["reference"], clip["expect"], manifest)}
        audio, mime, error = _check_file(clip, manifest["base"])
        if error:
            row["error"] = error
            rows.append(row)
            continue
        code = DECLARED[clip["language"]] if hint == "declared" else "unknown"
        started = time.monotonic()
        try:
            heard = stt.Heard(clip["reference"], DECLARED[clip["language"]]) if echo else \
                await provider.transcribe(audio, mime, code, mode)
        except stt.SttError as e:
            row["error"] = e.code
        except Exception as e:  # report and keep going; one bad clip must not hide the rest
            row["error"] = type(e).__name__
        else:
            latencies.append(int((time.monotonic() - started) * 1000))
            row.update(transcript=heard.text, detected=heard.language,
                       entities_ok=routes_as(heard.text, clip["expect"], manifest),
                       wer=round(word_error_rate(clip["reference"], heard.text), 3))
        audio_ms += clip.get("duration_ms") or 0
        rows.append(row)
    total = len(rows) or 1
    entities_ok = sum(r["entities_ok"] for r in rows)
    routed = sum(r["routed_after_edit"] for r in rows)
    wers = [r["wer"] for r in rows if r["wer"] is not None]
    return {
        "rows": rows, "entities_ok": entities_ok, "routed_after_edit": routed,
        "failed": sum(1 for r in rows if r["error"]), "total": len(rows),
        "passed": entities_ok / total >= ENTITY_BAR and routed / total >= EDIT_BAR,
        "p50_ms": sorted(latencies)[len(latencies) // 2] if latencies else 0, "p95_ms": _p95(latencies),
        "mean_wer": round(sum(wers) / len(wers), 3) if wers else None,
        "audio_seconds": audio_ms // 1000, "cost_paise": stt.cost_paise(audio_ms, paise_per_minute),
    }


async def evaluate(manifest_path, provider, *, modes=("transcribe", "codemix"), hint="auto",
                   paise_per_minute: Optional[int] = None, echo: bool = False) -> dict:
    path = Path(manifest_path)
    manifest = load_manifest_dict(json.loads(path.read_text(encoding="utf-8")), path.parent)
    rate = settings.voice_stt_paise_per_minute if paise_per_minute is None else paise_per_minute
    results = {m: await _run_mode(manifest, provider, m, hint, echo, rate) for m in modes}
    problems = set_problems(manifest["clips"])
    # DP-03: a passing mode beats a failing one; then more entities right; ties keep the listed order.
    best = max(modes, key=lambda m: (results[m]["passed"], results[m]["entities_ok"], -modes.index(m)))
    return {"modes": results, "set_problems": problems, "recommended_mode": best, "hint": hint,
            "passed": not problems and any(r["passed"] for r in results.values()),
            "clips": len(manifest["clips"]), "model": settings.assistant_stt_model}


def _pct(n: int, d: int) -> str:
    return f"{(100 * n / d) if d else 0:.1f} %"


def render_report(result: dict, provider_label: str) -> str:
    verdict = "PASS" if result["passed"] else "FAIL"
    out = [
        "# Kriya Ketu — G-KK-ASR report", "",
        f"Run: {datetime.now().astimezone().isoformat(timespec='minutes')} · provider: {provider_label} · "
        f"model `{result['model']}` · language hint: {result['hint']} · clips: {result['clips']}", "",
        f"**Verdict: {verdict}** (bars: entities right with zero edits ≥ 90 %, routed after edits ≥ 98 %, "
        "complete set of 60 = 20 te + 20 hi + 20 en, 10 code-mixed each)", "",
    ]
    if result["set_problems"]:
        out += ["Set problems (the gate cannot pass until fixed):", *[f"- {p}" for p in result["set_problems"]], ""]
    out += [f"**Recommended mode (DP-03): `{result['recommended_mode']}`**", "",
            "| Mode | Entities right (0 edits) | Routed after edit | Failed calls | Mean WER | p50 ms | p95 ms "
            "| Audio s | Est. cost (paise) | Bars |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for mode, r in result["modes"].items():
        out.append(f"| {mode} | {r['entities_ok']}/{r['total']} ({_pct(r['entities_ok'], r['total'])}) | "
                   f"{r['routed_after_edit']}/{r['total']} ({_pct(r['routed_after_edit'], r['total'])}) | "
                   f"{r['failed']} | {r['mean_wer']} | {r['p50_ms']} | {r['p95_ms']} | {r['audio_seconds']} | "
                   f"{r['cost_paise']} | {'pass' if r['passed'] else 'fail'} |")
    for mode, r in result["modes"].items():
        out += ["", f"## `{mode}` by language", "",
                "| Language | Plain: entities right | code-mixed: entities right |", "|---|---|---|"]
        for lang in LANGS:
            cells = []
            for mixed in (False, True):
                rows = [x for x in r["rows"] if x["language"] == lang and x["code_mixed"] == mixed]
                ok = sum(x["entities_ok"] for x in rows)
                cells.append(f"{ok}/{len(rows)}")
            out.append(f"| {lang} | {cells[0]} | {cells[1]} |")
        misses = [x for x in r["rows"] if not x["entities_ok"] or not x["routed_after_edit"]]
        if misses:
            out += ["", "Misses (test utterances only; no patient data):", "",
                    "| File | Lang | Error | Detected | Transcript | Reference routes |", "|---|---|---|---|---|---|"]
            for x in misses:
                text = x["transcript"].replace("|", "\\|")
                out.append(f"| {x['file']} | {x['language']} | {x['error'] or ''} | {x['detected'] or ''} | "
                           f"{text} | {'yes' if x['routed_after_edit'] else 'no'} |")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", required=True)
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--live", action="store_true", help="call Sarvam (paid)")
    who.add_argument("--fake", action="store_true", help="dry run: transcript = reference, no network")
    ap.add_argument("--modes", default="transcribe,codemix")
    ap.add_argument("--hint", choices=("auto", "declared"), default="auto",
                    help="auto = the widget default (language_code=unknown); declared = the clip's language")
    ap.add_argument("--paise-per-minute", type=int, default=None, help="default: VOICE_STT_PAISE_PER_MINUTE")
    ap.add_argument("--out", default="docs/kriya-ketu/g-kk-asr-report.md")
    args = ap.parse_args(argv)
    if args.live and not settings.sarvam_api_key:
        print("SARVAM_API_KEY is not set")
        return 2
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
    result = asyncio.run(evaluate(args.manifest, None if args.fake else stt.SarvamRest(), modes=modes,
                                  hint=args.hint, paise_per_minute=args.paise_per_minute, echo=args.fake))
    report = render_report(result, "dry run (echo)" if args.fake else "Sarvam REST (live)")
    Path(args.out).write_text(report, encoding="utf-8")
    print(report)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
