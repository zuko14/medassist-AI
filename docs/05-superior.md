# Phase 16: Superior Features


## 🏆 Phase 16 — Superior Features (What Makes This Beat Every Other Hospital Bot)

> Every feature below is based on a real weakness found in Medicover, Apollo, Fortis, and Manipal WhatsApp bots.
> These are not optional — they are what makes this system worth paying for.

---

### 16.1 Proactive Intelligence — Answer First, Ask Later

**The #1 mistake every hospital bot makes**: ignoring the patient's actual first message and jumping straight to "Please enter your name."

MediAssist must NEVER do this. The patient's first message is the most important signal in the entire conversation.

```python
async def handle_first_message(phone: str, message: str, lang: str) -> None:
    """
    Before showing any menu or asking any question,
    check if the patient already asked something answerable.
    """
    intent = await ai_engine.detect_intent(message)
    faq_answer = await try_answer_faq(message, lang)

    if faq_answer:
        # Answer immediately, THEN show menu
        await whatsapp.send_text(phone, faq_answer)
        await asyncio.sleep(0.5)
        await send_main_menu(phone, lang)
        return

    if intent == "view_services":
        dept = extract_department_from_message(message)
        if dept:
            await send_service_detail(phone, dept, lang)
            return

    if intent == "book_appointment":
        # Carry their context forward — skip generic menu
        await update_state(phone, "collecting_name", {
            "entry_intent": "book_appointment",
            "raw_first_message": message   # AI will re-use this for symptom mapping
        })
        await send_booking_start(phone, lang)
        return

    # Only fall back to menu if intent is genuinely unclear
    await send_main_menu(phone, lang)
```

**Result**: Patient asks "I want info about general surgery at Hitech City" → gets surgery info immediately. No name asked. No menu shown first. The conversation starts with value.

---

### 16.2 Instant FAQ Engine — No Booking Required

Most bots force patients into a booking flow even for simple questions. Build a standalone FAQ engine that answers immediately without any state change.

**`app/services/faq_engine.py`**:

```python
HOSPITAL_FAQS = {
    # Timings
    "timing|hours|open|close|working hours|time": {
        "en": "🕐 *{hospital_name} timings:*

OPD: Mon–Sat, 8 AM – 8 PM
Emergency: 24/7
Lab: 7 AM – 9 PM
Pharmacy: 24/7",
        "hi": "🕐 *{hospital_name} का समय:*

ओपीडी: सोम–शनि, सुबह 8 – रात 8
इमरजेंसी: 24/7
लैब: सुबह 7 – रात 9",
        "te": "🕐 *{hospital_name} సమయాలు:*

OPD: సోమ–శని, ఉ. 8 – రా. 8
అత్యవసర: 24/7
లాబ్: ఉ. 7 – రా. 9"
    },
    # Fees
    "fee|cost|charges|price|how much|consultation": {
        "en": "💰 *Consultation fees:*

General Medicine: ₹300
Specialist: ₹500–₹800
Senior Consultant: ₹1000+

Lab & diagnostics priced separately.",
        "hi": "💰 *परामर्श शुल्क:*

सामान्य चिकित्सा: ₹300
विशेषज्ञ: ₹500–₹800",
        "te": "💰 *పరామర్శ రుసుము:*

సాధారణ వైద్యం: ₹300
నిపుణుడు: ₹500–₹800"
    },
    # Parking
    "parking|park|vehicle|car|bike": {
        "en": "🅿️ Free parking available for patients. Entry from the east gate. 2-wheeler & 4-wheeler slots available.",
        "hi": "🅿️ मरीजों के लिए मुफ्त पार्किंग उपलब्ध है।",
        "te": "🅿️ రోగులకు ఉచిత పార్కింగ్ అందుబాటులో ఉంది."
    },
    # Insurance
    "insurance|cashless|mediclaim|tpa|policy": {
        "en": "🏦 *Cashless insurance accepted:*
Star Health, Apollo Munich, ICICI Lombard, HDFC Ergo, Medi Assist, and 50+ more.

Bring your insurance card + Aadhaar to the billing desk.",
        "hi": "🏦 कैशलेस बीमा स्वीकार किया जाता है।
स्टार हेल्थ, अपोलो म्यूनिख और 50+ और।",
        "te": "🏦 కాష్‌లెస్ బీమా అంగీకరించబడుతుంది.
స్టార్ హెల్త్, అపోలో మ్యూనిక్ మరియు 50+ మరిన్ని."
    },
    # Ambulance
    "ambulance|108|emergency number": {
        "en": "🚑 Emergency: *108* (free, 24/7)
Hospital direct: *{hospital_phone}*
We are open 24/7 for emergencies.",
        "hi": "🚑 आपातकालीन: *108* (मुफ्त, 24/7)
अस्पताल: *{hospital_phone}*",
        "te": "🚑 అత్యవసర: *108* (ఉచిత, 24/7)
ఆసుపత్రి: *{hospital_phone}*"
    },
    # Location / directions
    "location|address|directions|how to reach|where|maps": {
        "en": "📍 *{hospital_name}*
{hospital_address}

Google Maps: {hospital_maps_link}

Nearby landmark: {hospital_landmark}",
        "hi": "📍 *{hospital_name}*
{hospital_address}

गूगल मैप्स: {hospital_maps_link}",
        "te": "📍 *{hospital_name}*
{hospital_address}

గూగుల్ మ్యాప్స్: {hospital_maps_link}"
    },
    # Reports
    "report|results|test results|lab report": {
        "en": "📋 Lab reports are usually ready within 4–6 hours.
You can collect them from the lab counter or ask the front desk to WhatsApp them to you.",
        "hi": "📋 लैब रिपोर्ट आमतौर पर 4–6 घंटे में तैयार हो जाती है।",
        "te": "📋 లాబ్ రిపోర్టులు సాధారణంగా 4–6 గంటల్లో సిద్ధంగా ఉంటాయి."
    },
}

async def try_answer_faq(message: str, lang: str) -> str | None:
    msg = message.lower()
    for keywords, answers in HOSPITAL_FAQS.items():
        if any(kw in msg for kw in keywords.split("|")):
            answer = answers.get(lang, answers["en"])
            return answer.format(
                hospital_name=settings.hospital_name,
                hospital_phone=settings.hospital_phone,
                hospital_maps_link=settings.hospital_maps_link,
                hospital_address=settings.hospital_address,
                hospital_landmark=settings.hospital_landmark,
            )
    return None
```

