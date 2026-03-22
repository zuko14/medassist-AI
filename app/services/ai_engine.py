"""AI Engine for MediAssist - Intent detection and symptom mapping using Groq."""

import logging
from typing import Optional
from groq import Groq

from app.config import settings

logger = logging.getLogger(__name__)

# Initialize Groq client
groq_client = Groq(api_key=settings.groq_api_key)

# Intent detection keywords for fallback
INTENT_KEYWORDS = {
    "book_appointment": ["book", "appointment", "doctor", "slot", "visit", "consult", "fever", "pain", "cough", "ache", "बुक", "अपॉइंटमेंट", "అపాయింట్", "డాక్టర్"],
    "cancel_appointment": ["cancel", "रद्द", "రద్దు", "abort", "stop booking"],
    "reschedule_appointment": ["reschedule", "change", "move", "postpone", "shift", "बदलें", "మార్చు"],
    "view_services": ["service", "department", "speciality", "treatment", "facility", "सेवा", "సేవ"],
    "doctor_availability": ["available", "timing", "when", "schedule", "free", "उपलब्ध", "అందుబాటు"],
    "emergency": ["emergency", "dying", "bleeding", "unconscious", "accident", "heart attack", "stroke", "can't breathe", "cannot breathe", "not breathing", "overdose", "poisoning", "seizure", "fits", "paralysis", "severe chest pain", "खून बह", "बेहोश", "దెబ్బతింది", "అపస్మారం"],
    "opt_out": ["stop", "unsubscribe", "opt out", "don't message", "रुको", "ఆపు", "వద్దు"],
    "data_deletion_request": ["delete my data", "remove my information", "forget me", "erase data", "data delete"],
    "human_escalation": ["human", "staff", "agent", "person", "speak to someone", "talk to someone", "representative", "मानव", "మనిషి", "సిబ్బంది"],
    "followup_booking": ["follow up", "followup", "review", "checkup", "follow-up"],
    "greeting": ["hello", "hi", "hey", "namaste", "नमस्ते", "నమస్కారం", "good morning", "good afternoon", "good evening"],
}

