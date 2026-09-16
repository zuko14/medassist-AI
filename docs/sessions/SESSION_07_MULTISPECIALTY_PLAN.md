# Session 07 — The `multispecialty` plan: general hospital + treatments catalogue

**Date:** 2026-09-16
**Branch:** main
**Migration:** 078
**Plan doc:** `docs/specialty_plan/11-multispecialty-plan.md`

---

## 1. Intent

Real hospitals do not fit either half of the plan matrix. The trigger was a
photograph of the Aayush Hospitals board: emergency & accident care, heart and
brain stroke care, maternity and women care, dialysis and acute kidney care,
cardiac/medical/surgical ICU, laboratory and pharmacy, X-ray/ultrasound/CT —
a full general hospital that also sells specialty procedures inside.

* the six general plans (`enterprise`, `polyclinic`, …) give departments but no
  treatments catalogue;
* the four specialty plans (`derma`, `eye`, `dental`, `ivf`) give a catalogue
  but assume the whole facility is one specialty.

One plan for the intersection, with its own panel URL, and nothing about the
ten existing plans allowed to change.

## 2. Decisions

| Decision | Choice | Why |
| :-- | :-- | :-- |
| Slug / name | `multispecialty` — "Multi-Specialty Hospital" | How Indian hospitals describe themselves; "super specialty" would be wrong (it means tertiary cardiology/neuro/nephro). |
| Feature set | `polyclinic` ∪ `{specialty_treatments}` — every feature, enumerated | Enterprise-equivalent without the `*` wildcard, which cannot be trimmed per tenant and makes `has_feature()` answer for features never sold. Derived from polyclinic so the two cannot drift. |
| Specialty identity | **NOT** in `SPECIALTY_BY_PLAN`; new `HYBRID_SPECIALTY_PLANS` | That map drives whether the patient menu drops its departments row. A hospital with fifteen departments must keep it. |
| Starter lists | Admin picks per load, validated against `STARTER_TREATMENTS`; no default | A hybrid hospital has no single list. Seeding the wrong fifteen treatments silently is worse than a 400. |
| Message quota | 5,000/month (mirrors polyclinic) | Hospital scale, not a single-chair clinic. |
| Panel URL | `/hospital-panel` | Same pattern as `/derma-panel`: one more route onto `admin/index.html`. |
| AI drafts | Left resolving to `"general"` | Prompt renders it "an Indian hospital clinic", and the treatment's category is already in the prompt. A category→specialty guessing map would add a failure mode for no gain. |

## 3. The finding that made this small

`conversation.py:1502` already carried the branch:

> *"On a single-specialty plan 'Our Services' would list one department; Our
> Treatments replaces it. Override-enabled general clinics keep both."*

Written in Session 05 for clinics that switch the catalogue on by feature
override. This plan **is** that case, promoted to a product. Consequences:

* **no WhatsApp flow code was written** — `specialty_flow.py` and
  `conversation.py` are untouched;
* **no schema change** — `specialty_treatments` has no `specialty_type`
  column, so one clinic already mixes derma, eye and dental rows, separated by
  `category`. Migration 078 widens two CHECKs and inserts one tier row;
* **no permission or tenancy work** — `TREATMENTS_MANAGE` and both tables were
  already registered.

## 4. Files changed

**New:** `migrations/078_multispecialty_plan.sql`,
`migrations/rollback/078_down.sql`, `tests/test_multispecialty_plan.py`,
`docs/specialty_plan/11-multispecialty-plan.md`, this file.

**Modified:** `app/services/tenant.py` (plan comment, `PLAN_FEATURES` entry,
`HYBRID_SPECIALTY_PLANS`, one clause in `specialty_enabled()`),
`app/routers/admin.py` (`TreatmentStarterRequest`, `_starter_specialty()`,
optional body on the starter endpoint), `app/routers/platform.py`,
`app/routers/clinics.py`, `app/services/message_accounting.py`,
`app/main.py`, `admin/platform.html`, `admin/index.html`.

Every source edit is an added dict key, an added slug in a validator, an added
route decorator, or an added optional parameter. No existing plan's feature
set, no `booking_type`, no slot-uniqueness index and no payment path was
touched.

## 5. Tests

`tests/test_multispecialty_plan.py` — 42 tests, written before the
implementation and confirmed red (`ImportError: HYBRID_SPECIALTY_PLANS`):

* feature set equals `ALL_FEATURES` and equals polyclinic ∪ catalogue; no `*`;
* both halves on (departments + lab/radiology + treatments), parametrised;
* the four single-specialty plans did not gain `lab_reports`,
  `lab_test_booking` or `multi_department`;
* `SPECIALTY_BY_PLAN` still has exactly four entries and
  `is_specialty_plan()` is False for the hybrid — the test that fires first if
  someone "tidies" the slug into the wrong set;
* `specialty_enabled()` truth table including the explicit `False` override
  and the unchanged answers for enterprise and polyclinic;
* the patient menu keeps departments **and** lab tests next to treatments,
  stays within Meta's 10 rows, and respects the 24/72-character limits;
* the menu falls back to an ordinary hospital menu with nothing published;
* starter list: request model validates against the catalogue, plan wins on a
  specialty clinic, hybrid without a specialty is a 400, override-enabled
  general clinic likewise;
* registry sweep: both clinic validators, `platform.py`, the console's badge /
  filter / create options / `PLANS_WITH_LAB_REPORTS`, `/hospital-panel`,
  migration 078 and its rollback, and the panel's specialty picker.

**Results**

| Run | Result |
| :-- | :-- |
| `tests/test_multispecialty_plan.py` (before implementation) | 1 collection error — `ImportError: HYBRID_SPECIALTY_PLANS` (expected red) |
| `tests/test_multispecialty_plan.py` | **42 passed** in 4.54s |
| 20 specialty / plan-registry / admin / conversation suites | **361 passed, 1 skipped** in 28.29s |
| `tests/` minus `test_multi_worker_smoke.py` | **2231 passed, 1 skipped** in 279s |

`test_multi_worker_smoke.py` is excluded deliberately: it boots a real
`uvicorn --workers 2` that inherits the production `.env` and leaves orphaned
workers holding production `scheduler_locks`. It fails identically on a clean
main (Session 06, observation 6656).

After the full-suite run the orphan check from
`local-app-runs-steal-prod-locks` was performed: the only
`--multiprocessing-fork` python process on the machine belonged to a live
Serena MCP server (parent alive, uv's Python 3.13, not the project venv). No
test process was left holding a production `scheduler_locks` row.

## 6. Onboarding runbook

1. Apply `migrations/078_multispecialty_plan.sql` (additive, re-runnable,
   `lock_timeout = 5s`).
2. Deploy the application.
3. Owner console → Create Client → **Multi-Specialty Hospital**.
4. Clinic admin at `/hospital-panel`: departments and doctors as usual, then
   Treatments → pick a starter list → price and edit → "Show to patients".
   Repeat per specialty. Nothing reaches a patient until that last step.

Rollback: application first, then `migrations/rollback/078_down.sql`, which
refuses while any clinic is still on the plan. No data loss — 078 created no
schema.

## 7. Open items

- The patient menu sits at 8 of Meta's 10 rows for this plan. Two rows of
  headroom, and there is no search box in a WhatsApp list to fall back on —
  `test_menu_fits_the_whatsapp_list_limit` guards it.
- No starter treatments are seeded at onboarding for this plan. If owners ask
  for it, the create-clinic request would need a `specialties: list[str]`
  field; deliberately not built on speculation.
