# Session 28 — Home Sample Collection (Diagnostic Plans)

**Date:** 2026-10-01 · **Migration:** 097 (`migrations/097_home_sample_collection.sql`)  
**Plans affected:** `diagnostic_stream` and `diagnostic_test_booking` (Centre opt-in only)  
**Origin:** Comprehensive home sample collection system for diagnostic laboratory clients. Enables patients to book blood/pathology sample collection at their home or workplace directly through WhatsApp with interactive GPS pin drops, automated least-busy phlebotomist assignment, real-time status notifications, and a dedicated mobile-friendly phlebotomist portal.

---

## 1. Architectural Decisions

1. **Safe by Default & Per-Centre/Branch Opt-In**:
   - Gated behind a centre/branch configuration flag: `clinics.config / branches.config -> home_collection.enabled`.
   - Disabled by default across all diagnostic centres until explicitly activated in settings.
   - For all unenabled centres, the patient WhatsApp conversation remains 100% unchanged.
   - Existing bookings in the database default cleanly to `collection_mode = 'centre'`.

2. **Unified Data Model & Booking Pipeline**:
   - Rather than creating a detached secondary booking system, home sample collection is integrated directly into the existing `appointments` table (`booking_type = 'lab_test'`).
   - Reuses existing payment temporary holds, Razorpay settlement webhooks, slot concurrency guards, automated refunds, expiration jobs, and financial ledger accounting without duplicating logic or introducing drift.
   - Extended with specific collection columns: `collection_mode` ('centre' vs 'home'), `collection_slot`, `collection_address`, `collection_landmark`, `collection_lat`, `collection_lng`, `collection_contact_phone`, `home_collection_fee_paise`, `phlebotomist_id`, `collection_status`, `collection_status_at`, and `collection_notes`.

3. **Multi-Step WhatsApp Home Collection Experience**:
   - After selecting a booking date, patients choose between **🏡 Home Collection** or **🏥 Visit Centre**.
   - If **Home Collection** is chosen:
     1. **Time Slot Selection:** Dynamically generated based on centre slot duration and capacity constraints.
     2. **Beneficiary Confirmation:** Specifies whether the test is for self or a family member.
     3. **Interactive GPS Location Request:** Dispatches a WhatsApp interactive "Send Location" pin request (mandatory). Supports pasted Google Maps links as a seamless fallback for WhatsApp Web or older clients.
     4. **Address Details:** Collects house/flat number, apartment/building name, street, and landmark. Returning patients are offered a 1-tap "Same address as last time" option.
     5. **Phlebotomist Contact Number:** Designates the direct mobile number the phlebotomist should call upon arrival.
     6. **Fee Transparency & Summary:** Displays the test fees, home collection fee (or waivers if order exceeds free threshold), and proceeds to confirmation / payment hold.
   - **Exclusion Filters:** In-centre-only procedures (e.g. Scans, X-rays, ECG, Ultrasound, MRI) are never offered for home collection.
   - **Multilingual Support:** Complete localization across English, Hindi, and Telugu.

4. **Automated Least-Busy Phlebotomist Assignment Engine**:
   - **Assignment Heuristic:** Upon payment confirmation, the visit is assigned to the active phlebotomist with the fewest assigned visits in that specific time slot; ties are broken by fewest total visits for that date.
   - **Branch Affinity:** Phlebotomists mapped to a specific branch are exclusively assigned visits within that branch's territory.
   - **Background Sweep Job:** An APScheduler job runs every 5 minutes (`assign_unassigned_home_collections`) to catch and assign any unassigned visits (e.g., late payment confirmations or off-peak bookings).
   - **Deactivation Cascade:** If a phlebotomist account is deactivated/disabled by admin, their upcoming visits are automatically redistributed to other active phlebotomists.
   - **Fallback Alerts:** If no phlebotomist is available within the slot capacity, the visit remains `unassigned` and alerts the centre dispatch desk.
   - **Concurrency Safety:** Assignment and status updates enforce optimistic CAS checking (`WHERE phlebotomist_id IS NULL` or matching status), preventing race conditions between webhooks, background jobs, and manual admin overrides.

5. **Phlebotomist Role & Dedicated Mobile Web Portal**:
   - Added staff role: `staff_role = 'phlebotomist'` with mandatory `full_name` and `phone` in `clinic_admins`.
   - **Strict HTTP-Level Endpoint Confinement:** A phlebotomist login is strictly restricted at the API layer to `/admin/home-collection/my-visits` and `/admin/home-collection/my-visits/{id}/status`. Every other `/admin/*` and `/fhir/*` route returns `HTTP 403 Forbidden`.
   - **Phone-Friendly Interface:** Renders visit cards with 1-tap **Navigate** (Google Maps deep link), **Call** (`tel:` link), and status progression buttons.
   - **Status Lifecycle:** `assigned` ➔ `en_route` (Patient gets WhatsApp alert) ➔ `collected` ➔ `delivered`. Unsuccessful visits can be marked `failed` (mandatory reason required).
   - **Live Polling:** The mobile portal auto-refreshes every 60 seconds and emits browser alerts on new visit assignments. Re-authenticates on every request so deactivations apply instantaneously.

