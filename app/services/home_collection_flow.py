"""WhatsApp steps for home sample collection (migration 097).

Functions take the ConversationManager as `manager`, as specialty_flow does, so
conversation.py only gains routing lines and this flow can be reviewed alone.

Where it sits in the existing lab-test flow (all inside the
confirming_collection_date state, told apart by context["lab_step"]):

    test -> date -> [hc_mode: Home / Visit Centre]
        Visit Centre -> who -> name -> booking            (unchanged path)
        Home -> hc_slot -> who -> name -> hc_location -> hc_address
             -> hc_contact (-> hc_contact_number) -> hc_confirm -> booking

The mode question is only asked when the centre's plan includes home
collection, the centre has switched it on, and the chosen test can be
collected at home (context["hc_available"]). Otherwise nothing here runs and
the lab flow is exactly what it was.
"""

import logging
import re
from datetime import datetime
from typing import Optional

from app.services import home_collection as hc
from app.services.tenant import home_collection_available
from app.utils.validators import mask_phone

logger = logging.getLogger(__name__)

#: Context keys owned by this flow; merged into the context to clear them.
HC_CLEARED = {
    "hc_available": None,
    "hc_mode": None,
    "hc_slot": None,
    "hc_patient_name": None,
    "hc_lat": None,
    "hc_lng": None,
    "hc_pin": None,
    "hc_address": None,
    "hc_contact": None,
    "hc_fee_paise": None,
    "hc_saved": None,
    "hc_ready": None,
}

#: lab_step values whose answer is typed, not tapped. conversation.py lets
#: these through ahead of the global intent handlers, as it does the name step.
TYPED_STEPS = frozenset({"hc_location", "hc_address", "hc_contact_number"})

_YES = frozenset({"yes", "y", "ok", "okay", "confirm", "haan", "ha", "हाँ", "हां", "అవును", "sare", "sari"})
_LETTER = re.compile(r"[A-Za-zऀ-ൿ]")


def _t(lang: str, en: str, hi: str, te: str) -> str:
    return {"hi": hi, "te": te}.get(lang, en)


def _date_label(date_str: Optional[str]) -> str:
    try:
        return datetime.strptime(date_str or "", "%Y-%m-%d").strftime("%a, %d %b")
    except (TypeError, ValueError):
        return date_str or ""


def _rupees(paise: Optional[int]) -> str:
    return f"₹{(paise or 0) // 100}"


def _display_phone(phone: Optional[str]) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 12 and digits.startswith("91"):
        return f"+91 {digits[2:7]} {digits[7:]}"
    return phone or ""


