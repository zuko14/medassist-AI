# Kriya AI — Client Onboarding Standard Operating Procedure (SOP)
**Document Version:** 3.0 (All Plans — Production-Hardened Standard)  
**Provider:** Zuko Labs (Meta Tech Provider — Business ID: `1602916427428175`)  
**Platform:** Kriya AI Multi-Tenant WhatsApp Healthcare Automation (App ID: `946290901317238`)  
**Plans Covered:** soloclinic · diagstream · diagbooking · essential · polyclinic · enterprise · derma · eye · dental · ivf · multispecialty

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

## Plan Reference Matrix

Before onboarding, identify which plan matches the client's facility. Each plan determines which WhatsApp menu items the patient sees, which templates are required, and which admin panel URL the clinic admin uses.

| Plan Slug | Display Name | Target Facility | WhatsApp Menu | Admin Panel URL | Message Quota |
|---|---|---|---|---|---|
| `soloclinic` | Solo Clinic | Single doctor / small clinic | Book · Doctors · Emergency · Staff | `/admin-panel` | 1,000 |
| `diagstream` | Diagnostic Center | Lab-only (report delivery + lab test booking) | Book Lab Test · Emergency · Staff | `/admin-panel` | 2,000 |
| `diagbooking` | Diagnostic Booking | Lab-test booking only (no report delivery) | Book Lab Test · Emergency · Staff | `/admin-panel` | 1,000 |
| `essential` | Essential Hospital | Full-service hospital | Book · Services · Doctors · Emergency · Staff | `/admin-panel` | 2,500 |
| `polyclinic` | Polyclinic | Multi-branch hospital + diagnostics | Book · Services · Doctors · 🧪 Lab Test · Emergency · Staff | `/admin-panel` | 5,000 |
| `enterprise` | Enterprise | Unlimited (all features, wildcard) | Full menu (all features) | `/admin-panel` | 10,000 |
| `derma` | Dermatology Clinic | Skin, hair & cosmetology specialty | ✨ Treatments · 🔍 Concern · Book · Doctors · Emergency · Staff | `/derma-panel` | 2,500 |
| `eye` | Eye Hospital | Ophthalmology & vision care | ✨ Treatments · 🔍 Concern · Book · Doctors · Emergency · Staff | `/eye-panel` | 2,500 |
| `dental` | Dental Clinic | Dental surgery & orthodontics | ✨ Treatments · 🔍 Concern · Book · Doctors · Emergency · Staff | `/dental-panel` | 2,500 |
| `ivf` | IVF Centre | Fertility & reproductive medicine | ✨ Treatments · 🔍 Concern · Book · Doctors · 🧪 Lab Test · Emergency · Staff | `/ivf-panel` | 2,500 |
| `multispecialty` | Multi-Specialty Hospital | General hospital + treatments catalogue | ✨ Treatments · 🔍 Concern · Book · Services · Doctors · 🧪 Lab Test · Emergency · Staff | `/hospital-panel` | 5,000 |

> [!NOTE]
> **Specialty plans** (`derma`, `eye`, `dental`, `ivf`) drop the "Our Services" departments row because one department is not a menu. **Multispecialty** keeps it — a hospital with fifteen departments needs that row. The `enterprise` plan is a wildcard (`*`) — all features are always on, but the treatments catalogue must be explicitly enabled via a feature override.

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

| Template Name | Category | Plans That Need It |
|---|---|---|
| `lab_report_ready_v1` | UTILITY (DOCUMENT header) | diagstream, essential, polyclinic, enterprise, multispecialty |
| `lab_report_summary_v1` | UTILITY (DOCUMENT header) | diagstream, essential, polyclinic, enterprise, multispecialty |
| `appointment_reminder_24h` | UTILITY | soloclinic, essential, polyclinic, enterprise, derma, eye, dental, ivf, multispecialty |
| `appointment_reminder_2h` | UTILITY | soloclinic, essential, polyclinic, enterprise, derma, eye, dental, ivf, multispecialty |
| `appointment_confirmation` | UTILITY | soloclinic, essential, polyclinic, enterprise, derma, eye, dental, ivf, multispecialty |
| `appointment_cancelled_doctor_leave` | UTILITY | soloclinic, essential, polyclinic, enterprise, derma, eye, dental, ivf, multispecialty |
| `post_appointment_followup` | UTILITY | All plans with `reminders` feature |
| `followup_custom_message_v1` | UTILITY | All plans with `reminders` feature |
| `opt_out_confirmation` | UTILITY | All plans (DPDP Act 2023 compliance) |
| `data_deletion_confirmation` | UTILITY | All plans (DPDP Act 2023 compliance) |

