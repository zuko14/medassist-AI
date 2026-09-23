# 02 — End-to-End System & Business Flows: Kriya AI

This document traces the complete execution paths of all 15 core business and infrastructure workflows implemented in Kriya AI, derived directly from active source code.

---

## Flow 1: Inbound WhatsApp Message Ingress & State Machine

```text
[Meta WhatsApp Cloud API]
       │
       ▼  POST /webhook (Headers: X-Hub-Signature-256)
[app/routers/webhook.py: receive_webhook]
       │
       ├─► 1. Verify HMAC-SHA256 signature using settings.meta_app_secret
       ├─► 2. Extract display_phone_number & phone_number_id
       ├─► 3. Resolve Tenant Clinic: resolve_tenant(display_phone_number, phone_number_id)
       ├─► 4. Durable Enqueue: INSERT INTO inbound_messages (status='received')
       │      ─── RETURN HTTP 200 OK TO META (< 200ms) ───
       │
       ▼ (FastAPI BackgroundTask: process_message_safe)
[app/services/message_queue.py: acquire]
       │
       ├─► 5. Atomic Gate: INSERT INTO processed_messages (message_id, clinic_id) ON CONFLICT DO NOTHING
       │      (If duplicate -> log and drop)
       ├─► 6. Inbound Status: UPDATE inbound_messages SET status='processing'
       ├─► 7. Acquire Per-Phone Lock: asyncio.Lock + CAS lease on scheduler_locks ('phone_...')
       │
       ▼
[app/services/conversation.py: ConversationManager.handle_message]
       │
       ├─► 8. Mark Read: Asynchronously send read receipt via whatsapp_service.mark_as_read()
       ├─► 9. Guard 1: Duplicate check via conversation.last_processed_message_id
       ├─► 10. Guard 2: 30-minute mid-booking session timeout check
       ├─► 11. Guard 3: Unreadable media filter (Audio, images, PDFs -> "Please type as text")
       ├─► 12. Guard 4: Clinical Safety Firewall (Blocks medication & diagnosis questions)
       ├─► 13. Intent Classification: ai_engine.detect_intent (OpenRouter -> Groq -> Keyword fallback)
       ├─► 14. Execute State Handler: Run transition for current ConversationState
       │
       ▼
[app/services/whatsapp.py: WhatsAppService.send_text / send_interactive]
       │
       ├─► 15. Meta Graph API POST https://graph.facebook.com/v22.0/{phone_number_id}/messages
       ├─► 16. Outbound Ledger: INSERT INTO outbound_message_ledger (category, cost, wamid)
       ├─► 17. Queue Complete: UPDATE inbound_messages SET status='completed'
       └─► 18. Release Phone Lock & Lease
```

---

## Flow 2: Patient Identification, Language Selection & DPDP Consent

```text
Patient messages clinic for the first time
       │
       ▼
[app/services/conversation.py: _handle_message_locked]
       │
       ├─► 1. get_patient_by_phone(clinic_id, phone) -> Returns None
       ├─► 2. create_patient(clinic_id, phone) -> Inserts new row with language=None, consent_given=False
       ├─► 3. New state -> ConversationState.SELECTING_LANGUAGE
       │
       ▼
[whatsapp_service.send_interactive_buttons]
       │
       ├─► 4. Prompts language options: English ("en"), Hindi ("hi"), Telugu ("te")
       │
       ▼ Patient taps Language Button (e.g., "English")
[conversation.py: _handle_language_selection]
       │
       ├─► 5. UPDATE patients SET language='en' WHERE phone=... AND clinic_id=...
       ├─► 6. Check DPDP Consent: If consent_given is False:
       │      • State -> ConversationState.AWAITING_CONSENT
       │      • Sends DPDP consent text: "By chatting with us you consent to receiving appointment updates..."
       │      • Buttons: "I Agree" / "Decline"
       │
       ▼ Patient taps "I Agree"
[conversation.py: _handle_consent]
       │
       ├─► 7. UPDATE patients SET consent_given=True, consent_timestamp=NOW()
       ├─► 8. Log Consent: INSERT INTO consent_audit_log
       ├─► 9. State -> ConversationState.MAIN_MENU
       └─► 10. Send Main Menu (Book Appointment, Lab Tests, Our Doctors, Hospital Info, etc.)
```

