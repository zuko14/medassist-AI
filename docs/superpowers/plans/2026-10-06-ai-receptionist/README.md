# Kriya AI Receptionist (Voice) — Implementation Plan

> **For agentic workers:** read `EXECUTOR.md` first, then execute tasks T0–T11 in
> order, recording proof in `STATUS.md`. Code comes ONLY from `payload.patch`.

**Goal:** an owner-enabled, per-clinic AI phone receptionist that answers a
hospital's existing number in Telugu / Hindi / English, executes real
workflows (book, pay-link, cancel, reschedule, status, reports, fees, lab
tests, info, callbacks, human handoff), verifies every action by reading it
back, and gives the clinic and the platform owner a full control room
(live calls, transcripts, action trace, proof, analytics, cost/budget).

**Architecture:** Exotel (telephony) streams caller audio over a WebSocket to
Kriya; Sarvam converts speech⇄text; a deterministic engine (`app/voice/`)
understands, decides and acts through typed, tenant-scoped tools that reuse the
existing WhatsApp-proven services; an LLM is used only as a validated fallback
classifier and never writes speech or chooses actions.

**Tech:** FastAPI (existing app), Supabase Postgres (migration 098),
Sarvam STT `saaras` + TTS `bulbul:v3` over WebSocket, Exotel Voicebot applet,
OpenRouter via existing `ai_gateway`, APScheduler (existing).

**Spec:** the user's "KRIYA AI — UNIVERSAL INDIA AI RECEPTIONIST" prompt
(2026-10-06 session) plus this file's decisions. Where this plan narrows the
spec, the section "Deliberate scope decisions" says so explicitly.

---

## 1. Verified facts this design relies on

| Fact | Source (checked 2026-10-06) |
|---|---|
| Exotel Voicebot (bidirectional) sends `connected/start/media/dtmf/mark/stop` JSON; `start.start` has `call_sid, from, to, custom_parameters, media_format.sample_rate`; bot sends `media`, `mark`, `clear`. Audio = base64 16-bit LE mono PCM, 8 kHz default; chunks multiple of 320 B, ≥3.2 KB, ≤100 KB. Up to 3 custom query params on the WSS URL. | developer.exotel.com/docs/agentstream/stream-voicebot-applet |
| Passthru applet: HTTP 200 → branch A, 302 → branch B. | developer.exotel.com/docs/voice-v1/applets/passthru |
| Outbound: `POST /v1/Accounts/{sid}/Calls/connect` with `From, CallerId, Url (flow), CallType=trans, StatusCallback, CustomField`. | developer.exotel.com/docs/voice-v1/api-reference/outgoing-call-to-flow |
| Sarvam STT WS `wss://api.sarvam.ai/speech-to-text/ws`, header `Api-Subscription-Key`, query `language-code, model (saaras:v3/v4), mode, sample_rate (8000 ok), input_audio_codec (pcm_s16le), vad_signals`; responses `type=data` (transcript, language_code) and `type=events` (START_SPEECH/END_SPEECH). | docs.sarvam.ai/api-reference/speech-to-text/transcribe/ws |
| Sarvam TTS WS `wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true`; `config` (language_code, speaker, pace 0.5–2.0, speech_sample_rate 8000, output_audio_codec linear16, dict_id), `text`, `flush`; responses `audio` and `event final`. | docs.sarvam.ai/api-reference/text-to-speech/stream |
| ⇒ No resampling/transcoding needed: Exotel 8 kHz PCM ⇄ Sarvam 8 kHz PCM. | derived |
| Production Python is 3.11 (`Dockerfile: python:3.11-slim`); venv is 3.12. | repo |
| Patients' phones are stored `+91XXXXXXXXXX` (`validators.normalize_phone`); Exotel may send `0XXXXXXXXXX` ⇒ `app/voice/phone.to_e164`. | repo |
| `lab_reports` columns are `report_name`, `status` (`pending/needs_review/pending_retry/sent/failed`), `file_path` — NOT `test_name` as docs/agent-context/03 says. | migrations 001/041, lab_reports.py |
| `clinics.config.emergency_number` is the clinic emergency key. | tenant.get_clinic_contact |
| `whatsapp_service.send_text` returns **False** (does not raise) outside the 24 h window. | whatsapp.py |

Items still to confirm against the live services are human gates (§6), not assumptions buried in code.

## 2. How it fits the existing system (reuse map)

