# CallMeDex Processing Center Integration — Design

## Background

CallMeDex is a separate platform (owned/operated by the same team) for online
doctor-mediated diagnostic test booking with home sample collection. CallMeDex
has no WhatsApp integration of its own — MediAssist AI delivers CallMeDex's
lab reports to patients over WhatsApp, using a single WhatsApp number that is
shared across **every** diagnostic lab ("processing center") that fulfills
CallMeDex jobs. MediAssist confirms delivery back to CallMeDex.

Separately, a processing center may also be an ordinary MediAssist client in
its own right — a diagnostic center with its own WhatsApp number, booking
flow, and patient base, handling all reports that were **not** sourced from
CallMeDex. The same physical business can be both at once (a "common
client"), and can start as one and add the other later without re-registering
or disrupting the first.

## Goals

1. A processing center can be registered as CallMeDex-enabled, either at
   creation time (both capabilities together) or added to an existing
   ordinary diagnostic-center client later, without touching its existing
   registration.
2. CallMeDex report jobs are delivered to patients via the shared CallMeDex
   WhatsApp identity, not the processing center's own number — while still
   being attributed to that processing center's own dashboard/analytics.
3. MediAssist confirms delivery status back to CallMeDex.
4. The owner platform panel has a dedicated view of all CallMeDex processing
   centers and their delivery activity.
5. Zero behavior change to the existing MocDoc connector, the existing
   `/internal/integrations/lab-report` endpoint, inbound WhatsApp routing, or
   the `lab_reports` schema — this is purely additive.

## Non-goals

- CallMeDex does not get a conversational booking flow through MediAssist —
  its WhatsApp presence is delivery/notification only, reusing the normal
  patient-reply handling that every clinic tenant already gets for free via
  `resolve_tenant()`.
- No self-service CallMeDex enablement for clinic admins — this is an
  owner/platform-level action (CallMeDex partnerships are curated, not
  self-serve), unlike the existing clinic-admin-facing MocDoc connector UI.
- No new database migration. The schema is already generic enough
  (`integration_connectors.connector_type` is a free-text field,
  `config` is JSONB) to represent this without new tables or columns.

## Data model

### CallMeDex sender identity

CallMeDex is represented as one ordinary row in `clinics`, created through
the existing clinic-provisioning flow (`provision_clinic()` /
`POST /platform/clinics`) — same as any other tenant. It gets its own
WhatsApp number and Meta credentials in `config` exactly like any clinic.

Two new keys are added to that clinic's `config` JSONB (no schema change,
`config` is already unstructured):

- `config.callmedex_callback_url` — URL MediAssist calls to confirm delivery.
- `config.callmedex_callback_secret` — bearer secret sent with that callback.

One new env var, `CALLMEDEX_CLINIC_ID`, holds that clinic's UUID so the
report-job endpoint knows which clinic's credentials to send through. This
follows the existing `INTEGRATION_SECRET`-style pattern of small dedicated
env vars for internal wiring.

### Processing center enrollment

A processing center "activates CallMeDex behavior" via a normal
`integration_connectors` row on its own `clinics.id`:

```
connector_type: 'callmedex'
is_enabled:     true
config: { "clinic_slug": "<CallMeDex's code for this center>" }
```

`clinic_slug` is reused as-is — it already means "the external system's
identifier for this clinic" (same field MocDoc uses for its clinic slug).
No new column. Disabling CallMeDex behavior for a client is `is_enabled =
false` on this same row — it never touches the client's own `clinics` row,
its own WhatsApp number, or its own booking/report data.

This is the entire answer to "two numbers, activated independently": the
processing center's own number is its own `clinics.whatsapp_number` (already
exists, untouched). The CallMeDex "number" is never stored per-client at all
— it's the single shared CallMeDex clinic's credentials, referenced only via
the enabled connector flag + code.

### Idempotency & audit

Reused unchanged: `integration_processed_reports` and `connector_audit_log`,
keyed on `(clinic_id, connector_type='callmedex', external_report_id)` —
identical mechanism to MocDoc today, just a different `connector_type`
value. This is what gives the owner dashboard per-center report counts for
free.

## Report delivery flow

### New endpoint: `POST /internal/integrations/callmedex/report-job`

Added as a new route in `app/routers/integrations.py`, alongside (not
replacing) the existing `/lab-report` endpoint. Same shape as the MocDoc
intake (multipart form + file), authenticated with a **dedicated** secret —
`CALLMEDEX_INTEGRATION_SECRET` (separate from `INTEGRATION_SECRET`, since the
caller is an external platform, not our own trusted connector worker).

Request fields:

- `processing_center_code` (str) — CallMeDex's code for the center.
- `patient_phone`, `patient_name`, `report_name`, `report_type` — same as
  the existing intake.
- `external_report_id` (str) — CallMeDex's job id, used for idempotency.
- `file` — the report PDF.

Flow:

1. Verify `X-CallMeDex-Secret` header.
2. Resolve `processing_center_code` → look up `integration_connectors` where
   `connector_type='callmedex' AND is_enabled=true AND
   config->>'clinic_slug' = processing_center_code`. Not found/disabled →
   `403`. This lookup **is** the authorization check: a code that isn't
   actively enrolled cannot receive jobs, even with a valid shared secret.
   This is stricter than the current MocDoc path (which trusts
   `connector_type` blindly, since only our own worker calls it) — reasonable
   here because the caller is a different, external platform.