---

## Flow 3: Multi-Branch & Doctor Appointment Booking Flow

```text
Patient taps "Book Appointment" or types "I need an appointment"
       │
       ▼
[conversation.py: _start_booking]
       │
       ├─► 1. Branch Check:
       │      • If clinic has multi_branch plan AND >1 active branch:
       │        State -> SELECTING_BRANCH -> Prompts branch list (Interactive List)
       │      • Else: Uses default clinic branch context
       │
       ├─► 2. "Who is this appointment for?" Prompt:
       │      • Checks family_members table for registered members
       │      • Options: "For Me" / Saved Members / "Add New Person"
       │      • State -> SELECTING_FAMILY_MEMBER or COLLECTING_NAME
       │
       ├─► 3. Symptoms Collection:
       │      • State -> COLLECTING_SYMPTOMS
       │      • Patient types symptoms (e.g., "severe tooth pain")
       │      • map_symptom_to_department() matches to "Dental"
       │      • State -> SUGGESTING_DEPARTMENT ("Would you like to book with Dental?")
       │
       ├─► 4. Doctor Selection:
       │      • Patient confirms department or picks from list
       │      • Fetches active doctors via get_doctors(clinic_id, dept, branch_id)
       │      • State -> SELECTING_DOCTOR -> Interactive list of doctors with consultation fees
       │
       ├─► 5. Date & Slot Picker:
       │      • Doctor selected -> get_available_slots(clinic_id, doctor_id, date)
       │      • Checks hospital_holidays (cached) & doctor_leaves BEFORE computing slots
       │      • Filters out booked slots:
       │        SELECT appointment_time FROM appointments WHERE doctor_id=... AND appointment_date=...
       │        AND status IN ('confirmed', 'pending_payment', 'pending_review')
       │      • State -> SELECTING_SLOT
       │
       ├─► 6. Confirmation & Hold:
       │      • Patient selects slot (e.g., "10:30 AM")
       │      • State -> CONFIRMING_BOOKING
       │      • Patient confirms -> Checks payment mode (resolve_payment_mode)
       │      • If Online Payment Mandatory/Optional: Moves to Payment Flow (Flow 5)
       │      • If Pay at Counter ("none"):
       │        - Inserts into appointments with status='confirmed', booking_ref='MC-XXXXXX'
       │        - Generates queue token: Token #A-01
       │        - Sends WhatsApp booking confirmation template with Google Maps link
       └─► 7. State -> IDLE or MAIN_MENU
```

---

## Flow 4: Lab Test Catalogue Search & Diagnostic Booking

```text
Patient types "book lab test" or taps "Lab Tests / Services"
       │
       ▼
[conversation.py: _start_lab_booking]
       │
       ├─► 1. Checks clinic plan has 'lab_test_booking' feature enabled
       ├─► 2. State -> BROWSING_LAB_TESTS
       ├─► 3. Shows Lab Test Menu:
       │      • "Search by Name / Symptom"
       │      • "Browse by Category" (Blood Tests, Thyroid, Diabetes, Radiology...)
       │      • "Popular Health Packages"
       │
       ▼ Patient types search query (e.g., "sugar test" or "షుగర్")
[hybrid_search.py: search_tests_multilingual]
       │
       ├─► 4. Zero-LLM Deterministic Synonym Resolution:
       │      • Checks Hindi/Telugu translation maps ("షుగర్" -> ["sugar", "glucose", "hba1c"])
       │      • Evaluates word boundary matches against lab_tests table
       │      • Uses difflib for typo tolerance (e.g. "cholestrol" -> "cholesterol")
       │
       ▼
[conversation.py: _show_lab_test_results]
       │
       ├─► 5. Returns matching tests with prices:
       │      "1. Fasting Blood Sugar (FBS) — ₹150"
       │      "2. HbA1c (Glycated Hemoglobin) — ₹450"
       │      Buttons / List Reply to select test
       │
       ▼ Patient selects test
[conversation.py: _handle_lab_test_selection]
       │
       ├─► 6. Checks sample collection requirements (e.g. "10-12 hours fasting required")
       ├─► 7. Prompts for Collection Date: 3 buttons from _next_collection_dates(window).
       │      Window = branches.config.lab_collection > clinics.config.lab_collection > default
       │      (admin "Collection window" editor). Today is included while now(IST) < end
       │      (sunday_end on Sundays when both Sunday times are set). A labdate_ tap not in
       │      the current 3 dates (past / closed today) is refused and dates re-offered.
       ├─► 8. State -> CONFIRMING_COLLECTION_DATE
       ├─► 9. Prompts for Patient Name
       ├─► 10. Booking Creation:
       │       INSERT INTO appointments (
       │           clinic_id, patient_id, appointment_type='lab_test',
       │           lab_test_id=..., appointment_date=..., status='confirmed'
       │       )
       └─► 11. Sends confirmation card with preparation instructions & fasting notice
```

