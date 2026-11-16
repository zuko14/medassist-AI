# Kriya Pharmacy OS — Gap Analysis (current Kriya vs hospital pharmacy requirements)

> Part of the Pharmacy OS set: start at [status.md](status.md). Evidence: [AUDIT.md](AUDIT.md). Each gap names the
> requirement, what exists today, the closing task(s) and the phase. "Out of v1" gaps carry a reason.

## 1. Summary
Kriya has the clinical **demand** side of pharmacy (structured, signed OPD prescriptions; IPD medication orders; MAR;
IPD charges with a `pharmacy` category; GST and credit-note machinery) and **none** of the supply side (item master,
batches, stock, purchase, sale, indent, transfer, registers). Every gap below is greenfield except G4, G9, G10 and
G13, which extend existing objects additively.

## 2. Gap register
| ID | Requirement | Today (evidence) | Gap | Closing tasks | Phase |
|:-|:-|:-|:-|:-|:-|
| G1 | Drug/item master with generic, strength, form, HSN, GST, schedule, pack | none; Rx drug names are free text (A4, A16); `billable_services` has no drug category (A27) | full | RX-12…RX-16 | P2 |
| G2 | Batch with expiry, MRP, cost | none | full | RX-17 | P2 |
| G3 | Stock per store, append-only, never negative | none | full | RX-18…RX-20 | P2 |
| G4 | Module gate, zero impact | pattern exists for OPD/IPD/DX (A34, A35) | new flag + state + routes | RX-02…RX-09 | P1 |
| G5 | Multi-store hierarchy | wards exist (`ipd_wards`), no stores | full | RX-05, RX-41…RX-44 | P1, P6 |
| G6 | Suppliers, PO, GRN, landed cost, purchase GST | none | full | RX-22…RX-27 | P3 |
| G7 | OPD POS with Rx load, FEFO, inclusive GST, receipts | Rx signed and structured (A1–A5); no sale object; OPD invoices tax-exclusive and one-per-visit (A7, A9) | full (new sales series) | RX-28…RX-36 | P4 |
| G8 | Free-text Rx → item mapping | none | deterministic resolver + learned aliases | RX-29 | P4 |
| G9 | Cashier shift includes pharmacy cash | shifts total only `opd_receipts` (A14) | additive summary | RX-34 | P4 |
| G10 | IPD indent → dispense → charge | medication orders not charged anywhere (A22); `ipd_charges` has `pharmacy` category (A20) but source CHECK lacks `pharmacy` | widen CHECK, new indent tables, atomic dispense | RX-37…RX-40 | P5 |
| G11 | MAR batch traceability (recalls) | MAR has no batch link (A19) | nullable FK column | RX-39 | P5 |
| G12 | Returns: patient & IPD, credit notes | OPD credit notes exist but bound to `opd_invoices` (A12) | `rx_returns`, `rx_credit_notes`; IPD path reuses `opd_issue_credit_note` | RX-35, RX-40 | P4, P5 |
| G13 | Supplier returns / debit notes | none | full | RX-26 | P3 |
| G14 | GST reporting incl. pharmacy | GSTR-1 export reads `opd_invoices` only (A13) | add pharmacy sections, HSN summary, purchase register | RX-52 | P10 |
| G14b | E-invoice / e-way bill | none | **out of v1** — B2C counter sales do not need them; revisit for B2B supply | — | — |
| G15 | Offline POS | none (web app) | **out of v1** — conflicts with server-enforced stock and register guarantees; evaluate OTC-only queue | RX-D3 (deferred) | — |
| G16 | Accounts payable / Tally export | none | day-book CSV in v1; AP ledger out of v1 | RX-53 | P10 |
| G17 | Distributor invoice import | none | CSV/XLSX → GRN draft (stretch) | RX-54 | P10 |
| G18 | Physical stock audit | none | full | RX-45…RX-47 | P6 |
| G19 | Schedule H/H1/X/narcotic registers | prescriber reg. no. is captured in signed Rx (A2), nothing else | full (hash-chained) | RX-48…RX-51 | P7 |
| G20 | Low-stock / near-expiry alerts | scheduler + `admin_notifications` exist | new jobs | RX-56 | P8 |
| G21 | Chronic refill reminders | only patient-entered dose reminders (A45) — unrelated | new plans/requests + template + keyword replies | RX-57…RX-59 | P8 |
| G22 | Staff stock lookup by voice/chat | Ketu exists (A46, A47) | R0 actions + resolver | RX-60, RX-61 | P9 |
| G23 | Roles & permissions | `PHARMACY_OPERATOR` exists, empty (A36) | RX perms, 3 roles, implied grants, store scope | RX-04 | P1 |
| G24 | Admin UI | nav groups/loaders pattern (A40–A42) | 11 pages | RX-11, RX-16, RX-21, RX-27, RX-36, RX-40, RX-44, RX-47, RX-51, RX-59, RX-62 | P1–P9 |
| G25 | Opening stock & legacy cutover | none | import + runbook | RX-20, RX-64 | P2, P11 |
| G26 | Credit/on-account sales | none | **deferred** (owner O-1) | RX-D2 | — |
| G27 | Home delivery logistics | home collection exists for lab (`home_collection.py`) | **pickup only in v1** (owner O-6) | RX-D4 | — |
| G28 | Cold-chain temperature logging | none | **out of v1** (flag on item only; manual fridge log) | RX-D5 | — |
| G29 | Drug–drug interaction checking at dispense | none | **out of v1 by design** — clinical decision support belongs to the prescriber's OPD/IPD workflow, not the counter; adding it to pharmacy would put clinical judgement in a non-clinician flow (ADR-RX-06) | — | — |
| G30 | Standalone retail pharmacy (no OPD) | eligibility requires OPD (ADR-RX-09) | **out of v1** — needs patient registry, shifts and counters without OPD | RX-D6 | — |

## 3. Risks introduced by closing the gaps
| Risk | Mitigation |
|:-|:-|
| Widening `ipd_charges.source` CHECK breaks a reader | grep shows only 115 reads `source='order'` (A22); RG5 unedited |
| New trigger on `ipd_orders` adds latency to order end | early-exit indexed EXISTS; measured in RG7/P11 latency check |
| `close_shift` change alters cash totals | additive keys; existing keys asserted unchanged for non-RX clinics |
| Register purge during account deletion destroys statutory records | owner purge only, with retention acknowledgment (SECURITY §6) |
| GST rates change again by notification | rates are item data + per-line snapshot; `rx_item_changes` logs who changed what |
| Legacy 12% batches after the 2025 rationalisation | 12% kept in the CHECK for old stock, returns of pre-change bills and credit notes; item master defaults to the current rate on import (ROLLOUT §4.2) |
