# Kriya Diagnostic OS × Kriya Ketu, Voice Receptionist and WhatsApp

> Part of the Diagnostic OS set: start at [status.md](status.md). Ketu architecture: [docs/kriya-ketu/architecture.md](../kriya-ketu/architecture.md);
> registry rules: [action-registry-spec.md](../kriya-ketu/action-registry-spec.md); IPD precedent:
> [docs/ipd-os/KRIYA-KETU.md](../ipd-os/KRIYA-KETU.md). Delivered in **P9** (registry entries can land with each page).

## 1. Kriya Ketu (staff assistant)
Ketu dispatches the **same `/admin/dx/*` routes** in-process with the speaking user's session, so `_dx_scope`,
site scope, field filtering and audit apply unchanged. Results are rendered by deterministic `present.*` functions;
the L4 LLM fallback picks only an action id and slots and never sees DX data (ADR-DX-10).

| Action | Tier | Route / page | Permission | Example triggers (en / hi-Latin / te / hi) |
|:-|:-|:-|:-|:-|
| `nav.open` → Registration, Sample Collection, Logistics, Lab Worklist, Approval, Reports & Dispatch, Test Master, B2B Accounts, DX Dashboard, DX Setup | R0 | client `go(page)` | page visibility | "registration", "sample collection", "lab worklist", "reports dispatch", "टेस्ट रजिस्ट्रेशन", "శాంపిల్ కలెక్షన్" |
| `dx.pending_by_stage` | R0 read | `GET /admin/dx/dashboard` | DX_READ | "pending samples", "kitne samples pending hain", "ఎన్ని శాంపిల్స్ పెండింగ్", "कितने सैंपल बाकी" |
| `dx.tat_breaches` | R0 read | `GET /admin/dx/worklist?stage=…&overdue=true` | DX_READ | "TAT breach", "late reports", "der wale reports" |
| `dx.my_approvals` | R0 read | `GET /admin/dx/approval/queue` | LAB_VALIDATE | "reports waiting for my signature", "mere sign ke liye", "నా సంతకం కోసం" |
| `dx.critical_open` | R0 read | `GET /admin/dx/critical/open` | DX_READ | "critical values", "critical results today" |
| `dx.find_visit` | R0 read | `GET /admin/dx/visits?q=` (slot: name/phone/visit no.) | DX_READ | "find report of Ramana Babu", "LAB-2026-123 status" |
| `dx.held_dues` | R0 read | `GET /admin/dx/releases?status=held_dues` | DX_DISPATCH | "reports held for payment" |
| `dx.today_summary` (briefing) | R0 read | `GET /admin/dx/dashboard` | DX_READ | "today's lab summary", morning briefing entry |
| `dx.register` | H hand-off | opens Registration with patient prefilled; person presses **Save & Bill** | — | "register a patient for CBC" |
| `dx.collect` | H hand-off | opens Sample Collection on the visit; **Collected** | — | "mark sample collected" |
| `dx.approve` | H hand-off | opens Approval on the result; **Approve & sign** | — | "sign this report" |
| `dx.resend` | H hand-off | opens Reports & Dispatch on the release; **Resend** | — | "resend report on WhatsApp" |

All actions use `feature=_dx_on` (`dx_access(clinic) is not None`) and must pass `tests/test_assistant_registry.py`
(validate, route drift, RT-14 page table = sidebar). Deferred writes (each needs the KK-P0-15 endpoint hardening
checklist first): KK-DX-1 "resend report R1 to patient" (R1, verify = delivery read-back), KK-DX-2 "release partial
report" (R2 with preview), KK-DX-3 "put account on hold" (R2). No clinical writes (entry, verification, approval)
through Ketu, ever, without a separate clinical-governance decision.

## 2. Voice receptionist (patients calling)
Existing: `app/voice/scope.py` already treats a diagnostic centre as tests-only (booking, prices, prep from the
catalogue). Additions (P9):
| Tool | Behaviour | Guard |
|:-|:-|:-|
| `report_status` | caller phone → their visits in the last 30 days → per visit: "ready and sent on WhatsApp at 11:05", "2 of 5 tests ready", "awaiting payment", "sample needs recollection" | **status only — never values, flags or interpretation**; caller must match the registered phone (or the existing voice identity check); family members by name confirmation |
| `resend_report` | re-sends the latest release to the registered WhatsApp number | only to the number on record; one per 10 min per visit; logged |
| `book_recollection` | for a rejected sample, offers a home visit / centre time using the existing booking tools | — |
Voice code calls DX through a small read service (`app/services/dx_status.py`) that returns enums and timestamps only;
`tests/test_dx_no_llm.py` asserts voice imports nothing that returns values.

## 3. WhatsApp (patient chat)
| Intent | Response |
|:-|:-|
| "report", "my report", "రిపోర్ట్", "रिपोर्ट" | same status summary as voice; button **Send my report** → re-sends the latest release document |
| payment due template reply | payment link (existing gateway flow) → on settlement the held release is queued |
| recollection template reply | book home collection / centre slot buttons |
| booking flows | unchanged (live) |
All replies go from the clinic's own number (`_get_credentials`, no platform fallback) and are in-session replies or
approved templates; the conversation FSM gets a new intent but no change to existing states (`tests/test_fsm_transitions.py` stays green).

## 4. Status
| Item | State |
|:-|:-|
| DX pages in the Ketu page table | NOT_STARTED (P9) |
| R0 reads + H hand-offs registered and validated | NOT_STARTED (P9) |
| Voice `report_status` / `resend_report` | NOT_STARTED (P9) |
| WhatsApp report-status intent | NOT_STARTED (P9) |
| Boundary test `tests/test_dx_no_llm.py` | NOT_STARTED (P1) |
