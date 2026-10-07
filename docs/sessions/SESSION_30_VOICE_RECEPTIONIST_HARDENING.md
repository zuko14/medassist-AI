# Session 30 — Voice Receptionist Hardening (Post-Production Call Analysis)

**Date:** 2026-10-07  
**Branch:** `feat/ai-receptionist`  
**Scope:** Voice NLU accuracy, AI model retirement fix, dialog resilience, admin panel UX, filler/interruption handling  
**Migration:** None (code-only changes)

---

## 1. Summary

After deploying the AI Receptionist (Session 29 and prior), **8 real production calls** were analyzed from the database. **7 out of 8 calls were transferred to reception** — most within 3–5 seconds. This session identifies the four root causes and fixes all of them across 16 files (561 lines added, 79 removed).

### Root Causes Found

| # | Root Cause | Impact | Fix |
|---|---|---|---|
| 1 | **Retired AI model** — `google/gemini-2.0-flash-001` was retired by OpenRouter (HTTP 404). This was both the voice NLU primary and the platform-wide fallback model. | All 6 LLM backup attempts returned errors; NLU fell back to zero-confidence → transfer | Updated to `google/gemini-3.1-flash-lite` (primary) and `openai/gpt-4.1-mini` (fallback) |
| 2 | **Telugu filler words counted as misunderstandings** — "హలో", "హ్మ్", "ఓకే", "ఆ" each counted as a failed NLU turn. After 3 such, Kriya transferred. | Callers saying "hello?" 3 times at the start of a call were immediately transferred | Added `is_filler()` to policy.py and idle-turn patience in dialog.py |
| 3 | **Greeting cut off by caller** — Callers said "hello?" as the line connected, triggering barge-in. The greeting stopped and was never heard. | Callers never heard Kriya's greeting and thought the line was dead | Added `FILLER_OVER_SPEECH` detection in gateway.py — Kriya replays the cut-off response |
| 4 | **Confirmation and request phrases not recognised** — "ఓకే", "బుక్ చేయండి", "స్లాట్స్ ఖాళీ ఉన్నాయా?", "వేరే సమాచారం కావాలి" all returned `UNKNOWN` | Real callers who said "yes, book it" or asked about availability were not understood | Expanded lexicon with 40+ Telugu/Hindi phrases for AFFIRM, DENY, AVAIL, DOCTOR, INFO |

---

## 2. Files Changed

### 2.1 Backend — Voice NLU & Dialog

