# MediAssist AI — Multi-Tenant SaaS Platform
# CLAUDE.md — Complete Build Specification

---

## 1. PROJECT OVERVIEW

Convert MediAssist AI from a single-clinic WhatsApp bot into a
multi-tenant SaaS platform. One FastAPI server on Render.com serves
unlimited hospital/clinic clients. Each clinic is fully isolated by
`clinic_id`. The Meta WhatsApp webhook routes every incoming message
to the correct tenant using the receiving phone number as the key.

### Core Guarantees
- One clinic NEVER sees another clinic's data
- Every reply goes OUT from the correct clinic's WhatsApp number
- Concurrent messages from multiple clinics are handled in parallel
- A new clinic is onboarded by inserting one row — zero new deployments

---

## 2. TECH STACK

| Layer        | Technology                              |
|--------------|-----------------------------------------|
| Backend      | Python 3.11 + FastAPI                   |
| Database     | Supabase (PostgreSQL + RLS)             |
| LLM          | Groq — llama-3.3-70b-versatile          |
| WhatsApp     | Meta Cloud API (per-clinic numbers)     |
| HTTP Client  | httpx (async)                           |
| Deployment   | Render.com (single Web Service)         |
| Env Secrets  | Render Environment Variables            |

---

## 3. ENVIRONMENT VARIABLES

Store ONLY these in Render / .env. Nothing else is global.

```
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_KEY=service_role_key_here      # NOT anon key
GROQ_API_KEY=gsk_xxxx
ADMIN_SECRET=your_random_admin_api_key          # protects /admin routes
META_VERIFY_TOKEN=your_webhook_verify_token     # for Meta webhook handshake
```

Per-clinic secrets (Meta tokens, phone number IDs) live in the
`clinics.config` JSONB column — NOT in environment variables.

---

## 4. DATABASE SCHEMA

Run migrations in this exact order.

### 4.1 Master Clinics Table

```sql
CREATE TABLE clinics (
  id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  name              TEXT        NOT NULL,
  whatsapp_number   TEXT        UNIQUE NOT NULL,
  -- whatsapp_number = the E.164 number patients message TO
  -- e.g. "+919876543210"
  plan              TEXT        NOT NULL DEFAULT 'basic'
                                CHECK (plan IN ('basic','pro','enterprise')),
  config            JSONB       NOT NULL DEFAULT '{}'::jsonb,
  /*
    config JSONB schema:
    {
      "meta_phone_number_id": "1234567890",   -- Meta API phone number ID
      "meta_access_token":    "EAAxxxx",      -- permanent page token
      "clinic_name":          "Apollo Hyderabad",
      "doctor_name":          "Dr. Ravi Kumar",
      "system_prompt":        "custom AI personality (optional)",
      "language":             "en | hi | te | ta | kn",
      "logo_url":             "https://...",  -- for PDF reports
      "timezone":             "Asia/Kolkata"
    }
  */
  is_active         BOOLEAN     NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER clinics_updated_at
  BEFORE UPDATE ON clinics
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

### 4.2 Add clinic_id to All Existing Tables

```sql
-- Run for each existing table: patients, appointments,
-- lab_reports, prescriptions, reminders, conversations

ALTER TABLE patients      ADD COLUMN clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE appointments  ADD COLUMN clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE lab_reports   ADD COLUMN clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE prescriptions ADD COLUMN clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE reminders     ADD COLUMN clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE conversations ADD COLUMN clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;

-- Index every clinic_id for fast per-tenant queries
CREATE INDEX ON patients      (clinic_id);
CREATE INDEX ON appointments  (clinic_id);
CREATE INDEX ON lab_reports   (clinic_id);
CREATE INDEX ON prescriptions (clinic_id);
CREATE INDEX ON reminders     (clinic_id);
CREATE INDEX ON conversations (clinic_id, patient_phone, created_at DESC);
```

### 4.3 Row Level Security (Database-Level Firewall)

```sql
-- Enable RLS on every tenant table
ALTER TABLE patients      ENABLE ROW LEVEL SECURITY;
ALTER TABLE appointments  ENABLE ROW LEVEL SECURITY;
ALTER TABLE lab_reports   ENABLE ROW LEVEL SECURITY;
ALTER TABLE prescriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE reminders     ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;