6. **Fixes to Existing Regressions & DPDP Erasure**:
   - **Lab Test Payment Confirmation Text:** Fixed an existing bug in fast-poll and recovery payment paths where lab tests were announced using doctor consultation templates (`Doctor: N/A`). Now correctly formats lab tests with test names and collection mode.
   - **DPDP Act 2023 Erasure:** Updated `data_retention.anonymize_clinical_records` to redact `collection_address = '[REDACTED]'`, drop GPS coordinates (`collection_lat = NULL`, `collection_lng = NULL`), and nullify `collection_contact_phone`.

---

## 2. What Shipped

| Component | Files & Details |
| :--- | :--- |
| **Database Migration (097)** | `migrations/097_home_sample_collection.sql`: Added 12 columns to `appointments` with check constraints for valid modes (`centre`, `home`), statuses (`unassigned`, `assigned`, `en_route`, `collected`, `delivered`, `failed`), GPS coordinate boundaries, and completion integrity. Added `full_name` and `phone` to `clinic_admins`. Added indexes for day-view and phlebotomist queries. Added `migrations/rollback/097_down.sql`. |
| **Service Layer** | `app/services/home_collection.py`: Core business logic for distance calculations (Haversine formula), slot capacity checks, least-busy phlebotomist assignment, status transitions with CAS locks, and unassigned recovery sweeps.<br>`app/services/home_collection_flow.py`: Conversational FSM steps for location request, maps URL parsing, address intake, phone confirmation, and order summary generation. |
| **Routers & APIs** | `app/routers/home_collection.py`: Admin endpoints mounted under `/admin/home-collection` for dispatch day views, manual reassignment, batch auto-assignment, centre settings, and phlebotomist mobile APIs (`/my-visits`, `/my-visits/{id}/status`). |
| **Conversation & Webhook** | `app/routers/webhook.py` & `app/services/conversation.py`: Handlers for WhatsApp interactive location payloads and text Google Maps URLs. Integrated into lab test booking flow. |
| **Payment & Confirmations** | `app/services/payment.py`: Corrected lab test confirmation text across all 4 payment confirmation paths (webhook, fast-poll, recovery job, and cash on arrival). Triggers auto-assignment and phlebotomist WhatsApp alert on confirmation. |
| **Background Scheduler** | `app/services/scheduler.py`: Added `assign_unassigned_home_collections` recurring job running every 5 minutes with distributed locking (`scheduler_locks`). |
| **Auth & Permissions** | `app/routers/admin.py` & `app/services/permissions.py`: Added `phlebotomist` staff role, endpoint lockdown in `verify_credentials`, and `HOME_COLLECTION_MANAGE` granular staff permissions. |
| **Data Retention & Privacy** | `app/services/data_retention.py`: DPDP data erasure path updated to redact home addresses, wipe GPS coordinates, and clear collection contact numbers. |
| **Admin Panel UI** | `admin/index.html`: Added **Home Collections** navigation tab, dispatch view with date/branch filters, reassign dropdowns, auto-assign buttons, phlebotomist load roster, centre settings form, and the dedicated mobile phlebotomist portal. |
| **Tests** | `tests/test_home_collection.py` (79 tests) and `tests/test_home_collection_conversation.py` (11 tests). |

---

## 3. End-to-End Operational Lifecycle

```
[ Step 1: Patient Booking on WhatsApp ]
  Patient selects Lab Tests ➔ Picks Date ➔ Chooses 🏡 Home Collection.
  Selects Time Slot ➔ Specifies Patient (Self/Family).
  Taps native "Send Location" pin (or pastes Google Maps link).
  Types house number, building name, and landmark.
  Confirms phlebotomist contact mobile number.
  Reviews summary with home collection fee ➔ Pays via Razorpay UPI.

[ Step 2: Confirmation & Auto-Assignment ]
  Payment webhook confirms booking.
  Assignment engine assigns visit to the least-busy active phlebotomist in that slot.
  Patient receives confirmation on WhatsApp with Phlebotomist Name and Mobile Number.
  Phlebotomist receives visit on their mobile portal.

[ Step 3: Phlebotomist Dispatch & Collection ]
  Phlebotomist opens mobile portal: views address, 1-tap Google Maps Navigation, and Call button.
  Marks "On the Way": Patient receives WhatsApp alert that phlebotomist is en route.
  Arrives and collects sample ➔ Marks "Collected".
  If patient is unavailable/unreachable: Marks "Couldn't Collect" with mandatory reason.

[ Step 4: Admin Dispatch Oversight ]
  Lab admin monitors day view across branches.
  Can manually reassign visits or trigger batch auto-assignment.
```

