# Kriya Ketu — Regression and Rollback Plan

Parent: [spec.md](spec.md). A frontend-only change is **not** assumed to be risk-free: `admin/index.html`
is one 19.7k-line inline application, so one script error can break every page.

## 1. Kill switches (fastest rollback, no deploy)
| Switch | Effect | Where |
|---|---|---|
| `ASSISTANT_ENABLED=false` (env) | Launcher gone for all clinics; all `/admin/assistant/*` → 403 | Render env + restart |
| `clinics.features.kriya_ketu=false` | Off for one clinic | `PATCH /platform/clinics/{id}/features` |
| `ASSISTANT_LLM_ENABLED=false` | Rules-only mode | env |
| `ASSISTANT_STT_ENABLED=false` | Mic hidden. **Note:** the `Permissions-Policy` change stays (harmless) unless reverted | env |

## 2. Change-by-change analysis

| Change | Existing behaviour at risk | Direct deps | Indirect deps | DB / API compatibility | Auth / tenant | Background workers | Provider failure | Rollback | Regression tests |
|---|---|---|---|---|---|---|---|---|---|
| Widget in `admin/index.html` | Every panel page (a script error is fatal); global key handling; mobile layout; z-order of drawers and dialogs | `api*`, `go`, `esc`, `/admin/me` flow | OPD timers in `go()`; phlebotomist/corp views | API additive | None | None | Widget shows an error card; panel unaffected | Flag off; revert the commit (no data) | Existing `test_admin_panel_*`, `test_phase6_frontend_backend_wiring.py`, UI-01..17 |
| `/admin/me` new fields | Panel bootstrap parses `/admin/me` | `admin.py:841` | Every page loader | Additive JSON | Must not leak flags across tenants (computed from the scoped clinic) | None | — | Revert | Existing `/admin/me` tests |
| New router + package | Boot (an import error kills the app) | `app/main.py` | All routes | New paths only | New surface → SEC matrix | None | — | Revert; flag off | `test_production_launch_gates.py`, `test_multi_worker_smoke.py` |
| In-process dispatch | Existing endpoints run twice through middleware; audit IP | Every registered endpoint | Correlation logs | Unchanged | Uses the caller's own credentials | None | Endpoint failures map to FAILED/UNKNOWN | Remove an action from the registry (no deploy of endpoints) | EXE-*, SEC-04 |
| `ai_gateway.provider_prefs` | **Patient WhatsApp chat** and admin AI payloads | `call_ai_gateway` | `ai_engine`, voice knowledge | Default `None` → identical payload | None | None | — | Revert | LLM-10 snapshot, `test_ai_gateway.py` |
| `OPT_IN_FEATURES` entry | Platform features UI lists options; plan logic | `tenant.py` | `platform.html` feature toggles | JSONB key | Not via the enterprise wildcard | None | — | Revert; existing `kriya_ketu` keys are inert | Feature tests |
| Migration 108 | Migration pipeline and checksums | `run_migrations.py` | `schema_migrations` | New table only | Tenancy lint | Purge job | — | `108_down.sql` (drops assistant metadata only; **domain data untouched**) | Apply + rollback rehearsal on a branch |
| Retention purge job | Scheduler load; lock contention | `scheduler.py`, `distributed_job_lock` | Other jobs on the same cadence | — | Platform sweep | Yes | — | Remove the job registration | `test_data_retention.py` |
| `Permissions-Policy` mic | Any page relying on the mic being blocked (none found) | `SecurityHeadersMiddleware` | Public queue display (no mic use) | Header only | — | — | — | Revert one line | VOICE-11 |
| KK-P0-01 leave job | **Patient cancellations, refunds, WhatsApp** | `scheduler.py`, `create_leave` | Payment refunds, templates | None | Platform sweep remains scoped per leave | Yes | Meta/Razorpay down → existing alert path | Revert (no schema); cancellations already done stay done | Frozen-clock tests + session-20 tests |
| KK-P0-02 resend auth | The Lab Reports page resend for users without `REPORTS_RESOLVE` | `admin.py:5672` | `admin/index.html:12178` | 200→403 for some roles | Tightens | None | — | Revert | Route matrix + DP-02 decision |
| KK-P0-03 cancel audit | Cancel latency (+1 insert) | `admin.py:5390` | — | — | — | — | Audit insert failure is already swallowed by `log_admin_action` | Revert | Cancel tests |
| KK-P0-04 session revoke | Staff logged out on edit | `update_staff` | Front desk mid-shift | — | Tightens | — | — | Revert | Staff tests |