-- Policy template — repeat for each table
-- IMPORTANT: Use service_role key in backend to bypass RLS
-- where needed (admin routes). Use anon key nowhere.
CREATE POLICY "clinic_isolation" ON patients
  USING (clinic_id = current_setting('app.clinic_id')::uuid);
```

NOTE: Since the backend uses `service_role` key and manually
filters by `clinic_id` on every query, RLS is a safety net —
not the primary enforcement layer. Both layers protect together.

### 4.4 Seed Existing Client as First Clinic

```sql
-- Replace values with your actual existing client data
INSERT INTO clinics (id, name, whatsapp_number, plan, config)
VALUES (
  gen_random_uuid(),
  'Existing Hospital Name',
  '+919876543210',
  'pro',
  '{
    "meta_phone_number_id": "YOUR_EXISTING_PHONE_NUMBER_ID",
    "meta_access_token":    "YOUR_EXISTING_TOKEN",
    "clinic_name":          "Existing Hospital Name",
    "doctor_name":          "Dr. Name",
    "language":             "en",
    "timezone":             "Asia/Kolkata"
  }'::jsonb
);

-- Backfill existing rows (run once)
UPDATE patients      SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE appointments  SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE lab_reports   SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE prescriptions SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE reminders     SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE conversations SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;

-- NOW make clinic_id NOT NULL after backfill
ALTER TABLE patients      ALTER COLUMN clinic_id SET NOT NULL;
ALTER TABLE appointments  ALTER COLUMN clinic_id SET NOT NULL;
ALTER TABLE lab_reports   ALTER COLUMN clinic_id SET NOT NULL;
ALTER TABLE prescriptions ALTER COLUMN clinic_id SET NOT NULL;
ALTER TABLE reminders     ALTER COLUMN clinic_id SET NOT NULL;
ALTER TABLE conversations ALTER COLUMN clinic_id SET NOT NULL;
```

---

## 5. PROJECT FILE STRUCTURE

```
mediassist/
├── main.py                     # FastAPI app, all route registration
├── config.py                   # reads env vars only, no clinic data
├── requirements.txt
│
├── core/
│   ├── __init__.py
│   ├── tenant.py               # resolve_tenant(), get_clinic_by_id()
│   ├── plans.py                # can_use(), require_feature(), PLAN_FEATURES
│   ├── llm.py                  # build_system_prompt(), call_groq()
│   ├── whatsapp.py             # send_message(), send_template(), send_document()
│   └── exceptions.py           # custom exceptions
│
├── handlers/
│   ├── __init__.py
│   ├── dispatcher.py           # routes message intent to correct handler
│   ├── appointments.py         # book, reschedule, cancel flows
│   ├── lab_reports.py          # report delivery + PDF send
│   ├── reminders.py            # prescription reminder flows
│   └── khata.py                # billing/payment flows (enterprise)
│
├── admin/
│   ├── __init__.py
│   └── routes.py               # /admin/* CRUD for clinics
│
└── db/
    ├── supabase_client.py      # single shared client instance
    └── migrations/
        ├── 001_create_clinics.sql
        ├── 002_add_clinic_id.sql
        ├── 003_add_indexes.sql
        ├── 004_enable_rls.sql
        └── 005_seed_first_clinic.sql
```

---

## 6. CORE MODULE CODE

### 6.1 config.py

```python
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL        = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY= os.environ["SUPABASE_SERVICE_KEY"]
GROQ_API_KEY        = os.environ["GROQ_API_KEY"]
ADMIN_SECRET        = os.environ["ADMIN_SECRET"]
META_VERIFY_TOKEN   = os.environ["META_VERIFY_TOKEN"]
```

### 6.2 core/exceptions.py

```python
class TenantNotFound(Exception):
    """Raised when no clinic matches the incoming WhatsApp number."""
    pass

class FeatureNotAvailable(Exception):
    """Raised when a clinic's plan does not include the requested feature."""
    pass

class PatientNotFound(Exception):
    """Raised when no patient record exists for an incoming number."""
    pass

class InvalidPayload(Exception):
    """Raised when Meta webhook payload is malformed."""
    pass
```

### 6.3 db/supabase_client.py

```python
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_SERVICE_KEY

# Single module-level client — thread-safe for FastAPI
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
```

### 6.4 core/tenant.py

```python
from db.supabase_client import supabase
from core.exceptions import TenantNotFound
import logging

logger = logging.getLogger(__name__)

