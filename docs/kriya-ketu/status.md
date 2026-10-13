# Kriya Ketu — Status

## Current stage
Phase 0: architecture and specification, **v2 (full-system scope)**. Awaiting owner review.

## Implementation status
**Kriya Ketu assistant: NOT STARTED.**
**Existing-system bug fixes: DONE in the working tree (2026-10-14), verified by the full test suite,
NOT committed and NOT deployed** (see §9). No dependency, environment, deployment or production
configuration was changed, and no migration was applied to any live database.

## Audit baseline
| Item | Value |
|---|---|
| Branch / HEAD | `main` @ `d002b3b` (clean at start, 2026-10-13) |
| v2 re-audit | 2026-10-14: leads, outbound voice, catalogue, treatments, OPD registration, dental, home collection, staff, settings, error-text sweep |
| Convention | Feature folder (spec + status) like `docs/opd-os/` |
| Authority | `AGENTS.md` + `docs/agent-context/` cross-checked against source; every cited path or line re-read |

Status vocabulary: `NOT_STARTED` · `IN_PROGRESS` · `BLOCKED` · `NEEDS_REVIEW` · `VERIFIED`.

## 1. Milestones
- [x] Repository audit completed (v1 + v2)
- [x] Ketu mark approved: concept A, "Dhūmaketu" comet (owner, 2026-10-14)
- [x] Owner decisions DP-01…DP-15 taken: all recommendations approved (owner, 2026-10-14; see §2)
- [ ] Architecture specification approved (not yet stated by the owner)
- [ ] Security design approved (not yet stated by the owner)
- [ ] Implementation plan approved / implementation start authorised (not yet stated by the owner)
- [x] Bug-remediation fixes implemented and verified locally: 4,674 tests passed, 0 failed (2026-10-14, §9)
- [ ] Bug fixes committed, deployed, and migration 107 applied to production
- [ ] Kriya Ketu implementation started
- [ ] Integration tests passed
- [ ] Regression suite passed
- [ ] Production release approved

## 2. Decisions (approved by the owner on 2026-10-14: "go with logo A and approve all the decisions")
| ID | Decision | Approved outcome | Unblocks | Remaining owner action |
|---|---|---|---|---|
| DP-01 | Fix for D-01 (near-date leave cancellation) | Trigger cancellation on leave create for today/tomorrow; keep the 08:00 run; use `today_ist()` (D-06) | KK-P0-01, PB-01 cancel step | — |
| DP-02 | D-03 resend route | Require `REPORTS_RESOLVE`, generic 500 text; move the Lab Reports page (`admin/index.html:12178`) to `/admin/reports/{id}/resend` | KK-P0-02 | — |
| DP-03 | STT default mode | Choose `transcribe` or `codemix` by the G-KK-ASR result (the better entity accuracy wins) | KK-P3-02 (after the gate) | — |
| DP-04 | Maker-checker for R3 | Not in v1; R3 = clinic_admin/super_admin + typed confirmation | — | — |
| DP-05 | Staff booking of future consultations | Not in v1; same-day via OPD walk-in; future endpoint stays PLANNED | — | — |
| DP-06 | Promoting LLM phrases into rules | Manual weekly review of LLM-routed successes | — | — |
| DP-07 | DPDP notice / DPA (Sarvam dictation, OpenRouter text) | Approved to proceed: one-time in-widget disclosure; staff dictation added as a purpose; OpenRouter `data_collection: "deny"` if G-KK-LLM confirms support | KK-P3-*, pilot | **Owner/legal must update the privacy notice and confirm the Sarvam and OpenRouter terms before STT is enabled for any clinic** |
| DP-08 | Ketu mark | **Concept A, "Dhūmaketu" comet** (`assets/ketu-mark-comet.svg`) | KK-P2-01 | — |
| DP-09 | Session revocation on permission/role/branch edits (D-09) | Yes, revoke the edited user's sessions (they sign in again) | KK-P0-04 | Tell clinic admins about the behaviour change |
| DP-10 | Audit IP for assistant writes | Stored in `assistant_runs.ip_address` only in v1 | KK-P1-05 | — |
| DP-11 | Pilot clinic | Approved to run a pilot on one live clinic | KK-P6-02 | **Owner to name the clinic** |
| DP-12 | K-01 `get_treatment_doctor_ids` | Fail closed: on a DB error the patient is told to try again (no unfiltered doctor list) | KK-P0-12 | — |
| DP-13 | Unique index on leave dates | Yes: check data, merge duplicates, then add the index in its own migration | KK-P0-06 | — |
| DP-14 | `dry_run` on outbound bulk | Yes: additive flag, default off | KK-P4-06a | — |
| DP-15 | L4b standing write automations | Deferred to v2 behind ADR-006b; v1 ships L1–L3 + L4a | KK-P7 (design later) | — |

