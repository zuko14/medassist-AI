# Phase 6-10: Scheduler, Webhook, Analytics, Services, Multilingual


## ⏰ Phase 6 — Scheduler (Reminders & Follow-ups)

### 6.1 `app/services/scheduler.py`

Use `APScheduler` with `AsyncIOScheduler`. Jobs:

**Job 1: `send_24h_reminders`** — runs every hour
```python
# Query: appointments WHERE appointment_date = tomorrow AND reminder_24h_sent = false AND status = 'confirmed'
# For each: send template 'appointment_reminder_24h', update reminder_24h_sent = true
# Only send if patient.opted_in = true
```

**Job 2: `send_2h_reminders`** — runs every 15 minutes
```python
# Query: appointments WHERE (appointment_date, appointment_time) = now+2h AND reminder_2h_sent = false AND status = 'confirmed'
# For each: send template 'appointment_reminder_2h', update reminder_2h_sent = true
# Only send if patient.opted_in = true AND within 24h session OR use template
```

**Job 3: `send_followups`** — runs daily at 10:00 AM
```python
# Query: appointments WHERE appointment_date = yesterday AND status = 'confirmed' AND followup_sent = false
# For each: send template 'post_appointment_followup', update followup_sent = true
# Wait 24 hours after appointment before sending
```

**Job 4: `mark_no_shows`** — runs daily at 11:00 PM
```python
# Query: appointments WHERE appointment_date < today AND status = 'confirmed'
# Update status = 'no_show'
# Log analytics event
```

Start scheduler in `app/main.py` `lifespan` event.

---


## 🌐 Phase 7 — Webhook Handler

### 7.1 `app/routers/webhook.py`

**GET `/webhook`** — Meta verification:
```python
@router.get("/webhook")
async def verify(hub_mode: str, hub_verify_token: str, hub_challenge: str):
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403)
```

**POST `/webhook`** — Incoming messages:
```python
@router.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    # 1. Parse payload
    # 2. Extract: phone, message_type, message_body, message_id
    # 3. Mark message as read (background task)
    # 4. Check for opt-out keywords FIRST (before any other processing)
    # 5. Handle interactive replies (button_reply, list_reply)
    # 6. Handle text messages
    # 7. Route to ConversationManager
    # Always return 200 immediately — process in background
    return {"status": "ok"}
```

**Message types to handle**:
- `text` — free text message
- `button` — quick reply button click
- `interactive` → `button_reply` — interactive button
- `interactive` → `list_reply` — list selection
- `audio` — respond "Sorry, I can only process text messages."
- `image` / `document` — respond "Please describe your query in text."

---


## 📊 Phase 8 — Analytics & Admin API

### 8.1 `app/routers/admin.py`

Protect all admin routes with a simple API key header: `X-Admin-Key`.

**Endpoints**:

```
GET  /admin/stats/daily          → daily inquiry count, booking count, conversion rate
GET  /admin/stats/departments    → bookings per department (current month)
GET  /admin/stats/peak-times     → hour-of-day booking distribution
GET  /admin/appointments         → paginated appointment list with filters
GET  /admin/appointments/{id}    → single appointment detail
PUT  /admin/appointments/{id}    → update appointment status
GET  /admin/no-show-rate         → no-show % by department
GET  /admin/patients/count       → total registered patients, opted-in count
DELETE /admin/patients/{phone}   → GDPR/DPDP data deletion by hospital staff
```

**`app/services/analytics.py`** — `track_event(phone, event_type, metadata)`:
Track these events:
- `conversation_started`
- `booking_initiated`
- `booking_confirmed`
- `booking_cancelled`
- `booking_rescheduled`
- `emergency_detected`
- `opted_out`
- `data_deleted`
- `human_escalated`
- `symptom_mapped` (with department)
- `no_show`
- `followup_booked`

---


## 🏥 Phase 9 — Services & Doctor Availability Module

### 9.1 Hospital Services (hardcoded + DB configurable)

