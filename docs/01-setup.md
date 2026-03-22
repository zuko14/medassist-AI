# Phase 0-2: Project Structure, Anti-Rejection, Setup, Database


## 🗂️ Project Structure

```
mediassist-ai/
├── CLAUDE.md                        # This file
├── .env.example
├── .gitignore
├── requirements.txt
├── Dockerfile
├── railway.toml / render.yaml
│
├── app/
│   ├── main.py                      # FastAPI app entry point
│   ├── config.py                    # Settings & env vars
│   ├── database.py                  # Supabase client
│   │
│   ├── routers/
│   │   ├── webhook.py               # POST /webhook — Meta WhatsApp handler
│   │   ├── health.py                # GET /health — uptime check
│   │   └── admin.py                 # GET /admin/* — analytics dashboard API
│   │
│   ├── services/
│   │   ├── whatsapp.py              # Send messages, templates, buttons
│   │   ├── ai_engine.py             # Groq intent + symptom mapping
│   │   ├── conversation.py          # Session state machine
│   │   ├── appointment.py           # Booking CRUD logic
│   │   ├── scheduler.py             # APScheduler reminders + follow-ups
│   │   ├── consent.py               # DPDP/HIPAA opt-in management
│   │   └── analytics.py             # Event tracking
│   │
│   ├── models/
│   │   ├── patient.py               # Patient Pydantic model
│   │   ├── appointment.py           # Appointment Pydantic model
│   │   ├── conversation.py          # Session state model
│   │   └── message.py               # Incoming WhatsApp payload model
│   │
│   ├── templates/
│   │   └── whatsapp_templates.py    # All pre-approved Meta message templates
│   │
│   └── utils/
│       ├── logger.py                # Structured logging
│       ├── validators.py            # Phone, name, date validators
│       └── helpers.py               # Misc utilities
│
├── migrations/
│   └── 001_initial_schema.sql       # Full Supabase schema
│
└── tests/
    ├── test_webhook.py
    ├── test_ai_engine.py
    └── test_appointment.py
```

---


## 🚨 Phase 0 — Anti-Rejection Architecture (Build This FIRST)

> These features are the #1 reason hospitals and Meta reject WhatsApp AI bots.
> Every phase below depends on this foundation being correct.

### 0.1 Meta WhatsApp Business API Compliance

**Rules Claude Code MUST enforce throughout the entire build:**

1. **24-Hour Session Window**: The bot can only send free-form messages within 24 hours of the last user message. After 24 hours, ONLY pre-approved template messages can be sent. Implement a `session_expires_at` column in `conversations` table and check it before every outbound message.

2. **Template-First for Outbound**: All reminders, follow-ups, and re-engagement messages MUST use pre-approved Meta message templates (defined in `app/templates/whatsapp_templates.py`). Never send free-text to a user who hasn't messaged in 24 hours.

3. **Opt-In Before First Message**: Users must explicitly opt in before the bot sends ANY message to them. The first interaction must always be inbound (user messages the bot first). Store `opted_in: bool` and `opted_in_at: timestamp` per patient. Never message a patient who hasn't opted in.

4. **Opt-Out Handling (MANDATORY)**: Detect opt-out keywords in any language: `STOP`, `UNSUBSCRIBE`, `रुको`, `ఆపు`. On detection:
   - Immediately set `opted_in = false`
   - Reply with the pre-approved opt-out confirmation template
   - Never message that number again until they re-opt-in by messaging first

5. **No Diagnostic Claims**: The AI must NEVER say "You have [disease]" or "This sounds like [condition]". It may only say "Based on your symptoms, our *[Department]* team may be able to help." Add a system prompt guard for this.

6. **Emergency Redirect Always**: If emergency keywords are detected, the bot MUST immediately redirect to emergency services. It must NOT attempt to book an appointment or engage further. This is a hard rule — no exceptions.

7. **Healthcare Category Templates**: When registering templates with Meta, mark them as category `UTILITY` (not `MARKETING`). Healthcare utility templates have faster approval. Use the exact template names defined in `whatsapp_templates.py`.

8. **Business Display Name**: The WhatsApp Business Account must display the hospital's registered name. Ensure `WABA_DISPLAY_NAME` in `.env` matches the Meta business account.

### 0.2 Hospital Trust & Compliance Features

1. **Medical Disclaimer on Every Session Start**: Every new conversation session must display a disclaimer:
   > "MediAssist is a scheduling assistant. It does not provide medical advice. For emergencies, call 108."

2. **Data Consent Collection**: Before storing any patient data, collect explicit consent:
   > "To book your appointment, I need to save your name and contact details as per our privacy policy. Reply YES to continue or NO to proceed without saving."
   Store `data_consent: bool` per patient. If `false`, operate in stateless mode (no DB storage, session-only).