Add `HOSPITAL_ADDRESS` and `HOSPITAL_LANDMARK` to `.env`.

FAQ answers work from ANY state — even mid-booking. If patient asks "what are your timings?" while selecting a slot, answer it inline and then re-ask the slot question.

---

### 16.3 Smart Greeting — Context-Aware, Time-Aware

Every bot sends the same robotic "Welcome! Please select an option." Replace it with something that feels human.

```python
def build_smart_greeting(patient: dict, lang: str) -> str:
    from datetime import datetime
    hour = datetime.now().hour

    # Time-based greeting
    if hour < 12:
        time_greeting = {"en": "Good morning", "hi": "सुप्रभात", "te": "శుభోదయం"}
    elif hour < 17:
        time_greeting = {"en": "Good afternoon", "hi": "नमस्ते", "te": "శుభ మధ్యాహ్నం"}
    else:
        time_greeting = {"en": "Good evening", "hi": "शुभ संध्या", "te": "శుభ సాయంత్రం"}

    greeting = time_greeting[lang]

    if patient and patient.get("name") and patient.get("visit_count", 0) > 0:
        # Returning patient — personalised
        first_name = patient["name"].split()[0]
        return {
            "en": f"{greeting}, {first_name}! 👋 Welcome back to {settings.hospital_name}.
How can I help you today?",
            "hi": f"{greeting}, {first_name}! 👋 {settings.hospital_name} में आपका स्वागत है।
आज मैं आपकी कैसे मदद कर सकता हूं?",
            "te": f"{greeting}, {first_name}! 👋 {settings.hospital_name}కి తిరిగి స్వాగతం.
ఈరోజు నేను మీకు ఎలా సహాయం చేయగలను?"
        }[lang]
    else:
        # New patient
        return {
            "en": f"{greeting}! 👋 I'm MediAssist, your virtual receptionist at {settings.hospital_name}.

⚠️ I handle scheduling — not medical advice. For emergencies: {settings.hospital_emergency_number}.",
            "hi": f"{greeting}! 👋 मैं MediAssist हूं, {settings.hospital_name} का वर्चुअल रिसेप्शनिस्ट।

⚠️ मैं शेड्यूलिंग में मदद करता हूं — चिकित्सा सलाह नहीं।",
            "te": f"{greeting}! 👋 నేను MediAssist, {settings.hospital_name} వర్చువల్ రిసెప్షనిస్ట్.

⚠️ నేను షెడ్యూలింగ్ చేస్తాను — వైద్య సలహా కాదు."
        }[lang]
```