# Symptom to department mapping (fallback)
SYMPTOM_DEPARTMENT_MAP = {
    # GENERAL MEDICINE — English
    "fever": ("General Medicine", False),
    "jvaram": ("General Medicine", False),
    "jvar": ("General Medicine", False),
    "cold": ("General Medicine", False),
    "cough": ("General Medicine", False),
    "headache": ("General Medicine", False),
    "vomiting": ("General Medicine", False),
    "weakness": ("General Medicine", False),
    "body pain": ("General Medicine", False),
    "fatigue": ("General Medicine", False),
    "nausea": ("General Medicine", False),
    "diarrhea": ("General Medicine", False),
    "stomach pain": ("General Medicine", False),
    # GENERAL MEDICINE — Telugu
    "జ్వరం": ("General Medicine", False),
    "జ్వర": ("General Medicine", False),
    "జలుబు": ("General Medicine", False),
    "దగ్గు": ("General Medicine", False),
    "తలనొప్పి": ("General Medicine", False),
    "వాంతులు": ("General Medicine", False),
    "నీరసం": ("General Medicine", False),
    "నొప్పి": ("General Medicine", False),
    "వికారం": ("General Medicine", False),
    "విరేచనాలు": ("General Medicine", False),
    "కడుపు నొప్పి": ("General Medicine", False),
    # GENERAL MEDICINE — Hindi
    "बुखार": ("General Medicine", False),
    "सर्दी": ("General Medicine", False),
    "खांसी": ("General Medicine", False),
    "सिरदर्द": ("General Medicine", False),
    "उल्टी": ("General Medicine", False),
    "कमजोरी": ("General Medicine", False),
    "बदन दर्द": ("General Medicine", False),
    "थकान": ("General Medicine", False),
    "पेट दर्द": ("General Medicine", False),

    # CARDIOLOGY — English
    "chest pain": ("Cardiology", True),
    "heart": ("Cardiology", True),
    "breathless": ("Cardiology", True),
    "palpitation": ("Cardiology", False),
    "irregular heartbeat": ("Cardiology", False),
    # CARDIOLOGY — Telugu
    "గుండె నొప్పి": ("Cardiology", True),
    "గుండె": ("Cardiology", True),
    "శ్వాస": ("Cardiology", True),
    "గుండె దడ": ("Cardiology", False),
    # CARDIOLOGY — Hindi
    "छाती दर्द": ("Cardiology", True),
    "दिल": ("Cardiology", True),
    "सांस": ("Cardiology", True),
    "धड़कन": ("Cardiology", False),

    # DENTAL — English
    "tooth": ("Dental", False),
    "teeth": ("Dental", False),
    "dental": ("Dental", False),
    "gum": ("Dental", False),
    "gums": ("Dental", False),
    "toothache": ("Dental", False),
    "tooth pain": ("Dental", False),
    "tooth ache": ("Dental", False),
    "cavity": ("Dental", False),
    "cavities": ("Dental", False),
    "braces": ("Dental", False),
    "root canal": ("Dental", False),
    "extraction": ("Dental", False),
    # DENTAL — Telugu
    "పళ్ళు": ("Dental", False),
    "పల్లు": ("Dental", False),
    "చిగుళ్ళు": ("Dental", False),
    "దంతం": ("Dental", False),
    # DENTAL — Hindi
    "दांत": ("Dental", False),
    "मसूड़े": ("Dental", False),
    "दंत": ("Dental", False),

    # ORTHOPEDICS — English
    "bone": ("Orthopedics", False),
    "joint": ("Orthopedics", False),
    "fracture": ("Orthopedics", True),
    "back pain": ("Orthopedics", False),
    "knee": ("Orthopedics", False),
    "shoulder": ("Orthopedics", False),
    "spine": ("Orthopedics", False),
    # ORTHOPEDICS — Telugu
    "ఎముక": ("Orthopedics", False),
    "కీళ్ళు": ("Orthopedics", False),
    "విరుగు": ("Orthopedics", True),
    "వెన్నునొప్పి": ("Orthopedics", False),
    "మోకాలు": ("Orthopedics", False),
    # ORTHOPEDICS — Hindi
    "हड्डी": ("Orthopedics", False),
    "जोड़": ("Orthopedics", False),
    "कमर दर्द": ("Orthopedics", False),
    "घुटना": ("Orthopedics", False),

    # GYNECOLOGY — English
    "pregnancy": ("Gynecology", False),
    "periods": ("Gynecology", False),
    "menstrual": ("Gynecology", False),
    "women": ("Gynecology", False),
    "ladies": ("Gynecology", False),
    # GYNECOLOGY — Telugu
    "గర్భం": ("Gynecology", False),
    "ఋతుస్రావం": ("Gynecology", False),
    "మహిళ": ("Gynecology", False),
    # GYNECOLOGY — Hindi
    "गर्भ": ("Gynecology", False),
    "मासिक": ("Gynecology", False),
    "महिला": ("Gynecology", False),

    # PEDIATRICS — English
    "child": ("Pediatrics", False),
    "baby": ("Pediatrics", False),
    "infant": ("Pediatrics", False),
    "kid": ("Pediatrics", False),
    # PEDIATRICS — Telugu
    "పిల్లలు": ("Pediatrics", False),
    "శిశువు": ("Pediatrics", False),
    "పసిపిల్లలు": ("Pediatrics", False),
    # PEDIATRICS — Hindi
    "बच्चा": ("Pediatrics", False),
    "शिशु": ("Pediatrics", False),

    # ENT — English
    "ear": ("ENT", False),
    "nose": ("ENT", False),
    "throat": ("ENT", False),
    "hearing": ("ENT", False),
    "tonsil": ("ENT", False),
    # ENT — Telugu
    "చెవి": ("ENT", False),
    "ముక్కు": ("ENT", False),
    "గొంతు": ("ENT", False),
    "చెవుడు": ("ENT", False),
    # ENT — Hindi
    "कान": ("ENT", False),
    "नाक": ("ENT", False),
    "गला": ("ENT", False),
    "टॉन्सिल": ("ENT", False),

    # DERMATOLOGY — English
    "skin": ("Dermatology", False),
    "rash": ("Dermatology", False),
    "itching": ("Dermatology", False),
    "acne": ("Dermatology", False),
    "allergy": ("Dermatology", False),
    # DERMATOLOGY — Telugu
    "చర్మం": ("Dermatology", False),
    "దద్దు": ("Dermatology", False),
    "దురద": ("Dermatology", False),
    "అలర్జీ": ("Dermatology", False),
    # DERMATOLOGY — Hindi
    "त्वचा": ("Dermatology", False),
    "खुजली": ("Dermatology", False),
    "एलर्जी": ("Dermatology", False),

    # OPHTHALMOLOGY — English
    "eyes": ("Ophthalmology", False),
    "vision": ("Ophthalmology", False),
    "eye pain": ("Ophthalmology", False),
    # OPHTHALMOLOGY — Telugu
    "కళ్ళు": ("Ophthalmology", False),
    "చూపు": ("Ophthalmology", False),
    "కంటి నొప్పి": ("Ophthalmology", False),
    # OPHTHALMOLOGY — Hindi
    "आंख": ("Ophthalmology", False),
    "नजर": ("Ophthalmology", False),
}