def normalize_contact(text: Optional[str]) -> Optional[str]:
    """An Indian mobile number in any common spelling, as +91XXXXXXXXXX."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def slot_button_id(slot: str) -> str:
    return "hcslot_" + slot.replace(":", "")


def slot_from_button(button_id: str) -> Optional[str]:
    s = button_id.removeprefix("hcslot_")
    if not re.fullmatch(r"\d{4}-\d{4}", s):
        return None
    return f"{s[0:2]}:{s[2:4]}-{s[5:7]}:{s[7:9]}"


def booking_fields(context: dict) -> dict:
    """The appointments columns for a home booking (migration 097)."""
    return {
        "collection_mode": "home",
        "collection_slot": context.get("hc_slot"),
        "collection_address": context.get("hc_address"),
        "collection_landmark": context.get("hc_pin") or None,
        "collection_lat": context.get("hc_lat"),
        "collection_lng": context.get("hc_lng"),
        "collection_contact_phone": context.get("hc_contact"),
        "home_collection_fee_paise": int(context.get("hc_fee_paise") or 0),
        "collection_status": "unassigned",
    }


def is_complete(context: dict) -> bool:
    return all(context.get(k) not in (None, "") for k in (
        "hc_slot", "hc_patient_name", "hc_lat", "hc_lng", "hc_address", "hc_contact",
    ))


# ── Entry points called from conversation.py ─────────────────────────────────


async def annotate_test(clinic: dict, context: dict, test: dict, lang: str) -> str:
    """Decide whether this test may be collected at home and return the line
    that says so on the test card ("" when not offered)."""
    context["hc_available"] = False
    if not home_collection_available(clinic) or not hc.is_home_collectable(test):
        return ""
    settings = await hc.get_settings(clinic, context.get("branch_id"))
    if not settings["enabled"]:
        return ""
    context["hc_available"] = True
    fee = hc.fee_for(settings, test.get("price_paise") or 0)
    if fee:
        return _t(lang,
                  f"\n🏡 *Home sample collection available* (+{_rupees(fee)})",
                  f"\n🏡 *घर पर सैंपल कलेक्शन उपलब्ध* (+{_rupees(fee)})",
                  f"\n🏡 *ఇంటి వద్ద శాంపిల్ సేకరణ అందుబాటులో ఉంది* (+{_rupees(fee)})")
    return _t(lang,
              "\n🏡 *Free home sample collection available*",
              "\n🏡 *घर पर मुफ्त सैंपल कलेक्शन उपलब्ध*",
              "\n🏡 *ఉచిత ఇంటి వద్ద శాంపిల్ సేకరణ అందుబాటులో ఉంది*")


async def ask_mode(manager, clinic: dict, phone: str, context: dict, lang: str) -> None:
    """Home or centre, asked once the date is picked."""
    context.update({"hc_mode": None, "hc_slot": None, "hc_ready": None, "lab_step": "hc_mode"})
    when = _date_label(context.get("lab_collection_date"))
    body = _t(lang,
              f"How would you like to give the sample on *{when}*?\n\n"
              f"🏡 *Home Collection* — our phlebotomist visits your home\n"
              f"🏥 *Visit Centre* — come in during collection hours",
              f"*{when}* को सैंपल कैसे देना चाहेंगे?\n\n"
              f"🏡 *घर पर* — हमारे फ़्लेबोटोमिस्ट आपके घर आएंगे\n"
              f"🏥 *सेंटर पर* — कलेक्शन समय में आएं",
              f"*{when}* న శాంపిల్ ఎలా ఇవ్వాలనుకుంటున్నారు?\n\n"
              f"🏡 *ఇంటి వద్ద* — మా ఫ్లెబోటమిస్ట్ మీ ఇంటికి వస్తారు\n"
              f"🏥 *సెంటర్‌లో* — సేకరణ సమయంలో రండి")
    await manager.whatsapp.send_interactive_buttons(
        clinic, phone, body=body,
        buttons=[
            {"id": "hcmode_home", "title": _t(lang, "🏡 Home Collection", "🏡 घर पर", "🏡 ఇంటి వద్ద")},
            {"id": "hcmode_centre", "title": _t(lang, "🏥 Visit Centre", "🏥 सेंटर पर", "🏥 సెంటర్‌లో")},
        ],
    )
    await manager.update_state(clinic, phone, "confirming_collection_date", context)


async def intercept_finalize(
    manager, clinic: dict, phone: str, context: dict, lang: str, patient_name: str,
) -> bool:
    """Called where the lab flow is about to write the booking. A home booking
    still needs the address, so park the patient's name and ask for it."""
    if context.get("hc_mode") != "home" or context.get("hc_ready"):
        return False
    context["hc_patient_name"] = patient_name
    await _ask_location(manager, clinic, phone, context, lang)
    return True


