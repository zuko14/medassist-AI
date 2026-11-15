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
As built in migration 126 (paths under `/admin/dx`; NW = refused in WIND_DOWN). Masters are clinic-wide (branch-pinned
logins get 403); tests follow `lab_tests.branch_id` like the existing lab-test admin.

| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET | `/departments`, `/containers`, `/signatories` | DX_READ / DX_CATALOGUE / DX_ADMIN / DX_CATALOGUE_APPROVE | |
| POST / PUT | `/departments[/{id}]`, `/containers[/{id}]` | DX_CATALOGUE or DX_ADMIN (POST NW) | container `code` = 2 digits (barcode `YY cc SSSSSSSS`) |
| POST / PUT | `/signatories[/{id}]` | DX_CATALOGUE or DX_ADMIN (POST NW) | doctor must have a `registration_number` |
| POST | `/signatories/{id}/signature` | DX_CATALOGUE or DX_ADMIN | PNG ≤ 300 KB, content-addressed `<clinic>/dx-assets/signatures/<sha256>.png` in the private `lab-reports` bucket |
| GET | `/tests?kind&department_id&q&approved` | as GET above | tests, panels, packages with DX fields |
| POST | `/tests` | DX_CATALOGUE (NW) | creates an **inactive** draft |
| GET / PUT | `/tests/{id}` | read / DX_CATALOGUE | GET returns test, bands, components, `digest`, readiness `issues`; PUT = DX columns + `is_active`; clinical edits clear approval (trigger) |
| POST / PUT | `/tests/{id}/parameters[/{pid}]` | DX_CATALOGUE | 118 band shape + DX fields; formula validated at save (unknown code, unsafe construct, loop → 422 with `token`) |
| GET / POST / DELETE | `/tests/{id}/components[/{child_id}]` | read / DX_CATALOGUE | guard trigger enforces kinds / depth / self |
| POST | `/tests/{id}/approve` | DX_CATALOGUE_APPROVE | doctor-linked login with registration; body `{digest}` = what the approver reviewed (409 `dx_test_changed`); readiness 409 `dx_test_not_ready` |
| POST | `/formula/check` | DX_CATALOGUE / DX_CATALOGUE_APPROVE | parse + evaluate against sample inputs and patient context; no write |
| GET / POST | `/seed/preview`, `/seed/apply` | DX_CATALOGUE (apply NW) | idempotent by `test_code`; same-name clinic tests matched (only empty DX fields filled); inactive, unapproved drafts |
| POST | `/import/dry-run`, `/import/apply` | DX_CATALOGUE (apply NW) | CSV / XLSX ≤ 2 MB, one row per band; apply writes nothing unless every row is valid |

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

