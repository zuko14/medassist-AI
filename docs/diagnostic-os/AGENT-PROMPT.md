# Kriya Diagnostic OS — Master Prompts and Working Rules for Coding Agents

> For Claude Code, Antigravity or any other coding agent. Read [status.md](status.md) first; this file tells you *how*
> to work, status.md tells you *what* is next. If the two disagree, status.md §1 (current pointer) wins for "what",
> this file wins for "how", and AGENTS.md §1 invariants override both.

## 1. Master prompt — single agent (start or resume)

Copy everything in the block into a new agent session at the repository root.

```text
You are implementing the Kriya Diagnostic OS in this repository (Kriya AI, production healthcare SaaS for Indian
hospitals and diagnostic centres). Production is live: your changes must not alter behaviour for any clinic that has
not enabled the module.

READ FIRST, in this order, before touching code:
  1. AGENTS.md (non-negotiable invariants: RLS is bypassed so every tenant query is scoped in code; every PostgREST
     call goes through `await sb(...)`; never touch the HTTP/1.1 patch; webhook returns in 15 ms; zero-LLM clinical
     paths; no platform WhatsApp fallback; admin/index.html is the live UI and admin/admin.js is dead; IST).
  2. docs/diagnostic-os/status.md  — §1 current pointer, §2 resume protocol, §4 phase board, §6 tasks.
  3. docs/diagnostic-os/AGENT-PROMPT.md (this file) — §2 loop, §4 file ownership, §6 evidence, §7 stop rules.
  4. The design files the current task references (ARCHITECTURE, ADR, DATA-MODEL, WORKFLOWS, REPORT-TEMPLATES, API,
     UI, SECURITY, TEST-PLAN, ROLLOUT, KRIYA-KETU in docs/diagnostic-os/).
  5. The precedent code you are extending: migrations/111_ipd_os_core.sql, 115_investigation_and_procedure_orders.sql,
     118_lab_information_system.sql; app/routers/ipd.py (_ipd_scope), app/routers/lis.py, app/services/lis.py,
     app/services/orders.py, app/services/opd_billing.py, app/services/tenant.py, app/services/permissions.py,
     app/routers/platform.py (feature toggle), tests/test_migration_118_lis.py, tests/test_lis_api_e2e.py,
     tests/test_ipd_api_e2e.py.

THEN:
  - Follow status.md §2 (resume protocol). Work on exactly the task named in §1 "Next task". If its dependencies are
    not VERIFIED, stop and report.
  - Run the loop in AGENT-PROMPT.md §2 for that task. Then move §1 to the next task and repeat until the phase's
    Definition of Done holds and gates DG1–DG7 (TEST-PLAN.md §1) are green with results pasted in status.md §9.
  - Keep each commit to one task. Update status.md (task state, §9 evidence, §10 log, §1 pointer) in the same commit.
  - Design changes: allowed only when the code proves the design wrong. Record them in status.md §7 and update the
    affected design file in the same commit.
  - Never: skip/xfail/delete a test to get green; raise a linter baseline; run with KRIYA_TEST_LIVE=1; apply a
    migration to Supabase or deploy without the owner's explicit instruction; edit admin/admin.js; add an LLM call to
    any DX path; build referral-commission features (ADR-DX-11); put a model identifier in commits or code.
  - When you pause (end of session, context limit, blocker): update status.md §1 with the exact next step, the
    branch, last commit and anything half-done, commit, push, and stop.

Report at the end: tasks moved to VERIFIED with evidence, anything BLOCKED and why, and the next exact action.
```

## 2. The per-task loop
1. **Locate** — read the task row, its acceptance, the design sections it cites, and every file you will touch.
   Grep for all callers of anything you change (`grep -rn "<symbol>" app tests`).
2. **Plan in the commit message draft** — files, functions, migration objects, tests. Keep it in your notes.
3. **Test first where the rule is crisp** — write the real-Postgres or pure test from TEST-PLAN §2 that fails now.
4. **Implement** — migration (with verify block + rollback) → service → router → UI. Patterns: SQL functions own
   every state change (row lock order: visit → item → sample → order → result); Python maps error codes; reads use
   `scoped_query`/`fetch_all`; background work via `spawn_background_task`; money in paise; IST.