async def handle_step(
    manager, clinic: dict, phone: str, message: str, context: dict,
    patient: Optional[dict], lang: str, interactive_data: Optional[dict],
) -> bool:
    """One home-collection step inside confirming_collection_date. Returns
    False when the input is not this flow's, so the lab flow handles it."""
    button_id = (interactive_data or {}).get("id", "") or ""
    step = context.get("lab_step")
    text = (message or "").strip() if not interactive_data else ""

    if button_id == "hcmode_centre":
        context.update({"hc_mode": "centre", "hc_slot": None, "hc_ready": None})
        if context.get("hc_patient_name"):
            # Switched from home to centre after the name was given (e.g.
            # outside the service area): book it now rather than ask again.
            await manager._finalize_lab_booking(
                clinic, phone, context, patient, lang,
                context["lab_collection_date"], context["hc_patient_name"],
            )
        else:
            await manager._ask_lab_test_patient(clinic, phone, context, patient, lang)
        return True

    if button_id == "hcmode_home":
        if not context.get("hc_available"):
            await manager._ask_lab_test_patient(clinic, phone, context, patient, lang)
            return True
        context["hc_mode"] = "home"
        await _ask_slot(manager, clinic, phone, context, lang)
        return True

    if button_id.startswith("hcslot_"):
        if context.get("hc_mode") != "home":
            return False
        slot = slot_from_button(button_id)
        if not slot or slot not in await _open_slots(clinic, context):
            await manager.whatsapp.send_text(clinic, phone, _t(lang,
                "That time slot is no longer available. Please choose another:",
                "वह समय स्लॉट अब उपलब्ध नहीं है। कृपया दूसरा चुनें:",
                "ఆ సమయ స్లాట్ ఇప్పుడు అందుబాటులో లేదు. దయచేసి మరొకటి ఎంచుకోండి:"))
            await _ask_slot(manager, clinic, phone, context, lang)
            return True
        context["hc_slot"] = slot
        if not context.get("hc_patient_name"):
            await manager._ask_lab_test_patient(clinic, phone, context, patient, lang)
        elif is_complete(context):
            await _ask_confirm(manager, clinic, phone, context, lang)
        else:
            await _ask_location(manager, clinic, phone, context, lang)
        return True

    if context.get("hc_mode") != "home":
        return False

    # ── location ──
    if (interactive_data or {}).get("type") == "location":
        await _on_location(manager, clinic, phone, context, lang,
                           interactive_data.get("latitude"), interactive_data.get("longitude"),
                           interactive_data.get("name") or interactive_data.get("address") or "")
        return True
    if button_id == "hcaddr_new":
        await _ask_location(manager, clinic, phone, context, lang, offer_saved=False)
        return True
    if button_id == "hcaddr_saved" and context.get("hc_saved"):
        saved = context["hc_saved"]
        far = await _too_far(clinic, context, saved.get("lat"), saved.get("lng"))
        if far is not None:
            await _send_too_far(manager, clinic, phone, context, lang, far)
            return True
        context.update({
            "hc_lat": saved.get("lat"), "hc_lng": saved.get("lng"),
            "hc_pin": saved.get("landmark"), "hc_address": saved.get("address"),
        })
        await _ask_contact(manager, clinic, phone, context, lang)
        return True
    if step == "hc_location" and text:
        coords = hc.parse_coordinates(text)
        if coords:
            await _on_location(manager, clinic, phone, context, lang, coords[0], coords[1], "")
        else:
            await _send_location_help(manager, clinic, phone, lang)
        return True

    # ── address details ──
    if step == "hc_address" and text:
        address = " ".join(text.split())
        if not (10 <= len(address) <= 300) or not _LETTER.search(address):
            await manager.whatsapp.send_text(clinic, phone, _t(lang,
                "Please type the full address: house/flat number, building, street and a landmark.\n"
                "Example: _Flat 302, Sai Residency, MVP Colony, near SBI ATM_",
                "कृपया पूरा पता लिखें: मकान/फ्लैट नंबर, बिल्डिंग, गली और पास का लैंडमार्क।",
                "దయచేసి పూర్తి చిరునామా టైప్ చేయండి: ఇల్లు/ఫ్లాట్ నంబర్, భవనం, వీధి మరియు దగ్గరలోని ల్యాండ్‌మార్క్."))
            return True
        context["hc_address"] = address
        await _ask_contact(manager, clinic, phone, context, lang)
        return True

    # ── contact number ──
    if button_id == "hccontact_same":
        context["hc_contact"] = normalize_contact(phone) or phone
        await _ask_confirm(manager, clinic, phone, context, lang)
        return True
    if button_id == "hccontact_other":
        context["lab_step"] = "hc_contact_number"
        await manager.whatsapp.send_text(clinic, phone, _t(lang,
            "Please type the 10-digit mobile number our phlebotomist should call:",
            "कृपया वह 10 अंकों का मोबाइल नंबर लिखें जिस पर हमारे फ़्लेबोटोमिस्ट कॉल करें:",
            "మా ఫ్లెబోటమిస్ట్ కాల్ చేయాల్సిన 10 అంకెల మొబైల్ నంబర్ టైప్ చేయండి:"))
        await manager.update_state(clinic, phone, "confirming_collection_date", context)
        return True
    if step == "hc_contact_number" and text:
        contact = normalize_contact(text)
        if not contact:
            await manager.whatsapp.send_text(clinic, phone, _t(lang,
                "That doesn't look like a valid mobile number. Please type a 10-digit number, e.g. 9876543210",
                "यह मान्य मोबाइल नंबर नहीं लगता। कृपया 10 अंकों का नंबर लिखें, जैसे 9876543210",
                "ఇది సరైన మొబైల్ నంబర్‌లా లేదు. దయచేసి 10 అంకెల నంబర్ టైప్ చేయండి, ఉదా. 9876543210"))
            return True
        context["hc_contact"] = contact
        await _ask_confirm(manager, clinic, phone, context, lang)
        return True

    # ── confirm ──
    if button_id == "hcconfirm_change":
        context.update({"hc_lat": None, "hc_lng": None, "hc_pin": None, "hc_address": None})
        await _ask_location(manager, clinic, phone, context, lang, offer_saved=False)
        return True
    if button_id == "hcconfirm_yes" or (step == "hc_confirm" and text.lower() in _YES):
        await _confirm(manager, clinic, phone, context, patient, lang)
        return True

    # Anything else while waiting on one of our steps (a stray tap on an old
    # button, a sticker): ask that step again. Falling through to the lab
    # flow would re-ask "who is this for" and lose the address so far.
    if step == "hc_mode":
        await ask_mode(manager, clinic, phone, context, lang)
    elif step == "hc_slot":
        await _ask_slot(manager, clinic, phone, context, lang)
    elif step == "hc_location":
        await _send_location_help(manager, clinic, phone, lang)
    elif step == "hc_address":
        await manager.whatsapp.send_text(clinic, phone, _t(lang,
            "🏠 Please type your house / flat number, building, street and a nearby landmark.",
            "🏠 कृपया अपना मकान/फ्लैट नंबर, बिल्डिंग, गली और पास का लैंडमार्क लिखें।",
            "🏠 దయచేసి మీ ఇల్లు/ఫ్లాట్ నంబర్, భవనం, వీధి మరియు దగ్గరలోని ల్యాండ్‌మార్క్ టైప్ చేయండి."))
    elif step == "hc_contact":
        await _ask_contact(manager, clinic, phone, context, lang)
    elif step == "hc_contact_number":
        await manager.whatsapp.send_text(clinic, phone, _t(lang,
            "Please type the 10-digit mobile number our phlebotomist should call:",
            "कृपया वह 10 अंकों का मोबाइल नंबर लिखें जिस पर हमारे फ़्लेबोटोमिस्ट कॉल करें:",
            "మా ఫ్లెబోటమిస్ట్ కాల్ చేయాల్సిన 10 అంకెల మొబైల్ నంబర్ టైప్ చేయండి:"))
    elif step == "hc_confirm":
        await _ask_confirm(manager, clinic, phone, context, lang)
    else:
        return False
    return True