---

## Flow 5: Razorpay Online Payment & Webhook Lifecycle

```text
Booking requires payment (doctor fee or lab test fee)
       │
       ▼
[payment.py: PaymentService.create_booking_with_payment]
       │
       ├─► 1. Create Appointment Hold:
       │      INSERT INTO appointments (status='pending_payment', hold_expires_at=NOW() + 10 min)
       ├─► 2. Read Clinic Razorpay Credentials:
       │      get_razorpay_creds(clinic) -> (key_id, key_secret, webhook_secret) from clinics.config
       ├─► 3. Call Razorpay API: POST https://api.razorpay.com/v1/payment_links
       │      Carries reference_id = booking_ref, notes = {"booking_id": uuid}
       ├─► 4. Store razorpay_payment_link_id in appointments
       ├─► 5. Send Payment Link to patient via WhatsApp text
       │
       ▼ Patient completes payment on Razorpay checkout
[app/routers/razorpay_webhook.py: razorpay_webhook]
       │
       ├─► 6. Webhook receives POST /webhooks/razorpay/{clinic_id}
       ├─► 7. Reads raw body & X-Razorpay-Signature header
       ├─► 8. verify_webhook_signature: HMAC-SHA256 verification against clinic's webhook_secret
       │
       ▼
[payment.py: PaymentService.process_payment_webhook]
       │
       ├─► 9. Extract event: "payment_link.paid" or "payment.captured"
       ├─► 10. Match booking: Query appointments WHERE razorpay_payment_link_id=... OR id=notes.booking_id
       ├─► 11. Idempotency: appointments WHERE payment_id=<id> AND status='confirmed' (clinic-scoped)
       │       -> already processed, 200. (Razorpay sends BOTH payment.captured and
       │       payment_link.paid for one payment; every write below is CAS on status.)
       ├─► 12. Amount mismatch -> pending_review (never auto-confirmed); settled rows untouched + admin alert
       ├─► 13. By current status:
       │       • pending_payment -> CAS to 'confirmed', notify patient + admin
       │       • expired, OR cancelled with no payment_id (patient cancelled an unpaid
       │         booking; its payment link stays live) -> LATE PAYMENT:
       │           _refund_payment_id (idempotency key late-{payment_id})
       │           success: CAS -> 'refunded'; only the delivery that moved the row notifies
       │           failure: row UNCHANGED, admin alerted to refund manually, patient told
       │                    "our team will process it" (never told a refund started)
       │       • completed/refunded/cancelled -> blocked; admin alerted if this is a
       │         DIFFERENT payment_id than the one the booking settled on
       └─► 14. Return HTTP 200 (503 only when the idempotency read itself fails)
```

### Refunds & the cancellation window (verified 2026-09-23, Session 20)

`PaymentService.initiate_refund(..., enforce_window=True)` is the single refund path.

| Caller | enforce_window | Why |
| :--- | :--- | :--- |
| Patient cancels on WhatsApp (`conversation._cancel_with_refund`) | True | Clinic's `config.cancellation_window_hours` policy, as quoted in the booking confirmation |
| Admin Refund button (`POST /admin/bookings/{id}/refund`) | False | Staff can always return money |
| Admin Reject pending_review (`admin_reject_booking`) | False | Clinic cannot honour the payment |
| Admin Cancel confirmed (`admin_cancel_confirmed_booking`) | False | Clinic-initiated cancellation |
| Doctor full-day leave (`scheduler.check_doctor_leaves`) | False | Clinic-initiated; refund failure -> cancel + admin alert |

