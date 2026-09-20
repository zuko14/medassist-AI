"""Clinic-info engine: answers a patient's questions ABOUT the clinic.

Why this exists
---------------
Every intent the classifier knew was an *action* -- book, cancel, view
reports. A patient who simply asked "Where are you located?" or "What are
your timings?" had no intent to land on, so the classifier force-fit the
question into the nearest action (`view_services` / `doctor_availability`)
and the bot answered a question nobody asked -- at a diagnostics-only clinic,
the "What would you like to book?" picker. This module is the missing
destination, and `clinic_info` in ai_engine.py is the intent that reaches it.

Only real data is ever quoted
-----------------------------
Answers are built from the clinic's own config, its branches, its doctors'
bookable slots and its lab collection window. When a clinic has no data for a
topic, `answer()` returns None and the caller falls back to normal routing --
it NEVER invents a fact. An earlier version of this file shipped hardcoded
"visiting hours 4-7 PM" and "parking free for 2 hours" for every tenant;
quoting those to a diagnostic centre that has neither is worse than not
answering at all. Clinics that want extra topics (parking, insurance,
canteen) add them to `config.custom_faqs` -- see `_custom`.

Tenant isolation: every read is scoped to the clinic passed in.
"""

import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


#: Topics answerable from data every clinic actually has.
INFO_TOPICS = ("location", "hours", "contact")


def _t(lang: str, en: str, hi: str, te: str) -> str:
    return {"en": en, "hi": hi, "te": te}.get(lang, en)


# ─── Topic detection (deterministic, runs with the LLM down) ─────────────────

#: Phrases that identify an info question. Deliberately multi-word or
#: unambiguous single words: this list is consulted while a patient may be
#: mid-search in a 1,392-test catalogue, so a phrase that could plausibly be
#: part of a test, package or treatment name does not belong here. "number"
#: and "open" are absent for exactly that reason; "your number" and
#: "are you open" are safe.
TOPIC_PHRASES = {
    "location": [
        "where are you", "where is your", "where u located", "where r u",
        "your location", "your address", "the address", "full address",
        "how to reach", "how do i reach", "how to get there", "how to come",
        "directions", "located", "location", "address", "google map",
        "maps link", "which area", "landmark", "near which",
        # Hindi
        "कहां है", "कहाँ है", "पता क्या", "पता बताओ", "लोकेशन",
        "कैसे पहुंच", "कैसे आएं",
        # Telugu
        "ఎక్కడ ఉన్నార", "ఎక్కడ ఉంది", "చిరునామా", "లొకేషన్", "ఎలా రావాలి",
        "ఎలా వెళ్ళాలి",
    ],
    "hours": [
        "timing", "timings", "what time do you", "what time are you",
        "what time does", "opening hour", "opening time", "open time",
        "working hour", "working time", "closing time", "close time",
        "when do you open", "when do you close", "when are you open",
        "are you open", "office hours", "clinic hours", "hospital hours",
        "lab hours", "collection time", "collection hour", "how late",
        "open on sunday", "sunday open", "open today", "open tomorrow",
        # "do you work on Sunday" is the single most common way a patient asks
        # about hours without using the word "hours" or "timing".
        "do you work on", "you work on sun", "you work on sat",
        "are you working", "do you open on", "you open on sun",
        # Hindi
        "समय क्या", "कितने बजे", "खुलते", "बंद होते", "टाइमिंग", "खुला है",
        # Telugu
        "సమయం ఏమిటి", "ఎన్ని గంటల", "టైమింగ", "ఎప్పుడు తెరుస్తార",
        "తెరిచి ఉంట",
    ],
    "contact": [
        "phone number", "contact number", "mobile number", "your number",
        "contact you", "contact details", "call you", "reach you on",
        "whatsapp number", "landline", "helpline", "customer care",
        "email id", "email address",
        # Hindi
        "फोन नंबर", "संपर्क नंबर", "आपका नंबर", "संपर्क कैसे",
        # Telugu
        "ఫోన్ నంబర్", "సంప్రదింపు నంబర్", "మీ నంబర్", "ఎలా సంప్రదించ",
    ],
}

#: A question that names a doctor is about that doctor's availability, not the
#: clinic's opening hours -- "doctor timings" must keep reaching the doctor
#: list, which is what it has always done.
_DOCTOR_WORDS = ("doctor", "dr.", "dr ", "डॉक्टर", "డాక్టర్")