---

### 16.4 Intelligent Symptom Follow-up

Every other bot maps symptoms to department in one shot. MediAssist asks one smart follow-up question to improve accuracy — exactly like a real receptionist would.

```python
SYMPTOM_FOLLOWUPS = {
    "fever": {
        "question": {
            "en": "How long have you had the fever?",
            "hi": "आपको बुखार कितने दिनों से है?",
            "te": "మీకు జ్వరం ఎంత కాలంగా ఉంది?"
        },
        "options": {
            "en": ["Less than 2 days", "3–5 days", "More than a week"],
            "hi": ["2 दिन से कम", "3–5 दिन", "एक हफ्ते से ज्यादा"],
            "te": ["2 రోజుల కంటే తక్కువ", "3–5 రోజులు", "ఒక వారం కంటే ఎక్కువ"]
        },
        "routing": {
            "Less than 2 days": "General Medicine",
            "3–5 days": "General Medicine",
            "More than a week": "General Medicine",  # still GM but flags for senior doc
        }
    },
    "chest pain": {
        "question": {
            "en": "Is the pain sudden and severe, or mild and ongoing?",
            "hi": "दर्द अचानक और तेज है, या हल्का और लगातार?",
            "te": "నొప్పి అకస్మాత్తుగా మరియు తీవ్రంగా ఉందా, లేదా తేలికగా మరియు కొనసాగుతుందా?"
        },
        "options": {
            "en": ["Sudden and severe", "Mild and ongoing"],
            "hi": ["अचानक और तेज", "हल्का और लगातार"],
            "te": ["అకస్మాత్తుగా తీవ్రంగా", "తేలికగా కొనసాగుతోంది"]
        },
        "routing": {
            "Sudden and severe": "EMERGENCY",     # trigger emergency flow
            "Mild and ongoing": "Cardiology"
        }
    },
    "back pain": {
        "question": {
            "en": "Where is the pain located?",
            "hi": "दर्द कहाँ है?",
            "te": "నొప్పి ఎక్కడ ఉంది?"
        },
        "options": {
            "en": ["Lower back", "Upper back / neck", "Both sides"],
            "hi": ["पीठ के निचले हिस्से", "ऊपरी पीठ / गर्दन", "दोनों तरफ"],
            "te": ["వెనుక భాగం క్రింది", "పై వెనుక / మెడ", "రెండు వైపులా"]
        },
        "routing": {
            "Lower back": "Orthopedics",
            "Upper back / neck": "Orthopedics",
            "Both sides": "Orthopedics"
        }
    }
}
```

Flow: Patient types "I have fever" → bot sends ONE follow-up question with 3 buttons → patient taps → department confirmed. Feels like talking to a knowledgeable receptionist, not a dumb menu.

Store follow-up answer in `context["symptom_detail"]` for the doctor's reference in the appointment notes.

---

### 16.5 Doctor Profiles — Rich Information

Other bots show "Dr. Arjun Reddy · Cardiologist" and nothing else. Show a proper profile.

Add columns to `doctors` table:

```sql
ALTER TABLE doctors ADD COLUMN experience_years INTEGER DEFAULT 0;
ALTER TABLE doctors ADD COLUMN qualifications VARCHAR(200);   -- "MBBS, MD, DM Cardiology"
ALTER TABLE doctors ADD COLUMN languages_spoken VARCHAR(100) DEFAULT 'English,Hindi,Telugu';
ALTER TABLE doctors ADD COLUMN rating DECIMAL(2,1) DEFAULT 4.5;
ALTER TABLE doctors ADD COLUMN next_available_date DATE;      -- cached, updated by scheduler
ALTER TABLE doctors ADD COLUMN fun_fact VARCHAR(200);         -- "Performed 500+ cardiac surgeries"
```

When showing a doctor in `SELECTING_DOCTOR`, use a WhatsApp list with rich description:

```
👨‍⚕️ Dr. Arjun Reddy
MBBS, MD, DM Cardiology · 14 yrs exp
⭐ 4.8 · Speaks: English, Hindi, Telugu
Next available: Today 5:30 PM
```

Patient can tap "View Profile" to get full details before booking — no other hospital bot does this.

---

### 16.6 Pre-Appointment Instructions

After booking is confirmed, send department-specific preparation instructions. No other hospital bot does this.

