# Kriya Ketu — OpenRouter Fallback and Speech-to-Text Specification

Parent: [spec.md](spec.md) · ADRs: [ADR-002](decisions/ADR-002-deterministic-first-routing.md),
[ADR-003](decisions/ADR-003-voice-input-and-transcription.md)

Sources checked on 2026-10-13: OpenRouter *Structured Outputs* and *Tool Calling* guides; Sarvam
*Speech-to-Text REST* reference. Model capability claims below are **to be re-verified at implementation
time** (gate G-KK-LLM, G-KK-ASR), because OpenRouter support varies **by provider endpoint**, not by model.

---

## Part A — OpenRouter (fallback only)

### A.1 When it is called
Only after layers 1–3 fail ([architecture.md](architecture.md) §3), i.e.:
- no navigation, rule or help match; **or**
- two or more actions match with scores within 0.1 of each other; **and**
- `ASSISTANT_LLM_ENABLED=true`, the per-user rate limit is OK (20/h, `PersistentRateLimiter` pattern
  from `app/utils/security.py`), and the text is ≥ 3 words.

It is **never** called for slot filling (L3 handles that), confirmation, execution, verification,
summarising results, or "chatting". Expected share: ≤ 15 % of requests (KK-NFR-COST-01).

### A.2 Why structured outputs, not tool calling
OpenRouter tool calling is a multi-turn protocol: the model proposes `tool_calls`, the client runs them
and sends the results back, and the model may propose more. That costs ≥ 2 calls per request and lets the
model steer execution. Kriya Ketu needs **one classification**: *which registered action and which
surface slot strings?* Therefore:

- `response_format: {"type": "json_schema", "json_schema": {"name": "ketu_intent", "strict": true, "schema": …}}`
- `provider: {"require_parameters": true}` so OpenRouter routes only to endpoints that support
  `response_format` (per the OpenRouter docs; strictness still varies by provider, so output is
  **always** re-validated with Pydantic).
- No `tools`, no second turn, no tool results fed back.

### A.3 Schema (built per request)
```json title="ketu_intent schema (proposed)"
{
  "type": "object",
  "additionalProperties": false,
  "required": ["kind", "action_id", "slots", "question_for_user"],
  "properties": {
    "kind": {"enum": ["command", "question", "hypothetical", "negated", "unsupported", "multi"]},
    "action_id": {"enum": ["<only the action ids this user may run, + null>"]},
    "slots": {
      "type": "object", "additionalProperties": false,
      "properties": {
        "doctor_text": {"type": ["string", "null"], "maxLength": 80},
        "date_text": {"type": ["string", "null"], "maxLength": 40},
        "end_date_text": {"type": ["string", "null"], "maxLength": 40},
        "leave_type": {"enum": ["full", "half_morning", "half_evening", null]},
        "patient_text": {"type": ["string", "null"], "maxLength": 80},
        "booking_ref": {"type": ["string", "null"], "maxLength": 20},
        "page": {"enum": ["<page ids visible to this user>", null]},
        "message_text": {"type": ["string", "null"], "maxLength": 500}
      }
    },
    "question_for_user": {"type": ["string", "null"], "maxLength": 160}
  }
}
```
Post-processing (deterministic):
1. Pydantic-validate. Any failure → `REJECTED/unsupported` ("I couldn't match that").
2. `action_id` must be in the enum the server sent (recheck; never trust the enum to be enforced).
3. Slot strings go through the **same resolvers** as rules. Any UUID-looking value is discarded.
4. `kind == "multi"` → reply "Please send one request at a time", listing the recognised actions as chips.
   No automatic decomposition.
5. `question_for_user` is shown only as plain text (escaped), and only on a `NEEDS_CLARIFICATION` card.

### A.4 Prompt (system message, static, ≈ 350 tokens + action list)
- Role: "You classify a hospital staff member's instruction into exactly one action id from the list,
  or null." Short description per action (≤ 12 words each, from `ActionSpec.summary`).
- Rules: never invent ids, never output database ids, dates as the user said them, `negated` if the user
  says not to do something, `hypothetical` for "what if".
- The user text is placed in a single `user` message, delimited. Only the current request is sent: no
  history, no record data, no patient data (PII minimisation; see the threat model T-05).
- Text is pre-cleaned with the existing `sanitize_user_input` (`app/utils/security.py:114`) and
  `strip_injection_markers` (`app/utils/security.py:141`) helpers (verified 2026-10-13).

