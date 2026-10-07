# Session 31 — PhonePe Payment Gateway Integration (Dual Gateway Support)

**Date:** 2026-10-07  
**Branch:** `main` (direct)  
**Scope:** PhonePe PG v2 Standard Checkout, dual gateway (Razorpay | PhonePe), admin UI, webhook router  
**Migration:** `100_appointments_payment_gateway.sql` — adds `payment_gateway` and `gateway_order_id` columns to `appointments`

---

## 1. Summary

Kriya AI now supports **two payment gateways**: Razorpay (existing) and PhonePe (new). A clinic's admin can switch between gateways at any time. Each booking records which gateway it was charged through, so switching gateways never strands in-flight payments or sends refunds to the wrong account.

### Key Design Principle

> **The booking owns its gateway, not the clinic.** `clinics.config.payment_gateway` picks the gateway for NEW bookings. `appointments.payment_gateway` records which gateway actually charged that specific booking. Confirmation, polling, expiry recovery, and refunds always follow the booking's gateway.

---

## 2. Migration 100

**File:** [`migrations/100_appointments_payment_gateway.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/100_appointments_payment_gateway.sql)  
**Rollback:** [`migrations/rollback/100_down.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/rollback/100_down.sql)

```sql
ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS payment_gateway TEXT
        CONSTRAINT chk_appointments_payment_gateway
        CHECK (payment_gateway IS NULL OR payment_gateway IN ('razorpay', 'phonepe'));

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS gateway_order_id TEXT;
```

- `payment_gateway`: `NULL` = Razorpay (every row before this migration). `'phonepe'` for PhonePe bookings.
- `gateway_order_id`: PhonePe's `orderId`, written once the order exists. Analogous to `razorpay_payment_link_id`.
- Additive, nullable, no backfill required.

### Checksum registration (run in Supabase SQL Editor)

```sql
INSERT INTO schema_migrations (name, checksum) VALUES
    ('100_appointments_payment_gateway.sql', 'f35ec78e19e68c466f3c031fe082e5926678eb2369a1239b19bd59af12246bb1')
ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum;
```

---

## 3. Files Changed

### 3.1 Backend — New Files

| File | Lines | What It Does |
|---|---|---|
| [`app/routers/phonepe_webhook.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/phonepe_webhook.py) | 52 | **NEW.** PhonePe webhook receiver. One URL per clinic (`POST /webhooks/phonepe/{clinic_id}`). Authenticated by the clinic's own `webhook_username:webhook_password`. Rate-limited to prevent bad-auth flood. |
| [`migrations/100_appointments_payment_gateway.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/100_appointments_payment_gateway.sql) | 26 | **NEW.** Schema migration adding dual-gateway columns to `appointments`. |
| [`migrations/rollback/100_down.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/rollback/100_down.sql) | — | **NEW.** Rollback script dropping the two new columns. |
| [`tests/test_phonepe_payment.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_phonepe_payment.py) | — | **NEW.** Tests for PhonePe OAuth token flow, order creation, webhook authentication, and payment confirmation. |

### 3.2 Backend — Modified Files

| File | Lines Changed | What Changed |
|---|---|---|
| [`app/services/payment.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/payment.py) | +699 −117 | **Major expansion.** Added complete PhonePe PG v2 Standard Checkout integration: OAuth client credentials flow (`_phonepe_tokens` cache), order creation via PhonePe API, webhook processing with SHA256(username:password) authentication, order status polling, refund via PhonePe. Added helper functions: `active_gateway()`, `booking_gateway()`, `get_phonepe_creds()`, `gateway_configured()`. `PHONEPE_REQUIRED` tuple defines all required credential fields. `PHONEPE_HOSTS` maps sandbox/production environments. `resolve_payment_mode()` now checks the active gateway's credentials. Every confirmation/refund/poll now dispatches to Razorpay or PhonePe based on `booking_gateway()`. |
| [`app/routers/admin.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/admin.py) | +64 −6 | `PaymentSettingsUpdate` model now accepts PhonePe credentials and gateway selection. `get_payment_settings` returns both Razorpay and PhonePe credential status (masked secrets). `update_payment_settings` validates that switching to a gateway requires all its credentials to be present first (prevents silent payment breakage). Audit log includes gateway info. |
| [`app/services/conversation.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/conversation.py) | +6 −5 | Payment link WhatsApp messages now show the correct gateway name ("Razorpay" or "PhonePe") dynamically via `result.gateway_name`. Error handling accepts `gateway_error` alongside `razorpay_error`. |
| [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/main.py) | +3 −0 | Mounts the new `phonepe_webhook.router`. |
| [`app/services/scheduler.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/scheduler.py) | +1 −1 | Payment expiry/poll sweep uses `booking_gateway()` to choose the correct gateway for each expired hold. |
| [`app/services/tenant.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/tenant.py) | +1 −1 | Minor: aligned with new credential helpers. |
| [`app/templates/whatsapp_templates.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/templates/whatsapp_templates.py) | +4 −2 | Payment link template now includes gateway name parameter. |
| [`tests/test_tenant_omission_hardening.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_tenant_omission_hardening.py) | +2 −2 | Updated to account for new PhonePe credential fields in `clinics.config`. |

### 3.3 Frontend — Admin Panel

| File | Lines Changed | What Changed |
|---|---|---|
| [`admin/index.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/index.html) | +108 −5 | **Payments settings tab** now shows a gateway selector (Razorpay / PhonePe). PhonePe credential fields: Client ID, Client Secret, Client Version, Webhook Username, Webhook Password, Environment (Production / Sandbox). Gateway switch validation: refuses to switch if credentials are missing. Webhook URL displayed for easy copy to PhonePe dashboard. |

