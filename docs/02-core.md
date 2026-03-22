# Phase 3-5: AI Engine, Conversation State Machine, WhatsApp Service


## 🤖 Phase 3 — AI Engine

### 3.1 `app/services/ai_engine.py`

Build a Groq-powered intent and symptom classifier with these capabilities:

**Intent Detection** — classify incoming message into one of:
- `book_appointment`
- `view_services`
- `doctor_availability`
- `emergency`
- `cancel_appointment`
- `reschedule_appointment`
- `opt_out`
- `data_deletion_request`
- `human_escalation`
- `followup_booking`
- `greeting`
- `unknown`

**Symptom-to-Department Mapping** — given symptom text, return:
```python
{
    "suggested_department": "Cardiology",
    "confidence": "high",  # high / medium / low
    "reasoning": "Chest pain and breathlessness are cardiac symptoms",
    "is_emergency": False
}
```

**Symptom map (hardcoded fallback + AI)**:
```python
SYMPTOM_DEPARTMENT_MAP = {
    "chest pain": ("Cardiology", True),       # (department, could_be_emergency)
    "heart": ("Cardiology", True),
    "breathless": ("Cardiology", True),
    "tooth": ("Dental", False),
    "teeth": ("Dental", False),
    "dental": ("Dental", False),
    "fever": ("General Medicine", False),
    "cold": ("General Medicine", False),
    "cough": ("General Medicine", False),
    "bone": ("Orthopedics", False),
    "fracture": ("Orthopedics", True),
    "joint": ("Orthopedics", False),
    "back pain": ("Orthopedics", False),
    "pregnancy": ("Gynecology", False),
    "periods": ("Gynecology", False),
    "child": ("Pediatrics", False),
    "baby": ("Pediatrics", False),
    "skin": ("Dermatology", False),
    "eyes": ("Ophthalmology", False),
    "ear": ("ENT", False),
    "nose": ("ENT", False),
    "throat": ("ENT", False),
}
```

**Emergency Keywords** (MUST ALWAYS TRIGGER EMERGENCY FLOW):
```python
EMERGENCY_KEYWORDS = [
    "bleeding", "unconscious", "accident", "severe pain", "heart attack",
    "stroke", "can't breathe", "cannot breathe", "emergency", "urgent",
    "dying", "overdose", "poisoning", "seizure", "fits", "paralysis",
    "खून", "बेहोश", "दुर्घटना", "రక్తం", "అపస్మారం"  # Hindi/Telugu
]
```

**Groq API failure fallback (Critical — implement this)**:

Wrap EVERY Groq call in a try/except. If Groq is down or rate-limited, fall back to a fast keyword matcher. Bot stays functional — just less intelligent:

```python
async def detect_intent(message: str) -> str:
    try:
        # Primary: Groq AI
        response = groq_client.chat.completions.create(
            model=settings.groq_model,
            messages=[{"role": "user", "content": intent_prompt(message)}],
            timeout=5  # 5 second hard timeout — don't hang the webhook
        )
        return parse_intent(response)

    except Exception as e:
        logger.warning(f"Groq failed: {e}. Using keyword fallback.")
        return keyword_intent_fallback(message)

def keyword_intent_fallback(message: str) -> str:
    msg = message.lower().strip()
    # Emergency check first — always
    for kw in EMERGENCY_KEYWORDS:
        if kw in msg:
            return "emergency"
    # Opt-out
    if any(w in msg for w in ["stop", "unsubscribe", "रुको", "ఆపు"]):
        return "opt_out"
    # Booking
    if any(w in msg for w in ["book", "appointment", "doctor", "slot", "बुक", "అపాయింట్"]):
        return "book_appointment"
    # Cancel
    if any(w in msg for w in ["cancel", "रद्द", "రద్దు"]):
        return "cancel_appointment"
    # Human
    if any(w in msg for w in ["human", "staff", "agent", "person", "मानव", "మనిషి"]):
        return "human_escalation"
    # Data deletion
    if any(w in msg for w in ["delete", "remove my data", "forget me"]):
        return "data_deletion_request"
    return "unknown"
```

Same pattern for `map_symptom_to_department()` — if Groq fails, use `SYMPTOM_DEPARTMENT_MAP` dict directly.
Log every fallback with `logger.warning` so you can monitor Groq reliability.