| Need | Reused as-is | New code |
|---|---|---|
| Owner enables per clinic | `OPT_IN_FEATURES` + `PATCH /platform/clinics/{id}/features` | `ai_receptionist_enabled()` |
| Tenant isolation | `enforce_clinic_access`, `TENANT_OWNED_TABLES`, linters | 5 tables registered |
| Slots / leave / holidays | `database.get_available_slots`, `get_doctors` | — |
| Paid booking + link + auto-confirm on payment | `payment_service.create_booking_with_payment` → Razorpay webhook → `_notify_payment_confirmed` | template fallback when 24 h window closed |
| Free booking | `database.book_appointment` | — |
| Cancel + refund | `ConversationManager._cancel_with_refund` (kept in place: a test patches its module) | — |
| Report resend | `LabReportService().resend_report` (original number only) | — |
| Hospital info | `faq_engine.answer` | `_speakable()` |
| Lab search | `ConversationManager._match_lab_tests` | — |
| Safety | `clinical_firewall.screen_message`, `ai_engine.EMERGENCY_KEYWORDS` | spoken te/hi phrases + pair rule |
| LLM + cost ledger | `ai_gateway.call_ai_gateway` | `max_attempts`, voice exempt from admin cap |
| Leads | `analytics_events lead_interest`, leads consent rules (092/093) | outbound queue |

## 3. Using the hospital's EXISTING number (multi-branch)

1. Owner buys one Exotel virtual number (Exophone) per branch / desk line.
2. The hospital forwards its published number (the one on Google) to that
   Exophone at its telecom provider (unconditional, or on busy/no-answer if they
   want humans first). The public number does not change.
3. Owner maps Exophone → clinic (+ branch, reception number) in the owner panel
   (`/platform/voice/numbers`). Tenant is resolved ONLY from this table
   (`voice_numbers.exophone` is UNIQUE): one number can never reach two tenants.
4. Exotel flow on that Exophone: **Voicebot** (`wss://<host>/voice/exotel/stream?k=<VOICE_STREAM_TOKEN>`)
   → **Passthru** (`https://<host>/voice/exotel/passthru?k=<token>`) →
   200: **Connect** to the reception number; 302: **Hangup**.
   Kriya returns 200 whenever the AI did not finish cleanly (transfer asked,
   emergency, budget/capacity/feature refusal, crash, unknown call) — an outage
   means "caller reaches reception", never silence.
5. Outbound calls show the Exophone as caller ID (not the hospital's published
   number) unless the hospital later moves its number to Exotel/SIP.

## 4. Deliberate scope decisions (what the spec asked vs. what ships in v1)

| Spec item | v1 decision | Why |
|---|---|---|
| 11+ languages | Telugu, Hindi, English (+code-mix, romanised). Other scripts are detected; the caller is offered te/hi/en or a human. | Speech is template-only for zero fabrication; each language needs a native-reviewed template pack + golden tests (Phase 2 recipe in §8). |
| LLM conversational brain | Deterministic rules first; LLM only as a validated classifier fallback. Speech never from an LLM. | Eliminates hallucinated bookings/prices; works when the LLM is down; cheaper. |
| "Don't say I'm an AI" | Greets as "Kriya from <hospital>"; never volunteers "AI"; if asked directly, answers truthfully ("the hospital's automated assistant") and offers staff. | Denying being automated would be deceptive to patients. |
| Reschedule | Autonomous for unpaid bookings (book new → verify → cancel old). Paid bookings → staff. | Moving money needs a human until refund+rebook is proven. |
| Shadow mode | Not built. Use test console + one-branch canary instead. | Needs Exotel unidirectional-stream semantics verified first. |
| Workflow builder UI | Not built; workflows are code-reviewed state machines. | A UI that changes transactional logic is a safety risk; revisit after v1 data. |
| HMS/LIS connectors (MocDoc booking etc.) | Kriya's own appointment tables are the system of record (as WhatsApp today). | Same source WhatsApp uses; external HMS write-back is a separate project. |
| 98% reliability claim | Measured, never claimed: synthetic floor (100% rules, circular), then recorded-call and canary benchmarks are gates (§6). | Spec §77. |

## 5. Global constraints (apply to every task)

- Python code must compile on **3.11** (no 3.12-only f-strings). T11 checks.
- Every tenant-table query has `.eq("clinic_id", …)` or an `# unscoped: <ALLOWED_REASON>` annotation; both linters stay green, ratchets unchanged.
- No caller-facing sentence is generated by an LLM; all speech comes from `app/voice/responses.py`.
- No "booked/cancelled/sent" is spoken unless the tool returned `verified=True` from a read-back.
- Voice is dormant unless `VOICE_ENABLED=true` AND owner opt-in AND a mapped Exophone.
- IST everywhere; phones E.164 `+91…`; logs mask phones (`phone.mask`).

## 6. Human go-live gates (cannot be done by an agent)

