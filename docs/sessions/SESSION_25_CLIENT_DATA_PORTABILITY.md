# Session 25 — Client Data Portability, Storage Quota, Support Inbox

**Date:** 2026-09-26 · **Migration:** 088 · **Origin:** dental-clinic field visit — the clinic could
not bring its old software's patient list into Kriya, and could not get Kriya's data out.

## What shipped

| Capability | Where |
|---|---|
| Import existing patients from old software (CSV) | Admin → **Data & Support** → Import patients |
| Browse/search imported patients, undo an import | Data & Support → Imported patients / Import history |
| Export appointments, WhatsApp patients, imported patients as CSV (date range ≤ 1 year) | Data & Support → Export data |
| Per-clinic imported-record limit + monthly add-on price, owner-set | Platform → **Client Data Storage** |
| "Storage filling up" banner at ≥ 90 %, "full" at 100 % | Admin subscription strip (every page load) + Data page meter |
| Clinic admin → owner messages (concern / feature request / storage request / billing / other), owner replies | Admin → Data & Support → Messages to Kriya · Platform → **Client Messages** |
| Storage add-on billed on the next generated invoice | `platform_invoices.storage_addon_paise`, included in Finance expected MRR |

All of it is available on every plan; every endpoint is **admin-only** (`require_admin`) — staff accounts
cannot bulk-read or bulk-write the clinic's PHI and do not see the tab.

## The one architectural decision that matters

**Imported patients go to a new `patient_records` table, never to `patients`.**
A `patients` row is live WhatsApp state:

- `consent.accepts_engagement()` suppresses follow-ups when `opted_in` is False;
- STOP/START keyword handling keys on the same flag;
- `patient_match` auto-delivers lab-report PDFs to a phone whose `patients` row name-matches.

An old-software export carries no WhatsApp opt-in. Writing it into `patients` would either fabricate
consent (DPDP/Meta-policy violation) or silently mute reminders for those numbers, and would change
report routing for the diagnostic clients. `patient_records` is read **only** by the Data & Support page
and its export. Nothing in the bot, scheduler, or report pipeline reads it. Keep it that way; if imported
patients should ever become WhatsApp-reachable, that needs an explicit opt-in flow, not a join.

## Design details

- **Quota unit = records**, stored in `clinics.config.patient_records_limit` (default 5,000 ≈ 5 MB; 0 = import
  off). Add-on price in `clinics.config.data_storage_addon_paise` (owner-only; never returned by `/admin`).
- **Import pipeline** (`POST /admin/data/patient-records/import`): size/encoding/header check → in-memory
  validation (zero writes) → per-clinic distributed lock `patient_import:<clinic>` → quota check against rows
  that are *actually new* → batch insert. `dry_run=true` ("Check file") stops before writing.
- **Atomicity:** each upload is a `patient_import_batches` row; records FK to it `ON DELETE CASCADE`. Any
  chunk failure deletes the batch → all its records go in one statement. "Undo" uses the same path.
- **Idempotent re-import:** `UNIQUE (clinic_id, dedupe_key)`; key = old-software patient id, else
  phone+name, else name+DOB. Shared family phones keep separate members.
- **Dirty legacy data:** only a missing/overlong name blocks the file (422, row numbers). Bad phone / date /
  age values are kept verbatim in `extra` and reported as warnings. Unknown columns (e.g. "Tooth Chart") are
  preserved in `extra`. Dates are day-first (DD/MM/YYYY). UTF-8 and Excel cp1252 files, `,` `;` tab delimiters.
- **Export:** paginated (1,000/page), capped at 50,000 rows (413 → shorter range), UTF-8 BOM for Excel,
  formula-injection-safe cells, DPDP-erased `[REDACTED]` shells excluded, every export audit-logged
  (`CLINIC_DATA_EXPORT`, row count, range — no PHI in the log). Timestamp ranges are IST calendar days sent as
  UTC `Z` strings (a literal `+05:30` can be decoded as a space by PostgREST).