def _custom(clinic: dict, key: str, lang: str) -> dict:
    """Per-clinic overrides from config: `custom_faqs` / `custom_faq_keywords`.

    A clinic adds topics this module does not model, e.g.
      {"custom_faqs": {"en": {"parking": "Free parking in the basement."}},
       "custom_faq_keywords": {"en": {"parking": ["parking", "where to park"]}}}
    """
    block = ((clinic.get("config") or {}).get(key) or {})
    return block.get(lang) or block.get("en") or {}


def detect_topic(
    message: str, clinic: Optional[dict] = None, lang: str = "en"
) -> Optional[str]:
    """The info topic a message is asking about, or None.

    Clinic-defined topics are matched first so a clinic can override or extend
    the built-ins. Returns a key of INFO_TOPICS, or a custom topic name.
    """
    msg = (message or "").strip().lower()
    if not msg:
        return None

    for topic, phrases in _custom(clinic or {}, "custom_faq_keywords", lang).items():
        for phrase in phrases or []:
            if phrase and str(phrase).lower() in msg:
                return topic

    for topic, phrases in TOPIC_PHRASES.items():
        if topic == "hours" and any(w in msg for w in _DOCTOR_WORDS):
            continue
        for phrase in phrases:
            if phrase in msg:
                return topic
    return None


# ─── Answer building (real data only) ────────────────────────────────────────

_DAY_ORDER = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def format_days(days_str: str) -> str:
    """Compact day list: "Mon,Tue,Wed,Thu,Fri,Sat" becomes "Mon-Sat".

    A non-contiguous set stays a comma list; all seven days read "All days".
    """
    days = [d.strip()[:3].title() for d in (days_str or "").split(",") if d.strip()]
    present = [d for d in _DAY_ORDER if d in days]
    if not present:
        return ""
    if len(present) == 7:
        return "All days"
    first, last = _DAY_ORDER.index(present[0]), _DAY_ORDER.index(present[-1])
    if last - first + 1 == len(present):
        return present[0] if first == last else f"{present[0]}-{present[-1]}"
    return ", ".join(present)


def _to_ampm(hhmm: str) -> str:
    """Render "17:30" as "5:30 PM"; unparseable input is returned unchanged."""
    try:
        hour, minute = (int(p) for p in str(hhmm).split(":")[:2])
    except (ValueError, TypeError):
        return str(hhmm)
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {suffix}"


async def _location_answer(clinic: dict, lang: str) -> Optional[str]:
    """Address, landmark and map link -- per branch when the clinic has them."""
    from app.services.tenant import get_clinic_contact, get_clinic_branches

    head = _t(
        lang,
        "📍 *Where to find us*",
        "📍 *हम यहाँ हैं*",
        "📍 *మమ్మల్ని ఎక్కడ కలవాలి*",
    )
    lines: list[str] = []

    branches = [b for b in await get_clinic_branches(clinic["id"]) if b.get("address")]
    if len(branches) > 1:
        for b in branches:
            lines.append(f"\n*{b.get('name') or 'Branch'}*")
            lines.append(b["address"])
            if b.get("landmark"):
                lines.append(
                    _t(lang, "Landmark: ", "लैंडमार्क: ", "ల్యాండ్‌మార్క్: ") + b["landmark"]
                )
            if b.get("maps_link"):
                lines.append(b["maps_link"])
            if b.get("phone"):
                lines.append(f"📞 {b['phone']}")
    else:
        single = branches[0] if branches else {}
        address = single.get("address") or get_clinic_contact(
            clinic, "address", settings.hospital_address
        )
        if not address:
            return None
        lines.append(address)
        landmark = single.get("landmark") or get_clinic_contact(
            clinic, "landmark", settings.hospital_landmark
        )
        if landmark:
            lines.append(
                _t(lang, "Landmark: ", "लैंडमार्क: ", "ల్యాండ్‌మార్క్: ") + landmark
            )
        maps_link = single.get("maps_link") or get_clinic_contact(
            clinic, "maps_link", settings.hospital_maps_link
        )
        if maps_link:
            lines.append(maps_link)

    if not lines:
        return None
    return head + "\n" + "\n".join(lines)