EMERGENCY_KEYWORDS = [
    # English
    "bleeding", "unconscious", "accident",
    "heart attack", "stroke",
    "can't breathe", "cannot breathe", "not breathing",
    "dying", "overdose", "poisoning",
    "seizure", "fits", "paralysis",
    "severe chest pain",
    # Hindi
    "खून बह", "बेहोश", "दुर्घटना",
    "हार्ट अटैक", "लकवा",
    # Telugu
    "రక్తం కారుతోంది",
    "అపస్మారం",
    "ప్రమాదం",
    "గుండె పోటు",
    "పక్షవాతం",
    "శ్వాస అందడం లేదు",
]

# System prompt for Groq
SYSTEM_PROMPT = f"""You are MediAssist, a hospital appointment scheduling assistant for {settings.hospital_name}.

You understand medical symptoms in THREE languages:
- English: fever, chest pain, tooth pain, back pain
- Telugu: జ్వరం (fever), గుండె నొప్పి (chest pain), పళ్ళు నొప్పి (tooth pain), వెన్నునొప్పి (back pain), దగ్గు (cough), జలుబు (cold), తలనొప్పి (headache)
- Hindi: बुखार (fever), छाती दर्द (chest pain), दांत दर्द (tooth pain), कमर दर्द (back pain), खांसी (cough), सर्दी (cold), सिरदर्द (headache)

STRICT RULES:
1. NEVER diagnose — only suggest departments
2. NEVER say "you have [disease]"  
3. For emergencies ONLY (heart attack, unconscious, severe bleeding) → return intent: emergency
4. Fever, cold, cough, body pain are NOT emergencies
5. Respond in the SAME language the patient used
6. Keep responses under 160 characters
"""


def keyword_intent_fallback(message: str) -> str:
    """Fallback intent detection using keywords when Groq fails."""
    msg = message.lower().strip()

    # Emergency check first — always
    for kw in EMERGENCY_KEYWORDS:
        if kw in msg:
            return "emergency"

    # Check other intents
    for intent, keywords in INTENT_KEYWORDS.items():
        if intent == "emergency":
            continue  # Already checked above
        for kw in keywords:
            if kw in msg:
                return intent

    return "unknown"


def keyword_symptom_fallback(symptom: str) -> dict:
    """Fallback symptom mapping using keyword matching."""
    symptom_lower = symptom.lower().strip()

    # Check for emergency keywords first
    is_emergency = any(kw in symptom_lower or kw in symptom for kw in EMERGENCY_KEYWORDS)

    # Find matching department
    for keyword, (dept, could_be_emergency) in SYMPTOM_DEPARTMENT_MAP.items():
        if keyword == symptom_lower or keyword in symptom_lower or keyword in symptom:
            return {
                "suggested_department": dept,
                "confidence": "high" if keyword == symptom_lower else "medium",
                "reasoning": f"Based on your mention of '{keyword}', our {dept} team may be able to help.",
                "is_emergency": is_emergency or could_be_emergency
            }

    # No match found or low confidence
    return {
        "suggested_department": "General Medicine",
        "confidence": "low",
        "reasoning": "Based on your concern, our General Medicine team is the best starting point.",
        "is_emergency": is_emergency
    }


def detect_language(message: str) -> str:
    """Detect language from message (simplified)."""
    # Hindi detection
    hindi_chars = set("अआइईउऊएऐओऔकखगघचछजझटठडढतथदधनपफबभमयरलवशषसह")
    # Telugu detection
    telugu_chars = set("అఆఇఈఉఊఋఎఏఐఒఓఔకఖగఘఙచఛజఝఞటఠడఢణతథదధనపఫబభమయరలవశషసహ")

    for char in message:
        if char in hindi_chars:
            return "hi"
        if char in telugu_chars:
            return "te"

    return "en"


