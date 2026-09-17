"""WhatsApp flow for specialty hospitals (derma / eye / dental / ivf) — migration 077.

Functions take the ConversationManager as `manager` instead of living on it, so
conversation.py only gains routing lines and this flow can be reviewed alone.

Every entry point returns early unless specialty_enabled(clinic), which is
False for every tenant that existed before this release (see tenant.py for why
that is not has_feature()).

A treatment booking is an ordinary consultation: this module only chooses the
treatment and the doctors who perform it, then hands over to the existing
branch -> who-for -> name -> doctor -> date -> slot -> confirm -> payment flow.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import (
    get_conversation,
    get_doctors,
    get_patient_by_phone,
    get_specialty_treatments,
    get_treatment_by_id,
    get_treatment_doctor_ids,
    has_active_treatments,
    has_entry_treatment,
    log_analytics_event,
    sb,
    supabase,
    update_conversation,
)
from app.services.ai_engine import EMERGENCY_KEYWORDS, rank_treatments_for_concern
from app.services.specialty_catalog import CONCERN_EXAMPLES
from app.services.tenant import SPECIALTY_BY_PLAN, specialty_enabled

logger = logging.getLogger(__name__)

#: Context keys owned by this flow. update_state() drops them whenever the
#: patient lands in a state the treatment flow never uses, so an abandoned
#: treatment can never tag a later, unrelated booking.
TREATMENT_CONTEXT_KEYS = (
    "treatment_id",
    "treatment_name",
    "treatment_categories",
    "treatment_category",
    "treatment_page",
    "treatment_cat_page",
    "treatment_interest",
    "treatment_entry_page",
)
TREATMENT_RESET_STATES = frozenset({
    "main_menu",
    "idle",
    "selecting_department",
    "suggesting_department",
    "collecting_symptoms",
})

TREATMENT_BUTTON_IDS = frozenset({
    "menu_treatments", "menu_concern", "menu_entry_consult",
    "trtcat_more", "trt_more", "trtentry_more",
})
TREATMENT_BUTTON_PREFIXES = ("trtcat_", "trtbook_", "trtcall_", "trtexam_", "trt_")

#: migration 081. What the patient is allowed to choose, and what only the
#: doctor gets to decide.
PATHWAY_ENTRY = "entry"                       # the first visit
PATHWAY_DIRECT = "direct"                     # patient may ask for it by name
PATHWAY_ASSESSMENT_FIRST = "assessment_first"  # doctor decides after examining


def care_pathway(treatment: dict) -> str:
    """Never trust the column to be present: rows read before migration 081,
    and every clinic that has classified nothing, are 'direct'."""
    # str(): this runs while rendering the main menu and every card, so a
    # surprising value must degrade to the default rather than take the whole
    # reply down. The column is TEXT, but the dict also arrives from seeds,
    # fixtures and imports.
    value = str((treatment or {}).get("care_pathway") or "").strip().lower()
    return value if value in (PATHWAY_ENTRY, PATHWAY_DIRECT, PATHWAY_ASSESSMENT_FIRST) else PATHWAY_DIRECT


def entry_treatments(treatments: list) -> list:
    return [t for t in (treatments or []) if care_pathway(t) == PATHWAY_ENTRY]

#: Whole messages that mean "get me out of the treatment flow". Matched on the
#: EXACT message, never as a substring or via the intent classifier: a real
#: concern ("my skin is stopping me sleeping") must never read as an exit.
#: Duplicated from conversation.NAV_KEYWORDS rather than imported because
#: conversation.py imports this module.
TREATMENT_EXIT_WORDS = frozenset({
    "menu", "main menu", "home", "start over", "reset", "मेनू", "మెనూ",
    "cancel", "back", "exit", "stop", "quit",
    "hi", "hello", "hey", "book", "book appointment", "appointment",
})

CALLBACK_COOLDOWN = timedelta(hours=24)
_BODY_LIMIT = 1024
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "have", "has", "from", "since", "very", "about",
    "treatment", "problem", "issue", "doctor", "clinic", "want", "need", "what",
    "which", "some", "there", "this", "that", "please", "help", "get", "got",
})


def _t(lang: str, en: str, hi: str, te: str) -> str:
    return {"en": en, "hi": hi, "te": te}.get(lang, en)


def is_specialty_plan(clinic: Optional[dict]) -> bool:
    return (clinic or {}).get("plan") in SPECIALTY_BY_PLAN


def clear_treatment_context(context: dict) -> dict:
    for key in TREATMENT_CONTEXT_KEYS:
        context.pop(key, None)
    return context


async def treatment_menu_active(clinic: Optional[dict]) -> bool:
    """The specialty rows appear only for an enabled clinic with at least one
    active treatment. Existing plans return before any database call."""
    if not specialty_enabled(clinic) or not (clinic or {}).get("id"):
        return False
    return await has_active_treatments(clinic["id"])


def treatment_menu_rows(lang: str, has_entry: bool = False) -> list:
    """The specialty rows of the main menu.

    `has_entry` reorders them, and it is a fact about the clinic's catalogue
    rather than about its plan. An eye hospital that has published a
    "Comprehensive Eye Check-up" leads with booking it, because its patients
    arrive describing a symptom and expect to be examined -- being handed a
    procedure catalogue first is backwards. A derma clinic that classifies
    nothing keeps the order it has today, where the catalogue IS the product.
    """
    catalogue = {
        "id": "menu_treatments",
        "title": _t(lang, "✨ Our Treatments", "✨ हमारे उपचार", "✨ మా చికిత్సలు")[:24],
        "description": _t(lang, "Explore & book treatments", "उपचार देखें और बुक करें",
                          "చికిత్సలు చూసి బుక్ చేయండి")[:72],
    }
    concern = {
        "id": "menu_concern",
        "title": _t(lang, "🔍 Find by Concern", "🔍 समस्या से खोजें", "🔍 సమస్యతో వెతకండి")[:24],
        "description": _t(lang, "Describe your problem", "अपनी समस्या बताएं", "మీ సమస్య చెప్పండి")[:72],
    }
    if not has_entry:
        return [catalogue, concern]

    return [
        {
            "id": "menu_entry_consult",
            "title": _t(lang, "🩺 Book Consultation", "🩺 परामर्श बुक करें", "🩺 కన్సల్టేషన్")[:24],
            "description": _t(lang, "See a specialist first", "पहले विशेषज्ञ से मिलें",
                              "ముందు నిపుణుడిని కలవండి")[:72],
        },
        {
            **concern,
            "title": _t(lang, "🔍 Not sure? Tell us", "🔍 पता नहीं? बताएं", "🔍 తెలియదా? చెప్పండి")[:24],
            "description": _t(lang, "Describe what you feel", "आप क्या महसूस करते हैं",
                              "మీకు ఏమనిపిస్తోందో చెప్పండి")[:72],
        },
        {
            **catalogue,
            "title": _t(lang, "✨ What We Treat", "✨ हम क्या इलाज करते हैं", "✨ మేము ఏం చికిత్స")[:24],
            "description": _t(lang, "Browse our services", "हमारी सेवाएं देखें",
                              "మా సేవలు చూడండి")[:72],
        },
    ]


def _category(treatment: dict) -> str:
    return (treatment.get("category") or "").strip() or "General"


def _title(treatment: dict) -> str:
    return ((treatment.get("short_name") or treatment.get("name") or "Treatment").strip())[:24]


def localized_description(treatment: dict, lang: str) -> str:
    if lang in ("hi", "te"):
        text = (treatment.get(f"description_{lang}") or "").strip()
        if text:
            return text
    return (treatment.get("description") or "").strip()


def price_line(treatment: dict, lang: str) -> str:
    paise = int(treatment.get("price_from_paise") or 0)
    if paise <= 0:
        return _t(lang, "💰 Price: shared after consultation", "💰 कीमत: परामर्श के बाद बताई जाएगी",
                  "💰 ధర: సంప్రదింపు తర్వాత తెలియజేస్తాము")
    rupees = f"{paise // 100:,}"
    return _t(lang, f"💰 Starts from ₹{rupees}", f"💰 ₹{rupees} से शुरू", f"💰 ₹{rupees} నుండి ప్రారంభం")


def _row_description(treatment: dict, lang: str) -> str:
    desc = localized_description(treatment, lang)
    first_line = desc.split("\n", 1)[0].strip() if desc else ""
    return (first_line or price_line(treatment, lang))[:72]


def _truncate_body(text: str) -> str:
    return text if len(text) <= _BODY_LIMIT else text[: _BODY_LIMIT - 4].rstrip() + "…"


def ordered_categories(treatments: list) -> list:
    """Categories in the order the admin arranged treatments (lowest display_order first)."""
    first_order: dict = {}
    for t in treatments:
        cat = _category(t)
        order = int(t.get("display_order") or 0)
        if cat not in first_order or order < first_order[cat]:
            first_order[cat] = order
    return sorted(first_order, key=lambda c: (first_order[c], c.lower()))


def match_treatments(treatments: list, query: str) -> list:
    """Deterministic, free, instant: rank treatments by words the patient typed.

    A whole-phrase hit outranks scattered word hits; ties keep the admin's order.
    """
    q = (query or "").lower().strip()
    if not q:
        return []
    words = [w for w in re.findall(r"[^\W\d_]+", q) if len(w) >= 3 and w not in _STOPWORDS]
    scored = []
    for t in treatments:
        hay = " ".join(filter(None, [t.get("name"), t.get("short_name"), t.get("category"), t.get("concerns")])).lower()
        score = (5 if len(q) >= 3 and q in hay else 0) + sum(1 for w in words if w in hay)
        if score:
            scored.append((-score, int(t.get("display_order") or 0), (t.get("name") or "").lower(), t))
    scored.sort(key=lambda s: s[:3])
    return [s[3] for s in scored]


async def _treatment_doctors(clinic_id: str, treatment_id: str, branch_id: Optional[str] = None) -> list:
    """Active doctors who perform the treatment, at the branch when one is chosen.

    No mapping means any active doctor. get_doctors() already applies the
    doctor_branches junction when branch_id is given.
    """
    doctors = await get_doctors(clinic_id, branch_id=branch_id)
    mapped = await get_treatment_doctor_ids(clinic_id, treatment_id)
    if mapped:
        doctors = [d for d in doctors if str(d.get("id")) in mapped]
    doctors = [d for d in doctors if d.get("id") and d.get("is_active", True)]
    return sorted(doctors, key=lambda d: (d.get("name") or "").lower())


async def return_to_main_menu(manager, clinic: dict, phone: str, lang: str) -> None:
    await manager.update_state(clinic, phone, "main_menu", {"menu_shown": False})
    await manager._send_main_menu(clinic, phone, lang)


async def _send_unavailable(manager, clinic: dict, phone: str, lang: str) -> None:
    await manager.whatsapp.send_text(
        clinic,
        phone,
        _t(lang,
           "Sorry, this treatment is no longer listed. Here are our current treatments.",
           "क्षमा करें, यह उपचार अब उपलब्ध नहीं है। हमारे मौजूदा उपचार नीचे हैं।",
           "క్షమించండి, ఈ చికిత్స ఇప్పుడు అందుబాటులో లేదు. మా ప్రస్తుత చికిత్సలు ఇవి."),
    )
    await show_treatment_categories(manager, clinic, phone, lang)


# ── Browsing ─────────────────────────────────────────────────────────────────

async def show_treatment_categories(manager, clinic: dict, phone: str, lang: str, page: int = 0) -> None:
    treatments = await get_specialty_treatments(clinic["id"]) if specialty_enabled(clinic) else []
    if not treatments:
        await return_to_main_menu(manager, clinic, phone, lang)
        return

    categories = ordered_categories(treatments)
    if len(categories) == 1:
        await show_treatments_in_category(manager, clinic, phone, categories[0], lang, treatments=treatments)
        return

    counts: dict = {}
    for t in treatments:
        counts[_category(t)] = counts.get(_category(t), 0) + 1

    all_rows = [
        {
            "id": f"trtcat_{i}",
            "title": cat[:24],
            "description": _t(lang, f"{counts[cat]} treatments", f"{counts[cat]} उपचार", f"{counts[cat]} చికిత్సలు")[:72],
        }
        for i, cat in enumerate(categories)
    ]
    rows, page = manager._page_rows(all_rows, page, "trtcat_more", lang)
    await manager.whatsapp.send_interactive_list(
        clinic,
        phone,
        header=_t(lang, "Our Treatments", "हमारे उपचार", "మా చికిత్సలు")[:60],
        body=_t(lang,
                "Choose a category to see the treatments we offer.",
                "हमारे उपचार देखने के लिए एक श्रेणी चुनें।",
                "మా చికిత్సలు చూడటానికి ఒక విభాగాన్ని ఎంచుకోండి."),
        button_text=_t(lang, "View", "देखें", "చూడండి"),
        sections=[{"title": _t(lang, "Categories", "श्रेणियाँ", "విభాగాలు")[:24], "rows": rows}],
    )
    await manager.update_state(
        clinic, phone, "browsing_treatments",
        {"treatment_categories": categories, "treatment_cat_page": page},
    )


async def show_treatments_in_category(
    manager, clinic: dict, phone: str, category: str, lang: str,
    page: int = 0, treatments: Optional[list] = None,
) -> None:
    if treatments is None:
        treatments = await get_specialty_treatments(clinic["id"])
    in_category = [t for t in treatments if _category(t) == category]
    if not in_category:
        await show_treatment_categories(manager, clinic, phone, lang)
        return

    all_rows = [
        {"id": f"trt_{t['id']}", "title": _title(t), "description": _row_description(t, lang)}
        for t in in_category
    ]
    rows, page = manager._page_rows(all_rows, page, "trt_more", lang)
    await manager.whatsapp.send_interactive_list(
        clinic,
        phone,
        header=category[:60],
        body=_t(lang,
                "Tap a treatment to learn more and book.",
                "जानकारी और बुकिंग के लिए किसी उपचार पर टैप करें।",
                "వివరాలు, బుకింగ్ కోసం ఒక చికిత్సను నొక్కండి."),
        button_text=_t(lang, "View", "देखें", "చూడండి"),
        sections=[{"title": _t(lang, "Treatments", "उपचार", "చికిత్సలు")[:24], "rows": rows}],
    )
    await manager.update_state(
        clinic, phone, "browsing_treatments",
        {"treatment_category": category, "treatment_page": page},
    )


def _card_buttons(treatment: dict, lang: str) -> list:
    """The first button is the promise the card makes.

    For a doctor-decided treatment it must not say "Book Consultation" beside
    a procedure name, because the patient reads that as booking the procedure.
    It books an examination instead, and the card says so in words above.
    """
    treatment_id = str(treatment["id"])
    if care_pathway(treatment) == PATHWAY_ASSESSMENT_FIRST:
        first = {
            "id": f"trtexam_{treatment_id}",
            "title": _t(lang, "Book Examination", "जांच बुक करें", "పరీక్ష బుక్ చేయండి"),
        }
    else:
        first = {
            "id": f"trtbook_{treatment_id}",
            "title": _t(lang, "Book Consultation", "परामर्श बुक करें", "కన్సల్టేషన్ బుక్"),
        }
    return [
        first,
        {"id": f"trtcall_{treatment_id}", "title": _t(lang, "Request Callback", "कॉल बैक करें", "కాల్ బ్యాక్")},
        {"id": "menu_treatments", "title": _t(lang, "All Treatments", "सभी उपचार", "అన్ని చికిత్సలు")},
    ]


async def show_treatment_card(manager, clinic: dict, phone: str, treatment_id: str, lang: str) -> None:
    treatment = await get_treatment_by_id(clinic["id"], treatment_id)
    if not treatment:
        await _send_unavailable(manager, clinic, phone, lang)
        return

    lines = [f"✨ *{treatment['name']}*", f"_{_category(treatment)}_", ""]
    description = localized_description(treatment, lang)
    if description:
        lines += [description, ""]
    duration = treatment.get("duration_minutes")
    if duration:
        lines.append(_t(lang, f"⏱️ Visit time: about {duration} min",
                        f"⏱️ समय: लगभग {duration} मिनट", f"⏱️ సమయం: సుమారు {duration} నిమిషాలు"))
    lines.append(price_line(treatment, lang))

    doctors = await _treatment_doctors(clinic["id"], treatment["id"])
    if doctors:
        names = ", ".join(d.get("name") or "" for d in doctors[:3])
        if len(doctors) > 3:
            names += f" +{len(doctors) - 3}"
        lines.append(f"👨‍⚕️ {_t(lang, 'Specialists', 'विशेषज्ञ', 'నిపుణులు')}: {names}")

    prep = (treatment.get("prep_instructions") or "").strip()
    if prep:
        lines += ["", f"📝 *{_t(lang, 'Before your visit', 'आने से पहले', 'రావడానికి ముందు')}:* {prep[:300]}"]

    if care_pathway(treatment) == PATHWAY_ASSESSMENT_FIRST:
        # Stated plainly, above the buttons, because the button alone cannot
        # carry it: a patient reading "Cataract Surgery" with a Book button
        # believes they are booking surgery.
        lines += ["", _t(lang,
                         "🩺 *Planned by your specialist after an examination* — this is not "
                         "booked directly. Your first visit is a consultation, and the doctor "
                         "confirms whether this is right for you.",
                         "🩺 *यह विशेषज्ञ की जांच के बाद तय होता है* — इसे सीधे बुक नहीं किया जाता। "
                         "आपकी पहली विज़िट एक परामर्श है, और डॉक्टर तय करेंगे कि यह आपके लिए सही है या नहीं।",
                         "🩺 *ఇది నిపుణుల పరీక్ష తర్వాత నిర్ణయించబడుతుంది* — దీన్ని నేరుగా బుక్ చేయరు. "
                         "మీ మొదటి సందర్శన కన్సల్టేషన్, డాక్టర్ ఇది మీకు సరిపోతుందో నిర్ధారిస్తారు.")]
    else:
        lines += ["", _t(lang,
                         "_Our specialist will examine you and confirm what suits you._",
                         "_हमारे विशेषज्ञ जांच के बाद बताएंगे कि आपके लिए क्या सही है।_",
                         "_మా నిపుణులు పరీక్షించి మీకు ఏది సరిపోతుందో చెబుతారు._")]

    await manager.whatsapp.send_interactive_buttons(
        clinic,
        phone,
        body=_truncate_body("\n".join(lines)),
        buttons=_card_buttons(treatment, lang),
    )
    await manager.update_state(clinic, phone, "browsing_treatments", {})


# ── Booking hand-off ─────────────────────────────────────────────────────────

async def start_treatment_booking(
    manager, clinic: dict, phone: str, treatment_id: str, lang: str,
    interest_name: Optional[str] = None,
) -> None:
    treatment = await get_treatment_by_id(clinic["id"], treatment_id)
    if not treatment:
        await _send_unavailable(manager, clinic, phone, lang)
        return
    patient = await get_patient_by_phone(clinic["id"], phone)
    seed = {"treatment_id": str(treatment["id"]), "treatment_name": treatment["name"]}
    if interest_name:
        # The booking is the examination; this records what the patient came
        # asking about, so reception and the doctor are not guessing.
        seed["treatment_interest"] = interest_name
    await manager._start_booking(clinic, phone, patient, lang, seed_context=seed)


async def start_entry_consultation(
    manager, clinic: dict, phone: str, lang: str,
    interest: Optional[dict] = None, page: int = 0,
) -> None:
    """Book the clinic's first-visit consultation.

    Reached from "Book Consultation" on the main menu, from "Book Examination"
    on a doctor-decided card, and from "I'm not sure" in the concern search.

    `interest` is the procedure the patient was reading about. The APPOINTMENT
    is the examination -- that is the whole point -- but the interest travels
    with it so the clinic sees why the patient came, and so the booking is
    routed to a doctor who actually performs it when there is no entry
    treatment to fall back on.
    """
    treatments = await get_specialty_treatments(clinic["id"]) if specialty_enabled(clinic) else []
    entries = entry_treatments(treatments)

    if not entries:
        # No first-visit row published. Booking the treatment itself still
        # books a CONSULTATION with a doctor who performs it (a treatment is
        # only ever a tag on a consultation -- migration 077), so this is the
        # same promise, just without the dedicated examination row.
        if interest:
            await start_treatment_booking(manager, clinic, phone, str(interest["id"]), lang)
            return
        patient = await get_patient_by_phone(clinic["id"], phone)
        await manager._start_booking(clinic, phone, patient, lang)
        return

    if len(entries) == 1:
        await start_treatment_booking(
            manager, clinic, phone, str(entries[0]["id"]), lang,
            interest_name=(interest or {}).get("name"),
        )
        return

    # Several first-visit rows (an eye hospital may run a general check-up and
    # a child eye check). Ask, rather than guessing on the patient's behalf.
    if interest:
        ctx = ((await get_conversation(clinic["id"], phone)) or {}).get("context") or {}
        ctx["treatment_interest"] = interest.get("name")
        await update_conversation(clinic["id"], phone, {"context": ctx})

    rows = [
        {"id": f"trtbook_{t['id']}", "title": _title(t), "description": _row_description(t, lang)}
        for t in entries
    ]
    # Its own pager id, not "trt_more": that one means "next page of the
    # treatments in the chosen CATEGORY", and with no category set it falls
    # back to the category list -- so a tenth first-visit row would have been
    # silently unreachable.
    rows, page = manager._page_rows(rows, page, "trtentry_more", lang)
    await manager.whatsapp.send_interactive_list(
        clinic,
        phone,
        header=_t(lang, "Book a consultation", "परामर्श बुक करें", "కన్సల్టేషన్ బుక్ చేయండి")[:60],
        body=_t(lang,
                "Our specialist will examine you and explain what is needed. "
                "Which consultation would you like?",
                "हमारे विशेषज्ञ आपकी जांच करके बताएंगे कि क्या ज़रूरी है। आप कौन सा परामर्श चाहेंगे?",
                "మా నిపుణులు మిమ్మల్ని పరీక్షించి ఏమి అవసరమో చెబుతారు. మీకు ఏ కన్సల్టేషన్ కావాలి?"),
        button_text=_t(lang, "Choose", "चुनें", "ఎంచుకోండి")[:20],
        sections=[{"title": _t(lang, "Consultations", "परामर्श", "కన్సల్టేషన్లు")[:24], "rows": rows}],
    )
    await manager.update_state(
        clinic, phone, "browsing_treatments", {"treatment_entry_page": page}
    )


async def start_examination_for(manager, clinic: dict, phone: str, treatment_id: str, lang: str) -> None:
    """"Book Examination" on a doctor-decided card."""
    treatment = await get_treatment_by_id(clinic["id"], treatment_id)
    if not treatment:
        await _send_unavailable(manager, clinic, phone, lang)
        return
    await start_entry_consultation(manager, clinic, phone, lang, interest=treatment)


async def route_to_treatment_doctors(manager, clinic: dict, phone: str, context: dict, lang: str) -> bool:
    """Called where the normal flow would ask for symptoms. True = handled."""
    if not context.get("treatment_id"):
        return False
    await show_treatment_doctors(manager, clinic, phone, context, lang)
    return True


def _doctor_row_description(doctor: dict) -> str:
    parts = [p for p in [doctor.get("specialization")] if p]
    if doctor.get("consultation_fee") is not None:
        parts.append(f"₹{doctor['consultation_fee']}")
    return " · ".join(str(p) for p in parts)[:72]


async def show_treatment_doctors(manager, clinic: dict, phone: str, context: dict, lang: str, page: int = 0) -> None:
    treatment = await get_treatment_by_id(clinic["id"], context.get("treatment_id"))
    if not treatment:
        await manager.whatsapp.send_text(
            clinic, phone,
            _t(lang,
               "Sorry, this treatment is no longer listed.",
               "क्षमा करें, यह उपचार अब उपलब्ध नहीं है।",
               "క్షమించండి, ఈ చికిత్స ఇప్పుడు అందుబాటులో లేదు."),
        )
        clear_treatment_context(context)
        await return_to_main_menu(manager, clinic, phone, lang)
        return

    name = treatment["name"]
    doctors = await _treatment_doctors(clinic["id"], treatment["id"], context.get("branch_id"))
    if not doctors:
        branch = context.get("branch_name")
        where = {
            "en": f" at {branch}" if branch else "",
            "hi": f" {branch} में" if branch else "",
            "te": f" {branch}లో" if branch else "",
        }
        await manager.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=_t(lang,
                    f"Sorry, no specialist for *{name}* is available for online booking{where['en']} right now. "
                    "Tap *Request Callback* and our team will call you.",
                    f"क्षमा करें, *{name}* के लिए अभी{where['hi']} ऑनलाइन बुकिंग हेतु कोई विशेषज्ञ उपलब्ध नहीं है। "
                    "*कॉल बैक* दबाएं, हमारी टीम आपको कॉल करेगी।",
                    f"క్షమించండి, *{name}* కోసం ప్రస్తుతం{where['te']} ఆన్‌లైన్ బుకింగ్‌కు నిపుణులు అందుబాటులో లేరు. "
                    "*కాల్ బ్యాక్* నొక్కండి, మా బృందం కాల్ చేస్తుంది."),
            buttons=[
                {"id": f"trtcall_{treatment['id']}", "title": _t(lang, "Request Callback", "कॉल बैक करें", "కాల్ బ్యాక్")},
                {"id": "main_menu", "title": _t(lang, "Main Menu", "मुख्य मेनू", "ప్రధాన మెనూ")},
            ],
        )
        await manager.update_state(clinic, phone, "main_menu", {"menu_shown": False})
        return

    all_rows = [
        {"id": f"doc_{d['id']}", "title": (d.get("name") or "Doctor")[:24], "description": _doctor_row_description(d)}
        for d in doctors
    ]
    rows, page = manager._page_rows(all_rows, page, "doc_more", lang)
    await manager.whatsapp.send_interactive_list(
        clinic,
        phone,
        header=_t(lang, "Choose Your Specialist", "अपना विशेषज्ञ चुनें", "మీ నిపుణుడిని ఎంచుకోండి")[:60],
        body=_t(lang, f"Specialists for *{name}*:", f"*{name}* के विशेषज्ञ:", f"*{name}* కోసం నిపుణులు:"),
        button_text=_t(lang, "Select Doctor", "डॉक्टर चुनें", "డాక్టర్‌ ఎంచుకోండి"),
        sections=[{"title": _t(lang, "Specialists", "विशेषज्ञ", "నిపుణులు")[:24], "rows": rows}],
    )
    interest = (context.get("treatment_interest") or "").strip()
    context.update({
        "treatment_id": str(treatment["id"]),
        "treatment_name": name,
        "symptoms": f"Treatment: {name}" + (f" (asked about: {interest})" if interest else ""),
        "doctor_page": page,
    })
    await manager.update_state(clinic, phone, "selecting_doctor", context)


async def revalidate_treatment(clinic: dict, context: dict) -> None:
    """Just before the booking is written. A deleted treatment would fail the
    foreign key and lose the booking, so the tag is dropped and the patient's
    consultation goes ahead. A hidden (inactive) treatment still exists and
    keeps its tag — the patient chose it while it was listed."""
    treatment_id = context.get("treatment_id")
    if not treatment_id:
        return
    row = await get_treatment_by_id(clinic["id"], treatment_id, active_only=False)
    if not row:
        logger.warning(f"Treatment {treatment_id} vanished mid-booking for clinic {clinic.get('id')}; booking without tag")
        clear_treatment_context(context)


async def send_prep_note(whatsapp, clinic: dict, phone: str, treatment_id: Optional[str], lang: str) -> None:
    """'Before your visit' instructions after a confirmed booking. Never raises:
    the booking is already made and must not look failed."""
    if not treatment_id:
        return
    try:
        row = await get_treatment_by_id(clinic["id"], treatment_id, active_only=False)
        prep = ((row or {}).get("prep_instructions") or "").strip()
        if not prep:
            return
        header = _t(lang, "Before your visit", "आने से पहले", "రావడానికి ముందు")
        await whatsapp.send_text(clinic, phone, f"📝 *{header} — {row.get('name', '')}*\n{prep}")
    except Exception as e:
        logger.warning(f"Could not send treatment prep note to {phone[:6]}***: {e}")


# ── Callback lead ────────────────────────────────────────────────────────────

async def request_callback(manager, clinic: dict, phone: str, treatment_id: str, lang: str) -> None:
    treatment = await get_treatment_by_id(clinic["id"], treatment_id, active_only=False)
    if not treatment:
        await _send_unavailable(manager, clinic, phone, lang)
        return
    name = treatment["name"]
    key = str(treatment["id"])

    session = await get_conversation(clinic["id"], phone) or {}
    ctx = dict(session.get("context") or {})
    requested = dict(ctx.get("treatment_callbacks") or {})
    now = datetime.now(timezone.utc)
    last = requested.get(key)
    if last:
        try:
            if now - datetime.fromisoformat(last) < CALLBACK_COOLDOWN:
                await manager.whatsapp.send_text(
                    clinic, phone,
                    _t(lang,
                       f"We already have your callback request for *{name}*. Our team will call you soon.",
                       f"*{name}* के लिए आपका कॉल बैक अनुरोध हमें मिल चुका है। हमारी टीम जल्द ही कॉल करेगी।",
                       f"*{name}* కోసం మీ కాల్ బ్యాక్ అభ్యర్థన ఇప్పటికే అందింది. మా బృందం త్వరలో కాల్ చేస్తుంది."),
                )
                return
        except (TypeError, ValueError):
            pass

    patient = await get_patient_by_phone(clinic["id"], phone)
    patient_name = (patient or {}).get("name") or "Patient"

    try:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("admin_notifications").insert({
            "clinic_id": clinic["id"],
            "admin_id": None,
            "title": f"Callback request: {name}"[:120],
            "message": f"{patient_name} ({phone}) asked for a call about {name} on WhatsApp.",
            "is_read": False,
            "created_at": now.isoformat(),
        }))
    except Exception as e:
        logger.warning(f"Could not create callback notification for clinic {clinic.get('id')}: {e}")

    try:
        from app.services.payment import payment_service

        await payment_service._alert_admin(
            clinic,
            f"📞 *Callback Requested*\n\n👤 {patient_name} ({phone})\n🩺 {name}\n\nPlease call the patient.",
        )
    except Exception as e:
        logger.warning(f"Could not send callback alert for clinic {clinic.get('id')}: {e}")

    requested[key] = now.isoformat()
    ctx["treatment_callbacks"] = requested
    await update_conversation(clinic["id"], phone, {"context": ctx})
    await log_analytics_event(clinic["id"], phone, "treatment_callback_requested")

    await manager.whatsapp.send_interactive_buttons(
        clinic,
        phone,
        body=_t(lang,
                f"✅ Thank you! Our team will call you on this number about *{name}* during clinic hours.",
                f"✅ धन्यवाद! हमारी टीम क्लिनिक समय में *{name}* के बारे में इसी नंबर पर आपको कॉल करेगी।",
                f"✅ ధన్యవాదాలు! మా బృందం క్లినిక్ సమయంలో *{name}* గురించి ఈ నంబర్‌కు కాల్ చేస్తుంది."),
        buttons=[{"id": "main_menu", "title": _t(lang, "Main Menu", "मुख्य मेनू", "ప్రధాన మెనూ")}],
    )


# ── Concern search ───────────────────────────────────────────────────────────

async def prompt_concern(manager, clinic: dict, phone: str, lang: str) -> None:
    examples = CONCERN_EXAMPLES.get(SPECIALTY_BY_PLAN.get(clinic.get("plan")), "hair fall, tooth pain, blurred vision")

    # A patient who cannot name what is wrong is the normal case at an eye,
    # dental or fertility clinic -- that is the whole objection this pathway
    # answers. Offer the examination rather than insisting they describe it.
    if await has_entry_treatment(clinic["id"]):
        await manager.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=_t(lang,
                    f"🔍 Tell us what you are feeling, in a few words.\nFor example: _{examples}_\n\n"
                    "We will show what may be relevant at our clinic. Our specialist confirms "
                    "everything after examining you.\n\n"
                    "Not sure how to describe it? That is completely fine — tap below and our "
                    "specialist will examine you and explain what is needed.",
                    f"🔍 आप क्या महसूस कर रहे हैं, कुछ शब्दों में बताएं।\nउदाहरण: _{examples}_\n\n"
                    "हम बताएंगे कि हमारे क्लिनिक में क्या प्रासंगिक हो सकता है। जांच के बाद विशेषज्ञ सब कुछ तय करेंगे।\n\n"
                    "बताना मुश्किल लग रहा है? कोई बात नहीं — नीचे दबाएं, हमारे विशेषज्ञ जांच करके बताएंगे।",
                    f"🔍 మీకు ఏమనిపిస్తోందో కొన్ని మాటల్లో చెప్పండి.\nఉదాహరణ: _{examples}_\n\n"
                    "మా క్లినిక్‌లో ఏది ఉపయోగపడవచ్చో చూపిస్తాము. పరీక్ష తర్వాత నిపుణులు అన్నింటిని నిర్ధారిస్తారు.\n\n"
                    "చెప్పడం కష్టంగా ఉందా? ఫర్వాలేదు — క్రింద నొక్కండి, మా నిపుణులు పరీక్షించి చెబుతారు."),
            buttons=[{
                "id": "menu_entry_consult",
                "title": _t(lang, "Book Examination", "जांच बुक करें", "పరీక్ష బుక్ చేయండి"),
            }],
        )
        await manager.update_state(clinic, phone, "searching_treatments", {})
        return

    await manager.whatsapp.send_text(
        clinic,
        phone,
        _t(lang,
           f"🔍 Tell us your concern in a few words.\nFor example: _{examples}_\n\n"
           "We will show treatments at our clinic that may help. Our specialist confirms what suits you after an examination.",
           f"🔍 अपनी समस्या कुछ शब्दों में लिखें।\nउदाहरण: _{examples}_\n\n"
           "हम अपने क्लिनिक के ऐसे उपचार दिखाएंगे जो मदद कर सकते हैं। जांच के बाद विशेषज्ञ बताएंगे कि आपके लिए क्या सही है।",
           f"🔍 మీ సమస్యను కొన్ని మాటల్లో రాయండి.\nఉదాహరణ: _{examples}_\n\n"
           "సహాయపడగల మా క్లినిక్ చికిత్సలను చూపిస్తాము. పరీక్ష తర్వాత నిపుణులు మీకు ఏది సరిపోతుందో చెబుతారు."),
    )
    await manager.update_state(clinic, phone, "searching_treatments", {})


async def handle_treatment_search_text(manager, clinic: dict, phone: str, message: str, lang: str) -> None:
    query = (message or "").strip()[:200]
    if any(kw in query.lower() for kw in EMERGENCY_KEYWORDS):
        await manager._handle_emergency(clinic, phone, lang)
        return
    if len(query) < 3:
        await manager.whatsapp.send_text(
            clinic, phone,
            _t(lang,
               "Please type a few words about your concern, for example: hair fall.",
               "कृपया अपनी समस्या कुछ शब्दों में लिखें, जैसे: बाल झड़ना।",
               "దయచేసి మీ సమస్యను కొన్ని మాటల్లో రాయండి, ఉదా: జుట్టు రాలడం."),
        )
        return

    treatments = await get_specialty_treatments(clinic["id"]) if specialty_enabled(clinic) else []
    if not treatments:
        await return_to_main_menu(manager, clinic, phone, lang)
        return
    has_entry = bool(entry_treatments(treatments))

    matches = match_treatments(treatments, query)[:9]
    if not matches:
        by_id = {str(t["id"]): t for t in treatments}
        ranked = await rank_treatments_for_concern(query, treatments, clinic)
        matches = [by_id[i] for i in ranked if i in by_id]

    if not matches:
        # Not finding a match is not the patient's failure, and dumping a
        # category list on them implies it was. If the clinic examines first,
        # say so -- that is the answer to "I don't know what I need".
        if has_entry:
            await manager.whatsapp.send_interactive_buttons(
                clinic, phone,
                body=_t(lang,
                        "Thank you. Rather than guess from a message, the right next step is for "
                        "our specialist to examine you and explain what is needed.\n\n"
                        "_We will not suggest a treatment before a doctor has seen you._",
                        "धन्यवाद। संदेश से अनुमान लगाने के बजाय, सही अगला कदम यह है कि हमारे विशेषज्ञ "
                        "आपकी जांच करें और बताएं कि क्या ज़रूरी है।\n\n"
                        "_डॉक्टर के देखे बिना हम कोई उपचार नहीं सुझाते।_",
                        "ధన్యవాదాలు. సందేశం నుండి ఊహించే బదులు, మా నిపుణులు మిమ్మల్ని పరీక్షించి "
                        "ఏమి అవసరమో చెప్పడమే సరైన తదుపరి అడుగు.\n\n"
                        "_డాక్టర్ చూడకుండా మేము ఏ చికిత్సనూ సూచించము._"),
                buttons=[
                    {"id": "menu_entry_consult",
                     "title": _t(lang, "Book Examination", "जांच बुक करें", "పరీక్ష బుక్")},
                    {"id": "menu_treatments",
                     "title": _t(lang, "What We Treat", "हमारे उपचार", "మా చికిత్సలు")},
                    {"id": "menu_human",
                     "title": _t(lang, "Talk to Staff", "स्टाफ से बात", "సిబ్బందితో మాట్లాడు")},
                ],
            )
            await manager.update_state(clinic, phone, "searching_treatments", {})
            return

        await manager.whatsapp.send_text(
            clinic, phone,
            _t(lang,
               "I couldn't find a treatment matching that. Here are all our treatment categories. "
               "You can also choose *Talk to Staff* from the menu.",
               "इससे मिलता उपचार नहीं मिला। हमारी सभी उपचार श्रेणियाँ नीचे हैं। आप मेनू से *Talk to Staff* भी चुन सकते हैं।",
               "దీనికి సరిపోయే చికిత్స దొరకలేదు. మా అన్ని చికిత్స విభాగాలు ఇవి. మెనూ నుండి *Talk to Staff* కూడా ఎంచుకోవచ్చు."),
        )
        await show_treatment_categories(manager, clinic, phone, lang)
        return

    shown = query[:60]
    rows = [
        {"id": f"trt_{t['id']}", "title": _title(t), "description": _row_description(t, lang)}
        for t in matches
    ]
    if has_entry:
        # The honest last row. Nothing above is a diagnosis, and a patient who
        # recognises none of it must not be left re-typing their symptom.
        rows.append({
            "id": "menu_entry_consult",
            "title": _t(lang, "🦺 Book Examination", "🦺 जांच बुक करें", "🦺 పరీక్ష బుక్")[:24],
            "description": _t(lang, "Let the specialist decide", "विशेषज्ञ को तय करने दें",
                              "నిపుణులు నిర్ణయించనివ్వండి")[:72],
        })

    await manager.whatsapp.send_interactive_list(
        clinic,
        phone,
        header=_t(lang, "Treatments that may help", "उपयोगी उपचार", "ఉపయోగపడే చికిత్సలు")[:60],
        body=_t(lang,
                f"These are treatments our clinic offers that relate to *{shown}*. Tap one to read about it.\n\n"
                "_This is not a diagnosis. Your specialist examines you first and then confirms the plan._",
                f"*{shown}* से जुड़े हमारे क्लिनिक के उपचार ये हैं। पढ़ने के लिए किसी एक पर टैप करें।\n\n"
                "_यह निदान नहीं है। विशेषज्ञ पहले जांच करते हैं, फिर योजना तय करते हैं।_",
                f"*{shown}* కి సంబంధించి మా క్లినిక్ అందించే చికిత్సలు ఇవి. చదవడానికి ఒకదాన్ని నొక్కండి.\n\n"
                "_ఇది రోగ నిర్ధారణ కాదు. నిపుణులు ముందు పరీక్షించి, ఆపై ప్రణాళిక నిర్ధారిస్తారు._"),
        button_text=_t(lang, "View", "देखें", "చూడండి"),
        sections=[{
            "title": _t(lang, "Treatments", "उपचार", "చికిత్సలు")[:24],
            "rows": rows,
        }],
    )
    await manager.update_state(clinic, phone, "searching_treatments", {})


# ── Routing entry points used by conversation.py ────────────────────────────

async def handle_treatment_button(manager, clinic: dict, phone: str, button_id: str, session: dict, lang: str) -> None:
    if not specialty_enabled(clinic):
        # A list tapped after the clinic's plan changed.
        await return_to_main_menu(manager, clinic, phone, lang)
        return
    ctx = (session or {}).get("context") or {}

    if button_id == "menu_treatments":
        await show_treatment_categories(manager, clinic, phone, lang)
    elif button_id == "menu_concern":
        await prompt_concern(manager, clinic, phone, lang)
    elif button_id == "menu_entry_consult":
        await start_entry_consultation(manager, clinic, phone, lang)
    elif button_id == "trtentry_more":
        await start_entry_consultation(
            manager, clinic, phone, lang,
            page=int(ctx.get("treatment_entry_page") or 0) + 1,
        )
    elif button_id == "trtcat_more":
        await show_treatment_categories(manager, clinic, phone, lang, page=int(ctx.get("treatment_cat_page") or 0) + 1)
    elif button_id == "trt_more":
        category = ctx.get("treatment_category")
        if category:
            await show_treatments_in_category(manager, clinic, phone, category, lang,
                                              page=int(ctx.get("treatment_page") or 0) + 1)
        else:
            await show_treatment_categories(manager, clinic, phone, lang)
    elif button_id.startswith("trtcat_"):
        categories = ctx.get("treatment_categories") or []
        index = button_id[len("trtcat_"):]
        if index.isdigit() and int(index) < len(categories):
            await show_treatments_in_category(manager, clinic, phone, categories[int(index)], lang)
        else:
            await show_treatment_categories(manager, clinic, phone, lang)
    elif button_id.startswith("trtbook_"):
        # An interest recorded by a multi-entry prompt survives the tap.
        await start_treatment_booking(
            manager, clinic, phone, button_id[len("trtbook_"):], lang,
            interest_name=(ctx.get("treatment_interest") or None),
        )
    elif button_id.startswith("trtexam_"):
        await start_examination_for(manager, clinic, phone, button_id[len("trtexam_"):], lang)
    elif button_id.startswith("trtcall_"):
        await request_callback(manager, clinic, phone, button_id[len("trtcall_"):], lang)
    elif button_id.startswith("trt_"):
        await show_treatment_card(manager, clinic, phone, button_id[len("trt_"):], lang)
    else:
        await return_to_main_menu(manager, clinic, phone, lang)


async def handle_treatment_state(
    manager, clinic: dict, phone: str, lang: str, message: str = ""
) -> None:
    """Anything in a browsing/search state that no handler above claimed.

    Typed text lands here whenever the intent classifier labelled a free-text
    concern as something else. "Dark circles" comes back as book_appointment,
    and the guard in conversation.py that routes typed text to the concern
    search steps aside for that intent so a literal "book appointment" still
    escapes. The concern was therefore answered with the main menu -- and
    because that left the patient in main_menu, their NEXT concern ("acne")
    started a doctor booking instead of showing a treatment.

    So: text is the concern it plainly is, and the escapes keep working by
    matching the exit words themselves instead of trusting the classifier.
    """
    typed = (message or "").strip()
    if typed and typed.lower() not in TREATMENT_EXIT_WORDS:
        await handle_treatment_search_text(manager, clinic, phone, typed, lang)
        return
    await return_to_main_menu(manager, clinic, phone, lang)


async def offer_treatment_browse(manager, clinic: dict, phone: str, lang: str) -> None:
    """After the clinical firewall answers, point specialty patients at the
    catalogue instead of leaving them at a dead end. Never raises."""
    try:
        if not await treatment_menu_active(clinic):
            return
        await manager.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=_t(lang,
                    "You can also explore the treatments offered at our clinic.",
                    "आप हमारे क्लिनिक में उपलब्ध उपचार भी देख सकते हैं।",
                    "మా క్లినిక్‌లో లభించే చికిత్సలను కూడా చూడవచ్చు."),
            buttons=[{"id": "menu_treatments", "title": _t(lang, "Our Treatments", "हमारे उपचार", "మా చికిత్సలు")}],
        )
    except Exception as e:
        logger.warning(f"Could not offer treatment browse to {phone[:6]}***: {e}")
