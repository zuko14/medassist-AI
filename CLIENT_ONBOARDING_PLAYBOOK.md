# MediAssist AI — Pin-to-Pin Client Onboarding Playbook (Multi-Branch & Polyclinic Edition)

---

## THE MOST IMPORTANT THING TO UNDERSTAND FIRST

> **You have ONE server. ONE database. Unlimited clients on it.**
>
> Think of it like an apartment building:
> - **Render** = The building itself (one building, always running)
> - **Supabase** = The storage room shared by all apartments (one database)
> - **Each clinic / hospital / chain** = One apartment (all live in the same building, totally separate from each other)
>
> When you get a new client — whether a solo doctor, a multi-branch diagnostic centre, or a polyclinic chain — you do **NOT** create a new building. You just add a new apartment (one `clinics` row in Supabase, with optional `branches` rows underneath it). That's it!
>
> The bot knows which clinic/chain a patient is talking to by looking at **which WhatsApp number** the message arrived on:
> - Patient messages `+91AAAA` → bot knows it's **Clinic A (Solo Clinic)**.
> - Patient messages `+91BBBB` → bot knows it's **Hospital Chain B (Polyclinic with 3 branches)**. The bot automatically presents an interactive branch selection menu right inside the same WhatsApp chat!

---

## ONE-TIME SETUP (Do this ONCE. Never again.)

This is the setup of your core platform infrastructure. You do this before you have any clients.

---

### STEP 0.1 — Push your code to GitHub

