# Session 23 — CallMedex ↔ Kriya Sync: Signed Callbacks, CallMedex-Number Booking, Owner-Panel Enrollment

**Date:** 2026-09-24
**Migration:** `086_callmedex_booking_sessions.sql`. You ran the SQL on live Supabase on 2026-09-24. A read-only check confirmed the table exists, but **`schema_migrations` does not record 086** (the highest entry is `085_ai_features_phase2.sql`). The startup drift check (`app/main.py`) refuses to boot until that row exists. See §6.
**New env vars:** `CALLMEDEX_BASE_URL`, `CALLMEDEX_OUTBOUND_BEARER_TOKEN` (optional), `CALLMEDEX_HMAC_SECRET` (accepted as an alias of `CALLMEDEX_HMAC_SIGNATURE_SECRET`).
**Trigger:** the owner-panel screenshot showed Accumx Diagnostics twice under "CallMedex Processing Centers", with the CallMedex WhatsApp number shown as `100000000000000 · no token set · environment default`. The brief also asked for per-event callbacks and a booking flow on the CallMedex number.

## 1. What the brief assumed vs what the code does

Checked against CallMedex's source (`callmedex/backend`), which overrides the brief:

| Topic | Brief | Actual (source) |
|---|---|---|
| Callback signature header | `X-Signature-256`, bare hex | `X-Signature: sha256=<hex>` (`middleware/mediassist_auth.py`) |
| Signed message | raw body | `f"{X-Timestamp}." + raw_query + raw_body` |
| `X-Timestamp` | ISO 8601 | Unix epoch seconds (integer string); ISO is rejected as malformed |
| Auth | not mentioned | `Authorization: Bearer <MEDIASSIST_INBOUND_BEARER_TOKEN>` required |
| Report submission to Kriya | "connection established" | CallMedex posts to `POST {MEDIASSIST_BASE_URL}/api/v1/report-jobs`. **Kriya has no such route**, so the link is not wired (§6). |
| Booking response | `{booking_id, status, scheduled_slot}` | `{booking_id, status}` only |

Kriya now signs the way CallMedex's code verifies. The brief's format would have been rejected with 401 on every call.

## 2. Reports and root causes

| # | Report | Root cause | Where |
|---|---|---|---|
| 1 | Accumx shown twice in the owner panel | Accumx has two **enabled** MocDoc connectors: clinic-wide (`branch_id NULL`) and branch MAHARANIPETA (confirmed read-only on the live DB). Both are legitimate (migration 025). The panel listed one row per connector without its scope, and summed each clinic's reports once per connector (double count). | `platform.get_callmedex_processing_centers`, `admin/platform.html` |
| 2 | Enrollment "not perfect" (a) | `resolve_processing_center` used `.single()`. With Accumx's 2 rows PostgREST errors, so every CallMedex job fell back to the env MocDoc credentials with no base_url/slug. | `config/processing_centers.py` |
| 3 | Enrollment (b) | Passwords saved from the owner/admin panel are stored as `password_encrypted` (Fernet). The resolver passed the **ciphertext** to MocDoc as the password, so login was guaranteed to fail. | same |
| 4 | Enrollment (c) | "Edit" on a branch connector opened the modal without `branch_id`, so saving silently rewrote the clinic-wide row. Changing the type in the modal created a new row. Nothing was prefilled. | `admin/platform.html` |
| 5 | Patients could receive nothing | With the CallMedex number/token unset, `WhatsAppDeliveryService` "simulated" a send and returned DELIVERED in production. `lab_reports` said `sent` and the patient got silence. A real Strategy-1 send failure also had no fallback. | `whatsapp/service.py`, `workers/runner.py` |
| 6 | Callbacks | A single generic `callmedex_callback_url` (default localhost) and a sandbox bypass. `CALLMEDEX_APP_ENV` defaults to `development`, so the bypass was active in prod unless set. | `callbacks/handler.py` |
| 7 | Inbound on the CallMedex number | Unknown `phone_number_id` → `TenantNotFound` → dead-letter. There was no booking flow. | `routers/webhook.py` |

## 3. Changes

### New: `app/integrations/callmedex/api/client.py`
- Signed client for `CALLMEDEX_BASE_URL + /api/v1/integrations/mediassist`. Blank base URL means no outbound call at all.
- Headers: `Authorization: Bearer` (`CALLMEDEX_OUTBOUND_BEARER_TOKEN`, else `CALLMEDEX_BEARER_TOKEN`), `X-Timestamp` (epoch, re-stamped per attempt), `X-Signature: sha256=…`, `X-Correlation-Id`, `X-Idempotency-Key`.
- `idempotency_key(*parts)` is a uuid5, so the same event always yields the same key and CallMedex replays instead of re-applying.
- Retries only network errors and 5xx (3 attempts, 1s/2s backoff). Never raises. Booking creation uses `attempts=1` (CallMedex caches the response only after its handler finishes).

