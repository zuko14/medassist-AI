# Kriya Pharmacy OS — Security, Roles, Compliance

> Part of the Pharmacy OS set: start at [status.md](status.md). Code evidence: [AUDIT.md](AUDIT.md) §5. Platform
> model: `docs/agent-context/08-AUTH-AND-MULTI-TENANCY.md`, `10-SECURITY-MODEL.md`.

## 1. Threat model (what we defend against)
| # | Threat | Control |
|:-|:-|:-|
| T1 | Cross-tenant read/write (clinic A sees clinic B's stock, patients, registers) | composite `(clinic_id,id)` FKs; `scoped_query`; `_rx_scope`; AST lint DG1; adversarial matrix DG2 |
| T2 | Staff in branch/ward X acting on store Y | store scope (§3.3) → 404 |
| T3 | Selling expired / recalled stock (by bug or intent) | DB trigger lock (ADR-RX-02) independent of API |
| T4 | Stock theft hidden by edits | append-only ledger, function-only writes, audit variance with second approver |
| T5 | Narcotic diversion | `RX_CONTROLLED`, two-person transfers, per-unit dispensing, running balance, daily controlled audit, hash-chained register |
| T6 | Register tampering before inspection | append-only + hash chain + nightly verify + export footer shows status |
| T7 | Price manipulation above MRP / unauthorised discount | MRP CHECK, server re-pricing, discount cap by permission, audit log |
| T8 | AI giving clinical advice through pharmacy features | zero-LLM boundary (ADR-RX-06), AST test |
| T9 | PHI leakage through logs, receipts, WhatsApp, exports | masking, minimum fields, consent gate (§5) |
| T10 | Double charging a patient (retries, races) | idempotency keys, `uq_ipd_charges_source_ref`, `uq_rx_sm_ref` |
| T11 | Module affecting non-pharmacy clinics | `rx_access` gate before any read; DG5/DG7 |

## 2. Permissions
| Permission | Grants |
|:-|:-|
| `RX_READ` | view catalogue, stock, sales, ledger, alerts, dashboard (no cost/margin) |
| `RX_DISPENSE` | POS drafts/complete, returns within window, indent dispense, refill queue |
| `RX_PURCHASE` | suppliers, PO, GRN, supplier returns |
| `RX_STOCK` | transfers, write-offs, stock audits (count/review/post under threshold) |
| `RX_CONTROLLED` | sell/dispense/transfer/receive Schedule X, narcotic, psychotropic items; controlled audits |
| `RX_REGISTER_EXPORT` | view, verify and export statutory registers |
| `RX_ADMIN` | settings, stores, item master, price/discount override, voids, returns past window, audit second approval, margin/cost views, opening stock |

`clinic_admin` and `super_admin` (scoped to one clinic) hold all RX permissions while `rx_access` is not None —
same rule as `holds_order_permission` (`app/services/permissions.py`). `RX_*` names join the permission catalogue
in `permissions.py`; the staff grant cap (a staff user cannot grant what they do not hold) applies unchanged.

## 3. Roles
### 3.1 New staff roles (added to `STAFF_ROLES`, presets in `ROLE_PRESETS`)
| Role | Preset |
|:-|:-|
| `PHARMACIST` | RX_READ, RX_DISPENSE, RX_REGISTER_EXPORT, ORDERS_READ |
| `PHARMACY_MANAGER` | RX_READ, RX_DISPENSE, RX_PURCHASE, RX_STOCK, RX_REGISTER_EXPORT, RX_CONTROLLED, RX_ADMIN |
| `STORE_KEEPER` | RX_READ, RX_PURCHASE, RX_STOCK |

`RX_CONTROLLED` is **not** in the PHARMACIST preset: the clinic names its controlled-drug custodians explicitly
(usually the chief pharmacist) — a deliberate act, audited.

### 3.2 Existing roles (role-implied grants, only while `rx_access` is ACTIVE/WIND_DOWN)
| Role | Implied | Why |
|:-|:-|:-|
| `PHARMACY_OPERATOR` (exists, empty preset — AUDIT A36) | RX_READ, RX_DISPENSE | logins already created for counter staff work without re-saving |
| `NURSE` (holds IPD_NURSING) | none new; IPD_NURSING itself opens indent/ward-issue/receive routes | no stock-wide visibility for nurses |
| `CASHIER` | RX_READ | sees pharmacy totals in shift close |
| `DOCTOR` | none | doctors prescribe in OPD/IPD; they do not touch stock |

Implied grants live in `_RX_GRANTS_IMPLIED_BY_ROLE` and are evaluated by `holds_rx_permission`; they are **not**
written to `clinic_admins.permissions` (no data migration, nothing to roll back).

### 3.3 Store scope
`allowed_store_ids(user)`:
1. clinic_admin / super_admin → all stores of the clinic.
2. Staff with `RX_STORE:<uuid>` entries in permissions → exactly those stores.
3. Staff with `branch_id` → stores of that branch.
4. Nurse acting through IPD routes → the sub-store of the admission's ward (+ the indent's `from_store_id` for read).
5. Otherwise all stores of the clinic.
A row outside scope returns **404**.