# ── Steps ─────────────────────────────────────────────────────────────────────


async def _open_slots(clinic: dict, context: dict) -> list[str]:
    from app.database import get_lab_collection_window

    branch_id = context.get("branch_id")
    window = await get_lab_collection_window(clinic, branch_id=branch_id)
    settings = await hc.get_settings(clinic, branch_id)
    return await hc.open_slots(
        clinic["id"], window, settings, context.get("lab_collection_date"), branch_id
    )


async def _ask_slot(manager, clinic: dict, phone: str, context: dict, lang: str) -> None:
    from app.database import get_lab_collection_window

    date_str = context.get("lab_collection_date")
    # ponytail: a WhatsApp list holds 10 rows, so a long window cut into short
    # slots offers only its first 10; a "later times" row is the upgrade if a
    # centre needs it (or it can choose a longer slot length).
    slots = (await _open_slots(clinic, context))[: hc.MAX_SLOT_ROWS]
    if not slots:
        window = await get_lab_collection_window(clinic, branch_id=context.get("branch_id"))
        context.update({"lab_collection_date": None, "hc_mode": None, "hc_slot": None, "lab_step": None})
        await manager.whatsapp.send_interactive_buttons(
            clinic, phone,
            body=_t(lang,
                    f"No home collection slots are left on {_date_label(date_str)}. Please choose another date:",
                    f"{_date_label(date_str)} को होम कलेक्शन के स्लॉट खाली नहीं हैं। कृपया दूसरी तारीख चुनें:",
                    f"{_date_label(date_str)} న హోమ్ కలెక్షన్ స్లాట్లు లేవు. దయచేసి మరో తేదీ ఎంచుకోండి:"),
            buttons=manager._lab_date_buttons(window),
        )
        await manager.update_state(clinic, phone, "confirming_collection_date", context)
        return
    context["lab_step"] = "hc_slot"
    await manager.whatsapp.send_interactive_list(
        clinic, phone,
        body=_t(lang,
                f"🕐 Choose a time for the home visit on *{_date_label(date_str)}*:",
                f"🕐 *{_date_label(date_str)}* को घर आने का समय चुनें:",
                f"🕐 *{_date_label(date_str)}* న ఇంటికి రావాల్సిన సమయం ఎంచుకోండి:"),
        button_text=_t(lang, "Choose time", "समय चुनें", "సమయం ఎంచుకోండి"),
        sections=[{
            "title": _t(lang, "Time slots", "समय स्लॉट", "సమయ స్లాట్లు"),
            "rows": [{"id": slot_button_id(s), "title": s} for s in slots],
        }],
    )
    await manager.update_state(clinic, phone, "confirming_collection_date", context)


