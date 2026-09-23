# 03 — Database Model & Schema Intelligence: Kriya AI

## 1. Database Architecture Overview
The Kriya AI persistence layer runs on **Supabase PostgreSQL 15+**. Schema modifications are managed via 85 forward migrations in `migrations/` tracked by the `schema_migrations` table.

```text
                                  ┌────────────────────────┐
                                  │        clinics         │ (Root Tenant)
                                  └───────────┬────────────┘
                                              │ 1:N
        ┌───────────────────┬─────────────────┼─────────────────┬──────────────────┐
        ▼                   ▼                 ▼                 ▼                  ▼
  ┌───────────┐       ┌───────────┐     ┌───────────┐     ┌───────────┐      ┌───────────┐
  │ branches  │       │clinic_adm-│     │  doctors  │     │ lab_tests │      │specialty_ │
  │           │       │   ins     │     │           │     │           │      │treatments │
  └─────┬─────┘       └─────┬─────┘     └─────┬─────┘     └─────┬─────┘      └─────┬─────┘
        │                   │                 │                 │                  │
        │                   │                 │ 1:N             │ 1:N              │ 1:N
        │                   │                 ▼                 ▼                  ▼
        │                   │           ┌──────────────────────────────────────────────┐
        │                   └──────────►│                 appointments                 │
        │                               └───────────────────────┬──────────────────────┘
        │                                                       │ 1:N
        │                                                       ▼
        │                                               ┌──────────────┐
        │                                               │payment_events│
        │                                               └──────────────┘
        ▼
  ┌───────────┐
  │ patients  │◄────────────────────────┐
  └─────┬─────┘                         │
        │ 1:N                           │
        ├──────────────►┌───────────────┴┐
        │               │  lab_reports   │
        ▼ 1:N           └────────────────┘
  ┌───────────┐
  │family_mem-│
  │   bers    │
  └───────────┘
```

---

## 2. Complete 50-Table Inventory

