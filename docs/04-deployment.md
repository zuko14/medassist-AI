# Phase 11-15: Deployment, Security, Testing, Meta Verification, File Checklist


## 🐳 Phase 11 — Deployment

### 11.1 `Dockerfile`

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 11.2 `railway.toml`

```toml
[build]
builder = "dockerfile"

[deploy]
startCommand = "uvicorn app.main:app --host 0.0.0.0 --port $PORT"
healthcheckPath = "/health"
healthcheckTimeout = 30
restartPolicyType = "on_failure"
```

### 11.3 `app/routers/health.py`

```python
@router.get("/health")
async def health():
    return {"status": "ok", "service": "MediAssist AI", "env": settings.app_env}
```

Set up UptimeRobot on `/health` endpoint every 5 minutes.

---


---


## 🔒 Phase 12 — Security

1. **Webhook Signature Verification**: Verify `X-Hub-Signature-256` header on every POST to `/webhook`. Reject requests with invalid signatures with `403`. Use `hmac.compare_digest` (not `==`).

2. **Rate Limiting**: Implement per-phone rate limiting — max 20 messages per minute per phone. Use in-memory dict or Redis. Prevents flooding.

3. **Input Sanitization**: Strip all HTML/script tags from incoming messages before passing to AI. Max message length: 4096 chars.

4. **Admin API Key**: Generate a 32-char random key for admin routes. Store in `.env` as `ADMIN_API_KEY`. Check `X-Admin-Key` header.

5. **Supabase Row Level Security**: Enable RLS on all tables. The backend uses the service role key (bypasses RLS) but configure RLS policies so direct DB access is restricted.

6. **PII Masking in Logs**: Phone numbers in logs must be masked: `+91XXXXXX7890`. Patient names truncated to first name only in logs.

7. **No Sensitive Data in Error Responses**: All exception handlers return generic `{"error": "Something went wrong"}` — never expose stack traces or DB errors to webhook responses.

---


## ✅ Phase 13 — Testing

### 13.1 Test Cases to Implement

**`tests/test_webhook.py`**:
- Valid Meta webhook verification
- Invalid verify token returns 403
- Valid signature accepted
- Invalid signature rejected with 403
- Opt-out keyword triggers opt-out flow
- Emergency keyword triggers emergency flow
- Duplicate `message_id` → second call returns 200 but no message sent
- Mid-booking session expired → state reset to main_menu
- Patient types "book appointment" mid-booking → "continue or start over?" prompt

**`tests/test_ai_engine.py`**:
- "chest pain" → Cardiology intent, `is_emergency=True`
- "book appointment" → `book_appointment` intent
- "stop" → `opt_out` intent
- "delete my data" → `data_deletion_request` intent
- Language detection: Hindi text → `hi`

**`tests/test_appointment.py`**:
- Create appointment → returns valid UUID
- Duplicate slot → returns `{"success": False, "reason": "slot_taken"}`
- Simultaneous insert of same slot → only first succeeds, second gets `slot_taken` (test with asyncio.gather)
- Cancel appointment → status updates to `cancelled`
- Reschedule → old slot freed, new slot created
- `get_available_slots()` excludes confirmed bookings
- `get_available_slots()` returns `[]` for doctor's day off
- `get_available_slots()` returns `[]` when all slots are full
- `find_available_doctor_in_dept()` excludes the fully-booked doctor
- `find_available_doctor_in_dept()` returns `[]` when entire department is full
- Past date input → rejected with "choose a future date"
- Date with zero slots → next available date shown instead

Run with: `pytest tests/ -v --asyncio-mode=auto`

---


---


## 📋 Phase 14 — Meta Business Verification Checklist

> Complete this checklist before going live. Hospital rejection happens when these are skipped.

**Business Manager Setup**:
- [ ] Create Meta Business Manager at business.facebook.com
- [ ] Verify business with GST certificate or incorporation docs
- [ ] Upload hospital logo and fill business description
- [ ] Set business category: "Healthcare" → "Hospital or Clinic"
- [ ] Add website URL (must be live and mention hospital name)
- [ ] Business display name must match hospital's registered name

**WhatsApp Business Account**:
- [ ] Create WABA under the Business Manager
- [ ] Add the hospital phone number (must be able to receive OTP)
- [ ] Set display name = hospital name (pending Meta approval ~48h)
- [ ] Upload profile photo = hospital logo
- [ ] Fill About: "Official WhatsApp assistant for [Hospital Name]. For emergencies call 108."

