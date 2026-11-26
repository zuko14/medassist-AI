"""Self-harm signal on WhatsApp → crisis card (latent fix CC-L1, owner decision O-8; docs/continuous-care/AUDIT.md A31).

Zero-LLM and deterministic: a fixed phrase list in English, Hindi and Telugu (script and Latin), matched on word
boundaries so ordinary words ("diet", "die-cast", "studied") never trigger. A match from ANY conversation state gets
the fixed crisis card (Tele-MANAS 14416, the clinic's emergency number, 112) and an in-app alert to the clinic's
staff — never the message text, never the phone. With Continuous Care on, the person's enrollment also gets an urgent
case (cc_hooks.self_harm_signal). Nothing here calls a model or changes any other reply.
"""

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

TELE_MANAS = "14416"

SELF_HARM_PHRASES: tuple[str, ...] = (
    # English
    "kill myself", "killing myself", "suicide", "suicidal", "end my life", "ending my life", "end it all",
    "want to die", "wanna die", "wish i was dead", "wish i were dead", "better off dead", "take my own life",
    "take my life", "self harm", "self-harm", "harm myself", "hurt myself", "cut myself", "no reason to live",
    "don't want to live", "dont want to live", "do not want to live",
    # Hindi (Devanagari, Latin)
    "आत्महत्या", "खुदकुशी", "ख़ुदकुशी", "मरना चाहता", "मरना चाहती", "जीना नहीं चाहता", "जीना नहीं चाहती",
    "अपनी जान", "aatmahatya", "atmahatya", "aatmhatya", "khudkushi", "marna chahta", "marna chahti",
    "jeena nahi chahta", "jeena nahi chahti", "jina nahi chahta", "jina nahi chahti", "apni jaan le",
    # Telugu (script, Latin)
    "ఆత్మహత్య", "చనిపోవాలని", "చచ్చిపోవాలని", "బ్రతకాలని లేదు", "బతకాలని లేదు", "chanipovalani",
    "chachipovalani", "bratakalani ledu", "batakalani ledu",
)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "").lower().replace("’", "'")
    return " ".join(t.split())


# Word boundaries for Latin text; Indic scripts have no \b-friendly word chars after combining marks, so they match
# as a substring bounded by start/end or a non-letter (space, punctuation).
_PATTERNS = tuple(
    re.compile(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])") if p.isascii()
    else re.compile(r"(?:^|[\s.,!?;:'\"()-])" + re.escape(p))
    for p in SELF_HARM_PHRASES
)


def detect(text: str) -> bool:
    t = _norm(text)
    return bool(t) and any(p.search(t) for p in _PATTERNS)


CARD = {
    "en": ("💙 You are not alone, and help is available right now.\n\n"
           "📞 Tele-MANAS (free, 24×7, confidential): *{tele}*\n"
           "🏥 {clinic}: *{emergency}*\n"
           "🚨 If you are in danger now, call *112*.\n\n"
           "Please talk to someone you trust. Our team has been told and will reach out."),
    "hi": ("💙 आप अकेले नहीं हैं, मदद अभी उपलब्ध है।\n\n"
           "📞 टेली-मानस (निःशुल्क, 24×7, गोपनीय): *{tele}*\n"
           "🏥 {clinic}: *{emergency}*\n"
           "🚨 अगर आप अभी खतरे में हैं, तो *112* पर कॉल करें।\n\n"
           "कृपया किसी भरोसेमंद व्यक्ति से बात करें। हमारी टीम को बताया गया है, वे आपसे संपर्क करेंगे।"),
    "te": ("💙 మీరు ఒంటరి కాదు, సహాయం ఇప్పుడే అందుబాటులో ఉంది.\n\n"
           "📞 టెలి-మానస్ (ఉచితం, 24×7, గోప్యం): *{tele}*\n"
           "🏥 {clinic}: *{emergency}*\n"
           "🚨 మీరు ఇప్పుడు ప్రమాదంలో ఉంటే *112* కు కాల్ చేయండి.\n\n"
           "దయచేసి మీరు నమ్మే వారితో మాట్లాడండి. మా బృందానికి తెలియజేశాం, వారు మిమ్మల్ని సంప్రదిస్తారు."),
}


def card_text(lang: str, clinic_name: str, emergency: str) -> str:
    return CARD.get(lang if lang in CARD else "en").format(tele=TELE_MANAS, clinic=clinic_name or "Hospital",
                                                          emergency=emergency or "112")


async def respond(manager, clinic: dict, phone: str, lang: str) -> None:
    """Send the crisis card and alert the clinic's staff. Each step is independent: a failed alert never stops the
    card, and a failed card is logged (the patient's message is still answered by nothing else — by design the
    caller returns after this, so the card is the reply)."""
    from app.config import settings
    from app.services.tenant import get_clinic_contact
    from app.utils.validators import mask_phone
    emergency = get_clinic_contact(clinic, "emergency_number", settings.hospital_emergency_number)
    try:
        await manager.whatsapp.send_text(clinic, phone, card_text(lang, clinic.get("name") or "", emergency),
                                         _source="crisis")
    except Exception as e:                                # noqa: BLE001
        logger.error(f"CRISIS_CARD_SEND_FAILED clinic={clinic.get('id')} phone={mask_phone(phone)}: {type(e).__name__}")
    try:
        from app.database import sb, supabase
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("admin_notifications").insert({
            "clinic_id": clinic["id"], "admin_id": None, "is_read": False,
            "title": "[URGENT] Patient may be at risk of self-harm",
            "message": f"A WhatsApp message from {mask_phone(phone)} matched the self-harm safety list. The crisis "
                       "card (Tele-MANAS 14416, emergency number) was sent. Please call the patient now."}))
    except Exception as e:                                # noqa: BLE001
        logger.error(f"CRISIS_ALERT_FAILED clinic={clinic.get('id')}: {type(e).__name__}")
    try:
        from app.services.tenant import care_access
        if care_access(clinic):
            from app.services import cc_hooks
            await cc_hooks.self_harm_signal(clinic, phone)
    except Exception as e:                                # noqa: BLE001
        logger.error(f"CRISIS_CC_CASE_FAILED clinic={clinic.get('id')}: {type(e).__name__}")
    logger.warning(f"Self-harm safety card sent clinic={clinic.get('id')} phone={mask_phone(phone)}")