```python
PRE_APPOINTMENT_INSTRUCTIONS = {
    "General Medicine": {
        "en": "📋 *Before your appointment:*
• Carry any previous prescriptions
• Note your symptoms and duration
• Fasting not required",
        "hi": "📋 *अपॉइंटमेंट से पहले:*
• पिछले नुस्खे लेकर आएं
• लक्षण और अवधि नोट करें",
        "te": "📋 *అపాయింట్‌మెంట్ కు ముందు:*
• మునుపటి ప్రిస్క్రిప్షన్లు తీసుకు రండి"
    },
    "Cardiology": {
        "en": "📋 *Before your appointment:*
• Carry previous ECG / Echo reports if any
• List all current medications
• Avoid heavy meals 2 hours before
• Wear comfortable, loose clothing",
        "hi": "📋 *अपॉइंटमेंट से पहले:*
• पिछली ECG/Echo रिपोर्ट लाएं
• सभी दवाओं की सूची बनाएं",
        "te": "📋 *అపాయింట్‌మెంట్ కు ముందు:*
• మునుపటి ECG/Echo రిపోర్టులు తీసుకు రండి"
    },
    "Dental": {
        "en": "📋 *Before your appointment:*
• Brush and floss before coming
• Inform us of any dental allergies
• Avoid eating 1 hour before if getting extractions",
        "hi": "📋 *अपॉइंटमेंट से पहले:*
• आने से पहले ब्रश करें
• दंत एलर्जी बताएं",
        "te": "📋 *అపాయింట్‌మెంట్ కు ముందు:*
• రావడానికి ముందు బ్రష్ చేయండి"
    },
    "Pathology": {
        "en": "📋 *For blood tests:*
• Fast for 8–12 hours (water allowed)
• Come between 7–10 AM for best results
• Bring previous reports if any",
        "hi": "📋 *ब्लड टेस्ट के लिए:*
• 8–12 घंटे उपवास करें (पानी ठीक है)
• सुबह 7–10 बजे आएं",
        "te": "📋 *రక్త పరీక్షలకు:*
• 8–12 గంటలు ఉపవాసం (నీళ్ళు పర్వాలేదు)
• ఉ. 7–10 మధ్య రండి"
    },
    "Orthopedics": {
        "en": "📋 *Before your appointment:*
• Carry any X-rays or MRI scans
• Wear loose, comfortable clothing
• Note when the pain started and what makes it worse",
        "hi": "📋 *अपॉइंटमेंट से पहले:*
• एक्स-रे या MRI स्कैन लेकर आएं",
        "te": "📋 *అపాయింట్‌మెంట్ కు ముందు:*
• X-రే లేదా MRI స్కాన్లు తీసుకు రండి"
    },
}
```

Send this as a separate message 2 seconds after the booking confirmation — not bundled with it. The delay makes it feel like the receptionist remembered something helpful.

```python
# In confirm_booking():
await send_confirmation_template(phone, appointment)
await asyncio.sleep(2)
instructions = PRE_APPOINTMENT_INSTRUCTIONS.get(appointment.department)
if instructions:
    await whatsapp.send_text(phone, instructions[lang])
```

---

### 16.7 Unique Booking Reference Number

Every booking gets a short human-readable ID. Hospital staff can pull it up instantly when a patient calls.

```python
import random, string

def generate_booking_ref() -> str:
    # Format: MC-2026-XXXX  (MC = MediCare/MediAssist initials, configurable)
    prefix = settings.booking_ref_prefix   # add BOOKING_REF_PREFIX=MC to .env
    year = datetime.now().year
    suffix = ''.join(random.choices(string.digits, k=4))
    return f"{prefix}-{year}-{suffix}"
```

Add `booking_ref VARCHAR(20) UNIQUE` column to `appointments` table. Include it in the confirmation message:

```
✅ Appointment Confirmed!

📋 Ref: MC-2026-4821
👨‍⚕️ Dr. Arjun Reddy · Cardiology
📅 18 Mar 2026 · 10:00 AM
📍 {hospital_name}

Show this reference at the front desk.
Reply CANCEL to cancel.
```

Add `GET /admin/appointments?ref=MC-2026-4821` endpoint for staff to look up by reference.

---

### 16.8 Appointment History for Returning Patients

No hospital bot does this. When a returning patient opens the main menu, add an option:

```
📋 My Appointments
```

This shows their last 3 appointments:

```
📋 Your recent appointments:

1. Dr. Arjun Reddy · Cardiology
   18 Mar 2026 · ✅ Completed

2. Dr. Meena Patel · Dental
   05 Feb 2026 · ✅ Completed

3. Dr. Priya Sharma · General Medicine
   10 Jan 2026 · ❌ Cancelled

[📅 Book Again]  [🔙 Main Menu]
```

"Book Again" pre-fills their last department and doctor — they just pick a new slot.

```python
async def get_patient_history(phone: str, limit: int = 3) -> list:
    result = await db.table("appointments") \
                     .select("*") \
                     .eq("patient_phone", phone) \
                     .order("appointment_date", desc=True) \
                     .limit(limit) \
                     .execute()
    return result.data
```

Add `APPOINTMENT_HISTORY = "appointment_history"` to `ConversationState` enum.

---

### 16.9 Post-Visit Feedback Collection

Follow-up message (sent 4 hours after appointment time) includes a simple rating:

```
Hi {name}! How was your experience with Dr. Arjun today? 😊

[⭐⭐⭐⭐⭐ Excellent]
[⭐⭐⭐⭐ Good]
[⭐⭐⭐ Average]
[😞 Could be better]
```

Store ratings in `analytics_events` with `event_type = "feedback"` and `metadata = {"rating": 4, "doctor": "Dr. Arjun"}`.

Scheduler job `send_feedback_request` runs every 30 minutes:
```python
# Find appointments that ended 4 hours ago and haven't received feedback request
appointment_time_4h_ago = datetime.now() - timedelta(hours=4)
```

Update doctor's `rating` column in `doctors` table as running average.

Add `GET /admin/feedback?doctor=Dr. Arjun` to admin API — hospital management can see per-doctor satisfaction scores.

---

### 16.10 Smart Slot Recommendation

When showing slots, don't just list them neutrally. Recommend the best one with a reason.

```python
def get_slot_recommendation(slots: list[str], department: str) -> str:
    """Returns the recommended slot with a reason."""
    morning_slots = [s for s in slots if s < "13:00"]
    evening_slots = [s for s in slots if s >= "13:00"]

    recommendations = {
        "Pathology": ("morning", "Lab tests are best done in the morning on an empty stomach."),
        "Cardiology": ("morning", "Morning consultations allow time for same-day ECG/Echo if needed."),
        "Dental": ("morning", "Morning slots are recommended so anaesthesia wears off by evening."),
    }

    preferred, reason = recommendations.get(department, ("morning", "Morning slots tend to have shorter wait times."))

    if preferred == "morning" and morning_slots:
        return morning_slots[0], reason
    elif evening_slots:
        return evening_slots[0], reason
    return slots[0], None
```

Show recommendation at top of slot list:
```
💡 Recommended: 9:30 AM
   Morning slots are best for cardiac consultations.

All available slots:
• 9:30 AM  ⭐ Recommended
• 10:00 AM
• 10:30 AM
• 5:00 PM
• 5:30 PM
```

---

### 16.11 After-Hours Smart Handling

If a patient messages outside hospital hours (or on a holiday) and needs human support:

```python
def is_within_working_hours() -> bool:
    now = datetime.now()
    if now.weekday() == 6:  # Sunday
        return False
    return 9 <= now.hour < 20  # 9 AM – 8 PM

async def handle_after_hours(phone: str, lang: str, intent: str) -> None:
    if intent == "emergency":
        return  # Emergency always handled, 24/7

    if intent in ["book_appointment", "view_services", "doctor_availability"]:
        # Still handle booking — it's async anyway
        return

    if intent == "human_escalation":
        await whatsapp.send_text(phone, MESSAGES[lang]["after_hours_human"])
        # "Our staff are available Mon–Sat, 9 AM – 8 PM.
        #  I've noted your request. A staff member will contact you when we open.
        #  For emergencies: 108"
        # Log this as a pending callback in analytics_events
        await analytics.track_event(phone, "callback_requested", {"message": original_message})
```

Add `GET /admin/callbacks` endpoint — shows all patients who requested human help outside hours, with their message. Staff calls them back in the morning.

---

### 16.12 Gibberish / Unknown Intent Handling

After 2 consecutive unknown intents, offer specific help instead of repeating the menu:

```python
async def handle_unknown_intent(phone: str, lang: str, session: dict) -> None:
    count = session.get("unknown_intent_count", 0) + 1
    await update_session(phone, {"unknown_intent_count": count})

    if count == 1:
        await whatsapp.send_text(phone,
            MESSAGES[lang]["didnt_understand"])
        # "I didn't quite get that. Here's what I can help with:"
        await send_main_menu(phone, lang)

    elif count == 2:
        await whatsapp.send_text(phone,
            MESSAGES[lang]["suggest_human"])
        # "I'm having trouble understanding. Would you like to speak with our staff?"
        await whatsapp.send_interactive_buttons(phone,
            body="",
            buttons=[
                {"id": "talk_to_human", "title": "Talk to Staff"},
                {"id": "main_menu",     "title": "Main Menu"}
            ]
        )

    else:
        # 3+ unknowns — auto-escalate to human
        await escalate_to_human(phone, lang)
        await update_session(phone, {"unknown_intent_count": 0})

# Reset counter on any successful intent
# Add: await update_session(phone, {"unknown_intent_count": 0}) on every non-unknown intent
```

---

### 16.13 WhatsApp List Pagination (10-item limit fix)

WhatsApp interactive lists cap at 10 items per section. Paginate gracefully:

```python
async def send_doctor_list(phone: str, doctors: list, lang: str, page: int = 0) -> None:
    PAGE_SIZE = 8   # safe limit with buffer
    start = page * PAGE_SIZE
    page_doctors = doctors[start:start + PAGE_SIZE]
    has_more = len(doctors) > start + PAGE_SIZE

    sections = [{
        "title": MESSAGES[lang]["available_doctors"],
        "rows": [
            {
                "id": f"doc_{doc['id']}",
                "title": doc["name"][:24],   # WhatsApp title limit: 24 chars
                "description": f"{doc['specialization']} · ⭐{doc['rating']} · {doc['experience_years']}yr exp"[:72]
            }
            for doc in page_doctors
        ]
    }]

    if has_more:
        sections[0]["rows"].append({
            "id": f"more_doctors_page_{page+1}",
            "title": MESSAGES[lang]["see_more_doctors"],   # "See more doctors →"
            "description": f"{len(doctors) - start - PAGE_SIZE} more available"
        })

    await whatsapp.send_interactive_list(
        phone,
        header=MESSAGES[lang]["choose_doctor"],
        body=MESSAGES[lang]["doctor_list_body"],
        button_text=MESSAGES[lang]["select"],
        sections=sections
    )
```

---

### 16.14 Competitor Weakness Summary

This table documents exactly what each major competitor gets wrong and what MediAssist does instead.

| Weakness | Medicover | Apollo | Fortis | MediAssist |
|---|---|---|---|---|
| Ignores first message | ❌ Asks name instead | ❌ Shows menu | ❌ Shows menu | ✅ Answers it first |
| Language support | ❌ English only | ❌ English only | ❌ English only | ✅ EN/HI/TE with picker |
| Returning patient | ❌ Asks name again | ❌ Asks name again | ❌ Asks name again | ✅ Recognised, pre-filled |
| Doctor profiles | ❌ Name only | ❌ Name + dept | ❌ Name only | ✅ Rating, exp, languages |
| Pre-appointment prep | ❌ None | ❌ None | ❌ None | ✅ Dept-specific instructions |
| Booking reference | ❌ None | ❌ None | ❌ None | ✅ Human-readable ref |
| FAQ answering | ❌ None | ❌ None | ❌ None | ✅ Instant, no booking needed |
| Slot recommendation | ❌ Lists all equally | ❌ Lists all equally | ❌ None | ✅ Recommends best + reason |
| Gibberish handling | ❌ Loops forever | ❌ Loops | ❌ Crashes | ✅ 3-strike escalation |
| After-hours | ❌ No response | ❌ Generic error | ❌ No response | ✅ Callback logged + notified |
| Public holidays | ❌ Not handled | ❌ Not handled | ❌ Not handled | ✅ Named, next date shown |
| Family member booking | ❌ Not supported | ❌ Not supported | ❌ Not supported | ✅ Separate name, own name safe |
| AI down fallback | ❌ Bot crashes | ❌ Bot crashes | ❌ Bot crashes | ✅ Keyword fallback, stays up |
| Appointment history | ❌ None | ❌ None | ❌ None | ✅ Last 3 + book again |
| Post-visit feedback | ❌ None | ❌ SMS only | ❌ None | ✅ In-WhatsApp rating |
| Smart follow-up questions | ❌ None | ❌ None | ❌ None | ✅ Symptom-specific follow-up |

---

---