## 3. Gates
| Gate | What | Status |
|---|---|---|
| G-KK-LLM | Valid strict-schema JSON ≥ 29/30, p95 ≤ 4 s, via OpenRouter `require_parameters` | NOT_STARTED |
| G-KK-ASR | Sarvam REST formats, < 30 s, pricing, te/hi/en/code-mix accuracy (60 utterances) | NOT_STARTED |
| G-KK-HARDEN | Every registered endpoint passes the KK-P0-15 checklist before registration | NOT_STARTED |

## 4. Task tracker

### 4.1 Planning (Phase 0)
| ID | Task | Status | Evidence |
|---|---|---|---|
| KK-001 | Frontend shell audit | VERIFIED | architecture §1, §4 |
| KK-002 | Backend audit (auth, RBAC, tenancy, AI) | VERIFIED | spec §6, register D-01..D-09 |
| KK-002b | Endpoint inventory, all clinic-panel routers | VERIFIED | registry v2 §4 (admin, opd, voice_admin, home_collection, platform routes read) |
| KK-002c | Workflow traces | VERIFIED | v1 traces + v2: leads (`leads.list_leads`, `send_lead_message`), outbound (`outbound.enqueue_bulk`, `autoqueue_leads`, migration CHECKs), registration (`PatientRegisterIn`), catalogue (`LabTestUpdate`, `LabCleanupApplyRequest`, import ≤ 10 MB), treatments, dental, home collection, staff (`StaffCreate` has a password → H) |
| KK-003 | UI design | VERIFIED / NEEDS_REVIEW | ux spec §1–10 |
| KK-004 | Voice design | VERIFIED / NEEDS_REVIEW | openrouter-and-speech spec B, ADR-003 |
| KK-005 | Action registry | VERIFIED | registry v2 |
| KK-006 | Authorization pipeline | VERIFIED | architecture §5 + v2 note; threat model T-01..T-33 |
| KK-007 | OpenRouter fallback | VERIFIED | spec A, ADR-002 |
| KK-008 | WhatsApp + voice admin actions | VERIFIED | registry §4.9–4.10, PB-02..05 |
| KK-009 | Regression plan | VERIFIED | regression plan §1–6 |
| KK-010 | File impact | VERIFIED | matrix §1–10 (`app/voice/outbound.py` moved from untouched to a scoped change) |
| KK-011 | Test plan | VERIFIED | test plan §1–10 |
| KK-012 | Diagrams | VERIFIED | 14 `.mmd` + 1 inline, all parsed by Mermaid 11 (2026-10-14); rendering not visually reviewed |
| KK-013 | ADRs 001–006 | VERIFIED | decisions/ |
| KK-015 | Per-role journeys | VERIFIED | staff-journeys.md |
| KK-016 | Playbooks PB-01..15 | VERIFIED | playbooks-spec.md; parameter names re-checked (`q`, `date_from`, cleanup body, import limit) |
| KK-017 | Bug remediation register | VERIFIED | bug-remediation-register.md (D-01..D-16, K-01..K-03) |
| KK-018 | Ketu mark | VERIFIED (concept A approved 2026-10-14) | `assets/ketu-mark-comet.svg`; B and C kept as rejected alternatives; artifact "Kriya Ketu Mark" |
| KK-019 | Competitive positioning | VERIFIED (public sources only) | spec §1.4 |
| KK-014 | Consistency review v2 | VERIFIED | §6 below |

### 4.2 Implementation (all NOT_STARTED unless noted; details in implementation-plan.md)
| Group | IDs |
|---|---|
| P0 bug fixes | KK-P0-01 … KK-P0-12, KK-P0-15 (hardening sweep) |
| P1 backend foundation | KK-P1-01 … KK-P1-08 |
| P2 widget | KK-P2-01 … KK-P2-04 |
| P3 voice | KK-P3-01 … KK-P3-04 |
| P4 actions and playbooks | KK-P4-01 … KK-P4-15 (KK-P4-04 BLOCKED: separate approval; KK-P4-06a needs DP-14) |
| P5 owner console | BLOCKED (own Phase 0 addendum) |
| P6 rollout | KK-P6-01 … KK-P6-04 |
| P7 standing writes | BLOCKED (DP-15 / ADR-006b) |