```python
HOSPITAL_SERVICES = [
    {
        "id": "diagnostics",
        "name": "Diagnostics",
        "icon": "🔬",
        "description": "X-Ray, MRI, CT Scan, Ultrasound",
        "book_department": "Radiology"
    },
    {
        "id": "lab",
        "name": "Lab Tests",
        "icon": "🧪",
        "description": "Blood tests, urine analysis, culture reports",
        "book_department": "Pathology"
    },
    {
        "id": "surgery",
        "name": "Surgery",
        "icon": "🏥",
        "description": "General and specialized surgical procedures",
        "book_department": "Surgery"
    },
    {
        "id": "checkup",
        "name": "Health Checkups",
        "icon": "❤️",
        "description": "Full body checkup packages for all ages",
        "book_department": "General Medicine"
    },
    {
        "id": "vaccination",
        "name": "Vaccination",
        "icon": "💉",
        "description": "Adult and child vaccination programs",
        "book_department": "General Medicine"
    },
    {
        "id": "pharmacy",
        "name": "Pharmacy",
        "icon": "💊",
        "description": "24/7 in-house pharmacy",
        "book_department": None  # No booking, just info
    }
]
```

Show services as WhatsApp list message. Each item has "Book Now" button (except info-only items like pharmacy).

---


## 🌍 Phase 10 — Multilingual Support

### 10.1 Language Selection Flow

**Explicit selection comes first** — every new user sees the language picker before anything else. This is better than auto-detection because:
- User knows the bot supports their language upfront
- Avoids mis-detection on short first messages like "hi" or "hello"
- Feels more professional and inclusive to hospital clients

**Returning users** (language already set in DB): skip the picker, go straight to main menu.

**Auto-detect as fallback only**: If a user types free text during `SELECTING_LANGUAGE` state instead of pressing a button (e.g. they type "hindi"), detect it with Groq and set accordingly:
```python
LANGUAGE_KEYWORDS = {
    "en": ["english", "eng"],
    "hi": ["hindi", "हिंदी", "हिन्दी"],
    "te": ["telugu", "తెలుగు"],
}
```

**Language change mid-conversation**: Detect these phrases in ANY state:
```python
CHANGE_LANGUAGE_TRIGGERS = [
    "change language", "language", "भाषा बदलें", "భాష మార్చు",
    "switch language", "other language"
]
```
On detection → reset state to `SELECTING_LANGUAGE`, preserve appointment context in session so booking isn't lost.

### 10.2 Response Templates (multilingual)

**Language picker message** (always sent in all 3 languages simultaneously — user sees all):
```python
LANGUAGE_PICKER = (
    "Welcome to {hospital_name} 🏥\n"
    "नमस्ते | నమస్కారం\n\n"
    "Please select your language:\n"
    "अपनी भाषा चुनें | మీ భాష ఎంచుకోండి"
)
# Buttons: ["🇬🇧 English", "🇮🇳 हिंदी", "🌐 తెలుగు"]
```

**Post-language-selection greeting + disclaimer** (in chosen language):
```python
GREETINGS = {
    "en": (
        "Great! I'll assist you in English. 😊\n\n"
        "⚠️ *Disclaimer*: I'm an AI scheduling assistant for {hospital_name}. "
        "I do *not* provide medical advice. For emergencies, call {emergency_number}.\n\n"
        "How can I help you today?"
    ),
    "hi": (
        "बढ़िया! मैं आपकी हिंदी में मदद करूंगा। 😊\n\n"
        "⚠️ *सूचना*: मैं {hospital_name} का AI सहायक हूं। "
        "मैं चिकित्सा सलाह *नहीं* देता। आपातकाल के लिए {emergency_number} पर कॉल करें।\n\n"
        "आज मैं आपकी कैसे मदद कर सकता हूं?"
    ),
    "te": (
        "బాగుంది! నేను మీకు తెలుగులో సహాయం చేస్తాను। 😊\n\n"
        "⚠️ *నోటీసు*: నేను {hospital_name} AI సహాయకుడిని. "
        "నేను వైద్య సలహా *ఇవ్వను*. అత్యవసరానికి {emergency_number}కి కాల్ చేయండి।\n\n"
        "ఈరోజు నేను మీకు ఎలా సహాయం చేయగలను?"
    ),
}
```

All other user-facing strings (consent prompt, main menu, booking confirmations, error messages) must also be defined in all 3 languages in a `MESSAGES` dict keyed by language code.

---