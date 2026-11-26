# Kriya Continuous Care — Kriya Ketu (staff copilot) integration

> Ketu actions are registered in `app/assistant/registry.py` (`REGISTRY`, AUDIT A33) and checked by
> `tests/test_assistant_registry.py`. CC adds **read (R0) actions only**; anything that changes data navigates the
> user to the CC page with the form pre-filled (H1 hand-off), exactly like the RX/DX Ketu integrations.

## 1. Read actions (R0, deterministic handlers, session RBAC + branch pin)
| Action | Staff phrases (en / hi / te examples) | Permission | Returns |
|:-|:-|:-|:-|
| `cc.radar_today` | "who is overdue today", "aaj kaun follow-up se chhoot gaya" | CC_VIEW | top 10 radar rows with reasons |
| `cc.urgent_cases` | "any urgent care cases?" | CC_VIEW | open urgent cases with SLA left |
| `cc.patient_status` | "Lakshmi's sugar this week", "MRN 1043 care status" | CC_VIEW | last readings, adherence %, tier and why, next due items |
| `cc.inbox_count` | "how many patients need my approval" | CC_APPROVE | count + link to inbox |
| `cc.outcomes_month` | "care programme results this month" | CC_VIEW | headline outcome numbers with method version |
| `cc.not_responding` | "who has not replied to care messages" | CC_VIEW | list with channel and last attempt |

## 2. AI summary (the only AI use touching CC data — ADR-CC-02 item 2)
`cc.patient_summary` ("summarise Ravi's last 30 days"): Ketu's existing LLM path receives **only** rows the user can
already see on the timeline (readings with dates, adherence counts, tiers with reasons, actions) — no free text from
the patient (there is none), no phone, no address. Output is labelled *AI summary — check the record*, shown in the
Ketu panel only, never stored, never sent to a patient, never used by the protocol engine. Off unless the clinic has
Kriya Ketu enabled (`kriya_ketu_enabled`).

## 3. Hand-offs (H1)
"Enroll Lakshmi in diabetes care" → opens Patients in care with the enroll form pre-filled (doctor signs).
"Book review for this case" → opens the doctor inbox card. Ketu never performs either.

## 4. Drift
Each new CC page and route is added to Ketu's page table in the same commit; `test_assistant_registry.py` fails
otherwise.
