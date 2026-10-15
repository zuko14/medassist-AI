# Production Safety & Zero-Regression Attestation — Kriya Ketu + bug fixes

Date: 2026-10-15 · Branch `main` @ `1dd7731` + uncommitted working tree · Prepared by Claude Code (Opus 5.5)
Scope: every uncommitted change (23 modified tracked files, the new `app/assistant/`, `app/routers/assistant.py`,
`app/services/config_store.py`, migrations 108–110, two scripts). The 2026-10-14 bug fixes already in commit
`1dd7731` are covered by the test suite but were not re-diffed here.

## Verdict (read this first)
- **No regression was found.** All tests pass, every hook into existing code was inspected, and the default-off
  behaviour was proven in a browser.
- **This is not a "100 % production-safe" guarantee**, and none can honestly be given. The repo's own rules forbid
  that claim. The limits are listed in §7. The changes are **not byte-for-byte backward compatible**: a set of
  bug fixes deliberately changes behaviour (§3.2). Each one is intended, documented and tested.
- **Safe to deploy with Kriya Ketu switched off** (`ASSISTANT_ENABLED=false`, the default), provided the
  §6 deploy order is followed and the §3.2 behaviour changes are acceptable to the owner.

## 1. Test evidence (all run in this session, hermetic, `KRIYA_TEST_LIVE` unset)
| Suite | Result |
|---|---|
| **Full repository suite** | **6,477 passed, 0 failed, 5 skipped** (291.9 s). The original 4,674-test baseline is a subset |
| `test_lint_unscoped_queries.py` (AST tenant linter) | 4 passed |
| `test_admin_super_admin_scope_matrix.py` | 470 passed |
| `test_phase2_route_adversarial_matrix.py` | 237 passed |
| `test_clinical_firewall.py` | 32 passed |
| `test_webhook.py` | 12 passed |
| `test_fsm_transitions.py` | 3 passed |
| `test_phase6_frontend_backend_wiring.py` | 3 passed |
| `test_error_detail_hygiene.py` (no raw exception text in 5xx) | 7 passed |

Skipped (5), all external or live-gated and unrelated to these changes: `test_lab_tests_admin.py:369` (fixture CSV
not in the repo), `test_patient_metrics_production.py` ×3 (live production reads), the MocDoc live sandbox.

**Test integrity check** (a regression can hide behind a weakened test): 22 existing test files were modified;
**no test function was deleted**. Every removed assertion was traced to its replacement:
- The config-write assertions moved to the D-13 write point (`captured_config_writes`). Most are now **stricter**:
  they also assert that only the intended key was written.
- `sent_updates["name"] == …` became an exact-dict match.
- Two cross-tenant refund tests changed `403` → `404` (D-37 b). The request is still rejected, and now no longer
  reveals that the booking exists.