async def _ask_location(manager, clinic: dict, phone: str, context: dict, lang: str,
                        offer_saved: bool = True) -> None:
    context["lab_step"] = "hc_location"
    saved = await hc.last_home_address(clinic["id"], phone) if offer_saved else None
    if saved:
        context["hc_saved"] = {
            "address": saved["collection_address"],
            "landmark": saved.get("collection_landmark"),
            "lat": saved["collection_lat"],
            "lng": saved["collection_lng"],
        }
        await manager.whatsapp.send_interactive_buttons(
            clinic, phone,
            body=_t(lang,
                    f"📍 Collect at the same address as last time?\n\n_{saved['collection_address']}_",
                    f"📍 क्या पिछली बार वाले पते पर ही सैंपल लें?\n\n_{saved['collection_address']}_",
                    f"📍 గతసారి చిరునామాలోనే సేకరించాలా?\n\n_{saved['collection_address']}_"),
            buttons=[
                {"id": "hcaddr_saved", "title": _t(lang, "✅ Same address", "✅ वही पता", "✅ అదే చిరునామా")},
                {"id": "hcaddr_new", "title": _t(lang, "📍 New location", "📍 नया पता", "📍 కొత్త లొకేషన్")},
            ],
        )
    else:
        context["hc_saved"] = None
        sent = await manager.whatsapp.send_location_request(clinic, phone, _t(lang,
            "📍 Please share the *exact location* for sample collection.\n\n"
            "Tap *Send location* below and drop the pin on your home, so our phlebotomist can find you.",
            "📍 कृपया सैंपल कलेक्शन के लिए *सटीक लोकेशन* भेजें।\n\n"
            "नीचे *Send location* पर टैप करें और अपने घर पर पिन लगाएं।",
            "📍 దయచేసి శాంపిల్ సేకరణ కోసం *ఖచ్చితమైన లొకేషన్* పంపండి.\n\n"
            "క్రింద *Send location* నొక్కి మీ ఇంటిపై పిన్ పెట్టండి."))
        if not sent:
            await _send_location_help(manager, clinic, phone, lang)
    await manager.update_state(clinic, phone, "confirming_collection_date", context)