**Message Templates** (submit each from `whatsapp_templates.py`):
- [ ] `appointment_confirmation` — UTILITY category
- [ ] `appointment_reminder_24h` — UTILITY category
- [ ] `appointment_reminder_2h` — UTILITY category
- [ ] `post_appointment_followup` — UTILITY category
- [ ] `opt_out_confirmation` — UTILITY category
- [ ] `data_deletion_confirmation` — UTILITY category
- [ ] `emergency_response_v2` — UTILITY category
- [ ] All templates in English submitted first (approval faster)
- [ ] Hindi/Telugu variants submitted after English approved

**App Review (if using Cloud API directly)**:
- [ ] Create Facebook App at developers.facebook.com
- [ ] Add WhatsApp product
- [ ] Enable `whatsapp_business_messaging` permission
- [ ] Submit for App Review with use case: "Hospital appointment scheduling and patient reminders"
- [ ] Privacy Policy URL must mention: WhatsApp data usage, opt-out instructions, data retention

**Compliance Documents for Review Submission**:
- [ ] Privacy Policy on hospital website mentioning WhatsApp
- [ ] Terms of Service mentioning AI assistant usage
- [ ] Screenshot of opt-in flow (user messages first)
- [ ] Screenshot of opt-out flow (STOP keyword handling)
- [ ] Loom video demo of the complete appointment booking flow

---


---


## 📁 Phase 15 — Final File Checklist

Before submitting to Claude Code, ensure these files exist:

```
✅ app/main.py
✅ app/config.py
✅ app/database.py
✅ app/routers/webhook.py
✅ app/routers/health.py
✅ app/routers/admin.py
✅ app/services/whatsapp.py
✅ app/services/ai_engine.py
✅ app/services/conversation.py
✅ app/services/appointment.py
✅ app/services/scheduler.py
✅ app/services/consent.py
✅ app/services/analytics.py
✅ app/services/faq_engine.py
✅ app/services/feedback.py
✅ app/routers/admin_ui.py
✅ app/admin_templates/base.html
✅ app/admin_templates/dashboard.html
✅ app/admin_templates/appointments.html
✅ app/admin_templates/doctors.html
✅ app/admin_templates/leaves.html
✅ app/admin_templates/holidays.html
✅ app/admin_templates/analytics.html
✅ app/admin_templates/callbacks.html
✅ onboarding/import_doctors.py
✅ onboarding/import_holidays.py
✅ app/models/patient.py
✅ app/models/appointment.py
✅ app/models/conversation.py
✅ app/models/message.py
✅ app/templates/whatsapp_templates.py
✅ app/utils/logger.py
✅ app/utils/validators.py
✅ app/utils/helpers.py
✅ migrations/001_initial_schema.sql
✅ tests/test_webhook.py
✅ tests/test_ai_engine.py
✅ tests/test_appointment.py
✅ requirements.txt
✅ Dockerfile
✅ railway.toml
✅ .env.example
✅ .gitignore
✅ README.md
```

---


---


## 🚀 Build Order for Claude Code

Execute phases in this exact order:

1. **Phase 0** — Read and internalize all anti-rejection rules (no code yet)
2. **Phase 16.1–16.2** — Read superiority features before writing any conversation logic
3. **Phase 1** — Project setup, config, requirements
4. **Phase 2** — Database schema (run SQL in Supabase)
5. **Phase 3** — AI engine with Groq fallback (ai_engine.py)
6. **Phase 5** — WhatsApp service + templates
7. **Phase 4** — Conversation state machine with critical guards (depends on 3+5)
8. **Phase 16.3–16.8** — Wire smart entry, memory, proactive slots, post-booking actions
9. **Phase 16.9–16.15** — Error messages, hours awareness, booking ref, reschedule, escalation, my appointments, tone
10. **Phase 7** — Webhook handler (depends on 4)
11. **Phase 6** — Scheduler + leave-aware cancel job (depends on 5)
12. **Phase 8** — Analytics + admin API
13. **Phase 9** — Services module
14. **Phase 10** — Multilingual strings (all 3 languages for all new messages)
15. **Phase 11** — Deployment config
16. **Phase 12** — Security hardening
17. **Phase 13** — Tests (all updated test cases)
18. **Phase 14** — Meta verification checklist (human task)

---

*MediAssist AI — Built to get approved, not rejected.*