> [!IMPORTANT]
> **`diagstream`** and **`diagbooking`** do NOT need appointment-related templates (no doctor booking). **`diagbooking`** does NOT need lab report templates (no report connector). Specialty plans (`derma`, `eye`, `dental`, `ivf`) do NOT need lab report templates (no report connector). **`multispecialty`** needs ALL templates because it has both booking and lab report features.

---

### Step 10A: Lab Report Templates (diagstream / essential / polyclinic / enterprise / multispecialty)

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
**Required for:** soloclinic, essential, polyclinic, enterprise, derma, eye, dental, ivf, multispecialty  
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
  Your appointment at <CLINIC_NAME> is in 2 hours with {{1}}. Reply CANCEL to cancel.
  ```
* **Variables:**
  - `{{1}}` → Doctor Name (e.g., `Dr. Ramesh Sharma`)

> [!IMPORTANT]
> Replace `<CLINIC_NAME>` with the actual clinic/hospital name before submitting. Meta rejects modifications after approval.

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

### Step 10F: Plan-Specific Template Checklist

Use this checklist to confirm which templates to register per plan:

| # | Template Name | solo | diag-stream | diag-booking | essential | poly | enterprise | derma | eye | dental | ivf | multi-specialty |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `lab_report_ready_v1` | ❌ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| 2 | `lab_report_summary_v1` | ❌ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| 3 | `appointment_reminder_24h` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 4 | `appointment_reminder_2h` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 5 | `appointment_confirmation` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 6 | `appointment_cancelled_doctor_leave` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 7 | `post_appointment_followup` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 8 | `followup_custom_message_v1` | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 9 | `opt_out_confirmation` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 10 | `data_deletion_confirmation` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

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
            {'type': 'BODY', 'text': f'Your appointment at {clinic_name} is in 2 hours with {{{{1}}}}. Reply CANCEL to cancel.', 'example': {'body_text': [['Dr. Ramesh Sharma']]}}
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
1. Open Kriya AI Platform Panel: `https://medassist-ai-docker.onrender.com/platform-panel`
2. Click **Create Hospital / Clinic** and fill in:

| Field | Value | Notes |
|---|---|---|
| **Hospital / Clinic Name** | `Accumax Diagnostics` | Exact display name |
| **WhatsApp Number (E.164)** | `+919281235959` | Must match registered number |
| **Plan** | Select from dropdown | See Plan Reference Matrix above |
| **Meta Phone Number ID** | `1296654790197336` | From Step 9 |
| **Meta WABA ID** | `1702889104159864` | From Step 9 |
| **Meta Permanent Access Token** | `EAAN...` | From Step 8 |
| **Lab Report Template Name** | `lab_report_ready_v1` | Only for plans with `lab_reports` feature |

3. Click **Create Hospital / Clinic**.

#### Plan-Specific Platform Panel Settings:

| Plan | Plan Dropdown Label | Lab Report Template | Additional Notes |
|---|---|---|---|
| `soloclinic` | Solo Clinic | — | Single doctor setup |
| `diagstream` | Diagnostic Center | `lab_report_ready_v1` | Lab-only, no doctor booking |
| `diagbooking` | Diagnostic Booking | — | Lab test booking only, no reports |
| `essential` | Essential Hospital | `lab_report_ready_v1` | Full-service hospital |
| `polyclinic` | Polyclinic | `lab_report_ready_v1` | Multi-branch + diagnostics |
| `enterprise` | Enterprise | `lab_report_ready_v1` | All features, unlimited |
| `derma` | Dermatology Clinic | — | Treatments auto-seeded on creation |
| `eye` | Eye Hospital | — | Treatments auto-seeded on creation |
| `dental` | Dental Clinic | — | Treatments auto-seeded on creation |
| `ivf` | IVF Centre | — | Treatments + lab test booking auto-seeded |
| `multispecialty` | Multi-Specialty Hospital | `lab_report_ready_v1` | No auto-seed; admin loads starters manually |