| Table Name | Tenant Scoped? | Primary Key | Key Foreign Keys | Purpose |
|---|---|---|---|---|
| `clinics` | Master Tenant | `id` (UUID) | - | Root tenant entity: settings, WhatsApp number, WABA ID, plan, branding |
| `branches` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` -> `clinics.id` | Physical hospital/clinic branches with locations, OPD sessions |
| `clinic_admins` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id`, `branch_id` | Staff and admin logins, bcrypt password hashes, delegated roles |
| `admin_sessions` | Platform | `id` (UUID) | `clinic_id` -> `clinics.id` | Server-side revocable session tokens (SHA-256 hashed) |
| `admin_audit_logs` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id`, `user_id` | Immutable audit log of administrative mutations |
| `admin_notifications` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Alerts and push notifications generated for clinic staff |
| `doctors` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Doctor directory, qualifications, consultation fees, active status |
| `doctor_branches` | Junction | `id` (UUID) | `doctor_id`, `branch_id` | Doctor-to-branch schedule assignments and sessions |
| `doctor_leaves` | Yes (`clinic_id`) | `id` (UUID) | `doctor_id`, `clinic_id` | Scheduled doctor leaves and absence dates |
| `hospital_holidays` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Scheduled hospital-wide closures and public holidays |
| `patients` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Patient directory, normalized phone, language, DPDP consent |
| `family_members` | Yes (`clinic_id`) | `id` (UUID) | `patient_id`, `clinic_id` | Registered dependants and family members under a patient phone |
| `conversations` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Active WhatsApp conversational session state, FSM state, context JSON |
| `inbound_messages` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Durable inbound queue buffer for Meta webhooks |
| `processed_messages`| Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Idempotency deduplication gate with UNIQUE(message_id, clinic_id) |
| `failed_messages` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Dead-letter queue for lock timeouts and unhandled exceptions |
| `appointments` | Yes (`clinic_id`) | `id` (UUID) | `patient_id`, `doctor_id`, `branch_id`, `lab_test_id` | Master booking record: OPD consultations and lab tests |
| `payment_events` | Yes (`clinic_id`) | `id` (UUID) | `booking_id`, `clinic_id` | Ledger of Razorpay payment callbacks, captures, and refunds |
| `lab_tests` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id`, `branch_id` | Diagnostic test catalogue, prices, categories, preparation notes |
| `lab_reports` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id`, `patient_id` | Ingested lab reports, PDF storage file path, delivery receipts |
| `specialty_treatments`| Yes (`clinic_id`)| `id` (UUID)| `clinic_id` | Specialty clinical treatment packages (IVF, Derma, Dental, etc.) |
| `treatment_doctors` | Yes (`clinic_id`) | `id` (UUID) | `treatment_id`, `doctor_id`, `clinic_id` | Doctors mapped to specialty treatments |
| `prescriptions` | Yes (`clinic_id`) | `id` (UUID) | `appointment_id`, `patient_id`, `clinic_id` | Medication prescriptions, dosages, duration |
| `prescription_reminder_sends`| Yes (`clinic_id`)| `id` (UUID)| `prescription_id`, `clinic_id`| Ledger of scheduled medication reminders delivered |
| `broadcasts` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Broadcast campaigns dispatched to patients or staff |
| `integration_connectors`| Yes (`clinic_id`)| `id` (UUID)| `clinic_id`, `branch_id` | LIMS/EHR connector credentials (MocDoc, Fernet encrypted) |
| `connector_failed_reports`| Yes (`clinic_id`)| `id` (UUID)| `connector_id`, `clinic_id` | Dead-letter storage for unparseable or unmatchable lab reports |
| `connector_audit_log` | Yes (`clinic_id`)| `id` (UUID)| `connector_id`, `clinic_id`| Polling audit trail for LIMS connector executions |
| `integration_processed_reports`| Yes (`clinic_id`)| `id` (UUID)| `clinic_id`| Deduplication ledger preventing re-download of existing reports |
| `analytics_events` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Patient interaction events, booking drop-offs, funnel steps |
| `clinic_daily_usage`| Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Daily report delivery and message volume counters |
| `ai_usage_ledger` | Yes (`clinic_id`) | `id` (UUID) | `clinic_id` | Token counts and financial costs for every AI invocation |
| `catalogue_import_previews`| Yes (`clinic_id`)| `id` (UUID)| `clinic_id`| Staged diagnostic price list imports awaiting admin confirmation |
| `weekly_insights_summaries`| Yes (`clinic_id`)| `id` (UUID)| `clinic_id`| AI-generated weekly operational summaries for hospital leadership |
| `outbound_message_ledger`| Yes (`clinic_id`)| `id` (UUID)| `clinic_id`| Meta WABA outbound message log with billing category tags |
| `plan_tiers` | Platform | `id` (VARCHAR) | - | Platform subscription plan definitions and feature matrices |
| `platform_billing_rates`| Platform | `id` (UUID) | - | Platform charges and per-unit billing rates |
| `platform_expenses` | Platform | `id` (UUID) | - | Platform operating expenses and cloud infrastructure costs |
| `platform_invoices` | Platform | `id` (UUID) | `clinic_id` -> `clinics.id` | Invoices generated by platform owner for hospital subscriptions |
| `meta_pricing_config`| Platform | `id` (UUID) | - | Meta conversation pricing tiers (Utility, Marketing, Service) |
| `rate_limits` | Platform | `id` (UUID) | - | Distributed rate limiting counter records (IP and key based) |
| `scheduler_locks` | Platform | `id` (VARCHAR) | - | Distributed CAS advisory lease locks for APScheduler jobs |
| `schema_migrations` | Platform | `id` (SERIAL) | - | Migration execution records and SHA-256 checksums |
| `webhook_security_events`| Platform | `id` (UUID) | - | Security event log for invalid webhook signatures |
| `callmedex_audit_logs`| Subsystem | `id` (UUID) | - | CallMedex LIMS integration audit log |
| `callmedex_connector_configs`| Subsystem | `id` (UUID) | - | CallMedex connector configurations |
| `callmedex_report_jobs`| Subsystem | `id` (UUID) | - | CallMedex queued PDF extraction jobs |
| `callmedex_whatsapp_settings`| Subsystem | `id` (UUID) | - | CallMedex WABA credentials |
| `_dup_lab_tests` | Temporary | `id` (UUID) | - | Migration 076 cleanup scratch table (deduplication) |
| `_verification_results`| Temporary | `id` (UUID) | - | Schema verification run results |

---

## 3. Core Table Definitions & Constraints

### 3.1 `appointments` (Core Operational Entity)
- **Columns**: `id` (UUID PK), `clinic_id` (UUID NOT NULL), `patient_id` (UUID), `doctor_id` (UUID NULL for lab tests), `branch_id` (UUID), `lab_test_id` (UUID NULL for OPD), `appointment_date` (DATE NOT NULL), `appointment_time` (TIME NOT NULL), `status` (VARCHAR NOT NULL), `booking_ref` (VARCHAR NOT NULL), `payment_status` (VARCHAR DEFAULT 'pending'), `amount_paise` (INTEGER), `razorpay_payment_link_id` (VARCHAR), `queue_token` (VARCHAR), `followup_sent` (BOOLEAN DEFAULT FALSE), `created_at`, `updated_at`.
- **Status Lifecycle States**:
  - `pending_payment`: Held for 10 minutes awaiting online payment.
  - `pending_review`: Held awaiting staff review.
  - `confirmed`: Confirmed booking with reserved doctor slot.
  - `completed`: Closed visit after appointment date passes.
  - `cancelled`: Cancelled by patient or staff.
- **Constraints**:
  - `chk_appointments_status`: `status IN ('pending_payment', 'pending_review', 'confirmed', 'completed', 'cancelled')`
  - Unique Slot Invariant (Migration 064): `idx_appointments_slot_unique` enforces unique `(clinic_id, doctor_id, appointment_date, appointment_time)` WHERE `status IN ('confirmed', 'pending_payment', 'pending_review')`. This prevents double-booking at the database level!

### 3.2 `lab_reports` (Diagnostic Ingestion Entity)
- **Columns**: `id` (UUID PK), `clinic_id` (UUID NOT NULL), `patient_id` (UUID), `patient_phone` (VARCHAR NOT NULL), `patient_name` (VARCHAR), `test_name` (VARCHAR), `file_path` (VARCHAR), `report_date` (DATE), `delivery_status` (VARCHAR NOT NULL), `whatsapp_message_id` (VARCHAR), `ai_summary` (TEXT), `match_source` (VARCHAR), `retry_count` (INTEGER DEFAULT 0), `created_at`.
- **Delivery Lifecycle States**:
  - `pending_upload`: Report discovered in LIMS, awaiting download.
  - `pending_review`: Held because phone is unknown (`match_source = 'moc_doc_only'`) and clinic config requires verification.
  - `pending_retry`: Transient WhatsApp delivery failure; scheduled for retry.
  - `sent`: Document message or template dispatched to Meta API.
  - `delivered`: Meta confirmed delivery to patient's device.
  - `read`: Patient opened the WhatsApp message.
  - `failed`: Permanent delivery failure (invalid number or block).
  - `dismissed`: Staff dismissed the report from admin queue.

### 3.3 `inbound_messages` (Durable Message Buffer)
- **Columns**: `id` (UUID PK), `message_id` (VARCHAR UNIQUE NOT NULL), `clinic_id` (UUID NULL until resolved), `phone` (VARCHAR NOT NULL), `display_phone` (VARCHAR NOT NULL), `phone_number_id` (VARCHAR), `payload` (JSONB NOT NULL), `status` (VARCHAR NOT NULL), `retry_count` (INTEGER DEFAULT 0), `locked_at` (TIMESTAMPTZ), `locked_by` (VARCHAR), `created_at`.
- **States**: `received` -> `processing` -> `completed` / `failed_retryable` / `dead_letter`.

### 3.4 `clinics` (Tenant Definition)
- **Columns**: `id` (UUID PK), `name` (VARCHAR NOT NULL), `whatsapp_number` (VARCHAR NOT NULL), `phone_number_id` (VARCHAR), `waba_id` (VARCHAR), `plan` (VARCHAR DEFAULT 'standard'), `is_active` (BOOLEAN DEFAULT TRUE), `status` (VARCHAR DEFAULT 'ACTIVE'), `config` (JSONB DEFAULT '{}'), `created_at`, `deleted_at`.
- **Plan Tiers**: `starter`, `standard`, `premium`, `enterprise`, `diagbooking`, `derma`, `eye`, `dental`, `ivf`, `hospital`, `women_child`.

---

## 4. Row Level Security (RLS) vs Application Scoping

Migration 049 (`049_force_row_level_security.sql`) enforces RLS policies across all tenant tables using:
```sql
CREATE POLICY tenant_isolation_policy ON appointments
    FOR ALL
    USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid);
```
**CRITICAL OPERATIONAL FACT**:
The FastAPI application connects to Supabase using `SUPABASE_SERVICE_ROLE_KEY`. Under PostgreSQL internals, the `service_role` superuser holds the `BYPASSRLS` attribute. Therefore:
1. Postgres ignores RLS policies for all queries issued by `app.database.supabase`.
2. Multi-tenant isolation is enforced **strictly in Python application code** via [`app/tenancy.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/tenancy.py) (`TENANT_OWNED_TABLES`) and [`app/database.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py) (`scoped_query()`).
3. If an engineer writes a direct `supabase.table("patients").select("*")` without `scoped_query` or `.eq("clinic_id", clinic_id)`, PostgreSQL will return rows belonging to **every hospital on the platform**.
4. The automated safety gate against this is `tests/test_lint_unscoped_queries.py`, which parses the Python AST and fails CI if any unscoped query is added.
