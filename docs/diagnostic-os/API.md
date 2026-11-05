# Kriya Diagnostic OS — API Map

> Part of the Diagnostic OS set: start at [status.md](status.md). Permissions are defined in [SECURITY.md](SECURITY.md) §3.
>
> Rules for **every** route below: `_dx_scope(user, clinic_id, *perms, new_work=…)` first (permission → tenant via
> `enforce_clinic_access` → `dx_access` → `WIND_DOWN` refusal for new work); then site scope; writes are audit-logged
> with `log_admin_action`; clinical reads (worksheets, results, report PDFs) are audit-logged; errors are mapped
> `dx_*`/`lis_*`/`opd_*` codes with fixed messages (never raw DB text); list endpoints page with `fetch_all` and
> `limit ≤ 200`; ids from the path are always re-checked against `clinic_id` (404, never 403, for foreign ids);
> request bodies are pydantic models with `extra="forbid"`; money in integer paise; times returned as ISO UTC and
> displayed in IST by the UI. `clinic_id` comes from the session (`?clinic_id=` only for super-admins, fail-closed).
> New work = creates a visit, site, test, batch, template, account, rate card.

Prefix `/admin/dx` unless stated. `NW` = refused in WIND_DOWN.

## P1 — Foundation
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET | `/setup` | DX_ADMIN | go-live checklist (sites, processing site, signatory with registration, ≥1 approved test, template, delivery owner per branch) |
| GET | `/settings` | DX_READ | returns `dx_settings` + `version` |
| PATCH | `/settings` | DX_ADMIN | CAS on `version` (409 on mismatch); pydantic-validated keys only |
| GET / POST / PATCH | `/sites`, `/sites/{id}` | DX_ADMIN (POST NW) | branch must be this clinic's; one site per branch |
| POST | `/go-live` | DX_ADMIN | refuses until checklist complete; sets `went_live_at` |
| GET | `/admin/me` (existing) | — | adds `dx_access`, `dx_state`, `dx_site_ids` |
| PATCH | `/platform/clinics/{id}/features` (existing) | owner | `feature=dx_enabled`; eligibility 400; disable → 409 preview / `confirm_disable` → WIND_DOWN |
| GET | `/platform/clinics/{id}/dx/deactivation-preview` | owner | counts: visits in process, samples in transit, unreleased results, B2B balance |

## P2 — Catalogue
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET/POST/PATCH | `/departments`, `/containers`, `/signatories` | DX_ADMIN (POST NW) | signatory doctor must have `registration_number` |
| POST | `/signatories/{id}/signature` | DX_ADMIN | PNG ≤ 300 KB, stored under clinic prefix, content-addressed |
| GET | `/catalog?kind&department&q&approved` | DX_READ | tests, panels, packages with DX fields |
| PATCH | `/catalog/tests/{id}` | DX_CATALOGUE | DX columns only; clears approval on clinical-field change |
| GET/POST/DELETE | `/catalog/tests/{id}/components[/{child_id}]` | DX_CATALOGUE | guard trigger enforces kinds/depth/cycles |
| POST | `/catalog/tests/{id}/approve` | DX_CATALOGUE_APPROVE | lab director; body echoes the band digest the user saw (409 if changed) |
| POST | `/catalog/formula/check` | DX_CATALOGUE | parse + evaluate against sample inputs, returns tokens / errors |
| POST | `/catalog/seed/preview`, `/catalog/seed/apply` | DX_CATALOGUE | apply is idempotent by `test_code`; creates inactive, unapproved drafts |
| POST | `/catalog/import/parameters?dry_run=true` | DX_CATALOGUE | CSV/XLSX ≤ 2 MB, dry-run report then commit |
| GET/POST/PUT | `/admin/lis/tests/{id}/parameters…` (existing, extended) | LAB_TESTS_MANAGE | new DX fields accepted; formula validated at save |