`appointment_time` is a Postgres TIME (returned as `HH:MM:SS`). Before Session 20 the
window parser accepted only `HH:MM`, so the window was never enforced in production
and every paid cancellation was refunded. Tests that build slots as `HH:MM` masked it.

---

## Flow 6: Post-Visit Auto-Completion & Automated Patient Follow-ups

```text
Nightly Scheduler (00:30 IST)
       │
       ▼
[app/services/scheduler.py: auto_complete_appointments]
       │
       ├─► 1. Scans appointments for yesterday:
       │      SELECT id FROM appointments WHERE appointment_date < TODAY
       │      AND status = 'confirmed'
       ├─► 2. Bulk updates:
       │      UPDATE appointments SET status='completed', completed_at=NOW()
       │
       ▼ Daily Scheduler (10:00 IST)
[app/services/scheduler.py: send_followups]
       │
       ├─► 3. Scans completed visits:
       │      SELECT a.*, p.name, p.language FROM appointments a
       │      JOIN patients p ON a.patient_id = p.id
       │      WHERE a.status = 'completed' AND a.followup_sent = FALSE
       │      AND a.appointment_date BETWEEN (TODAY - 3 days) AND (TODAY - 1 day)
       ├─► 4. Checks Subscription Limits: automated_outbound_allowed(clinic_id)
       ├─► 5. Checks Patient Opt-Out: p.marketing_opt_out must be FALSE
       ├─► 6. Resolves Clinic Follow-up Template:
       │      Uses pre-approved WhatsApp Utility Template: post_appointment_followup
       ├─► 7. Sends Follow-up message asking how patient is feeling
       └─► 8. UPDATE appointments SET followup_sent=TRUE, followup_sent_at=NOW()
```

---

## Flow 7: Diagnostic Lab Report Ingestion, OCR & WhatsApp Delivery

```text
[connectors/mocdoc/worker.py or app/integrations/callmedex]
       │
       ├─► 1. Headless Chromium logs into hospital LIMS (MocDoc) using encrypted credentials
       ├─► 2. Scrapes report table, detects newly signed reports
       ├─► 3. Downloads PDF bytes into memory
       │
       ▼ POST /internal/integrations/callmedex/process-report
[app/services/lab_reports.py: LabReportService.upload_and_send_report]
       │
       ├─► 4. Storage Upload: Uploads PDF bytes to Supabase Storage ('lab-reports' bucket)
       │      storage_path = "{clinic_id}/{phone}/{uuid}_{filename}.pdf"
       ├─► 5. Text Extraction & OCR:
       │      PyMuPDF / pdfplumber extracts text; fallback to tesseract OCR if scanned
       ├─► 6. AI Clinical Summary:
       │      ReportSummarizer calls LLM with strict grounding prompt to generate 3-bullet layman summary
       ├─► 7. Patient Matching (patient_match.py):
       │      Matches extracted phone/name to patients table -> Assigns match_source ('registered_patient' / 'moc_doc_only')
       ├─► 8. Provider Routing Check (report_routing.py):
       │      If report belongs to corporate/TPA panel, redirects delivery to panel desk number
       ├─► 9. WhatsApp Dispatch:
       │      • If within 24h conversation window: Sends document message with caption & summary
       │      • If outside 24h window: Sends pre-approved Meta Template lab_report_delivery with signed PDF URL
       ├─► 10. Ledger Record:
       │       INSERT INTO lab_reports (clinic_id, patient_phone, file_path, delivery_status='sent', ...)
       └─► 11. Meta Webhook Status Tracking:
               Webhook receives delivery receipts (delivered -> read), updating lab_reports.delivery_status
```

---

## Flow 8: Clinical Safety Firewall Interception