async def _send_location_help(manager, clinic: dict, phone: str, lang: str) -> None:
    await manager.whatsapp.send_text(clinic, phone, _t(lang,
        "📍 We need your exact location to send our phlebotomist.\n\n"
        "Tap 📎 (attach) → *Location* → *Send your current location* (or drop a pin on your home).\n"
        "On WhatsApp Web, paste a Google Maps link of your home instead.",
        "📍 फ़्लेबोटोमिस्ट भेजने के लिए हमें आपकी सटीक लोकेशन चाहिए।\n\n"
        "📎 (अटैच) → *Location* → *Send your current location* पर टैप करें।\n"
        "WhatsApp Web पर अपने घर का Google Maps लिंक पेस्ट करें।",
        "📍 ఫ్లెబోటమిస్ట్‌ను పంపడానికి మీ ఖచ్చితమైన లొకేషన్ కావాలి.\n\n"
        "📎 (అటాచ్) → *Location* → *Send your current location* నొక్కండి.\n"
        "WhatsApp Web లో మీ ఇంటి Google Maps లింక్ పేస్ట్ చేయండి."))


async def _too_far(clinic: dict, context: dict, lat, lng) -> Optional[float]:
    settings = await hc.get_settings(clinic, context.get("branch_id"))
    return hc.outside_service_area(settings, float(lat), float(lng))


async def _send_too_far(manager, clinic: dict, phone: str, context: dict, lang: str, km: float) -> None:
    settings = await hc.get_settings(clinic, context.get("branch_id"))
    limit = settings.get("max_distance_km")
    context["lab_step"] = "hc_location"
    await manager.whatsapp.send_interactive_buttons(
        clinic, phone,
        body=_t(lang,
                f"Sorry, that location is about {km:.0f} km away. We collect samples at home within {limit} km of the centre.\n\n"
                f"You can share another location, or visit the centre instead.",
                f"क्षमा करें, यह लोकेशन लगभग {km:.0f} किमी दूर है। हम सेंटर से {limit} किमी के भीतर ही घर से सैंपल लेते हैं।",
                f"క్షమించండి, ఆ లొకేషన్ సుమారు {km:.0f} కి.మీ దూరంలో ఉంది. మేము సెంటర్ నుండి {limit} కి.మీ లోపల మాత్రమే ఇంటి వద్ద సేకరిస్తాము."),
        buttons=[
            {"id": "hcaddr_new", "title": _t(lang, "📍 Other location", "📍 दूसरी लोकेशन", "📍 వేరే లొకేషన్")},
            {"id": "hcmode_centre", "title": _t(lang, "🏥 Visit Centre", "🏥 सेंटर पर", "🏥 సెంటర్‌లో")},
        ],
    )
    await manager.update_state(clinic, phone, "confirming_collection_date", context)


async def _on_location(manager, clinic: dict, phone: str, context: dict, lang: str,
                       lat, lng, label: str) -> None:
    try:
        lat, lng = float(lat), float(lng)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError
    except (TypeError, ValueError):
        await _send_location_help(manager, clinic, phone, lang)
        return
    far = await _too_far(clinic, context, lat, lng)
    if far is not None:
        logger.info(f"Home collection location outside service area for {mask_phone(phone)} ({far:.1f} km)")
        await _send_too_far(manager, clinic, phone, context, lang, far)
        return
    context.update({"hc_lat": lat, "hc_lng": lng, "hc_pin": (label or "")[:200] or None, "lab_step": "hc_address"})
    await manager.whatsapp.send_text(clinic, phone, _t(lang,
        "✅ Location received.\n\n🏠 Now type your *house / flat number, building and street*, with a nearby landmark.\n"
        "Example: _Flat 302, Sai Residency, MVP Colony, near SBI ATM_",
        "✅ लोकेशन मिल गई।\n\n🏠 अब अपना *मकान/फ्लैट नंबर, बिल्डिंग और गली* पास के लैंडमार्क के साथ लिखें।",
        "✅ లొకేషన్ అందింది.\n\n🏠 ఇప్పుడు మీ *ఇల్లు/ఫ్లాట్ నంబర్, భవనం, వీధి* దగ్గరలోని ల్యాండ్‌మార్క్‌తో టైప్ చేయండి."))
    await manager.update_state(clinic, phone, "confirming_collection_date", context)