---

## 4. Verification Evidence

| Verification Target | Scope & Test Suite | Result |
| :--- | :--- | :--- |
| **Unit & Integration Suite** | `tests/test_home_collection.py` (79 tests) — Real Postgres constraints, Haversine radius, slot capacity, least-busy assignment, CAS status updates, role confinement, visit ownership. | 79 passed |
| **Conversational Flow Suite** | `tests/test_home_collection_conversation.py` (11 tests) — WhatsApp location payload parsing, maps URLs, address intake, FSM transitions, payment confirmations. | 11 passed |
| **AST Tenant Isolation Linter** | `tests/test_lint_unscoped_queries.py` across all new routes and service functions. | 4 passed |
| **Security & Regressions** | `tests/test_audit_p0_p1_regressions.py` (RT-16 migration ownership, route matrices). | 6 passed |
| **Full Repository Test Suite** | Full hermetic repository test run. | 3,778 passed |
| **Browser UI & Confinement** | Verified admin dispatch view, settings form, and phlebotomist mobile card view in Chrome; verified phlebotomist token is blocked on all other routes. | Verified |

---

## 5. Deployment & Go-Live Runbook

1. **Supabase SQL Editor (Pre-Deploy)**:
   Execute `migrations/097_home_sample_collection.sql` on the live database and record the migration:
   ```sql
   INSERT INTO schema_migrations (name, checksum) VALUES
       ('097_home_sample_collection.sql', 'fb683e1e50d4ee109b311a3f8775d5ab2f6848ad2f1d91c9929918ed735dfb89')
   ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum;
   ```

2. **Verify Database Columns & Constraints**:
   ```sql
   SELECT column_name, data_type, is_nullable 
   FROM information_schema.columns 
   WHERE table_name = 'appointments' AND column_name LIKE 'collection_%';
   ```

3. **Deploy Codebase to Render**:
   - Push commit to `main`.
   - Render startup pre-flight check in `app/main.py` will confirm `highest_applied (097) >= disk (097)` and boot cleanly.

4. **Centre Activation & Setup**:
   - Diagnostic Centre Admin logs into `admin/index.html`.
   - Navigates to **Staff Accounts** ➔ Creates Phlebotomist accounts (specifying Name and Mobile Phone).
   - Navigates to **Home Collections** ➔ Opens Settings tab.
   - Configures:
     * Toggle: **Enable Home Sample Collection**.
     * Home Collection Fee (₹) & Free Collection Threshold (₹).
     * Slot duration (e.g. 60 mins) & Max visits per slot (e.g. 3).
     * Service radius (e.g. 15 km) and Centre GPS coordinates.
     * Minimum advance notice (e.g. 120 mins).

---

## 6. Operational Considerations, Edge Cases & Known Unknowns

- **WhatsApp Native Location Button on Real Handsets:** The interactive "Send Location" button is part of Meta WhatsApp Cloud API v21.0. If older or non-standard third-party WhatsApp clients fail to render the button, patients receive explicit typed instructions to paste a Google Maps link or type their location.
- **Meta 24-Hour Messaging Window:** Real-time WhatsApp alerts for phlebotomist dispatch ("on the way") are free-form customer service messages. If a patient books sample collection multiple days in advance, outbound status updates sent outside the 24-hour window require an approved Meta Utility Message Template.
- **High-Concurrency Last-Slot Overbooking:** Slot capacity checks enforce atomic reads, but near-simultaneous checkouts (within milliseconds of each other) on the very last available slot capacity could theoretically overbook by one visit before slot exhaustion takes effect.
- **WhatsApp 10-Row Interactive List Limit:** Meta restricts WhatsApp interactive list menus to a maximum of 10 rows. When short slot intervals (e.g. 30 mins) span a wide operating window (e.g. 8 AM to 8 PM = 24 slots), only the first 10 chronological slots are displayed.
- **Partial Online Deposits:** If a diagnostic centre configures partial online deposits (e.g., 20% advance hold online), the phlebotomist portal card does not currently compute the residual cash balance due at the doorstep.
- **Patient Cancellation Dispatch Alert:** If a patient cancels an appointment, the visit is removed from the phlebotomist's portal list upon refresh, but an active outbound push notification to the phlebotomist is not yet dispatched.
