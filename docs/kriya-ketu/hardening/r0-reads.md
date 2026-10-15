# KK-P0-15 hardening checklist — R0 read endpoints (KK-P4-01)

Audited 2026-10-14 by reading each route and the service it calls, at the working tree on `main`
(`1dd7731` + uncommitted Kriya Ketu work). One file covers every read endpoint registered in KK-P4-01
instead of one file per endpoint: the checks are the same and reads have no CAS, idempotency or audit
row to check.

Checklist per endpoint: **Perm** = permission and where the route checks it (the drift test in
`tests/test_assistant_registry.py` checks dependency/role routes; `tests/test_assistant_reads.py` calls
each handler-checked route as staff without the permission and expects 403) · **Tenant** =
`enforce_clinic_access` + every query `.eq("clinic_id", …)` · **Branch** = branch-pinned staff confined ·
**Bounds** = every parameter bounded · **5xx** = no raw exception text in a response · **Off-loop** = all DB
calls through `await sb(...)` (no `.execute()` in the handlers or the services they call; grep 2026-10-14).

Owner decision (2026-10-14): a read whose only gap is an unbounded parameter is registered, because Kriya
Ketu sends a fixed in-range value; the gap is logged as a defect for its own fix. A branch-scope gap holds
the action back until it is fixed.

