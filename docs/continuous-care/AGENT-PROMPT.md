# Kriya Continuous Care — Master Prompt for Implementing Agents

> Paste §1–§3 at the start of every CC session (Claude Code, Antigravity, any agent). Then follow
> [status.md](status.md) §1 "Next task".

## 1. Context you must load (in this order)
1. `AGENTS.md` (repo invariants: service-role RLS bypass, `sb()` off-loop, HTTP/1.1 patch, 15 ms webhook, zero-LLM
   clinical firewall, clinic-only WhatsApp credentials, `admin/index.html` is the live UI, IST everywhere).
2. `docs/continuous-care/status.md` §1, §4, §6 and the §10 log tail.
3. `docs/continuous-care/AUDIT.md` — re-locate by symbol every row your task depends on.
4. The design docs your task names (ARCHITECTURE / ADR / DATA-MODEL / PROTOCOLS / WORKFLOWS / CHANNELS / API / UI /
   SECURITY / TEST-PLAN).

## 2. Rules (non-negotiable)
1. **Production system, zero regression tolerance.** CC is OFF for every clinic until the owner enables it. Nothing
   you write may change behaviour for a clinic without `cc_access`. Prove it with the zero-impact tests.
2. **Never edit, skip, xfail or delete an existing test.** Never raise a lint baseline. A red existing test means your
   change is wrong (or you found a latent bug — stop and report it in status §8 before fixing).
3. **Zero-LLM boundary** (ADR-CC-02): no AI/voice-NLU/assistant import in `app/services/cc*`, `app/services/cc_protocols/`,
   `app/routers/cc.py`. Readings are parsed by code. Packs never change a medicine or dose (ADR-CC-04).
4. **No trigger on any pre-existing table** (ADR-CC-13). Existing objects change only as listed in ARCHITECTURE §7.
5. Every `cc_*` table goes into `TENANT_OWNED_TABLES` in the same commit; every query uses `scoped_query` or
   `.eq("clinic_id", …)`; every DB call is `await sb(...)`.
6. Every new `/admin/cc` route is added to `tests/test_cc_route_matrix.py` in the same commit.
7. Migrations: next free number at the time you start (status §5 says the reserved numbers); idempotent; rollback
   file; verify block; real-Postgres test. If `main` took your number, renumber in one commit with every doc reference.
8. Conversation hook changes are limited to the two claims in ARCHITECTURE §7; any other edit to `conversation.py`
   needs an ADR change first.
9. Patient-facing text is fixed, versioned and approved (templates, cards). No generated text reaches a patient.
10. Logs mask phones; never log readings, PHQ answers or names.

## 3. Definition of done for every task
- Code + tests listed in TEST-PLAN §2 for the task exist and pass.
- Standing gates CG1–CG7 (CG8 from P4) pass; paste summary lines into status §9.
- UI pages checked at 1440/390 px, dark and light.
- status.md updated in the same commit (task state, §1 pointer, §10 log line).
- Commit message: `feat(cc): P<n> <what> (CC-xx…CC-yy)`; latent fixes: `fix(core): <what> (CC-L<n>)`.
- Work on a branch `cc/p<n>-<name>`; do not push or merge without the owner.

## 4. When you are blocked
Write `BLOCKED` with the reason in status §6, add an entry in §10, and stop. Do not work around a blocked
dependency, a missing owner decision (ADR "Open owner decisions"), or a failing gate.