async def detect_intent(message: str) -> str:
    """Detect intent using Groq with keyword fallback."""
    try:
        # Primary: Groq AI
        response = groq_client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"""Classify this patient message into exactly one intent:
book_appointment, cancel_appointment, reschedule_appointment, view_services,
doctor_availability, emergency, opt_out, data_deletion_request, human_escalation,
followup_booking, greeting, or unknown.

*NOTE*: If the user mentions a common symptom (e.g., 'fever', 'pain', 'cough'), the intent is book_appointment, NOT emergency.

Message: "{message}"

Respond with ONLY the intent name, nothing else."""}
            ],
            timeout=5,
            max_tokens=20
        )

        intent = response.choices[0].message.content.strip().lower()

        # Validate intent is in allowed list
        allowed_intents = [
            "book_appointment", "cancel_appointment", "reschedule_appointment",
            "view_services", "doctor_availability", "emergency", "opt_out",
            "data_deletion_request", "human_escalation", "followup_booking",
            "greeting", "unknown"
        ]

        if intent in allowed_intents:
            return intent

        # If Groq returns something unexpected, use fallback
        return keyword_intent_fallback(message)

    except Exception as e:
        logger.warning(f"Groq intent detection failed: {e}. Using keyword fallback.")
        return keyword_intent_fallback(message)


async def map_symptom_to_department(symptom: str) -> dict:
    """Map symptoms to department using Groq with keyword fallback."""
    # Step 1: Try keyword map first (instant, no API call)
    msg_lower = symptom.lower().strip()
    for keyword, (dept, is_emg) in SYMPTOM_DEPARTMENT_MAP.items():
        if keyword == msg_lower or keyword in msg_lower or keyword in symptom:
            return {
                "suggested_department": dept,
                "confidence": "high",
                "reasoning": f"Based on your mention of '{keyword}', our {dept} team may be able to help.",
                "is_emergency": is_emg
            }
            
    # Step 2: Only call Groq if keyword map fails
    try:
        # Primary: Groq AI
        response = groq_client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"""Given this patient symptom or concern, suggest the appropriate hospital department.

Symptom: "{symptom}"

Respond in this exact JSON format:
{{
    "suggested_department": "Department Name",
    "confidence": "high|medium|low",
    "reasoning": "Brief explanation of why this department",
    "is_emergency": true|false
}}

Departments available: General Medicine, Cardiology, Dental, Orthopedics, Gynecology, Pediatrics, Dermatology, Ophthalmology, ENT.

IMPORTANT: Do NOT diagnose. Only suggest which department may be appropriate."""}
            ],
            timeout=5,
            max_tokens=150
        )

        # Parse JSON response
        import json
        content = response.choices[0].message.content.strip()

        # Extract JSON if wrapped in code blocks
        content = content or ""
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0] if "```" in content.split("```json")[1] else content.split("```json")[1]
        elif "```" in content:
            parts = content.split("```")
            content = parts[1] if len(parts) > 1 else content

        result = json.loads(content.strip())

        # Validate required fields
        required = ["suggested_department", "confidence", "reasoning", "is_emergency"]
        if all(k in result for k in required):
            # If low confidence, fallback to keyword
            if result.get("confidence") == "low":
                return keyword_symptom_fallback(symptom)
            return result

        # If validation fails, use fallback
        return keyword_symptom_fallback(symptom)

    except Exception as e:
        logger.warning(f"Groq symptom mapping failed: {e}. Using keyword fallback.")
        return keyword_symptom_fallback(symptom)


async def generate_response(message: str, context: dict, language: str = "en") -> str:
    """Generate a contextual response using Groq."""
    try:
        lang_instruction = {
            "en": "Respond in English.",
            "hi": "Respond in Hindi (Devanagari script).",
            "te": "Respond in Telugu."
        }.get(language, "Respond in English.")

        response = groq_client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT + f"\n\n{lang_instruction}"},
                {"role": "user", "content": message}
            ],
            timeout=5,
            max_tokens=200
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.warning(f"Groq response generation failed: {e}. Using fallback.")
        # Return simple fallback messages
        fallbacks = {
            "en": "I'm here to help you book an appointment. What would you like to do?",
            "hi": "मैं आपकी अपॉइंटमेंट बुक करने में मदद करने के लिए यहां हूं। आप क्या करना चाहेंगे?",
            "te": "నేను మీ అపాయింట్‌మెంట్ బుక్ చేయడంలో సహాయం చేయడానికి ఇక్కడ ఉన్నాను. మీరు ఏమి చేయాలనుకుంటున్నారు?"
        }
        lang = language or "en"
        return fallbacks.get(lang, fallbacks["en"])