async def _ask_contact(manager, clinic: dict, phone: str, context: dict, lang: str) -> None:
    context["lab_step"] = "hc_contact"
    shown = _display_phone(phone)
    await manager.whatsapp.send_interactive_buttons(
        clinic, phone,
        body=_t(lang,
                f"📞 Our phlebotomist will call before arriving. Should they call *{shown}*?",
                f"📞 हमारे फ़्लेबोटोमिस्ट आने से पहले कॉल करेंगे। क्या *{shown}* पर कॉल करें?",
                f"📞 మా ఫ్లెబోటమిస్ట్ వచ్చే ముందు కాల్ చేస్తారు. *{shown}* కి కాల్ చేయాలా?"),
        buttons=[
            {"id": "hccontact_same", "title": _t(lang, "✅ Use this number", "✅ यही नंबर", "✅ ఈ నంబర్")},
            {"id": "hccontact_other", "title": _t(lang, "📱 Another number", "📱 दूसरा नंबर", "📱 వేరే నంబర్")},
        ],
    )
    await manager.update_state(clinic, phone, "confirming_collection_date", context)


async def _ask_confirm(manager, clinic: dict, phone: str, context: dict, lang: str) -> None:
    if not is_complete(context):
        # A stale button from an older summary: restart at the missing piece.
        if not context.get("hc_slot"):
            await _ask_slot(manager, clinic, phone, context, lang)
        else:
            await _ask_location(manager, clinic, phone, context, lang)
        return
    settings = await hc.get_settings(clinic, context.get("branch_id"))
    price = int(context.get("lab_test_price_paise") or 0)
    fee = hc.fee_for(settings, price)
    context["hc_fee_paise"] = fee
    context["lab_step"] = "hc_confirm"
    fee_line = (
        _t(lang, f"🏡 Home collection: {_rupees(fee)}", f"🏡 होम कलेक्शन: {_rupees(fee)}", f"🏡 హోమ్ కలెక్షన్: {_rupees(fee)}")
        if fee else _t(lang, "🏡 Home collection: Free", "🏡 होम कलेक्शन: मुफ्त", "🏡 హోమ్ కలెక్షన్: ఉచితం")
    )
    pin = f"\n📌 {context['hc_pin']}" if context.get("hc_pin") else ""
    body = _t(lang, "🏡 *Home Sample Collection — please confirm*", "🏡 *होम सैंपल कलेक्शन — कृपया पुष्टि करें*",
              "🏡 *హోమ్ శాంపిల్ కలెక్షన్ — దయచేసి నిర్ధారించండి*") + (
        f"\n\n🧪 {context.get('lab_test_name')}"
        f"\n👤 {context.get('hc_patient_name')}"
        f"\n📅 {_date_label(context.get('lab_collection_date'))}, {context.get('hc_slot')}"
        f"\n📍 {context.get('hc_address')}{pin}"
        f"\n📞 {_display_phone(context.get('hc_contact'))}"
        f"\n\n💰 {_t(lang, 'Test', 'टेस्ट', 'టెస్ట్')}: {_rupees(price)}\n{fee_line}"
        f"\n*{_t(lang, 'Total', 'कुल', 'మొత్తం')}: {_rupees(price + fee)}*\n\n"
    ) + _t(lang,
           "_Your address and phone number are shared only with our phlebotomist for this visit._",
           "_आपका पता और फ़ोन नंबर केवल इस विज़िट के लिए हमारे फ़्लेबोटोमिस्ट के साथ साझा किया जाएगा।_",
           "_మీ చిరునామా మరియు ఫోన్ నంబర్ ఈ విజిట్ కోసం మా ఫ్లెబోటమిస్ట్‌తో మాత్రమే పంచుకోబడతాయి._")
    await manager.whatsapp.send_interactive_buttons(
        clinic, phone, body=body,
        buttons=[
            {"id": "hcconfirm_yes", "title": _t(lang, "✅ Confirm", "✅ पुष्टि करें", "✅ నిర్ధారించండి")},
            {"id": "hcconfirm_change", "title": _t(lang, "✏️ Change address", "✏️ पता बदलें", "✏️ చిరునామా మార్చు")},
        ],
    )
    await manager.update_state(clinic, phone, "confirming_collection_date", context)


