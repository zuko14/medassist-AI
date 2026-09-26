# Kriya AI — Client Onboarding Standard Operating Procedure (SOP)
**Document Version:** 4.0 (All Plans — every Kriya-side step verified against the code on 2026-09-26)  
**Provider:** Zuko Labs (Meta Tech Provider — Business ID: `1602916427428175`)  
**Platform:** Kriya AI Multi-Tenant WhatsApp Healthcare Automation (App ID: `946290901317238`)  
**Plans Covered:** soloclinic · diagstream · diagbooking · essential · polyclinic · enterprise · derma · eye · dental · ivf · multispecialty · womenchild

---

## How to Use This Document (the order, start to finish)

Do the parts in this order for every new client. Tick each box; do not skip ahead — later steps need values from earlier ones.

| # | Part | Who | Output you carry forward |
|---|---|---|---|
| 0 | **Platform Prerequisites** (once per release) | You | Migrations applied |
| 1 | **Plan Reference Matrix** — pick the plan | You + client | Plan slug |
| 2 | **Part A** — client's Meta steps 1–6 | Client | WABA shared with Zuko Labs |
| 3 | **Part B** — steps 7–9 | You | Token, WABA ID, Phone Number ID, display number |
| 4 | **Part C** — register the plan's templates | You | Templates `APPROVED` |
| 5 | **Part D** — create the clinic in the Platform Panel, then the post-create settings | You | Clinic UUID, admin username + password |
| 6 | **Part E** — configure the clinic's admin panel for its plan | You / clinic admin | Doctors, prices, payments live |
| 7 | **Part F** — live end-to-end verification | You | Go-live sign-off |

Keep a per-client sheet with: plan · WABA ID · Phone Number ID · display number · clinic UUID · admin username (password handed to the client, not stored) · template names used · Razorpay webhook set (yes/no).

---

## Executive Summary & Architecture
In the Meta Cloud API ecosystem, hospitals maintain **100% legal ownership of their WhatsApp Business Account (WABA) and phone number** to guarantee instant Display Name approval (e.g., `"Accumax Diagnostics"`) without third-party naming rejections. **Zuko Labs** acts as the authorized **Technology Provider**, managing AI conversational flows, slot scheduling, payments, and lab report delivery behind the scenes.

```
┌─────────────────────────────────────────────────────────┐
│                 CLIENT (Hospital / Clinic)              │
│  1. Owns Meta Business Portfolio & WhatsApp Number      │
│  2. Fills Address / Tax Info & Links Payment Card       │
│  3. Verifies Phone Number with OTP                      │
│  4. Shares WABA Asset with Zuko Labs Partner ID         │
└────────────────────────────┬────────────────────────────┘
                             │ (Meta Partner Sharing)
                             ▼
┌─────────────────────────────────────────────────────────┐
│                 TECH PROVIDER (Zuko Labs)               │
│  5. Assigns WABA to ADMIN System User (Full Control)    │
│  6. Generates Token with business_management scope      │
│  7. Calls Cloud API /register to activate certificates  │
│  8. Subscribes Global Webhook (messages)                │
│  9. Runs whatsapp_doctor to verify health status        │
│ 10. Registers Tenant in Kriya AI Platform Panel         │
│ 11. Seeds Starter Data (treatments/departments/doctors) │
└─────────────────────────────────────────────────────────┘
```

---

## Platform Prerequisites (check once per release, before any onboarding)

These database migrations must be applied on production before the features below are used. Each is additive and safe to run while the current build is live; apply the migration **before** deploying the code that uses it.

| Migration | Adds | Needed for |
|---|---|---|
| `088_client_data_and_support.sql` | Imported patient records, import batches, support messages, invoice storage add-on | Admin → Data & Support (import/export, messages to Kriya), owner storage limits |
| `089_dental_treatment_plans.sql` | Dental treatment plans, sitting columns on appointments, doctor WhatsApp number, typical sittings, doctor digests, monthly message quotas, invoice messaging add-on | **Dental plan** Treatment Plans page, dental WhatsApp messages, owner Dental Messaging module |
| `090_dental_plan_branch.sql` | Branch on dental treatment plans | Multi-branch dental clinics (branch-pinned front desk) |
| `091_quota_functions_revoke_api_roles.sql` | Removes public-API (`anon` / `authenticated`) access to the two quota functions from 089 | Security — clears Supabase linter warnings 0028 / 0029. Run right after 089. |

Verify on production: `SELECT name FROM schema_migrations WHERE name LIKE '08%' OR name LIKE '09%' ORDER BY name;`

---

## Plan Reference Matrix

Before onboarding, identify which plan matches the client's facility. Each plan determines which WhatsApp menu items the patient sees, which templates are required, and which admin panel URL the clinic admin uses.

| Plan Slug | Dropdown label in Create Clinic | Target Facility | Admin Panel URL | Included messages / month* |
|---|---|---|---|---|
| `soloclinic` | SoloClinic — single doctor practice | Single doctor / small clinic | `/admin-panel` | 500 |
| `essential` | Essential — small clinic, core booking | Clinic with several departments | `/admin-panel` | 2,500 |
| `diagstream` | DiagStream — diagnostic / lab center | Lab with report delivery (connector) + test booking | `/admin-panel` | 1,000 |
| `diagbooking` | Diagnostic Test Booking — lab-test booking & payments only | Lab-test booking only, no reports | `/admin-panel` | 1,000 |
| `polyclinic` | Polyclinic — multi-department hospital | Multi-branch hospital + diagnostics | `/admin-panel` | 5,000 |
| `enterprise` | Enterprise — large multi-branch hospital | Everything (wildcard) | `/admin-panel` | Unlimited |
| `derma` | Dermatology & Hair | Skin, hair & cosmetology | `/derma-panel` | 2,500 |
| `eye` | Eye Hospital | Ophthalmology | `/eye-panel` | 2,500 |
| `dental` | Dental Clinic | Dental (with **Treatment Plans** for multi-sitting care) | `/dental-panel` | 2,500 |
| `ivf` | IVF & Fertility Centre | Fertility + own hormone/semen tests | `/ivf-panel` | 2,500 |
| `multispecialty` | Multi-Specialty Hospital | General hospital + treatments catalogue | `/hospital-panel` | 5,000 |
| `womenchild` | Women & Child Hospital | Child Care + Women Care + Fertility Care | `/women-child-panel` | 5,000 |

\* Defaults from the `plan_tiers` table; the owner can change them in Platform → Plan Tiers. Dental sitting messages are counted separately (Platform → Dental Messaging).

**Feature matrix (from `app/services/tenant.py → PLAN_FEATURES`)**

| Feature | solo | essential | diagstream | diagbooking | poly | enterprise | derma | eye | dental | ivf | multi | women-child |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Doctor booking + reminders | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lab reports (manual upload) | ❌ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| Report connector (automatic reports) | ❌ | ❌ | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| Lab test booking | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |
| Treatments catalogue | ❌ | ❌ | ❌ | ❌ | ❌ | override | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Multi-department | ❌ | ✅ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| Multi-branch | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Razorpay payments | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Insights / analytics, feedback | ❌ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Dental Treatment Plans | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ |

**Patient WhatsApp main menu — how it is built (`conversation._send_main_menu`)**

The menu is assembled by rules, not a fixed list per plan:
1. **Treatments rows first** — only when the clinic has a treatments catalogue AND at least one treatment is *shown to patients*: `✨ Our Treatments` + `🔍 Find by Concern`. If a published *Start here* (first-visit) treatment exists they become `🩺 Book Consultation` · `🔍 Not sure? Tell us` · `✨ What We Treat`.
2. **Diagnostics-only** (lab test booking and **no active doctors**): the service types filed in the catalogue (e.g. Health Packages, Radiology) as rows, or a single `Book Lab Test`.
3. Otherwise `Book Appointment`, then `Our Services` (**dropped** only on derma/eye/dental/ivf once treatments are published), then `Our Doctors`, then `🧪 Book Lab Test` if the plan has lab test booking.
4. Always `Emergency` and `Talk to Staff`, then `❓ How to use` if there is room (10 rows max).

So a brand-new dental clinic with no published treatments shows the general menu (Book · Our Services · Our Doctors · Emergency · Talk to Staff · How to use) until the first treatment is shown to patients.

> [!NOTE]
> **Specialty plans** (`derma`, `eye`, `dental`, `ivf`) drop the "Our Services" departments row (once treatments are published) because one department is not a menu. **Multispecialty** keeps it. The `enterprise` plan is a wildcard (`*`), but the treatments catalogue must be explicitly enabled via a feature override, and Dental Treatment Plans are for the `dental` plan only.

---

## Part A: Client-Side Steps (Hospital Admin — 5 Minutes)

