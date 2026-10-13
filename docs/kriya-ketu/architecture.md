# Kriya Ketu — Architecture

Parent: [spec.md](spec.md). Diagrams are Mermaid sources in [diagrams/](diagrams/). **Current state**
diagrams show only what exists at `d002b3b`. **Proposed** components are labelled *(proposed)* or *(new)*.

### Diagram index (all parsed successfully with Mermaid 11 on 2026-10-13; rendering not visually reviewed)
| # | Subject (brief §23) | File | State |
|---|---|---|---|
| 1 | Current-state architecture | [current-state.mmd](diagrams/current-state.mmd) | Current |
| 2 | Proposed architecture | [system-architecture.mmd](diagrams/system-architecture.mmd) | Proposed |
| 3 | Text request lifecycle | [request-lifecycle.mmd](diagrams/request-lifecycle.mmd) | Proposed |
| 4 | Voice → editable transcript | [voice-input-lifecycle.mmd](diagrams/voice-input-lifecycle.mmd) | Proposed |
| 5 | Deterministic router → OpenRouter fallback | [routing-fallback.mmd](diagrams/routing-fallback.mmd) | Proposed |
| 6 | Authorization and confirmation | [authorization-confirmation.mmd](diagrams/authorization-confirmation.mmd) | Proposed |
| 7 | Execution, verification, audit | [execution-verification-audit.mmd](diagrams/execution-verification-audit.mmd) | Proposed |
| 8 | WhatsApp/voice vs internal assistant boundaries | [assistant-boundaries.mmd](diagrams/assistant-boundaries.mmd) | Proposed |
| 9 | Failure and recovery | [failure-recovery.mmd](diagrams/failure-recovery.mmd) | Proposed |
| 10 | Frontend component hierarchy | [frontend-components.mmd](diagrams/frontend-components.mmd) | Proposed |
| + | Action state machine | [action-state-machine.mmd](diagrams/action-state-machine.mmd) | Proposed |
| + | Widget lifecycle | [widget-lifecycle.mmd](diagrams/widget-lifecycle.mmd) | Proposed |
| v2 | Leads → voice calls (PB-03) | [leads-to-calls.mmd](diagrams/leads-to-calls.mmd) | Proposed |
| v2 | Autonomy levels (ADR-006) | [autonomy-levels.mmd](diagrams/autonomy-levels.mmd) | Proposed |

**v2 note (2026-10-14).** Scope now covers every clinic-panel module ([action-registry-spec.md](action-registry-spec.md)),
with playbooks in `app/assistant/playbooks.py` and hand-off in `app/assistant/handoff.py` (both proposed). An
extra state guard applies to playbooks: `EXECUTING → NEEDS_CONFIRMATION` when a step's actual input differs from
the previewed input (only the remaining steps are re-confirmed; completed steps stay done and verified).
H-tier actions never reach the executor (`SEC-18`).

---

## 1. Current state (audited)

| Concern | What exists | Evidence |
|---|---|---|
| Web framework | FastAPI, one app, routers mounted in `app/main.py:363-391` | `app/main.py` |
| Clinic panel | One inlined file `admin/index.html` (19,678 lines). No build step. Page switching via `go(page)` (`:7980`), fetch helpers `api/apiPost/apiPut/apiPatch/apiDel/apiRaw` (`:8081`), `withScope()` adds `?clinic_id`, `toast()` (`:8155`), `confirmDialog()` (`:8219`), SVG sprite incl. `#kriya-mark` (`:1852`), "Clinical Depth" glass tokens (`:20-107`), light theme via `:root[data-theme="light"]` | `admin/index.html` |
| Owner console | `admin/platform.html`, Basic auth (`verify_owner_credentials`) | `app/routers/platform.py` |
| Auth | Session cookie `admin_session` (HttpOnly, SameSite=strict = CSRF control) or HTTP Basic. `verify_credentials` (`admin.py:551`) → `_enforce_opd_role_scope` → corporate-viewer and phlebotomist confinement | `app/routers/admin.py` |
| Tenant | `enforce_clinic_access` fails closed (KRIYA-TENANT-001); `TENANT_OWNED_TABLES`; AST lint `tests/test_lint_unscoped_queries.py` | `app/tenancy.py`, AGENTS.md |
| RBAC | `require_permission(perm)`, `ROLE_PRESETS`, `enforce_branch_scope`, `require_admin` | `app/services/permissions.py` |
| Feature gates | `has_feature` (plan + overrides, enterprise wildcard), `OPT_IN_FEATURES` (owner-only: `corporate_health`, `ai_receptionist`, `opd_enabled`), toggled via `PATCH /platform/clinics/{id}/features` | `app/services/tenant.py:660-760` |
| Audit | `log_admin_action` → `admin_audit_logs` (migration 014) | `admin.py:159` |
| LLM | `call_ai_gateway` (OpenRouter, `models:[primary,fallback]`, 2 attempts, `response_format` passthrough, admin spend cap, `ai_usage_ledger`). **No tool-calling use anywhere.** | `app/services/ai_gateway.py` |
| Deterministic NLU (voice) | `app/voice/nlu_rules.py` (intents + DENY/AFFIRM lexicon in te/hi/en), `app/voice/dates.py` (`parse_date`, `today_ist`), `app/voice/lexicon.py`, LLM only when rules miss (`nlu_llm.py`) | `app/voice/` |
| Verified writes (voice) | `KriyaTools._create` → existing services → `_read_back` + `_proof` trace | `app/voice/tools.py:285-420` |
| Speech | Sarvam **streaming** STT/TTS over WebSocket (8 kHz telephony), `get_providers()` | `app/voice/providers/` |
| Browser policy | `Permissions-Policy: microphone=()` and CSP `connect-src 'self'` on **every** response | `app/utils/security.py:387-403`, `app/main.py:55-70` |
| Scheduler | APScheduler + `distributed_job_lock`. `check_doctor_leaves` daily at 08:00 cancels, refunds and notifies | `app/services/scheduler.py:257,1240` |