### 3.4 Documentation

| File | What Changed |
|---|---|
| [`docs/agent-context/02-SYSTEM-FLOWS.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/02-SYSTEM-FLOWS.md) | Added PhonePe payment flow trace alongside existing Razorpay flow. |
| [`docs/agent-context/03-DATABASE-MODEL.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/03-DATABASE-MODEL.md) | Documented `payment_gateway` and `gateway_order_id` columns on `appointments`. |
| [`docs/agent-context/04-API-MAP.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/04-API-MAP.md) | Added `POST /webhooks/phonepe/{clinic_id}` endpoint. |
| [`docs/agent-context/05-FRONTEND-MAP.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/05-FRONTEND-MAP.md) | Updated payment settings tab description. |
| [`docs/agent-context/07-INTEGRATIONS.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/07-INTEGRATIONS.md) | Added PhonePe PG v2 integration section. |
| [`docs/agent-context/09-BACKGROUND-JOBS.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/09-BACKGROUND-JOBS.md) | Updated payment sweep job to mention dual-gateway dispatch. |
| [`docs/CLIENT_ONBOARDING_SOP.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/CLIENT_ONBOARDING_SOP.md) | Added PhonePe onboarding steps for new clinics. |

---

## 4. PhonePe Integration Architecture

### 4.1 Credential Storage

PhonePe credentials are stored in `clinics.config` (JSONB), same pattern as Razorpay:

| Key | Purpose |
|---|---|
| `phonepe_client_id` | OAuth client ID |
| `phonepe_client_secret` | OAuth client secret |
| `phonepe_client_version` | API version (e.g. "1") |
| `phonepe_webhook_username` | Webhook Basic Auth username |
| `phonepe_webhook_password` | Webhook Basic Auth password |
| `phonepe_env` | `"production"` or `"sandbox"` |
| `payment_gateway` | `"razorpay"` (default/NULL) or `"phonepe"` |

### 4.2 Payment Flow (PhonePe)

```
Patient confirms booking
  → PaymentService.create_payment_hold()
  → active_gateway(clinic) == "phonepe"
  → PhonePe OAuth token (cached per credential set)
  → PhonePe Create Order API (merchantOrderId = appointments.id)
  → appointments.payment_gateway = "phonepe", gateway_order_id = orderId
  → Payment link sent to patient via WhatsApp

Patient pays
  → PhonePe webhook → POST /webhooks/phonepe/{clinic_id}
  → SHA256(username:password) authentication
  → Re-read order from PhonePe Order Status API (webhook is only a hint)
  → Confirm booking if PAID

Expiry/poll sweep
  → booking_gateway(appointment) == "phonepe"
  → PhonePe Order Status API check
  → Confirm if paid, expire if not
```

### 4.3 Security Model

- **Webhook is only a hint**: The PhonePe webhook triggers a re-read from PhonePe's Order Status API. A leaked webhook header can never confirm an unpaid booking.
- **Per-clinic webhook URL**: `/webhooks/phonepe/{clinic_id}` — each clinic authenticates with its own credentials.
- **No global fallback**: Like Razorpay, a clinic must have its own PhonePe credentials. No platform-level credentials are used.
- **Gateway switch validation**: Admin panel refuses to switch to PhonePe without all 5 required credentials.

---

## 5. How to Test

1. Set a clinic to **PhonePe sandbox** in the admin panel (Payments tab → select PhonePe → enter sandbox credentials → set environment to Sandbox).
2. Book an appointment via WhatsApp — the payment link should be a PhonePe checkout URL.
3. Complete the sandbox payment — webhook should fire and confirm the booking.
4. Switch back to Razorpay — the PhonePe booking should still show its gateway correctly.

---

## 6. No New Environment Variables Required

All PhonePe configuration is per-clinic in `clinics.config`. No platform-level env vars needed.