**System Prompt for Groq** (include medical safety guards):
```
You are MediAssist, a hospital appointment scheduling assistant for {hospital_name}.

STRICT RULES — NEVER VIOLATE:
1. NEVER diagnose a patient or say they "have" any condition.
2. NEVER provide medical advice, dosage information, or treatment recommendations.
3. NEVER claim to be a human doctor or medical staff.
4. ALWAYS recommend consulting a doctor for medical questions.
5. For ANY emergency keyword, return intent=emergency immediately.
6. Keep responses under 160 characters for WhatsApp readability.
7. Be warm, professional, and reassuring — healthcare tone always.

You help patients: book appointments, find doctors, check services, and navigate the hospital.
Respond in the same language the patient used (English/Hindi/Telugu supported).
```

**Language Detection**: Detect language from message and store in `patient.language`. Respond in detected language using Groq's multilingual capability.

---


## 💬 Phase 4 — Conversation State Machine

### 4.1 States

```python
class ConversationState(str, Enum):
    IDLE = "idle"
    SELECTING_LANGUAGE = "selecting_language"      # NEW — always first for new users
    AWAITING_CONSENT = "awaiting_consent"
    MAIN_MENU = "main_menu"
    COLLECTING_NAME = "collecting_name"
    COLLECTING_SYMPTOMS = "collecting_symptoms"
    SUGGESTING_DEPARTMENT = "suggesting_department"
    SELECTING_DOCTOR = "selecting_doctor"
    SELECTING_SLOT = "selecting_slot"
    CONFIRMING_BOOKING = "confirming_booking"
    MANAGING_APPOINTMENT = "managing_appointment"
    RESCHEDULING = "rescheduling"
    EMERGENCY = "emergency"
    ESCALATED_TO_HUMAN = "escalated_to_human"
    AWAITING_DATA_DELETION = "awaiting_data_deletion"
```

### 4.1b Critical Guards (implement these BEFORE the state machine logic)

These 5 checks run at the TOP of `handle_message()`, before any state processing. Order matters.

```python
async def handle_message(phone: str, message: str, message_type: str, message_id: str) -> None:

    # ── Guard 1: Duplicate webhook delivery ──────────────────────────────────
    # Meta sometimes delivers the same message twice on network retries.
    # If we already processed this message_id, drop it silently.
    session = await get_or_create_session(phone)
    if session.get("last_processed_message_id") == message_id:
        return  # already handled, do nothing
    await update_session(phone, {"last_processed_message_id": message_id})

    # ── Guard 2: Session timeout mid-booking ─────────────────────────────────
    # If patient was mid-booking and went silent for 30+ minutes, reset them.
    booking_expires = session.get("booking_context_expires_at")
    mid_booking_states = [
        "collecting_name", "collecting_symptoms", "suggesting_department",
        "selecting_doctor", "selecting_slot", "confirming_booking"
    ]
    if (booking_expires and
        session["state"] in mid_booking_states and
        datetime.utcnow() > datetime.fromisoformat(booking_expires)):
        await update_state(phone, "main_menu", {"context": {}})
        await whatsapp.send_text(phone,
            MESSAGES[lang]["session_timeout"])  # "Your session timed out. Here's the main menu."
        await send_main_menu(phone, lang)
        return

    # Reset booking timer on every message while mid-booking
    if session["state"] in mid_booking_states:
        expires = (datetime.utcnow() + timedelta(minutes=30)).isoformat()
        await update_session(phone, {"booking_context_expires_at": expires})

    # ── Guard 3: Groq API failure fallback ───────────────────────────────────
    # Handled inside ai_engine.py — see Phase 3 notes.
    # ConversationManager receives intent regardless; never crashes here.

    # ── Guard 4: Family member booking flag ──────────────────────────────────
    # Handled inside COLLECTING_NAME state — see flow logic below.

    # ── Guard 5: Concurrent booking protection ───────────────────────────────
    # If patient types "book appointment" while already mid-booking, ask them.
    intent = await ai_engine.detect_intent(message)
    if (intent == "book_appointment" and
        session["state"] in mid_booking_states):
        await whatsapp.send_interactive_buttons(phone,
            body=MESSAGES[lang]["already_booking"],
            # "You're already booking with Dr. {doctor}. Continue or start over?"
            buttons=[
                {"id": "continue_booking", "title": "Continue"},
                {"id": "restart_booking",  "title": "Start Over"}
            ]
        )
        return

    # ── Continue to normal state machine below ───────────────────────────────
    await process_state(phone, message, message_type, intent, session)
```