# In-memory cache: {whatsapp_number: clinic_dict}
# Avoids a DB hit on every single message.
# Cache is cleared on clinic update via /admin.
_tenant_cache: dict[str, dict] = {}

async def resolve_tenant(display_phone_number: str) -> dict:
    """
    Resolve clinic from the receiving WhatsApp number.
    display_phone_number comes from Meta payload metadata.
    Format: "+919876543210" (E.164, with + prefix)
    
    REAL-TIME SCENARIO: Two hospitals message simultaneously.
    This function is called independently per request with its
    own local `clinic` dict — no shared mutable state.
    """
    # Normalize: Meta sometimes sends without +
    phone = display_phone_number if display_phone_number.startswith("+") \
            else f"+{display_phone_number}"

    # Check cache first (avoids DB round-trip on every message)
    if phone in _tenant_cache:
        clinic = _tenant_cache[phone]
        if clinic.get("is_active"):
            return clinic
        else:
            raise TenantNotFound(f"Clinic for {phone} is inactive.")

    # Cache miss → query DB
    try:
        result = supabase.table("clinics") \
            .select("*") \
            .eq("whatsapp_number", phone) \
            .eq("is_active", True) \
            .single() \
            .execute()
    except Exception as e:
        logger.error(f"DB error resolving tenant for {phone}: {e}")
        raise TenantNotFound(f"No active clinic found for {phone}")

    if not result.data:
        logger.warning(f"Unknown WhatsApp number hit webhook: {phone}")
        raise TenantNotFound(f"No clinic registered for {phone}")

    _tenant_cache[phone] = result.data
    return result.data

def invalidate_tenant_cache(whatsapp_number: str = None):
    """Call after /admin clinic update to clear stale cache."""
    if whatsapp_number:
        _tenant_cache.pop(whatsapp_number, None)
    else:
        _tenant_cache.clear()

async def get_clinic_by_id(clinic_id: str) -> dict:
    result = supabase.table("clinics") \
        .select("*") \
        .eq("id", clinic_id) \
        .single() \
        .execute()
    if not result.data:
        raise TenantNotFound(f"Clinic {clinic_id} not found")
    return result.data
```

### 6.5 core/plans.py

```python
from core.exceptions import FeatureNotAvailable

PLAN_FEATURES: dict[str, list[str]] = {
    "basic": [
        "appointments",
        "reminders"
    ],
    "pro": [
        "appointments",
        "reminders",
        "lab_reports",
        "prescriptions"
    ],
    "enterprise": [
        "appointments",
        "reminders",
        "lab_reports",
        "prescriptions",
        "khata",
        "analytics",
        "custom_prompt",
        "bulk_blast"
    ]
}

UPGRADE_MESSAGE: dict[str, str] = {
    "lab_reports":   "Lab report delivery requires the Pro plan.",
    "khata":         "KhataBot requires the Enterprise plan.",
    "bulk_blast":    "SchemeBlast requires the Enterprise plan.",
    "analytics":     "Analytics requires the Enterprise plan.",
    "custom_prompt": "Custom AI personality requires the Enterprise plan."
}

def can_use(clinic: dict, feature: str) -> bool:
    plan = clinic.get("plan", "basic")
    return feature in PLAN_FEATURES.get(plan, [])

def require_feature(clinic: dict, feature: str) -> None:
    """
    Call before any feature handler. Raises FeatureNotAvailable
    with a user-friendly message if the plan doesn't allow it.
    """
    if not can_use(clinic, feature):
        msg = UPGRADE_MESSAGE.get(feature, f"This feature is not available on your plan.")
        raise FeatureNotAvailable(msg)
```

### 6.6 core/llm.py

```python
import httpx
import logging
from config import GROQ_API_KEY

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL    = "llama-3.3-70b-versatile"

DEFAULT_SYSTEM_PROMPT = """
You are a helpful WhatsApp medical assistant.
Be concise, professional, and empathetic.
Never make up medical information.
If unsure, say so and suggest the patient contact the clinic directly.
"""

