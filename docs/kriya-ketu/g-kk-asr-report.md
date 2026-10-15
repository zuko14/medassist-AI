# Kriya Ketu — G-KK-ASR report

**Verdict: NOT RUN.** No recordings exist yet and the run is live and paid. STT stays off
(`ASSISTANT_STT_ENABLED=false`) until this report says PASS **and** DP-07 (privacy notice, Sarvam
terms) is done. Running `scripts/assistant_stt_eval.py` overwrites this file with the real result.

## What the owner needs to do
1. **Record 60 clips** on a normal laptop microphone in a normal clinic room, as the browser records
   them (WebM from Chrome/Edge, or MP4 from Safari; ≤ 30 s, ≤ 1 MiB each):
   20 Telugu, 20 Hindi, 20 English; in each language 10 are code-mixed with English.
   Use real staff phrasing for real jobs: leave ("Dr X ni repu leave lo pettu"), appointments for a
   date, check-in, lab price changes, opening pages. Name doctors that exist in the manifest.
   Use several speakers. No patient names or numbers.
2. **Write `manifest.json`** next to the clips (format in the script's docstring): a fixed `now`,
   the doctor list, and for each clip the language, `code_mixed`, `duration_ms`, the sentence actually
   said (`reference`), and the expected route (`expect`).
3. **Dry run (free):** `python scripts/assistant_stt_eval.py --manifest <dir>/manifest.json --fake`.
   It must show *Routed after edit* = 60/60 for every mode. If a reference sentence does not route,
   that is a Kriya Ketu rules gap: fix the phrase list (or the sentence) before paying for the live run.
4. **Live run (paid):** `SARVAM_API_KEY=… python scripts/assistant_stt_eval.py --manifest <dir>/manifest.json --live --paise-per-minute <Sarvam rate>`.
   Cost is about 2 × the total audio minutes at Sarvam's per-minute rate. Run it once with `--hint auto`
   (the widget default) and keep that report. A `--hint declared` run is optional and only for comparison.
5. **Record the outcome:** the verdict, the recommended mode (this decides DP-03), p95 latency, and
   the confirmed per-minute rate (set `VOICE_STT_PAISE_PER_MINUTE` so `ai_usage_ledger` costs are real).

## Pass bar (openrouter-and-speech-spec.md B.4)
- The set is complete: 60 = 20 te + 20 hi + 20 en, 10 code-mixed each.
- In at least one mode, **≥ 90 %** of clips route to the right action with the right doctor and date
  **with zero edits**. Scoring uses Kriya Ketu's own router (`rules.route`), so the result is the
  experience staff will have.
- **≥ 98 %** route correctly after edits, i.e. the reference sentence routes.

If the bar is missed, narrow the language claims in `spec.md` (for example English + Telugu only)
before enabling STT. Do not lower the bar.

## Still to confirm at the live run
- Sarvam REST's maximum clip duration. The current API reference does not state it; the spec assumes
  < 30 s.
- That `mode` (including `codemix`) still requires `saaras:v3`, now that Sarvam's default model is
  `saaras:v4`.
- The per-minute price.