| File | Lines Changed | What Changed |
|---|---|---|
| [`app/config.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/config.py) | +12 −3 | Replaced retired `gemini-2.0-flash-001` with `gemini-3.1-flash-lite` (voice primary), added `voice_llm_fallback_model = openai/gpt-4.1-mini`, updated platform fallback to `gemini-2.5-flash-lite`, added `voice_stt_auto_language` flag |
| [`app/voice/nlu_rules.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/nlu_rules.py) | +36 −4 | **Version:** `nlu-rules-2026.10.07`. Added `DOCTOR`, `INFO` word groups. Expanded `AVAIL` with "స్లాట్", "ఖాళీ", "khali". Expanded `AFFIRM` with 20+ Telugu/Hindi variants ("ఓకే", "అలాగే", "బుక్ చేయండి", "ज़रूर", "go ahead"). Expanded `DENY` with "వద్దండి", "అక్కర్లేదు". Added `SHORT_AFFIRM` frozenset for bare single-word affirmations ("ఆ", "హా", "హాఁ", "aa", "haa", etc.) that are affirmative only when the entire utterance. Added secondary intent detection for "doctor slots free?" and "information" requests. |
| [`app/voice/nlu_llm.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/nlu_llm.py) | +41 −10 | **Version:** `voice-nlu-prompt-2026.10.07`. LLM prompt now receives `EXPECTING` context (what Kriya just asked), so a bare "ఆ" after "shall I book?" is classified correctly. Added `_REMAP` for intent aliases (`DEPARTMENT_INFORMATION` → `DOCTOR_AVAILABILITY`). Removed `EMERGENCY` and `CLINICAL_QUERY` from LLM-allowed intents (those are handled by the zero-LLM safety screen). Added `info_topic` entity extraction. Passes `expect` state to `_validate()` so confirmation-stage AFFIRM is only from rules (not hallucinated by LLM). Now passes `fallback_model` to `call_ai_gateway`. |
| [`app/voice/dialog.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/dialog.py) | +60 −8 | **Version:** `dialog-2026.10.07`. Added `MAX_IDLE = 2` — filler turns ("hello?", "hmm") are answered patiently before counting as misses. Added `offer_self_help` flow — first-time "connect me to reception" gets a one-time "I can help you myself" offer. Added bare-date opening handling ("tomorrow evening" → `BOOK_APPOINTMENT`). Added `_wf_info` topic prompt: "I need information" now asks "which: timings, address, or fees?". Outbound greeting now accepts `name` and `slots` for personalized lead calls. Language detection auto-confirms on first turn if ≥2 words. |
| [`app/voice/policy.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/policy.py) | +14 −4 | **Version:** `policy-2026.10.07`. Extracted `is_filler()` function — detects "hello?", "hmm", "హలో హలో" etc. as line-checking sounds rather than requests. Cleaned up `FILLERS` frozenset: removed "ఆ" (it means "yes"), added "హ్మ్", "ఉమ్", "హలో హలో". |
| [`app/voice/responses.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/responses.py) | +25 −4 | **Version:** `templates-2026.10.07`. Added 4 new response templates in te/en/hi: `listening` ("I'm listening, how can I help?"), `offer_self_help`, `ask_info_topic` ("Which info: timings, address, or fees?"), `who` (name honorific: "గారు" / "जी"). Outbound greeting now includes `{who}` for personalized name ("Namaste Ravi garu"). |
| [`app/voice/session.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/session.py) | +33 −3 | Added `_lead_context()` — fetches caller's name from WhatsApp profile and resolves their interest to a clinic department, so the outbound greeting includes both. Now passes `expect` state to `understand_llm()` for context-aware classification. |
| [`app/voice/gateway.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/gateway.py) | +20 −0 | Added **interruption replay** logic: if a caller says "hello?" over Kriya's speech (barge-in), and it's a filler, Kriya re-speaks the interrupted response instead of dropping it. Logs `FILLER_OVER_SPEECH` event. Bounded to 2 replays to prevent loops. Tracks `self.interrupted` and `self.resumes` on `CallRunner`. |
| [`app/voice/providers/sarvam.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/providers/sarvam.py) | +1 −1 | Minor fix: respects `voice_stt_auto_language` config flag. |
| [`app/services/ai_gateway.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/ai_gateway.py) | +1 −1 | Changed hardcoded fallback default from retired `gemini-2.0-flash-001` to `gemini-2.5-flash-lite`. |

### 2.2 Frontend — Admin Panel