---

## Part E: Post-Onboarding Configuration (Plan-Specific)

After the tenant is created in the Platform Panel, the **clinic admin** (or you during the demo) must configure the clinic through the admin panel. The admin panel URL depends on the plan.

### E1: General Plans (soloclinic / essential / polyclinic / enterprise)

**Admin Panel URL:** `https://medassist-ai-docker.onrender.com/admin-panel`

1. **Login** → Use clinic WhatsApp number or credentials.
2. **Hospital Profile** → Set operating hours, address, emergency number.
3. **Departments** → Add departments (essential, polyclinic, enterprise, multispecialty only).
4. **Doctors** → Add doctors with name, department, qualification, slot duration, and schedule.
5. **Payment Settings** → Enter Razorpay Key ID, Key Secret, and Webhook Secret.
6. **Holiday Calendar** → Set public holidays and clinic closures.
7. **Patient Follow-ups** → Enable and configure follow-up timing and message.

**Additional for polyclinic / enterprise:**
8. **Branches** → Add branch locations with address and phone.
9. **Lab Tests** → Add lab tests with name, price, category, turnaround time.

---

### E2: Diagnostic Plans (diagstream / diagbooking)

**Admin Panel URL:** `https://medassist-ai-docker.onrender.com/admin-panel`

1. **Login** → Use clinic WhatsApp number.
2. **Hospital Profile** → Set operating hours, address.
3. **Branches** → Add branches (multi-branch supported).
4. **Lab Tests** → Add tests with name, price, category, turnaround time.
5. **Payment Settings** → Enter Razorpay credentials.
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

1. **Login** → Use clinic WhatsApp number.
2. **Hospital Profile** → Set operating hours, address, emergency number.
3. **Doctors** → Add doctors with name, qualification, slot duration, schedule.
4. **Treatments Tab** → This appears automatically for specialty plans:
   - **Load Starter Treatments** → Click the starter card. The system auto-seeds 10+ clinically vetted treatments specific to the specialty (dermatology, ophthalmology, dental, or fertility).
   - **Review & Activate** → Each treatment is seeded as **hidden** (`is_active = false`). The admin must:
     - Review the name, description, and category
     - Set pricing (₹ per procedure or "Consultation" for free assessment)
     - Toggle **"Show to patients"** to make it visible on WhatsApp
   - **Add Custom Treatments** → Click "Add Treatment" to create custom treatments with AI-assisted description drafting.
   - **Link Doctors to Treatments** → Assign which doctors perform each treatment. If no doctors are linked, any available doctor can be booked.
5. **Payment Settings** → Enter Razorpay credentials.
6. **Holiday Calendar** → Set closures.
7. **Branches** → Add branch locations (specialty chains often have multiple centres).

**Additional for ivf:**
8. **Lab Tests** → Add hormone/fertility tests (AMH, semen analysis, etc.) with pricing.

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

1. **Login** → Use clinic WhatsApp number.
2. **Hospital Profile** → Set operating hours, address, emergency number.
3. **Departments** → Add all OPD departments (Cardiology, Orthopaedics, ENT, etc.).
4. **Doctors** → Add doctors, assign to departments, set schedules.
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
8. **Payment Settings** → Enter Razorpay credentials.
9. **Holiday Calendar** → Set closures.
10. **Patient Follow-ups** → Enable and configure.

