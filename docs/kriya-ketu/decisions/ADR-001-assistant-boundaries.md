# ADR-001 — Kriya Ketu stays separate from the patient-facing agents

Status: **Proposed** (Phase 0) · Date: 2026-10-13

## Context
Kriya has two patient-facing agents: the WhatsApp FSM (`app/services/conversation.py`, entered via
`POST /webhook`) and the voice receptionist (`app/voice/`, its own `KriyaTools` with verified writes).
Both act for a **phone number** with self-service authority only. Kriya Ketu acts for an
**authenticated staff member** with that person's delegated permissions. The brief asks for shared
infrastructure where sensible, without merging identities.

## Options
1. **Extend the voice `KriyaTools` / dialog engine** to serve staff too. It already has rules → LLM
   order and read-back proofs.
2. **Route staff requests through the WhatsApp FSM** with an "admin mode".
3. **A separate package (`app/assistant/`)** that executes via the existing `/admin` endpoints, and
   borrows only *patterns* and pure helpers (`app/voice/dates.py`) from voice.

## Decision
Option 3.

## Reasons
- `KriyaTools` derives authority from `CallContext` (clinic + caller phone) and writes as the patient.
  Giving it staff powers would put admin capabilities one bug away from a phone caller.
- The FSM is the most regression-sensitive code in the repo (Meta 20 s SLA, phone locks, consent). An
  admin mode inside it would put patient traffic at risk for an internal feature.
- The `/admin` endpoints already encode staff authority (`verify_credentials`, `require_permission`,
  branch scope, OPD role confinement). Reusing them is the shortest path to identical rules.

## Consequences
- Some duplication of *pattern*, not logic: the assistant has its own rules module instead of
  `nlu_rules.py`, because the intents differ (admin operations versus a patient booking dialog).
- `tests/test_assistant_boundaries.py` enforces no imports between `app/assistant` and
  `conversation.py` / `app/voice/dialog.py` / `app/voice/tools.py`.
- Separate `task_type` values keep cost and budgets separate.

## Risks
Helpers imported from `app/voice/` (dates, lexicon data) could change for voice reasons and shift the
assistant's behaviour. Mitigation: the corpus tests run in CI.

## Verification
The boundary test; a code review checklist item; the SEC matrix.

## Revisit when
A third internal channel (e.g. an owner console assistant) needs the same registry. Then extract
`app/assistant/registry.py` into a shared module, still under staff auth only.