5. **Run** the task's tests, then the phase's touched regression files, then DG1 (tenancy linter) at minimum.
6. **At phase end** run DG1–DG7 and the phase DoD; paste summary lines into status.md §9.
7. **Commit and push** (§5). Move status.md §1.

## 3. Parallel mode (several agents at once)

### 3.1 Coordinator prompt (one agent that only plans, merges and verifies)
```text
You coordinate parallel implementation of the Kriya Diagnostic OS. Read AGENTS.md and docs/diagnostic-os/status.md,
AGENT-PROMPT.md. Do not write feature code yourself.
  1. From status.md §4, list lanes whose dependencies are VERIFIED and that may run in parallel (column "Can run in
     parallel with"). Never start two lanes that both need a migration unless you will merge them one at a time.
  2. For each lane create a branch from main (dx/<phase>-<lane>) or a git worktree, and start a worker with the lane
     prompt (§3.2) filled in. Record lanes in status.md §1 "Lanes running in parallel".
  3. Merge order: (a) lanes without a migration in any order after their gates pass; (b) lanes with a migration
     strictly one at a time — before merging, the lane rebases on main, renames its migration to the next free number
     (ADR-DX-18), re-runs DG1–DG7, and only then merges. Fill status.md §5 at each merge.
  4. After each merge run DG7 on main. If red, revert the merge commit and send the failure back to the lane.
  5. Keep status.md the single source of truth; resolve conflicting status edits by re-reading both lanes' evidence.
```

### 3.2 Lane worker prompt (fill the brackets)
```text
You are a lane worker on the Kriya Diagnostic OS. Lane: [LANE NAME]. Tasks: [DX-.. list]. Branch/worktree: [name].
Read AGENTS.md, docs/diagnostic-os/status.md, AGENT-PROMPT.md (§2 loop, §4 file ownership, §6 evidence, §7 stop), and
the design sections your tasks cite. Edit ONLY the files your lane owns (AGENT-PROMPT.md §4). Shared hot files may be
edited only inside your lane's DX marker block; if a change outside your block is needed, stop and ask the
coordinator. If your lane has a migration, name it with the next free number now and expect to renumber on rebase.
Run your tests and DG1 locally; run DG1–DG7 before asking to merge. Update status.md only for your tasks' rows, §9
evidence lines and a §10 log line. Never merge to main yourself.
```

### 3.3 Lane catalogue
| Lane | Tasks | Start when | Owns (exclusive) | Touches hot files (marker block) |
|:-|:-|:-|:-|:-|
| **F — Foundation/core** (serial spine) | P1, P2 schema+routes, P3, P4, P6 core, P7 core | phase deps VERIFIED | `migrations/*_dx_*.sql`, `migrations/rollback/*dx*`, `app/services/dx.py`, `dx_samples.py`, `dx_results.py`, `dx_reports.py`, `app/routers/dx.py`, `dx_public.py`, `tests/test_migration_dx_*.py`, `tests/test_dx_*_e2e.py` | tenancy, permissions, tenant, main, platform, lis, orders, admin |
| **PURE** | DX-13, DX-27a, DX-43a (+ template schema), Westgard rules for DX-62 | from P1 | `app/services/dx_formula.py`, `dx_barcode.py`, `dx_pdf.py`, `app/services/dx_template_schema.py`, `tests/test_dx_formula.py`, `test_dx_labels.py`, `test_dx_pdf.py`, `test_dx_templates.py` (schema part) | none |
| **UI** | DX-11, 17, 25, 30, 35, 40, 47, 53, 58 | when the phase's routes are merged | — | `admin/index.html` (DX:<page> blocks), `admin/platform.html` (DX block) |
| **LOGISTICS** | P5 | P4 VERIFIED | `tests/test_dx_logistics.py`; functions in its migration | `app/routers/dx.py` (DX:logistics block), `dx_samples.py` (logistics section) |
| **B2B** | P8a, P8b | P3 (P8a), P7 (P8b) | `app/services/dx_b2b.py`, `tests/test_dx_b2b_e2e.py`, `test_migration_dx_m8.py` | `dx.py` router block, `permissions.py`, `admin.py` (`verify_credentials` partner allowlist) |
| **ASSIST** | P9 | P7 | `app/services/dx_status.py`, `tests/test_dx_mis.py` | `app/assistant/registry.py` (DX block), voice tools, conversation intent |
| **LAB-IO** | P10 | P6 | `app/routers/dx_bridge.py`, `connectors/bridge/`, `tests/test_dx_bridge.py` | `app/main.py` include |