Add these to `MESSAGES` dict for all 3 languages:
- `session_timeout` — "Your booking session timed out. Here's the main menu to start again."
- `already_booking` — "You're already booking an appointment with Dr. {doctor}. Continue that or start a new booking?"

### 4.2 `app/services/conversation.py`

Implement a `ConversationManager` class with:
- `handle_message(phone: str, message: str, message_type: str, message_id: str) -> None`
- `get_or_create_session(phone: str) -> dict`
- `update_state(phone: str, new_state: str, context_update: dict) -> None`
- `reset_session(phone: str) -> None`

**Flow Logic per State**:

**`IDLE` → First message received**:
1. Mark `opted_in = true` (they messaged us — implicit opt-in)
2. Check if `patient.language` is already set (returning user) → skip to step 4
3. Send language selection buttons (see below) → go to `SELECTING_LANGUAGE`
4. (After language set) Check if `data_consent` exists; if not, go to `AWAITING_CONSENT`
5. Show medical disclaimer in chosen language
6. Show main menu in chosen language

**`SELECTING_LANGUAGE` → Language picker (ALWAYS FIRST for new users)**:

Send this as a WhatsApp interactive button message. The message body itself must be trilingual so the user understands it regardless:

```
Message body:
"Welcome to {hospital_name} 🏥
नमस्ते | నమస్కారం

Please select your language:
अपनी भाषा चुनें | మీ భాష ఎంచుకోండి"

Buttons:
[🇬🇧 English] [🇮🇳 हिंदी] [🌐 తెలుగు]
```

On button click:
- Store `patient.language = 'en' | 'hi' | 'te'`
- Proceed to `AWAITING_CONSENT` in the chosen language

**Language can be changed anytime** — detect "change language", "भाषा बदलें", "భాష మార్చు" in any state → reset to `SELECTING_LANGUAGE`.

Also add "🌐 Change Language" as a persistent footer option in the main menu.

**`AWAITING_CONSENT`**:
- YES → set `data_consent = true`, proceed to `MAIN_MENU`
- NO → operate statelessly (session only), proceed to `MAIN_MENU`

**`MAIN_MENU`** — Interactive button message with 5 options:
1. 📅 Book Appointment
2. 🏥 Our Services
3. 👨‍⚕️ Doctor Availability
4. 🚨 Emergency Help
5. 🤝 Talk to Staff

**`COLLECTING_NAME`**:

First check if this is a returning patient with name already stored:
```python
if patient.name and patient.visit_count > 0:
    # Skip asking name — ask who they're booking for instead
    await send_interactive_buttons(phone,
        body=MESSAGES[lang]["welcome_back"].format(name=patient.name.split()[0]),
        # "Welcome back, Ravi! Who is this appointment for?"
        buttons=[
            {"id": "self",   "title": "For Me"},
            {"id": "family", "title": "For Family Member"}
        ]
    )
    update_state(phone, "collecting_name", {"for_self": None})
    return
```

If new patient OR "For Family Member" selected:
- Ask: "Please share the patient's full name."
- Validate: letters + spaces only, min 2 chars, max 60 chars
- If `for_self = True` → save to both `patients.name` AND `context["booking_name"]`
- If `for_self = False` → save ONLY to `context["booking_name"]`, never overwrite `patients.name`
  - This way the patient's own name stays correct for future visits
- Proceed to `COLLECTING_SYMPTOMS`

Family member booking flows identically from here — the name in `context["booking_name"]` is what gets written to `appointments.patient_name`.

**`COLLECTING_SYMPTOMS`**:
- Allow skip ("no symptoms", "skip", "don't know")
- If symptoms given → AI maps to department → go to `SUGGESTING_DEPARTMENT`
- If no symptoms → show department list → `SELECTING_DOCTOR`

**`SUGGESTING_DEPARTMENT`**:
- Show AI suggestion with reasoning (NOT diagnosis)
- "Based on your concern, our *{Department}* team can help. Shall I book there?"
- YES/NO buttons
- If YES → fetch doctors for that department → `SELECTING_DOCTOR`
- If NO → show full department list