Diagram: [diagrams/current-state.mmd](diagrams/current-state.mmd).

**No existing internal assistant, action registry, event bus or tool-execution framework was found.**
The closest reusable patterns are the voice receptionist's *rules → LLM fallback* order and its
*write → read-back proof* discipline. Kriya Ketu copies these **patterns**, but not the voice code
(different actor and authority, ADR-001).

---

## 2. Proposed architecture

Diagram: [diagrams/system-architecture.mmd](diagrams/system-architecture.mmd).

```
admin/index.html  ──(same-origin fetch, session cookie)──►  /admin/assistant/*  (proposed router)
  KetuWidget (proposed, inline)                                │
                                                               ▼
                                                   app/assistant/ (proposed package)
   ┌──────────────┬───────────────┬───────────────┬───────────────┬──────────────┬──────────────┐
   │ rules.py     │ resolve.py    │ llm.py        │ policy.py     │ executor.py  │ store.py     │
   │ L1 nav, L2   │ doctor/date/  │ L4 fallback,  │ perm/role/    │ state machine│ assistant_   │
   │ triggers,    │ patient/appt  │ 1 structured  │ feature/risk  │ in-process   │ runs (CAS)   │
   │ negation     │ resolvers     │ call          │ pre-check     │ dispatch +   │              │
   │              │               │               │               │ verify       │              │
   └──────────────┴───────────────┴───────────────┴───────────────┴──────┬───────┴──────────────┘
                                 registry.py (ActionSpec catalogue)       │ httpx.ASGITransport(app)
                                                                          ▼  same cookie / Basic header
                                              EXISTING /admin/* and /admin/opd/* endpoints (unchanged)
                                              → existing services → Supabase (via sb())
   stt.py ──► Sarvam REST speech-to-text (proposed; server-side; key never in browser)
```

### 2.1 Proposed package `app/assistant/` (mirrors the `app/voice/` layout convention)
| Module | Responsibility (logical name from the brief) |
|---|---|
| `registry.py` | `ActionRegistry`: the `ActionSpec` catalogue ([action-registry-spec.md](action-registry-spec.md)) |
| `rules.py` | `DeterministicIntentRouter`: L1 navigation, L2 trigger matching, negation/hypothetical/question detection, L3 missing-slot detection |
| `resolve.py` | Slot resolvers (doctor, date, patient, appointment, report). Reuses `app/voice/dates.py` |
| `llm.py` | `AssistantLLMProvider` / `OpenRouterAssistantProvider`: a thin call into the existing `call_ai_gateway` with `task_type="assistant_intent"` and a json_schema |
| `policy.py` | `ActionPolicyEngine`: permission (same semantics as `require_permission`), role, feature gate, branch, risk → confirmation requirement |
| `executor.py` | `ActionExecutor` + `ActionVerificationService`: state machine, in-process dispatch, verification, playbooks |
| `store.py` | `assistant_runs` read/write with CAS; idempotency by `client_request_id` |
| `stt.py` | `SpeechRecognitionProvider` (Sarvam REST implementation + fake for tests) |
| `help.py` | Static help index (how-to answers) |

Router: `app/routers/assistant.py` (proposed), `APIRouter(prefix="/admin/assistant")`, every route
`Depends(verify_credentials)`. Mounted in `app/main.py` next to `voice_admin`.

