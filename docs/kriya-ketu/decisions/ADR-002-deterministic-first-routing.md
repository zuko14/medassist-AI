# ADR-002 — Deterministic-first routing; OpenRouter as a one-shot classifier fallback

Status: **Proposed** · Date: 2026-10-13

## Context
Owner direction: use the LLM only as a fallback and automate workflows without it, to keep tokens and
cost down. The repo already proves this order in the voice receptionist (safety → rules → LLM only when
rules miss; `app/voice/nlu_rules.py` → `nlu_llm.py`). OpenRouter supports tool calling (a multi-turn
loop where the client executes tools) and structured outputs (`response_format.json_schema`, with strict
enforcement that varies by provider endpoint).

## Options
1. **LLM-first agent with tool calling**: the model plans and calls tools in a loop.
2. **LLM classifies every request** and rules only validate.
3. **Rules first (navigation → triggers → form completion); LLM only on a miss or conflict, as one
   structured-output call that names a registered action id.** Multi-step work comes from fixed
   playbooks.

## Decision
Option 3.

## Reasons
- Cost: most admin requests are a small set of repetitive commands ("today's appointments", "mark
  leave", "failed reports"). Rules handle them for ₹0 and in milliseconds.
- Safety: an LLM-planned multi-step write is hard to preview or bound. Fixed playbooks are reviewable and
  testable.
- One call with an enum-restricted schema caps both tokens (≈ 400 in, ≤ 200 out) and blast radius (it
  can only pick an action the user may already run, and policy + confirm still apply).
- Tool calling needs ≥ 2 round trips and hands the model control over sequencing. Nothing in v1 needs
  that.

## Consequences
- Phrasings that rules miss cost one LLM call. A weekly manual review promotes them into rules (no
  automatic learning in v1, DP-06).
- "Multi" requests are split by the user ("one at a time"), not by the model.
- Requires a ≥ 200-phrase multilingual corpus, maintained with every new action.

## Risks
- Rules may be brittle for free-form Telugu/Hindi phrasing, which raises the LLM share. Measured by
  `route_layer`; the target is ≥ 85 % deterministic.
- `strict` json_schema may not be honoured by every endpoint. Mitigated by `require_parameters` +
  Pydantic re-validation + the G-KK-LLM gate.

## Verification
RT-01..14, LLM-01..10, the G-KK-LLM gate, the pilot metric "deterministic route %".

## Revisit when
The LLM share stays above 25 % for a month after rule promotion, or the owner wants open-ended
multi-step automation (that would need a new ADR with an approval workflow).