### A.5 Configuration (new settings in `app/config.py`, proposed)
| Setting | Default | Notes |
|---|---|---|
| `assistant_enabled` | `False` | Global kill switch |
| `assistant_llm_enabled` | `True` | Fallback on/off without redeploying code |
| `assistant_llm_model` | `google/gemini-3.1-flash-lite` | The voice NLU uses it for JSON classification in te/hi/en (`app/config.py:234`). **Re-verify structured-output support on OpenRouter (G-KK-LLM).** `deepseek/deepseek-chat` is excluded: it returned no valid JSON in JSON mode (06-AI-SYSTEM.md §7). |
| `assistant_llm_fallback_model` | `openai/gpt-4.1-mini` | Same reasoning (voice fallback) |
| `assistant_llm_timeout_seconds` | `5.0` | One attempt (`max_attempts=1`), as the voice NLU does; the UI shows a spinner |
| `assistant_llm_max_tokens` | `200` | The output is a small JSON object |
| `assistant_llm_rate_per_hour` | `20` | Per user |
| `assistant_stt_enabled` | `False` | Mic on/off |
| `assistant_stt_model` | `saaras:v3` | Matches `sarvam_stt_model`; `codemix` mode is v3-only |
| `assistant_stt_timeout_seconds` | `10.0` | |

Secrets: reuses `OPENROUTER_API_KEY` and `SARVAM_API_KEY` (server-only). No new secret.

### A.6 Call path and accounting
`app/assistant/llm.py` → `call_ai_gateway(messages, task_type="assistant_intent", clinic_id=…,
primary_model=…, fallback_model=…, timeout=…, max_tokens=200, temperature=0, response_format=…,
max_attempts=1)`.

`call_ai_gateway` today builds its own payload and **does not pass a `provider` object**. Adding an
optional `provider_prefs: dict | None = None` parameter (default `None`, so existing callers are
byte-identical) is the **only** change to `ai_gateway.py` (KK-P1-07). `SpendCapExceededError` and
`ai_usage_ledger` recording come for free. `assistant_intent` is **not** added to
`PATIENT_FACING_TASKS`, so the admin cap applies.

### A.7 Cost metrics (all from existing or new tables, no new infra)
| Metric | Formula |
|---|---|
| Deterministic route % | `count(route_layer in ('nav','rules','form')) / count(*)` over `assistant_runs` |
| LLM invocation % | `count(route_layer='llm') / count(*)` |
| Avg tokens per LLM request | `avg(total_tokens)` from `ai_usage_ledger where task_type='assistant_intent'` |
| Cost per successful task | `sum(cost_paise where task_type like 'assistant_%') / count(state='COMPLETED_VERIFIED')` |
| Failed model calls | `ai_usage_ledger.success=false` for `assistant_intent` |
| LLM→success rate | `count(route_layer='llm' and state='COMPLETED_VERIFIED') / count(route_layer='llm')` |
| Latency by route | percentile of `assistant_runs.route_ms` grouped by `route_layer` |
| Cost by tenant / module | group by `clinic_id`, `split_part(action_id,'.',1)` |

Retry cost is 0 by design (`max_attempts=1`). Tool-call rate is 0 by design (no tools). Caching: **none**.
Each request is classified fresh; no cross-user or cross-tenant cache exists to leak.

### A.8 Rule-corpus feedback loop (cost reduction over time, no LLM training)
Every `route_layer='llm'` run that ends `COMPLETED_VERIFIED` is listed in a weekly report: input text →
action. A developer promotes good phrasings into `rules.py` triggers. This mirrors the voice
receptionist's reviewed-suggestion pattern (migration 106), but stays **manual** in v1 (DP-06).

---

## Part B — Voice input (speech-to-text)

### B.1 Options evaluated
| Option | Verdict | Why |
|---|---|---|
| Browser Web Speech API (`webkitSpeechRecognition`) | **Rejected as the primary path** | Not in Firefox. In Chrome the audio is sent to Google's servers: a third processor not covered by the clinic's DPDP notice. Language and code-mix behaviour is out of our control. It is still blocked by `Permissions-Policy: microphone=()`. |
| Reuse the Sarvam **streaming** WS (`app/voice/providers/sarvam.py`) | Rejected for v1 | Built for 8 kHz telephony with VAD. Needs browser→server audio streaming over a WebSocket through the web worker. More moving parts than a ≤ 30 s utterance needs. |
| **Sarvam REST `POST /speech-to-text`** via a server proxy | **Chosen** | One HTTPS call per utterance. Accepts WebM, OPUS and MP4, so browser `MediaRecorder` output goes as-is. `language_code=unknown` auto-detect. `codemix` mode (saaras:v3) for Telugu/Hindi + English. The vendor and key are already contracted. The documented sync limit is < 30 s, which matches our cap. |
| OpenRouter audio input | Rejected | It is an LLM gateway. Using it for ASR would add token cost to every utterance, against the owner's cost principle. |