def build_system_prompt(clinic: dict) -> str:
    """
    Builds a fully tenant-specific system prompt.
    Each clinic gets its own AI personality.
    
    REAL-TIME SCENARIO: Hospital A = Telugu, Hospital B = Hindi.
    This function is called separately per request with each
    clinic's own config — prompts never mix.
    """
    config = clinic.get("config", {})

    # Use custom prompt if clinic has enterprise plan and set one
    custom = config.get("system_prompt", "").strip()
    base   = custom if custom else DEFAULT_SYSTEM_PROMPT.strip()

    clinic_name = config.get("clinic_name", clinic.get("name", "our clinic"))
    doctor_name = config.get("doctor_name", "the doctor")
    language    = config.get("language", "en")

    lang_instruction = {
        "en": "Always respond in clear English.",
        "hi": "Always respond in Hindi (हिंदी). Use simple language.",
        "te": "Always respond in Telugu (తెలుగు). Use simple language.",
        "ta": "Always respond in Tamil (தமிழ்). Use simple language.",
        "kn": "Always respond in Kannada (ಕನ್ನಡ). Use simple language."
    }.get(language, "Always respond in English.")

    return f"""
You are a WhatsApp medical assistant for {clinic_name}.
Consulting Doctor: {doctor_name}
{lang_instruction}

{base}

STRICT RULES — NEVER VIOLATE:
1. Never share any patient data without confirming their identity first.
2. Never discuss or reference any other clinic or hospital.
3. Never fabricate medical advice, dosages, or diagnoses.
4. If a question is outside your scope, say: "Please visit {clinic_name} or call us directly."
5. Always be brief — WhatsApp messages should be under 300 words.
""".strip()

async def call_groq(
    clinic: dict,
    conversation_history: list[dict],
    max_tokens: int = 500
) -> str:
    """
    Calls Groq LLM with tenant-specific system prompt + conversation history.
    
    conversation_history format:
    [
      {"role": "user",      "content": "I want to book an appointment"},
      {"role": "assistant", "content": "Sure! What date works for you?"},
      {"role": "user",      "content": "Tomorrow at 10am"}
    ]
    
    REAL-TIME SCENARIO: Multiple clinics calling Groq simultaneously.
    Each call is fully independent — separate HTTP requests with
    separate system prompts. No shared state.
    """
    system_prompt = build_system_prompt(clinic)
    messages = [{"role": "system", "content": system_prompt}] + conversation_history

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json"
    }
    payload = {
        "model":       MODEL,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": 0.3  # Lower = more consistent medical responses
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(GROQ_URL, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()

    except httpx.TimeoutException:
        logger.error(f"Groq timeout for clinic {clinic['id']}")
        return "Sorry, I'm taking too long to respond. Please try again in a moment."

    except httpx.HTTPStatusError as e:
        logger.error(f"Groq HTTP error {e.response.status_code} for clinic {clinic['id']}: {e}")
        return "I'm having trouble processing your request. Please try again shortly."

    except Exception as e:
        logger.error(f"Groq unexpected error for clinic {clinic['id']}: {e}")
        return "Something went wrong. Please contact the clinic directly."
```

### 6.7 core/whatsapp.py

```python
import httpx
import logging

logger = logging.getLogger(__name__)
META_API_VERSION = "v19.0"
META_BASE_URL    = f"https://graph.facebook.com/{META_API_VERSION}"

def _get_headers(clinic: dict) -> dict:
    token = clinic.get("config", {}).get("meta_access_token")
    if not token:
        raise ValueError(f"Clinic {clinic['id']} has no meta_access_token in config")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json"
    }

def _get_phone_number_id(clinic: dict) -> str:
    pid = clinic.get("config", {}).get("meta_phone_number_id")
    if not pid:
        raise ValueError(f"Clinic {clinic['id']} has no meta_phone_number_id in config")
    return pid

async def send_message(clinic: dict, to: str, text: str) -> bool:
    """
    Send a plain text WhatsApp message FROM the clinic's registered number.
    
    `to` = patient's phone number in E.164 format, e.g. "+919876543210"
    
    REAL-TIME SCENARIO: Hospital A replies to patient A,
    Hospital B replies to patient B at the same time.
    Each call uses its own clinic's phone_number_id and token.
    Completely independent HTTP requests. No collision possible.
    """
    # Strip + for Meta API (Meta expects without +)
    to_clean = to.lstrip("+")
    phone_number_id = _get_phone_number_id(clinic)

    url     = f"{META_BASE_URL}/{phone_number_id}/messages"
    headers = _get_headers(clinic)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to_clean,
        "type":              "text",
        "text":              {"body": text, "preview_url": False}
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            logger.info(f"[{clinic['name']}] Message sent to {to_clean}")
            return True

    except httpx.HTTPStatusError as e:
        logger.error(
            f"[{clinic['name']}] WhatsApp send failed. "
            f"Status: {e.response.status_code}. Body: {e.response.text}"
        )
        return False

    except Exception as e:
        logger.error(f"[{clinic['name']}] WhatsApp send error: {e}")
        return False

async def send_document(clinic: dict, to: str, doc_url: str, filename: str, caption: str = "") -> bool:
    """
    Send a PDF document (lab report) via WhatsApp.
    doc_url must be a publicly accessible HTTPS URL.
    """
    to_clean        = to.lstrip("+")
    phone_number_id = _get_phone_number_id(clinic)
    url             = f"{META_BASE_URL}/{phone_number_id}/messages"
    headers         = _get_headers(clinic)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to_clean,
        "type":              "document",
        "document": {
            "link":     doc_url,
            "filename": filename,
            "caption":  caption
        }
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            logger.info(f"[{clinic['name']}] Document sent to {to_clean}: {filename}")
            return True

    except Exception as e:
        logger.error(f"[{clinic['name']}] Document send error: {e}")
        return False

async def mark_as_read(clinic: dict, message_id: str) -> None:
    """Mark incoming message as read (shows blue ticks on patient's side)."""
    phone_number_id = _get_phone_number_id(clinic)
    url     = f"{META_BASE_URL}/{phone_number_id}/messages"
    headers = _get_headers(clinic)
    payload = {
        "messaging_product": "whatsapp",
        "status":            "read",
        "message_id":        message_id
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=payload, headers=headers)
    except Exception as e:
        logger.warning(f"[{clinic['name']}] Mark read failed: {e}")
        # Non-critical — don't raise
```

---

## 7. WEBHOOK — MAIN ENTRYPOINT

### 7.1 main.py

```python
import logging
import json
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Depends, Header
from fastapi.responses import PlainTextResponse

from config import META_VERIFY_TOKEN, ADMIN_SECRET
from core.tenant import resolve_tenant, invalidate_tenant_cache
from core.exceptions import TenantNotFound, FeatureNotAvailable, InvalidPayload
from core.whatsapp import mark_as_read, send_message
from handlers.dispatcher import dispatch
from admin.routes import admin_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="MediAssist AI", version="2.0.0")
app.include_router(admin_router, prefix="/admin")


