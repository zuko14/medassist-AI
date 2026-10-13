# Kriya Ketu — File-Impact Matrix

Parent: [spec.md](spec.md) · Tasks: [implementation-plan.md](implementation-plan.md).
Every *Existing* path was verified at `d002b3b` (2026-10-13). *Proposed new* files do not exist yet.
Each block lists the required fields in a fixed order:
**Status · Current · Change · Reason · Deps · API · DB · Security · Regression risk · Verification · Task**.

---

## 1. Files that must stay untouched

| Path | Why |
|---|---|
| `app/services/conversation.py`, `app/routers/webhook.py`, `app/services/message_queue.py` | Patient WhatsApp FSM and ingest (ADR-001). The assistant calls none of it. |
| `app/voice/**` except `outbound.py` | Voice receptionist. Only **read-only imports** of pure helpers (`dates.py`, `lexicon.match_doctors`, lexicon data from `nlu_rules.py`) are allowed. No edits. (v2: `app/voice/outbound.py` gets two small, separately approved changes — see §9.) |
| `app/services/clinical_firewall.py` | Reused read-only (`screen_message`). |
| `app/services/payment.py`, `app/services/opd*.py`, `app/database.py` | Domain logic is reached only through the existing endpoints. |
| `app/routers/opd.py`, `voice_admin.py`, `platform.py`, `home_collection.py`, `corporate_health.py` | Dispatched as-is. |
| `admin/platform.html`, `admin/queue-display.html`, `admin/admin.js` (dead) | Out of scope for v1. |
| `migrations/001..106*.sql` | Applied history. Never edited. |
| `requirements.txt`, `requirements-dev.txt`, `Dockerfile`, `build.sh` | No new dependency (`httpx` already pinned `>=0.26,<0.29`, which has `ASGITransport`). |

---

## 2. Backend: routing, API and package (new)

**`app/routers/assistant.py`** · Proposed new · — · The `/admin/assistant/*` routes (requests, runs,
confirm, cancel, transcribe) · Entry point for the widget · `verify_credentials`,
`enforce_clinic_access`, `app/assistant/*` · New endpoints (architecture §2.4) · Writes `assistant_runs`
· Session auth, BOLA by username, `extra="forbid"`, rate limits · A new router could shadow `/admin/*`
paths, so the prefix must stay `/admin/assistant` · `tests/test_assistant_runs.py`, the adversarial
matrix · KK-P1-03, KK-P3-02

**`app/assistant/__init__.py`, `registry.py`, `help.py`** · Proposed new · — · ActionSpec catalogue and
help index · Single source of actions · — · None · None · Permission drift risk → drift test · Low ·
`tests/test_assistant_registry.py` · KK-P1-04

**`app/assistant/rules.py`, `resolve.py`** · Proposed new · — · L1–L3 deterministic routing and
resolvers · Cost principle · `app/voice/dates.py` (read-only import), `nlu_rules.py` lexicon (data),
`difflib` · None · Reads via the executor (scoped GETs) · Never resolves outside scope · A wrong match
could pick the wrong doctor → never auto-pick on ambiguity · `tests/test_assistant_rules.py` + corpus ·
KK-P1-06

**`app/assistant/llm.py`** · Proposed new · — · L4 structured-output call · Fallback only ·
`ai_gateway.call_ai_gateway` · None · `ai_usage_ledger` rows (existing table) · No record data sent;
enum-limited output · Spend cap shared with other admin AI features (insights summary etc.) → budget
consumption is visible in `/admin/ai/usage` · `tests/test_assistant_llm.py`, G-KK-LLM · KK-P1-07

**`app/assistant/policy.py`** · Proposed new · — · Pre-check, risk, plan_hash, expiry · UX-level gate +
confirmation contract · `permissions.py`, `tenant.py` gates · None · None · Must mirror, never replace,
endpoint checks · False "allowed" is harmless (the endpoint refuses); false "denied" hides a capability
→ matrix test · `tests/test_assistant_policy.py` · KK-P1-08

**`app/assistant/executor.py`** · Proposed new · — · State machine, in-process dispatch, verify,
playbooks · ADR-004 · `httpx.ASGITransport(app)`, `app.main.app` (lazy import to avoid a cycle) ·
Consumes the existing endpoints unchanged · `assistant_runs` CAS · Forwards only the caller's own
credentials · Double middleware execution (harmless); audit IP shows the transport client (T-24) ·
`tests/test_assistant_executor.py` · KK-P1-05

**`app/assistant/store.py`** · Proposed new · — · `assistant_runs` CRUD with CAS + idempotency ·
Persistence · `app/database.sb`, `scoped_query` · — · New table · Tenant + username scoping · Lint ·
`tests/test_assistant_runs.py` · KK-P1-03