### B.2 Flow
Diagram: [diagrams/voice-input-lifecycle.mmd](diagrams/voice-input-lifecycle.mmd).

1. Mic button → `navigator.mediaDevices.getUserMedia({audio: true})` (needs `microphone=(self)`, D-05).
2. `MediaRecorder` with the first supported mime of `audio/webm;codecs=opus`, `audio/mp4`. Hard stop at
   30 s; the elapsed time is shown, and a warning appears from 25 s.
3. Stop → blob (≤ 1 MB, else `CAPTURE_FAILED/too_long`) → `apiRaw('/admin/assistant/transcribe',
   {method:'POST', body: FormData})`.
4. Server: auth, feature gate, per-user rate limit (60/h), size/mime check → Sarvam REST with
   `model=saaras:v3`, `language_code` = the user's chosen language or `unknown`, `mode=codemix` if the
   chosen language is "Mixed", else `transcribe` (decision DP-03 — default mixed or transcribe; evaluate
   in G-KK-ASR) → `{text, language, duration_ms}`. The audio is held in memory only and dropped after the
   call. The text is not stored here.
5. Client puts the text **into the composer**, places the cursor at the end, and shows the chip
   "From voice · Telugu · edit before sending". **No auto-send.**
6. The user edits (optional) and presses Send → `requests` with `input_mode="voice"` and
   `transcript_original` (= the text at step 5). Both are stored on the run (KK-FR-007).

No interim (partial) transcripts in v1: REST returns once. This removes the "submitting an incomplete
interim result" risk entirely.

### B.3 Failure states → UI copy (all keep typing available)
| State | Detection | Copy |
|---|---|---|
| PERMISSION_DENIED | `NotAllowedError` | "Microphone blocked. Allow it in the browser's site settings, or type instead." |
| NO_DEVICE | `NotFoundError` | "No microphone found." |
| CAPTURE_FAILED | `MediaRecorder` error / blob > 1 MB | "Recording failed. Try again." |
| NO_SPEECH | empty `transcript` or < 2 chars | "Didn't catch anything. Try again closer to the mic." |
| STT_FAILED | Sarvam 4xx/5xx | "Voice service unavailable. Type your request." |
| STT_TIMEOUT | > 10 s server-side | same as STT_FAILED |
| UNSUPPORTED_LANGUAGE | returned `language_code` not in the clinic's languages (te/hi/en-IN) | Text is inserted anyway, with the chip "Detected <lang> — please check" |
| CANCELLED | Esc / Cancel during recording or upload (`AbortController`) | Silently returns to idle |
| WIDGET_CLOSED | panel closed while recording | Tracks are stopped, audio discarded |
| SESSION_EXPIRED | 401 from transcribe | Existing login modal; the audio is discarded |

The mic track is **always** stopped (`track.stop()`) on every exit path, so the browser's recording
indicator turns off.

### B.4 Multilingual
- Languages offered in the widget language menu: **Auto, Telugu, Hindi, English, Mixed** (the clinic
  languages used in the codebase: te/hi/en). Default: Auto.
- The original script is kept: Telugu script stays Telugu. Rules match on the original text **and** on a
  transliteration-normalised copy (the voice lexicon helpers, to verify). There is no forced translation
  to English.
- Claims: only te-IN, hi-IN and en-IN plus code-mix are in scope. Accuracy is unverified until G-KK-ASR
  runs the 60-utterance test set (20 per language, 10 of them code-mixed) recorded on a real laptop
  mic. Pass bar: ≥ 90 % utterances where the entities (doctor name, date, action word) come out right
  after **zero** edits, and ≥ 98 % after the user's edits are routed correctly.

### B.5 Privacy
| Item | Decision |
|---|---|
| Raw audio | Never written to disk, DB or logs. Held in request memory, forwarded to Sarvam, then dropped. |
| Processor | Sarvam AI (already the voice-receptionist processor). The DPDP notice and DPA must name staff dictation as a purpose — **owner action** (DP-07). |
| Retained | The final submitted text and the original transcript, inside `assistant_runs` (90-day retention). Unsubmitted transcripts are never stored. |
| Logs | Duration, mime, size, latency, language, status. **Never** the transcript. |
| Disclosure | First use shows a one-time inline notice (stored per browser in `localStorage`): "Voice is sent to our speech provider to turn it into text. Audio isn't stored." |
| Limits | 30 s per recording; 60 transcriptions per user per hour. |
| Cost | Recorded in `ai_usage_ledger` with `task_type='assistant_stt'`, `provider='sarvam'`, and cost from Sarvam per-second pricing (rate to be confirmed, G-KK-ASR). |