Hard ordering: KK-P0-05 → KK-P4-08 · KK-P0-11 → KK-P4-06 · KK-P0-07/08 → KK-P4-13 · KK-P0-01 → PB-01 cancel step.

## 5. Unverified assumptions
1. Whether profile and payment settings also do an unguarded config read-modify-write (D-13 scope).
2. WhatsApp conversation phone format vs E.164 (D-16).
3. `broadcast_service` supports per-admin notification targeting (L4a briefing).
4. Exact statuses after a refund-path cancellation (`cancelled` confirmed for the non-refund path).
5. ~~Test Catalogue form and price 0 (D-10)~~ resolved: D-10 withdrawn, the update validator already existed.
6. OpenRouter `data_collection` / `require_parameters` with the chosen models (G-KK-LLM).
7. Sarvam REST pricing and duration limit (G-KK-ASR).
8. Competitor capabilities are from public pages, not hands-on tests.

## 6. Consistency review v2 (KK-014)
| Check | Result |
|---|---|
| spec.md links every document, including the v2 additions | ✔ |
| Every FR has a task: FR-001..035 → P1/P2/P3; FR-040..044 → P4-02 (+P0-01, P0-06); FR-050..056 → P4-06, P4-03; FR-060..062 → P4-07, P4-14; FR-070..076 → P4-08..10; FR-080..083 → P4-13; FR-025 (H) → P4-12 | ✔ |
| Every registry action has an existing endpoint or is marked NEW-BE / PLANNED | ✔ |
| Permission sources match the code (dependency vs handler vs role) | ✔ (voice `_scope`, OPD `_opd_scope`, leads `_LEADS`, `require_admin` routes noted) |
| Playbook parameters match the real routes | ✔ (re-verified 2026-10-14) |
| State machine diagram includes the divergence guard | ✔ |
| Consent is never inferred; opt-out, 24 h window and call window are never bypassed | ✔ (registry P tier, T-26/T-28, WA-09, REG-03) |
| Clinical writes and secrets are hand-off only | ✔ |
| The LLM stays fallback-only; playbooks are zero-token | ✔ (ADR-002/006, BRF-02, RT-12) |
| Bugs that gate features are ordered before them | ✔ (§4.2 hard ordering) |
| Files untouched list updated for v2 | ✔ (matrix §10) |
| No implementation performed; no production config changed | ✔ |

## 7. Next actions
1. Owner: review the bug-fix diff (`git diff`), then ask for it to be committed (branch + one commit per
   defect is recommended) and deployed through the normal pipeline.
2. Ops: apply `migrations/107_doctor_leaves_unique_per_clinic.sql` to production via the usual migration
   process (back up `doctor_leaves` first). Until then D-20 shows a clear 409 instead of the old 500.
3. Owner: authorise Kriya Ketu implementation start; name the pilot clinic (DP-11); complete DP-07 before STT.
4. Open defect D-13 (config lost update) needs its own change: atomic DB merge + 10 writers.

## 8. Log
| Date | Entry |
|---|---|
| 2026-10-13 | v1 Phase 0 written; D-01..D-09 found. |
| 2026-10-14 | Bug fixes: D-01, D-03, D-04, D-06, D-09, D-11, D-12, D-14, D-15, D-16, D-17, D-18, D-19, D-20 (code + migration 107), K-01 fixed; D-10 withdrawn (audit error); D-13 left open; 7 pre-existing test failures fixed (T-01, T-02). Full suite 4,674 passed / 0 failed. Not committed, not deployed. |
| 2026-10-14 | Owner: "go with logo A and approve all the decisions" → DP-08 = A; DP-01…DP-15 approved as recommended (§2). Open owner actions: DP-07 legal, DP-11 clinic name. |
| 2026-10-14 | v2: full-system scope, staff journeys, 15 playbooks, H tier, autonomy levels (ADR-006), bug register D-10..D-16, three drawn Ketu concepts, competitive positioning, cross-check corrections (permission sources, `q` param, cleanup body, voice tests location, `app/voice/outbound.py` change scope). |

## 9. Existing-system bug fixes (2026-10-14)

Owner instruction: *"first fix the bugs and update status.md that bugs are fixed permanently, production level."*
Full detail per defect: [bug-remediation-register.md](bug-remediation-register.md).