### `callbacks/handler.py`
- `send_report_accepted / send_report_processing / send_report_delivered / send_report_failed` post to `/callbacks/report-*`, keyed `uuid5(report_job_id, event)`.
- `build_analysis_payload(summary_report, canonical_report)` builds `plain_language_summary` from the patient summary and `doctor_clinical_summary` from the clinician summary. `abnormal_flags` holds high/low/critical tests (`marker`, `value`, `status`, `reference_range`). `health_score` is `None` and `recommendations` is `[]`, because nothing produces them and we never invent clinical data.
- Legacy `send_status_callback` is untouched and no longer used by the runner.
- `notification-status` is not implemented: Kriya issues no notification ids.

### `api/schemas.py`
- `ProcessReportRequest.report_job_id` (optional) holds **CallMedex's** job id. Callbacks are sent only when it is present, so connector-driven jobs are unchanged.

### `workers/runner.py`
- `report-accepted` at start and `report-processing` before OCR both run as background tasks, so a slow CallMedex never delays the patient.
- `_settle_callbacks()` waits ≤15 s for them, then cancels stragglers **before** the terminal callback, so "processing" can never land after "delivered".
- Delivery: if Strategy 1 (the CallMedex number) produced no summary **or its send failed**, it falls back to `LabReportService.upload_and_send` on the clinic's own number. The WhatsApp message id is captured from either path.
- Step 9 sends `report-delivered` (with analysis) if sent, else `report-failed / delivery_failed`.
- The exception path sends `report-failed` with `_failure_reason()`:

  | Condition | `failure_reason` |
  |---|---|
  | `ValidationError` or failure at `PDF_DOWNLOADED` | `invalid_source_document` |
  | "did not appear in MocDoc" (wait timeout) | `report_not_ready_timeout` |
  | anything else (login, navigation, download) | `download_automation_failed` |

  A bill-unpaid report comes back as empty bytes, which can't be told apart, so it maps to `download_automation_failed` rather than a guessed `bill_payment_pending`.

### `whatsapp/service.py`
- In `APP_ENV=production`, placeholder/missing CallMedex credentials return **FAILED** (log `CALLMEDEX_WHATSAPP_NOT_CONFIGURED`), which triggers the clinic-number fallback. Dev/test still simulate.

### `config/processing_centers.py`
- No `.single()`. Prefers the clinic-wide row (CallMedex jobs carry no branch), else the first enabled row. Decrypts `password_encrypted` with `CONNECTOR_ENCRYPTION_KEY`; on failure it logs an actionable error.

### `config/settings.py`
- `callmedex_base_url`, `outbound_bearer_token`; `hmac_signature_secret` also reads `CALLMEDEX_HMAC_SECRET`; `populate_by_name=True`.

### New: `whatsapp/booking.py` (+ migration 086)
- **Isolation:** `is_callmedex_number(pid)` covers the DB `callmedex_whatsapp_settings` id plus the env id, **minus any id a clinic owns** (`clinics.phone_number_id`, `config.meta_phone_number_id`, `config.phone_number_id`) and the placeholder. It is cached for 60 s. `cached_callmedex_number()` is a pure set lookup for the webhook hot path.
- Replies go straight to Graph from the number the patient wrote to, with the CallMedex token. It never uses `app.services.whatsapp` or any clinic table.
- **Flow:**
  1. `menu` → [Book home collection]
  2. `date`: 3 day buttons. Today is offered only before 15:00 IST and only if a window is left.
  3. `window`: list of 7–9, 9–11, 11–1, 4–6 IST, each starting ≥1 h ahead.
  4. `address`: [Use saved address] / [New address] when the lookup returned one; otherwise typed text that must contain a 6-digit pincode, then the city.
  5. `confirm` → `POST /whatsapp-bookings` (`service_type=home_blood_collection`, IST window, `lat/lng=null`, `source_conversation_id=session_id`).
- **Lookup:** `GET /patients/lookup?phone=` (signed, query included) prefills `patient_id`, language (en/hi/te), and the saved address.
- **Confirmation** uses the brief's text in the patient's language, with the booking ID.
- **Safety:**
  - Emergency keywords (word-bounded) get a "call 108" reply.
  - `cancel/stop/exit/quit` clears the session; `hi/menu/start…` restarts.
  - Slot validity is re-checked at confirm.
  - Same details → same idempotency key, so re-tapping after a timeout can't double-book.
  - 5xx/timeout keeps the Confirm step; 4xx clears the session with "reply HI".
  - If `CALLMEDEX_BASE_URL` is unset, the patient gets a polite "unavailable".
- **State:** `callmedex_booking_sessions` (phone PK, state, data jsonb, updated_at). 30-min timeout; rows deleted on booking/cancel and purged after 1 day (DPDP); RLS forced, service_role only.
- The `is_callmedex` clinic flag and a synthesized CallMedex tenant were **not** built. Every clinic table has a clinic_id FK, and replies would come from a different number than the one the patient messaged.