**`SELECTING_DOCTOR`**:
- Show available doctors as a list message (WhatsApp list picker)
- Each row: Doctor name + specialization + fee

**`SELECTING_SLOT`**:

Only show slots that are actually free. Before rendering the slot list, query booked slots and subtract them:

```python
async def get_available_slots(doctor_name: str, date: date) -> list[str]:
    # 1. Get all slots for this doctor from doctors table
    doctor = await db.table("doctors").select("morning_slots,evening_slots,available_days") \
                     .eq("name", doctor_name).single().execute()

    day_name = date.strftime("%a")  # Mon, Tue…
    if day_name not in doctor.available_days.split(","):
        return []  # Doctor not available on this day

    all_slots = doctor.morning_slots + doctor.evening_slots  # e.g. ["09:00","09:30",...]

    # 2. Get already-booked slots for this doctor on this date
    booked = await db.table("appointments") \
                     .select("appointment_time") \
                     .eq("doctor_name", doctor_name) \
                     .eq("appointment_date", str(date)) \
                     .eq("status", "confirmed") \
                     .execute()

    booked_times = {row["appointment_time"] for row in booked.data}

    # 3. Return only free slots
    return [s for s in all_slots if s not in booked_times]
```

Show only free slots as a WhatsApp list. Never show a slot that is already taken.

**Leave check (runs before slot query)**: Before fetching slots for any date, check `doctor_leaves` first:

```python
async def get_available_slots(doctor_name: str, date: date) -> list[str]:
    from datetime import date as dt

    # Step 0a: Check if it's a hospital public holiday
    holiday = await db.table("hospital_holidays") \
                      .select("name") \
                      .eq("holiday_date", str(date)) \
                      .execute()
    if holiday.data:
        return []   # entire hospital closed — caller will tell patient the holiday name

    # Step 0b: Check if doctor is on leave this date
    leave = await db.table("doctor_leaves") \
                    .select("leave_type") \
                    .eq("doctor_name", doctor_name) \
                    .eq("leave_date", str(date)) \
                    .execute()

    if leave.data:
        leave_type = leave.data[0]["leave_type"]
        if leave_type == "full":
            return []                    # entire day blocked
        if leave_type == "half_morning":
            blocked_sessions = ["morning"]
        elif leave_type == "half_evening":
            blocked_sessions = ["evening"]
    else:
        blocked_sessions = []

    # Step 1: Get doctor's configured slots
    doc = await db.table("doctors") \
                  .select("morning_slots,evening_slots,available_days") \
                  .eq("name", doctor_name) \
                  .single() \
                  .execute()

    day_name = date.strftime("%a")   # "Mon", "Tue" etc.
    if day_name not in doc.data["available_days"]:
        return []                    # doctor doesn't work this day of week

    all_slots = []
    if "morning" not in blocked_sessions:
        all_slots += doc.data["morning_slots"]
    if "evening" not in blocked_sessions:
        all_slots += doc.data["evening_slots"]

    # Step 2: Remove already-booked slots
    booked = await db.table("appointments") \
                     .select("appointment_time") \
                     .eq("doctor_name", doctor_name) \
                     .eq("appointment_date", str(date)) \
                     .eq("status", "confirmed") \
                     .execute()

    booked_times = {row["appointment_time"] for row in booked.data}
    available = [s for s in all_slots if s not in booked_times]

    # Step 3: If today, filter out past slots (+ 30 min buffer)
    from datetime import datetime, timedelta
    if date == datetime.now().date():
        cutoff = (datetime.now() + timedelta(minutes=30)).strftime("%H:%M")
        available = [s for s in available if s > cutoff]

    return available
```

**If a date has zero free slots** (due to leave, fully booked, holiday, or past time) → skip that date and show the next available date. Scan up to 14 days forward.

The skip message varies by reason:
```python
async def find_next_available_date(doctor_name: str, from_date: date) -> tuple[date, list, str]:
    for i in range(14):
        check_date = from_date + timedelta(days=i)

        # Check holiday first (so we can name it in the message)
        holiday = await db.table("hospital_holidays") \
                          .select("name") \
                          .eq("holiday_date", str(check_date)) \
                          .execute()
        if holiday.data:
            skip_reason = f"hospital is closed for {holiday.data[0]['name']}"
            continue  # try next day, reason stored for last skip message only

        slots = await get_available_slots(doctor_name, check_date)
        if slots:
            return check_date, slots, None

    return None, [], "no_availability_14_days"
```