# ─── META WEBHOOK VERIFICATION ──────────────────────────────────────────────

@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Meta calls this GET once when you register the webhook.
    Must return the hub.challenge value to confirm ownership.
    """
    params     = dict(request.query_params)
    mode       = params.get("hub.mode")
    token      = params.get("hub.verify_token")
    challenge  = params.get("hub.challenge")

    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        logger.info("Webhook verified by Meta.")
        return PlainTextResponse(challenge)

    logger.warning("Webhook verification failed — wrong token.")
    raise HTTPException(status_code=403, detail="Verification failed")


# ─── META WEBHOOK — MESSAGE RECEIVER ────────────────────────────────────────

@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Every WhatsApp message from every clinic arrives here.
    
    CRITICAL REQUIREMENT: Must return HTTP 200 within 5 seconds.
    If we don't, Meta will retry — causing duplicate processing.
    
    Solution: Validate payload, then offload all processing to
    a background task. Return 200 immediately.
    
    REAL-TIME SCENARIO: Hospital A and Hospital B send messages
    at the same millisecond. FastAPI creates two independent
    async tasks. They run in parallel with zero shared state.
    """
    try:
        body = await request.json()
    except Exception:
        # Return 200 even on bad JSON — Meta retries on non-200
        logger.error("Received non-JSON webhook payload")
        return {"status": "ignored"}

    # Ignore non-message events (delivery receipts, read receipts, etc.)
    try:
        entry   = body["entry"][0]
        changes = entry["changes"][0]["value"]

        # Skip if no messages key (status updates, etc.)
        if "messages" not in changes:
            return {"status": "ignored"}

        message  = changes["messages"][0]
        metadata = changes["metadata"]

    except (KeyError, IndexError):
        logger.warning("Webhook payload missing expected fields — ignoring")
        return {"status": "ignored"}

    # Offload to background — return 200 immediately to Meta
    background_tasks.add_task(process_message, message, metadata)
    return {"status": "received"}


