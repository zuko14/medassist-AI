# Kriya Pharmacy OS — Agent Prompt (rules of engagement)

> For Claude Code, Antigravity and any coding agent working on lane **RX**. Start at [status.md](status.md) §1 and §2,
> then this file, then [AUDIT.md](AUDIT.md). Repository-wide rules: `AGENTS.md` (they win on conflict).

## 1. Non-negotiables (stop and ask the owner before breaking any)
1. **Tenancy in code.** RLS is bypassed (service role). Every query on an `rx_*` or other tenant table uses
   `scoped_query(table, clinic_id)` or `.eq("clinic_id", …)` after `is_valid_clinic_scope`. Every new table is added to
   `TENANT_OWNED_TABLES` in the same commit that creates it. Every table has `UNIQUE (clinic_id, id)` and composite FKs.
2. **`await sb(...)`** for every PostgREST call. Never `.execute()` on the event loop.
3. **Zero-LLM clinical boundary.** No RX module imports AI/LLM code. Nothing in pharmacy suggests a dose, a molecule,
   or a substitution beyond the same `generic_id`. `tests/test_rx_no_llm.py` must stay green.
4. **Zero impact.** Pharmacy is off unless `features.pharmacy_enabled is True` (owner toggle). Never use
   `has_feature(clinic, "pharmacy_enabled")`. Existing tests are never edited to make RX pass (RG5 checks `git diff`).
5. **IST** for business dates, expiry, jobs (`rx_ist_date`, `Asia/Kolkata`).
6. **UI inline** in `admin/index.html` inside `<!-- RX:<lane>:START/END -->` / `/* RX:<lane>:START/END */`.
   `admin/admin.js` is dead.
7. **Stock only through ledger functions.** Never insert/update `rx_stock_movements` or `rx_stock_balances` from Python;
   call the `rx_*` SQL function. Never add a code path that edits a posted GRN, a completed sale, a movement or a
   register row.
8. **Money in integer paise**, one rounding rule (half-up integer), Python mirrors SQL and is property-tested.
9. **No PHI in logs**: phones masked `+91XXXXXX7890`; no drug + phone in the same log line.
10. **Never expose stack traces** in API responses; map `rx_*` error codes (API.md §0).

## 2. Lane ownership
| Lane | Owns (may edit) | Must not edit |
|:-|:-|:-|
| RX-DB | `migrations/13[4-9]_rx_*.sql`, `migrations/14[0-2]_rx_*.sql`, `migrations/rollback/1(3[4-9]|4[0-2])_*`, verify block | any earlier migration |
| RX-API | `app/routers/rx.py`, `app/services/rx*.py`; **listed** edits to `tenant.py`, `permissions.py`, `tenancy.py`, `platform.py`, `admin.py` (`/admin/me`), `opd_billing.py` (shift summary only), `gst_billing.py` (RX-52 only), `routers/ipd.py` (MAR `rx_dispense_line_id` only), `scheduler.py` (job registration only), `main.py` (router include) | anything else in `app/` |
| RX-UI | `RX:` blocks in `admin/index.html`, one `loaders` entry per page, IPD MAR form batch select (RX-40) | non-RX markup, `admin.js` |
| RX-KETU | `app/assistant/registry.py` / `resolve.py` / `present` additions for RX, `app/voice/intents.py` `PHARMACY_REFILL` | other intents' behaviour |
| RX-TEST | `tests/test_rx_*.py`, `tests/test_migration_rx_*.py`, bridge extensions in `tests/ipd_pg_bridge.py` (additive, documented) | existing tests |
Two agents never hold the same lane on overlapping tasks. Parallel lanes are recorded in status §1.

## 3. Per-task loop
1. **Locate.** Read the task row (status §6), its workflow (WORKFLOWS), schema (DATA-MODEL), routes (API), the AUDIT
   rows it depends on. Re-locate each cited line by symbol; if it moved, record in status §7; if the finding changed,
   stop and update the design first.
2. **Plan.** Write the file list and the acceptance bullets you will prove in the task's status row (or the PR body).
   Mark the task `IN_PROGRESS` in status.md (commit with the first code change, not separately).
3. **Test first.** Write the failing tests named in the row (real PG for SQL behaviour; route tests through the
   bridge; pure tests for `rx_tax`). Run them; confirm they fail for the right reason.
4. **Implement** the smallest change that passes. Migrations: idempotent (`IF NOT EXISTS`, `CREATE OR REPLACE`,
   `DROP … IF EXISTS` before re-add), with rollback and verify block.
5. **Verify.** Run the task's tests, then the phase gates RG1–RG7 (RG8 from P4). Paste summary lines into status §9.
   UI tasks: live check at 1440 and 390 px, dark and light; record in §9.
6. **Commit** — one task per commit, message `feat(rx): <what> (RX-NN)` (or `test(rx)`, `fix(rx)`, `docs(rx)`), status.md
   updated in the same commit (state → `VERIFIED` only with pasted evidence; otherwise `IMPLEMENTED`). No attribution
   lines. Do not push or open PRs unless the owner asks.

## 4. Commit discipline
- One task ID per commit; a fix to a previous task is its own commit referencing that ID.
- Never mix migration and unrelated UI in one commit (rollback must map to a commit).
- Never commit `.env`, `.DS_Store`, generated PDFs or test databases.
- If a gate fails and you cannot fix it within the task, revert your change, mark the task `BLOCKED` with the
  failing command output, and stop.

## 5. Things that look like shortcuts and are forbidden
| Temptation | Why not |
|:-|:-|
| "Just update the balance row" | breaks ADR-RX-01; trigger refuses anyway |
| Reserve stock on draft | stale holds corrupt counts; completion re-checks instead |
| Use `opd_invoices` for pharmacy sales | one-per-visit unique + tax-exclusive model (AUDIT A7, A9) |
| Charge medicines at doctor order time | ADR-RX-04 |
| Let an LLM map "Pan D" to an item | ADR-RX-06/07 — trigram + aliases + pharmacist confirmation |
| Add `pharmacy` to `opd_invoice_items.item_type` | unnecessary widening of a core OPD CHECK |
| Delete or edit a register row to "fix" it | write a correcting entry; chain must verify |
| Skip a failing existing test | RG5/RG7 rule — find the regression |

## 6. Reporting back
At the end of a session: update status §1 (pointer), §9 (evidence), §10 (one-line log). Report to the owner: tasks
moved to VERIFIED with commands run, anything BLOCKED with the reason, and any owner decision needed (ADR open list).