## P3 — Registration, visits, billing
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET | `/patients/search?q` | DX_REGISTER | same matcher as OPD registry (`app/services/opd.py`) |
| POST | `/patients` | DX_REGISTER | registry create (DPDP consent required) |
| GET/POST/PATCH | `/referrers` | DX_REGISTER (create) / DX_ADMIN (edit) | dedupe by `name_key` + phone, 409 with candidate |
| GET/POST/PATCH | `/b2b/accounts` (identity fields) | DX_B2B_MANAGE (POST NW) | money fields arrive in P8 |
| GET | `/bookings/today?branch_id&date` | DX_REGISTER | confirmed `lab_test` appointments not yet converted |
| POST | `/price-quote` | DX_REGISTER | cart pricing (MRP / rate card / package), no write |
| POST | `/visits` | DX_REGISTER (NW) | `Idempotency-Key` header required; returns visit, items, samples |
| GET | `/visits?date&site&status&q&account&referrer` | DX_READ | site-scoped |
| GET | `/visits/{id}` | DX_READ | role-filtered (SECURITY §4) |
| GET | `/visits/{id}/timeline` | DX_READ | `dx_events` |
| POST | `/visits/{id}/items` | DX_REGISTER | add tests (idempotent) |
| POST | `/items/{id}/cancel` | DX_REGISTER + reason | refund-first rule |
| POST | `/visits/{id}/cancel` | DX_REGISTER | only before any collection |
| POST | `/visits/{id}/bill` | DX_BILLING | build/refresh draft invoice |
| POST | `/visits/{id}/bill/issue`, `/visits/{id}/receipts`, `/visits/{id}/refunds`, `/visits/{id}/payment-link` | DX_BILLING | delegate to `opd_billing` (same idempotency, shift rules) |
| GET | `/visits/{id}/bill.pdf?format=a5|thermal` | DX_BILLING or DX_REGISTER | |

## P4 — Collection and samples
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET | `/collection/queue?site&date` | DX_COLLECT | pending collection, STAT first |
| GET | `/visits/{id}/labels.pdf`, `/visits/{id}/labels.zpl` | DX_COLLECT or DX_REGISTER | |
| POST | `/samples/{id}/bind` | DX_COLLECT | pre-printed barcode |
| POST | `/visits/{id}/collect` | DX_COLLECT | idempotent; `dx_awaiting_payment` per settings |
| POST | `/samples/{id}/reject` | DX_RECEIVE or DX_COLLECT | reason code; `recollect` bool |
| GET/POST/PATCH | `/rejection-reasons` | DX_ADMIN | |
| GET | `/samples/track/{barcode}` | DX_READ | location, status, batch, timeline |
| POST | `/admin/home-collection/prepare-route` (existing router) | HOME_COLLECTION_MANAGE | creates visits for the day's home bookings (idempotent) |
| POST | `/admin/home-collection/my-visits/{id}/samples` (existing router) | PHLEBOTOMIST (own visit only) | bind + collect; added to the phlebotomist allowlist |

## P5 — Logistics and outsourcing
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET/POST/PATCH | `/outsource-partners`, `/routing-rules` | DX_ADMIN (POST NW) | rule conflict 409 |
| GET | `/routing/resolve?lab_test_id&site_id` | DX_ADMIN | preview |
| POST | `/batches` | DX_LOGISTICS (NW) | from user's site (pinned) |
| POST / DELETE | `/batches/{id}/samples`, `/batches/{id}/samples/{sample_id}` | DX_LOGISTICS | scan add/remove while open |
| POST | `/batches/{id}/dispatch` | DX_LOGISTICS | carrier, temperature |
| GET | `/batches?direction=incoming|outgoing&status` | DX_LOGISTICS or DX_RECEIVE | |
| POST | `/batches/{id}/receive` | DX_RECEIVE | scanned list, receipt temperature; discrepancy report |
| POST | `/samples/receive` | DX_RECEIVE | direct scan at same site |

## P6 — Bench, verification, approval
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET | `/worklist?stage&department&site&priority&page` | DX_READ + stage permission | stages per ARCHITECTURE §4.2 |
| GET | `/orders/{id}/worksheet` | LAB_ENTRY or DX_VERIFY or LAB_VALIDATE | LIS worksheet + previous values, delta, calculated preview, instrument prefill |
| POST | `/orders/{id}/results` | LAB_ENTRY | `dx_record_results`; flags/formulas server-side |
| POST | `/results/{id}/verify` | DX_VERIFY | `expected_updated_at`, decision, comment |
| GET | `/approval/queue?normal_only&department` | LAB_VALIDATE | |
| POST | `/results/approve` | LAB_VALIDATE (doctor-linked) | `{items:[{result_id, expected_updated_at}], batch: bool}` ≤ 50 items; per-item outcomes |
| POST | `/results/{id}/critical-events` | DX_VERIFY or LAB_VALIDATE | call record |
| GET | `/critical/open` | DX_READ | |
| POST / GET | `/results/{id}/attachments`, `/attachments/{id}` | LAB_ENTRY / DX_READ | PNG/JPEG/PDF ≤ 2 MB, only while unsigned |
| — | `/admin/lis/orders/{id}/results`, `/admin/lis/results/{id}/validate` (existing) | — | for a `source='dx'` order: **409 `dx_use_dx_route`** (prevents bypassing receipt check and verification UI); OPD/IPD unchanged |