| Gate | Who | Pass condition |
|---|---|---|
| G-MIGRATE-PROD | Owner | Migration 098 applied to Supabase via `python scripts/migrate.py` (DATABASE_URL) or SQL editor + checksum row recorded; `tests/test_migration_098_voice.py` already green locally. |
| G-META-TEMPLATE | Owner | UTILITY template `kriya_payment_link` approved in each clinic's WABA. Body (8 vars): `Hello {{1}}, your slot for {{2}} on {{3}} at {{4}} is held for {{5}} minutes. Pay Rs {{6}} to confirm it: {{7}} - {{8}}`. `appointment_confirmation` must also be approved. |
| G-SARVAM-LIVE | Engineer | `SARVAM_API_KEY=… python scripts/voice_provider_smoke.py` exits 0 (proves wire formats). |
| G-EXOTEL-FLOW | Owner | Flow configured per §3.4; a real call reaches the stream (`voice_calls` row appears); killing the server makes a test call land at reception; Passthru query param name for the call id confirmed as `CallSid` (else update `gateway.exotel_passthru`). |
| G-LANG-REVIEW | Native speakers | Every te/hi template in `responses.py` reviewed for natural spoken style. |
| G-MED-REVIEW | Hospital medical lead | `safety.py` emergency phrases + responses approved. |
| G-RATES | Owner | `VOICE_*_PAISE_*` set from real invoices; owner sets a monthly budget per clinic. |
| G-PILOT | Clinic | 50 test-console conversations + 30 real recorded calls per language reviewed in the call-detail view; ≥98 % workflow success on that set, zero false confirmations, zero safety misses. |
| G-CANARY | Owner | One branch number for 1 week; daily review of Failures/“top phrases”; then expand. |

## 7. Tasks (executor applies these; details + expected counts in STATUS.md)

Each task: `git apply --check --include=<paths> payload.patch && git apply --include=<paths> payload.patch`, then the Verify commands. Paths per task are listed in STATUS.md.

| Task | What | Depends |
|---|---|---|
| T0 | Branch `feat/ai-receptionist` from `main`; confirm HEAD descends from `d263bb4`; record baseline failing tests. | — |
| T1 | Migration 098 + rollback + real-Postgres test. | T0 |
| T2 | Tenancy registry, `ai_receptionist` opt-in, `/admin/me` flag, owner features guard, settings. | T1 |
| T3 | `ai_gateway`: `max_attempts`, voice exempt from admin cap. | T2 |
| T4 | Payment confirmation: template fallback when the 24 h window is closed (bug fix for phone bookings). | T2 |
| T5 | Pure voice core (dates, lexicon, NLU rules, dialog FSM, responses, Exotel codec, policy). | T2 |
| T6 | Voice I/O: store, safety, LLM fallback, tools, session. | T3, T5 |
| T7 | Telephony: providers (Sarvam + fake), gateway, outbound queue. | T6 |
| T8 | Routers `/admin/voice`, `/platform/voice`, `main.py` mounting, scheduler jobs. | T7 |
| T9 | Admin + owner panel UI, `VOICE_VIEW`/`VOICE_MANAGE` permissions. | T8 |
| T10 | Eval harness + Sarvam smoke script + first synthetic report. | T5 |
| T11 | Full regression vs baseline, Python 3.11 compile, browser QA, PR. | T1–T10 |

## 8. Phase 2 backlog (not in the patch; do not start without a new plan)

1. More languages: add `ta/kn/ml/mr/bn/gu/pa/od` packs to `responses.T` + lexicon synonyms + `dates` words, each with golden tests and native review; extend `nlu_rules.SUPPORTED_LANGS`.
2. Persistent TTS connection per call (today one WS per utterance ≈ +200–400 ms).
3. Shadow mode (Exotel unidirectional Stream applet) and automatic canary rollback.
4. Payment-link resend and paid-booking reschedule.
5. Home sample collection and insurance questions by voice.
6. Load tests at 100/250 concurrent calls with the fake providers (`loadtest/`), failure-injection matrix (STT/TTS/LLM/DB/Razorpay timeouts).
7. Daily/weekly voice reports and alerting on failure spikes.
8. Docs set (`docs/voice-architecture.md`, runbook, incident response…).

## 9. Review focus (failure modes most likely to bite; each has a test)

1. Phone booking + paid link with closed WhatsApp window → confirmation lost. Test: `test_voice_payment_confirmation_template.py`.
2. A patient name that matches a doctor ("Lakshmi") re-planning the booking. Test: `test_two_options_then_name_for_mother_then_confirm`.
3. Short Hindi "जी" matching inside "कार्डियोलॉजी" as a yes. Test: `test_spec_73_booking_utterances` (hi case).
4. Kriya down / crashed mid-call → caller stranded. Test: `test_passthru_always_fails_safe_to_reception`.
5. Caller asks for someone else's report / cancels someone else's booking. Tests: `test_unknown_caller_cannot_get_others_report`, `test_cancel_refuses_a_booking_on_another_number`.