async def _confirm(manager, clinic: dict, phone: str, context: dict, patient: Optional[dict], lang: str) -> None:
    """Re-check what may have changed since the summary, then book."""
    if not is_complete(context):
        await _ask_confirm(manager, clinic, phone, context, lang)
        return
    settings = await hc.get_settings(clinic, context.get("branch_id"))
    if not hc.is_offered(clinic, settings):
        context.update({"hc_mode": "centre", "hc_ready": None})
        await manager.whatsapp.send_text(clinic, phone, _t(lang,
            "Sorry, home sample collection has just been paused by the centre. Your test will be booked as a centre visit.",
            "क्षमा करें, सेंटर ने अभी होम कलेक्शन रोक दिया है। आपका टेस्ट सेंटर विज़िट के रूप में बुक होगा।",
            "క్షమించండి, సెంటర్ ఇప్పుడే హోమ్ కలెక్షన్‌ను నిలిపివేసింది. మీ టెస్ట్ సెంటర్ విజిట్‌గా బుక్ అవుతుంది."))
        await manager._finalize_lab_booking(
            clinic, phone, context, patient, lang, context["lab_collection_date"], context["hc_patient_name"]
        )
        return
    if context["hc_slot"] not in await _open_slots(clinic, context):
        context["hc_slot"] = None
        await manager.whatsapp.send_text(clinic, phone, _t(lang,
            "That time slot has just filled up. Please choose another:",
            "वह समय स्लॉट अभी भर गया। कृपया दूसरा चुनें:",
            "ఆ సమయ స్లాట్ ఇప్పుడే నిండిపోయింది. దయచేసి మరొకటి ఎంచుకోండి:"))
        await _ask_slot(manager, clinic, phone, context, lang)
        return
    context["hc_fee_paise"] = hc.fee_for(settings, int(context.get("lab_test_price_paise") or 0))
    context["hc_ready"] = True
    await manager._finalize_lab_booking(
        clinic, phone, context, patient, lang, context["lab_collection_date"], context["hc_patient_name"]
    )


def confirmation_lines(slot: Optional[str], address: Optional[str], lang: str) -> str:
    """The home-visit part of a booking confirmation."""
    return _t(lang,
              f"\n🏡 Home collection: {slot}\n📍 {address}\n\nOur phlebotomist will call you before arriving. "
              f"You'll get their name and number here once assigned.",
              f"\n🏡 होम कलेक्शन: {slot}\n📍 {address}\n\nहमारे फ़्लेबोटोमिस्ट आने से पहले कॉल करेंगे। "
              f"तय होते ही उनका नाम और नंबर यहीं भेजेंगे।",
              f"\n🏡 హోమ్ కలెక్షన్: {slot}\n📍 {address}\n\nమా ఫ్లెబోటమిస్ట్ వచ్చే ముందు కాల్ చేస్తారు. "
              f"కేటాయించిన వెంటనే వారి పేరు మరియు నంబర్ ఇక్కడ పంపుతాము.")
