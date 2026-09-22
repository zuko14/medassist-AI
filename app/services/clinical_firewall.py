"""Clinical Firewall for MediAssist AI.

A zero-LLM deterministic safety layer that intercepts messages requesting
medical advice, medication recommendations, or diagnoses BEFORE they reach
the OpenRouter LLM.

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

#: Nutrients and hormones a lab MEASURES as often as a pharmacy sells them.
#: "Vitamin B12", "Serum Calcium" and "Fasting Insulin" are test names at every
#: diagnostic centre, so on their own they are a catalogue search. They count
#: as a medication request only next to a dosage form, or a take-verb that is
#: not about a test ("how much insulin should I take" still blocks).
ANALYTE_NAMES = frozenset({"calcium", "zinc", "vitamin d3", "vitamin b12", "insulin"})

_DOSAGE_FORM = re.compile(
    r"\b(?:medicine|medicines|medication|medications|tablet|tablets|tab|tabs|capsule|capsules|"
    r"syrup|injection|injections|drug|drugs|dose|doses|dosage|pill|pills|supplement|"
    r"supplements|ointment|cream|remedy|remedies|mg|mcg|iu|prescri\w*)\b",
    re.IGNORECASE,
)
_TAKE_VERB = re.compile(r"\b(?:take|taking|took|eat|eating|drink|drinking)\b", re.IGNORECASE)

#: A question about a lab test, scan or package. "Which test should I take for
#: sugar" and "can I take the test on Sunday" are catalogue questions, not a
#: request for medicine -- as long as no dosage form is mentioned too.
_TEST_CONTEXT = re.compile(
    r"\b(?:test|tests|testing|scan|scans|x-?ray|mri|ct|ultrasound|usg|sonography|checkup|"
    r"check-up|profile|panel|package|packages|screening|lab)\b|टेस्ट|जांच|जाँच|పరీక్ష|టెస్ట్",
    re.IGNORECASE,
)

#: Latin-script names are matched as whole words -- a substring match read
#: "Pandey" and "lipid panel" as the antacid "pan", "genotype" as "eno", and
#: told those patients we cannot give medical advice. A trailing digit still
#: counts ("dolo650", "pan40"). Hindi/Telugu names keep the substring match:
#: their vowel signs are not word characters, so \b is unreliable there.
_ASCII_MEDICATION_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(m)
        for m in sorted(MEDICATION_NAMES - ANALYTE_NAMES, key=len, reverse=True)
        if m.isascii()
    )
    + r")(?=\d|\b)",
    re.IGNORECASE,
)
_ANALYTE_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in sorted(ANALYTE_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_NON_ASCII_MEDICATION_NAMES = tuple(m for m in MEDICATION_NAMES if not m.isascii())

#: The phrases below that are ambiguous between a medicine and a test.
_TAKE_PHRASES = frozenset({"should i take", "can i take", "what can i take for"})


def _is_test_question(message: str) -> bool:
    return bool(_TEST_CONTEXT.search(message)) and not _DOSAGE_FORM.search(message)

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

# ── PCPNDT Act, 1994: sex determination of the foetus ─────────────────────────
# Disclosing, or offering to disclose, the sex of an unborn baby is a criminal
# offence in India, for the facility as well as the doctor. Maternity and
# Women & Child hospitals (migration 082) are asked this constantly. The answer
# is always the same and must never reach the LLM.
# Precise on purpose. "Gender: Female, need a pregnancy scan" is a patient
# giving her own details and must go through, so a gender word only counts
# when it is ABOUT the baby ("sex of the baby", "baby's gender"), or when the
# message asks to know/tell it and mentions a pregnancy at all.
_BABY_WORDS = r"(?:baby|babies|foetus|fetus|unborn|child in (?:the )?womb)"
_PREGNANCY_WORDS = re.compile(
    r"\b(?:baby|babies|foetus|fetus|unborn|pregnan\w*|garbh\w*|scan|ultrasound|sonography|womb)\b",
    re.IGNORECASE,
)
_SEX_DETERMINATION_PATTERNS = [
    re.compile(r"\bsex[- ]?determination\b|\bgender[- ]?(?:determination|test|scan|prediction|reveal)\b",
               re.IGNORECASE),
    re.compile(rf"\b(?:gender|sex)\s+of\s+(?:the\s+|my\s+|our\s+|a\s+)?{_BABY_WORDS}", re.IGNORECASE),
    re.compile(rf"\b{_BABY_WORDS}(?:'s|s)?\s+(?:gender|sex)\b", re.IGNORECASE),
    # Hindi / Telugu: "boy or girl?" and "sex test"
    re.compile(r"लड़का\s*(?:है\s*)?या\s*लड़की|लड़की\s*(?:है\s*)?या\s*लड़का|लिंग\s*(?:जांच|जाँच|परीक्षण)"),
    re.compile(r"అబ్బాయా\s*అమ్మాయా|అమ్మాయా\s*అబ్బాయా|లింగ\s*నిర్ధారణ"),
]
#: Only count when the same message also mentions a pregnancy or a scan.
_SEX_DETERMINATION_IN_PREGNANCY = [
    re.compile(r"\bboy or (?:a )?girl\b|\bgirl or (?:a )?boy\b", re.IGNORECASE),
    re.compile(r"\b(?:know|tell|find out|predict|reveal|disclose|detect)\b.{0,25}\b(?:gender|sex)\b",
               re.IGNORECASE | re.DOTALL),
]

_SEX_DETERMINATION_RESPONSE = {
    "en": (
        "🚫 Finding out or disclosing the sex of an unborn baby is prohibited by law "
        "in India (PCPNDT Act, 1994). Our doctors and staff do not disclose it in any way.\n\n"
        "I can help you book a pregnancy check-up or scan. Reply *menu* to continue."
    ),
    "hi": (
        "🚫 भारत में गर्भ में शिशु का लिंग जानना या बताना कानूनन अपराध है (PCPNDT अधिनियम, 1994)। "
        "हमारे डॉक्टर और स्टाफ किसी भी तरह यह नहीं बताते।\n\n"
        "मैं आपकी गर्भावस्था जांच या स्कैन बुक करने में मदद कर सकता हूं। आगे बढ़ने के लिए *menu* लिखें।"
    ),
    "te": (
        "🚫 భారతదేశంలో గర్భంలోని శిశువు లింగాన్ని తెలుసుకోవడం లేదా చెప్పడం చట్టరీత్యా నేరం "
        "(PCPNDT చట్టం, 1994). మా డాక్టర్లు, సిబ్బంది ఏ విధంగానూ దీన్ని చెప్పరు.\n\n"
        "గర్భధారణ పరీక్ష లేదా స్కాన్ బుక్ చేయడంలో సహాయం చేయగలను. కొనసాగించడానికి *menu* అని పంపండి."
    ),
}


def is_sex_determination_request(message: str) -> bool:
    if not message:
        return False
    if any(p.search(message) for p in _SEX_DETERMINATION_PATTERNS):
        return True
    return bool(_PREGNANCY_WORDS.search(message)) and any(
        p.search(message) for p in _SEX_DETERMINATION_IN_PREGNANCY
    )


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

    # 0. PCPNDT: sex determination of the foetus. Its own answer, not the
    #    generic medical-advice one -- the patient needs to hear it is the law.
    if is_sex_determination_request(message):
        logger.info("Clinical firewall triggered: fetal sex determination request (PCPNDT)")
        return True, _SEX_DETERMINATION_RESPONSE.get(lang, _SEX_DETERMINATION_RESPONSE["en"])

    test_question = _is_test_question(message)

    # 1. Check for medication names anywhere in message
    med_hit = _ASCII_MEDICATION_PATTERN.search(message)
    med = med_hit.group(0) if med_hit else next(
        (m for m in _NON_ASCII_MEDICATION_NAMES if m in msg_lower), None
    )
    analyte = None if med else _ANALYTE_PATTERN.search(message)
    if analyte and (
        _DOSAGE_FORM.search(message) or (_TAKE_VERB.search(message) and not test_question)
    ):
        med = analyte.group(0)
    if med:
        logger.info(
            f"Clinical firewall triggered: medication keyword '{med}' detected"
        )
        return True, _build_response(lang)

    # 2. Check for diagnostic/prescription phrases
    for phrase in DIAGNOSTIC_PHRASES:
        if phrase in _TAKE_PHRASES and test_question:
            continue
        if phrase.lower() in msg_lower:
            logger.info(
                f"Clinical firewall triggered: diagnostic phrase '{phrase}' detected"
            )
            return True, _build_response(lang)

    # 3. Check regex treatment-seeking patterns. The first one ("what ...
    #    take ... for ... sugar") is also how a test question is worded.
    for i, pattern in enumerate(_TREATMENT_SEEKING_PATTERNS):
        if i == 0 and test_question:
            continue
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