**`app/assistant/stt.py`** · Proposed new · — · Sarvam REST transcription provider + fake · ADR-003 ·
`settings.sarvam_api_key`, the `websockets` dependency is **not** needed (it uses `httpx`) · Used by
`/transcribe` · `ai_usage_ledger` · Key server-side; no audio persisted · None to the voice receptionist
(separate code path) · `tests/test_assistant_stt.py` · KK-P3-02

## 3. Backend: existing files that change

**`app/main.py`** · Existing · Mounts routers (`:363-391`), middlewares · Add
`app.include_router(assistant.router)` beside `voice_admin` · Expose the API · — · New paths only · None
· None · An import error at boot would take down the app → covered by the existing boot tests
(`tests/test_production_launch_gates.py`) · Boot test + `test_multi_worker_smoke.py` · KK-P1-03

**`app/routers/admin.py`** · Existing · Admin routes · (a) `GET /admin/me` (`:841`) adds
`assistant_enabled`, `assistant_stt_enabled`; (b) KK-P0-02 hardens `/lab-reports/{id}/resend` (`:5672`);
(c) KK-P0-03 audits the cancel (`:5390`); (d) KK-P0-04 revokes sessions in `update_staff` (`:1337`);
(e) KK-P0-01 near-date trigger in `create_leave` (`:5145`) · Feature visibility + defect fixes · Panel,
`permissions.py` · `/admin/me` gains fields (additive). (b) may change a 200 to a 403 for roles without
`REPORTS_RESOLVE`, which the Lab Reports page calls at `admin/index.html:12178`, so DP-02 is needed ·
None · Tightens auth (b), adds audit (c), revokes stale sessions (d) · (b) can break resend for lab staff
without the permission; (d) logs users out on edit · Existing staff/report tests +
`test_phase2_route_adversarial_matrix.py` · KK-P1-02, KK-P0-01..04

**`app/services/ai_gateway.py`** · Existing · OpenRouter calls, spend cap, ledger · Optional
`provider_prefs` param on `call_ai_gateway` (default `None` → identical payload) · `require_parameters`
routing for structured outputs · All admin AI callers · None for existing callers · None · None · A
payload change for existing callers would affect patient chat → the default must be byte-identical ·
`tests/test_ai_gateway.py` unchanged + a new case · KK-P1-07

**`app/services/tenant.py`** · Existing · Plans, feature gates (`OPT_IN_FEATURES` `:664`) · Add
`"kriya_ketu"` to `OPT_IN_FEATURES` and `kriya_ketu_enabled()` · Owner opt-in like `ai_receptionist` ·
`platform.py:886-895` validation picks it up automatically · The platform features PATCH accepts the new
key · None (JSONB `clinics.features`) · Not reachable through the enterprise wildcard · Low ·
`tests/test_assistant_feature_gate.py` · KK-P1-02

**`app/config.py`** · Existing · Settings · Add the `assistant_*` settings (A.5) · Config · — · — · — ·
No secrets added · Low · Settings test · KK-P1-02

**`app/tenancy.py`** · Existing · `TENANT_OWNED_TABLES` · Add `assistant_runs` · AGENTS.md §1.1 ·
Lint · — · — · Enforces scoping · Low · `tests/test_lint_unscoped_queries.py` · KK-P1-01

**`app/services/data_retention.py`** · Existing · Purges (`DataRetentionService`) · Add
`purge_assistant_runs(days=90)` · KK-NFR-RET-01 · `scheduler.py` · — · Deletes old assistant rows only ·
— · Wrong predicate → deletes too much; test it with a fixed clock · `tests/test_data_retention.py`
extended · KK-P1-01

**`app/services/scheduler.py`** · Existing · APScheduler jobs, `check_doctor_leaves` (`:1240`) ·
(a) Register the purge job; (b) KK-P0-01 window and `today_ist()` fix · Retention; D-01/D-06 · Distributed
lock · — · — · Patient messaging (b) · (b) changes which bookings get cancelled → frozen-clock tests ·
`tests/test_session20_payment_audit_fixes.py`, `tests/test_opd_walkin_slots.py` (existing leave
coverage) + a new same-day test · KK-P1-01, KK-P0-01

**`app/utils/security.py`** · Existing · `SECURITY_HEADERS` (`:387`) · `microphone=()` →
`microphone=(self)` · D-05 · Applied globally by `SecurityHeadersMiddleware` (`app/main.py:55`) · Header
value · — · Grants the mic to same-origin pages only (the public queue display never requests it) · No
test asserts this header today → **add one** · new assertion in `tests/test_security.py` · KK-P3-01

