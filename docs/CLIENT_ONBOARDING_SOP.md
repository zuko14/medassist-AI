# Kriya AI — Client Onboarding Standard Operating Procedure (SOP)
**Document Version:** 2.1 (Production-Hardened Standard)  
**Provider:** Zuko Labs (Meta Tech Provider — Business ID: `1602916427428175`)  
**Platform:** Kriya AI Multi-Tenant WhatsApp Healthcare Automation (App ID: `946290901317238`)

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
└─────────────────────────────────────────────────────────┘
```

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

### Step 10: Diagnostic Center Message Template Setup (Crucial for Outbound Reports)
Every diagnostic center requires an approved **`UTILITY`** template with a **`DOCUMENT`** header to deliver PDF lab reports to patients outside the 24-hour window.

> [!TIP]
> Always submit the template in **`en_US`** (English US). Meta's automated classifier evaluates `en_US` utility templates much faster than generic `en`.

#### Option A: Standard 2-Variable Report Template
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

#### Option B: AI Summary 3-Variable Report Template (Carries Clinical Summary)
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
  - `{{1}}` $\rightarrow$ Patient Full Name (e.g., `Mrs. P. Kalyani`)
  - `{{2}}` $\rightarrow$ Lab Test Name (e.g., `Complete Blood Picture / Lipid Profile`)
  - `{{3}}` $\rightarrow$ Clean flattened AI Summary text (e.g., `All tested parameters are within normal reference ranges.`)

#### 1-Click Automated Creation Script (Terminal):
Run this script to upload the document sample and register both templates on the client's WABA automatically:
```bash
python -c "
import httpx
token = '<META_ADMIN_SYSTEM_USER_TOKEN>'
app_id = '946290901317238'
waba_id = '<CLIENT_WABA_ID>'

# 1. Resumable session upload
pdf_bytes = b'%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n'
r1 = httpx.post(f'https://graph.facebook.com/v22.0/{app_id}/uploads', headers={'Authorization': f'Bearer {token}'}, params={'file_length': len(pdf_bytes), 'file_type': 'application/pdf'}).json()
r2 = httpx.post(f'https://graph.facebook.com/v22.0/{r1[\"id\"]}', headers={'Authorization': f'OAuth {token}', 'file_offset': '0'}, content=pdf_bytes).json()
h = r2['h']

# 2. Create standard template (lab_report_ready_v1)
p1 = {
    'name': 'lab_report_ready_v1',
    'category': 'UTILITY',
    'language': 'en_US',
    'components': [
        {'type': 'HEADER', 'format': 'DOCUMENT', 'example': {'header_handle': [h]}},
        {'type': 'BODY', 'text': 'Dear {{1}}, your medical lab report for {{2}} is ready and attached above. Please consult your physician for interpretation.', 'example': {'body_text': [['Mrs. P. Kalyani', 'Lipid Profile']]}}
    ]
}
httpx.post(f'https://graph.facebook.com/v22.0/{waba_id}/message_templates', headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, json=p1)

# 3. Create 3-variable AI summary template (lab_report_summary_v1)
p2 = {
    'name': 'lab_report_summary_v1',
    'category': 'UTILITY',
    'language': 'en_US',
    'components': [
        {'type': 'HEADER', 'format': 'DOCUMENT', 'example': {'header_handle': [h]}},
        {'type': 'BODY', 'text': 'Dear {{1}}, your medical lab report for {{2}} is ready and attached above.\n\nSummary: {{3}}\n\nPlease consult your physician for interpretation.', 'example': {'body_text': [['Mrs. P. Kalyani', 'Lipid Profile', 'All tested parameters are within normal reference ranges.']]}}
    ]
}
httpx.post(f'https://graph.facebook.com/v22.0/{waba_id}/message_templates', headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, json=p2)
print('Templates Created Successfully!')
"
```

### Step 10B: Post-Visit Follow-up Message Template Setup (Required for Patient Follow-ups)
Every clinic that enables **Patient Follow-ups** (via Hospital Profile → Patient Follow-ups in Admin Panel) requires approved `UTILITY` templates on their WABA. Follow-ups always land **outside the 24h customer-service window**, so only templates can deliver them.

> [!TIP]
> Two templates are needed — one is the **built-in default** (always used as fallback) and the other **carries the admin's custom message** (from the Admin Panel textarea). Both must be approved before follow-ups can actually reach patients.

#### Template A: Built-in Default Follow-up (`post_appointment_followup`)
* **Template Name:** `post_appointment_followup`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Dear patient {{1}}, we hope you are feeling better after your recent visit to <CLINIC_NAME>. Your health and recovery are important to us. If you need a follow-up appointment, please reply YES or call us at {{2}} to schedule. Wishing you good health!
  ```
* **Variables:**
  - `{{1}}` → Patient Full Name (e.g., `Ravi Kumar`)
  - `{{2}}` → Hospital Phone Number (e.g., `+919490386668`)

#### Template B: Custom Message Follow-up (`followup_custom_message_v1`)
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
  - `{{1}}` → Patient First Name (e.g., `Ravi Kumar`)
  - `{{2}}` → Custom message text entered by admin in the Admin Panel (e.g., `We hope your recovery is going well. Please continue the prescribed medication and attend your next check-up as scheduled.`)

> [!IMPORTANT]
> Replace `<CLINIC_NAME>` in the body text with the actual clinic name before submitting. Meta rejects template modifications after approval, so get the name right the first time.