### 2.2 Why in-process dispatch of existing endpoints (summary of ADR-004)
`admin.py` keeps most business logic **inside route handlers** (e.g. `create_leave` does the
branch-doctor check, range expansion, insert and audit inline). Calling services directly would mean
copying that logic. Calling the handler functions directly would skip their `Depends(...)`. Dispatching
the **real HTTP route in-process** (`httpx.AsyncClient(transport=httpx.ASGITransport(app=app))`, already a
dependency, `httpx>=0.26`) with the caller's own cookie or Basic header means:

- `verify_credentials`, `_OPD_ROLE_DENIED_ROUTES` (path regex), `require_permission`,
  `enforce_clinic_access`, `enforce_branch_scope`, validation, the endpoint's own `log_admin_action`, CAS
  guards and error mapping **all run unchanged**;
- a permission revoked after preview is enforced at execution (KK-FR-021);
- there is no network hop and no new service.

Known side effects (handled):

| Effect | Handling |
|---|---|
| `request.client.host` inside the dispatched route is the transport's fake client, so the audit `ip_address` would be wrong | The executor sends `X-Kriya-Assistant-Run: <run_id>`; the real IP is stored in `assistant_runs.ip_address`. KK-P1-03 decides whether `log_admin_action` should read a trusted internal header (only accepted when the request came through the in-process transport; it cannot be set from outside because the transport is not exposed). |
| Middlewares run twice (outer + inner): correlation id, CORS and security headers | Harmless. The executor forwards `X-Correlation-ID` (read by `CorrelationIdMiddleware`, `app/main.py:40-52`), so inner and outer logs share one id. |
| A long write can exceed the dispatch timeout | Timeout 20 s → `UNKNOWN_OUTCOME` → verify (KK-FR-032). |
| Error bodies may contain raw `str(e)` (e.g. D-03) | The executor maps status codes to fixed messages and never forwards `detail` verbatim, except an allow-list of known 4xx validation messages. |

### 2.3 Data model (migration `108_kriya_ketu.sql`, proposed; 107 is taken by the doctor_leaves fix, so 108)
```sql title="migrations/108_kriya_ketu.sql (proposed — NOT applied)"
CREATE TABLE IF NOT EXISTS assistant_runs (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id           uuid NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
  user_id             uuid NULL,               -- clinic_admins.id; NULL for env super_admin
  username            text NOT NULL,
  role                text NOT NULL,
  client_request_id   uuid NOT NULL,
  input_mode          text NOT NULL CHECK (input_mode IN ('text','voice')),
  input_text          text NOT NULL CHECK (length(input_text) <= 1000),
  transcript_original text NULL CHECK (transcript_original IS NULL OR length(transcript_original) <= 1000),
  language            text NULL,
  route_layer         text NULL CHECK (route_layer IN ('nav','rules','form','llm','none')),
  action_id           text NULL,
  params              jsonb NOT NULL DEFAULT '{}',
  preview             jsonb NULL,
  plan_hash           text NULL,
  state               text NOT NULL,
  steps               jsonb NOT NULL DEFAULT '[]',
  result              jsonb NULL,
  error_code          text NULL,
  llm_tokens          int  NOT NULL DEFAULT 0,
  llm_cost_paise      int  NOT NULL DEFAULT 0,
  route_ms            int  NULL,
  ip_address          text NULL,
  expires_at          timestamptz NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (clinic_id, username, client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_assistant_runs_clinic_created ON assistant_runs (clinic_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_assistant_runs_active ON assistant_runs (clinic_id, username)
  WHERE state IN ('NEEDS_CLARIFICATION','NEEDS_CONFIRMATION','EXECUTING','VERIFYING','UNKNOWN_OUTCOME');
ALTER TABLE assistant_runs ENABLE ROW LEVEL SECURITY;   -- house style; service_role bypasses it (AGENTS.md §1.1)
```
Plus `migrations/rollback/108_down.sql` (`DROP TABLE assistant_runs`, safe: the table only holds
assistant metadata; domain data lives in the existing tables). `assistant_runs` is added to
`TENANT_OWNED_TABLES` in `app/tenancy.py`.

No other schema change. Speech audio is never stored. LLM cost also goes to the existing
`ai_usage_ledger` (`task_type='assistant_intent'` / `'assistant_stt'`).