| Endpoint | Action | Perm | Tenant | Branch | Bounds | 5xx | Off-loop | Result |
|---|---|---|---|---|---|---|---|---|
| `GET /admin/stats` | `stats.summary` | none | ✔ | clinic-wide counts only (no PHI), as on the dashboard | ✔ `days` bounded (D-23 fixed) | ✔ fixed flag (D-24 fixed) | ✔ | Registered |
| `GET /admin/insights` | `insights.view` | admin (role) | ✔ | admin only | ✔ clamped to `_INSIGHTS_MAX_DAYS` | ✔ | ✔ | Registered |
| `GET /admin/opd/analytics` | `opd.analytics` | OPD_ADMIN (handler) + `opd_enabled` | ✔ | ✔ `_read_branch` | ✔ 50,000-row cap | ✔ | ✔ | Registered |
| `GET /admin/appointments` | `appointments.search` | none (DOCTOR/CASHIER confined by `verify_credentials`) | ✔ | ✔ `_branch_kw` | ✔ limit ≤ 100, span ≤ 366 d | ✔ | ✔ | Registered |
| `GET /admin/bookings/pending-review` | `bookings.pending_review` | none | ✔ | ✔ `restrict_to_branch` | ✔ 2,000 | ✔ | ✔ | Registered |
| `GET /admin/payments/reconciliation` | `payments.reconciliation` | admin (role) | ✔ | admin only | ✔ `date_str` validated, IST default (D-25 fixed) | ✔ | ✔ | Registered |
| `GET /admin/payments/stats` | `payments.stats` | admin (role) | ✔ | admin only | ✔ `days` bounded (D-23 fixed) | ✔ | ✔ | Registered |
| `GET /admin/opd/queue` | `opd.queue` | OPD_FRONT_DESK / OPD_CLINICAL / OPD_BILLING (handler, any) + OPD live | ✔ | ✔ `_read_branch`, doctor-only logins see their own patients | ✔ | ✔ | ✔ | Registered (`ActionSpec.also`) |
| `GET /admin/doctors` | `doctors.list` | none | ✔ | all clinic doctors (names, not PHI), as on the page | ✔ 2,000 | ✔ | ✔ | Registered |
| `GET /admin/lab-tests` | `lab_tests.list` | none | ✔ | catalogue (not PHI); `branch_id` ownership-checked | ✔ paged | ✔ `_friendly_db_error` | ✔ | Registered |
| `GET /admin/treatments` | `treatments.list` | none + `specialty_enabled` | ✔ | catalogue | ✔ | ✔ | ✔ | Registered |
| `GET /admin/reports/deliveries` | `reports.deliveries` | REPORTS_VIEW (dependency) | ✔ | `lab_reports` has no branch by design (migration 010) | ✔ `days` bounded (D-23 fixed) | ✔ | ✔ | Registered; phones masked by the route |
| `GET /admin/reports/queue` | `reports.queue` | REPORTS_VIEW (dependency) | ✔ | connector rows confined; `lab_reports` patient-scoped | ✔ 100 / 50 | ✔ | ✔ | Registered |
| `GET /admin/home-collection/visits` | `homecollect.visits` | HOME_COLLECTION_MANAGE (handler) + `home_collection_available` | ✔ | ✔ `restrict_to_branch` | ✔ date validated, 1,000 | ✔ | ✔ | Registered |
| `GET /admin/leads` | `leads.list` | LEADS_MANAGE (dependency `_LEADS`) | ✔ | WhatsApp contacts are clinic-level | ✔ all bounded | ✔ | ✔ | Registered |
| `GET /admin/messaging-usage` | `messaging.usage` | none | ✔ | counts only | ✔ no params | ✔ | ✔ | Registered (the route writes a `VIEW_MESSAGING_USAGE` audit row per call, as for the page) |
| `GET /admin/voice/calls` | `voice.calls` | VOICE_VIEW (handler) + `ai_receptionist_enabled` | ✔ | calls are clinic-level | ✔ days ≤ 90, limit ≤ 100 | ✔ | ✔ | Registered |
| `GET /admin/voice/live` | `voice.live` | VOICE_VIEW (handler) | ✔ | clinic-level | ✔ 50 | ✔ | ✔ | Registered |
| `GET /admin/voice/analytics` | `voice.analytics` | VOICE_VIEW (handler) | ✔ | clinic-level | ✔ days ≤ 90, 5,000 | ✔ | ✔ | Registered |
| `GET /admin/voice/usage` | `voice.usage` | VOICE_VIEW (handler) | ✔ | clinic-level | ✔ | ✔ | ✔ | Registered |
| `GET /admin/voice/outbound` | `voice.outbound_list` | VOICE_VIEW (handler) | ✔ | clinic-level | ✔ 100 | ✔ | ✔ | Registered |
| `GET /admin/notifications` | `notifications.list` | none (own notifications only) | ✔ | per admin | ✔ `limit`/`offset` bounded (D-23 fixed) | ✔ | ✔ | Registered |
| `GET /admin/audit-logs` | `audit.recent` | admin (role) | ✔ | admin only | ✔ `limit` bounded (D-23 fixed) | ✔ | ✔ | Registered; card shows time, user, action only (never `details`) |
| `GET /admin/ai/usage` | `ai.usage` | none | ✔ | counts | ✔ | ✔ | ✔ | Registered |
| `GET /admin/ai/budget` | `ai.budget` | none | ✔ | counts | ✔ | ✔ | ✔ | Registered |
| `GET /admin/subscription` | `subscription.view` | none | ✔ | clinic-level | ✔ | ✔ | ✔ | Registered |
| `GET /admin/data/storage` | `data.storage` | admin (role) | ✔ | counts | ✔ | ✔ | ✔ | Registered |
| `GET /admin/connectors/failed-reports` | `connectors.failed` | none | ✔ | ✔ fixed 2026-10-14 (D-21: own branch + branch-less rows; resolve confined too) | ✔ 2,000 | ✔ | ✔ | Registered 2026-10-14 |
| `GET /admin/patients` | `patients.search` | none | ✔ | ✔ fixed 2026-10-14 (D-22: patients with an appointment at the branch; fails closed) | ✔ | ✔ | ✔ | Registered 2026-10-14 |

Not registered in KK-P4-01 (owner, 2026-10-14): `patient.view`, `voice.call_detail` (need a record-id
resolver), `patient.duplicate_check` (→ KK-P4-07), `voice.test_turn` (→ KK-P4-13).

## KK-P4-14 briefings (PB-12 / PB-13), 2026-10-15
The briefings add no endpoint: each section is one optional GET already listed above or used by earlier tasks
(`/admin/appointments`, `/admin/leaves`, `/admin/opd/queue`, `/admin/bookings/pending-review`,
`/admin/reports/deliveries?state=failed`, `/admin/leads?segment=hot`, `/admin/voice/calls`,
`/admin/notifications/unread-count`), sent as the caller. A 4xx (permission, feature off) leaves the section
out; a 5xx or timeout shows "couldn't load it". Only counts, times and doctor names are shown or stored.
`GET /admin/leaves` has no date filter (≤ 2000 rows, clinic-scoped); the briefing filters today / tomorrow.
L4a (scheduled briefing to notifications) is not built: `admin_notifications` are read clinic-wide
(`broadcast.get_admin_notifications` ignores `admin_id`), see status.md §5 #17.