## P7 — Reports, templates, delivery
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET/POST/PATCH | `/templates`, `/templates/{id}` | DX_ADMIN (POST NW) | draft edits only |
| POST | `/templates/{id}/publish`, `/templates/{id}/retire` | DX_ADMIN | |
| POST | `/templates/{id}/assets` | DX_ADMIN | header/footer images |
| POST | `/templates/clone-from-pdf` | DX_ADMIN | rasterise page 1, return image for cropping |
| GET | `/templates/{id}/preview.pdf` | DX_ADMIN | fixture visit |
| GET | `/releases?status&site&date` | DX_DISPATCH | tabs To release / Held / Sent / Failed / Printed |
| POST | `/visits/{id}/releases` | DX_DISPATCH | `{result_ids?, kind}`; default = all signed not released |
| GET | `/releases/{id}/pdf` | DX_DISPATCH or DX_READ | audit-logged clinical read |
| POST | `/releases/{id}/resend` | DX_DISPATCH | idempotent per minute |
| POST | `/releases/{id}/print` | DX_DISPATCH | logs copies/purpose |
| POST / DELETE | `/releases/{id}/share-link` | DX_DISPATCH | token shown once |
| GET / PATCH | `/delivery-owner` | DX_ADMIN | per branch `connector|shadow|dx`; `dx` refused with active connector |
| GET | `/public/dx/verify/{code}` | public, rate-limited | minimal fields (SECURITY §6) |
| GET / POST | `/public/dx/r/{token}`, `/public/dx/r/{token}/unlock` | public, rate-limited | last-4 phone check, attempts, expiry |

## P8 — B2B
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET/POST/PATCH | `/rate-cards`, `/rate-cards/{id}/items` (+ CSV import) | DX_B2B_MANAGE (POST NW) | |
| PATCH | `/b2b/accounts/{id}/billing` | DX_B2B_MANAGE | limit, days, mode, rate card, white label, template |
| GET | `/b2b/accounts/{id}/ledger?from&to` | DX_B2B_MANAGE | derived balance |
| POST | `/b2b/accounts/{id}/payments` | DX_B2B_MANAGE | idempotency key; UTR for NEFT |
| POST | `/b2b/accounts/{id}/hold`, `/release-hold` | DX_B2B_MANAGE | reason |
| POST / GET | `/b2b/statements`, `/b2b/statements/{id}`, `/b2b/statements/{id}/pdf`, `/b2b/statements/{id}/void` | DX_B2B_MANAGE | |
| GET | `/b2b/receivables` | DX_B2B_MANAGE | ageing |
| POST / GET | `/barcode-stock` | DX_ADMIN | non-overlapping ranges |
| — | staff create (existing) | STAFF_MANAGE | `staff_role=B2B_PARTNER` requires `dx_b2b_account_id` of this clinic |
| GET | `/partner/me`, `/partner/catalog`, `/partner/visits`, `/partner/visits/{id}`, `/partner/releases/{id}/pdf`, `/partner/statements`, `/partner/visits/{id}/labels.pdf` | B2B_PARTNER (confined) | every query `.eq("b2b_account_id", user.dx_b2b_account_id)` |
| POST | `/partner/visits` | B2B_PARTNER (NW) | `source='partner_portal'`, account forced from the session |

## P9 — Dashboards, MIS, assistants
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET | `/dashboard?site&date` | DX_READ | counts per stage, TAT breaches, critical open, held dues |
| GET | `/mis/tat`, `/mis/volumes`, `/mis/revenue`, `/mis/rejections`, `/mis/referrers` | DX_REPORTS_MIS | date range ≤ 366 days; revenue needs DX_BILLING too |
| GET | `/mis/export.csv?report` | DX_REPORTS_MIS | streamed, audit-logged |

## P10 — Instruments and QC
| Method | Path | Auth | Notes |
|:-|:-|:-|:-|
| GET | `/internal/dx/bridge/orders?barcode` | bridge HMAC | host query: machine codes for open orders of the tube |
| POST | `/internal/dx/bridge/results` | bridge HMAC | idempotent by `(instrument_id, message_id)` |
| POST | `/internal/dx/bridge/heartbeat` | bridge HMAC | `last_seen_at` |
| GET/POST/PATCH | `/bridge-agents` (key shown once), `/instruments` | DX_ADMIN | |
| GET / POST | `/instrument-results?status=unmatched`, `/instrument-results/{id}/map`, `/ignore` | LAB_ENTRY | |
| GET/POST | `/qc/materials`, `/qc/results`, `/qc/chart?material` | DX_QC | Westgard flags computed server-side |