## 4. Database

**`migrations/108_kriya_ketu.sql`** · Proposed new · — · Create `assistant_runs` + indexes (architecture
§2.3) · Run persistence · `clinics` FK · — · New table · RLS enabled (house style; bypassed by
service_role) · None on existing tables · Applied on a branch first; `verify_supabase_schema.sql` ·
KK-P1-01

**`migrations/rollback/108_down.sql`** · Proposed new · — · `DROP TABLE IF EXISTS assistant_runs` ·
Rollback · — · — · Drops assistant metadata only · — · Loses run history (acceptable; domain data is
untouched; export before dropping if audit needs it) · Rollback rehearsal on a branch · KK-P1-01

## 5. Frontend

**`admin/index.html`** · Existing (19,678 lines, inlined app) · The whole clinic panel · Add: the
`#ketu-mark` symbol in the sprite (`:1852` region); a scoped CSS block (tokens, launcher, panel, cards,
mobile, reduced motion); the launcher + panel elements after `#toastContainer` (`:5972`); the `ketu`
namespace script; `ketu.init(me)` after `/admin/me` resolves (near `:6009`, scope logic); Ctrl+K handler
· The widget · `api*`, `withScope`, `esc`, `go`, `toast`, `apiFail` 401 handling · Calls the new
endpoints · None · All server strings via `esc()`; no new external origins (CSP unchanged) · Global
keydown conflicts (Ctrl+K inside inputs), z-index overlap with drawers, layout shift on mobile, script
errors breaking the whole single-file panel → wrap `ketu.init` in try/catch so a widget bug can never
block the panel · `tests/test_assistant_panel_static.py` (node), Playwright e2e, existing panel tests ·
KK-P2-01..04, KK-P3-03

## 6. Tests (all proposed new unless noted)

| Path | Covers | Task |
|---|---|---|
| `tests/test_assistant_feature_gate.py` | KK-FR-001, opt-in semantics | P1-02 |
| `tests/test_assistant_runs.py` | Idempotency, CAS, BOLA, scope, sweep | P1-03 |
| `tests/test_assistant_registry.py` | Permission drift, P-tier deny-list + H-tier never executable, verify present, page table = sidebar | P1-04 |
| `tests/test_assistant_executor.py` | Dispatch states, no retry, error mapping, revoked session | P1-05 |
| `tests/test_assistant_rules.py` + `tests/fixtures/assistant_corpus.yaml` | Routing accuracy, negation, hypothetical, LLM bypass | P1-06 |
| `tests/test_assistant_llm.py` | Fallback validation and failure modes | P1-07 |
| `tests/test_assistant_policy.py` | Risk, confirmation, plan hash, expiry | P1-08 |
| `tests/test_assistant_stt.py` | Size/mime/limits, no persistence, failure mapping | P3-02 |
| `tests/test_assistant_boundaries.py` | No imports between `app/assistant` and the patient FSM / voice dialog | P1-03 |
| `tests/test_assistant_panel_static.py` | Node-executed widget helpers (send keys, draft, no auto-submit) | P2-02, P3-03 |
| `tests/e2e/assistant.spec.*` (Playwright; location — **unresolved**: no e2e directory exists. Decide in P2-01 whether to use `scripts/` + the Playwright MCP, or add `@playwright/test` as a dev-only tool outside `requirements*.txt`) | UI matrix | P2-* |
| `tests/test_security.py` (existing, extended) | Permissions-Policy value | P3-01 |
| `tests/test_doctor_leave_same_day.py` | D-01 fix | P0-01 |

## 7. Config, docs and deployment

| Path | Status | Change | Task |
|---|---|---|---|
| `.env.example` | Existing | Add `ASSISTANT_*` keys (off by default) | P1-02 |
| `render.yaml` | Existing | Env keys only, default `false`. No service topology change | P1-02 |
| `docs/agent-context/05-FRONTEND-MAP.md`, `06-AI-SYSTEM.md`, `08-AUTH-AND-MULTI-TENANCY.md`, `03-DATABASE-MODEL.md`, `04-API-MAP.md` | Existing | Add Kriya Ketu sections **after** implementation (not before) | P6-04 |
| `AGENTS.md` | Existing | One line pointing to `docs/kriya-ketu/spec.md` after approval | P6-04 |
| `scripts/assistant_llm_smoke.py`, `scripts/assistant_stt_eval.py` | Proposed new | Gates G-KK-LLM / G-KK-ASR | P1-07, P3-04 |

## 8. Unresolved paths

