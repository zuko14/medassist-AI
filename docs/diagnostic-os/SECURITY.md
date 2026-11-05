# Kriya Diagnostic OS — Security, Privacy and Compliance

> Part of the Diagnostic OS set: start at [status.md](status.md). Baseline rules: AGENTS.md §1 (RLS bypassed — tenancy in
> code; `sb()` for every query; HTTP/1.1 patch untouched; webhook SLA; zero-LLM clinical firewall; no platform WhatsApp
> fallback; IST) and `docs/agent-context/10-SECURITY-MODEL.md`.

## 1. Assets and threats
| Asset | Threat | Control |
|:-|:-|:-|
| Results, reports (PHI) | cross-tenant read via a forged id | `scoped_query` / `.eq("clinic_id")` on every read (AST linter), composite FKs, functions treat foreign ids as not found (404) |
| | cross-site read by a branch-pinned user | site scope (§4) on visits, samples, batches, worklists, releases |
| | partner (hospital) reading another account's patients | confinement + account filter on every partner query (§5) |
| | public link guessing / brute force | 128-bit tokens stored hashed, expiry, last-4 phone check, 5 attempts, rate limit per IP and per token (§6) |
| | report forgery (fake lab reports) | verify code + SHA-256 on the public verify page |
| Signed result integrity | silent edit after signing | 118 triggers (unchanged), release guard, append-only verifications/critical events |
| | signature over values not reviewed | `expected_updated_at` on verify and approve; gate trigger requires verification of the *current* version |
| | technician self-verifying | four-eyes CHECK in `dx_verify_result` when levels = 3 |
| Money | double charge / lost payment | OPD ledger invariants; DX bill = Σ active items (DB-checked line guard); B2B ledger append-only, unique charge per item, idempotency keys |
| | B2B over-credit | hold check inside `dx_register_visit` under account row lock |
| Sample identity | wrong patient on a tube | barcode unique per clinic; labels print patient name + visit; receipt by scan only; rejection reason `MISLABELLED` |
| Catalogue | malicious formula / template | AST-whitelisted formula grammar; template JSON schema, no HTML, assets under the clinic prefix only |
| Uploads | malware / oversized files | type sniffing (magic bytes), size caps, images re-encoded (Pillow) before storage, PDFs only rasterised server-side for cloning |
| Bridge | spoofed instrument results | per-agent key, HMAC-SHA256 over method+path+timestamp+body, 300 s window, nonce/`message_id` idempotency, results only *prefill* or enter as unsigned (human still verifies/approves) |
| Assistants | prompt injection writing clinical data | Ketu has no DX write action; LLM never sees result values (ADR-DX-10) |
| WhatsApp | report sent from another clinic's number | clinic's own credentials only (`_get_credentials`, no platform fallback); release send refused when delivery owner ≠ `dx` |

## 2. Entitlement
`dx_access(clinic)` is checked on every `/admin/dx/*` route (server-side); the sidebar is cosmetic. Partner accounts
(`account_type='corporate_partner'`) are never eligible. The enterprise wildcard never enables DX.

## 3. Permissions and roles