3. Idempotency check against `integration_processed_reports` (unchanged
   pattern, `connector_type='callmedex'`).
4. Call `LabReportService.upload_and_send(clinic_id=<processing center>,
   ..., send_via_clinic_id=CALLMEDEX_CLINIC_ID)` — see below.
5. Record the processed report (unchanged pattern).
6. Log to `connector_audit_log` (`clinic_id=<processing center>,
   connector_type='callmedex'`) — unchanged pattern.
7. POST a delivery-status callback to CallMeDex: fetch the CallMeDex clinic's
   `config.callmedex_callback_url` / `callmedex_callback_secret`, send
   `{external_report_id, status: "delivered"|"failed", lab_report_id}`.
   Best-effort — logged on failure, does not fail the request or roll back
   the delivery (matches the existing "don't fail the whole request" pattern
   already used for the processed-report insert in the MocDoc path).

### `LabReportService.upload_and_send()` — one additive parameter

New optional parameter: `send_via_clinic_id: Optional[str] = None`.

- When `None` (every existing caller: MocDoc connector, manual admin
  upload, scheduled resend) — behavior is **exactly** what it is today.
- When set, the method resolves that clinic via `get_clinic_by_id()` and
  uses **its** credentials for `whatsapp_service.send_text()` /
  `send_document()`, while the `lab_reports` row's `clinic_id` still records
  the `clinic_id` argument (the processing center) — so the report shows up
  correctly in that center's own admin dashboard, patient history, and
  analytics, even though it was sent from a different WhatsApp number.

This is the one intentional touch to shared/existing code, called out
explicitly per your request — everything else is new, additive code in new
functions/endpoints.

## Registration flow

### Register both capabilities at once

`CreateClinicRequest` (`app/routers/clinics.py`) gets one new optional
field: `callmedex_processing_center_code: Optional[str] = None`.

In `provision_clinic()`, if that field is set, after the clinic row (and any
branches) are created, one `integration_connectors` row is inserted
(`connector_type='callmedex', is_enabled=true, config={'clinic_slug':
code}`) — mirrors the existing branch-seeding block immediately above it in
the same function. This is the "register both at once" path from your Vizag
example, minus the actual case where CallMeDex isn't live yet.

### Add CallMeDex to an existing client later

New endpoint: `POST /platform/clinics/{clinic_id}/callmedex` (owner-secret
protected, in `app/routers/platform.py`). Body: `{processing_center_code:
str, is_enabled: bool}`. Upserts the same connector row shape as above
(insert if absent, else update `config.clinic_slug` / `is_enabled`). This
is a self-contained addition to `platform.py` — it does not call into
`admin.py`'s existing `PUT /admin/connectors` (which is clinic-admin-facing
and MocDoc-oriented); it writes directly to `integration_connectors` via
supabase, matching the simplicity of the existing branch-insert code in
`provision_clinic()`.

Calling this again with `is_enabled=false` deactivates CallMeDex behavior
for that client without touching anything else about their account —
satisfies "activating its CallMeDex behavior [as an] option" independently
of their primary registration.

## Owner platform dashboard

New page in `admin/platform.html`: "CallMeDex Processing Centers".

Backed by one new read endpoint, `GET /platform/callmedex/centers`
(owner-secret protected, `platform.py`), which joins `clinics` with their
enabled `connector_type='callmedex'` row and aggregates counts from
`integration_processed_reports` / `connector_audit_log`:

- Per-center: name, processing-center code, enabled/disabled, reports
  delivered (all-time + last 30 days), last delivery time, last error (if
  any).
- Summary card: total active centers, total CallMeDex reports delivered
  across the platform.

UI reuses the existing leaderboard-table and summary-card CSS/JS patterns
already in `platform.html`; the "create clinic" modal gets the new optional
`callmedex_processing_center_code` field, and the existing clinic
detail/edit modal gets an "Enable CallMeDex" toggle + code input that calls
the new `/platform/clinics/{id}/callmedex` endpoint.

## Explicitly untouched

- `app/routers/admin.py` — MocDoc connector CRUD (`/admin/connectors*`) and
  its clinic-admin-facing UI in `admin/index.html`.
- `app/routers/integrations.py` — existing `/internal/integrations/lab-report`
  endpoint, byte-for-byte.
- `resolve_tenant()` / inbound WhatsApp routing — CallMeDex's clinic row
  works through this unchanged; patient replies to CallMeDex-sourced
  messages route normally to that tenant.
- `lab_reports` table schema.
- No new migration file.

## Testing

- Unit test: `upload_and_send()` with `send_via_clinic_id` set sends via the
  override clinic's credentials but writes `lab_reports.clinic_id` as the
  original clinic — and with it unset, behavior is byte-identical to today
  (regression guard for MocDoc).
- Unit test: `POST /internal/integrations/callmedex/report-job` rejects an
  unknown/disabled `processing_center_code` (403), accepts a valid one,
  is idempotent on repeated `external_report_id`, and still returns success
  if the CallMeDex callback POST fails.
- Unit test: `provision_clinic()` creates the `callmedex` connector row when
  `callmedex_processing_center_code` is supplied, and creates none when it
  isn't (existing clinics unaffected).
- Unit test: `POST /platform/clinics/{id}/callmedex` upserts and toggles the
  connector row without touching the clinic's own `clinics` row.
- Unit test: `GET /platform/callmedex/centers` returns correct per-center
  aggregates from seeded `integration_processed_reports`/`connector_audit_log`
  rows.
