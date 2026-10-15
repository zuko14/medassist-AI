# Kriya IPD OS × Kriya Ketu — Integration Architecture and Status

Ketu architecture: [docs/kriya-ketu/architecture.md](../kriya-ketu/architecture.md) · registry rules:
[action-registry-spec.md](../kriya-ketu/action-registry-spec.md) · Ketu status: [docs/kriya-ketu/status.md](../kriya-ketu/status.md).

## 1. How Ketu reaches IPD
Ketu never touches IPD tables or services. It dispatches the **same `/admin/ipd/*` routes** a person's browser
calls, in-process, with that person's session (ADR-004 in the Ketu docs). So `_ipd_scope()` — entitlement,
tenant, permission, wind-down, branch — and the audit log apply unchanged, and a permission revoked after a preview
is enforced at execution.

```mermaid
flowchart LR
  U[Staff types / speaks<br/>"kitne bed khali hain"] --> R[rules.py: L1 nav / L2 triggers]
  R -->|ipd.census| P[policy.py: permission IPD_* + feature ipd_access]
  P --> E[executor: GET /admin/ipd/census as the user]
  E --> G[_ipd_scope] --> S[services/ipd.census]
  S --> C[present.metrics card + link to Bed Board]
```

## 2. What is registered (`app/assistant/registry.py`, block `IPD_ACTIONS`)
| Action | Tier | Route / page | Permission (handler) | Example triggers (en / hi-Latin / te / hi) |
|---|---|---|---|---|
| `nav.open` → Bed Board, Admissions, Inpatient Chart, IPD Setup | R0 | client `go(page)` | page visibility | "bed board", "beds", "admission", "nursing station", "बेड", "బెడ్లు", "भर्ती" |
| `ipd.census` | R0 read | `GET /admin/ipd/census` | any IPD permission | "free beds", "occupancy", "khali beds", "ఖాళీ బెడ్లు", "खाली बेड" |
| `ipd.inpatients` | R0 read | `GET /admin/ipd/admissions?status=admitted,discharge_pending` | any IPD permission | "who is admitted", "ward patients", "भर्ती मरीज" |
| `ipd.waiting` | R0 read | `GET /admin/ipd/worklist` (awaiting_bed) | any IPD permission | "waiting for a bed", "pending admissions" |
| `ipd.discharges` | R0 read | `GET /admin/ipd/worklist` (discharge_pending) | any IPD permission | "pending discharges", "డిశ్చార్జ్", "डिस्चार्ज" |
| `ipd.admit` | H hand-off | opens Admissions; person presses **Admit patient** | — | "admit patient", "new admission" |
| `ipd.discharge` | H hand-off | opens Inpatient Chart; **Complete discharge** | — | "complete discharge", "discharge summary" |
| `ipd.vitals` | H hand-off | opens Inpatient Chart; **Save** | — | "record vitals", "nursing note", "medication given" |

All actions use `feature=_ipd_on` (`ipd_access(clinic) is not None`, i.e. the same gate as the routes) and are
checked by `tests/test_assistant_registry.py` (`validate` + route drift + RT-14 page table = sidebar).

## 3. What Ketu will not do (ADR-IPD-08)
- No R1–R3 IPD writes in v1 (no admit, transfer, bed status, order, MAR, note, charge, discharge through Ketu).
- No AI-generated clinical text stored anywhere; no AI discharge-summary drafting.
- No IPD data in LLM prompts: the L4 LLM fallback only picks an action id + slots from the user's text; the
  results are rendered by deterministic `present.*` functions.

## 4. Roadmap (each item needs the KK-P0-15 hardening checklist on its endpoint first)
| ID | Candidate | Tier | Preconditions |
|---|---|---|---|
| KK-IPD-1 | "Mark bed ICU1 clean" | R1 | bed-code resolver slot; verify = bed status read-back |
| KK-IPD-2 | "Reserve a general bed for <patient>" | R2 | patient resolver; preview shows the exact bed; confirm |
| KK-IPD-3 | Morning IPD briefing (occupancy, discharges due, waiting list) in `BRIEFINGS` | R0 | none beyond R0 reads |
| KK-IPD-4 | Discharge-readiness summary from the checklist | R0 | none — deterministic checklist read |
| KK-IPD-5 | Clinician-reviewed draft discharge summary | — | clinical governance decision; draft-only workflow; out of v1 |

## 5. Status
| Item | State | Evidence |
|---|---|---|
| IPD pages in Ketu page table (RT-14) | VERIFIED | `tests/test_assistant_registry.py` — 153 passed |
| 4 R0 reads + 3 H hand-offs registered, validated, no route drift | VERIFIED | same |
| Boundary: no assistant code names IPD tables | VERIFIED | `tests/test_ipd_no_llm.py` |
| Live NL routing quality for IPD phrases (te/hi) | NOT_STARTED | needs `scripts/assistant_llm_smoke.py`-style eval set for IPD triggers |
| KK-IPD-1…5 | DEFERRED | §4 |