| Item | Investigation needed |
|---|---|
| E2E test location and runner | No Playwright suite exists in the repo (only Python node-extraction tests). Decide in P2-01. |
| Exact statuses after `admin_cancel_confirmed_booking` | Read `app/services/payment.py` (`admin_cancel_confirmed_booking`) before writing the `appointment.cancel` verify. |
| Whether `doctor_leaves` has a unique constraint on (clinic, doctor_name, date) | `grep doctor_leaves migrations/*.sql` before KK-P4-02. |
| Permission dependency of `POST /admin/connectors/{id}/run-now` and `/admin/voice/calls` | Read the route signatures in P1-04 (the drift test will also catch it). |
| Lexicon helper reusable for doctor-name transliteration | Read `app/voice/lexicon.py` in P1-06; if it is call-specific, use only `difflib` + a Latin normaliser. |

---

## 9. v2 additions (full-system scope + bug remediation, 2026-10-14)

Fields in the same order: **Status · Current · Change · Reason · Deps · API · DB · Security · Regression risk · Verification · Task**.

**`app/voice/outbound.py`** · Existing · Outbound eligibility, `enqueue`, `enqueue_bulk`, `autoqueue_leads` · (a) `enqueue_bulk(..., dry_run=False)`: when true, return `{would_queue, skipped}` without inserting, using the same rules; (b) D-16: normalise phones with `to_e164` before the booked/recent checks, and apply the cap after filtering · (a) honest PB-03 preview; (b) correctness of automatic lead calls · `voice_admin.create_outbound_bulk`, `scheduler` hourly job · (a) none by default · None · No new authority; same rules · (b) changes who the hourly job calls → fixture tests with mixed phone formats; (a) must not insert when `dry_run` → test · existing `tests/voice/test_lead_calls.py` (covers `enqueue_bulk` / `autoqueue_leads`) + `tests/voice/test_voice_routes.py`, plus new cases · KK-P4-06a, KK-P0-11

**`app/routers/voice_admin.py`** · Existing · Voice admin routes · (a) `OutboundBulkIn.dry_run: bool = False` passed through; (b) D-12: `_scope(...VOICE_MANAGE)` before window validation in `put_settings`; (c) D-14 classification of its 2 `str(e)` sites · Preview; auth order · outbound · Additive field · None · (b) tightens · Low · Route tests · KK-P4-06a, KK-P0-07, KK-P0-09

**`app/routers/admin.py`** (v2 bug fixes, each its own commit) · D-11 skip existing leave dates in `create_leave` (`:5145`); D-13 compare-and-set for config writes in `update_clinic_profile` / `update_payment_settings` (verify first); D-14 classify 15 `str(e)` sites · See [bug-remediation-register.md](bug-remediation-register.md) · Catalogue, leaves, settings · None · Tightens validation · A client sending price 0 on update now gets 422 → check the panel never sends it (the Test Catalogue form) · Unit + route tests per defect · KK-P0-05/06/08/09

**`app/services/ai_gateway.py`** · D-15 fix the stale comment (docs only) · KK-P0-10

**`app/routers/platform.py`** · D-14: classify 3 `str(e)` sites · KK-P0-09

**`app/assistant/playbooks.py`** · Proposed new · — · PB-01…PB-15 as data + step functions over registry actions · ADR-006 · registry, executor · None · None · No new authority · A playbook bug can make several writes → divergence guard, caps, per-playbook tests · `tests/test_assistant_playbooks.py` · KK-P4-01..15

**`app/assistant/handoff.py`** + `ketu.prefill()` in `admin/index.html` · Proposed new · — · H-tier page + field maps; client fills existing inputs by id, never submits · Sensitive forms · `go()`, page DOM ids · None · None · Never passwords; only visible pages · Element-id drift → test parses ids from HTML · `tests/test_assistant_handoff.py` · KK-P4-12

**`app/services/scheduler.py`** (v2) · Register the L4a morning-briefing job (read-only, writes `admin_notifications` via `broadcast_service` helpers — **verify per-admin targeting**) · ADR-006 L4a · `distributed_job_lock` · — · `admin_notifications` rows · Per-clinic scoping · Extra load at 08:30 IST · Job test · KK-P4-14

**`docs/kriya-ketu/assets/ketu-mark-*.svg`** · New (design source, not served) · Three concepts · DP-08 · — · KK-P2-01

## 10. Files still untouched in v2
`app/services/conversation.py`, `app/routers/webhook.py`, `app/services/message_queue.py`, `app/voice/dialog.py`, `app/voice/gateway.py`, `app/voice/tools.py`, `app/services/clinical_firewall.py`, `app/database.py`, `app/services/payment.py`, `app/services/opd*.py`, `admin/platform.html`, `admin/queue-display.html`, `requirements*.txt`, `Dockerfile`.