## 2. Blast radius
| Area | Change | Compatibility |
|---|---|---|
| `admin/index.html` | **+1,089 / −0 lines.** No existing line changed. Integration points: `myKetu`/`myKetuStt` flags set from `/admin/me` (2 paths, cleared on sign-out); `ketuSafe('init'/'place'/'teardown')` calls, each in try/catch so a widget error cannot break the page; 2 SVG symbols; **one shared change**: `detailText()` now returns `detail.message` when an error `detail` is an object (it used to return `''`, so callers showed their generic fallback) | Additive. The `detailText` branch shows the server's own fixed message on every page (OPD/voice errors use `{error_code, message}`); the AST hygiene gate keeps exception text out of those messages |
| `admin/platform.html` | +10 / −0: Kriya Ketu toggle card, through the existing `toggleClinicFeature` | Additive |
| `admin/admin.js` | Untouched (dead file, AGENTS.md #7) | — |
| `app/main.py` | `include_router(assistant.router)` | Additive (new `/admin/assistant/*` prefix only) |
| `app/config.py` | 10 `assistant_*` settings, all off or inert by default | Additive |
| `app/services/ai_gateway.py` | `provider_prefs=None` param; payload unchanged when `None` | Existing callers byte-identical |
| `app/voice/outbound.py`, `voice_admin.py` bulk | `dry_run=False` default; the router only passes it when true | Existing path identical |
| `app/database.py` | `get_genuine_patients(branch_id=None)`; `restrict_to_branch(None)` is a no-op; new pure helpers | Existing callers identical |
| `app/services/scheduler.py` | Leave-cancel selection moved to `database.leave_affected()`: same order (same-day filter, then half-day), same skip when the doctor is unknown | Logically equivalent; sweep tests unchanged and passing |
| Config writers (13: admin, platform, voice, home collection) | `config_store.save_config` writes only changed keys (D-13); falls back to the old whole write until migration 110 exists | Same stored result; fixes lost updates under concurrent saves |
| `app/utils/security.py` | `Permissions-Policy` `microphone=()` → `microphone=(self)` (all responses) | Header relaxed for same origin only; nothing requests the mic unless the Ketu mic is on |

### 2.1 New input bounds were checked against the live panel
D-23/D-28 added `days ≤ 365`, `limit ≤ 100/200` and `offset` bounds on 6 routes. Every panel call to them sends a
default or an in-range value (`/admin/stats?days=30`, `/admin/notifications?limit=30`, connector
`audit-log?limit=20`, payments/stats and deliveries with defaults). Leads "All time" (`days=0`) uses
`/admin/leads`, whose existing `ge=0` is unchanged, and Insights (`/admin/insights`) is unbounded and unchanged.
**No existing panel request is newly rejected.**

## 3. Behaviour changes
### 3.1 None for clinics without Kriya Ketu, except the §3.2 fixes, which apply to everyone
### 3.2 Intentional bug-fix behaviour changes (owner should know; all tested)
| Fix | Before | After |
|---|---|---|
| D-23/24/25/28 | Out-of-range `days`/`limit`, a bad reconciliation date, a missing connector → 500 or overflow | 422 / 404 with fixed text; the stats `error` field is the flag `stats_unavailable` instead of the exception text |
| D-25 | Reconciliation default date was the UTC date (wrong 00:00–05:30 IST) | IST date |
| D-26 | Leave over any range | Over 92 days → 422 ("add longer leave in parts") |
| D-27 | Holiday name unchecked | Empty or > 100 characters → 422 |
| D-31/34 | Lab price over the INT limit, doctor fee/name unbounded, malformed doctor id → 500 | 422 with a reason. Editing one shift no longer resets the other shift or the slot length |
| D-21/22/28/31/33/38 | Branch-pinned staff could reach another branch's connector errors, patients, lab rows, OPD invoices | Confined to their branch (+ branch-less rows) |
| D-30 | Any OPD user could recall any patient | Desk: own branch; clinical-only user: own doctor's patients |
| D-32/37 | Import and refund failures could show provider/exception text | Fixed staff-facing text; details in the server log |
| D-37 b | Super-admin refund looked up by id only (any tenant from any panel) | Scoped to the session's `?clinic_id`; cross-tenant → 404 |
| D-36 | Clinical Q&A could be saved; the receptionist spoke stored entries | Refused on save (422); **already-saved clinical entries are no longer spoken** (logged `VOICE_QA_SKIPPED_CLINICAL`) |
| OPD register | Name/phone unbounded | Name ≤ 100, phone ≤ 20 (422) |
| Audit | — | New audit rows: OPD recall, invoice create/issue, payment link, lab collection window |

## 4. Default-off isolation (`features.kriya_ketu` not true, or `ASSISTANT_ENABLED=false`)
| Claim | Result | Evidence |
|---|---|---|
| Launcher and panel DOM never injected | **Proven** | Browser (Chromium, real panel script): flag off, phlebotomist, corporate viewer → no `#ketuLauncher`/`#ketuPanel`; switched on → off removes both |
| Ctrl+K is a no-op | **Proven** | The keydown event is not `defaultPrevented` (the browser keeps its own shortcut); panel stays closed |
| 0 extra database queries | **Proven** | `/admin/me` already loaded the clinic (`get_clinic_by_id` predates this work); `kriya_ketu_enabled()` is a pure check. 0 calls to `/admin/assistant/*` in the browser run. `/admin/assistant/*` returns 403 `assistant_disabled` when called |
| 0 byte overhead | **Not true** | The widget's CSS/JS/mark ship inline in `index.html` to every clinic: **16,376 B gzipped** (budget ≤ 20 KB, DP-16). Parsed, never executed into DOM |
| 0 background jobs | **Not true** | One new daily job, `assistant_runs_purge` (04:30 IST, distributed lock): a single DELETE on `assistant_runs` rows older than 90 days. It touches no other table |
| Mic | Header only | `microphone=(self)` is sent to everyone; no code asks for the mic unless Ketu and `ASSISTANT_STT_ENABLED` are both on |

## 5. AGENTS.md invariants
| # | Invariant | Result | Evidence |
|---|---|---|---|
| 1 | Tenant isolation in app code | ✔ | AST linter 4/4; scope matrix 470; adversarial 237; `assistant_runs` in `TENANT_OWNED_TABLES`; every store query is `.eq("clinic_id", …)` + username |
| 2 | No sync PostgREST on the event loop | ✔ | 0 `.execute()` in new code; every `supabase.table(…)` in `app/assistant/`, `routers/assistant.py`, `config_store.py` runs inside `await sb(…)` (inspected line by line) |
| 3 | HTTP/1.1 patch | ✔ | Untouched (`http2=False`, `database.py:86`; 0 `http2` lines in the diff) |
| 4 | Webhook 20 s SLA | ✔ | `app/routers/webhook.py` untouched; `test_webhook` 12, `test_fsm_transitions` 3. Latency itself not measured here |
| 5 | Zero-LLM clinical firewall | ✔ | `clinical_firewall.py` untouched, imports no AI module; firewall tests 32; Ketu calls it read-only and adds a staff CLINICAL list in `rules.py` |
| 6 | No platform WhatsApp fallback | ✔ | `app/services/whatsapp.py` untouched |
| 7 | Inlined frontend | ✔ | All UI in `admin/index.html`; `admin/admin.js` untouched |
| 8 | IST | ✔ | No naive `date.today()` / `datetime.now()` / `utcnow()` in new code; D-25 moved one UTC default to `today_ist()` |

## 6. Deploy order (each step is an owner/ops action; none was performed)
1. Review the diff and commit (feature branch; nothing is committed today).
2. Apply migrations to production **in order, after a backup**: 107 (in `1dd7731`), **108** (`assistant_runs`),
   **109** (holiday key per clinic), **110** (`kriya_config_apply`). Each has a rollback in `migrations/rollback/`.
   - Without 110: config saves fall back to the old whole write (no breakage; lost-update risk remains).
   - Without 109: the existing cross-clinic holiday-date conflict remains (pre-existing bug, not a regression).
   - Without 108: harmless while Ketu is off, except that the daily purge job logs a handled error; **108 is
     required before any clinic is switched on**.
3. Deploy with `ASSISTANT_ENABLED=false`, `ASSISTANT_STT_ENABLED=false` (the defaults). Existing clinics see the
   §3.2 fixes only.
4. Switch on per clinic from the Platform Owner toggle, after `ASSISTANT_ENABLED=true`. Keep STT off until
   G-KK-ASR passes and DP-07 is done.

## 7. Not verified (limits of this attestation)
- **No staging or production run**: no live Supabase, no real Meta webhook latency, no load test. The webhook SLA
  is argued from "file untouched", not measured.
- Migrations 108–110 were run only on the embedded test Postgres, never on a copy of production data.
- Browser checks used Chromium with a stubbed API. Firefox, Safari/iOS and Android Chrome were not run, and no
  screen-reader pass was done.
- The 2026-10-14 fixes in `1dd7731` were verified by the suite, not re-diffed in this audit.
- Some working-tree files (`app/routers/opd.py`, `tests/conftest_db.py`, three test files) contain LF line endings
  where the repo uses CRLF; git normalises them on commit. Not functional.
- Kriya Ketu task state: 30/34 verified; KK-P3-04 (speech accuracy) and KK-P6-01/03/04 (owner self-test) are open.