3. **Data Deletion on Request**: Detect "delete my data", "remove my information" → trigger `DELETE FROM patients WHERE phone = ?` and confirm deletion. Required by India's DPDP Act 2023.

4. **No Upselling or Marketing**: The bot must not promote services unprompted. Only respond to user-initiated service inquiries. This avoids Meta's `MARKETING` template category which has stricter approval.

5. **Transparent AI Disclosure**: The greeting must state: "I'm an AI assistant for [Hospital Name]." Never impersonate a human doctor or staff member.

6. **"Talk to Human" Always Available**: At any point in the flow, if user says "human", "agent", "speak to someone", "staff" → immediately escalate and send the hospital's direct phone number.

---


## ⚙️ Phase 1 — Project Setup

### 1.1 Initialize Repository

```bash
mkdir mediassist-ai && cd mediassist-ai
python -m venv venv && source venv/bin/activate
pip install fastapi uvicorn supabase groq apscheduler python-dotenv httpx pydantic
pip freeze > requirements.txt
```

### 1.2 Environment Variables

Create `.env` (never commit this):

```env
# Meta WhatsApp Cloud API
WHATSAPP_TOKEN=your_permanent_access_token
WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
WHATSAPP_VERIFY_TOKEN=your_custom_verify_string
WABA_DISPLAY_NAME=YourHospitalName

# Groq AI
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=llama-3.3-70b-versatile

# Supabase
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key

# Hospital Config
HOSPITAL_NAME=City Care Hospital
HOSPITAL_EMERGENCY_NUMBER=108
HOSPITAL_PHONE=+919876543210
HOSPITAL_MAPS_LINK=https://maps.google.com/?q=YourHospitalLocation
HOSPITAL_WEBSITE=https://yourhospital.com
HOSPITAL_PRIVACY_POLICY_URL=https://yourhospital.com/privacy
HOSPITAL_ADDRESS=Plot 12, Hitech City, Hyderabad, Telangana 500081
HOSPITAL_LANDMARK=Near Cyber Towers
BOOKING_REF_PREFIX=MC

# App
APP_ENV=production
APP_PORT=8000
LOG_LEVEL=INFO
```

Create `.env.example` with all keys but empty values. Add `.env` to `.gitignore`.

### 1.3 `app/config.py`

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_verify_token: str
    waba_display_name: str
    groq_api_key: str
    groq_model: str = "llama-3.3-70b-versatile"
    supabase_url: str
    supabase_service_role_key: str
    hospital_name: str
    hospital_emergency_number: str = "108"
    hospital_phone: str
    hospital_maps_link: str
    hospital_website: str
    hospital_privacy_policy_url: str
    hospital_address: str
    hospital_landmark: str
    booking_ref_prefix: str = "MC"
    app_env: str = "production"
    app_port: int = 8000
    log_level: str = "INFO"

    class Config:
        env_file = ".env"

settings = Settings()
```

---


## 🗄️ Phase 2 — Database Schema

### 2.1 `migrations/001_initial_schema.sql`

Run this in Supabase SQL editor:

```sql
-- Patients table
CREATE TABLE patients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone VARCHAR(20) UNIQUE NOT NULL,
    name VARCHAR(100),
    language VARCHAR(10) DEFAULT NULL,       -- NULL until user selects; 'en' | 'hi' | 'te'
    opted_in BOOLEAN DEFAULT false,
    opted_in_at TIMESTAMPTZ,
    opted_out_at TIMESTAMPTZ,
    data_consent BOOLEAN DEFAULT false,
    data_consent_at TIMESTAMPTZ,
    visit_count INTEGER DEFAULT 0,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Appointments table
CREATE TABLE appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID REFERENCES patients(id) ON DELETE CASCADE,
    patient_phone VARCHAR(20) NOT NULL,
    patient_name VARCHAR(100),
    department VARCHAR(50) NOT NULL,
    doctor_name VARCHAR(100),
    appointment_date DATE NOT NULL,
    appointment_time TIME NOT NULL,
    symptoms TEXT,
    status VARCHAR(20) DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'cancelled', 'rescheduled', 'completed', 'no_show')),
    reminder_24h_sent BOOLEAN DEFAULT false,
    reminder_2h_sent BOOLEAN DEFAULT false,
    followup_sent BOOLEAN DEFAULT false,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Conversations (session state)
CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone VARCHAR(20) UNIQUE NOT NULL,
    state VARCHAR(50) DEFAULT 'idle',
    context JSONB DEFAULT '{}',
    session_expires_at TIMESTAMPTZ,
    booking_context_expires_at TIMESTAMPTZ,     -- mid-booking timeout (30 min)
    last_message_at TIMESTAMPTZ DEFAULT NOW(),
    last_processed_message_id VARCHAR(100),     -- duplicate webhook guard
    unknown_intent_count INTEGER DEFAULT 0,     -- gibberish loop counter
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Analytics events
CREATE TABLE analytics_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone VARCHAR(20),
    event_type VARCHAR(50) NOT NULL,
    department VARCHAR(50),
    intent VARCHAR(50),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Doctors (configurable per hospital)
CREATE TABLE doctors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    specialization VARCHAR(50) NOT NULL,
    department VARCHAR(50) NOT NULL,
    available_days VARCHAR(100) DEFAULT 'Mon,Tue,Wed,Thu,Fri',
    morning_slots JSONB DEFAULT '["09:00","09:30","10:00","10:30","11:00","11:30"]',
    evening_slots JSONB DEFAULT '["17:00","17:30","18:00","18:30"]',
    is_active BOOLEAN DEFAULT true,
    consultation_fee INTEGER DEFAULT 500,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Doctor leaves
-- Hospital staff adds rows here via Supabase dashboard or admin API
-- Bot checks this before showing any doctor or slot
CREATE TABLE doctor_leaves (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doctor_name VARCHAR(100) NOT NULL,
    leave_date DATE NOT NULL,
    leave_type VARCHAR(20) DEFAULT 'full'
        CHECK (leave_type IN ('full', 'half_morning', 'half_evening')),
    reason VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(doctor_name, leave_date)
);

CREATE INDEX idx_leaves_doctor_date ON doctor_leaves(doctor_name, leave_date);

-- Hospital public holidays
-- Staff adds rows here; bot blocks all slots on these dates
CREATE TABLE hospital_holidays (
    holiday_date DATE PRIMARY KEY,
    name VARCHAR(100)   -- "Republic Day", "Diwali", "Christmas" etc.
);

-- Pre-seed common Indian national holidays (hospital can add/remove)
INSERT INTO hospital_holidays (holiday_date, name) VALUES
('2026-01-26', 'Republic Day'),
('2026-08-15', 'Independence Day'),
('2026-10-02', 'Gandhi Jayanti');

-- Seed doctors
-- Add superior feature columns to doctors
ALTER TABLE doctors ADD COLUMN experience_years INTEGER DEFAULT 0;
ALTER TABLE doctors ADD COLUMN qualifications VARCHAR(200);
ALTER TABLE doctors ADD COLUMN languages_spoken VARCHAR(100) DEFAULT 'English,Hindi,Telugu';
ALTER TABLE doctors ADD COLUMN rating DECIMAL(2,1) DEFAULT 4.5;
ALTER TABLE doctors ADD COLUMN fun_fact VARCHAR(200);

-- Add booking reference to appointments
ALTER TABLE appointments ADD COLUMN booking_ref VARCHAR(20) UNIQUE;
CREATE INDEX idx_appointments_booking_ref ON appointments(booking_ref);

INSERT INTO doctors (name, specialization, department, experience_years, qualifications, rating, fun_fact) VALUES
('Dr. Priya Sharma',  'General Physician',   'General Medicine', 8,  'MBBS, MD General Medicine',         4.7, 'Treated 10,000+ outpatients'),
('Dr. Arjun Reddy',   'Cardiologist',        'Cardiology',       14, 'MBBS, MD, DM Cardiology',           4.8, 'Performed 500+ cardiac procedures'),
('Dr. Meena Patel',   'Dentist',             'Dental',           6,  'BDS, MDS Oral Surgery',             4.6, 'Specialist in painless dentistry'),
('Dr. Suresh Kumar',  'Orthopedic Surgeon',  'Orthopedics',      12, 'MBBS, MS Orthopedics',              4.7, 'Expert in joint replacement'),
('Dr. Anita Singh',   'Gynecologist',        'Gynecology',       10, 'MBBS, MD Obstetrics & Gynecology',  4.9, '2000+ safe deliveries'),
('Dr. Ravi Nair',     'Pediatrician',        'Pediatrics',       9,  'MBBS, MD Pediatrics, DCH',          4.8, 'Child-friendly consultations');

-- Indexes
CREATE INDEX idx_appointments_patient_phone ON appointments(patient_phone);
CREATE INDEX idx_appointments_date ON appointments(appointment_date);
CREATE INDEX idx_appointments_status ON appointments(status);
CREATE INDEX idx_conversations_phone ON conversations(phone);
CREATE INDEX idx_analytics_event_type ON analytics_events(event_type);
CREATE INDEX idx_analytics_created_at ON analytics_events(created_at);
```

---