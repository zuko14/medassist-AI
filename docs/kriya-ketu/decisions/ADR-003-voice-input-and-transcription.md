# ADR-003 — Voice input: browser recording → server-side Sarvam REST → editable transcript

Status: **Proposed** · Date: 2026-10-13

## Context
Staff should dictate an instruction, see it as editable text, and submit it explicitly. Audit facts:
- Every response carries `Permissions-Policy: microphone=()` (`app/utils/security.py:393`), so the
  microphone is blocked today.
- CSP `connect-src 'self'` blocks direct browser calls to third-party APIs.
- The repo already uses Sarvam (streaming WebSocket STT at 8 kHz for telephony; key in `settings`).
- Sarvam's REST `POST /speech-to-text` accepts WebM/OPUS/MP4/WAV, `language_code=unknown`, and a
  `codemix` mode (saaras:v3). It is documented for clips under 30 s.

## Options
1. Browser Web Speech API (`webkitSpeechRecognition`).
2. Stream browser audio to the server over a WebSocket and reuse the Sarvam streaming provider.
3. Record a short clip in the browser (`MediaRecorder`), POST it to `/admin/assistant/transcribe`, and
   the server calls Sarvam REST.
4. Send the audio to a multimodal LLM via OpenRouter.

## Decision
Option 3, with a 30 s cap, no interim transcripts, and no auto-submit.

## Reasons
- Works in Chrome, Edge, Firefox and Safari (`MediaRecorder` produces webm/opus or mp4, both accepted),
  with no client-side encoder.
- One processor (Sarvam) that is already used and contracted. Option 1 sends audio to Google (Chrome),
  has no Firefox support, and its language behaviour is outside our control.
- Option 2 adds WebSocket plumbing through the web worker for a ≤ 30 s utterance.
- Option 4 costs LLM tokens per utterance, which is against the cost principle.
- No interim results means no risk of submitting a half-recognised command.

## Consequences
- Requires `microphone=(self)` (KK-P3-01).
- Latency = recording + upload + ~1–3 s recognition (to measure in G-KK-ASR).
- Language claims are limited to te/hi/en + code-mix until evaluated.

## Risks
- The Sarvam REST limit or pricing differs from the docs → G-KK-ASR checks before enabling.
- Some Safari versions record in formats outside the accepted list → mime negotiation + a 415 mapped to
  "Voice not supported in this browser".

## Verification
VOICE-01..11, G-KK-ASR (60 utterances), the browser matrix.

## Revisit when
Staff want live captions (would need streaming, option 2), or a cheaper/on-device ASR with comparable
te/hi accuracy appears.