### 3.4 Tool-specific notes
- **Claude Code**: use one session per lane, or the Agent tool with `isolation: "worktree"` for lane workers; the
  coordinator merges. Each worker's final report is not visible to the user — the coordinator relays it.
- **Antigravity** (Windows): use `.venv\Scripts\python.exe -m pytest …`; after a full run, kill orphaned workers left
  by `test_multi_worker_smoke.py` (PowerShell command in `docs/opd-os/status.md` "Harness notes"); a killed run can leave
  `tmp_pg_pytest_data` dirty — re-run the failing real-PG file alone before treating it as a regression.
- Linux cloud sessions: install dev requirements first (`pip install -r requirements.txt -r requirements-dev.txt`);
  Chromium + Playwright are preinstalled — do not run `playwright install`.

## 4. Shared hot files and markers
| File | Rule |
|:-|:-|
| `app/tenancy.py` | append DX tables in one `# Diagnostic OS (migration NNN)` group per migration |
| `app/services/permissions.py` | one `# ── Diagnostic OS ──` section; new roles added to `STAFF_ROLES` and `ROLE_PRESETS` only there |
| `app/services/tenant.py` | `dx_*` helpers next to `ipd_access`; `OPT_IN_FEATURES` one line |
| `app/main.py` | one include line per DX router, grouped with a comment |
| `app/routers/platform.py` | one `if body.feature == "dx_enabled":` block; purge call order |
| `app/routers/admin.py` | `/admin/me` fields; `verify_credentials` confinement allowlists for new roles |
| `app/routers/lis.py`, `app/services/lis.py`, `app/routers/orders.py`, `app/services/orders.py` | only the changes listed in ROLLOUT §0.3; each with a regression test |
| `admin/index.html` | `<!-- DX:<page>:START/END -->` and `/* DX:<page>:START/END */` blocks only (UI.md §1) |
| `admin/platform.html` | one DX block |
| `app/assistant/registry.py` | one `DX_ACTIONS` block |
| `migrations/verify_supabase_schema.sql` | one DX section appended per migration |

## 5. Branches, commits, PRs
- Branch from `main`: `dx/p<N>-<short>` (or the branch the session was assigned). Never push to `main` directly.
- Commit message: `feat(dx): DX-<id> <what>` / `test(dx): …` / `docs(diagnostic-os): …`; body = what changed, the
  tests run and their summary lines. Use the attribution lines required by the environment; never include a model
  identifier.
- PRs only when the owner asks; describe the phase, migrations, gates with pasted results, and the rollback.
- Production migration application and deploys are the owner's action (ROLLOUT §0.4), or an agent's only with
  explicit written instruction in the session.

## 6. Evidence format (status.md §9)
`| 2026-11-12 14:05 | DX-18 / DG5 | python -m pytest tests/test_migration_115_orders.py … -q | 37 passed in 21.4s |`
For live UI checks: what was clicked, with which role, at which widths, and what was seen (as IPD status §4 does).

## 7. Stop and ask the owner when
- a design rule here would change existing production behaviour beyond ROLLOUT §0.3;
- an owner decision (ADR.md "Open owner decisions") must differ from its default for the work to make sense;
- a migration needs a table rewrite, a lock longer than a few seconds on a large table, or a destructive change;
- a test you did not write fails and the cause is not clearly yours;
- you would need credentials, production data, Meta template approval, or a Supabase/Render action.

## 8. Definition of done for any task
- [ ] Acceptance in the task row holds and the listed tests pass (summary line pasted).
- [ ] Tenancy linter green; new tables in `TENANT_OWNED_TABLES`; matrices cover new routes.
- [ ] Every write audit-logged; clinical reads audit-logged; errors mapped (no raw DB text).
- [ ] No AI/voice/assistant import in DX clinical code (`test_dx_no_llm.py` green).
- [ ] Migration (if any): verify block, rollback, idempotent re-apply, `verify_supabase_schema.sql` updated.
- [ ] UI (if any): checked at 1440 px and 390 px with a real login for each role involved.
- [ ] status.md updated (state, evidence, log, pointer) in the same commit; design docs updated if anything deviated.