- **DPDP erasure** (`data_retention.anonymize_patient`) now also deletes `patient_records` for that phone.
- **Support messages:** max 20/clinic/day, 4,000 chars, escaped on render in both panels. Owner opening a
  message marks it seen; replying sets status `in_progress` unless chosen otherwise.

## Round 2 — notifications and the follow-up switch

- **Admin bell (existing, sidebar top):** when the owner replies to or changes the status of a message,
  `platform._notify_clinic_of_support_update` inserts one clinic-wide `admin_notifications` row
  (`admin_id` NULL — the shape payment/callback alerts already use). Titles start with `Kriya Support`; the
  bell shows an **Open messages** button for those, which marks it read and opens Data & Support → Messages.
  Opening a message in the owner panel (a body-less PATCH that only marks it seen) never notifies.
  Notification failure is logged and never fails the reply. The drawer is now titled "Notifications";
  on phones the menu button carries the unread badge and the drawer fits the screen.
- **Owner bell (new, top bar):** unread badge from `GET /platform/support-messages/unread-count`
  (`owner_seen_at IS NULL`), polled every 60 s, count also shown in the browser-tab title. The dropdown
  lists unread then open messages; clicking one opens the reply modal (which marks it seen).
- **Patient Follow-ups switch:** the checkbox became an ON/OFF switch in the card header that saves
  immediately with `PUT /admin/profile {followup_enabled}` — the endpoint merges key by key, so the message,
  days and template are untouched. On failure it flips back and says so. The flag was already read by the
  scheduler at send time (`followup_config`), so OFF stops follow-ups from the next run, including ones
  already due. Existing semantics, now stated in the UI: visits that fall due while OFF are not followed up
  later. The switch change is recorded in the audit log (`followup_enabled` in `update_clinic_profile`).
  No migration.

## Deploy order

1. **Apply `migrations/088_client_data_and_support.sql` first.** It is purely additive and safe with the
   current build running. Rollback: `migrations/rollback/088_down.sql` (drops imported data — export first).
2. Deploy the code.

If the code lands before the migration nothing existing breaks: the banner field is fail-quiet, the owner
board marks counts unavailable, erasure logs an error for the one step, and the invoice only sends
`storage_addon_paise` when an add-on is set. The new Data & Support endpoints would return 500s until 088 runs.

## Verification record

| What | How | Result |
|---|---|---|
| Parsing, quota levels (89/90/100 %), CSV injection, export ranges, IST bounds | `tests/test_client_data.py` unit tests | pass |
| Auth: staff 403, cross-clinic 403, owner routes 401 | route tests with dependency override | pass |
| Import: only new rows written, over-quota 409 writes nothing, row errors 422 write nothing, dry run writes nothing, write failure → 500 "rolled back" without leaking internals | route tests | pass |
| Rollback deletes the batch scoped to the clinic; bulk rows have identical keys; export paginates, scopes, drops erased rows, caps at max | recording fake PostgREST builder | pass |
| Invoice: add-on added only where set; plain clinic's row byte-identical to pre-088 | route test | pass |
| Migration 088 on real PostgreSQL: unique key, phone CHECK, cascade undo, FORCE RLS isolates tenants for `kriya_app`, re-runnable twice | `tests/test_migration_088_client_data.py` + manual double apply | pass |
| Tenant-query linter + ratchet, 250-case super-admin scope matrix (new routes auto-included) | existing suites | pass |
| Full regression suite | `pytest` (hermetic conftest) | 3,256 passed, 5 skipped, 0 failed |

**Not verified here:** live PostgREST behaviour against the production Supabase (upsert
`ignore-duplicates` returning only inserted rows, `count=exact`), and the two HTML panels in a real browser.
Verify on production after deploy: import the template file with "Check file" then "Import", export it back,
undo it, and send/answer one support message.