### `routers/webhook.py`
- `POST /webhook`: `cached_callmedex_number()` (no I/O) skips `resolve_tenant` for the CallMedex number. Clinics are unchanged.
- `process_message`: when `is_callmedex_number(phone_number_id)`, it runs `acquire(clinic_id=None)`, `claim_message`, then `handle_callmedex_inbound`, and returns. Every other number continues on the unchanged path.

### `routers/platform.py` + `admin/platform.html`
- `GET /platform/callmedex/centers` returns one row per clinic and `connectors[]` (id, type, branch_id, branch_name, base_url, clinic_slug, username, has_password; the password never leaves the server). The clinic-wide row is primary and reports are counted once per clinic. Old top-level fields are kept.
- UI:
  - Scope label per connector ("Clinic-wide" / "Branch: MAHARANIPETA").
  - One Edit per connector; it sends `branch_id`, prefills non-secret fields, and locks clinic and type.
  - "+ Enroll" stays unlocked and clinic-wide.
  - An unconfigured CallMedex number shows "NOT configured" instead of `100000000000000`.
- `PUT /platform/callmedex/whatsapp-settings` rejects (400) a phone_number_id that any clinic owns.

### Docs / config
- `.env.example`, `docs/agent-context/07-INTEGRATIONS.md` §5, and `13-KNOWN-ISSUES-AND-GAPS.md` §6.

## 4. Tests

- `app/integrations/callmedex/tests/test_callmedex_bidirectional_sync.py` (22):
  - the signature re-verified with CallMedex's exact formula, including GET query signing
  - bodies validated against mirrors of CallMedex's pydantic models
  - stable idempotency keys, 5xx retries, and no call when unconfigured
  - the analysis payload
  - the runner's delivered, fallback, delivery_failed, login-failure and no-job-id cases, and the failure mapping
  - simulation in prod counts as FAILED
  - the resolver's row preference and decryption
  - clinic ids never treated as CallMedex
  - the full booking with a saved address (Telugu confirmation), a typed address with retry reusing the key, emergency/cancel/unconfigured, 4xx clearing the session, and slot cutoffs
- `tests/test_callmedex_webhook_isolation.py` (3): the CallMedex number never resolves a tenant or enters the clinic conversation flow; a clinic number never reaches CallMedex; duplicates are dropped.
- `tests/test_platform.py` (+2): Accumx grouping and no double count; a clinic's number is refused as the CallMedex number.
- **Full suite (hermetic, forced test credentials): 3112 passed, 5 skipped, 0 failed.** The first run hit one lint ratchet (free-text `# unscoped:` comments), fixed with canonical reasons.
- Owner-panel JS: `node --check` passes, and the render/modal logic was executed in Node with Accumx-shaped data.
- Cleanup: 3 orphaned smoke-test workers (test credentials, created 15:06/15:09) stopped.

## 5. Isolation guarantees for live clinics (Aura, Accumx, Visakha)

- Clinic messages: the only additions on their path are an in-memory set lookup in `POST /webhook`, and at most one 60-second-TTL refresh (2 small queries) per worker in `process_message`. A refresh DB error keeps the last known set.
- No clinic table, clinic WhatsApp credential, Razorpay or appointment code was changed.
- The CallMedex runner still inserts `lab_reports` exactly as before. The only new behaviour is the fallback to the clinic number when the CallMedex send fails.

## 6. Still open — required before CallMedex is production-ready

1. **Record 086 in `schema_migrations`.** The table exists but the row doesn't (highest = 085). Until `INSERT INTO schema_migrations (name) VALUES ('086_callmedex_booking_sessions.sql');` runs, the next deploy refuses to boot.
2. **`POST /api/v1/report-jobs` is not built.** CallMedex submits reports there (with `report_job_id`, `source_document_url`, `patient`, `delivery`, and CallMedex's own `processing_center_id`, signed `X-Signature: sha256=…(ts.body)`). Until it exists, no job carries a CallMedex `report_job_id`, so the new callbacks never fire in practice. This needs a CallMedex-center → Kriya-clinic mapping decision.
3. **CallMedex WhatsApp number not configured on Kriya.** Save it in the owner panel → "CallMedex WhatsApp Number". Until then, reports go out on each centre's own number and the booking flow can't reply.
4. **Meta:** the CallMedex number's webhook must point to Kriya `/webhook`; `lab_report_summary` must be approved on the CallMedex WABA.
5. **Secrets parity (not verifiable from here):**
   - `CALLMEDEX_BASE_URL` set on Kriya's Render service.
   - Kriya HMAC secret = CallMedex `MEDIASSIST_HMAC_SECRET`.
   - Kriya outbound bearer = CallMedex `MEDIASSIST_INBOUND_BEARER_TOKEN`.
6. Accumx's two MocDoc connectors poll the same portal/slug. The `lab_reports` unique index prevents double sends; one connector is probably redundant (owner decision).
7. Running `app/integrations/callmedex/tests` **alone** uses `.env` (production). Always run it together with `tests/`, or with the test credentials exported.
8. Not verified live: no real CallMedex call, Meta send, or booking was made. Do one real report and one real booking end to end after 1–5.