Skip message examples:
- Holiday: *"18 Mar is Republic Day — hospital is closed. Showing next available: 20 Mar."*
- Fully booked / leave: *"Dr. Arjun has no slots on 18 Mar. Next available: 20 Mar."*
- No slots in 14 days: falls through to "doctor fully booked" flow (suggest other doctors).

**`CONFIRMING_BOOKING`**:
- Show full summary: name, doctor, department, date, time
- Confirm / Edit buttons
- On confirm → attempt DB insert with race condition protection (see below)

### Slot Conflict Handling (implement in `app/services/appointment.py`)

#### Case 1 — Slot taken by another patient (normal conflict)

When the patient confirms, do NOT just insert. Use a **check-then-insert** pattern inside a Supabase transaction:

```python
async def book_appointment(data: AppointmentCreate) -> dict:
    # Step 1: Re-check availability at insert time (race condition guard)
    conflict = await db.table("appointments") \
                       .select("id") \
                       .eq("doctor_name", data.doctor_name) \
                       .eq("appointment_date", str(data.appointment_date)) \
                       .eq("appointment_time", str(data.appointment_time)) \
                       .eq("status", "confirmed") \
                       .execute()

    if conflict.data:
        # Slot was grabbed between patient seeing it and confirming
        return {"success": False, "reason": "slot_taken"}

    # Step 2: Insert
    result = await db.table("appointments").insert(data.dict()).execute()
    return {"success": True, "appointment": result.data[0]}
```

Also add a database-level unique constraint as a final safety net (run in Supabase SQL editor):

```sql
CREATE UNIQUE INDEX no_double_booking
ON appointments (doctor_name, appointment_date, appointment_time)
WHERE status = 'confirmed';
```

This means even if two patients confirm at the exact same millisecond, the database itself rejects the second insert. FastAPI catches the `PostgrestAPIError` and treats it as `slot_taken`.

**When `slot_taken` is returned**, the bot:
1. Does NOT change the conversation state
2. Sends this message:
```
"That slot was just booked by someone else. 
Here are the next available times with Dr. Arjun:"
[Shows 3 next free slots as buttons]
```
3. Patient picks again → loops back to `CONFIRMING_BOOKING`

#### Case 2 — Doctor fully booked for next 7 days

When `get_available_slots()` returns empty for ALL dates in the next 7 days:

```python
async def find_available_doctor_in_dept(department: str, exclude_doctor: str) -> list[dict]:
    # Fetch all active doctors in the same department except the full one
    doctors = await db.table("doctors") \
                      .select("*") \
                      .eq("department", department) \
                      .eq("is_active", True) \
                      .neq("name", exclude_doctor) \
                      .execute()

    available = []
    for doc in doctors.data:
        # Check if this doctor has at least one free slot in next 7 days
        for i in range(7):
            check_date = date.today() + timedelta(days=i+1)
            slots = await get_available_slots(doc["name"], check_date)
            if slots:
                available.append({
                    "doctor": doc["name"],
                    "specialization": doc["specialization"],
                    "next_available_date": check_date.strftime("%d %b"),
                    "next_available_slot": slots[0]
                })
                break  # Found at least one slot, no need to check more days

    return available
```

**Bot message when original doctor is fully booked**:
```
"Dr. Arjun Reddy has no available slots in the next 7 days.

Here are other Cardiologists who can see you:

👨‍⚕️ Dr. Meena Patel · Available from 19 Mar
👨‍⚕️ Dr. Suresh Nair · Available from 21 Mar"
[Each shown as a selectable button]
```
State stays at `SELECTING_DOCTOR` — patient just picks a different doctor and the flow continues normally.

#### Case 3 — Entire department has no doctors available

If `find_available_doctor_in_dept()` also returns empty (very rare — all doctors in dept fully booked):

```
"Our Cardiology team is fully booked right now.
Please call us directly to schedule: {hospital_phone}
Or check back in a day or two — slots open up regularly."
```
Then offer main menu buttons again. Do not dead-end the conversation.

#### Case 4 — Doctor on leave (full day)