1. Go to [github.com](https://github.com) → Sign up / Log in
2. Click the **+** button → **New repository**
3. Name it: `medassist-ai` → Set to **Private** → Click **Create repository**
4. Open your terminal in the `hospital-bot` project folder and run:
   ```bash
   git init
   git remote add origin https://github.com/YOUR_GITHUB_USERNAME/medassist-ai.git
   git add .
   git commit -m "Initial deployment with multi-branch support"
   git push -u origin main
   ```
5. Verify your code appears on GitHub. ✅

---

### STEP 0.2 — Set up Supabase (your database)

1. Go to [supabase.com](https://supabase.com) → Sign up / Log in
2. Click **New Project**
3. Fill in:
   - **Name:** `medassist-ai`
   - **Database Password:** Choose a strong password (save it somewhere safe)
   - **Region:** `South Asia (Mumbai)` — closest to India
4. Click **Create new project** and wait ~2 minutes for it to be ready
5. Once ready, go to **Project Settings** (gear icon) → **API**
6. Copy and save these two values:
   ```
   SUPABASE_URL              = https://xxxxxxxxxxxxxx.supabase.co
   SUPABASE_SERVICE_ROLE_KEY = eyJhbGci...  (the "service_role" key, NOT the anon key!)
   ```
7. **Now run all database schema migrations in order.** Go to **SQL Editor** (left sidebar) → **New Query**.
8. Open and run the following files one by one from your `migrations/` folder:
   - Run `migrations/001_initial_schema.sql` → Click **Run**
   - Run `migrations/002_lab_reports.sql` → Click **Run**
   - Run `migrations/002_security_tables.sql` → Click **Run**
   - Run `migrations/003_multi_tenant.sql` → Click **Run**
   - Run `migrations/007_data_retention.sql` → Click **Run**
   - Run `migrations/008_payments.sql` → Click **Run**
   - Run `migrations/009_integration_connectors.sql` → Click **Run**
   - **CRITICAL (Multi-Branch Schema):** Run `migrations/010_branches.sql` → Click **Run**
9. Go to **Table Editor** — verify you now see `clinics`, `branches`, `doctors`, `doctor_branches`, `appointments`, `lab_reports`, etc. ✅

---

### STEP 0.3 — Set up Groq (the AI brain)

1. Go to [console.groq.com](https://console.groq.com) → Sign up / Log in
2. Click **API Keys** → **Create API Key**
3. Name it `medassist` → Copy and save it:
   ```
   GROQ_API_KEY = gsk_xxxxxxxxxxxxxxxx
   ```

---

### STEP 0.4 — Deploy to Render (your server)

> This is your single cloud server. ALL your clients will run on this exact instance.

1. Go to [render.com](https://render.com) → Sign up / Log in with GitHub
2. Click **New +** → **Web Service**
3. Click **Connect a repository** → Select your `medassist-ai` GitHub repo
4. Fill in the settings exactly as follows:

   | Setting | Value |
   |---|---|
   | **Name** | `medassist-ai` |
   | **Region** | Singapore (closest to India) |
   | **Branch** | `main` |
   | **Runtime** | `Docker` |
   | **Build Command** | *(leave blank — Dockerfile handles it automatically)* |
   | **Start Command** | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
   | **Instance Type** | `Starter` ($7/month) to start |

5. Scroll down to **Environment Variables**. Add ALL of these one by one:

   | Variable Name | Value | Where to get it |
   |---|---|---|
   | `WHATSAPP_TOKEN` | `EAA...` | Meta Developer Portal (fallback/global token) |
   | `WHATSAPP_PHONE_NUMBER_ID` | *(leave blank for multi-tenant)* | Populated per clinic in DB |
   | `WHATSAPP_VERIFY_TOKEN` | `ck2006` | This exact value, or make your own |
   | `META_VERIFY_TOKEN` | `ck2006` | Same as above |
   | `META_APP_SECRET` | *(from Meta, see Phase 2)* | Meta App → Settings → Basic → App Secret |
   | `GROQ_API_KEY` | `gsk_...` | From Step 0.3 |
   | `GROQ_MODEL` | `llama-3.3-70b-versatile` | Type exactly this |
   | `SUPABASE_URL` | `https://xxx.supabase.co` | From Step 0.2 |
   | `SUPABASE_SERVICE_ROLE_KEY` | `eyJhbGci...` | From Step 0.2 |
   | `HOSPITAL_NAME` | `MediAssist AI Platform` | Your platform name |
   | `HOSPITAL_PHONE` | `+917981945956` | Your support number |
   | `HOSPITAL_ADDRESS` | `Visakhapatnam, AP` | Your address |
   | `ADMIN_USERNAME` | `admin` | Type exactly this |
   | `ADMIN_PASSWORD` | *(choose a strong one, e.g. `Secure@9999`)* | Make your own |
   | `ADMIN_SECRET` | `mediassist_admin_2026` | Your master API secret, keep private |
   | `INTEGRATION_SECRET` | `vam-chaitanya` | Used by MocDoc connector |
   | `CONNECTOR_ENCRYPTION_KEY` | `Chaitanya@123` | Used to encrypt HMIS passwords |
   | `MEDASSIST_URL` | `https://medassist-ai.onrender.com` | Your Render URL (from step 6 below) |
   | `APP_ENV` | `production` | Type exactly this |
   | `BOOKING_FEE_PAISE` | `50000` | = ₹500 booking fee (change as needed) |
   | `BOOKING_HOLD_MINUTES` | `10` | Slot held for 10 min during payment |
   | `REFUND_WINDOW_HOURS` | `4` | Refund window |
   | `CLINICAL_RETENTION_YEARS` | `7` | NMC compliance, type exactly this |
   | `CONVERSATION_PURGE_DAYS` | `30` | DPDP compliance, type exactly this |

6. Click **Create Web Service** and wait for the first deploy (~5 minutes)
7. Once done, you'll see a URL like: `https://medassist-ai.onrender.com`
8. **Copy this URL** — this is YOUR RENDER URL. Use it everywhere below.
9. Test it: open `https://medassist-ai.onrender.com/health` in your browser
   - You should see: `{"status": "ok", "service": "MediAssist AI", ...}` ✅

> [!IMPORTANT]
> Every time you update your code and push to GitHub (`git push`), Render will automatically redeploy. You never need to touch Render again for code changes!

---

### STEP 0.5 — Configure the Meta Webhook (ONCE)

1. Go to [developers.facebook.com](https://developers.facebook.com)
2. Open your App → **WhatsApp** → **Configuration**
3. Under **Webhook**, click **Edit**
4. Set:
   - **Callback URL:** `https://medassist-ai.onrender.com/webhook`
   - **Verify Token:** `ck2006`
5. Click **Verify and Save**
6. Under **Webhook Fields**, find `messages` → click **Subscribe**
7. You should see a green checkmark. ✅

---

> ## ✅ ONE-TIME SETUP IS COMPLETE
> Your server is live. Your database is ready. Meta knows where to send messages.
> From here on, every new client just adds rows to your database!

---
---

## FOR EVERY NEW CLIENT — Follow This Checklist

---

## PHASE 1 — Collect All Information from the Client

Do NOT start anything until you have ALL of this. Fill out this sheet:

```
CLIENT INFORMATION SHEET
========================
Business Name (legal):        _______________________
Short Display Name:           _______________________
WhatsApp Number to use:       +91 _____________________  (ONE number for the entire clinic/chain!)
Language:                     □ en (English)  □ hi (Hindi)  □ te (Telugu)
Timezone:                     Asia/Kolkata (default)

SELECT THE CLIENT PLAN:
-----------------------
□ soloclinic  (Single location clinic, appointments + triage + reminders)
□ diagstream  (Single location diagnostics center, lab report delivery only)
□ essential   (Single location clinic + diagnostics combined)
□ polyclinic  (Multiple physical branches under ONE WhatsApp number, appointments + lab reports)
□ enterprise  (Full custom hospital/chain setup with unlimited branches)

BRANCHES (Required if Plan = polyclinic or enterprise, Optional otherwise)
-------------------------------------------------------------------------
Branch 1:
  Branch Name:        _______________________  e.g. Kukatpally Main Clinic
  Short Ref Code:     ____ (Max 4 chars, e.g. KPL)
  Full Address:       _______________________
  Landmark:           _______________________
  Contact Phone:      _______________________
  Is Diagnostics Only? □ No (Booking + Lab)  □ Yes (Lab Reports Only, No Appointments)

Branch 2: (Add more blocks for extra branches)
  Branch Name:        _______________________  e.g. Ameerpet Diagnostics
  Short Ref Code:     ____ (e.g. AMP)
  Full Address:       _______________________
  Landmark:           _______________________
  Contact Phone:      _______________________
  Is Diagnostics Only? □ No (Booking + Lab)  □ Yes (Lab Reports Only)

PAYMENTS (if they want patients to pay online via bot)
------------------------------------------------------
Do they have a Razorpay account?  □ Yes  □ No
  Razorpay Key ID:          rzp_live_________________
  Razorpay Key Secret:      _______________________
  Razorpay Webhook Secret:  _______________________

MOCDOC / HMIS (if they use MocDoc for automated lab reports)
------------------------------------------------------------
Do they use MocDoc?   □ Yes  □ No
  MocDoc Username:    _______________________
  MocDoc Password:    _______________________
  MocDoc Clinic Slug: _______________________  (explained in Phase 7)

DOCTORS & BRANCH ASSIGNMENTS
----------------------------
Doctor 1:
  Full Name:          _______________________
  Specialization:     _______________________   e.g. General Physician
  Department:         _______________________   e.g. General Medicine
  Working Days:       _______________________   e.g. Mon,Tue,Wed,Thu,Fri,Sat
  Morning Slots:      _______________________   e.g. 09:00,09:30,10:00,10:30,11:00,11:30
  Evening Slots:      _______________________   e.g. 17:00,17:30,18:00,18:30
  Consultation Fee:   ₹ ___________________
  
  Branch Assignments (Which branches does this doctor sit at?):
  - Works at Branch Name: _________________ | Session: □ morning  □ evening  □ both
  - Works at Branch Name: _________________ | Session: □ morning  □ evening  □ both

Doctor 2: (copy the block above)
...
```

---

## PHASE 2 — Add the Single WhatsApp Number to Meta

> Even if the client has 5 branches, you ONLY register ONE phone number in Meta!

### Step 2.1 — Get a fresh SIM / phone number
- Buy or dedicate a single SIM card/number for the client's brand.
- Make sure this number is **NOT** active on personal WhatsApp or WhatsApp Business App right now.
- If it is active on a phone, delete that WhatsApp account from the phone app first.

### Step 2.2 — Add the number in Meta
1. Go to [developers.facebook.com](https://developers.facebook.com)
2. Open your App → **WhatsApp** → **API Setup**
3. Scroll to **Step 1: Select phone numbers** → Click **Add phone number**
4. Enter:
   - Display name: Client's brand name (e.g. `Apollo Multispeciality Hospitals`)
   - Category: `Healthcare`
   - Description: `Official AI Hospital Assistant`
5. Click **Next** → Enter the phone number → Choose **SMS** or **Voice call** to receive OTP
6. Enter the OTP → ✅ Number is now registered!

### Step 2.3 — Copy the Phone Number ID
1. Stay on the **API Setup** page.
2. In the phone number dropdown, select the number you just verified.
3. Copy the **Phone Number ID** below it (e.g., `971342239407011`).

### Step 2.4 — Generate a Permanent Access Token
> The token shown on the API Setup page expires in 24 hours. Do NOT use it!

1. Go to [business.facebook.com](https://business.facebook.com)
2. Left sidebar → **Settings** → **System Users**
3. Click **Add** → Create a new System User (`medassist_bot` / **Admin** role).
4. Click **Add Assets** → Select the WhatsApp Business Account → give **Full Control**.
5. Click **Generate New Token** on the system user.
6. Select your app → tick `whatsapp_business_messaging` and `whatsapp_business_management`.
7. Set **Token expiration** to `Never` → Click **Generate Token** → **Copy and save it!**

```
SAVE THIS:
Phone Number ID:    _______________________
Permanent Token:    _______________________
```

---

## PHASE 3 — Register the Clinic / Brand in Your System

Open your terminal and run this command. Replace `< >` with real data:

```bash
curl -X POST https://medassist-ai.onrender.com/admin/clinics \
  -H "X-Admin-Secret: mediassist_admin_2026" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "<Full Legal Name of Clinic/Chain>",
    "whatsapp_number": "<+91XXXXXXXXXX>",
    "plan": "<soloclinic|diagstream|essential|polyclinic|enterprise>",
    "meta_phone_number_id": "<Phone Number ID from Step 2.3>",
    "meta_access_token": "<Permanent Token from Step 2.4>",
    "meta_waba_id": "<WABA ID from WhatsApp Manager > Account tools>",
    "clinic_name": "<Short Display Name>",
    "doctor_name": "<Primary Doctor Name or Multiple Doctors>",
    "language": "en",
    "timezone": "Asia/Kolkata",
    "razorpay_key_id": "<rzp_live_xxx or omit>",
    "razorpay_key_secret": "<secret or omit>",
    "razorpay_webhook_secret": "<webhook_secret or omit>"
  }'
```

**You will get back a JSON response:**
```json
{
  "success": true,
  "clinic": {
    "id": "f13ea1b8-ec12-4d15-82a8-82668b74bd29",
    "name": "Apollo Multispeciality Hospitals"
  }
}
```

> [!IMPORTANT]
> The `id` in the response is the **CLINIC UUID**. Write it down! Every subsequent step requires this exact ID.

```
CLINIC UUID: _________________________________________
```

---

## PHASE 4 — Add Branches (Required for `polyclinic` & `enterprise` plans)

> If the client is a single-location `soloclinic` or `diagstream`, **skip this phase**.  
> If they have multiple locations, run this command **once for every physical branch/location**:

### Step 4.1 — Create each branch

```bash
curl -X POST "https://medassist-ai.onrender.com/admin/branches?clinic_id=<CLINIC_UUID>" \
  -u "admin:Secure@9999" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Kukatpally Main Clinic",
    "short_name": "KPL",
    "address": "Plot 45, KPHB Colony, Kukatpally, Hyderabad - 500072",
    "landmark": "Near Metro Station Exit 2",
    "phone": "+919876543210",
    "is_diagnostic": false,
    "display_order": 1
  }'
```

> **Important parameters explained:**
> - `is_diagnostic: false` → Normal clinic branch. Patients can book doctor appointments AND receive lab reports here.
> - `is_diagnostic: true` → Diagnostics-only branch. When a patient selects this branch on WhatsApp, the bot skips appointment booking and allows them to download lab reports or check test pricing!
> - `display_order` → Controls the order branches appear inside the interactive WhatsApp selection menu (1 = top of list).

### Step 4.2 — Record all Branch UUIDs
Run a check to list all created branches and **save their UUIDs**:

```bash
curl "https://medassist-ai.onrender.com/admin/branches?clinic_id=<CLINIC_UUID>" \
  -u "admin:Secure@9999"
```

Write down each branch ID and its name:
```
Branch 1 UUID (__________________): ____________________________________
Branch 2 UUID (__________________): ____________________________________
Branch 3 UUID (__________________): ____________________________________
```

---

## PHASE 5 — Add Doctors & Assign Them to Branches

> Skip entirely if the plan is `diagstream` (diagnostics only).

### Step 5.1 — Create each Doctor

Run this command for **each doctor** in the clinic or hospital chain:

```bash
curl -X POST "https://medassist-ai.onrender.com/admin/doctors?clinic_id=<CLINIC_UUID>" \
  -u "admin:Secure@9999" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Dr. Priya Sharma",
    "specialization": "General Physician",
    "department": "General Medicine",
    "available_days": "Mon,Tue,Wed,Thu,Fri,Sat",
    "morning_slots": ["09:00","09:30","10:00","10:30","11:00","11:30"],
    "evening_slots": ["17:00","17:30","18:00","18:30"],
    "is_active": true,
    "consultation_fee": 500
  }'
```

Write down the `id` of each doctor returned in the response:
```
Doctor 1 UUID (Dr. Priya Sharma): ____________________________________
Doctor 2 UUID (Dr. Arjun Reddy):  ____________________________________
```

### Step 5.2 — Map Doctors to Specific Branches (`polyclinic` / `enterprise` only)

If you have multiple branches, doctors must be assigned to their specific locations so the bot only lists available doctors when a patient picks a branch on WhatsApp!

Run this command to assign a doctor to a branch:

```bash
curl -X POST "https://medassist-ai.onrender.com/admin/branches/<BRANCH_UUID>/doctors?clinic_id=<CLINIC_UUID>" \
  -u "admin:Secure@9999" \
  -H "Content-Type: application/json" \
  -d '{
    "doctor_id": "<DOCTOR_UUID>",
    "session": "morning"
  }'
```

> **Session Options:**
> - `"morning"` → Doctor only takes morning appointments at this specific branch.
> - `"evening"` → Doctor only takes evening appointments at this specific branch.
> - `"both"` → Doctor takes all morning and evening appointments at this branch.

*Example Real-World Scenario:* Dr. Priya Sharma sits at **Kukatpally Branch** in the morning (`session: "morning"`) and visits the **Ameerpet Branch** in the evening (`session: "evening"`). You simply make two separate assignment requests to the respective `BRANCH_UUID`s!

---

## PHASE 6 — Add Hospital Holidays (Optional)

If the clinic/chain has fixed holidays where no bookings should occur:

```bash
curl -X POST "https://medassist-ai.onrender.com/admin/holidays?clinic_id=<CLINIC_UUID>&holiday_date=2026-10-20&name=Diwali" \
  -u "admin:Secure@9999"
```

---

## PHASE 7 — Enable MocDoc Connector (If client uses MocDoc HMIS)

> Skip if the client does NOT use MocDoc for automated lab report fetching.

### Step 7.1 — Find the MocDoc clinic slug
When the client logs into MocDoc and opens reports, the URL looks like:
```
https://mocdoc.com/investigation/listbydate/order/visakha-multispeciality-clinics
```
The slug here is: `visakha-multispeciality-clinics`

### Step 7.2 — Encrypt the MocDoc password
Open your terminal and run:
```bash
python -m connectors.runner --encrypt-password
```
Enter the client's MocDoc password and press Enter. Copy the encrypted string (`gAAAAAB...`).

### Step 7.3 — Add the connector to Supabase
1. Go to **Supabase Dashboard** → **Table Editor** → `integration_connectors` table.
2. Click **Insert** → **Insert Row**.
3. Fill in:
   - `clinic_id`: `<CLINIC_UUID>`
   - `connector_type`: `mocdoc`
   - `is_enabled`: `true`
   - `config`: Paste the JSON below:
     ```json
     {
       "base_url": "https://mocdoc.com",
       "username": "clinic_mocdoc_email@example.com",
       "password": "gAAAAABh3X2KpLm...",
       "clinic_slug": "visakha-multispeciality-clinics"
     }
     ```
4. Click **Save**.

### Step 7.4 — Test the connector (Dry Run)
```bash
python -m connectors.runner --connector mocdoc --clinic-id <CLINIC_UUID> --once --dry-run
```
Look for `DRY RUN RESULTS: X reports found` ✅.

---

## PHASE 8 — End-to-End Testing

### Test 8.1 — Verify clinic registration
```bash
curl "https://medassist-ai.onrender.com/admin/clinics" -H "X-Admin-Secret: mediassist_admin_2026"
```
Should return the client's clinic/brand. ✅

### Test 8.2 — Send a test WhatsApp push message
```bash
curl -X POST "https://medassist-ai.onrender.com/admin/clinics/<CLINIC_UUID>/test?to=+91XXXXXXXXXX" \
  -H "X-Admin-Secret: mediassist_admin_2026"
```
Replace `+91XXXXXXXXXX` with YOUR phone number. You should receive: *"✅ Test message from [Brand]. Your MediAssist AI is live!"* ✅

### Test 8.3 — Test the interactive Multi-Branch WhatsApp flow
1. Save the client's new WhatsApp number on your phone.
2. Send: `Hi` or `Book appointment`
3. **If Plan = `polyclinic` or `enterprise`:**
   - The bot immediately presents an **Interactive Branch Selection List** showing your branches (e.g., *1. Kukatpally Branch, 2. Ameerpet Diagnostics*).
   - Select a branch. The bot confirms the location (*"📍 Selected: Kukatpally Branch"*).
   - Proceed to book → The bot **only shows doctors and slots assigned to that specific branch/session**! ✅
4. Verify that appointment summaries and reminders explicitly state the exact branch address and phone number! ✅

---

## PHASE 9 — Billing & Client Handover

### Step 9.1 — Invoice the Client

| Plan | Recommended Pricing | Key Capabilities Included |
|---|---|---|
| `soloclinic` | ₹2,999/month | 1 Location, Appointments + Reminders + Triage |
| `diagstream` | ₹3,999/month | 1 Location, Automated Lab Report Delivery |
| `essential` | ₹6,999/month | 1 Location, Appointments + Lab Reports + Reminders |
| `polyclinic` | ₹9,999/month | **Single WhatsApp Number**, Multiple Branches, Branch-Specific Doctor Scheduling |
| `enterprise` | ₹15,000+/month | Custom Hospital Chains, HMIS Integration, Dedicated SLA |

**If a client fails to pay or cancels:**
```bash
# Soft-deactivate clinic (preserves data, stops bot responses)
curl -X DELETE "https://medassist-ai.onrender.com/admin/clinics/<CLINIC_UUID>" \
  -H "X-Admin-Secret: mediassist_admin_2026"
```

---

### Step 9.2 — Give the Client Handover Document

Provide this complete summary sheet to the client on launch day:

```
============================================================
  MediAssist AI — YOUR OFFICIAL SETUP IS LIVE
============================================================

Your Single WhatsApp Bot Number:  +91XXXXXXXXXX
Plan Type:                        polyclinic / enterprise / essential
Live Date:                        DD/MM/YYYY

Admin & Management Dashboard:
  URL:       https://medassist-ai.onrender.com/admin-panel
  Username:  admin
  Password:  Secure@9999

How It Works for Your Patients:
  1. Patients save +91XXXXXXXXXX or click your WhatsApp QR/Link.
  2. They say "Hi" → The bot instantly asks them to select their preferred Branch!
  3. Based on their branch choice, the bot schedules appointments with available
     doctors or delivers branch-specific lab reports seamlessly.

Your Active Branches:
  📍 Kukatpally Main Clinic (Booking + Lab Reports)
  📍 Ameerpet Diagnostics Center (Automated Lab Reports Only)

Support & Escalations:
  Platform Support: +917981945956
  Email: support@mediassist-ai.com

============================================================
```

---

## FINAL GO-LIVE CHECKLIST

Print this and tick every box before telling the client they are live:

```
ONE-TIME INFRASTRUCTURE SETUP (done once, skip after first time)
  □ Code pushed to GitHub (`medassist-ai` repo)
  □ Supabase project created and ALL migrations executed (001 through 010_branches.sql)
  □ Render web service running and `/health` returns status ok
  □ Meta Webhook verified (`/webhook` with token `ck2006`)

FOR EVERY NEW CLIENT ONBOARDING
  □ Phase 1  — All client info, branch details, and doctor schedules collected
  □ Phase 2  — Single WhatsApp Number verified in Meta; Phone Number ID & Token saved
  □ Phase 3  — Clinic/Chain registered via API (`POST /admin/clinics`); UUID saved
  □ Phase 4  — All physical branches created via API (`POST /admin/branches`)
  □ Phase 5  — Doctors created (`POST /admin/doctors`) & mapped to branch sessions
  □ Phase 6  — Hospital holidays added (if applicable)
  □ Phase 7  — MocDoc connector configured and dry-run verified (if applicable)
  □ Phase 8.1 — Clinic verified in API list (`GET /admin/clinics`)
  □ Phase 8.2 — Test push message sent to your mobile from clinic number
  □ Phase 8.3 — Interactive branch selection menu & doctor filtering tested on WhatsApp
  □ Phase 9.1 — Payment received and invoiced
  □ Phase 9.2 — Official Handover Document delivered to client

DONE ✅ — Client is LIVE on their single multi-branch WhatsApp number!
```

---

> [!CAUTION]
> **NEVER share these critical system secrets with any client:**
> - `ADMIN_SECRET` (`mediassist_admin_2026`)
> - `INTEGRATION_SECRET` / `CONNECTOR_ENCRYPTION_KEY`
> - `SUPABASE_SERVICE_ROLE_KEY`
>
> These are YOUR platform master keys. The client only receives the Admin Dashboard credentials!