### 2.4 HTTP contract (proposed)
| Method + path | Body | Returns |
|---|---|---|
| `POST /admin/assistant/requests` | `{client_request_id, text, input_mode, transcript_original?, language?, ui_context:{page}, reply_to_run_id?, slot_answers?}` (`extra="forbid"`) | `RunOut` |
| `POST /admin/assistant/runs/{run_id}/confirm` | `{plan_hash, typed_count?, ack_duplicate?}` | `RunOut` |
| `POST /admin/assistant/runs/{run_id}/cancel` | — | `RunOut` (only from non-executing states) |
| `GET  /admin/assistant/runs?active=1&limit=30` | — | `RunOut[]` for **this user** only |
| `POST /admin/assistant/transcribe` | multipart `audio` (browser `MediaRecorder` output: `audio/webm` or `audio/mp4`, ≤ 1 MB, ≤ 30 s), `language_hint?` | `{text, language, duration_ms}` (text is not stored) |
| `GET  /admin/me` (existing, extended) | — | adds `assistant_enabled`, `assistant_stt_enabled` |

`RunOut = {run_id, state, action_id?, route_layer, message, cards[], steps[], plan_hash?, expires_at?,
navigate?, error_code?}`. The card types are listed in [ux-design-spec.md](ux-design-spec.md) §6.

All routes use the `?clinic_id` scope convention, `enforce_clinic_access`, and return only fixed
messages on 5xx.

---

## 3. Request lifecycle

Diagram: [diagrams/request-lifecycle.mmd](diagrams/request-lifecycle.mmd) (text) and
[diagrams/routing-fallback.mmd](diagrams/routing-fallback.mmd) (layers).

1. **Accept.** Validate the body, check the feature gate, do the idempotent insert on
   `client_request_id` → `RECEIVED`.
2. **Screen.** Run `clinical_firewall.screen_message(text, lang)` (`app/services/clinical_firewall.py:376`,
   deterministic, reused read-only). A clinical request is answered with a link to
   the right page and the run becomes `REJECTED/clinical`.
3. **L1 nav.** Page-table match → `navigate` → `COMPLETED_VERIFIED` (R0).
4. **L2 rules.** Score triggers. Exactly one action above threshold → resolve slots. Negation found →
   `REJECTED/negated` (with a "did you mean the opposite?" chip). Hypothetical found → the action is
   swapped for its `*.impact` read.
5. **L3 form.** Missing or ambiguous slots → `NEEDS_CLARIFICATION` with a typed form card. The reply
   comes back as `slot_answers` with `reply_to_run_id` (no text parsing and no LLM).
6. **L4 LLM.** Only if steps 3–5 found zero or several actions. One `call_ai_gateway` with a json_schema
   (see [openrouter-and-speech-spec.md](openrouter-and-speech-spec.md) §A). The output goes back into
   step 4 or 5 as if a rule had matched, so the same resolvers and policy apply.
7. **Policy.** Permission, role, feature and branch pre-check → `AUTHORIZED` or `REJECTED/forbidden`.
8. **Preview.** For R≥1, run `preview()` (reads only), compute `plan_hash`, set `expires_at = now + 5 min`
   → `NEEDS_CONFIRMATION`. R0 → step 10.
9. **Confirm.** Load the run and CAS `NEEDS_CONFIRMATION → EXECUTING` **only if** it is not expired, the
   `plan_hash` matches a freshly recomputed preview, and the typed count matches for R3.
10. **Execute.** In-process dispatch of the action's endpoint(s). Playbooks run step by step and stop at
    the first failed write.
11. **Verify.** Run `verify()` → `COMPLETED_VERIFIED`, `FAILED`, `UNKNOWN_OUTCOME` or
    `PARTIALLY_COMPLETED`.
12. **Report.** Return `RunOut`, update the run row, and log a masked line.

---

## 4. Frontend integration (summary; full spec in [ux-design-spec.md](ux-design-spec.md))

- Inline in `admin/index.html` (house rule: the frontend is inlined; `admin/admin.js` is dead). There is
  no new library, no framework and no CDN (CSP blocks them anyway).
- One `<button id="ketuLauncher">` + one `<section id="ketuPanel" role="dialog">` appended once after
  the toast container. The panel's DOM is built lazily on first open.
- Uses the existing `apiPost`/`api`, `withScope`, `esc()`, `go()`, `toast()`, and tokens (`--surface`,
  `--border2`, `--rim`, `--brand-grad`, `--radius-xl`, `--blur`, `--motion-*`, `--ease-out`).
- Hidden when `/admin/me.assistant_enabled` is false, and for `body.corp-viewer` and `body.phleb-view`.
- Stacking (existing z-indexes found: glass overlay 450, nav drawer 500, confirm overlay 600, toast 700,
  scope banner 900, notification drawer 999, session-expired overlay 9999): launcher **540**, panel
  **560**, so toasts, confirms, the scope banner and session-expiry stay above it.