`get_available_slots()` returns `[]` because of a `full` leave row. This is treated identically to a fully-booked day — the date is skipped silently and the next available date is shown. The word "leave" is never exposed to the patient (privacy). The bot just says "no availability on that date."

If the doctor is on leave for all 14 days scanned → falls through to Case 2 (suggest other doctors in department).

#### Case 5 — Doctor on half-day leave

`leave_type = 'half_morning'` → only evening slots shown for that date.
`leave_type = 'half_evening'` → only morning slots shown for that date.
Patient sees a reduced slot list with no explanation — the bot never mentions "doctor is on half-day leave." If the patient asks why fewer slots are available, reply: "Those are the available slots for that date."

#### Case 6 — Patient already has an appointment with that doctor during their leave

When the scheduler runs its daily reminder job, it must also check `doctor_leaves`:

```python
async def check_cancelled_due_to_leave():
    # Run daily at 8:00 AM
    # Find appointments where doctor has a full-day leave on that date
    tomorrow = date.today() + timedelta(days=1)
    leaves = await db.table("doctor_leaves") \
                     .select("doctor_name, leave_date") \
                     .gte("leave_date", str(tomorrow)) \
                     .lte("leave_date", str(tomorrow + timedelta(days=7))) \
                     .eq("leave_type", "full") \
                     .execute()

    for leave in leaves.data:
        affected = await db.table("appointments") \
                           .select("*") \
                           .eq("doctor_name", leave["doctor_name"]) \
                           .eq("appointment_date", leave["leave_date"]) \
                           .eq("status", "confirmed") \
                           .execute()

        for appt in affected.data:
            # 1. Update status to 'cancelled'
            await db.table("appointments").update({"status": "cancelled"}) \
                    .eq("id", appt["id"]).execute()

            # 2. Notify patient via WhatsApp template
            await whatsapp.send_template(
                appt["patient_phone"],
                "appointment_cancelled_doctor_leave",
                components_builder(appt["doctor_name"], appt["appointment_date"])
            )
            # Template body:
            # "We're sorry, your appointment with {{1}} on {{2}} has been cancelled
            #  as the doctor is unavailable. Reply REBOOK to reschedule. We apologise
            #  for the inconvenience."
```

Add `appointment_cancelled_doctor_leave` to `whatsapp_templates.py` and submit it to Meta as a UTILITY template.

When patient replies `REBOOK` → set conversation state to `SELECTING_DOCTOR` with department pre-filled → they pick a new doctor or new slot without re-entering their details.

#### How hospital staff manages leaves

Staff uses the **Supabase dashboard** directly — no custom UI needed. They open the `doctor_leaves` table and add a row:

```
doctor_name  | leave_date | leave_type    | reason
-------------|------------|---------------|----------
Dr. Arjun    | 2026-03-20 | full          | Personal
Dr. Meena    | 2026-03-21 | half_morning  | Conference
```

The bot picks this up automatically on the next slot query — no restart needed, no code change.

For hospitals that want a proper admin UI, expose these endpoints in `app/routers/admin.py`:
```
POST   /admin/leaves              → add leave (body: doctor_name, leave_date, leave_type, reason)
DELETE /admin/leaves/{id}         → remove leave (doctor back from leave early)
GET    /admin/leaves?doctor=...   → list all upcoming leaves for a doctor
```

#### Summary: slot unavailability response matrix

| Situation | What the bot does |
|---|---|
| Single slot taken (race condition) | "Slot just booked — here are 3 alternatives" (same doctor) |
| Doctor fully booked on chosen date | Skip date, show next available date |
| Doctor on full-day leave | Skip date silently (never say "on leave"), show next date |
| Doctor on half-day leave | Show only available session's slots |
| Doctor fully booked / on leave for 7 days | Suggest other doctors in same department |
| Entire department unavailable | "Please call us" + hospital phone + main menu |
| Past date chosen | Reject immediately: "Please choose a future date" |
| Today's time slot already passed | Filter out slots within 30 min from now |
| Public holiday | Name the holiday, show next available date |
| Patient's existing appt affected by leave | Auto-cancel + WhatsApp notification + REBOOK flow |
| Duplicate webhook from Meta | Check `last_processed_message_id` → drop silently |
| Mid-booking session timeout (30 min idle) | Reset to main menu with timeout message |
| Patient typing during mid-booking | Ask: "Continue current booking or start over?" |
| Groq API down | Keyword fallback — bot stays functional |
| Booking for family member | Collect name separately, don't overwrite patient's own name |
| Returning patient | Skip name collection, pre-fill from DB |