## 3. Specific areas called out by the brief
| Area | Why it is safe or what guards it |
|---|---|
| OPD booking concurrency | The assistant never books. It only reaches OPD reads and `call-next` through the existing CAS endpoints. |
| Existing FSM | No import, no shared state (`test_assistant_boundaries.py`). |
| WhatsApp workflows | Only via `/admin/reports/{id}/resend`, `/admin/leads/message`, cancel/confirm/reject endpoints, unchanged. No broadcast. |
| Voice receptionist state | No edits in `app/voice/`. Read-only import of `dates.py`. Separate `ai_usage_ledger` task types. Voice usage is excluded from the admin cap sum (`ai_gateway.py` comment). That comment points at `app/voice/usage.py`, **which does not exist**; the voice budget lives in `app/routers/voice_admin.py` / `voice_platform.py` (stale-comment finding, not fixed here). |
| Tenant feature flags | Opt-in key only. No change to `has_feature`. |
| Audit records | Endpoint audits unchanged. `assistant_runs` is additive. |
| Session state | Same cookie. The assistant never mints sessions. |
| Webhook idempotency | Untouched. |
| Migrations | A single additive migration with a down script. |
| External connectors | `connector.run_now` uses the existing endpoint and its lock. |

## 4. Rollout and monitoring
1. Staging tenant (KK-P6-01) → full test plan.
2. Pilot clinic with LLM on and STT off for one week. Watch the following.
   - `assistant_runs`: state distribution, `UNKNOWN_OUTCOME` count (alert if > 1 %), `route_layer` mix.
   - `ai_usage_ledger`: `assistant_intent` cost per day (alert if > 3× the pilot median).
   - Webhook p95 and patient chat error rate: must not move.
   - `admin_audit_logs`: assistant-originated writes, cross-checked against `assistant_runs`.
3. STT on after G-KK-ASR.
4. GA via the per-clinic flag.

## 5. Rules that rollback must never break
- Never delete domain rows to "undo" an assistant action. Reversal is a **new**, separately confirmed
  action (e.g. delete the leave). Cancelled bookings and sent messages cannot be reversed. Say so.
- Never drop `assistant_runs` in production without exporting it first if an audit request is open.
- A migration rollback is only needed if the table itself causes harm. Disabling via flags is preferred.

## 6. v2 additions (full-system scope)

| Change | Existing behaviour at risk | Guard | Rollback | Regression tests |
|---|---|---|---|---|
| `enqueue_bulk(dry_run)` | Voice page bulk import; hourly `autoqueue_leads` | Default `False` → byte-identical behaviour | Revert one function | `tests/voice/test_lead_calls.py`, `test_voice_routes.py` |
| D-16 fix in `autoqueue_leads` | Which leads get auto-called | Fixture with mixed phone formats; cap semantics documented | Revert; switch `auto_leads` off per clinic | `test_lead_calls.py` + new cases |
| D-11 leave dedupe | Leaves page double-click behaviour | Skip, never error | Revert | `test_admin_holiday_leave_delegation.py` |
| D-12 / D-13 settings | Voice settings and profile saves | 409 only on a real concurrent change | Revert | Voice routes + profile tests |
| D-14 error text | Messages the panel shows for intended 4xx | Classify before changing; keep intended 4xx text | Revert per site | Route tests grep |
| Playbooks (bulk writes) | Catalogue, prices, calls | Caps, typed counts, per-step verify, divergence guard, kill switch per action (remove from registry) | Remove the playbook from the registry (config release), reverse data via a reversal plan where possible (prices); calls already placed cannot be undone | Playbook tests |
| Hand-off pre-fill | Page forms | Fills only, never submits; ids drift-tested | Disable H actions in the registry | UI-19 |
| L4a scheduled briefing (**v2, DP-17**; not built in v1) | Scheduler load at 08:30 IST | Distributed lock, read-only | Remove the job registration | BRF-03 |

Irreversible by nature (stated in previews): placed calls, sent WhatsApp messages, refunds issued, cancelled bookings (patients notified).