Component hierarchy: [diagrams/frontend-components.mmd](diagrams/frontend-components.mmd).

---

## 5. Action state machine: guards (backend-enforced)

Diagram: [diagrams/action-state-machine.mmd](diagrams/action-state-machine.mmd).

| From → To | Guard (all must hold) | Writer |
|---|---|---|
| ∅ → RECEIVED | Feature on, body valid, `(clinic, username, client_request_id)` new | `requests` |
| RECEIVED → NEEDS_CLARIFICATION | Action known, ≥ 1 required slot missing or ambiguous | router |
| RECEIVED → REJECTED | unsupported / negated / clinical / not_available / forbidden (pre-check) | router |
| RECEIVED → AUTHORIZED | Unique action, all slots resolved, policy pre-check passes | router |
| NEEDS_CLARIFICATION → RECEIVED | `reply_to_run_id` owned by the same user, run not expired (10 min) | `requests` |
| AUTHORIZED → EXECUTING | risk == R0 | executor |
| AUTHORIZED → NEEDS_CONFIRMATION | risk ≥ R1; preview built; plan_hash stored; expires_at set | executor |
| NEEDS_CONFIRMATION → EXECUTING | Same user; `now < expires_at`; recomputed hash == sent hash == stored hash; R3 typed count == len(affected); `ack_duplicate` when required; CAS on state | `confirm` |
| NEEDS_CONFIRMATION → NEEDS_CONFIRMATION | Recomputed hash ≠ stored hash → store the new preview and hash and reset expiry | `confirm` |
| NEEDS_CONFIRMATION → CANCELLED | Same user | `cancel` |
| NEEDS_CONFIRMATION → EXPIRED | `now ≥ expires_at` (checked lazily on every read/confirm) | any |
| EXECUTING → VERIFYING | Dispatch returned 2xx | executor |
| EXECUTING → FAILED | Dispatch returned 4xx (nothing written, by endpoint contract) | executor |
| EXECUTING → UNKNOWN_OUTCOME | Timeout / 5xx / connection error, or stuck > 2 min | executor / sweep |
| VERIFYING → COMPLETED_VERIFIED | `verify()` ok | executor |
| VERIFYING → UNKNOWN_OUTCOME | `verify()` mismatch or read failure | executor |
| UNKNOWN_OUTCOME → COMPLETED_VERIFIED / FAILED | Re-verify finds the expected / original state | executor (max 3 re-verifications) |
| UNKNOWN_OUTCOME → NEEDS_HUMAN | Still undecidable after 3 re-verifications | executor |
| EXECUTING → PARTIALLY_COMPLETED | Playbook: ≥ 1 write step done and a later one failed | executor |

---

## 6. Boundaries with the patient-facing agents

Diagram: [diagrams/assistant-boundaries.mmd](diagrams/assistant-boundaries.mmd).

- Kriya Ketu imports **nothing** from `app/services/conversation.py` or `app/voice/dialog.py`. It
  reuses only pure helpers: `app/voice/dates.py` and, if suitable, the lexicons in
  `app/voice/lexicon.py` / `nlu_rules.py`. Those are read-only imports with no state.
- The patient channels never import `app/assistant/`. A test asserts both directions
  (`tests/test_assistant_boundaries.py`).
- Different `task_type` values in `ai_usage_ledger`. The assistant is subject to the admin spend cap;
  patient chat stays exempt (unchanged `PATIENT_FACING_TASKS`).

---

## 7. Failure and recovery

Diagram: [diagrams/failure-recovery.mmd](diagrams/failure-recovery.mmd).

| Failure | Behaviour |
|---|---|
| OpenRouter down, timeout or 429 | `call_ai_gateway` already retries once. Then a deterministic "couldn't match" + suggestions. Typed rules still work. |
| Spend cap exceeded | `SpendCapExceededError` → the same deterministic reply, plus a note visible to admins. |
| Sarvam down | Mic shows "Voice input unavailable — type instead". Text continues. |
| Session expires mid-run | Server: the run continues (it is already authorized). Client: `apiFail` 401 shows the existing login modal. On re-login the widget restores runs via `GET runs?active=1`. Confirm after expiry → 401 → the run expires after 5 min, so nothing runs. |
| Worker restart mid-execution | The run is stuck in `EXECUTING` → the sweep (> 2 min) moves it to `UNKNOWN_OUTCOME` → verification. |
| Partial playbook | `PARTIALLY_COMPLETED` with per-step evidence. Completed steps are not rolled back. Any reversal is offered as a new, separately confirmed action (e.g. `doctor_leave.delete`). |