### Step 1: Access Meta Business Portfolio
1. The client opens **[business.facebook.com/settings](https://business.facebook.com/settings)**.
2. Ensure their Business Portfolio is selected (e.g., *Accumax Diagnostics*).

### Step 2: Complete Business Information & Tax Details (Crucial for INR Billing)
1. Go to **Business Portfolio Info** ([business.facebook.com/settings/info](https://business.facebook.com/settings/info)).
2. Ensure **Legal Business Name**, **Physical Address** (Street, City, State, PIN Code), and **GSTIN / PAN** are entered.
   > [!IMPORTANT]
   > Meta suspends new WABAs in Inactive state (`141008`) if the physical address and tax info are missing under Indian invoicing regulations.

### Step 3: Add WhatsApp Phone Number & Display Name
1. In the left sidebar under **Accounts**, click **WhatsApp accounts**.
2. Click **Add** (or select existing WABA) $\rightarrow$ Open **WhatsApp Manager**.
3. In WhatsApp Manager, click the **Phone numbers (📞)** tab on the left $\rightarrow$ Click **Add phone number**.
4. Enter:
   - **Display Name:** Clean hospital/clinic name (e.g., `Accumax Diagnostics`).
   - **Category:** `Medical & Health` or `Hospital/Clinic`.
   - **Phone Number:** Mobile or landline number with country code `+91`.

### Step 4: Complete Phone Number OTP Verification
1. Click **Next** $\rightarrow$ Choose **Text message (SMS)** or **Voice call**.
2. Client receives a 6-digit OTP on the registered phone $\rightarrow$ Enters it on the screen.
3. Status changes to **Connected** with a Green quality rating.

### Step 5: Add Payment Method to WABA
1. In WhatsApp Manager, click **Billing** ([business.facebook.com/wa/manage/billing/](https://business.facebook.com/wa/manage/billing/)).
2. Under **Payment methods for WhatsApp Business Account**, attach a valid Credit/Debit Card and set as **Primary**.
   > [!NOTE]
   > Adding a card under general "Meta Ads" does NOT automatically link it to WhatsApp. It must be linked under WhatsApp Business Account billing.

### Step 6: Share Asset with Zuko Labs (Tech Provider)
1. In client Business Settings left menu, click **Users** $\rightarrow$ **Partners**.
2. Click the blue **+ Add** button $\rightarrow$ Select **"Give a partner access to your assets"**.
3. In the Partner Business ID field, enter Zuko Labs ID:
   ```
   1602916427428175
   ```
4. On the asset assignment screen:
   - Left column: Click **WhatsApp accounts**.
   - Middle column: Select the hospital's WhatsApp account (e.g., `Accumax Diagnostics`).
   - Right column: Toggle **Full control (Manage WhatsApp account)** to **ON**.
   - Click **Save Changes**.

---

## Part B: Tech Provider Steps (Zuko Labs / You — 3 Minutes)

### Step 7: Assign Shared Asset to ADMIN System User
1. Open Zuko Labs Business Settings: **[business.facebook.com/settings?business_id=1602916427428175](https://business.facebook.com/settings/system-users?business_id=1602916427428175)**.
2. In the left sidebar, click **Users** $\rightarrow$ **System users**.
3. Select your **Admin System User** (e.g., `chaitanya kumar` or `MedAssist Admin`).
   > [!WARNING]
   > The System User **MUST have the Admin role**. An "Employee" system user is blocked by Meta from managing partner-shared WhatsApp assets.
4. Click **Assign assets**:
   - Left column: **WhatsApp accounts**.
   - Middle column: Check the client's account (e.g., `Accumax Diagnostics`).
   - Right column: Toggle **Full control (Manage WhatsApp account)** $\rightarrow$ **ON**.
   - Click **Save Changes**.

### Step 8: Generate Token with Required Scopes
1. On the Admin System User, click **"Generate New Token"**.
   > [!IMPORTANT]
   > Tokens in Meta are immutable permission snapshots. You **MUST regenerate the token AFTER assigning the new WABA asset**.
2. Select App: **`KriyaAI`** (`946290901317238`).
3. Token Expiration: **Never**.
4. Check all **3 required permission scopes**:
   - ✅ **`business_management`** *(Required for partner-shared cross-business assets)*
   - ✅ **`whatsapp_business_management`** *(Required for templates & phone settings)*
   - ✅ **`whatsapp_business_messaging`** *(Required for sending messages & uploading media)*
5. Copy the generated permanent token.

### Step 9: Extract the 3 Core Identifiers
In Zuko Labs Business Settings $\rightarrow$ **Accounts** $\rightarrow$ **WhatsApp accounts** $\rightarrow$ Client's Account:
1. **WABA ID:** Displayed at the top (e.g., `1702889104159864`).
2. Click **Phone numbers** tab:
   - **Phone Number ID:** Copy the 15-16 digit ID (e.g., `1296654790197336`).
   - **Display Phone Number:** Copy E.164 number (e.g., `+919281235959`).

---

## Part C: WhatsApp Message Template Setup (Plan-Specific)

Templates are required for any proactive outbound message sent outside the 24-hour customer-service window. The templates required depend on the client's plan. Use the matrix below to determine which templates to register.

> [!TIP]
> Always submit templates in **`en_US`** or **`en`** (English). Meta's automated classifier evaluates English utility templates faster.

### Template Requirement Matrix

| Template Name | Category | Plans That Need It | Variables | Sent by |
|---|---|---|---|---|
| `lab_report_ready_v1` | UTILITY (DOCUMENT header) | diagstream, essential, polyclinic, enterprise, multispecialty, womenchild | 2 | Report delivery (connector / manual upload) outside the 24h window |
| `lab_report_summary_v1` | UTILITY (DOCUMENT header) | diagstream, essential, polyclinic, enterprise, multispecialty, womenchild | 3 | Report delivery with AI summary |
| `admin_alert_v1` | UTILITY | Plans with a report connector: diagstream, polyclinic, enterprise, multispecialty, womenchild | 1 | Connector failure alerts to the clinic admin's phone |
| `appointment_reminder_24h` | UTILITY | soloclinic, essential, polyclinic, enterprise, derma, eye, dental, ivf, multispecialty, womenchild | 2 | 09:00 IST, day before |
| `appointment_reminder_2h` | UTILITY | same as above | **2** | Hourly, 2 hours before |
| `appointment_confirmation` | UTILITY | same as above | 5 | Dental sitting confirmation fallback (see Step 10H) |
| `appointment_cancelled_doctor_leave` | UTILITY | same as above | 2 | 08:00 IST when a doctor's leave cancels bookings |
| `post_appointment_followup` | UTILITY | All plans with `reminders` feature | 2 | 10:00 IST, N days after the visit (Patient Follow-ups switch) |
| `followup_custom_message_v1` | UTILITY | All plans with `reminders` feature | 2 | Same, when the admin writes their own follow-up wording |
| `patient_health_checkin` | UTILITY + 2 quick replies | All plans with `reminders` feature (**opt-in per clinic**) | 2 | 10:30 IST, day 3 and day 7 after the visit |
| `dental_sitting_confirmation` | UTILITY | **dental only** | 8 | When the front desk books a sitting |
| `dental_sitting_reminder` | UTILITY | **dental only** | 8 | 08:30 IST, day before a sitting |
| `dental_doctor_sitting` | UTILITY | **dental only** | 7 | To the dentist when a sitting is assigned (on booking / button) |
| `dental_doctor_schedule` | UTILITY | **dental only** | 4 | 19:00 IST (retry 20:30) — each dentist's schedule for tomorrow |
| `dental_sitting_review` | UTILITY + 3 quick replies | **dental only** | 3 | ~1 hour after a sitting is marked done (09:00–21:00 IST) |
| `opt_out_confirmation` | UTILITY | Optional (all plans) | — | **Not sent by the code today** — STOP/deletion replies go out as normal chat messages inside the 24h window. Register only if the client wants them on file. |
| `data_deletion_confirmation` | UTILITY | Optional (all plans) | — | Same as above |

> [!IMPORTANT]
> **`diagstream`** and **`diagbooking`** do NOT need appointment-related templates (no doctor booking). **`diagbooking`** does NOT need lab report templates (no report connector). Specialty plans (`derma`, `eye`, `dental`, `ivf`) do NOT need lab report templates (no report connector). **`multispecialty`** and **`womenchild`** need ALL templates because they have both booking and lab report features. The five `dental_*` templates are for the **`dental`** plan only.

> [!CAUTION]
> **The variable count must match exactly.** The code fills a fixed number of variables per template (column "Variables"). A template approved with a different number fails every send with Meta error **`132000` (number of parameters does not match)**. Copy bodies from this document, not from memory.

---

### Step 10A: Lab Report Templates (diagstream / essential / polyclinic / enterprise / multispecialty / womenchild)

#### Template 1: Standard 2-Variable Report (`lab_report_ready_v1`)
* **Template Name:** `lab_report_ready_v1`
* **Category:** `UTILITY`
* **Language:** `en_US`
* **Header Format:** `DOCUMENT` (PDF file sample)
* **Body Text:**
  ```text
  Dear {{1}}, your medical lab report for {{2}} is ready and attached above. Please consult your physician for interpretation.
  ```
* **Variables:**
  - `{{1}}` $\rightarrow$ Patient Full Name (e.g., `Mrs. P. Kalyani`)
  - `{{2}}` $\rightarrow$ Lab Test Name (e.g., `Complete Blood Picture / Lipid Profile`)

#### Template 2: AI Summary 3-Variable Report (`lab_report_summary_v1`)
* **Template Name:** `lab_report_summary_v1`
* **Category:** `UTILITY`
* **Language:** `en_US`
* **Header Format:** `DOCUMENT` (PDF file sample)
* **Body Text:**
  ```text
  Dear {{1}}, your medical lab report for {{2}} is ready and attached above.

  Summary: {{3}}

  Please consult your physician for interpretation.
  ```
* **Variables:**
  - `{{1}}` $\rightarrow$ Patient Full Name
  - `{{2}}` $\rightarrow$ Lab Test Name
  - `{{3}}` $\rightarrow$ Clean flattened AI Summary text

---

### Step 10B: Appointment Reminder Templates (All booking plans)
**Required for:** soloclinic, essential, polyclinic, enterprise, derma, eye, dental, ivf, multispecialty, womenchild  
**NOT required for:** diagstream, diagbooking (no doctor appointments)

#### Template 3: 24-Hour Reminder (`appointment_reminder_24h`)
* **Template Name:** `appointment_reminder_24h`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Reminder: Your appointment with {{1}} is tomorrow at {{2}}. Please arrive 10 mins early. Reply CANCEL if you can't make it.
  ```
* **Variables:**
  - `{{1}}` → Doctor Name (e.g., `Dr. Ramesh Sharma`)
  - `{{2}}` → Slot Time (e.g., `10:30 AM`)

#### Template 4: 2-Hour Reminder (`appointment_reminder_2h`)
* **Template Name:** `appointment_reminder_2h`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Your appointment at {{1}} is in 2 hours with {{2}}. Reply CANCEL to cancel.
  ```
* **Variables:**
  - `{{1}}` → Location — the branch name for multi-branch clinics, otherwise the clinic name (filled by the code)
  - `{{2}}` → Doctor Name (e.g., `Dr. Ramesh Sharma`)

> [!WARNING]
> **CORRECTED 2026-09-26.** An earlier version of this SOP registered this template with ONE variable and the clinic name typed in. The code has always sent TWO (location, doctor), so every 2-hour reminder on such a WABA failed with error `132000`. If a live client's `appointment_reminder_2h` was created from the old text, create the 2-variable version (Meta does not allow editing the variable count of an approved template: delete it, wait for the name to free up, or register it under a new name and tell engineering).

---

### Step 10C: Appointment Confirmation & Cancellation Templates (All booking plans)

#### Template 5: Outbound Booking Confirmation (`appointment_confirmation`)
* **Template Name:** `appointment_confirmation`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Your appointment with {{1}} ({{2}}) is confirmed for {{3}} at {{4}}. Reply CANCEL to cancel. - {{5}}
  ```
* **Variables:**
  - `{{1}}` → Doctor Name (e.g., `Dr. Ramesh Sharma`)
  - `{{2}}` → Department (e.g., `Cardiology`) — for specialty plans, this shows the treatment name
  - `{{3}}` → Date (e.g., `05 Sep 2026`)
  - `{{4}}` → Slot Time (e.g., `10:30 AM`)
  - `{{5}}` → Hospital / Clinic Name

#### Template 6: Doctor Cancellation / Leave (`appointment_cancelled_doctor_leave`)
* **Template Name:** `appointment_cancelled_doctor_leave`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  We're sorry, your appointment with {{1}} on {{2}} has been cancelled as the doctor is unavailable. Reply REBOOK to reschedule. We apologise for the inconvenience.
  ```
* **Variables:**
  - `{{1}}` → Doctor Name
  - `{{2}}` → Date & Time (e.g., `Tomorrow at 10:30 AM`)

---

### Step 10D: Post-Visit Follow-up Templates (All plans with reminders)

#### Template 7: Built-in Default Follow-up (`post_appointment_followup`)
* **Template Name:** `post_appointment_followup`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Dear patient {{1}}, we hope you are feeling better after your recent visit to <CLINIC_NAME>. Your health and recovery are important to us. If you need a follow-up appointment, please reply YES or call us at {{2}} to schedule. Wishing you good health!
  ```
* **Variables:**
  - `{{1}}` → Patient Full Name
  - `{{2}}` → Hospital Phone Number

#### Template 8: Custom Message Follow-up (`followup_custom_message_v1`)
* **Template Name:** `followup_custom_message_v1`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Dear patient {{1}}, this is a follow-up message from <CLINIC_NAME> regarding your recent visit.

  {{2}}

  If you have any concerns, please do not hesitate to reach out. We wish you good health and a speedy recovery.
  ```
* **Variables:**
  - `{{1}}` → Patient First Name
  - `{{2}}` → Custom message text entered by admin

> [!IMPORTANT]
> Replace `<CLINIC_NAME>` with the actual clinic name before submitting. Meta rejects template modifications after approval.

#### Clinic Config Keys (set via Admin Panel or manually in DB):
| Config Key | Purpose | Example Value |
|---|---|---|
| `followup_enabled` | Enable/disable follow-ups | `true` |
| `followup_days` | Days after visit to send (1–30) | `1` |
| `followup_template_name` | Built-in template name | `post_appointment_followup` |
| `followup_message_template_name` | Custom-message template name | `followup_custom_message_v1` |
| `followup_message` | Admin's custom message text | *(set via Admin Panel textarea)* |
| `health_checkins_enabled` | Day 3 / day 7 health check-ins (**default OFF**) | `true` *(Admin Panel switch)* |
| `health_checkin_template_name` | Override the check-in template name | `patient_health_checkin` |

> [!NOTE]
> The built-in follow-up's `{{2}}` "call us" number is the clinic's own number: **front-desk phone** (Hospital Profile → staff phone) → clinic phone → clinic WhatsApp number. Fill in the front-desk phone during setup, otherwise patients are told to call the WhatsApp number. *(Before 2026-09-26 this was one platform-wide number for every clinic.)*

#### Template 8b: Health Check-in, day 3 and day 7 (`patient_health_checkin`)
* **Template Name:** `patient_health_checkin`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Hello {{1}}, it has been a few days since your visit with {{2}}. How are you feeling now? Please tap an option below.
  ```
* **Buttons (Quick Reply, in this order):** `Feeling fine` · `Still have symptoms`
* **Variables:**
  - `{{1}}` → Patient first name
  - `{{2}}` → Doctor (e.g., `Dr. Rao`)
* **Behaviour:** off until the clinic turns on **Hospital Profile → Patient Follow-ups → Health check-ins on day 3 and day 7**. Sent 10:30 IST for consultations seen 3 and 7 days earlier (lab bookings and dental plan sittings are excluded), skipped for patients who replied STOP, retried for 2 more days if Meta refuses. "Still have symptoms" replies with the clinic's number; "Feeling fine" thanks the patient.

---

### Step 10E: DPDP Act 2023 & Compliance Templates (ALL Plans)

#### Template 9: Opt-Out Confirmation (`opt_out_confirmation`)
* **Template Name:** `opt_out_confirmation`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  You've been unsubscribed from {{1}} WhatsApp reminders. Message us anytime to re-subscribe. For urgent help call {{2}}.
  ```

#### Template 10: Data Deletion Receipt (`data_deletion_confirmation`)
* **Template Name:** `data_deletion_confirmation`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Your data has been deleted from {{1}} systems as requested. Reference: {{2}}. For records, contact {{3}}.
  ```

---

### Step 10E2: Connector Admin Alert Template (plans with a report connector)

#### Template 11: Admin Alert (`admin_alert_v1`)
* **Template Name:** `admin_alert_v1` (any name works — it is configured, not hard-coded)
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Kriya alert for your clinic: {{1}}. Please check the admin panel.
  ```
* **Variables:** `{{1}}` → the alert text (flattened to one line by the code)
* **Configure:** set `admin_alert_template_name` = `admin_alert_v1` in the connector config, the clinic config, or the `ADMIN_ALERT_TEMPLATE_NAME` env var. Without it, connector failure alerts outside the 24h window are logged as `ADMIN_ALERT_UNDELIVERED` and never reach the admin.

---

### Step 10H: Dental Plan Templates (dental plan ONLY)

Dental care is a course of **sittings** (Admin → Treatment Plans). These five templates carry every dental message. They are submitted **from the owner panel with one click** (Platform → Dental Messaging → **Submit Templates**) using the clinic's own Meta token and WABA id; use **Template Status** to watch for `APPROVED`. The definitions below are exactly what that button sends (source of truth: `app/services/dental_plans.py → DENTAL_TEMPLATES`).

| # | Name | Recipient | Body | Variables |
|---|---|---|---|---|
| 12 | `dental_sitting_confirmation` | Patient | `Hello {{1}}, your {{2}} appointment at {{3}} is confirmed: sitting {{4}} of {{5}} with {{6}} on {{7}} at {{8}}. Please reply here or call the clinic if you need to change it.` | first name, treatment, clinic, sitting no., planned sittings, doctor, date, time |
| 13 | `dental_sitting_reminder` | Patient | `Reminder from {{1}}: {{2}}, your {{3}} sitting {{4}} of {{5}} with {{6}} is tomorrow, {{7}} at {{8}}. Please arrive 10 minutes early.` | clinic, first name, treatment, sitting no., planned, doctor, date, time |
| 14 | `dental_doctor_sitting` | Dentist | `Hello {{1}}, a dental sitting has been assigned to you: {{2}} for {{3}}, sitting {{4}} of {{5}}, on {{6}} at {{7}}. Please plan your chair time accordingly.` | doctor, patient, treatment, sitting no., planned, date, time |
| 15 | `dental_doctor_schedule` | Dentist | `Hello {{1}}, your dental sittings for {{2}} at {{3}}: {{4}}. Please contact the front desk for any changes.` | doctor, date, clinic, list of sittings (one line) |
| 16 | `dental_sitting_review` | Patient | `Thank you for visiting {{1}}, {{2}}. How was your {{3}} sitting today? Please tap an option below.` + Quick Replies **Excellent · Good · Needs improvement** | clinic, first name, treatment |

**What happens if a template is not approved yet**

| Message | Fallback |
|---|---|
| Sitting confirmation | Sent with the generic `appointment_confirmation` (register it — Step 10C) |
| Day-before reminder | The standard 09:00 `appointment_reminder_24h` goes out instead |
| Doctor notice / daily schedule | **Nothing** — dentists receive no WhatsApp until 14 and 15 are approved |
| Review request | Nothing |

> [!IMPORTANT]
> * Meta may re-categorise `dental_sitting_review` as **MARKETING** (feedback requests sometimes are). It still works; it just costs more. Check the category in Template Status.
> * **Patient consent:** a patient imported from old software or a walk-in is only messaged after the front desk ticks *"The patient agreed to receive appointment messages on WhatsApp"* on the plan (or the patient has messaged the clinic before). Otherwise only the dentist is messaged.
> * Every dental message counts against the clinic's **monthly limits** set in Platform → Dental Messaging (patient / doctor / review). The clinic is notified at 90 % and 100 %. At 100 % those messages pause until next month; patients still get the standard reminder.

---

### Step 10F: Plan-Specific Template Checklist

Use this checklist to confirm which templates to register per plan:

| # | Template Name | solo | diag-stream | diag-booking | essential | poly | enterprise | derma | eye | dental | ivf | multi-specialty / women-child |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `lab_report_ready_v1` | ❌ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| 2 | `lab_report_summary_v1` | ❌ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| 3 | `appointment_reminder_24h` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 4 | `appointment_reminder_2h` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 5 | `appointment_confirmation` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 6 | `appointment_cancelled_doctor_leave` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 7 | `post_appointment_followup` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 8 | `followup_custom_message_v1` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 8b | `patient_health_checkin` *(if the clinic will switch check-ins on)* | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 9 | `opt_out_confirmation` *(optional)* | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ |
| 10 | `data_deletion_confirmation` *(optional)* | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ |
| 11 | `admin_alert_v1` *(only with a report connector)* | ❌ | ✅ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| 12–16 | `dental_*` (5 templates, owner-panel button) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |

⚪ = optional: not sent by the code today.

---

### Step 10G: 1-Click Master Template Setup Script (Registers ALL Templates)
Run this single terminal script to upload sample media and register **every required template** on the client's WABA automatically in 15 seconds:

```bash
python -c "
import httpx

token = '<META_ADMIN_SYSTEM_USER_TOKEN>'
app_id = '946290901317238'
waba_id = '<CLIENT_WABA_ID>'
clinic_name = '<CLINIC_NAME>'

headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

# 1. Upload sample PDF for document headers
pdf_bytes = b'%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n'
r1 = httpx.post(f'https://graph.facebook.com/v22.0/{app_id}/uploads', headers={'Authorization': f'Bearer {token}'}, params={'file_length': len(pdf_bytes), 'file_type': 'application/pdf'}).json()
r2 = httpx.post(f'https://graph.facebook.com/v22.0/{r1[\"id\"]}', headers={'Authorization': f'OAuth {token}', 'file_offset': '0'}, content=pdf_bytes).json()
h = r2['h']

templates = [
    # 1. Lab Report Standard
    {
        'name': 'lab_report_ready_v1',
        'category': 'UTILITY',
        'language': 'en_US',
        'components': [
            {'type': 'HEADER', 'format': 'DOCUMENT', 'example': {'header_handle': [h]}},
            {'type': 'BODY', 'text': 'Dear {{1}}, your medical lab report for {{2}} is ready and attached above. Please consult your physician for interpretation.', 'example': {'body_text': [['Mrs. P. Kalyani', 'Lipid Profile']]}}
        ]
    },
    # 2. Lab Report with AI Summary
    {
        'name': 'lab_report_summary_v1',
        'category': 'UTILITY',
        'language': 'en_US',
        'components': [
            {'type': 'HEADER', 'format': 'DOCUMENT', 'example': {'header_handle': [h]}},
            {'type': 'BODY', 'text': 'Dear {{1}}, your medical lab report for {{2}} is ready and attached above.\n\nSummary: {{3}}\n\nPlease consult your physician for interpretation.', 'example': {'body_text': [['Mrs. P. Kalyani', 'Lipid Profile', 'All tested parameters are within normal reference ranges.']]}}
        ]
    },
    # 3. 24-Hour Reminder
    {
        'name': 'appointment_reminder_24h',
        'category': 'UTILITY',
        'language': 'en',
        'components': [
            {'type': 'BODY', 'text': 'Reminder: Your appointment with {{1}} is tomorrow at {{2}}. Please arrive 10 mins early. Reply CANCEL if you can\'t make it.', 'example': {'body_text': [['Dr. Ramesh Sharma', '10:30 AM']]}}
        ]
    },
    # 4. 2-Hour Reminder
    {
        'name': 'appointment_reminder_2h',
        'category': 'UTILITY',
        'language': 'en',
        'components': [
            {'type': 'BODY', 'text': 'Your appointment at {{1}} is in 2 hours with {{2}}. Reply CANCEL to cancel.', 'example': {'body_text': [[clinic_name, 'Dr. Ramesh Sharma']]}}
        ]
    },
    # 5. Outbound Booking Confirmation
    {
        'name': 'appointment_confirmation',
        'category': 'UTILITY',
        'language': 'en',
        'components': [
            {'type': 'BODY', 'text': 'Your appointment with {{1}} ({{2}}) is confirmed for {{3}} at {{4}}. Reply CANCEL to cancel. - {{5}}', 'example': {'body_text': [['Dr. Ramesh Sharma', 'Cardiology', '05 Sep 2026', '10:30 AM', clinic_name]]}}
        ]
    },
    # 6. Post-Visit Follow-up Default
    {
        'name': 'post_appointment_followup',
        'category': 'UTILITY',
        'language': 'en',
        'components': [
            {'type': 'BODY', 'text': f'Dear patient {{{{1}}}}, we hope you are feeling better after your recent visit to {clinic_name}. Your health and recovery are important to us. If you need a follow-up appointment, please reply YES or call us at {{{{2}}}} to schedule. Wishing you good health!', 'example': {'body_text': [['Ravi Kumar', '+919490386668']]}}
        ]
    },
    # 6b. Post-Visit Follow-up with the clinic's own wording
    {
        'name': 'followup_custom_message_v1',
        'category': 'UTILITY',
        'language': 'en',
        'components': [
            {'type': 'BODY', 'text': f'Dear patient {{{{1}}}}, this is a follow-up message from {clinic_name} regarding your recent visit.\n\n{{{{2}}}}\n\nIf you have any concerns, please do not hesitate to reach out. We wish you good health and a speedy recovery.', 'example': {'body_text': [['Ravi', 'Please continue the tablets for 5 more days and come back if the pain returns.']]}}
        ]
    },
    # 6c. Day 3 / day 7 Health Check-in (opt-in per clinic)
    {
        'name': 'patient_health_checkin',
        'category': 'UTILITY',
        'language': 'en',
        'components': [
            {'type': 'BODY', 'text': 'Hello {{1}}, it has been a few days since your visit with {{2}}. How are you feeling now? Please tap an option below.', 'example': {'body_text': [['Ravi', 'Dr. Rao']]}},
            {'type': 'BUTTONS', 'buttons': [{'type': 'QUICK_REPLY', 'text': 'Feeling fine'}, {'type': 'QUICK_REPLY', 'text': 'Still have symptoms'}]}
        ]
    },
    # 6d. Connector admin alert (plans with a report connector)
    {
        'name': 'admin_alert_v1',
        'category': 'UTILITY',
        'language': 'en',
        'components': [
            {'type': 'BODY', 'text': 'Kriya alert for your clinic: {{1}}. Please check the admin panel.', 'example': {'body_text': [['Report connector login failed at 10:05 AM']]}}
        ]
    },
    # 7. Doctor Cancellation / Emergency Leave
    {
        'name': 'appointment_cancelled_doctor_leave',
        'category': 'UTILITY',
        'language': 'en',
        'components': [
            {'type': 'BODY', 'text': 'We\'re sorry, your appointment with {{1}} on {{2}} has been cancelled as the doctor is unavailable. Reply REBOOK to reschedule. We apologise for the inconvenience.', 'example': {'body_text': [['Dr. Ramesh Sharma', 'Tomorrow at 10:30 AM']]}}
        ]
    },
    # 8. DPDP Opt-Out
    {
        'name': 'opt_out_confirmation',
        'category': 'UTILITY',
        'language': 'en',
        'components': [
            {'type': 'BODY', 'text': f'You\'ve been unsubscribed from {clinic_name} WhatsApp reminders. Message us anytime to re-subscribe. For urgent help call {{{{1}}}}.', 'example': {'body_text': [['+919490386668']]}}
        ]
    },
    # 9. DPDP Data Deletion
    {
        'name': 'data_deletion_confirmation',
        'category': 'UTILITY',
        'language': 'en',
        'components': [
            {'type': 'BODY', 'text': f'Your data has been deleted from {clinic_name} systems as requested. Reference: {{{{1}}}}. For records, contact {{{{2}}}}.', 'example': {'body_text': [['DEL-20260916-001', '+919490386668']]}}
        ]
    }
]

for t in templates:
    res = httpx.post(f'https://graph.facebook.com/v22.0/{waba_id}/message_templates', headers=headers, json=t)
    status = 'CREATED' if res.status_code in [200, 201] else f'FAILED ({res.status_code}): {res.text}'
    print(f'Template [{t[\"name\"]}]: {status}')

print('\nAll Core Templates Submitted for Meta Review Successfully!')
"
```

> [!WARNING]
> **For diagstream / diagbooking plans:** You may skip templates 3–7 (appointment-related). For diagbooking, also skip templates 1–2 (lab report delivery). The script registers all templates — Meta silently ignores templates the clinic never triggers, so registering extras is safe but unnecessary.
>
> **Dental plan:** this script does NOT include the five `dental_*` templates. Submit those from Platform → Dental Messaging → **Submit Templates** (Step 10H) — same token and WABA, one click, and the definitions can never drift from the code.
>
> **After running:** set `lab_report_template_name` (platform panel) and `admin_alert_template_name` (connector/clinic config) to the names you registered.

---

### Step 11: Cloud API Activation (`/register` — Automated in Kriya AI)
> **Automatic Activation:** When you register the clinic in the Kriya AI Platform Panel (Step 13 below), Kriya AI automatically calls Meta's `/register` API in the background using the provided token and phone number ID. The status flips to **`Connected`** automatically.

**Manual Command Fallback (if needed before platform creation):**
```bash
curl -X POST "https://graph.facebook.com/v22.0/<PHONE_NUMBER_ID>/register" \
  -H "Authorization: Bearer <META_SYSTEM_USER_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"messaging_product": "whatsapp", "pin": "123456"}'
```

### Step 12: Verify Global Webhook Subscription
1. Open Meta Developer Dashboard: **[developers.facebook.com/apps/946290901317238](https://developers.facebook.com/apps/946290901317238)**.
2. Go to **Use Cases** $\rightarrow$ **Connect on WhatsApp** $\rightarrow$ **Step 2. Production setup** (or **WhatsApp $\rightarrow$ Configuration**).
3. Confirm that **`messages`** is toggled to **Subscribed (Blue ON)**.

---

## Part D: Register Tenant in Kriya AI Platform Panel

### Step 13: Create the Clinic / Hospital
1. Open Kriya AI Platform Panel: `https://medassist-ai-docker.onrender.com/platform-panel` and sign in with the owner credentials (`OWNER_USERNAME` / `OWNER_PASSWORD`).
2. Click **Create Hospital / Clinic** and fill in (these are the exact fields of the form):

| Field | Required | Value / example | Stored as |
|---|---|---|---|
| **Hospital / Clinic Name** | ✅ | `Accumax Diagnostics` | `clinics.name` |
| **WhatsApp Number (E.164)** | ✅ | `+919281235959` — must start with `+` and match the registered number | `clinics.whatsapp_number` |
| **Plan** | ✅ | from the dropdown (Plan Reference Matrix) | `clinics.plan` |
| **Daily Report Limit** | shown for report plans | 50 / 100 / 200 / 300 / 500 / Unlimited | `clinics.daily_report_limit` |
| **Meta Phone Number ID** | ✅ | `1296654790197336` (Step 9) | `phone_number_id` + config |
| **Meta Permanent Access Token** | ✅ | `EAAN...` (Step 8) | config `meta_access_token` |
| **Meta WABA ID** | strongly recommended | `1702889104159864` (Step 9) — **required** for dental template submission and template status checks | config `meta_waba_id` |
| Clinic Display Name (for AI replies) | optional | defaults to the name | config |
| Doctor / Team Name | optional | `Medical Team` | config |
| Language · Timezone | optional | `en` · `Asia/Kolkata` | config |
| **Front-Desk Phone** | recommended | reception number, distinct from WhatsApp | config `phone` — used as the "call us" number |
| **Address** · Google Maps link · Emergency number | recommended | sent to patients who ask | config |
| Razorpay Key ID / Key Secret / Webhook Secret | optional here | can instead be set later by the clinic admin (Part E6) | config |

3. Click **Create Hospital / Clinic**. The result screen shows:
   - **Clinic ID (UUID)** — copy it into the client sheet (needed for the Razorpay webhook URL, SQL settings, the doctor CLI).
   - **Admin username and password** — generated automatically, **shown only once**. Copy both now and hand them to the clinic admin securely. If lost: Reset it with the owner API: `curl -X PUT https://medassist-ai-docker.onrender.com/platform/reset-admin-password -u '<OWNER_USERNAME>:<OWNER_PASSWORD>' -H 'Content-Type: application/json' -d '{"username": "<ADMIN_USERNAME>", "new_password": "<NEW_PASSWORD_8+_CHARS>"}'`

#### What the system does automatically on Create
| Action | Detail |
|---|---|
| Cloud API activation | Calls Meta `POST /{phone_number_id}/register` with PIN `123456`. If the phone later shows *Pending*, run Step 11's manual command. |
| Admin login | One `clinic_admin` account (username = name slug + random suffix, random 16-char password). |
| Starter treatments | derma / eye / dental / ivf: the specialty's starter list, seeded **hidden**. womenchild: Child Care, Women Care and Fertility Care lists, hidden. multispecialty: none (admin loads lists from the starter picker). |
| Nothing else | No doctors, prices, templates or payments are created — Part E does that. |

> [!CAUTION]
> Each clinic must have its **own** Meta Phone Number ID. Reusing another clinic's ID is refused (HTTP 409) — it is what routes incoming messages to the right clinic.

### Step 13b: Post-Create Settings Only Set by SQL (Supabase → SQL Editor)

These per-clinic keys have **no form field**. Set the ones that apply, replacing `<CLINIC_UUID>` and the template names with the ones you registered. Changes take effect within **30 seconds** (tenant cache).

```sql
-- Lab report template (plans with lab reports). Without it the global default
-- LAB_REPORT_TEMPLATE_NAME ('lab_report_delivery') is used, which this SOP does not register.
UPDATE clinics SET config = config || '{"lab_report_template_name": "lab_report_ready_v1"}'::jsonb
WHERE id = '<CLINIC_UUID>';

-- AI-summary report template (only if you registered lab_report_summary_v1)
UPDATE clinics SET config = config || '{"lab_report_summary_template_name": "lab_report_summary_v1"}'::jsonb
WHERE id = '<CLINIC_UUID>';

-- Connector failure alerts to the admin's phone (plans with a report connector)
UPDATE clinics SET config = config || '{"admin_alert_template_name": "admin_alert_v1"}'::jsonb
WHERE id = '<CLINIC_UUID>';

-- Only if a template was registered under a different name than this SOP uses:
-- followup_template_name, followup_message_template_name, health_checkin_template_name

-- Check what is set:
SELECT id, name, plan, config->>'lab_report_template_name' AS report_tpl,
       config->>'admin_alert_template_name' AS alert_tpl, config->>'meta_waba_id' AS waba
FROM clinics WHERE id = '<CLINIC_UUID>';
```

#### Plan-Specific Platform Panel Settings:

| Plan | Lab report template (Step 13b SQL) | Admin alert template (Step 13b SQL) | Auto-seeded on create |
|---|---|---|---|
| `soloclinic` | — | — | nothing |
| `essential` | `lab_report_ready_v1` | — | nothing |
| `diagstream` | `lab_report_ready_v1` | `admin_alert_v1` | nothing |
| `diagbooking` | — | — | nothing |
| `polyclinic` | `lab_report_ready_v1` | `admin_alert_v1` | nothing |
| `enterprise` | `lab_report_ready_v1` | `admin_alert_v1` | nothing |
| `derma` / `eye` / `dental` / `ivf` | — | — | specialty starter treatments (hidden) |
| `multispecialty` | `lab_report_ready_v1` | `admin_alert_v1` | nothing (starter picker in admin) |
| `womenchild` | `lab_report_ready_v1` | `admin_alert_v1` | Child / Women / Fertility lists (hidden) |

---

#### Owner settings after creating the clinic (all plans)

| Where (Platform Panel) | Set | Default if you skip it |
|---|---|---|
| **Client Data Storage** → Set Limit / Price | Imported patient record limit and the monthly storage add-on (₹) | 5,000 records, no add-on |
| **Subscriptions** | Subscription period, daily report limit | as created |
| **Finance → Billing Rate** | The clinic's monthly price | plan list price |
| **Client Messages** (bell in the top bar) | Nothing to set — clinic admins' messages arrive here, reply from the bell | — |

#### Extra owner settings for the **dental** plan

1. **Dental Messaging → Submit Templates** for the clinic, then **Template Status** until all five show `APPROVED` (Step 10H).
2. **Dental Messaging → Set Limits / Price:** monthly limits for patient sitting messages, doctor reminders and review requests (defaults 1,000 / 300 / 500; 0 switches a kind off) and the monthly messaging add-on (₹), which is billed on the next generated invoice.
3. The Dental Messaging table shows each month's usage and the estimated Meta cost (messages × the utility rate in Pricing).

---

## Part E: Post-Onboarding Configuration (Plan-Specific)

After the tenant is created in the Platform Panel, the **clinic admin** (or you during the demo) must configure the clinic through the admin panel. The admin panel URL depends on the plan.

### E1: General Plans (soloclinic / essential / polyclinic / enterprise)

**Admin Panel URL:** `https://medassist-ai-docker.onrender.com/admin-panel`

1. **Login** → open the plan's admin URL and sign in with the **admin username and password from Step 13**. Change the password at first login (sidebar → Change Password). Create front-desk accounts under **Staff Accounts** instead of sharing the admin login.
2. **Hospital Profile** → bot / hospital name, address, Google Maps link, emergency number, **front-desk phone**.
3. **Departments** → there is no separate Departments page: a department is created by typing it in the **Department** field when adding a doctor (it then appears in the patient's *Our Services* list). Multi-department plans: essential, polyclinic, enterprise, multispecialty, womenchild.
4. **Doctors** → add each doctor with department, fee, working days, morning/evening hours and slot duration.
5. **Payment Settings** → see **E6: Razorpay Setup** below.
6. **Holiday Calendar** → Set public holidays and clinic closures.
7. **Patient Follow-ups** (Hospital Profile) → the **ON/OFF switch** saves instantly; set the days after the visit and, optionally, your own wording (needs `followup_custom_message_v1`). Set the **front-desk phone** in Basic Details — it is the "call us" number in follow-ups.
8. **Health check-ins on day 3 and day 7** (same card) → OFF by default. Switch on only after `patient_health_checkin` is approved.
9. **Data & Support** → import the clinic's existing patient list from their old software (CSV; use *Check file* first), and show the admin where to export data and message Kriya.

**Additional for polyclinic / enterprise:**
8. **Branches** → Add branch locations with address and phone.
9. **Lab Tests** → Add lab tests with name, price, category, turnaround time.

---

### E2: Diagnostic Plans (diagstream / diagbooking)

**Admin Panel URL:** `https://medassist-ai-docker.onrender.com/admin-panel`

1. **Login** → admin username and password from Step 13 (change it at first login).
2. **Hospital Profile** → address, maps link, emergency number, front-desk phone.
3. **Branches** → Add branches (multi-branch supported).
4. **Lab Tests** → Add tests with name, price, category, turnaround time.
5. **Payment Settings** → see **E6: Razorpay Setup** below.
6. **Holiday Calendar** → Set closures.

**Additional for diagstream:**
7. **Lab Report Connector** → Configure the lab system integration for automated PDF delivery.

---

### E3: Specialty Plans (derma / eye / dental / ivf)

**Admin Panel URLs:**
| Plan | URL |
|---|---|
| `derma` | `https://medassist-ai-docker.onrender.com/derma-panel` |
| `eye` | `https://medassist-ai-docker.onrender.com/eye-panel` |
| `dental` | `https://medassist-ai-docker.onrender.com/dental-panel` |
| `ivf` | `https://medassist-ai-docker.onrender.com/ivf-panel` |

> [!NOTE]
> All specialty panel URLs serve the same `admin/index.html`. The panel detects the clinic's plan from `GET /admin/me` and shows/hides tabs accordingly. The URL is purely for branding in the browser bar.

#### Onboarding Steps:

1. **Login** → admin username and password from Step 13 (change it at first login).
2. **Hospital Profile** → address, maps link, emergency number, front-desk phone.
3. **Doctors** → Add doctors with name, qualification, slot duration, schedule.
4. **Treatments Tab** → This appears automatically for specialty plans:
   - **Starter treatments are already there** → seeded automatically when the clinic was created (10+ treatments for the specialty, all hidden). Only if the list is empty (seeding failed at creation) click the starter card to load them — loading is idempotent, it never duplicates.
   - **Review & Activate** → Each treatment is seeded as **hidden** (`is_active = false`). The admin must:
     - Review the name, description, and category
     - Set pricing (₹ per procedure or "Consultation" for free assessment)
     - Toggle **"Show to patients"** to make it visible on WhatsApp
   - **Add Custom Treatments** → Click "Add Treatment" to create custom treatments with AI-assisted description drafting.
   - **Link Doctors to Treatments** → Assign which doctors perform each treatment. If no doctors are linked, any available doctor can be booked.
5. **Payment Settings** → see **E6: Razorpay Setup** below.
6. **Holiday Calendar** → Set closures.
7. **Branches** → Add branch locations (specialty chains often have multiple centres).

8. **Patient Follow-ups / Health check-ins / Data & Support** → as in E1 (steps 7–9).

**Additional for ivf:**
9. **Lab Tests** → Add hormone/fertility tests (AMH, semen analysis, etc.) with pricing.

#### Additional for **dental** — Treatment Plans (multi-sitting courses)

Dental treatments run over several **sittings** (a root canal is typically 2–4). The **Treatment Plans** page appears for the dental plan only.

1. **Doctors** → for each dentist fill **Doctor's WhatsApp number (for sitting reminders)**. A dentist without it gets no WhatsApp.
2. **Treatments** → set **Typical sittings** on each multi-sitting treatment (e.g. Root Canal 3, Implant 4). It pre-fills new plans.
3. **Staff Accounts** → tick **Manage Dental Treatment Plans & Sittings** for each receptionist who books sittings (admins always can). A receptionist tied to one **branch** only sees that branch's plans and clinic-wide plans, and can only book that branch's dentists.
4. **Treatment Plans → Automation settings** (admin only): review request after each sitting (on by default), dentists' evening schedule (on by default), **Google review link** (sent to patients who tap *Excellent*).
5. **Show the front desk the daily flow:**
   - **New treatment plan** → find the patient (WhatsApp patients and imported patients are both searchable) → treatment → planned sittings → tooth numbers → estimate → tick **patient agreed to WhatsApp messages** if they said yes → optionally book the first sitting (doctor → date → a free time). Multi-branch clinics also pick the **Branch**.
   - A patient who booked on WhatsApp: their booking can be chosen as **sitting 1**.
   - After each sitting: **Done** → work notes + amount collected. The plan shows collected vs estimate and the balance, and completes itself after the last planned sitting.
   - **Book sitting** for the next visit (any free dentist — only genuinely free times are offered). Reschedule = **Cancel** the sitting, then book the same sitting number again.
   - **WhatsApp patient / WhatsApp doctor** buttons resend the details on demand.

#### Starter Treatment Categories by Plan:

| Plan | Starter Categories |
|---|---|
| `derma` | Acne & Scars, Pigmentation, Skin Conditions, Hair & Scalp, Anti-Aging, Body Treatments, Laser Treatments |
| `eye` | Cataract, Refractive, Retina, Glaucoma, Paediatric, Oculoplasty |
| `dental` | Cosmetic, Orthodontic, Restorative, Periodontic, Oral Surgery, Endodontic |
| `ivf` | Female Fertility, Male Fertility, ART Procedures, Diagnostic, Surgical, Preservation |

---

### E4: Multi-Specialty Hospital Plan (multispecialty)

**Admin Panel URL:** `https://medassist-ai-docker.onrender.com/hospital-panel`

This plan combines everything: departments, doctors, lab tests, branches **AND** the treatments catalogue. It is the most feature-rich plan.

#### Onboarding Steps:

1. **Login** → admin username and password from Step 13 (change it at first login).
2. **Hospital Profile** → address, maps link, emergency number, front-desk phone.
3. **Departments** → typed per doctor in the Doctors form (no separate page): Cardiology, Orthopaedics, ENT, ...
4. **Doctors** → Add doctors with their department, set schedules.
5. **Treatments Tab** → Unlike single-specialty plans, **no treatments are auto-seeded** because the hospital has no single specialty.
   - **Load Starter Treatments** → Click the starter card. A **specialty picker** appears. Choose which specialty's list to load:
     - `dermatology` → Loads dermatology treatments
     - `ophthalmology` → Loads eye treatments
     - `dental` → Loads dental treatments
     - `fertility` → Loads IVF/fertility treatments
   - Repeat for each specialty the hospital offers. Seeding is idempotent — loading the same list twice won't create duplicates.
   - **Review, price, and activate** each treatment (same flow as specialty plans).
   - **Link doctors to treatments**.
6. **Branches** → Add branch locations.
7. **Lab Tests** → Add lab tests with pricing.
8. **Payment Settings** → see **E6: Razorpay Setup** below.
9. **Holiday Calendar** → Set closures.
10. **Patient Follow-ups / Health check-ins / Data & Support** → as in E1 (steps 7–9).

> [!IMPORTANT]
> The patient WhatsApp menu for multispecialty shows **8 rows** (within Meta's 10-row limit):
> ✨ Our Treatments · 🔍 Find by Concern · Book Appointment · Our Services · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff

---

### E5: Women & Child Hospital Plan (womenchild)

**Admin Panel URL:** `https://medassist-ai-docker.onrender.com/women-child-panel`

For Rainbow Children's / BirthRight-style hospitals: **Child Care**, **Women Care** and **Fertility Care** under one roof, plus OPD departments, doctors, lab and branches. Same feature set as multispecialty. Details: `docs/specialty_plan/12-women-child-plan.md`.

#### Onboarding Steps:

1. **Migration 082 must be applied** before the first Women & Child clinic is created.
2. **Hospital Profile** → Operating hours, address and the **emergency number** (for a children's hospital, the paediatric emergency line). Obstetric and newborn red flags ("water broke", "baby not moving", "convulsion") send patients here.
3. **Departments** → typed per doctor in the Doctors form: Paediatrics, Neonatology, Obstetrics & Gynaecology, Fertility, and each paediatric sub-specialty.
4. **Doctors** → Add doctors, assign departments, set schedules.
5. **Treatments tab** → Three **section tabs**: 👶 Child Care (16 starters), 🌸 Women Care (20), 🌱 Fertility Care (12), all **auto-seeded hidden** at creation. Per tab: set prices, edit, link doctors, then **Show to patients**. A treatment added while a tab is open is filed under that section.
   - First-visit rows (Paediatric / Gynaecology / Fertility Consultation) are marked **Start here** — keep them published, they lead the WhatsApp menu.
   - Delivery, epidural, VBAC, NICU and gynae surgery are **Doctor decides** — patients can read about them, but the button books an examination.
6. **Branches · Lab Tests · Payment Settings · Holiday Calendar · Follow-ups** → as for multispecialty.

> [!IMPORTANT]
> With first-visit rows published the menu shows **9 rows** (within Meta's 10): 🩺 Book Consultation · 🔍 Not sure? Tell us · ✨ What We Treat · Book Appointment · Our Services · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff. *What We Treat* opens 👶 Child Care / 🌸 Women Care / 🌱 Fertility Care.

> [!NOTE]
> **PCPNDT:** any question about the sex of the unborn baby is answered with a legal refusal (en/hi/te) before any AI is involved. Tell the hospital this is built in.

---

### E6: Razorpay Setup (every plan that takes online payment)

1. In the **client's** Razorpay dashboard → Account & Settings → **API Keys**: generate a live key; copy **Key ID** and **Key Secret**.
2. Razorpay → **Webhooks → Add New Webhook**:
   - **URL:** `https://medassist-ai-docker.onrender.com/webhooks/razorpay/<CLINIC_UUID>` (the clinic's own UUID from Step 13 — this is how the payment is matched to the right clinic)
   - **Secret:** create a strong secret and copy it
   - **Events:** `payment.captured` and `payment_link.paid`
3. Admin panel → **Payment Settings**: paste Key ID, Key Secret, Webhook Secret; choose the mode — *Full payment*, *Partial deposit* (set the %), or *No payment required*; set the **cancellation & refund cutoff** (0/2/4/6/12/24 h).
4. There is no platform-wide fallback account: a clinic without its own keys takes payment at the counter.

> [!WARNING]
> Without the webhook, patients pay but bookings stay *pending* until the reconciliation job catches up. A wrong webhook secret is rejected and alerts the clinic admin.

---

## Part F: Live End-to-End Verification (1 Minute)

### 1. Automated Health & Media Probe (Doctor CLI)
Run the diagnostic script to verify token, phone connection, health status, and media uploads:
```bash
python -m scripts.whatsapp_doctor --clinic <CLINIC_UUID>
```
**Expected Output:**
```
[1] token       HTTP 200  OK (Admin Identity)
[2] phone_id    HTTP 200  OK (+91 ... GREEN / VERIFIED / LIVE TIER 250)
[2b] health     can_send_message=AVAILABLE
[4] upload      HTTP 200  OK (id: ...)
```

### 2. Inbound WhatsApp Test:
- From any phone, send `"Hi"` to the clinic's WhatsApp number.
- **Expected response per plan:**

The first "Hi" from a new number first asks for **language** and **data consent** (DPDP), then shows the menu. Expected menu after setup (built by the rules in the Plan Reference Matrix):

| Plan (after setup) | Expected Menu |
|---|---|
| **soloclinic / essential** | Book Appointment · Our Services · Our Doctors · Emergency · Talk to Staff · ❓ How to use |
| **diagstream / diagbooking** (no doctors) | Book Lab Test *(or one row per service type)* · Emergency · Talk to Staff · ❓ How to use |
| **polyclinic** | Book Appointment · Our Services · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff · ❓ How to use |
| **derma / eye / dental** (treatments published) | ✨ Our Treatments · 🔍 Find by Concern · Book Appointment · Our Doctors · Emergency · Talk to Staff · ❓ How to use |
| **ivf** (treatments published) | ✨ Our Treatments · 🔍 Find by Concern · Book Appointment · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff · ❓ How to use |
| **multispecialty** (treatments published) | ✨ Our Treatments · 🔍 Find by Concern · Book Appointment · Our Services · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff · ❓ How to use |
| **womenchild** (first-visit rows published) | 🩺 Book Consultation · 🔍 Not sure? Tell us · ✨ What We Treat · Book Appointment · Our Services · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff · ❓ How to use *(10 rows — the maximum)* |

> [!TIP]
> Treatment menu items (✨ Our Treatments, 🔍 Find by Concern) only appear if the clinic has at least one **active** (visible) treatment. If you just onboarded and haven't activated any treatments yet, send a test after activating at least one.

### 3. Server Log Verification:
- In Render logs, confirm tenant resolution:
  ```json
  {"level": "INFO", "message": "[Accumax Diagnostics] Resolved tenant via phone_number_id '1296654790197336'"}
  ```

### 4. Specialty-Specific Verification (derma / eye / dental / ivf / multispecialty / womenchild):
After activating treatments in the admin panel:
1. Send `"Hi"` → Verify ✨ Our Treatments and 🔍 Find by Concern appear in the menu.
2. Tap **✨ Our Treatments** → Verify treatment categories list appears.
3. Tap a category → Verify individual treatments with pricing.
4. Tap a treatment → Verify treatment card with description, price, and 3 buttons (Book Now / Call Us / Back).
5. Tap **Book Now** → Verify doctor selection and slot booking flow.
6. Send a concern keyword (e.g., `"acne"` for derma) via 🔍 Find by Concern → Verify matching treatments are shown.

---

### 5. Dental Verification (dental plan)
1. Platform → Dental Messaging → **Template Status**: all five `APPROVED`.
2. Admin → Doctors: add your own number as a test dentist's WhatsApp number.
3. Admin → Treatment Plans → **New treatment plan** for your own phone, tick consent, book sitting 1 **tomorrow** with that dentist → you receive `dental_sitting_confirmation`.
4. That evening at 19:00 IST the dentist number receives `dental_doctor_schedule`; next morning 08:30 the patient number receives `dental_sitting_reminder`.
5. Click **Done** on the sitting → about an hour later (09:00–21:00) you receive `dental_sitting_review`; tap **Needs improvement** → the admin bell shows "Patient feedback needs attention".
6. Platform → Dental Messaging shows the messages counted for the month.

### 6. Health Check-in Verification (only if the clinic switched it on)
The job runs 10:30 IST for consultations seen 3 and 7 days earlier. Tap **Still have symptoms** on the received message → the bot replies with the clinic's number.

---

## Outbound Message Master Reference (every automated WhatsApp message)

| Message | When (IST) | Template | Plans | Clinic switch / condition |
|---|---|---|---|---|
| Lab report PDF | on report arrival | `lab_report_ready_v1` / `lab_report_summary_v1` (via `lab_report_template_name`) | report plans | daily report limit (Subscriptions) |
| Connector failure alert → admin | on failure | `admin_alert_template_name` | report plans | configured template |
| Appointment reminder, day before | 09:00 | `appointment_reminder_24h` | booking plans | `reminders` feature; skipped if a dental reminder already went |
| Appointment reminder, 2 h before | hourly | `appointment_reminder_2h` (2 vars) | booking plans | `reminders` feature |
| Doctor-leave cancellation | 08:00 | `appointment_cancelled_doctor_leave` | booking plans | when a doctor leave is added |
| Post-visit follow-up | 10:00, N days after | `post_appointment_followup` / `followup_custom_message_v1` | `reminders` plans | Patient Follow-ups switch (default ON) |
| Health check-in | 10:30, day 3 & 7 | `patient_health_checkin` | `reminders` plans | Health check-ins switch (default **OFF**) |
| Prescription reminders | every 5 min when due | normal chat message (only lands inside the 24h window) | booking plans | per prescription |
| Dental sitting confirmation | on booking / button | `dental_sitting_confirmation` (fallback `appointment_confirmation`) | dental | patient consent; monthly patient limit |
| Dental sitting reminder | 08:30, day before | `dental_sitting_reminder` | dental | patient consent; monthly patient limit |
| Dentist sitting notice | on booking / button | `dental_doctor_sitting` | dental | doctor WhatsApp number; monthly doctor limit |
| Dentist evening schedule | 19:00 (retry 20:30) | `dental_doctor_schedule` | dental | automation setting; monthly doctor limit |
| Sitting review | ~1 h after Done, 09:00–21:00 | `dental_sitting_review` | dental | automation setting; monthly review limit; STOP respected |
| Storage / message limit warnings | at 90 % and 100 % | admin panel bell (no WhatsApp) | all / dental | — |
| Owner reply to a clinic message | on reply | admin panel bell (no WhatsApp) | all | — |

---

## Part G: Troubleshooting & Edge Cases

| Issue / Error | Root Cause | Exact Fix |
|---|---|---|
| **`141008: WABA status is not active`** | Newly created WABA held by Meta pending initial review / verification. | Ensure address + card are linked in WABA. If still blocked, submit 1-click case on [Direct Support](https://business.facebook.com/direct-support/) $\rightarrow$ Topic: Account Status $\rightarrow$ Activate WABA. |
| **`141010: Business verification limited`** | Business portfolio is unverified. | For Tier 250 messaging, formal document upload is not required if card is attached. Meta Security Center will show *"Your organization does not need to be verified"*. |
| **`OAuthException Code 1 / HTTP 500 on /media`** | WABA is inactive, or Token missing `business_management` scope. | Generate a new token on Admin System User with `business_management`, `whatsapp_business_management`, and `whatsapp_business_messaging`. |
| **`(#200) Requires business_management permission`** | Token lacks cross-portfolio permissions for partner-shared assets. | Check `business_management` box when generating the System User Token in Step 8. |
| **`Display Name Rejected / Stuck`** | Number added directly under Zuko Labs instead of Client WABA. | Client must create WABA in their own portfolio with their brand name, then share asset to Zuko Labs (`1602916427428175`). |
| **Status Stuck on `Pending`** | Cloud API cryptographic certificate uninitialized. | Submitting clinic in Platform Panel auto-activates it, or execute manual curl (`POST /{PHONE_NUMBER_ID}/register`). |
| **OTP SMS Not Arriving** | Number currently registered on WhatsApp consumer/business mobile app. | In mobile app: `Settings > Account > Delete Account` (or select Voice Call OTP). |
| **Webhook Not Firing** | Webhook field unsubscribed in Meta Developer portal. | In Meta Developer Portal $\rightarrow$ Step 2 Production Setup $\rightarrow$ Ensure `messages` is toggled Blue (Subscribed). |
| **Treatments not showing in WhatsApp menu** | No treatments are active (all seeded as hidden). | Admin panel → Treatments → Toggle "Show to patients" on at least one treatment. |
| **"Our Treatments" row missing for specialty plan** | `treatment_menu_active()` returns False when zero treatments are published. | Activate at least one treatment in the admin panel. The menu row appears dynamically. |
| **Starter treatments not seeding** | Clinic plan mismatch or treatments already exist. | Seeding is idempotent. For multispecialty and womenchild, select a list in the starter picker. |
| **Reports fail with "template not found" for a new clinic** | `lab_report_template_name` not set → the global default `lab_report_delivery` is used, which the client's WABA does not have. | Step 13b SQL; check with `python -m scripts.whatsapp_doctor --clinic <UUID>` (line `[3] template`). |
| **Admin password lost** | It is shown only once on Create. | Reset it with the owner API: `curl -X PUT https://medassist-ai-docker.onrender.com/platform/reset-admin-password -u '<OWNER_USERNAME>:<OWNER_PASSWORD>' -H 'Content-Type: application/json' -d '{"username": "<ADMIN_USERNAME>", "new_password": "<NEW_PASSWORD_8+_CHARS>"}'` |
| **Paid booking stays pending** | Razorpay webhook missing, wrong URL (clinic UUID) or wrong secret. | Part E6. |
| **`132000` / "number of parameters does not match"** | Template approved with a different number of variables than the code sends. | Compare with the Variables column of the Template Requirement Matrix. Most common: `appointment_reminder_2h` created with 1 variable (old SOP) — it needs 2. |
| **Dental sitting messages not arriving** | Templates not approved; patient consent not recorded; monthly limit reached. | Platform → Dental Messaging → Template Status. Plan detail shows "Patient WhatsApp: Not allowed" → **Record consent**. Check the usage bars on Treatment Plans. |
| **Dentist gets no WhatsApp** | No WhatsApp number on the doctor; plan's "WhatsApp the doctor" off; evening schedule switched off; `dental_doctor_*` not approved. | Fill the number on Doctors; check the plan and Automation settings; Template Status. |
| **Receptionist can't see Treatment Plans** | Missing permission, or clinic is not on the dental plan. | Staff Accounts → tick *Manage Dental Treatment Plans & Sittings*. |
| **"This doctor does not work at this plan's branch"** | The plan belongs to a branch and the dentist is not assigned there. | Branches → assign the dentist to that branch, or pick a dentist of that branch. |
| **Health check-ins never sent** | Switched off (default), or template not approved. | Hospital Profile → Health check-ins switch; register `patient_health_checkin`. |
| **Connector alerts never reach the admin** | `admin_alert_template_name` not set. | Register `admin_alert_v1` and set the name (Step 10E2). |
| **Treatment booking creates duplicate appointments** | Should not happen — `uq_appointment_active_slot` constraint prevents this. | Verify the unique index exists: `SELECT indexname FROM pg_indexes WHERE indexname = 'uq_appointment_active_slot';` |

---

## Quick Reference Cheat Sheet for New Clients

```
Zuko Labs Partner Business ID: 1602916427428175
Kriya AI Meta App ID:          946290901317238
Webhook URL:                   https://medassist-ai-docker.onrender.com/webhook
Platform Panel URL:            https://medassist-ai-docker.onrender.com/platform-panel
Default Activation PIN:        123456
Doctor Diagnostic CLI:         python -m scripts.whatsapp_doctor --clinic <CLINIC_UUID>
Razorpay webhook (per clinic): https://medassist-ai-docker.onrender.com/webhooks/razorpay/<CLINIC_UUID>
                               events: payment.captured, payment_link.paid
Dental templates:              Platform Panel -> Dental Messaging -> Submit Templates
```

### Admin Panel URLs by Plan:
```
General Plans:     https://medassist-ai-docker.onrender.com/admin-panel
Dermatology:       https://medassist-ai-docker.onrender.com/derma-panel
Eye Hospital:      https://medassist-ai-docker.onrender.com/eye-panel
Dental Clinic:     https://medassist-ai-docker.onrender.com/dental-panel
IVF Centre:        https://medassist-ai-docker.onrender.com/ivf-panel
Multi-Specialty:   https://medassist-ai-docker.onrender.com/hospital-panel
Women & Child:     https://medassist-ai-docker.onrender.com/women-child-panel
```

### Feature Summary by Plan:
See the verified **Feature matrix** in the Plan Reference Matrix at the top of this document (generated from `PLAN_FEATURES`).
