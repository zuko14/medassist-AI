# Kriya AI — Client Onboarding Standard Operating Procedure (SOP)
**Document Version:** 2.0 (August 2026 Production Standard)  
**Provider:** Zuko Labs (Meta Tech Provider)  
**Platform:** Kriya AI Multi-Tenant WhatsApp Healthcare Automation

---

## Executive Summary & Architecture
In the Meta Cloud API ecosystem, hospitals maintain **100% legal ownership of their WhatsApp Business Account (WABA) and phone number** to guarantee instant Display Name approval (e.g., `"Accumax Diagnostics"`) without third-party naming rejections. **Zuko Labs** acts as the authorized **Technology Provider**, managing AI conversational flows, slot scheduling, payments, and lab report delivery behind the scenes.

```
┌─────────────────────────────────────────────────────────┐
│                 CLIENT (Hospital / Clinic)              │
│  1. Owns Meta Business Portfolio & WhatsApp Number      │
│  2. Verifies Phone Number with OTP                      │
│  3. Shares WABA Asset with Zuko Labs Partner ID         │
└────────────────────────────┬────────────────────────────┘
                             │ (Meta Partner Sharing)
                             ▼
┌─────────────────────────────────────────────────────────┐
│                 TECH PROVIDER (Zuko Labs)               │
│  4. Assigns WABA to System User (Full Access)           │
│  5. Calls Cloud API /register to activate certificates  │
│  6. Subscribes Global Webhook (messages)                │
│  7. Registers Tenant in Kriya AI Platform Panel         │
└─────────────────────────────────────────────────────────┘
```

---

## Part A: Client-Side Steps (Hospital Admin — 5 Minutes)

### Step 1: Access Meta Business Portfolio
1. The client opens **[business.facebook.com/settings](https://business.facebook.com/settings)**.
2. Ensure their Business Portfolio is selected (e.g., *Accumax Diagnostics*).

### Step 2: Add WhatsApp Phone Number & Display Name
1. In the left sidebar under **Accounts**, click **WhatsApp accounts**.
2. Click **Add** (or select existing WABA) $\rightarrow$ Open **WhatsApp Manager**.
3. In WhatsApp Manager, click the **Phone numbers (📞)** tab on the left $\rightarrow$ Click **Add phone number**.
4. Enter:
   - **Display Name:** Clean hospital/clinic name (e.g., `Accumax Diagnostics`).
   - **Category:** `Medical & Health` or `Hospital/Clinic`.
   - **Phone Number:** Mobile or landline number with country code `+91`.

### Step 3: Complete Phone Number OTP Verification
1. Click **Next** $\rightarrow$ Choose **Text message (SMS)** or **Voice call**.
2. Client receives a 6-digit OTP on the registered phone $\rightarrow$ Enters it on the screen.
3. The red *“Phone number verification required”* banner will disappear.

### Step 4: Share Asset with Zuko Labs (Tech Provider)
1. In the client's Business Settings left menu, click **Users** $\rightarrow$ **Partners**.
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

### Step 5: Assign Shared Asset to System User
1. Open Zuko Labs Business Settings: **[business.facebook.com/settings?business_id=1602916427428175](https://business.facebook.com/settings?business_id=1602916427428175)**.
2. In the left sidebar, click **Users** $\rightarrow$ **System users**.
3. Click on the System User (e.g., `Employee`).
4. Click **Assign assets**:
   - Left column: **WhatsApp accounts**.
   - Middle column: Check the client's account (e.g., `Accumax Diagnostics`).
   - Right column: Toggle **Everything (Full control)** $\rightarrow$ **ON**.
   - Click **Save Changes**.

### Step 6: Extract the 3 Core Identifiers
In Zuko Labs Business Settings $\rightarrow$ **Accounts** $\rightarrow$ **WhatsApp accounts** $\rightarrow$ Client's Account:
1. **WABA ID:** Displayed at the top (e.g., `1702889104159864`).
2. Click **Phone numbers** tab:
   - **Phone Number ID:** Copy the 15-16 digit ID (e.g., `1296654790197336`).
   - **Display Phone Number:** Copy E.164 number (e.g., `+919281235959`).

### Step 7: Cloud API Activation (`/register` — Automated in Kriya AI)
> **Automatic Activation:** When you register the clinic in the Kriya AI Platform Panel (Step 8 below), Kriya AI automatically calls Meta's `/register` API in the background using the provided token and phone number ID. The status will flip from `Pending` to **`Connected`** automatically.

**Manual Command Fallback (if needed before platform creation):**
If you ever want to activate the number before creating the clinic in the panel:
```bash
curl -X POST "https://graph.facebook.com/v21.0/<PHONE_NUMBER_ID>/register" \
  -H "Authorization: Bearer <META_SYSTEM_USER_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"messaging_product": "whatsapp", "pin": "123456"}'
```
**Expected Response:** `{"success": true}` (Status flips to `Connected (Green)` immediately).

### Step 8: Verify Global Webhook Subscription
1. Open Meta Developer Dashboard: **[developers.facebook.com/apps/946290901317238](https://developers.facebook.com/apps/946290901317238)**.
2. Go to **Use Cases** $\rightarrow$ **Connect on WhatsApp** $\rightarrow$ **Step 2. Production setup** (or **WhatsApp $\rightarrow$ Configuration**).
3. Confirm that **`messages`** is toggled to **Subscribed (Blue ON)**.

### Step 9: Register Tenant in Kriya AI Platform Panel
1. Open Kriya AI Platform Panel: `https://medassist-ai-docker.onrender.com/platform-panel`
2. Click **Create Hospital / Clinic**:
   - **Hospital / Clinic Name:** `Accumax Diagnostics`
   - **WhatsApp Number (E.164):** `+919281235959`
   - **Plan:** `Diagnostic center` or `Polyclinic`
   - **Meta Phone Number ID:** `1296654790197336`
   - **Meta Permanent Access Token:** `EAAG...` *(System User Token)*
3. Click **Create Hospital / Clinic**.

*(Kriya AI backend immediately persists the tenant and executes the Meta `/register` activation in the background).*

---

## Part C: Live End-to-End Verification (1 Minute)

1. **Inbound WhatsApp Test:**
   - From any personal phone, send `"Hi"` to `+919281235959`.
   - **Verification:** Kriya AI responds within 1.5 seconds with the clinic's personalized interactive menu.
2. **Server Log Verification:**
   - In Render logs, confirm tenant resolution:
     ```json
     {"level": "INFO", "message": "[Accumax Diagnostics] Resolved tenant via phone_number_id '1296654790197336'"}
     ```
3. **Interactive Booking / Lab Test Flow:**
   - Test selecting a test/doctor $\rightarrow$ Date Picker $\rightarrow$ Slot List $\rightarrow$ Token generation.

---

## Part D: Troubleshooting & Edge Cases

| Issue / Error | Root Cause | Exact Fix |
|---|---|---|
| **Display Name Rejected / Stuck** | Number added directly under Zuko Labs instead of Client WABA. | Client must create WABA in their own portfolio with their brand name, then share asset to Zuko Labs (`1602916427428175`). |
| **Status Stuck on `Pending`** | Cloud API cryptographic certificate uninitialized. | Submitting the clinic in Platform Panel auto-activates it, or execute Step 7 manual curl (`POST /{PHONE_NUMBER_ID}/register`). |
| **`Account does not exist in Cloud API`** | 2-step verification attempted before `/register` call. | Submitting the clinic in Platform Panel automatically creates and registers the Cloud API account. |
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
```