async def process_message(message: dict, metadata: dict):
    """
    Background task — runs after 200 is already returned to Meta.
    All real logic lives here.
    """
    try:
        # 1. Identify WHICH clinic this message was sent TO
        display_phone = metadata.get("display_phone_number", "")
        clinic        = await resolve_tenant(display_phone)

        # 2. Extract sender info
        from_number = message.get("from", "")  # patient's number
        message_id  = message.get("id", "")
        msg_type    = message.get("type", "")

        if not from_number:
            logger.error("Message has no 'from' field — skipping")
            return

        # 3. Mark as read (shows double blue ticks to patient)
        await mark_as_read(clinic, message_id)

        # 4. Extract text content
        if msg_type == "text":
            user_text = message["text"]["body"].strip()
        elif msg_type == "interactive":
            # Button/list reply
            interactive = message["interactive"]
            if interactive["type"] == "button_reply":
                user_text = interactive["button_reply"]["title"]
            elif interactive["type"] == "list_reply":
                user_text = interactive["list_reply"]["title"]
            else:
                user_text = ""
        elif msg_type == "document":
            # Patient sent a file — handle if needed
            user_text = "[document received]"
        else:
            # Voice, image, sticker, etc. — unsupported
            await send_message(
                clinic, from_number,
                "Sorry, I can only handle text messages at the moment."
            )
            return

        if not user_text:
            return

        # 5. Dispatch to the correct feature handler
        await dispatch(clinic, from_number, user_text, message_id)

    except TenantNotFound as e:
        # Message came in on an unregistered number — ignore silently
        logger.warning(f"TenantNotFound: {e}")

    except Exception as e:
        logger.error(f"Unhandled error in process_message: {e}", exc_info=True)
        # Attempt to notify the patient something went wrong
        try:
            await send_message(
                clinic, from_number,
                "Sorry, I encountered an error. Please try again or contact the clinic directly."
            )
        except Exception:
            pass  # Last resort — don't crash the background task
```

---

## 8. MESSAGE DISPATCHER

### 8.1 handlers/dispatcher.py

```python
import logging
from db.supabase_client import supabase
from core.plans import require_feature, can_use
from core.llm import call_groq
from core.whatsapp import send_message
from core.exceptions import FeatureNotAvailable, PatientNotFound

logger = logging.getLogger(__name__)

# Conversation history per (clinic_id, patient_phone)
# In-memory for now — acceptable for single Render instance.
# For multi-instance: replace with Redis or Supabase conversations table.
_conversation_cache: dict[str, list[dict]] = {}
MAX_HISTORY = 10  # Keep last 10 turns to avoid token bloat

def _history_key(clinic: dict, patient_phone: str) -> str:
    return f"{clinic['id']}:{patient_phone}"

def _get_history(clinic: dict, patient_phone: str) -> list[dict]:
    return _conversation_cache.get(_history_key(clinic, patient_phone), [])

def _add_to_history(clinic: dict, patient_phone: str, role: str, content: str):
    key = _history_key(clinic, patient_phone)
    if key not in _conversation_cache:
        _conversation_cache[key] = []
    _conversation_cache[key].append({"role": role, "content": content})
    # Trim to last MAX_HISTORY messages
    _conversation_cache[key] = _conversation_cache[key][-MAX_HISTORY:]

async def dispatch(clinic: dict, patient_phone: str, user_text: str, message_id: str):
    """
    Routes incoming message to the correct feature handler.
    Falls back to LLM for general queries.
    
    REAL-TIME SCENARIO: Patient from Hospital A and patient from
    Hospital B both send messages. Each call has its own `clinic`
    dict and its own history_key — completely isolated.
    """
    text_lower = user_text.lower().strip()

    # ── Keyword-based routing ─────────────────────────────────────
    try:
        if any(k in text_lower for k in ["appointment", "book", "schedule", "reschedule", "cancel"]):
            require_feature(clinic, "appointments")
            from handlers.appointments import handle_appointments
            await handle_appointments(clinic, patient_phone, user_text)

        elif any(k in text_lower for k in ["report", "result", "lab", "test"]):
            require_feature(clinic, "lab_reports")
            from handlers.lab_reports import handle_lab_reports
            await handle_lab_reports(clinic, patient_phone, user_text)

        elif any(k in text_lower for k in ["reminder", "medicine", "tablet", "dose", "prescription"]):
            require_feature(clinic, "reminders")
            from handlers.reminders import handle_reminders
            await handle_reminders(clinic, patient_phone, user_text)

        elif any(k in text_lower for k in ["bill", "payment", "balance", "due", "khata"]):
            require_feature(clinic, "khata")
            from handlers.khata import handle_khata
            await handle_khata(clinic, patient_phone, user_text)

        else:
            # General query → LLM
            history = _get_history(clinic, patient_phone)
            _add_to_history(clinic, patient_phone, "user", user_text)

            reply = await call_groq(
                clinic,
                history + [{"role": "user", "content": user_text}]
            )
            _add_to_history(clinic, patient_phone, "assistant", reply)
            await send_message(clinic, patient_phone, reply)

    except FeatureNotAvailable as e:
        await send_message(clinic, patient_phone, str(e))

    except Exception as e:
        logger.error(f"[{clinic['name']}] Dispatch error for {patient_phone}: {e}", exc_info=True)
        await send_message(
            clinic, patient_phone,
            "Sorry, something went wrong. Please try again."
        )