### What "fixed" means here
- Each fix started with a regression test that failed on the old code, then the fix, then the module's
  existing tests. The tests stay in the suite, so a returning bug fails CI.
- **Full suite after all fixes: 4,674 passed, 0 failed, 5 skipped** (`pytest` from the repo root,
  hermetic test environment, 2026-10-14). Baseline before the fixes: 4,667 passed, **7 failed**. The 7 were
  pre-existing (confirmed by running the same tests on an untouched `HEAD` worktree) and are fixed too
  (T-01, T-02 below).
- Migration 107 was executed on an embedded PostgreSQL built from migrations 001→107 (`tests/test_migration_107_doctor_leaves.py`).
- **Not done: commit, deploy, production migration.** Nothing changes for users until those happen.

### Results
| ID | Result | Where |
|---|---|---|
| D-01 / D-06 | Fixed: leaves for today/tomorrow cancel and notify bookings immediately; daily sweep window starts today on IST | `app/services/scheduler.py`, `app/routers/admin.py` (`create_leave`) |
| D-03 | Fixed (permission level kept, see register): no raw errors; staff-facing messages kept; audit row | `app/routers/admin.py` |
| D-04 | Fixed: staff cancellations audited | `app/routers/admin.py` |
| D-09 | Fixed: role/permission/branch/doctor change or deactivation ends the user's sessions | `app/routers/admin.py` (`update_staff`) |
| D-10 | **Withdrawn**: not a bug (validator already existed) | — |
| D-11 | Fixed: covered dates skipped; half day widened to full day | `app/routers/admin.py` |
| D-12 | Fixed: permission before validation | `app/routers/voice_admin.py` |
| D-13 | **Open** (needs atomic DB merge across 10 writers) | — |
| D-14 | Fixed: 21 sites; AST guard over all routers | `admin.py`, `platform.py`, `integrations.py` |
| D-15 | Fixed (comment) | `app/services/ai_gateway.py` |
| D-16 | Fixed: daily cap counts queued calls (format-mismatch half disproved) | `app/voice/outbound.py` |
| D-17 | Fixed: invalid leave types → 422 | `app/routers/admin.py` |
| D-18 | Fixed: no leave WhatsApp unless the booking was actually cancelled | `app/services/scheduler.py` |
| D-19 | Fixed: all of a day's leave rows combined (slot picker + OPD walk-in) | `app/database.py`, `app/services/opd.py` |
| D-20 | Fixed in code (clear 409) + **migration 107 written and verified, not applied to production** | `app/routers/admin.py`, `migrations/107_…`, `migrations/rollback/107_down.sql` |
| K-01 | Fixed: treatment-doctor read error offers no doctor instead of every doctor | `app/database.py`, `app/services/specialty_flow.py` |
| D-02, D-05, D-07, D-08, K-02, K-03 | Not code defects to fix now (limitation / by design / ops / accepted) | register |

### Pre-existing test failures fixed
| ID | Cause | Fix |
|---|---|---|
| T-01 | 3 tenant-resolution tests counted DB calls; Strategy 1 is skipped without a `phone_number_id` and a dev-only sandbox lookup was added, so mocks answered the wrong query (the code was correct and fails closed) | Tests now recognise the count query by its filters (`tests/test_tenant_omission_hardening.py`) |
| T-02 | Test harness forced credentials at import but set `APP_ENV` too late, so the suite ran as **production** (`Settings.app_env` defaults to production); 4 CallMedex tests of non-production behaviour failed | `APP_ENV=testing` forced at import in `tests/conftest.py` |

### New regression tests
`tests/test_doctor_leave_same_day.py`, `tests/test_leave_rows_combined.py`, `tests/test_error_detail_hygiene.py`,
`tests/test_staff_edit_revokes_sessions.py`, `tests/test_treatment_doctors_fail_closed.py`,
`tests/test_migration_107_doctor_leaves.py`, plus new cases in `tests/voice/test_voice_routes.py` and
`tests/voice/test_lead_calls.py`. One existing fixture corrected (`tests/test_admin_holiday_leave_delegation.py`
used the invalid leave type `"full_day"`).

### Behaviour changes staff may notice
- Saving a doctor leave for today or tomorrow now cancels and notifies affected bookings **right away**
  (previously: next morning, or never for same-day).
- Changing a staff member's role, permissions, branch or doctor link signs that person out.
- Error messages on server failures are now plain sentences instead of technical text.