## P4 — Collection and samples (as built, migration 128; under `/admin/dx`)
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET | `/collection/queue?site_id&priority&q&date` | DX_COLLECT | visits of the day with a tube to draw, STAT → urgent → routine, oldest first; a branch-pinned login sees its own site; also returns the caller's sites |
| POST | `/visits/{id}/labels/print` | DX_COLLECT or DX_REGISTER | body `sample_ids[]` (empty = every labellable tube), `mode` `per_sample`/`per_test`/`register`, `format` `pdf`/`zpl`, `copies` (default 1 + `extra_labels`), `register_label`; returns PDF or ZPL; every print logged in `dx_label_prints`, `X-Reprint` header |
| POST | `/samples/{id}/bind` | DX_COLLECT | pre-printed / external label → pending tube (`dx_barcode_in_use`, `dx_barcode_already_set`) |
| POST | `/visits/{id}/collect` | DX_COLLECT | `samples [{id, barcode?, volume_ml?}]`, `collection_mode`, `idempotency_key`; a scanned barcode must match the tube; `dx_awaiting_payment` per settings |
| POST | `/samples/{id}/reject` | DX_RECEIVE or DX_COLLECT | `reason_code`, `notes`, `recollect`; replacement tube + orders; recollection WhatsApp (in-session text or `dx_recollection_required`) sent off the request path; `dx_sample_results_signed` |
| POST | `/samples/receive-direct` | DX_RECEIVE | `barcode`, `site_id`: tube drawn and processed at that site (`dx_sample_wrong_site`, `dx_sample_not_collected`) |
| GET | `/samples/{barcode}/track` | DX_READ (or any visit reader, DX_RECEIVE) | tube, visit, replacement, timeline (`dx_events` of the tube + visit-level events) |
| GET | `/rejection-reasons?include_inactive` | DX_RECEIVE, DX_COLLECT, DX_ADMIN or DX_READ | editing reasons: not built yet |
| GET / POST | `/home-collection/visits/{appointment_id}/samples` | PHLEBOTOMIST (own booking only, else 404) or DX_COLLECT | tubes of the booking's visit + amount due; POST = bind + collect, mode `home`; in `_PHLEBOTOMIST_ROUTES` |
| POST | `/home-collection/visits/{appointment_id}/doorstep-cash` | same | `amount_paise` (≤ due), `mode` `cash`/`upi_to_clinic` (UTR required), `idempotency_key` → `dx_field_collections` `pending_handover`; in `_PHLEBOTOMIST_ROUTES` |
| GET | `/home-collection/field-collections?status=pending_handover` | DX_BILLING | cashier's handover list (branch-pinned: own branch) |
| POST | `/home-collection/field-collections/{id}/handover` | DX_BILLING | receipt on the cashier's open shift (key `fc:<id>`) then `handed_over`; retry-safe |

Not built yet: `prepare-route`, rejection-reason editing, the field-collection dispute, a route for `dx_mark_sample_stored` (P6).