```

---

## 9. ADMIN ONBOARDING API

### 9.1 admin/routes.py

```python
from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel
from typing import Optional
from db.supabase_client import supabase
from core.tenant import invalidate_tenant_cache
from core.whatsapp import send_message
from config import ADMIN_SECRET

admin_router = APIRouter()

def verify_admin(x_admin_secret: str = Header(...)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")

class CreateClinicRequest(BaseModel):
    name:              str
    whatsapp_number:   str   # E.164, e.g. "+919876543210"
    plan:              str = "basic"
    meta_phone_number_id: str
    meta_access_token: str
    clinic_name:       str
    doctor_name:       str
    language:          str = "en"
    timezone:          str = "Asia/Kolkata"
    system_prompt:     Optional[str] = None
    logo_url:          Optional[str] = None

@admin_router.post("/clinics", dependencies=[Depends(verify_admin)])
async def create_clinic(req: CreateClinicRequest):
    """Onboard a new hospital. Zero deployment needed."""
    config = {
        "meta_phone_number_id": req.meta_phone_number_id,
        "meta_access_token":    req.meta_access_token,
        "clinic_name":          req.clinic_name,
        "doctor_name":          req.doctor_name,
        "language":             req.language,
        "timezone":             req.timezone
    }
    if req.system_prompt: config["system_prompt"] = req.system_prompt
    if req.logo_url:       config["logo_url"]       = req.logo_url

    result = supabase.table("clinics").insert({
        "name":             req.name,
        "whatsapp_number":  req.whatsapp_number,
        "plan":             req.plan,
        "config":           config
    }).execute()

    if not result.data:
        raise HTTPException(500, "Failed to create clinic")

    return {"success": True, "clinic": result.data[0]}

@admin_router.patch("/clinics/{clinic_id}", dependencies=[Depends(verify_admin)])
async def update_clinic(clinic_id: str, updates: dict):
    """Update plan, config, or status. Clears tenant cache."""
    result = supabase.table("clinics") \
        .update(updates) \
        .eq("id", clinic_id) \
        .execute()

    if not result.data:
        raise HTTPException(404, "Clinic not found")

    # Clear cache so next message picks up new config
    clinic = result.data[0]
    invalidate_tenant_cache(clinic["whatsapp_number"])
    return {"success": True, "clinic": clinic}

@admin_router.get("/clinics", dependencies=[Depends(verify_admin)])
async def list_clinics():
    result = supabase.table("clinics").select("id,name,whatsapp_number,plan,is_active,created_at").execute()
    return {"clinics": result.data}

@admin_router.post("/clinics/{clinic_id}/test", dependencies=[Depends(verify_admin)])
async def test_clinic(clinic_id: str, to: str):
    """Send a test WhatsApp message from the clinic's number."""
    from core.tenant import get_clinic_by_id
    clinic = await get_clinic_by_id(clinic_id)
    success = await send_message(clinic, to, f"✅ Test message from {clinic['name']}. Your MediAssist AI is live!")
    return {"sent": success}

@admin_router.delete("/clinics/{clinic_id}", dependencies=[Depends(verify_admin)])
async def deactivate_clinic(clinic_id: str):
    """Soft-delete: sets is_active=false. Data preserved."""
    result = supabase.table("clinics") \
        .update({"is_active": False}) \
        .eq("id", clinic_id) \
        .execute()
    if not result.data:
        raise HTTPException(404, "Clinic not found")
    invalidate_tenant_cache(result.data[0]["whatsapp_number"])
    return {"success": True, "message": "Clinic deactivated"}
```

---

## 10. REAL-TIME SCENARIO CROSSCHECK

### ✅ Scenario 1 — Two Hospitals Message Simultaneously
- Meta sends two POST /webhook requests
- FastAPI handles both async — no blocking
- `resolve_tenant()` returns different `clinic` dicts for each
- `dispatch()` runs in separate background tasks
- DB queries each have their own `clinic_id` filter
- Replies go from each clinic's own WhatsApp number
- **RESULT: Zero collision**

### ✅ Scenario 2 — Same Patient Number, Different Clinics
- Patient +91-9999 is registered in Hospital A AND Hospital B
- Messages to Hospital A's number → `clinic_id = A`
- Messages to Hospital B's number → `clinic_id = B`
- History key is `clinic_id:patient_phone` — separate histories
- DB queries filter by `clinic_id` first — no data bleed
- **RESULT: Fully isolated**

### ✅ Scenario 3 — Meta Retries (Slow Processing)
- `process_message` is offloaded to `background_tasks`
- Webhook returns `{"status": "received"}` in < 50ms
- Meta never retries
- **RESULT: No duplicate messages**

### ✅ Scenario 4 — New Clinic Onboarded Mid-Day
- POST /admin/clinics inserts one row in `clinics`
- Existing server picks it up on next message automatically
- Tenant cache is empty for new number → hits DB → caches it
- **RESULT: Zero downtime, zero redeployment**

### ✅ Scenario 5 — Clinic Plan Upgraded Mid-Month
- PATCH /admin/clinics/{id} updates `plan` field
- `invalidate_tenant_cache()` clears stale cache entry
- Next message resolves fresh clinic dict with new plan
- `require_feature()` now allows the upgraded features
- **RESULT: Instant plan activation**

### ✅ Scenario 6 — Meta Sends Status Updates (Not Messages)
- Delivery receipts, read receipts hit /webhook POST
- Payload has no "messages" key — caught by guard clause
- Returns `{"status": "ignored"}` with 200
- **RESULT: No spurious processing**

### ✅ Scenario 7 — Groq API Timeout
- `call_groq()` has 30s timeout with try/except
- Returns a safe fallback string to patient
- Does NOT crash the background task
- **RESULT: Graceful degradation**

### ✅ Scenario 8 — Unknown WhatsApp Number Hits Webhook
- `resolve_tenant()` raises `TenantNotFound`
- `process_message` catches it, logs warning, returns silently
- No error response sent to unknown number
- **RESULT: No crash, no data exposure**

---

## 11. REQUIREMENTS.TXT

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
httpx==0.27.2
supabase==2.7.2
python-dotenv==1.0.1
pydantic==2.8.2
```

---

## 12. RENDER DEPLOYMENT

Start command:
```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Set in Render Environment:
```
SUPABASE_URL
SUPABASE_SERVICE_KEY
GROQ_API_KEY
ADMIN_SECRET
META_VERIFY_TOKEN
```

Meta Webhook URL: `https://your-render-url.onrender.com/webhook`

---

## 13. WHAT CLAUDE CODE MUST NOT DO

- Do NOT create per-clinic config files or .env files
- Do NOT use global variables to store clinic state
- Do NOT skip `clinic_id` on ANY database insert or select
- Do NOT call send_message without passing the `clinic` dict
- Do NOT expose /admin routes without `verify_admin` dependency
- Do NOT use synchronous DB calls inside async route handlers
- Do NOT log patient data (phone numbers, medical info) at INFO level
- Do NOT hardcode any WhatsApp tokens or phone number IDs

---

## 14. DEFINITION OF DONE

- [ ] All 6 tables have clinic_id NOT NULL with index
- [ ] RLS enabled on all tenant tables
- [ ] Existing client data backfilled and verified
- [ ] Webhook returns 200 in < 100ms (measure with logs)
- [ ] Two test clinics created via /admin and both reply correctly
- [ ] Feature gate blocks pro features on basic plan with correct message
- [ ] No cross-tenant data returned in any query (manual test)
- [ ] Groq timeout returns fallback message (test with bad API key)
- [ ] Meta status update payloads return "ignored" without error
- [ ] Render deploy succeeds with zero code changes needed