**Emergency flow** (can trigger from ANY state):
1. Detect emergency keyword → immediately override state
2. Send emergency response (template)
3. Log analytics event
4. Set state to `EMERGENCY` (do not attempt to book)

---


## 📨 Phase 5 — WhatsApp Service

### 5.1 `app/services/whatsapp.py`

Implement `WhatsAppService` class with these methods:

```python
async def send_text(phone: str, message: str) -> bool
async def send_template(phone: str, template_name: str, components: list) -> bool
async def send_interactive_buttons(phone: str, body: str, buttons: list[dict]) -> bool
async def send_interactive_list(phone: str, header: str, body: str, button_text: str, sections: list) -> bool
async def send_location(phone: str, lat: float, lng: float, name: str, address: str) -> bool
async def mark_as_read(message_id: str) -> bool
```

**Key rules in `whatsapp.py`**:
- Before every send, check `session_expires_at` in conversations table
- If session expired AND it's not a template message → log error and skip (never violate 24hr rule)
- All API calls use `httpx.AsyncClient` with timeout=10s and retry=2
- Log every outbound message with phone (last 4 digits masked), template name, status

### 5.2 `app/templates/whatsapp_templates.py`

Define ALL pre-approved templates here. These must be submitted to Meta for approval before launch:

```python
TEMPLATES = {
    # Template name: as registered in Meta Business Manager
    
    "appointment_confirmation": {
        "name": "appointment_confirmation",
        "language": "en",
        "category": "UTILITY",
        # Body: "Your appointment with {{1}} ({{2}}) is confirmed for {{3}} at {{4}}. Reply CANCEL to cancel. - {hospital_name}"
        "components_builder": lambda doctor, dept, date, time: [
            {"type": "body", "parameters": [
                {"type": "text", "text": doctor},
                {"type": "text", "text": dept},
                {"type": "text", "text": date},
                {"type": "text", "text": time},
            ]}
        ]
    },
    
    "reminder_24h": {
        "name": "appointment_reminder_24h",
        "language": "en",
        "category": "UTILITY",
        # Body: "Reminder: Your appointment with {{1}} is tomorrow at {{2}}. Please arrive 10 mins early. Reply CANCEL if you can't make it."
    },
    
    "reminder_2h": {
        "name": "appointment_reminder_2h",
        "language": "en",
        "category": "UTILITY",
        # Body: "Your appointment at {{1}} Hospital is in 2 hours with {{2}}. Reply CANCEL to cancel."
    },
    
    "followup_message": {
        "name": "post_appointment_followup",
        "language": "en",
        "category": "UTILITY",
        # Body: "Hello {{1}}, we hope you're feeling better after your visit. Would you like to book a follow-up appointment? Reply YES or call us at {{2}}."
    },
    
    "opt_out_confirmation": {
        "name": "opt_out_confirmation",
        "language": "en",
        "category": "UTILITY",
        # Body: "You've been unsubscribed from {hospital_name} WhatsApp reminders. Message us anytime to re-subscribe. For urgent help call {{1}}."
    },
    
    "data_deletion_confirmation": {
        "name": "data_deletion_confirmation",
        "language": "en",
        "category": "UTILITY",
        # Body: "Your data has been deleted from {hospital_name} systems as requested. Reference: {{1}}. For records, contact {{2}}."
    },
    
    "emergency_response": {
        "name": "emergency_response_v2",
        "language": "en",
        "category": "UTILITY",
        # Body: "⚠️ This sounds urgent. Please call {{1}} (ambulance) immediately or visit our emergency ward. Address: {{2}}"
    },
    
    "reengagement": {
        "name": "patient_reengagement",
        "language": "en",
        "category": "UTILITY",
        # Body: "Hello {{1}}, it's been a while. Your health matters to us. Would you like to schedule a checkup? Message YES to get started."
    }
}
```

> **Note for Claude Code**: Add a `# META_TEMPLATE_APPROVAL_NOTE` comment above each template explaining what to submit to Meta. All templates must be registered at business.facebook.com before going live.

---