| File | Lines Changed | What Changed |
|---|---|---|
| [`admin/index.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/index.html) | +135 −40 | **AI Receptionist tab restyled** — All raw inputs now have proper `<label>` elements. Settings split into 3 logical sections: "Voice and language", "Reception hours", "Outbound calls". Added outbound call initiation form ("Ask Kriya to call a lead") with phone + interest inputs. Lexicon management now uses a proper table with headers (Type, Callers say, Means). Added cost display with "Rates not set" notes where applicable. Test console reformatted to chat-bubble view. All styling matched to existing Leads page design language. |

### 2.3 Tests

| File | Lines Changed | What Changed |
|---|---|---|
| [`tests/voice/test_dialog.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/voice/test_dialog.py) | +81 −2 | Tests for: filler patience (hello/hmm don't count as misses), offer-self-help before transferring to human, info topic prompting, outbound name + department pre-fill. |
| [`tests/voice/test_gateway.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/voice/test_gateway.py) | +57 −0 | Tests for: `FILLER_OVER_SPEECH` replay (greeting interrupted by "hello?" is re-spoken), replay bounded to 2 max. |
| [`tests/voice/test_io_layer.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/voice/test_io_layer.py) | +85 −2 | Tests for: expanded AFFIRM recognition ("ఓకే", "బుక్ చేయండి"), SHORT_AFFIRM whole-utterance matching, `is_filler()` function, AVAIL/DOCTOR/INFO intent detection from Telugu phrases. |

### 2.4 Documentation

| File | Lines Changed | What Changed |
|---|---|---|
| [`docs/agent-context/06-AI-SYSTEM.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/06-AI-SYSTEM.md) | +15 −3 | Updated voice model references to reflect the new primary/fallback models. |
| [`docs/superpowers/plans/2026-10-06-ai-receptionist/STATUS.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/superpowers/plans/2026-10-06-ai-receptionist/STATUS.md) | +25 −0 | Added Phase 2 hardening status entries. |
| [`docs/voice/eval-reports/2026-10-07-synthetic.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/voice/eval-reports/2026-10-07-synthetic.md) | NEW | 19,120-case synthetic NLU benchmark results across Telugu, Hindi, and English. |

---

## 3. Intent & Design Decisions

### 3.1 Why `google/gemini-3.1-flash-lite` as voice primary

- OpenRouter retired `gemini-2.0-flash-001` → all voice LLM calls failed silently with HTTP 404.
- Tested `gemini-3.1-flash-lite` against the 19,120-case benchmark: best Telugu/Hindi accuracy among sub-3s latency models.
- `openai/gpt-4.1-mini` as fallback: different provider ensures one outage doesn't take down both.

### 3.2 Why filler patience instead of ignoring fillers

- Simply ignoring "hello?" would lose genuine one-word requests.
- `MAX_IDLE = 2` allows two filler turns with a gentle reprompt ("I'm listening, how can I help?") before counting as a miss. This handles the common pattern of callers checking the line while preserving detection of actual misunderstandings.

### 3.3 Why replay interrupted speech

- Production calls showed callers saying "hello?" exactly as Kriya started speaking. Barge-in detection cut the greeting. Callers never heard what Kriya was saying.
- `FILLER_OVER_SPEECH` detection: if what the caller said during the barge-in is a filler, Kriya re-speaks the interrupted response. Max 2 replays prevents infinite loops.

### 3.4 Why `SHORT_AFFIRM` is separate from the AFFIRM word group

- "ఆ" (aa) means "yes" in Telugu but also means "that" (as in "ఆ డాక్టర్" = "that doctor"). If added to the AFFIRM word group, "ఆ డాక్టర్" would match as affirmation + doctor.
- `SHORT_AFFIRM` only matches when the entire cleaned utterance is one of these short phrases — "ఆ" alone = yes, but "ఆ డాక్టర్" is not caught by SHORT_AFFIRM and is correctly parsed as a doctor reference.

### 3.5 Why LLM receives `EXPECTING` context

- A bare "okay" or "ఆ" after "Shall I book Dr. Arjun at 6 PM?" is a confirmation. Without context, the LLM classified it as `UNKNOWN`.
- The prompt now includes what Kriya last asked (e.g., "a yes/no confirmation"), so short replies are classified accurately.

### 3.6 Why `offer_self_help` before transferring to reception

- Some callers say "connect me to reception" reflexively. Production logs showed callers who wanted bookings but asked for reception out of habit.
- First-time request: Kriya offers "I can book your appointment myself. Tell me what you need, or shall I connect you?" If they say no, transfer happens immediately.
- Second request or after any misunderstanding: instant transfer, no second offer.

---

## 4. Test Results

```
4,051 tests passed (including 19,120-case NLU benchmark)
0 failures
```

### Key Test Categories
- **NLU rules:** Expanded AFFIRM, DENY, AVAIL, DOCTOR, INFO recognition across Telugu, Hindi, English
- **Dialog filler patience:** Hello/hmm don't count as misses for first 2 turns
- **Offer-self-help:** Human agent request gets one-time counter-offer
- **Gateway replay:** Interrupted greeting re-spoken when barge-in is a filler
- **Info topic prompt:** "I need information" prompts for which topic
- **Outbound personalisation:** Lead calls use caller name and pre-fill department from interest

---

## 5. Configuration Notes

### No New Environment Variables Required

All new model defaults are baked into [`app/config.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/config.py):

| Setting | Default | Env Override |
|---|---|---|
| `voice_llm_model` | `google/gemini-3.1-flash-lite` | `VOICE_LLM_MODEL` |
| `voice_llm_fallback_model` | `openai/gpt-4.1-mini` | `VOICE_LLM_FALLBACK_MODEL` |
| `openrouter_fallback_model` | `google/gemini-2.5-flash-lite` | `OPENROUTER_FALLBACK_MODEL` |
| `voice_stt_auto_language` | `True` | `VOICE_STT_AUTO_LANGUAGE` |

> **⚠️ Important:** If `VOICE_LLM_MODEL` or `OPENROUTER_FALLBACK_MODEL` was previously set to `google/gemini-2.0-flash-001` in Render, **delete those entries** — they would override the new working defaults.

---

## 6. Post-Deployment Verification

After pushing and deploying:

1. **Call the reception number** — Kriya should greet in Telugu, not transfer immediately.
2. **Say "hello?" twice** — Kriya should respond "I'm listening, how can I help?" (not transfer).
3. **Say "ఓకే" after an offer** — Should be recognised as affirmation.
4. **Check `ai_usage_ledger`** in Supabase for `task_type = 'voice_nlu'` — confirms LLM backup is responding.
5. **Check voice_calls table** for `status = 'completed'` — confirms calls are completing instead of transferring.