## P5 — Logistics and outsourcing (as built, migration 129; under `/admin/dx`)
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET | `/outsource-partners` | DX_ADMIN or DX_LOGISTICS | |
| POST / PUT | `/outsource-partners`, `/outsource-partners/{id}` | DX_ADMIN, clinic-wide (POST NW) | name unique per clinic (409) |
| GET | `/routing-rules` | DX_ADMIN | |
| POST / PUT / DELETE | `/routing-rules`, `/routing-rules/{id}` | DX_ADMIN, clinic-wide (POST NW) | one target (site or partner); a collection-only site 422; tie 409; PUT replaces the rule |
| POST | `/routing-rules/preview` | DX_ADMIN | `from_site_id`, `lab_test_ids[]` → destination and which rule matched (same SQL resolver as registration) |
| GET | `/batches?from_site&to_site&status` | DX_LOGISTICS or DX_RECEIVE | pinned: batches leaving or arriving at the caller's site; also returns `my_site_ids`, active sites and partners (destination pickers) |
| POST | `/batches` | DX_LOGISTICS (NW) | `from_site_id` (default: the pinned user's only site), `to_site_id` or `outsource_partner_id`, `carrier_name`, `temp_c` |
| GET | `/batches/{id}` | DX_LOGISTICS or DX_RECEIVE | manifest with per-tube stability (`normal`/`amber`/`expired`; cold window when the box was ≤ 8 °C) |
| POST | `/batches/{id}/samples` | DX_LOGISTICS | `barcodes[]`; error `reason` names the barcode |
| POST | `/batches/{id}/dispatch` | DX_LOGISTICS | `temp_c`, `notes`; tubes → `in_transit`; replay-safe |
| POST | `/batches/{id}/receive` | DX_RECEIVE at the destination site (a partner batch: its sending site, DX_LOGISTICS allowed) | `barcodes[]`, `temp_c`, `discrepancy_notes` → `discrepancy {received, missing, damaged, unexpected}`; replay-safe |

`LOGISTICS_RUNNER` is confined to `/admin/dx/logistics…`, `/admin/dx/batches…`, `GET /outsource-partners`, `GET
/samples/{barcode}/track`. Visit reads (`/visits/{id}`) drop `external_ref` / `delivery_prefs` without a desk grant and
money without a money grant — and both for a pinned user reaching another branch's visit through a sample site.
Orders on a visit carry `outsourced_to` (partner name); collection-queue tubes too. Not built: removing a tube from an
open batch, cancelling a batch, re-batching a tube recorded missing.

## P6 — Bench, verification, approval (as built, migration 130; under `/admin/dx`)
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET | `/worklist?stage&site_id&department_id&priority&date&open_only` | any DX read grant, DX_RECEIVE, DX_VERIFY, LAB_ENTRY or LAB_VALIDATE | stages per ARCHITECTURE §4.2 (`dx_stage`), counts per stage, TAT (setting clock + both clocks), tube colour, `critical_pending`, `verified` / `returned`, `batch_ok`; `open_only` = open visits of 7 days; pinned: own sites; no phone / money |
| GET | `/orders/{id}/worksheet` | LAB_ENTRY, LAB_VALIDATE or DX_VERIFY | LIS worksheet + calculated flag, previous value per line, open result with meta, verifications, critical events, attachments |
| POST | `/orders/{id}/results` | LAB_ENTRY | `values [{parameter_id, value, remarks}]`, `amendment_reason`, `preview` (evaluate only — live calculated values, flags, deltas); `dx_record_results`; tube must be received |
| GET | `/results/{id}` | LAB_ENTRY, LAB_VALIDATE or DX_VERIFY | result + values + meta + verifications + critical events (+ `critical_pending`) + attachments |
| POST | `/results/{id}/verify` | DX_VERIFY | `expected_updated_at`, `decision` verified / returned (comment ≥ 3 chars), `dx_self_verification` at three levels |
| POST | `/results/approve` | LAB_VALIDATE, doctor-linked login with a registration number | `{items:[{result_id, expected_updated_at}] ≤ 50, batch}`; per-item `{ok, status}` or `{code, message}` (`dx_batch_not_normal`, `dx_critical_not_notified`, `dx_verification_required`, `lis_result_changed`) |
| POST | `/critical-events/{id}/call` | DX_VERIFY, LAB_VALIDATE or LAB_ENTRY | `notified_to`, `notified_role`, `channel`, `read_back_confirmed`, `notes`; only a call with read-back clears approval |
| POST / GET | `/results/{id}/attachments?kind&caption`, `/results/{id}/attachments/{att}` | LAB_ENTRY / bench read | PDF, PNG, JPEG ≤ 5 MB (sniffed), only while the result is open |
| — | `/admin/lis/orders/{id}/results`, `/admin/lis/results/{id}/validate` (existing) | — | for a `source='dx'` order: **409 `dx_use_dx_route`**; the LIS sign-off queue omits DX results; `validate_and_sign_report` never delivers a DX result; OPD/IPD unchanged |

## P6b — OPD / IPD orders on the DX bench (as built, migration 131)
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| POST | `/orders/adopt` | DX_REGISTER or DX_COLLECT (NW) | `{order_ids: [uuid, 1..100]}`; uncollected OPD / IPD lab-test orders → one internal visit per patient (`billing_mode='internal'`, shares 0); the order keeps its source, encounter / admission and bill; already-adopted ids come back in `skipped`. LIS / G1 routes answer 409 `dx_use_dx_route` for an adopted order |

## P7 — Reports, templates, delivery (as built, migration 131)
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET | `/templates?owner_kind` | DX_ADMIN or DX_DISPATCH | `{templates, starters}`; a branch-pinned admin sees the clinic default and its own branch |
| POST | `/templates` | DX_ADMIN (NW) | `{owner_kind, owner_id?, name, starter, layout}` → draft, version = next for owner + name; layout validated (422 `dx_template_invalid`) |
| GET / PUT | `/templates/{id}` | read: DX_ADMIN or DX_DISPATCH; PUT: DX_ADMIN (NW) | PUT edits a **draft** only (409 `dx_template_published`) |
| POST | `/templates/{id}/publish`, `/templates/{id}/retire` | DX_ADMIN | publish stamps `published_at` in SQL; retire only from published |
| POST | `/templates/{id}/preview` | DX_ADMIN or DX_DISPATCH | `{layout?}` → PDF of the sample visit (Taiyo HbA1c + lipid), "INTERNAL VIEW" |
| POST | `/templates/assets` | DX_ADMIN (NW) | multipart PNG / JPEG ≤ 1 MB, ≤ 3000 px → `{path}` = `<clinic>/dx-assets/templates/<sha256>.<ext>` |
| GET | `/visits/{id}/releases` | DX_DISPATCH, DX_READ or DX_ADMIN | releases with their deliveries + the visit's `dues_paise` |
| POST | `/visits/{id}/releases` | DX_DISPATCH | `{result_ids?, kind?}`; default = every current signed result; kind derived (409 `dx_release_kind_mismatch` if given and wrong); render + store + send in the background |
| POST | `/releases/{id}/render` | DX_DISPATCH | retry a `render_failed` release |
| GET | `/releases/{id}/pdf` | DX_DISPATCH, DX_READ, DX_ADMIN or DX_VERIFY | audit-logged staff view; watermark INTERNAL VIEW / SHADOW / SUPERSEDED when a patient copy would be refused |
| POST | `/releases/{id}/print` | DX_DISPATCH | decision `print_patient`: 409 `held_dues` / `payer_routed` / …; else the stored PDF (SHADOW watermark in shadow mode) + `dx_print_log` row |
| POST | `/releases/{id}/share-link` | DX_DISPATCH | decision `share_link`; 128-bit token shown once, stored as SHA-256, TTL `share_link_ttl_hours` |
| GET | `/deliveries?status&target&date` | DX_DISPATCH, DX_READ or DX_ADMIN | Reports & Dispatch tabs; rows carry visit no., patient, release no., `reason_text`, `dues_paise` when held |
| POST | `/deliveries/{id}/resend` | DX_DISPATCH | re-runs the decision; queued → sent in the background; 409 `dx_delivery_already_sent`, 409 `dx_delivery_in_pipeline` (the pipeline's retry queue owns it) |
| GET | `/prints?date` | DX_DISPATCH, DX_READ or DX_ADMIN | desk print log (Printed tab) |
| POST | `/visits/{id}/delivery-overrides` | DX_ADMIN (clinic_admin holds it) | `{reason 10..300, expires_at?}` (default 7 days, max 31); rule 7 only; re-evaluates and sends |
| — | `/admin/lab-reports/{id}/resend`, held-report resolve with `new_phone` (existing) | — | refuse rows with `source='dx'` → 409 "use Reports & Dispatch" (AUDIT A20) |
| PATCH | `/settings` (P1) | DX_ADMIN | `delivery_owner` per branch `connector|shadow|dx` (no separate `/delivery-owner` route) |
| GET | `/public/dx/verify/{code}` | public, 30/min/IP | lab, released at, release no., tests, current / superseded, SHA-256, initials + age/sex (SECURITY §6) |
| GET / POST | `/public/dx/r/{token}`, `/public/dx/r/{token}/unlock` | public, 10/min/IP | HTML prompt; unlock `{phone_last4}` (JSON or form): 5 attempts consumed before the compare, decision re-checked, PDF `no-store` |
Not built in P7: `templates/clone-from-pdf`, `DELETE /releases/{id}/share-link` (links expire), `GET /releases/{id}/deliveries` (the visit's releases carry them).

## P8 — B2B (as built, migration 132)
All under `/admin/dx`. NW = refused while the module is in WIND_DOWN. Money routes are audit-logged.
| Method | Path | Permission | Notes |
|:-|:-|:-|:-|
| GET / POST | `/rate-cards` | read DX_B2B_MANAGE, DX_REPORTS_MIS, DX_BILLING · write DX_B2B_MANAGE (NW) | list with `item_count` |
| GET / PUT | `/rate-cards/{id}` | as above | detail with items (test name, code, MRP) |
| POST | `/rate-cards/{id}/items` | DX_B2B_MANAGE | upsert by test; `price_paise: null` removes the test |
| POST | `/rate-cards/{id}/import?dry_run` | DX_B2B_MANAGE | CSV `test_code` + `price` (₹) or `price_paise`, ≤ 1 MB, all or nothing (422 lists rows) |
| GET | `/b2b-accounts` | (P3) | + `balance_paise` for money grants |
| POST / PUT | `/b2b-accounts`, `/b2b-accounts/{id}` | DX_B2B_MANAGE | + `rate_card_id`, `credit_limit_paise`, `credit_days`, `gstin`, `billing_address`, `statement_cycle`, `auto_hold`, `copay_mode`, `copay_value` (basis points / paise); a status set here is a manual hold |
| GET | `/b2b-accounts/{id}` | DX_B2B_MANAGE, DX_REPORTS_MIS | account + `dx_b2b_balance` + rate card + partner logins |
| GET | `/b2b-accounts/{id}/ledger` | as above | every entry, oldest first, running balance, visit / patient / test |
| POST | `/b2b-accounts/{id}/payments` | DX_B2B_MANAGE | body idempotency key; `payment` (credit) or `prepaid_topup` (prepaid); reference required for NEFT / UPI / cheque |
| POST | `/b2b-accounts/{id}/hold` | DX_B2B_MANAGE | `{status: active\|on_hold, reason}` (manual hold) |
| GET | `/b2b-accounts/ageing` | DX_B2B_MANAGE, DX_REPORTS_MIS | not due, 1–30, 31–60, 61–90, 90+ days past due (+ totals) |
| GET / POST | `/b2b-accounts/{id}/statements` | read as above · issue DX_B2B_MANAGE | issue `{period_from, period_to}` → `B2B-YYYY-NNNNN`, PDF stored at `<clinic>/dx-statements/` |
| GET | `/b2b-accounts/statements/{id}/pdf` | DX_B2B_MANAGE, DX_REPORTS_MIS | stored PDF, else re-rendered |
| POST | `/b2b-accounts/statements/{id}/send` | DX_B2B_MANAGE | WhatsApp text + 7-day private link to the account desk (in-session) |
| POST | `/b2b-accounts/statements/{id}/void` | DX_B2B_MANAGE | reason; latest live statement only |
| POST | `/b2b-accounts/{id}/users` | DX_B2B_MANAGE (clinic-wide, NW) | creates a `B2B_PARTNER` login bound to the account; the generic staff route refuses this role |
| GET / POST | `/barcode-stock` | read + DX_ADMIN, DX_COLLECT · issue DX_B2B_MANAGE, DX_ADMIN (NW) | one account or one site; ranges of a prefix never overlap (409) |
| GET | `/referrers/volume?months` | VISIT_READ, DX_B2B_MANAGE, DX_REPORTS_MIS | visits and tests per referrer per IST month — counts only (ADR-DX-11) |
| GET | `/partner/me` | B2B_PARTNER (confined) | own account, balance, rate card, sites, tests at contract price |
| GET / POST | `/partner/visits` | B2B_PARTNER | own visits (+ current releases); register: `source='partner_portal'`, account and billing mode from the login, subject = account dependant |
| GET | `/partner/statements`, `/partner/statements/{id}/pdf` | B2B_PARTNER | issued statements only |
| GET | `/partner/releases/{id}/pdf` | B2B_PARTNER | only when `report_delivery` is `account` / `both` and `dx_delivery_decision(…, 'partner_portal')` allows (dues, owner, payer rules) |

Partner confinement (`verify_credentials`): a `B2B_PARTNER` login reaches only `/admin/dx/partner/*`, `/admin/me` and its
own password / username changes; without `dx_b2b_account_id` it reaches nothing. The account id is read live from
`clinic_admins` on every session request and never taken from the request.

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