### 3.1 Permissions (added to `app/services/permissions.py`, DX block)
| Permission | Allows |
|:-|:-|
| `DX_READ` | read visits, stages, dashboards (role-filtered fields) |
| `DX_REGISTER` | register patients/visits, add/cancel items, referrers (create) |
| `DX_BILLING` | DX bills, receipts, refunds, payment links (on the user's open shift) |
| `DX_COLLECT` | collection queue, labels, bind, collect, reject at collection |
| `DX_LOGISTICS` | batches create/add/dispatch |
| `DX_RECEIVE` | receive batches/samples, reject at receipt, storage |
| `LAB_ENTRY` (existing) | result entry, attachments, instrument mapping |
| `DX_VERIFY` | technical verification / return, critical-call records |
| `LAB_VALIDATE` (existing) | approval and signing (doctor-linked login with registration required) |
| `DX_DISPATCH` | releases, print, resend, share links |
| `DX_CATALOGUE` | test master edits, components, seed/import |
| `DX_CATALOGUE_APPROVE` | approve test definitions (lab director) |
| `DX_B2B_MANAGE` | accounts, rate cards, ledger, statements, payments, holds, partner logins |
| `DX_REPORTS_MIS` | MIS and exports |
| `DX_QC` | QC materials and results |
| `DX_ADMIN` | setup, sites, settings, templates, routing, instruments (site-limited for pinned users) |

`clinic_admin` and scoped `super_admin` hold all (house convention), **except** that `LAB_VALIDATE` still needs a
doctor link (118 rule) — an administrator cannot sign a report.

### 3.2 Roles (presets apply at account creation only; implied grants respect the staff grant cap fixed in Session 51)
| Role | Preset | Implied by role (works for logins saved before DX) |
|:-|:-|:-|
| `FRONT_DESK` / `RECEPTIONIST` (existing) | + `DX_REGISTER`, `DX_READ` when created on a DX clinic | none (existing logins gain nothing) |
| `CASHIER` (existing) | + `DX_BILLING`, `DX_READ` | none |
| `DIAGNOSTIC_OPERATOR` (existing) | + `DX_REGISTER`, `DX_COLLECT`, `DX_DISPATCH`, `DX_READ` | `DX_READ` |
| `LAB_OPERATOR` (existing = technician) | + `DX_RECEIVE`, `DX_READ` | `DX_READ`, `DX_RECEIVE` |
| `PATHOLOGIST` (existing, doctor-linked) | + `DX_VERIFY`, `DX_READ` | `DX_READ`, `DX_VERIFY` (LAB_VALIDATE already implied) |
| `PHLEBOTOMIST` (existing, confined) | — | `DX_COLLECT` **only** on the home-collection samples route of its own visits |
| **new** `LAB_SUPERVISOR` | `LAB_ENTRY`, `DX_RECEIVE`, `DX_VERIFY`, `DX_READ` | same |
| **new** `LOGISTICS_RUNNER` | `DX_LOGISTICS`, `DX_READ` (confined nav: Logistics only) | same |
| **new** `PROCESSING_ADMIN` | `DX_ADMIN`, `DX_RECEIVE`, `LAB_ENTRY`, `DX_VERIFY`, `DX_DISPATCH`, `DX_READ` — **must be branch-pinned**; no billing, no B2B money | same |
| **new** `B2B_MANAGER` | `DX_B2B_MANAGE`, `DX_READ` | same |
| **new** `B2B_PARTNER` | confined to `/admin/dx/partner/*`; requires `dx_b2b_account_id` | — |

## 4. Site scope and data minimisation
- A branch-pinned user may read a visit iff its `branch_id` is theirs **or** any of its samples has
  `processing_site_id` / `received_site_id` / `collection_site_id` at their branch's site. Writes are allowed only for
  the stage that happens at their site (e.g. a processing-site technician cannot edit the bill; a collection-site
  desk cannot approve). Implemented in `app/routers/dx.py::_visit_for(user, …)` and in every list query's filter.
- Field filtering by permission in read models:

| Field group | Who sees it |
|:-|:-|
| name, age, sex, MRN, visit no., tests, sample IDs, clinical notes, fasting | everyone with DX_READ at an involved site |
| phone, address, e-mail, external ref | DX_REGISTER, DX_BILLING, DX_DISPATCH, DX_COLLECT (collection site), B2B_MANAGE |
| money (prices, bill, dues, ledger) | DX_BILLING, DX_B2B_MANAGE, DX_REPORTS_MIS (revenue) |
| result values | LAB_ENTRY, DX_VERIFY, LAB_VALIDATE, DX_DISPATCH (via the released PDF), partner (released only) |

Tests assert the absence of `phone` for a processing-site technician and of values for a front-desk login.

## 5. Partner (B2B_PARTNER) confinement
- `verify_credentials` confines the role to `/admin/dx/partner/*` (403 elsewhere, including `/fhir/*`), mirroring the
  PHLEBOTOMIST allowlist; deactivating the login or the account takes effect on the next request.
- Every partner query adds `.eq("b2b_account_id", user.dx_b2b_account_id)` after the clinic filter; the account id is
  never taken from the request. Release PDFs are served only when the account's `report_delivery` includes `account`.
- Rate-limit partner visit creation (e.g. 120/min per login) and log every PDF download.

## 6. Public endpoints
| Route | Returns | Protections |
|:-|:-|:-|
| `GET /public/dx/verify/{code}` | lab display name, release date, release no., test names, status (current/superseded), SHA-256, patient initials + age/sex | 10-char base32 code (50 bits) + rate limit 30/min/IP; no values, no phone, no full name |
| `GET /public/dx/r/{token}` | a page asking for the last 4 digits of the registered phone | token 128-bit random, stored as SHA-256, expiry (default 72 h), revocable |
| `POST /public/dx/r/{token}/unlock` | the stored PDF (streamed, `Cache-Control: no-store`) | constant-time compare, max 5 attempts then locked, rate limit per IP, audit row per attempt |

Both run without a session and must not import admin auth. CSP and security headers follow the existing public routes.

## 7. Storage
Bucket `dx-assets` (templates, signatures, attachments) and the existing lab-report bucket for released PDFs; object
keys always start with `<clinic_id>/`; the service builds keys itself (never from user input); signed URLs short-lived;
signature images are never served outside a rendered PDF.

## 8. Audit and evidence
`log_admin_action` on every write and every clinical read (worksheet, result, release PDF, partner download, MIS
export) with resource ids, never values. `dx_events` gives the per-visit timeline (who collected, received,
verified, approved, released, printed). NABL-relevant evidence available from data: rejection log with reasons,
critical-call log with read-back, TAT per test, amendment reasons, verification/approval signatories, QC runs.

## 9. DPDP Act 2023
Purpose-bound collection (registration consent reused from the registry); report copies to referrers need the
patient's consent flag on the visit; erasure = redact-in-place for clinical records (DATA-MODEL §11), stored PDFs
deleted; retention periods are data (O-DX-10); partner accounts see only their patients; exports are logged.

## 10. Invariants (must hold even for a buggy future caller — each has a real-Postgres test)
1. A DX order belongs to exactly one visit and one item; never billed through `opd_invoice_items.order_id`.
2. A visit's patient bill total = Σ (price − discount) of its active items; at most one live bill per visit.
3. A barcode identifies at most one sample per clinic; a collected sample's barcode never changes.
4. A sample is in at most one live batch.
5. A DX result cannot be validated on a 3-level clinic without a verification of its current version.
6. A validated/amended result never changes (118); a release's content never changes; a published template never changes.
7. A B2B ledger row is never updated or deleted (except owner purge); one charge per accession item; balance derived.
8. Every `dx_*` state column changes only inside `dx_*` functions (`kriya.dx_tx` guard).
