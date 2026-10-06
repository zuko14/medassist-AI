"""Deterministic, zero-LLM safety screen for every caller utterance.

Runs BEFORE understanding. The LLM cannot see, override or skip it.
  "emergency" -> approved emergency script + immediate human transfer
  "clinical"  -> NMC-safe refusal (no diagnosis / medicine / dosage) + offer a doctor
  None        -> normal flow
Reuses the WhatsApp firewall (app/services/clinical_firewall.py) and emergency
keywords (app/services/ai_engine.EMERGENCY_KEYWORDS) unchanged, and adds the
spoken Telugu / Hindi phrasings a caller actually uses. The list must be
reviewed by the hospital's medical lead before go-live (STATUS gate G-MED-REVIEW).
"""

from typing import Optional

from app.services.ai_engine import EMERGENCY_KEYWORDS
from app.services.clinical_firewall import screen_message

from .dates import has_any, norm

SAFETY_VERSION = "safety-2026.10.06"

# Phrases that are an emergency on their own.
VOICE_EMERGENCY_PHRASES = (
    "chest pain", "can't breathe", "cannot breathe", "not breathing", "unconscious", "fainted",
    "heavy bleeding", "suicide", "kill myself", "end my life", "stroke", "paralysis", "seizure",
    "స్పృహ తప్పి", "స్పృహ లేదు", "ఆత్మహత్య", "చనిపోవాలని", "రక్తం ఆగట్లేదు", "రక్తం ఆగడం లేదు",
    "बेहोश", "आत्महत्या", "खून बंद नहीं", "सांस नहीं आ", "दौरा पड़",
)
# (body-part words, symptom words): both present in one utterance = emergency.
# "నాకు ఛాతీలో బాగా నొప్పిగా ఉంది" has the words apart, so phrase matching misses it.
_PAIRS = (
    (("ఛాతీ", "chathi", "chaathi", "chest", "छाती", "सीने", "seene", "seena"),
     ("నొప్పి", "noppi", "pain", "दर्द", "dard")),
    (("ఊపిరి", "oopiri", "upiri", "సాస", "सांस", "saans", "breath", "breathe"),
     ("ఆడటం లేదు", "ఆడట్లేదు", "రావట్లేదు", "aadatledu", "ravatledu", "नहीं", "nahi", "can't", "cannot",
      "not")),
)


def screen(text: str, lang: str = "en") -> Optional[str]:
    t = norm(text)
    if not t:
        return None
    if has_any(t, VOICE_EMERGENCY_PHRASES) or has_any(t, tuple(EMERGENCY_KEYWORDS)):
        return "emergency"
    for parts, symptoms in _PAIRS:
        if has_any(t, parts) and has_any(t, symptoms):
            return "emergency"
    blocked, _ = screen_message(text, (lang or "en").split("-")[0])
    return "clinical" if blocked else None
