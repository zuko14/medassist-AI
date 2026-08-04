# MediAssist AI — Client Onboarding Setup Recipes (Step-by-Step)

This guide provides dummy-proof, copy-pasteable setup recipes for onboarding any client type. Anyone (operations manager, assistant, or developer) can follow these exact steps to onboard a client in 10 minutes.

---

## 🛑 COMMON PRE-FLIGHT STEP (Do This For ALL Client Types First)

Before setting up any clinic type, you **must** get their WhatsApp number registered in Meta and get two values:
1. `meta_phone_number_id`
2. `meta_access_token`

### How to get Meta credentials in 3 minutes:
1. Go to [developers.facebook.com](https://developers.facebook.com) → Open your App → **WhatsApp → API Setup**.
2. Click **Add Phone Number** → Enter clinic display name → Verify via OTP.
3. Select the number → Copy **Phone Number ID** (e.g. `971342239407011`).
4. Go to [business.facebook.com](https://business.facebook.com) → **Settings → System Users**.
5. Click **Generate New Token** → Tick `whatsapp_business_messaging` & `whatsapp_business_management` → Expiration: **Never** → Copy Permanent Token (`EAAG...`).

---

# 🏥 RECIPE 1: SOLO CLINIC (`soloclinic`)
> **Use case:** Single doctor operating at 1 location (e.g. Dr. Sharma's Clinic).  
> **Features:** 24/7 WhatsApp AI Bot, Appointment Scheduling, Reminders, Patient FAQ.

### Step 1: Register the Clinic
Open terminal and run (replace values in `< >`):
```bash
curl -X POST https://medassist-ai.onrender.com/admin/clinics \
  -H "X-Admin-Secret: mediassist_admin_2026" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Sharma Healthcare Clinic",
    "clinic_name": "Sharma Clinic",
    "whatsapp_number": "+919876543210",
    "plan": "soloclinic",
    "meta_phone_number_id": "<META_PHONE_NUMBER_ID>",
    "meta_access_token": "<META_PERMANENT_TOKEN>",
    "doctor_name": "Dr. Rahul Sharma",
    "language": "en",
    "timezone": "Asia/Kolkata"
  }'
```
> 📌 **COPY THE RETURNED `id` (CLINIC_UUID)** e.g., `a1b2c3d4-1111-2222-3333-444455556666`.

### Step 2: Add the Doctor
```bash
curl -X POST "https://medassist-ai.onrender.com/admin/doctors?clinic_id=<CLINIC_UUID>" \
  -u "admin:Secure@9999" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Dr. Rahul Sharma",
    "specialization": "General Physician",
    "department": "General Medicine",
    "available_days": "Mon,Tue,Wed,Thu,Fri,Sat",
    "morning_slots": ["09:00","09:30","10:00","10:30","11:00","11:30"],
    "evening_slots": ["17:00","17:30","18:00","18:30"],
    "is_active": true,
    "consultation_fee": 500
  }'
```

### Step 3: Test on WhatsApp
1. Save `+919876543210` on your phone.
2. Send `Hi`.
3. Bot greets as *Sharma Clinic* and lets you book appointments with *Dr. Rahul Sharma*. Done! ✅

---

# 🔬 RECIPE 2: DIAGNOSTIC CENTER (`diagstream`)
> **Use case:** Standalone Pathology / Radiology Lab (e.g. Vijaya Diagnostics). No doctor bookings.  
> **Features:** PDF Lab Report WhatsApp Delivery, Groq AI Plain-Language Report Breakdown, MocDoc Auto-Sync.

### Step 1: Register the Diagnostic Center
```bash
curl -X POST https://medassist-ai.onrender.com/admin/clinics \
  -H "X-Admin-Secret: mediassist_admin_2026" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Apex Diagnostic Labs",
    "clinic_name": "Apex Labs",
    "whatsapp_number": "+919876543211",
    "plan": "diagstream",
    "meta_phone_number_id": "<META_PHONE_NUMBER_ID>",
    "meta_access_token": "<META_PERMANENT_TOKEN>",
    "doctor_name": "Laboratory Services",
    "language": "en",
    "timezone": "Asia/Kolkata"
  }'
```
> 📌 **COPY THE RETURNED `id` (CLINIC_UUID)**.

### Step 2: (Optional) Connect MocDoc HMIS for Auto Report Sync
If the lab uses MocDoc:
1. Encrypt MocDoc password in terminal:
   ```bash
   python -m connectors.runner --encrypt-password
   ```
   Paste password → Copy encrypted output (`gAAAAAB...`).
2. Go to **Supabase Dashboard → Table Editor → `integration_connectors`** table → Insert Row:
   - `clinic_id`: `<CLINIC_UUID>`
   - `connector_type`: `mocdoc`
   - `is_enabled`: `true`
   - `config`:
     ```json
     {
       "base_url": "https://mocdoc.com",
       "username": "lab@apexlabs.com",
       "password": "<ENCRYPTED_PASSWORD>",
       "clinic_slug": "apex-diagnostic-labs"
     }
     ```
3. Test dry run:
   ```bash
   python -m connectors.runner --connector mocdoc --clinic-id <CLINIC_UUID> --once --dry-run
   ```

### Step 3: Test Manual Lab Report Delivery (If not using MocDoc)
Upload a test report to send via WhatsApp:
```bash
curl -X POST "https://medassist-ai.onrender.com/admin/lab-reports/upload" \
  -u "admin:Secure@9999" \
  -F "file=@C:/path/to/test_report.pdf" \
  -F "patient_phone=+91XXXXXXXXXX" \
  -F "patient_name=Ismat Parveen" \
  -F "report_name=Complete Blood Count" \
  -F "report_type=Laboratory" \
  -F "clinic_id=<CLINIC_UUID>"
```
Patient receives PDF + AI report breakdown on WhatsApp! Done! ✅

---

# 🏢 RECIPE 3: POLYCLINIC CHAIN (`polyclinic`)
> **Use case:** Polyclinic with 2 or more physical locations under ONE WhatsApp number (e.g., City Clinics with Kukatpally and Ameerpet branches).  
> **Features:** Interactive Branch Selector on WhatsApp, Branch-Specific Doctor Scheduling, Combined Lab Reports.

### Step 1: Register the Polyclinic Chain
```bash
curl -X POST https://medassist-ai.onrender.com/admin/clinics \
  -H "X-Admin-Secret: mediassist_admin_2026" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "City Care Polyclinic Network",
    "clinic_name": "City Care",
    "whatsapp_number": "+919876543212",
    "plan": "polyclinic",
    "meta_phone_number_id": "<META_PHONE_NUMBER_ID>",
    "meta_access_token": "<META_PERMANENT_TOKEN>",
    "doctor_name": "Multiple Specialists",
    "language": "en",
    "timezone": "Asia/Kolkata"
  }'
```
> 📌 **COPY THE RETURNED `id` (CLINIC_UUID)**.

### Step 2: Add Branch 1 (Kukatpally Branch)
```bash
curl -X POST "https://medassist-ai.onrender.com/admin/branches?clinic_id=<CLINIC_UUID>" \
  -u "admin:Secure@9999" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Kukatpally Branch",
    "short_name": "KPL",
    "address": "Plot 12, KPHB Phase 1, Hyderabad",
    "landmark": "Near Metro Gate 2",
    "phone": "+919876543212",
    "is_diagnostic": false,
    "display_order": 1
  }'
```
> 📌 **COPY BRANCH 1 UUID** e.g., `BRANCH_1_UUID`.

### Step 3: Add Branch 2 (Ameerpet Branch)
```bash
curl -X POST "https://medassist-ai.onrender.com/admin/branches?clinic_id=<CLINIC_UUID>" \
  -u "admin:Secure@9999" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Ameerpet Branch",
    "short_name": "AMP",
    "address": "Main Road, Ameerpet, Hyderabad",
    "landmark": "Opposite Big Bazaar",
    "phone": "+919876543213",
    "is_diagnostic": false,
    "display_order": 2
  }'
```
> 📌 **COPY BRANCH 2 UUID** e.g., `BRANCH_2_UUID`.

### Step 4: Add Doctor (Dr. Priya)
```bash
curl -X POST "https://medassist-ai.onrender.com/admin/doctors?clinic_id=<CLINIC_UUID>" \
  -u "admin:Secure@9999" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Dr. Priya Sharma",
    "specialization": "Gynecologist",
    "department": "Gynecology",
    "available_days": "Mon,Tue,Wed,Thu,Fri,Sat",
    "morning_slots": ["09:00","09:30","10:00","10:30","11:00"],
    "evening_slots": ["17:00","17:30","18:00","18:30"],
    "is_active": true,
    "consultation_fee": 600
  }'
```
> 📌 **COPY DOCTOR UUID** e.g., `DOCTOR_UUID`.

### Step 5: Assign Doctor to Branches by Session
Dr. Priya sits at **Kukatpally in the Morning** and **Ameerpet in the Evening**:

- Assign Morning Session to Kukatpally:
```bash
curl -X POST "https://medassist-ai.onrender.com/admin/branches/<BRANCH_1_UUID>/doctors?clinic_id=<CLINIC_UUID>" \
  -u "admin:Secure@9999" \
  -H "Content-Type: application/json" \
  -d '{"doctor_id": "<DOCTOR_UUID>", "session": "morning"}'
```

- Assign Evening Session to Ameerpet:
```bash
curl -X POST "https://medassist-ai.onrender.com/admin/branches/<BRANCH_2_UUID>/doctors?clinic_id=<CLINIC_UUID>" \
  -u "admin:Secure@9999" \
  -H "Content-Type: application/json" \
  -d '{"doctor_id": "<DOCTOR_UUID>", "session": "evening"}'
```

### Step 6: Test on WhatsApp
1. Send `Hi` on WhatsApp.
2. Bot shows **Interactive Branch List** (1. Kukatpally Branch, 2. Ameerpet Branch).
3. Pick Kukatpally → Only morning slots show for Dr. Priya.
4. Pick Ameerpet → Only evening slots show for Dr. Priya. Done! ✅

---

# 🏥 RECIPE 4: HOSPITAL / ENTERPRISE (`enterprise`)
> **Use case:** Full Multi-Branch Hospital Chain with Online Payments (Razorpay), MocDoc HMIS, and Staff Portal Logins (`clinic_admins`).  
> **Features:** Full Suite + Razorpay Online Payment + Staff Dashboard + Audit Logs.

### Step 1: Register Enterprise Hospital with Razorpay
```bash
curl -X POST https://medassist-ai.onrender.com/admin/clinics \
  -H "X-Admin-Secret: mediassist_admin_2026" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Sunshine Super Speciality Hospitals",
    "clinic_name": "Sunshine Hospital",
    "whatsapp_number": "+919876543214",
    "plan": "enterprise",
    "meta_phone_number_id": "<META_PHONE_NUMBER_ID>",
    "meta_access_token": "<META_PERMANENT_TOKEN>",
    "doctor_name": "Multi-Speciality Medical Board",
    "language": "en",
    "timezone": "Asia/Kolkata",
    "razorpay_key_id": "rzp_live_xxxxxxxx",
    "razorpay_key_secret": "secret_xxxxxxxx",
    "razorpay_webhook_secret": "whsec_xxxxxxxx"
  }'
```
> 📌 **COPY RETURNED `id` (CLINIC_UUID)**.

### Step 2: Add Hospital Branches & Doctors
Follow the exact same branch and doctor commands as shown in **Recipe 3 (Polyclinic)** above.

### Step 3: Create Staff Login Credentials for Clinic Admin Portal
To allow hospital receptionists/staff to log in to `https://medassist-ai.onrender.com/admin-panel` without seeing other hospitals' data:

Open **Supabase Dashboard → SQL Editor → New Query** and execute:
```sql
INSERT INTO clinic_admins (clinic_id, username, password_hash, role, is_active)
VALUES (
  '<CLINIC_UUID>',
  'sunshine_staff',
  'Sunshine@2026', -- Standard password hash or plain key handled by system auth
  'clinic_admin',
  true
);
```

### Step 4: Hand Over Access to Hospital Staff
Give staff:
- Dashboard URL: `https://medassist-ai.onrender.com/admin-panel`
- Username: `sunshine_staff`
- Password: `Sunshine@2026`

Staff can now view appointments, patient history, and lab report dispatches **strictly isolated to Sunshine Hospital**! Done! ✅

---

## 📋 QUICK SUMMARY COMPARISON TABLE FOR OPS STAFF

| Client Type | Plan Parameter | Needs Doctors Added? | Needs Branches Added? | Needs MocDoc Config? | Needs Razorpay Config? |
|---|---|---|---|---|---|
| **Solo Doctor** | `soloclinic` | Yes (1 Doctor) | No | Optional | Optional |
| **Diagnostic Lab** | `diagstream` | No | No | Yes (Recommended) | No |
| **Polyclinic Chain** | `polyclinic` | Yes (Multiple) | Yes (Multiple) | Optional | Optional |
| **Full Hospital** | `enterprise` | Yes (Multiple) | Yes (Multiple) | Yes | Yes |

---

## 🛠️ TROUBLESHOOTING & EMERGENCY CHECKS

1. **Bot not responding to WhatsApp messages:**
   - Verify Meta Webhook is set to `https://medassist-ai.onrender.com/webhook` with token `ck2006`.
   - Ensure `meta_phone_number_id` and `meta_access_token` match the clinic in Supabase `clinics` table.

2. **"Error: Could not store the uploaded report":**
   - Go to Supabase Dashboard → Storage → Create bucket named `lab-reports` (Private bucket).

3. **Wrong doctor times showing:**
   - Check `doctor_branches` mappings for morning vs evening session settings.