async def _hours_answer(clinic: dict, lang: str) -> Optional[str]:
    """Hours derived from what is actually bookable.

    Consultation hours come from the active doctors' own slot lists and the
    lab line from the clinic's configured collection window, so a centre that
    only collects 7-11 AM is quoted 7-11 AM. Nothing here is a constant.
    """
    from app.database import (
        get_doctors,
        get_lab_collection_window,
        format_collection_window,
    )
    from app.services.tenant import has_feature

    blocks: list[str] = []

    try:
        doctors = await get_doctors(clinic["id"])
    except Exception as e:  # an info answer must never break the conversation
        logger.warning(f"clinic_info hours: doctor lookup failed: {e}")
        doctors = []

    starts, ends, days_seen = [], [], set()
    for doc in doctors:
        for key in ("morning_slots", "evening_slots"):
            slots = doc.get(key) or []
            if isinstance(slots, list) and slots:
                starts.append(min(slots))
                ends.append(max(slots))
        for d in (doc.get("available_days") or "").split(","):
            if d.strip():
                days_seen.add(d.strip()[:3].title())
    if starts and ends:
        days = format_days(
            ",".join(
                sorted(
                    days_seen,
                    key=lambda d: _DAY_ORDER.index(d) if d in _DAY_ORDER else 9,
                )
            )
        )
        line = f"{_to_ampm(min(starts))} - {_to_ampm(max(ends))}"
        blocks.append(
            _t(lang, "🩺 *Consultation hours*", "🩺 *परामर्श समय*", "🩺 *సంప్రదింపు సమయం*")
            + f"\n{line}"
            + (f" · {days}" if days else "")
        )

    if has_feature(clinic, "lab_test_booking"):
        try:
            window = await get_lab_collection_window(clinic)
            days = format_days(window.get("days", ""))
            blocks.append(
                _t(lang, "🧪 *Sample collection*", "🧪 *सैंपल कलेक्शन*", "🧪 *శాంపిల్ సేకరణ*")
                + f"\n{format_collection_window(window)}"
                + (f" · {days}" if days else "")
            )
        except Exception as e:
            logger.warning(f"clinic_info hours: collection window lookup failed: {e}")

    if not blocks:
        return None
    return "\n\n".join(blocks)


async def _contact_answer(clinic: dict, lang: str) -> Optional[str]:
    """Reception and emergency numbers, from the clinic's own config."""
    from app.services.tenant import get_clinic_contact

    reception = (
        get_clinic_contact(clinic, "staff_phone", "")
        or get_clinic_contact(clinic, "phone", "")
        or clinic.get("whatsapp_number")
        or settings.hospital_phone
    )
    emergency = get_clinic_contact(
        clinic, "emergency_number", settings.hospital_emergency_number
    )

    lines = [
        _t(
            lang,
            "📞 *How to reach us*",
            "📞 *हमसे संपर्क करें*",
            "📞 *మమ్మల్ని సంప్రదించండి*",
        )
    ]
    if reception:
        lines.append(_t(lang, "Reception: ", "रिसेप्शन: ", "రిసెప్షన్: ") + reception)
    if emergency and emergency != reception:
        lines.append(_t(lang, "Emergency: ", "आपातकाल: ", "అత్యవసరం: ") + emergency)
    website = get_clinic_contact(clinic, "website", settings.hospital_website)
    if website:
        lines.append(website)
    return "\n".join(lines) if len(lines) > 1 else None


_BUILDERS = {
    "location": _location_answer,
    "hours": _hours_answer,
    "contact": _contact_answer,
}


async def answer(clinic: dict, topic: Optional[str], lang: str = "en") -> Optional[str]:
    """The answer for a topic, or None when this clinic has no data for it.

    `topic=None` means the classifier recognised an info question but not which
    kind -- the patient gets every topic the clinic has data for, which is
    always a better reply than guessing one and guessing wrong.
    """
    custom = _custom(clinic, "custom_faqs", lang)
    if topic and topic in custom:
        return custom[topic]

    if topic in _BUILDERS:
        try:
            return await _BUILDERS[topic](clinic, lang)
        except Exception as e:
            logger.warning(f"clinic_info: building '{topic}' answer failed: {e}")
            return None

    if topic is None:
        parts = []
        for name, build in _BUILDERS.items():
            try:
                text = await build(clinic, lang)
            except Exception as e:
                logger.warning(f"clinic_info: building '{name}' answer failed: {e}")
                text = None
            if text:
                parts.append(text)
        return "\n\n".join(parts) if parts else None

    return None


class ClinicInfoEngine:
    """Thin object wrapper kept for callers that prefer an instance."""

    detect_topic = staticmethod(detect_topic)
    answer = staticmethod(answer)
    topics = INFO_TOPICS


#: Historical name -- `faq_engine` is what docs/05-superior.md and the file
#: checklist in CLAUDE.md call this module's singleton.
faq_engine = ClinicInfoEngine()
