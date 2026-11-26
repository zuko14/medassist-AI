# SESSION 58: Continuous Care, specialty gaps, standalone pharmacy — specification suite

**Date**: 2026-11-24
**Branch**: `main` @ `c8c37ff` (docs only; nothing committed by the session)
**Ask (owner)**: analyse the "Kriya Continuous Care" deck and the product-review gap list (continuous care, data
import, nightly unbilled-charge check); design Continuous Care end to end on top of WhatsApp and the AI receptionist;
analyse the gaps of every specialty plan (derma, eye, dental, IVF, women & child, multispecialty) against the software
those hospitals use; plan a standalone pharmacy offering with its own owner-platform section; write the docs in the
style of the OPD/IPD/DX/RX OS suites without risking any production workflow.

**No code, migration, endpoint, permission or environment variable changed.**

## 1. Method
Code audit by symbol of the plan registry, feature gates, OPD clinical tables, LIS results, Pharmacy refills, WhatsApp
pipeline (`_handle_message_locked` guard order), voice outbound queue, scheduler jobs, owner console. Every "exists"
claim in the new docs carries file:line evidence (docs/continuous-care/AUDIT.md).

## 2. Findings that change designs
| Finding | Evidence | Consequence |
|:-|:-|:-|
| `opd_encounters.follow_up_date` is stored and printed but no job reads it | 103:296; scheduler has no reader | Continuous Care radar starts here |
| Auto-complete marks every past confirmed visit `completed`, no-shows included | `scheduler.py:1046` | outcome verification uses check-in / signed encounter only (ADR-CC-08) |
| WhatsApp emergency keywords have no self-harm phrases (voice has them) | `ai_engine.py:569`; `voice/safety.py:25` | latent fix CC-L1 proposed (owner approval) |
| Legacy medication reminders send free text → undelivered outside the 24 h window; STOP not checked | `prescriptions.py:103`, `:168` | latent fix CC-L2 proposed |
| Pharmacy OS eligibility requires OPD, which requires a doctor plan | `tenant.py:730`, `:755` | `pharmacy` plan + 3 thin bridges (RETAIL-PLAN.md) |
| No departments table (department is a text column on doctors) | `001_initial_schema.sql:68` | data-import spec adjusted |

## 3. Files written
`docs/continuous-care/` (16 docs), `docs/revenue-integrity/{SPEC,status}.md`, `docs/data-import/{SPEC,status}.md`,
`docs/specialty-os/` (README, GAP-ANALYSIS, EYE, DENTAL, DERMA, IVF, WOMEN-CHILD, MULTISPECIALTY-AND-DIAGNOSTICS,
OWNER-CONSOLE), `docs/pharmacy-os/RETAIL-PLAN.md`, `docs/KRIYA_NEXT_ROADMAP.md`; pointers added to `AGENTS.md`,
`docs/pharmacy-os/status.md` (RX-D6) and `docs/pharmacy-os/GAP-ANALYSIS.md` (G30).

## 3a. Later in the session
Design review round 1 (16 defects, VERIFICATION.md), round 2 UI (12 findings, binding NEXT-LANES-UI-SPEC.md), and the
owner-approved panel-wide contrast plan (A11Y-CONTRAST-PLAN.md: 6 items incl. an invisible light-theme title).

## 4. Tests run
None (documentation only).

## 5. Open items (owner)
CC O-1…O-8 (ADR.md; O-2 medical-director sign-off and O-7 legal review of consent notices have no default), CC-L1/L2
approval, MI-O1/O2, SP-O1…O3, RX O-6 (home delivery) for the retail plan. Apply migrations 125–142 on Supabase before
any new lane's migration.
