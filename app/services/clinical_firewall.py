"""Clinical Firewall for MediAssist AI.

A zero-LLM deterministic safety layer that intercepts messages requesting
medical advice, medication recommendations, or diagnoses BEFORE they reach
the Groq LLM.

This protects the hospital against National Medical Commission (NMC) liability
for AI-generated medical advice. The LLM is NEVER called for these inputs.

Screening covers:
  - Medication name requests (English, Hindi, Telugu)
  - Dosage and prescription queries
  - Diagnostic questions ("what disease do I have")
  - Treatment-seeking patterns ("what should I take for fever")
  - Prescription drug names (common Indian OTC + Rx drugs)

On trigger: Returns a safe static response redirecting to appointment booking.
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── Medication Keywords ────────────────────────────────────────────────────────
# Common Indian OTC drugs, antibiotics, and prescription medications

MEDICATION_NAMES = {
    # Antibiotics
    "antibiotic",
    "antibiotics",
    "azithromycin",
    "amoxicillin",
    "amoxyclav",
    "augmentin",
    "doxycycline",
    "ciprofloxacin",
    "metronidazole",
    "flagyl",
    "cefixime",
    "ceftriaxone",
    "levofloxacin",
    # Pain / Fever
    "paracetamol",
    "dolo",
    "crocin",
    "calpol",
    "ibuprofen",
    "combiflam",
    "brufen",
    "meftal",
    "nimesulide",
    "diclofenac",
    "voveran",
    # Antacids / GI
    "pantoprazole",
    "omeprazole",
    "pan",
    "rantac",
    "ranitidine",
    "gelusil",
    "eno",
    "digene",
    "cremaffin",
    # Steroids
    "steroid",
    "steroids",
    "prednisolone",
    "dexamethasone",
    "betamethasone",
    "cortisone",
    "hydrocortisone",
    # Diabetes
    "metformin",
    "glycomet",
    "glipizide",
    "insulin",
    "glargine",
    "januvia",
    "sitagliptin",
    "jardiance",
    # Cardiac / BP
    "aspirin",
    "ecosprin",
    "clopidogrel",
    "atorvastatin",
    "rosuvastatin",
    "amlodipine",
    "atenolol",
    "losartan",
    "telma",
    # Allergy / Cold
    "cetirizine",
    "levocetrizine",
    "loratadine",
    "chlorpheniramine",
    "allegra",
    "montair",
    "montelukast",
    # Vitamins (when asked in treatment context)
    "vitamin d3",
    "vitamin b12",
    "zinc",
    "calcium",
    "iron tablet",
    # Hindi medication terms
    "एंटीबायोटिक",
    "दवाई",
    "दवा",
    "गोली",
    "टैबलेट",
    "कैप्सूल",
    "इंजेक्शन",
    "सिरप",
    # Telugu medication terms
    "యాంటీబయోటిక్",
    "మందు",
    "మాత్ర",
    "గుళిక",
    "క్యాప్సూల్",
    "ఇంజెక్షన్",
    "సిరప్",
}

# ── Diagnostic Request Patterns ────────────────────────────────────────────────

DIAGNOSTIC_PHRASES = [
    # English — diagnosis requests
    "what disease do i have",
    "what is wrong with me",
    "what is my diagnosis",
    "diagnose me",
    "is this cancer",
    "do i have diabetes",
    "do i have covid",
    "am i diabetic",
    "is it serious",
    "what disease",
    "which disease",
    # English — medication requests
    "what medicine should i take",
    "which medicine for",
    "which tablet for",
    "what tablet for",
    "which drug for",
    "what antibiotic",
    "which antibiotic",
    "should i take",
    "can i take",
    "what can i take for",
    "dosage for",
    "dose of",
    "how many tablets",
    "how many mg",
    # English — treatment questions
    "how to cure",
    "how to treat",
    "home remedy for",
    "home treatment for",
    "natural remedy",
    "treatment for",
    "cure for",
    # Hindi — medication/diagnosis
    "कौन सी दवा",
    "क्या दवा",
    "कौन सी गोली",
    "कितनी गोली",
    "कितनी दवा",
    "कौन सा इलाज",
    "घरेलू उपाय",
    "मुझे क्या बीमारी है",
    "मेरी बीमारी क्या है",
    "क्या मुझे डायबिटीज",
    # Telugu — medication/diagnosis
    "ఏ మందు",
    "ఏ మాత్ర",
    "ఎంత మందు",
    "నాకు ఏ జబ్బు",
    "ఎలా తీసుకోవాలి",
    "ఇంట్లో చికిత్స",
    "నయం అవుతుందా",
]

# ── Regex Patterns for Treatment-Seeking ──────────────────────────────────────

_TREATMENT_SEEKING_PATTERNS = [
    # "(should|can|what) ... take/use ... for ... (symptom)"
    re.compile(
        r"\b(?:should|can|what|which)\b.{0,30}\b(?:take|use|eat|apply|drink)\b.{0,30}"
        r"\b(?:for|when|if)\b.{0,30}\b(?:pain|fever|cold|cough|headache|infection|"
        r"swelling|rash|itch|loose motion|diarrhea|vomiting|nausea|diabetes|bp|pressure|"
        r"sugar|jaundice|dengue|typhoid|malaria|flu|viral)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    # "what medicine/tablet/capsule"
    re.compile(
        r"\bwhat\b.{0,20}\b(?:medicine|tablet|tablet|capsule|syrup|drug|injection)\b",
        re.IGNORECASE,
    ),
    # "prescribe me" / "write me a prescription"
    re.compile(
        r"\b(?:prescribe|prescription|recommend me a drug|suggest me a medicine)\b",
        re.IGNORECASE,
    ),
]

# ── Safe static response templates ────────────────────────────────────────────

_SAFE_RESPONSE = {
    "en": (
        "🏥 For your safety, I cannot provide medical advice, diagnoses, or "
        "medication recommendations.\n\n"
        "⚠️ *Please consult a qualified doctor for any health concerns.*\n\n"
        "I can help you book an appointment with the right specialist right now!\n\n"
        "Would you like to book an appointment? 📋"
    ),
    "hi": (
        "🏥 आपकी सुरक्षा के लिए, मैं कोई चिकित्सा सलाह, निदान या दवा की "
        "सिफारिश नहीं दे सकता।\n\n"
        "⚠️ *कृपया किसी योग्य डॉक्टर से परामर्श करें।*\n\n"
        "मैं अभी सही विशेषज्ञ के साथ आपका अपॉइंटमेंट बुक करने में मदद कर सकता हूं!\n\n"
        "क्या आप अपॉइंटमेंट बुक करना चाहेंगे? 📋"
    ),
    "te": (
        "🏥 మీ భద్రత కోసం, నేను వైద్య సలహా, రోగ నిర్ధారణ లేదా మందుల "
        "సిఫారసులు అందించలేను.\n\n"
        "⚠️ *దయచేసి ఏ ఆరోగ్య సమస్యకైనా అర్హత కలిగిన వైద్యుడిని సంప్రదించండి.*\n\n"
        "నేను ఇప్పుడే సరైన నిపుణుడితో అపాయింట్‌మెంట్ బుక్ చేయడంలో సహాయం చేయగలను!\n\n"
        "మీరు అపాయింట్‌మెంట్ బుక్ చేయాలనుకుంటున్నారా? 📋"
    ),
}

# ── Output Scan Patterns (for LLM response validation) ────────────────────────

_DOSAGE_OUTPUT_PATTERN = re.compile(
    r"\b\d+\s*(?:mg|mcg|ml|tablet|cap|dose|daily|twice|thrice|tid|bid|od)\b",
    re.IGNORECASE,
)

_MEDICATION_OUTPUT_SNIPPET = re.compile(
    r"\b(?:"
    + "|".join(re.escape(m) for m in sorted(MEDICATION_NAMES, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)

_SAFE_OUTPUT_FALLBACK = {
    "en": (
        "I recommend consulting your doctor for guidance on this. "
        "I can help you book an appointment right now!"
    ),
    "hi": (
        "इसके लिए कृपया अपने डॉक्टर से सलाह लें। "
        "मैं आपको अभी अपॉइंटमेंट बुक करने में मदद कर सकता हूं!"
    ),
    "te": (
        "దయచేసి దీని కోసం మీ డాక్టర్‌ను సంప్రదించండి. "
        "నేను ఇప్పుడే అపాయింట్‌మెంట్ బుక్ చేయడంలో సహాయం చేయగలను!"
    ),
}


def screen_message(message: str, lang: str = "en") -> tuple[bool, Optional[str]]:
    """Screen incoming patient message for medical advice/medication requests.

    This runs BEFORE any LLM call. It is purely deterministic — no AI involved.

    Args:
        message: Raw patient message text.
        lang: Patient's language code ("en", "hi", "te").

    Returns:
        Tuple (is_blocked, response_text):
            - is_blocked=True means the message was intercepted.
              DO NOT call the LLM. Send response_text to the patient.
            - is_blocked=False means the message is safe to pass to the LLM.

    Usage:
        blocked, response = screen_message(message, lang)
        if blocked:
            await whatsapp.send_text(clinic, phone, response)
            return
        # proceed to LLM...
    """
    if not message or not message.strip():
        return False, None

    msg_lower = message.lower().strip()

    # 1. Check for medication names anywhere in message
    for med in MEDICATION_NAMES:
        if med.lower() in msg_lower:
            logger.info(
                f"Clinical firewall triggered: medication keyword '{med}' detected"
            )
            return True, _build_response(lang)

    # 2. Check for diagnostic/prescription phrases
    for phrase in DIAGNOSTIC_PHRASES:
        if phrase.lower() in msg_lower:
            logger.info(
                f"Clinical firewall triggered: diagnostic phrase '{phrase}' detected"
            )
            return True, _build_response(lang)

    # 3. Check regex treatment-seeking patterns
    for pattern in _TREATMENT_SEEKING_PATTERNS:
        if pattern.search(message):
            logger.info(
                "Clinical firewall triggered: treatment-seeking pattern matched"
            )
            return True, _build_response(lang)

    return False, None


def validate_llm_output(response: str, lang: str = "en") -> tuple[bool, str]:
    """Scan LLM output for accidental medication/dosage content.

    Secondary safety layer applied AFTER the LLM responds. If the LLM
    hallucinated medical advice despite the system prompt, this catches it.

    Args:
        response: Raw LLM-generated response text.
        lang: Patient's language for fallback message.

    Returns:
        Tuple (is_safe, final_response):
            - is_safe=True → response is clean, use as-is.
            - is_safe=False → response was unsafe, final_response is the
              safe fallback message to send instead.
    """
    if not response:
        return True, response

    # Check for dosage patterns (e.g., "500mg", "twice daily")
    if _DOSAGE_OUTPUT_PATTERN.search(response):
        logger.warning(
            "Clinical firewall: LLM output contained dosage pattern — replaced"
        )
        return False, _SAFE_OUTPUT_FALLBACK.get(lang, _SAFE_OUTPUT_FALLBACK["en"])

    # Check for medication names in output
    if _MEDICATION_OUTPUT_SNIPPET.search(response):
        logger.warning(
            "Clinical firewall: LLM output contained medication name — replaced"
        )
        return False, _SAFE_OUTPUT_FALLBACK.get(lang, _SAFE_OUTPUT_FALLBACK["en"])

    return True, response


def _build_response(lang: str) -> str:
    """Build the safe static response in the patient's language."""
    return _SAFE_RESPONSE.get(lang, _SAFE_RESPONSE["en"])