```text
Patient sends: "Which antibiotic should I take for fever?"
       │
       ▼
[app/services/conversation.py: handle_message]
       │
       ▼
[app/services/clinical_firewall.py: screen_message]
       │
       ├─► 1. Regex check against MEDICATION_NAMES (antibiotics, pain, fever, antacids, steroids)
       ├─► 2. Regex check against DOSAGE_PATTERNS ("mg", "tablets", "dose", "twice a day")
       ├─► 3. Diagnostic inquiry check ("what disease do I have", "diagnose my symptoms")
       ├─► 4. Result: firewall_blocked = True
       │
       ▼ (LLM IS NEVER CALLED — NMC Liability Protected)
[conversation.py]
       │
       ├─► 5. Sends static localized clinical safety notice:
       │      "I cannot recommend medications or provide clinical diagnoses.
       │       Please consult a qualified doctor for treatment advice."
       ├─► 6. Tenders alternative: "Would you like to book an appointment with our General Physician?"
       └─► 7. Halts processing; does not modify active booking state
```

---

## Flow 9: DPDP Data Deletion ("DELETE MY DATA") & NMC Compliance

```text
Patient sends: "DELETE MY DATA"
       │
       ▼
[conversation.py: _handle_message_locked]
       │
       ├─► 1. Matches data deletion keyword
       ├─► 2. State -> AWAITING_DATA_DELETION
       ├─► 3. Sends confirmation challenge:
       │      "Are you sure? This will delete your conversation history and anonymize your records.
       │       Type CONFIRM DELETE to proceed."
       │
       ▼ Patient replies "CONFIRM DELETE"
[app/services/data_retention.py: execute_patient_data_deletion]
       │
       ├─► 4. DPDP Deletion (Conversation Sessions):
       │      DELETE FROM conversations WHERE phone=... AND clinic_id=...
       │      DELETE FROM inbound_messages WHERE phone=... AND clinic_id=...
       │      DELETE FROM processed_messages WHERE phone=... AND clinic_id=...
       │
       ├─► 5. NMC Mandate Compliance (7-Year Clinical Record Preservation):
       │      Clinical records (appointments, prescriptions, lab_reports) CANNOT be deleted by law.
       │      Instead, Anonymize Identifiers:
       │      UPDATE patients SET name='[REDACTED]', address='[REDACTED]' WHERE phone=...
       │      UPDATE appointments SET patient_name='[REDACTED]' WHERE patient_phone=...
       │      UPDATE lab_reports SET patient_name='[REDACTED]' WHERE patient_phone=...
       │
       ├─► 6. Record Deletion Event in audit logs
       └─► 7. Sends final WhatsApp confirmation: "Your personal data has been erased and anonymized."
```

---

## Flow 10: Admin Authentication, Session Minting & Scoped API Access

```text
Admin accesses https://hospital.kriya.ai/admin-panel
       │
       ▼ POST /admin/login (Body: username, password)
[app/routers/admin.py: admin_login]
       │
       ├─► 1. Check IP Rate Limit: PersistentRateLimiter (Max 5 attempts / minute)
       ├─► 2. Look up clinic_admins WHERE username=... AND is_active=TRUE
       ├─► 3. Verify Password: bcrypt.checkpw(password, stored_hash)
       ├─► 4. Mint Session: 48-byte cryptographically secure random token (secrets.token_urlsafe)
       ├─► 5. Store Session Hash:
       │      INSERT INTO admin_sessions (token_hash=sha256(token), clinic_id=..., expires_at=NOW() + 12h)
       ├─► 6. Set Response Cookie:
       │      kriya_admin_session = <token> (HttpOnly, Secure, SameSite=Lax, Max-Age=12 hours)
       └─► 7. Return 200 OK with AdminUser profile and authorized permissions
       │
       ▼ Admin UI calls GET /admin/appointments?clinic_id=...
[app/routers/admin.py: get_current_admin]
       │
       ├─► 8. Reads cookie kriya_admin_session -> Hashes with SHA-256
       ├─► 9. SELECT * FROM admin_sessions WHERE token_hash=... AND revoked_at IS NULL AND expires_at > NOW()
       ├─► 10. Builds AdminUser(username, role, clinic_id, permissions, branch_id)
       ├─► 11. enforce_clinic_access(user, requested_clinic_id):
       │       • If user.role == 'super_admin' -> Permitted
       │       • If user.clinic_id == requested_clinic_id -> Permitted
       │       • Else -> HTTP 403 Forbidden
       ├─► 12. enforce_branch_scope: If user has branch_id assigned, limits query to that branch
       └─► 13. Executes scoped query and returns appointment data
```
