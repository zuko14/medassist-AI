# MediAssist AI — Hospital WhatsApp Assistant
## CLAUDE.md — Master Index for Claude Code

> Stack: Python FastAPI · Meta WhatsApp Cloud API · Supabase PostgreSQL · Groq AI (Llama 3.3-70b) · APScheduler · Render.com
> Anti-Rejection Compliance: Meta Business Policy · India DPDP Act · Hospital trust requirements built-in at every layer

---

## How to Build

Read each doc file in order and implement it fully before moving to the next.
Do NOT skip any phase. Do NOT start the next doc until the current one is complete and tested.

---

## Build Order

1. `docs/01-setup.md`      → Project structure, anti-rejection rules (read FIRST), setup, full DB schema
2. `docs/02-core.md`       → AI engine with Groq fallback, conversation state machine, WhatsApp service
3. `docs/03-features.md`   → Scheduler, webhook handler, analytics API, services module, multilingual
4. `docs/04-deployment.md` → Dockerfile, security hardening, tests, Meta verification checklist, file checklist
5. `docs/05-superior.md`   → Superior features: FAQ engine, smart greeting, symptom follow-up, doctor profiles, pre-appt instructions, booking ref, appointment history, feedback, slot recommendation, after-hours, pagination
6. `docs/06-admin.md`      → Admin panel: single index.html, all 7 pages, HTTP Basic Auth, mobile responsive

---

## Critical Rules (apply throughout all phases)

- Phase 0 in `docs/01-setup.md` contains anti-rejection rules — read before writing any code
- All 5 critical guards must be at the top of `handle_message()` — see `docs/02-core.md`
- Every Groq call must have a keyword fallback — see `docs/02-core.md` Phase 3
- Never expose stack traces in webhook API responses
- Phone numbers in logs must be masked: +91XXXXXX7890
- Check `last_processed_message_id` before processing any message (duplicate webhook guard)
- Session timeout 30 min for mid-booking states
- Public holiday + doctor leave check runs BEFORE slot availability check

---

## Environment Variables Needed

```
WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN
GROQ_API_KEY, GROQ_MODEL
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
HOSPITAL_NAME, HOSPITAL_PHONE, HOSPITAL_EMERGENCY_NUMBER
HOSPITAL_MAPS_LINK, HOSPITAL_ADDRESS, HOSPITAL_LANDMARK
HOSPITAL_WEBSITE, HOSPITAL_PRIVACY_POLICY_URL
BOOKING_REF_PREFIX, ADMIN_USERNAME, ADMIN_PASSWORD
APP_ENV, APP_PORT
```

---

## Final File Checklist

After all 6 docs are implemented, verify every file exists:

```
app/main.py                         app/config.py
app/database.py                     app/routers/webhook.py
app/routers/health.py               app/routers/admin.py
app/services/whatsapp.py            app/services/ai_engine.py
app/services/conversation.py        app/services/appointment.py
app/services/scheduler.py           app/services/consent.py
app/services/analytics.py           app/services/faq_engine.py
app/services/feedback.py            app/models/patient.py
app/models/appointment.py           app/models/conversation.py
app/models/message.py               app/templates/whatsapp_templates.py
app/utils/logger.py                 app/utils/validators.py
app/utils/helpers.py                migrations/001_initial_schema.sql
admin/index.html                    tests/test_webhook.py
tests/test_ai_engine.py             tests/test_appointment.py
requirements.txt                    Dockerfile
railway.toml                        .env.example
.gitignore                          README.md
```