> [!IMPORTANT]
> The patient WhatsApp menu for multispecialty shows **8 rows** (within Meta's 10-row limit):
> ✨ Our Treatments · 🔍 Find by Concern · Book Appointment · Our Services · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff

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

| Plan Type | Expected Menu |
|---|---|
| **soloclinic** | Book Appointment · Our Doctors · Emergency · Talk to Staff |
| **diagstream / diagbooking** | Book Lab Test · Emergency · Talk to Staff |
| **essential** | Book Appointment · Our Services · Our Doctors · Emergency · Talk to Staff |
| **polyclinic** | Book Appointment · Our Services · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff |
| **derma / eye / dental** | ✨ Our Treatments · 🔍 Find by Concern · Book Appointment · Our Doctors · Emergency · Talk to Staff |
| **ivf** | ✨ Our Treatments · 🔍 Find by Concern · Book Appointment · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff |
| **multispecialty** | ✨ Our Treatments · 🔍 Find by Concern · Book Appointment · Our Services · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff |

> [!TIP]
> Treatment menu items (✨ Our Treatments, 🔍 Find by Concern) only appear if the clinic has at least one **active** (visible) treatment. If you just onboarded and haven't activated any treatments yet, send a test after activating at least one.

### 3. Server Log Verification:
- In Render logs, confirm tenant resolution:
  ```json
  {"level": "INFO", "message": "[Accumax Diagnostics] Resolved tenant via phone_number_id '1296654790197336'"}
  ```

### 4. Specialty-Specific Verification (derma / eye / dental / ivf / multispecialty):
After activating treatments in the admin panel:
1. Send `"Hi"` → Verify ✨ Our Treatments and 🔍 Find by Concern appear in the menu.
2. Tap **✨ Our Treatments** → Verify treatment categories list appears.
3. Tap a category → Verify individual treatments with pricing.
4. Tap a treatment → Verify treatment card with description, price, and 3 buttons (Book Now / Call Us / Back).
5. Tap **Book Now** → Verify doctor selection and slot booking flow.
6. Send a concern keyword (e.g., `"acne"` for derma) via 🔍 Find by Concern → Verify matching treatments are shown.

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
| **Starter treatments not seeding** | Clinic plan mismatch or treatments already exist. | Seeding is idempotent. For multispecialty, select a specialty in the starter picker. |
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
```

### Admin Panel URLs by Plan:
```
General Plans:     https://medassist-ai-docker.onrender.com/admin-panel
Dermatology:       https://medassist-ai-docker.onrender.com/derma-panel
Eye Hospital:      https://medassist-ai-docker.onrender.com/eye-panel
Dental Clinic:     https://medassist-ai-docker.onrender.com/dental-panel
IVF Centre:        https://medassist-ai-docker.onrender.com/ivf-panel
Multi-Specialty:   https://medassist-ai-docker.onrender.com/hospital-panel
```

### Feature Summary by Plan:
```
                    solo  diag-s  diag-b  essential  poly  enterprise  derma  eye  dental  ivf  multi
Booking              ✅     ❌      ❌       ✅       ✅      ✅        ✅    ✅     ✅    ✅     ✅
Reminders            ✅     ❌      ❌       ✅       ✅      ✅        ✅    ✅     ✅    ✅     ✅
Lab Reports          ❌     ✅      ❌       ✅       ✅      ✅        ❌    ❌     ❌    ❌     ✅
Lab Test Booking     ❌     ✅      ✅       ❌       ✅      ✅        ❌    ❌     ❌    ✅     ✅
Treatments           ❌     ❌      ❌       ❌       ❌      ❌*       ✅    ✅     ✅    ✅     ✅
Multi-Department     ❌     ❌      ❌       ✅       ✅      ✅        ❌    ❌     ❌    ❌     ✅
Multi-Branch         ❌     ✅      ✅       ❌       ✅      ✅        ✅    ✅     ✅    ✅     ✅
Payments             ✅     ✅      ✅       ✅       ✅      ✅        ✅    ✅     ✅    ✅     ✅
Analytics            ❌     ❌      ❌       ✅       ✅      ✅        ✅    ✅     ✅    ✅     ✅

* Enterprise has all features via wildcard, but treatments catalogue requires explicit feature override.
```