#### Clinic Config Keys (set automatically via Admin Panel or manually in DB):
| Config Key | Purpose | Example Value |
|---|---|---|
| `followup_enabled` | Enable/disable follow-ups | `true` |
| `followup_days` | Days after visit to send (1–30) | `1` |
| `followup_template_name` | Built-in template name | `post_appointment_followup` |
| `followup_message_template_name` | Custom-message template name | `followup_custom_message_v1` |
| `followup_message` | Admin's custom message text | *(set via Admin Panel textarea)* |

---

### Step 10C: Automated Appointment Reminder Templates (24-Hour & 2-Hour)
Appointment reminders are automatically scheduled by `SchedulerService` (`app/services/scheduler.py`) and sent proactively before the patient's slot. Because they land outside the customer-initiated window, they strictly require approved `UTILITY` templates.

#### Template A: 24-Hour Appointment Reminder (`appointment_reminder_24h`)
* **Template Name:** `appointment_reminder_24h`
* **Category:** `UTILITY`
* **Language:** `en` (or `en_US`)
* **Body Text:**
  ```text
  Reminder: Your appointment with {{1}} is tomorrow at {{2}}. Please arrive 10 mins early. Reply CANCEL if you can't make it.
  ```
* **Variables:**
  - `{{1}}` → Doctor Name (e.g., `Dr. Ramesh Sharma`)
  - `{{2}}` → Slot Time (e.g., `10:30 AM`)

#### Template B: 2-Hour Appointment Reminder (`appointment_reminder_2h`)
* **Template Name:** `appointment_reminder_2h`
* **Category:** `UTILITY`
* **Language:** `en` (or `en_US`)
* **Body Text:**
  ```text
  Your appointment at {{1}} is in 2 hours with {{2}}. Reply CANCEL to cancel.
  ```
* **Variables:**
  - `{{1}}` → Clinic / Hospital Name (e.g., `City Care Hospital`)
  - `{{2}}` → Doctor Name (e.g., `Dr. Ramesh Sharma`)

---

### Step 10D: Appointment Confirmation & Doctor Cancellation Templates

#### Template A: Outbound Appointment Confirmation (`appointment_confirmation`)
* **Template Name:** `appointment_confirmation`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Your appointment with {{1}} ({{2}}) is confirmed for {{3}} at {{4}}. Reply CANCEL to cancel. - {{5}}
  ```
* **Variables:**
  - `{{1}}` → Doctor Name (e.g., `Dr. Ramesh Sharma`)
  - `{{2}}` → Department (e.g., `Cardiology`)
  - `{{3}}` → Date (e.g., `05 Sep 2026`)
  - `{{4}}` → Slot Time (e.g., `10:30 AM`)
  - `{{5}}` → Hospital / Clinic Name (e.g., `City Care Hospital`)

#### Template B: Emergency Doctor Cancellation / Leave (`appointment_cancelled_doctor_leave`)
* **Template Name:** `appointment_cancelled_doctor_leave`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  We're sorry, your appointment with {{1}} on {{2}} has been cancelled as the doctor is unavailable. Reply REBOOK to reschedule. We apologise for the inconvenience.
  ```
* **Variables:**
  - `{{1}}` → Doctor Name (e.g., `Dr. Ramesh Sharma`)
  - `{{2}}` → Date & Time (e.g., `Tomorrow at 10:30 AM`)

---

### Step 10E: DPDP Act 2023 & Compliance Templates

#### Template A: Opt-Out Confirmation (`opt_out_confirmation`)
* **Template Name:** `opt_out_confirmation`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  You've been unsubscribed from {{1}} WhatsApp reminders. Message us anytime to re-subscribe. For urgent help call {{2}}.
  ```

#### Template B: Data Deletion Receipt (`data_deletion_confirmation`)
* **Template Name:** `data_deletion_confirmation`
* **Category:** `UTILITY`
* **Language:** `en`
* **Body Text:**
  ```text
  Your data has been deleted from {{1}} systems as requested. Reference: {{2}}. For records, contact {{3}}.
  ```

---

### Step 10F: 1-Click Master Template Setup Script (Registers ALL Templates)
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
    }
]

for t in templates:
    res = httpx.post(f'https://graph.facebook.com/v22.0/{waba_id}/message_templates', headers=headers, json=t)
    status = 'CREATED' if res.status_code in [200, 201] else f'FAILED ({res.status_code}): {res.text}'
    print(f'Template [{t[\"name\"]}]: {status}')

print('\nAll Core Templates Submitted for Meta Review Successfully!')
"
```


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

### Step 13: Register Tenant in Kriya AI Platform Panel
1. Open Kriya AI Platform Panel: `https://medassist-ai-docker.onrender.com/platform-panel`
2. Click **Create Hospital / Clinic**:
   - **Hospital / Clinic Name:** `Accumax Diagnostics`
   - **WhatsApp Number (E.164):** `+919281235959`
   - **Plan:** `Diagnostic center` or `Polyclinic`
   - **Meta Phone Number ID:** `1296654790197336`
   - **Meta WABA ID:** `1702889104159864`
   - **Meta Permanent Access Token:** `EAAN...` *(From Step 8)*
   - **Lab Report Template Name:** `lab_report_ready_v1`
3. Click **Create Hospital / Clinic**.

---

## Part C: Live End-to-End Verification (1 Minute)

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
- **Verification:** Kriya AI responds within 1.5 seconds with the clinic's personalized interactive menu.

### 3. Server Log Verification:
- In Render logs, confirm tenant resolution:
  ```json
  {"level": "INFO", "message": "[Accumax Diagnostics] Resolved tenant via phone_number_id '1296654790197336'"}
  ```

---

## Part D: Troubleshooting & Edge Cases

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
