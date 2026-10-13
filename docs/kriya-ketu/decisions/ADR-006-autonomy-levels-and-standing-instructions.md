# ADR-006 — Autonomy levels: one instruction runs a whole job; standing instructions are staged

Status: **Proposed** · Date: 2026-10-14

## Context
Owner direction (2026-10-14): staff should *only give the instruction* and Kriya Ketu should manage
everything: registering patients, setting up outbound receptionist calls, correcting catalogues, editing
treatments and prices. It should be simpler than competitors and safe for production. At the same time
the repo has hard constraints: tenant isolation in app code, NMC (no AI clinical decisions), DPDP
consent, Meta's 24 h window, outbound-call consent and window rules (`app/voice/outbound.py`), and payment
correctness (Session 20 incidents).

## Options
1. **Fully autonomous agent**: the LLM plans and executes without confirmation.
2. **Per-step confirmation**: safe, but every multi-step job needs 3–6 taps, which is not "just give the
   instruction".
3. **Levelled autonomy**:
   - **L1 Ask** (R0): answer immediately.
   - **L2 Do**: one action, one confirmation scaled to risk.
   - **L3 Run a job**: a deterministic playbook, **one confirmation for the whole plan** with a complete,
     bounded preview; each write verified; stops and re-asks if reality diverges from the preview.
   - **L4 Standing instructions**: "every day at 11, call yesterday's hot leads", "every morning brief me".
     Staged:
     - **L4a (v1)**: (i) switch the **built-in automations** that already exist on or off (`outbound.auto_leads`
       hourly job, its cap and window); (ii) scheduled **read-only** briefings delivered to the existing
       admin notifications.
     - **L4b (v2)**: scheduled **write** playbooks. Needs a non-interactive principal, because a 12 h
       session cannot back a recurring job.

## Decision
Option 3. Ship L1–L3 and L4a in v1. L4b needs this ADR's follow-up (ADR-006b) and owner approval.

## Reasons
- L3 gives the one-instruction experience: "call today's hot dermatology leads" is one sentence, one
  preview, one confirm, and it is done and verified. The confirmation is cheap and is what makes the
  automation trustworthy enough to use daily.
- Every L3 step is a registered action executed through the existing endpoint (ADR-004), so autonomy adds
  no new authority.
- L4a reuses automation that already exists and is tested (`autoqueue_leads`), instead of a second lead
  dialler.
- L4b would need the scheduler to act as a user without a live session. That means a new trusted
  internal-auth path into `verify_credentials`, the most security-critical function in the repo. It
  deserves its own design: per-rule principal re-read on every run (active, permissions, branch), per-run
  caps, daily digest of what ran, automatic pause on any failure, and owner opt-in per clinic.

## Consequences
- New playbooks are code and reviewed, not prompts. New automations need a release, by design.
- The scheduled briefing needs one new scheduler job + `admin_notifications` writes through the existing
  `broadcast_service` helpers (to verify: they support per-admin targeting).

## Risks
- Users may expect L4b in v1. The spec states it plainly as v2.
- A one-confirmation plan hides detail if previews are poor. So previews must list every write with
  counts, and anything over 10 needs a typed count.

## Verification
Playbook tests (fake API + real-route fixtures), divergence test (EXE-14), and, for L4a, a settings
read-back plus the existing `autoqueue_leads` tests.

## Revisit when
The pilot shows staff confirm the same playbook daily without changes. That is the signal to design
L4b for that playbook first.