### 3.4 Permissions matrix (abridged; full matrix is `tests/test_rx_route_matrix.py`)
| Action | PHARMACIST | PHARMACY_MANAGER | STORE_KEEPER | NURSE | CASHIER | DOCTOR | clinic_admin |
|:-|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| View stock | ✓ | ✓ | ✓ | ward sub-store via IPD | ✓ | — | ✓ |
| POS sale (non-controlled) | ✓ | ✓ | — | — | — | — | ✓ |
| POS sale Schedule X / narcotic | only with RX_CONTROLLED | ✓ | — | — | — | — | ✓ |
| Discount ≤ cap | ✓ | ✓ | — | — | — | — | ✓ |
| Discount > cap / void sale | — | ✓ | — | — | — | — | ✓ |
| Raise indent | — | — | — | ✓ | — | — | ✓ |
| Dispense indent | ✓ | ✓ | — | — | — | — | ✓ |
| GRN post | — | ✓ | ✓ | — | — | — | ✓ |
| Transfer dispatch / receive | — | ✓ | ✓ | receive into own ward | — | — | ✓ |
| Stock audit post (≤ threshold) | — | ✓ | ✓ | — | — | — | ✓ |
| Audit 2nd approval | — | ✓ | — | — | — | — | ✓ |
| Item master edit | — | ✓ | — | — | — | — | ✓ |
| Register export | ✓ | ✓ | — | — | — | — | ✓ |
| Settings | — | ✓ | — | — | — | — | ✓ |
| Margin / cost reports | — | ✓ | — | — | — | — | ✓ |

## 4. Schedule H / H1 / X and narcotic controls
- **Sale preconditions** (enforced in `rx_complete_sale`, not only in UI):
  - H, H1: prescriber registration number present (in-house signed Rx or external Rx captured); patient name.
  - H1: additionally patient address line (snapshot); quantity ≤ prescribed when linked to an in-house Rx line.
  - X / narcotic / psychotropic: in-house signed Rx **or** a captured external Rx with prescriber registration and
    Rx date ≤ `rx_validity_days`; actor holds `RX_CONTROLLED`; per-unit quantities; no OTC; no refill plans.
- **Custody:** controlled transfers need dispatcher ≠ receiver (both RX_CONTROLLED); controlled audit scope available
  daily; register running balance must equal `rx_stock_balances` for that item×store (nightly check; mismatch → P1).
- **Trail:** every controlled movement has a register row in the same transaction; voids create `return` rows.
- **Print:** the Rx copy retention for Schedule X (duplicate copy) is a physical process — the POS prompts "Retain
  prescription copy" and records `rx_copy_retained = true` in the register snapshot.

## 5. DPDP Act 2023 and PHI
- **Purpose limitation.** Pharmacy uses patient identity for dispensing, billing, statutory registers and (only with
  consent) refill reminders. Refill reminders are a distinct purpose recorded in `consent.py` (`purpose='pharmacy_refill'`);
  absent consent → no plan is messaged.
- **Data minimisation.** Receipts print name, age/gender, MRN, prescriber name — never phone or full address.
  Registers include address only where the law asks (H1/X). Ketu and dashboards show masked phones.
- **Logs.** Phones masked `+91XXXXXX7890` (CLAUDE.md); no drug names alongside phone numbers in application logs
  (log the sale id instead).
- **Rights.** Patient data export (existing DPDP export route) gains pharmacy sales and refill plans; erasure requests
  **cannot** delete statutory register rows or GST invoices within retention — the response explains the legal
  retention basis and the rows are kept (DPDP s.8(7) allows retention required by law); refill plans and consents are
  deleted.
- **Retention.** Registers and sales ≥ the longest statutory requirement configured for the clinic's state (default
  setting 8 years, covering GST record retention); stock movements kept for the life of the account.
- **Breach.** Same incident runbook (`docs/runbooks`); pharmacy tables are in the PHI inventory.

## 6. Purge and deletion
`rx_purge_clinic` runs only from the platform owner's account-deletion flow (AUDIT A38) after the owner confirms the
statutory-retention notice (UI checkbox, logged). No clinic-level route deletes register rows, movements or sales.

## 7. Zero-LLM clinical firewall (restated)
`tests/test_rx_no_llm.py` fails if any RX module imports an LLM path (`app.services.ai_engine`, `ai_gateway`,
`hybrid_search`, `openai`, `groq`, `httpx` calls to an LLM host via `app.services.ai_*`). Refill reply parsing is
keyword matching. The firewall (`clinical_firewall.py`) remains the guard for patient chat; pharmacy adds no LLM
surface to it.

## 8. Web hardening (inherits platform)
CSP of `admin/index.html` unchanged (no new external origins; receipt printing uses `window.print()` on a same-origin
page); barcode scanner input is keyboard-wedge (no device APIs); PDF generation server-side; CSV exports prefix
cells beginning with `= + - @` with `'` (formula-injection guard).
