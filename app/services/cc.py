"""Kriya Continuous Care — core service (migrations 145–149; docs/continuous-care/*).

Zero-LLM (ADR-CC-02): no AI, voice-NLU or assistant import anywhere under app/services/cc*. Settings, consent
notices, protocol activations (doctor-signed, ADR-CC-06), enrollments (no 'active' without a live consent,
ADR-CC-11), care plans (verbatim projection of the latest signed prescription, ADR-CC-05), observations (append-only,
plausibility-checked) and evaluation (the pure protocol engine; safety card + urgent case in the same call, ADR-CC-07).
Every query is clinic-scoped; every DB call goes through sb().
"""

import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services import cc_protocols
from app.services.cc_protocols.base import Facts, Inputs, Lab, Obs, ParamError, inputs_hash, resolve_params
from app.utils.helpers import today_ist

logger = logging.getLogger(__name__)
NIL = "00000000-0000-0000-0000-000000000000"
LANGS = ("en", "hi", "te")


def _rows(res) -> list:
    return list(getattr(res, "data", None) or [])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def actor_id(user) -> Optional[str]:
    import uuid
    try:
        return str(uuid.UUID(str(getattr(user, "user_id", None))))
    except (ValueError, TypeError):
        return None


def actor_name(user) -> str:
    return (getattr(user, "username", None) or "staff")[:100]


def raise_for(e: Exception, default: str = "The care operation failed. Nothing was changed; please retry."):
    if isinstance(e, HTTPException):
        raise e
    msg = str(getattr(e, "message", None) or e)
    known = {"cc_enrollments_active_consent": (422, "cc_no_live_consent", "A live care consent is needed first."),
             "uq_cc_enrollment_open": (409, "cc_conflict", "This person already has an open enrollment in this programme."),
             "uq_cc_consent_live": (409, "cc_conflict", "This person already has a live care consent."),
             "uq_cc_activation_active": (409, "cc_conflict", "Another activation of this pack was signed meanwhile."),
             "kriya_cross_tenant_ref": (404, "not_found", "Not found in this clinic."),
             "cc_observation_append_only": (409, "cc_conflict", "Readings are never edited — record a correction."),
             "cc_enrollment_not_active": (409, "cc_conflict", "The enrollment is not active."),
             "uq_cc_case_open": (409, "cc_conflict", "An open case for this reason already exists."),
             "cc_actions_no_drug": (422, "cc_forbidden_field", "Care actions never carry a medicine or dose.")}
    for k, (st, code, text) in known.items():
        if k in msg:
            raise HTTPException(status_code=st, detail={"code": code, "message": text})
    logger.error(f"CC_DB_ERROR type={type(e).__name__}")
    raise HTTPException(status_code=500, detail=default)


# ── settings ────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS: dict = {
    "reply_hours": {"days": "Mon-Sat", "from": "09:00", "to": "18:00"},
    "med_times": {"OD": ["08:00"], "BD": ["08:00", "20:00"], "TDS": ["08:00", "14:00", "20:00"],
                  "QID": ["08:00", "12:00", "16:00", "20:00"], "HS": ["22:00"]},
    "card_window": {"from": "07:30", "to": "21:00"},
    "sla_hours": {"urgent": 2, "watch": 16, "overdue": 40},
    "emergency_number": None,
    "template_lang_default": "en",
    "card_show_medicine_names": False,          # DPDP minimisation on shared phones (VERIFICATION D11)
    "care_tier_share": 40,                      # % of the Meta messaging tier (ADR-CC-17)
    "radar_threshold": 30,
    "glucose_unit": "mg/dL",
    "voice_fallback": True,
    "care_daily_cap": 30,
}
_SLA_BOUNDS = {"urgent": (1, 8), "watch": (4, 48), "overdue": (8, 120)}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def get_settings(clinic: dict) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in (clinic.get("cc_settings") or {}).items() if k in DEFAULT_SETTINGS or k == "wa_health"})
    return s


def validate_settings(changes: dict) -> dict:
    out = {}
    for k, v in changes.items():
        if v is None:
            continue
        if k == "reply_hours":
            if not (isinstance(v, dict) and _HHMM.match(str(v.get("from", ""))) and _HHMM.match(str(v.get("to", "")))
                    and v["from"] < v["to"] and re.fullmatch(r"[A-Z][a-z]{2}(-[A-Z][a-z]{2})?(,[A-Z][a-z]{2})*",
                                                             str(v.get("days", "")))):
                raise HTTPException(status_code=422, detail="Reply hours need days (e.g. Mon-Sat) and from < to (HH:MM).")
        elif k == "card_window":
            if not (isinstance(v, dict) and _HHMM.match(str(v.get("from", ""))) and _HHMM.match(str(v.get("to", "")))
                    and "06:00" <= v["from"] < v["to"] <= "22:00"):
                raise HTTPException(status_code=422, detail="Cards go between 06:00 and 22:00 only.")
        elif k == "med_times":
            if not (isinstance(v, dict) and set(v) <= {"OD", "BD", "TDS", "QID", "HS"} and all(
                    isinstance(t, list) and len(t) == {"OD": 1, "BD": 2, "TDS": 3, "QID": 4, "HS": 1}[f]
                    and all(_HHMM.match(str(x)) for x in t) for f, t in v.items())):
                raise HTTPException(status_code=422, detail="Medicine times: OD 1, BD 2, TDS 3, QID 4, HS 1 time (HH:MM).")
            v = {**DEFAULT_SETTINGS["med_times"], **v}
        elif k == "sla_hours":
            if not (isinstance(v, dict) and set(v) <= set(_SLA_BOUNDS) and all(
                    isinstance(x, int) and _SLA_BOUNDS[t][0] <= x <= _SLA_BOUNDS[t][1] for t, x in v.items())):
                raise HTTPException(status_code=422, detail="SLA hours out of bounds (urgent 1–8, watch 4–48, overdue 8–120).")
            v = {**DEFAULT_SETTINGS["sla_hours"], **v}
        elif k == "emergency_number":
            if not re.fullmatch(r"\+?[0-9 -]{3,20}", str(v)):
                raise HTTPException(status_code=422, detail="Give a valid emergency number.")
        elif k in ("template_lang_default",):
            if v not in LANGS:
                raise HTTPException(status_code=422, detail="Language must be en, hi or te.")
        elif k in ("card_show_medicine_names", "voice_fallback"):
            if not isinstance(v, bool):
                raise HTTPException(status_code=422, detail=f"{k} must be true or false.")
        elif k == "care_tier_share":
            if not (isinstance(v, int) and 10 <= v <= 60):
                raise HTTPException(status_code=422, detail="Care share must be 10–60 % of the messaging tier.")
        elif k == "radar_threshold":
            if not (isinstance(v, int) and 0 <= v <= 100):
                raise HTTPException(status_code=422, detail="Radar threshold must be 0–100.")
        elif k == "care_daily_cap":
            if not (isinstance(v, int) and 0 <= v <= 500):
                raise HTTPException(status_code=422, detail="Care call cap must be 0–500 a day.")
        elif k == "glucose_unit":
            if v not in ("mg/dL", "mmol/L"):
                raise HTTPException(status_code=422, detail="Glucose unit must be mg/dL or mmol/L.")
        else:
            raise HTTPException(status_code=422, detail=f"Unknown setting {k}.")
        out[k] = v
    return out


async def save_settings(clinic: dict, changes: dict, expected_updated_at: Optional[str]) -> dict:
    """CAS on cc_settings.updated_at (a stale editor gets 409)."""
    cur = clinic.get("cc_settings") or {}
    if expected_updated_at is not None and cur.get("updated_at") != expected_updated_at:
        raise HTTPException(status_code=409, detail={"code": "cc_conflict", "message": "Settings changed meanwhile. Reload."})
    merged = {**cur, **validate_settings(changes), "updated_at": _now()}
    q = supabase.table("clinics").update({"cc_settings": merged}).eq("id", clinic["id"])
    q = q.eq("cc_settings->>updated_at", cur["updated_at"]) if cur.get("updated_at") else q
    # unscoped: platform_admin
    res = await sb(q)
    if not _rows(res):
        raise HTTPException(status_code=409, detail={"code": "cc_conflict", "message": "Settings changed meanwhile. Reload."})
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    return get_settings({"cc_settings": merged})


# ── consent notices (ADR-CC-11; texts for legal review, O-7) ─────────────────

NOTICE_VERSION = "cc-notice-1"
NOTICES = {
    "en": ("{hospital} Care programme — how we use your information.\n"
           "What we collect: your medicine taken/missed answers, readings you send (sugar, BP, weight), your answers to "
           "wellbeing questions, and results of tests done at {hospital}.\n"
           "Why: so your doctor's care team can remind you, follow up and book your reviews and tests.\n"
           "Who sees it: {hospital}'s doctors and care team only. It is not shared for marketing.\n"
           "Automated service: messages are sent by an automated system; replies are read {hours}. It is NOT an "
           "emergency service — in an emergency call {emergency}.\n"
           "How long: for as long as you are in the programme and as the hospital's medical-record policy requires.\n"
           "Your choice: you can stop at any time by replying STOP CARE, by pressing 'Stop care messages', or by telling "
           "the hospital. Questions or complaints: contact {hospital} on {phone}."),
    "hi": ("{hospital} केयर कार्यक्रम — हम आपकी जानकारी का उपयोग कैसे करते हैं।\n"
           "हम क्या लेते हैं: दवा ली/नहीं ली के जवाब, आपके भेजे रीडिंग (शुगर, बीपी, वज़न), स्वास्थ्य प्रश्नों के जवाब और "
           "{hospital} में हुई जाँचों के परिणाम।\n"
           "क्यों: ताकि आपके डॉक्टर की केयर टीम आपको याद दिला सके, फ़ॉलो-अप कर सके और जाँच/विज़िट बुक कर सके।\n"
           "कौन देखता है: केवल {hospital} के डॉक्टर और केयर टीम। मार्केटिंग के लिए साझा नहीं किया जाता।\n"
           "स्वचालित सेवा: संदेश एक स्वचालित प्रणाली भेजती है; जवाब {hours} पढ़े जाते हैं। यह आपातकालीन सेवा नहीं है — "
           "आपात स्थिति में {emergency} पर कॉल करें।\n"
           "कब तक: जब तक आप कार्यक्रम में हैं और अस्पताल की मेडिकल-रिकॉर्ड नीति के अनुसार।\n"
           "आपकी पसंद: STOP CARE लिखकर, 'केयर संदेश बंद करें' दबाकर या अस्पताल को बताकर कभी भी बंद करें। "
           "प्रश्न या शिकायत: {hospital} से {phone} पर संपर्क करें।"),
    "te": ("{hospital} కేర్ కార్యక్రమం — మీ సమాచారాన్ని ఎలా ఉపయోగిస్తాం.\n"
           "మేము తీసుకునేది: మందు వేసుకున్నారా/లేదా సమాధానాలు, మీరు పంపే రీడింగ్‌లు (షుగర్, బీపీ, బరువు), ఆరోగ్య ప్రశ్నల "
           "సమాధానాలు, {hospital}లో చేసిన పరీక్షల ఫలితాలు.\n"
           "ఎందుకు: మీ డాక్టర్ కేర్ బృందం మీకు గుర్తు చేయడానికి, ఫాలో-అప్ చేయడానికి, పరీక్షలు/విజిట్‌లు బుక్ చేయడానికి.\n"
           "ఎవరు చూస్తారు: {hospital} డాక్టర్లు, కేర్ బృందం మాత్రమే. మార్కెటింగ్‌కు పంచుకోము.\n"
           "స్వయంచాలక సేవ: సందేశాలు స్వయంచాలక వ్యవస్థ పంపుతుంది; సమాధానాలు {hours} చదువుతాం. ఇది అత్యవసర సేవ కాదు — "
           "అత్యవసరంలో {emergency} కు కాల్ చేయండి.\n"
           "ఎంతకాలం: మీరు కార్యక్రమంలో ఉన్నంతకాలం, ఆసుపత్రి వైద్య-రికార్డు విధానం ప్రకారం.\n"
           "మీ ఇష్టం: STOP CARE అని పంపి, 'కేర్ సందేశాలు ఆపండి' నొక్కి లేదా ఆసుపత్రికి చెప్పి ఎప్పుడైనా ఆపవచ్చు. "
           "ప్రశ్నలు లేదా ఫిర్యాదులు: {hospital} ను {phone} లో సంప్రదించండి."),
}


def notice(clinic: dict, lang: str) -> dict:
    from app.config import settings as app_settings
    from app.services.tenant import get_clinic_contact
    s = get_settings(clinic)
    hours = f"{s['reply_hours']['days']} {s['reply_hours']['from']}–{s['reply_hours']['to']}"
    emergency = s.get("emergency_number") or get_clinic_contact(clinic, "emergency_number",
                                                                app_settings.hospital_emergency_number)
    text = NOTICES[lang if lang in NOTICES else "en"].format(
        hospital=clinic.get("name") or "the hospital", hours=hours, emergency=emergency or "112",
        phone=get_clinic_contact(clinic, "phone", clinic.get("whatsapp_number") or ""))
    return {"version": NOTICE_VERSION, "language": lang if lang in NOTICES else "en", "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


# ── safety / education cards (PROTOCOLS §4) ─────────────────────────────────

#: Kriya standard texts — only crisis_support and hypo_now may go out without a clinic-approved text (owner default).
KRIYA_DEFAULT_CARDS = {
    "crisis_support": {
        "en": "💙 You are not alone. Tele-MANAS (free, 24×7): 14416. {hospital}: {emergency}. If you are in danger now, call 112.",
        "hi": "💙 आप अकेले नहीं हैं। टेली-मानस (निःशुल्क, 24×7): 14416। {hospital}: {emergency}। अभी खतरे में हैं तो 112 पर कॉल करें।",
        "te": "💙 మీరు ఒంటరి కాదు. టెలి-మానస్ (ఉచితం, 24×7): 14416. {hospital}: {emergency}. ఇప్పుడు ప్రమాదంలో ఉంటే 112 కు కాల్ చేయండి."},
    "hypo_now": {
        "en": "⚠️ Your sugar is low. Take 15 g of fast sugar now (3 teaspoons of sugar in water, or glucose tablets). Check "
              "again in 15 minutes and repeat once if still low. Do not drive. If you feel confused or faint, call {emergency}.",
        "hi": "⚠️ आपकी शुगर कम है। अभी 15 ग्राम जल्दी असर वाली शुगर लें (पानी में 3 चम्मच चीनी या ग्लूकोज़ गोली)। 15 मिनट बाद फिर "
              "जाँचें, कम हो तो एक बार दोहराएँ। गाड़ी न चलाएँ। उलझन या बेहोशी लगे तो {emergency} पर कॉल करें।",
        "te": "⚠️ మీ షుగర్ తక్కువగా ఉంది. ఇప్పుడే 15 గ్రా. త్వరగా పనిచేసే చక్కెర తీసుకోండి (నీటిలో 3 చెంచాల చక్కెర లేదా గ్లూకోజ్ "
              "మాత్రలు). 15 నిమిషాల తర్వాత మళ్లీ చూడండి, తక్కువగానే ఉంటే ఒకసారి మళ్లీ తీసుకోండి. వాహనం నడపవద్దు. గందరగోళం లేదా "
              "స్పృహ తప్పినట్లు ఉంటే {emergency} కు కాల్ చేయండి."},
}
#: Every card key a pack can select (PROTOCOLS §4 / §6) — each needs clinic-approved text before that pack activates.
CARD_KEYS = ("hypo_now", "high_sugar_symptoms", "bp_very_high_symptoms", "low_bp_dizzy", "crisis_support", "lab_due",
             "visit_due", "eye_check_due", "empty_stomach_timing", "post_op_danger", "eye_post_op_danger",
             "sudden_vision_change", "anc_danger_signs", "postnatal_danger", "ohss_danger", "vaccine_fever",
             "mood_change", "mtx_warning_signs", "post_extraction_care",
             # validated translations of the instrument items (owner decision CC-O-9); English items are built in
             *(f"phq9_q{i}" for i in range(1, 10)), *(f"gad7_q{i}" for i in range(1, 8)))


async def card_text(clinic: dict, key: str, lang: str) -> Optional[str]:
    """The live approved text for (key, lang), else the Kriya default for crisis_support / hypo_now, else None."""
    from app.config import settings as app_settings
    from app.services.tenant import get_clinic_contact
    rows = _rows(await sb(scoped_query("cc_cards", clinic["id"], "body").eq("card_key", key).eq("language", lang)
                          .eq("status", "approved").limit(1)))
    body = rows[0]["body"] if rows else (KRIYA_DEFAULT_CARDS.get(key) or {}).get(lang) or (
        (KRIYA_DEFAULT_CARDS.get(key) or {}).get("en"))
    if not body:
        return None
    emergency = get_settings(clinic).get("emergency_number") or get_clinic_contact(
        clinic, "emergency_number", app_settings.hospital_emergency_number) or "112"
    return body.replace("{hospital}", clinic.get("name") or "Hospital").replace("{emergency}", str(emergency))


async def approve_card(clinic_id: str, key: str, lang: str, body: str, user) -> dict:
    if key not in CARD_KEYS or lang not in LANGS:
        raise HTTPException(status_code=404, detail="Unknown card or language.")
    body = body.strip()
    if not 10 <= len(body) <= 1000:
        raise HTTPException(status_code=422, detail="Card text must be 10–1000 characters.")
    cur = _rows(await sb(scoped_query("cc_cards", clinic_id, "id, version").eq("card_key", key).eq("language", lang)
                         .order("version", desc=True).limit(1)))
    if cur:
        await sb(supabase.table("cc_cards").update({"status": "superseded"}).eq("clinic_id", clinic_id)
                 .eq("card_key", key).eq("language", lang).eq("status", "approved"))
    try:
        # unscoped: insert_scoped_by_payload
        return _rows(await sb(supabase.table("cc_cards").insert({
            "clinic_id": clinic_id, "card_key": key, "language": lang, "version": (cur[0]["version"] + 1) if cur else 1,
            "body": body, "approved_by": actor_id(user), "approved_by_name": actor_name(user)})))[0]
    except Exception as e:
        raise_for(e)


async def list_cards(clinic_id: str) -> list:
    rows = _rows(await sb(scoped_query("cc_cards", clinic_id).eq("status", "approved")))
    have = {(r["card_key"], r["language"]): r for r in rows}
    return [{"card_key": k, "language": lang, "approved": (k, lang) in have,
             "version": have.get((k, lang), {}).get("version"), "body": have.get((k, lang), {}).get("body"),
             "kriya_default": k in KRIYA_DEFAULT_CARDS} for k in CARD_KEYS for lang in LANGS]


# ── protocol activations (ADR-CC-06) ─────────────────────────────────────────

def pack_catalogue(clinic: Optional[dict] = None) -> list:
    out = []
    for code, p in sorted(cc_protocols.PACKS.items()):
        out.append({"code": code, "version": p.VERSION, "title": p.TITLE, "plan": getattr(p, "PLAN", "any"),
                    "icd10": list(p.ICD10), "cards": list(p.CARDS),
                    "params": {k: {"default": v.default, "min": v.min, "max": v.max, "unit": v.unit, "source": v.source}
                               for k, v in p.PARAMS.items()}})
    return out


async def list_activations(clinic_id: str) -> list:
    return _rows(await sb(scoped_query("cc_protocol_activations", clinic_id).order("created_at", desc=True).limit(200)))


async def create_activation(clinic_id: str, code: str, params: dict, step_text: Optional[str], user) -> dict:
    pack = cc_protocols.get(code)
    if not pack:
        raise HTTPException(status_code=404, detail="Unknown protocol pack.")
    try:
        resolved = resolve_params(pack, params)
    except ParamError as e:
        raise HTTPException(status_code=422, detail={"code": "cc_param_out_of_bounds", "message": str(e)})
    try:
        # unscoped: insert_scoped_by_payload
        return _rows(await sb(supabase.table("cc_protocol_activations").insert({
            "clinic_id": clinic_id, "protocol_code": code, "version": pack.VERSION, "params": resolved,
            "step_protocol_text": (step_text or "").strip()[:1000] or None, "created_by": actor_id(user),
            "created_by_name": actor_name(user)})))[0]
    except Exception as e:
        raise_for(e)


async def _activation(clinic_id: str, activation_id: str) -> dict:
    rows = _rows(await sb(scoped_query("cc_protocol_activations", clinic_id).eq("id", activation_id).limit(1)))
    if not rows:
        raise HTTPException(status_code=404, detail="Activation not found.")
    return rows[0]


async def signer_snapshot(clinic_id: str, user) -> dict:
    """A doctor login whose doctor row carries a registration number (prescription-signing shape, AUDIT A6)."""
    if getattr(user, "role", None) == "super_admin" or not getattr(user, "doctor_id", None):
        raise HTTPException(status_code=403, detail="Only a doctor with a registration number can sign care protocols.")
    rows = _rows(await sb(scoped_query("doctors", clinic_id, "id, name, registration_number, registration_council")
                          .eq("id", user.doctor_id).limit(1)))
    if not rows or len((rows[0].get("registration_number") or "").strip()) < 3:
        raise HTTPException(status_code=403, detail="Your doctor profile has no registration number; add it first.")
    d = rows[0]
    return {"doctor_id": d["id"], "name": d["name"], "registration_number": d["registration_number"].strip(),
            "registration_council": d.get("registration_council"), "signed_at": _now()}


async def sign_activation(clinic: dict, activation_id: str, user) -> dict:
    cid = clinic["id"]
    act = await _activation(cid, activation_id)
    if act["status"] != "draft":
        raise HTTPException(status_code=409, detail="Only a draft activation can be signed.")
    pack = cc_protocols.get(act["protocol_code"])
    missing = []
    for key in getattr(pack, "CARDS", ()):
        if key in KRIYA_DEFAULT_CARDS:
            continue
        approved = _rows(await sb(scoped_query("cc_cards", cid, "language").eq("card_key", key).eq("status", "approved")))
        if {r["language"] for r in approved} < {"en"}:
            missing.append(key)
    if missing:
        raise HTTPException(status_code=409, detail={"code": "cc_cards_unapproved", "message":
                            "Approve the care card texts first: " + ", ".join(missing)})
    snap = await signer_snapshot(cid, user)
    now = _now()
    await sb(supabase.table("cc_protocol_activations").update({"status": "retired", "retired_at": now,
                                                               "retired_reason": "replaced by a newer signed activation",
                                                               "updated_at": now})
             .eq("clinic_id", cid).eq("protocol_code", act["protocol_code"]).eq("status", "active"))
    try:
        done = _rows(await sb(supabase.table("cc_protocol_activations").update({
            "status": "active", "signed_by_admin_id": actor_id(user) or snap["doctor_id"], "signer_snapshot": snap,
            "signed_at": now, "updated_at": now}).eq("clinic_id", cid).eq("id", activation_id).eq("status", "draft")))
    except Exception as e:
        raise_for(e)
    if not done:
        raise HTTPException(status_code=409, detail={"code": "cc_conflict", "message": "Changed meanwhile. Reload."})
    # enrollments on the retired activation move to the new one (same pack, newly signed parameters)
    await sb(supabase.table("cc_enrollments").update({"activation_id": activation_id, "updated_at": now})
             .eq("clinic_id", cid).eq("protocol_code", act["protocol_code"])
             .in_("status", ["awaiting_doctor", "invited", "active", "paused"]))
    return done[0]


async def retire_activation(clinic_id: str, activation_id: str, reason: str) -> dict:
    act = await _activation(clinic_id, activation_id)
    live = _rows(await sb(scoped_query("cc_enrollments", clinic_id, "id").eq("activation_id", activation_id)
                          .in_("status", ["invited", "active", "paused"]).limit(1)))
    if live:
        raise HTTPException(status_code=409, detail="Patients are enrolled on this activation. Sign a new activation "
                                                    "(they move to it) or end their enrollments first.")
    if act["status"] == "retired":
        return act
    return _rows(await sb(supabase.table("cc_protocol_activations").update({
        "status": "retired", "retired_at": _now(), "retired_reason": reason.strip()[:300], "updated_at": _now()})
        .eq("clinic_id", clinic_id).eq("id", activation_id)))[0]


# ── consent (ADR-CC-11) ──────────────────────────────────────────────────────

async def live_consent(clinic_id: str, patient_id: str, family_member_id: Optional[str]) -> Optional[dict]:
    q = scoped_query("cc_consents", clinic_id).eq("patient_id", patient_id).is_("withdrawn_at", "null")
    q = q.eq("family_member_id", family_member_id) if family_member_id else q.is_("family_member_id", "null")
    rows = _rows(await sb(q.limit(1)))
    return rows[0] if rows else None


async def record_wa_optin(clinic_id: str, patient_id: str, family_member_id: Optional[str], method: str, user,
                          language: str = "en") -> dict:
    """Step 0 (W2, CC-62): staff ticked 'Agreed to receive care messages on WhatsApp at <number>'."""
    if method not in ("in_person", "phone_call", "whatsapp_reply"):
        raise HTTPException(status_code=422, detail="Opt-in method must be in_person, phone_call or whatsapp_reply.")
    cur = await live_consent(clinic_id, patient_id, family_member_id)
    patch = {"wa_optin_at": _now(), "wa_optin_method": method, "wa_optin_by": actor_id(user), "updated_at": _now()}
    try:
        if cur:
            return _rows(await sb(supabase.table("cc_consents").update(patch).eq("clinic_id", clinic_id).eq("id", cur["id"])))[0]
        # unscoped: insert_scoped_by_payload
        return _rows(await sb(supabase.table("cc_consents").insert({
            "clinic_id": clinic_id, "patient_id": patient_id, "family_member_id": family_member_id,
            "language": language if language in LANGS else "en", **patch})))[0]
    except Exception as e:
        raise_for(e)


async def grant_consent(clinic: dict, patient_id: str, family_member_id: Optional[str], method: str, language: str,
                        user=None, guardian_relation: Optional[str] = None, notice_version: Optional[str] = None) -> dict:
    """Record the DPDP purpose consent with the exact notice (version + SHA-256) the person saw."""
    if notice_version is not None and notice_version != NOTICE_VERSION:
        raise HTTPException(status_code=409, detail="The consent notice changed. Show the current notice and try again.")
    if family_member_id and not (guardian_relation or "").strip():
        raise HTTPException(status_code=422, detail="Consent for a family member is given by the account holder as guardian: "
                                                    "say the relation.")
    n = notice(clinic, language)
    cid = clinic["id"]
    cur = await live_consent(cid, patient_id, family_member_id)
    patch = {"notice_version": n["version"], "notice_sha256": n["sha256"], "language": n["language"], "method": method,
             "granted_at": _now(), "captured_by": actor_id(user) if user else None,
             "captured_by_name": actor_name(user) if user else None,
             "guardian_relation": (guardian_relation or "").strip()[:40] or None, "updated_at": _now()}
    if method == "whatsapp_button" and not (cur and cur.get("wa_optin_at")):
        patch.update(wa_optin_at=_now(), wa_optin_method="whatsapp_reply")
    try:
        if cur:
            return _rows(await sb(supabase.table("cc_consents").update(patch).eq("clinic_id", cid).eq("id", cur["id"])))[0]
        # unscoped: insert_scoped_by_payload
        return _rows(await sb(supabase.table("cc_consents").insert({
            "clinic_id": cid, "patient_id": patient_id, "family_member_id": family_member_id, **patch})))[0]
    except Exception as e:
        raise_for(e)


async def withdraw(clinic_id: str, consent_id: str, via: str) -> None:
    """Withdrawal is a column update, never a delete: every prompt stops at once (W11)."""
    now = _now()
    await sb(supabase.table("cc_consents").update({"withdrawn_at": now, "withdrawn_via": via, "updated_at": now})
             .eq("clinic_id", clinic_id).eq("id", consent_id).is_("withdrawn_at", "null"))
    enr = _rows(await sb(scoped_query("cc_enrollments", clinic_id, "id").eq("consent_id", consent_id)
                         .in_("status", ["invited", "active", "paused"])))
    for e in enr:
        await sb(supabase.table("cc_enrollments").update({"status": "withdrawn", "ended_at": now,
                                                          "end_reason": "consent withdrawn", "updated_at": now})
                 .eq("clinic_id", clinic_id).eq("id", e["id"]))
        await sb(supabase.table("cc_prompts").update({"status": "expired", "updated_at": now}).eq("clinic_id", clinic_id)
                 .eq("enrollment_id", e["id"]).in_("status", ["queued", "sent", "delivered", "read", "awaiting_value"]))
        await sb(supabase.table("cc_cases").update({"status": "closed", "closed_reason": "patient_declined",
                                                    "closed_at": now, "updated_at": now})
                 .eq("clinic_id", clinic_id).eq("enrollment_id", e["id"])
                 .in_("status", ["open", "acknowledged", "awaiting_doctor", "actioned"]))


# ── enrollments (W2) ─────────────────────────────────────────────────────────

OPEN = ["awaiting_doctor", "invited", "active", "paused"]


async def _person(clinic_id: str, patient_id: str, family_member_id: Optional[str]) -> dict:
    p = _rows(await sb(scoped_query("patients", clinic_id, "id, name, phone, language, opted_in").eq("id", patient_id).limit(1)))
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found.")
    out = {**p[0], "person_name": p[0].get("name")}
    if family_member_id:
        fm = _rows(await sb(scoped_query("family_members", clinic_id, "id, full_name").eq("id", family_member_id).limit(1)))
        if not fm:
            raise HTTPException(status_code=404, detail="Family member not found.")
        out["person_name"] = fm[0]["full_name"]
    return out


async def create_enrollment(clinic: dict, body: dict, user, is_doctor: bool) -> dict:
    cid = clinic["id"]
    act = _rows(await sb(scoped_query("cc_protocol_activations", cid).eq("protocol_code", body["protocol_code"])
                         .eq("status", "active").limit(1)))
    if not act:
        raise HTTPException(status_code=409, detail="This care programme has no signed activation yet (Protocols page).")
    person = await _person(cid, body["patient_id"], body.get("family_member_id"))
    doc = _rows(await sb(scoped_query("doctors", cid, "id").eq("id", body["primary_doctor_id"]).limit(1)))
    if not doc:
        raise HTTPException(status_code=404, detail="Doctor not found.")
    if is_doctor and getattr(user, "doctor_id", None) and str(user.doctor_id) != str(body["primary_doctor_id"]):
        is_doctor = False                                  # a doctor enrolling for a colleague still needs that colleague
    row = {"clinic_id": cid, "patient_id": body["patient_id"], "family_member_id": body.get("family_member_id"),
           "activation_id": act[0]["id"], "protocol_code": body["protocol_code"],
           "primary_doctor_id": body["primary_doctor_id"], "branch_id": body.get("branch_id"),
           "status": "invited" if is_doctor else "awaiting_doctor",
           "language": next((x for x in (body.get("language"), person.get("language")) if x in LANGS), "en"),
           "channel_pref": body.get("channel_pref") or "whatsapp", "source": body.get("source") or (
               "doctor_enrolled" if is_doctor else "radar_candidate"),
           "enrolled_by": actor_id(user), "enrolled_by_name": actor_name(user),
           "doctor_approved_at": _now() if is_doctor else None, "baseline": await _baseline(cid, body["patient_id"])}
    try:
        # unscoped: insert_scoped_by_payload
        enr = _rows(await sb(supabase.table("cc_enrollments").insert(row)))[0]
    except Exception as e:
        raise_for(e)
    if body.get("wa_optin_method"):
        await record_wa_optin(cid, body["patient_id"], body.get("family_member_id"), body["wa_optin_method"], user,
                              enr["language"])
    return enr


async def _baseline(clinic_id: str, patient_id: str) -> dict:
    """Snapshot for OUTCOMES: checked-in visits and validated labs in the 90 days before enrollment."""
    since = (today_ist() - timedelta(days=90)).isoformat()
    visits = _rows(await sb(scoped_query("appointments", clinic_id, "id").eq("patient_id", patient_id)
                            .gte("appointment_date", since).not_.is_("checked_in_at", "null").limit(500)))
    return {"visits_90d": len(visits), "as_of": today_ist().isoformat()}


async def get_enrollment(clinic_id: str, enrollment_id: str, branch_id: Optional[str] = None) -> dict:
    q = scoped_query("cc_enrollments", clinic_id).eq("id", enrollment_id)
    rows = _rows(await sb(q.limit(1)))
    if not rows or (branch_id and rows[0].get("branch_id") and str(rows[0]["branch_id"]) != str(branch_id)):
        raise HTTPException(status_code=404, detail="Enrollment not found.")
    return rows[0]


async def approve_enrollment(clinic_id: str, enrollment_id: str, user) -> dict:
    e = await get_enrollment(clinic_id, enrollment_id)
    if e["status"] != "awaiting_doctor":
        raise HTTPException(status_code=409, detail="This enrollment is not waiting for a doctor.")
    done = _rows(await sb(supabase.table("cc_enrollments").update({"status": "invited", "doctor_approved_at": _now(),
                                                                   "updated_at": _now()})
                          .eq("clinic_id", clinic_id).eq("id", enrollment_id).eq("status", "awaiting_doctor")))
    if not done:
        raise HTTPException(status_code=409, detail={"code": "cc_conflict", "message": "Changed meanwhile. Reload."})
    return done[0]


async def activate(clinic: dict, enrollment: dict, consent: dict) -> dict:
    """invited → active once a live consent exists (the DB CHECK refuses otherwise), then build the plan."""
    cid = clinic["id"]
    if enrollment["status"] != "invited" or not enrollment.get("doctor_approved_at"):
        raise HTTPException(status_code=409, detail="Only a doctor-approved invited enrollment can start.")
    try:
        done = _rows(await sb(supabase.table("cc_enrollments").update({
            "status": "active", "consent_id": consent["id"], "enrolled_at": _now(), "updated_at": _now()})
            .eq("clinic_id", cid).eq("id", enrollment["id"]).eq("status", "invited")))
    except Exception as e:
        raise_for(e)
    if not done:
        raise HTTPException(status_code=409, detail={"code": "cc_conflict", "message": "Changed meanwhile. Reload."})
    try:
        await rebuild_plan(clinic, done[0], built_by="system")
    except HTTPException:
        raise
    except Exception as e:                                # noqa: BLE001 - the enrollment stands; the job rebuilds later
        logger.error(f"CC plan build failed clinic={cid} enrollment={enrollment['id']}: {type(e).__name__}")
    return done[0]


async def counter_consent(clinic: dict, enrollment_id: str, notice_version: str, guardian_relation: Optional[str],
                          user) -> dict:
    e = await get_enrollment(clinic["id"], enrollment_id)
    consent = await grant_consent(clinic, e["patient_id"], e.get("family_member_id"), "counter_signed", e["language"],
                                  user, guardian_relation, notice_version)
    return await activate(clinic, e, consent)


async def set_status(clinic_id: str, enrollment_id: str, to: str, reason: str) -> dict:
    e = await get_enrollment(clinic_id, enrollment_id)
    allowed = {"paused": ("active",), "active": ("paused",), "ended_by_clinic": tuple(OPEN), "graduated": ("active", "paused"),
               "lost": ("active", "paused")}
    if e["status"] not in allowed.get(to, ()):
        raise HTTPException(status_code=409, detail=f"Cannot move an enrollment from {e['status']} to {to}.")
    patch = {"status": to, "updated_at": _now()}
    if to in ("ended_by_clinic", "graduated", "lost"):
        patch.update(ended_at=_now(), end_reason=reason.strip()[:300])
    done = _rows(await sb(supabase.table("cc_enrollments").update(patch).eq("clinic_id", clinic_id)
                          .eq("id", enrollment_id).eq("status", e["status"])))
    if not done:
        raise HTTPException(status_code=409, detail={"code": "cc_conflict", "message": "Changed meanwhile. Reload."})
    if to in ("ended_by_clinic", "graduated", "lost", "paused"):
        await sb(supabase.table("cc_prompts").update({"status": "expired", "updated_at": _now()}).eq("clinic_id", clinic_id)
                 .eq("enrollment_id", enrollment_id).in_("status", ["queued", "sent", "delivered", "read", "awaiting_value"]))
    return done[0]


async def list_enrollments(clinic_id: str, status: Optional[str], protocol: Optional[str], doctor_id: Optional[str],
                           q: Optional[str], branch_id: Optional[str], limit: int, offset: int) -> dict:
    from app.utils.validators import mask_phone
    qq = supabase.table("cc_enrollments").select("*", count="exact").eq("clinic_id", clinic_id)
    if status:
        qq = qq.eq("status", status)
    if protocol:
        qq = qq.eq("protocol_code", protocol)
    if doctor_id:
        qq = qq.eq("primary_doctor_id", doctor_id)
    if branch_id:
        qq = qq.eq("branch_id", branch_id)
    res = await sb(qq.order("created_at", desc=True).range(offset, offset + limit - 1))
    rows = _rows(res)
    pats = {}
    if rows:
        pats = {p["id"]: p for p in _rows(await sb(scoped_query("patients", clinic_id, "id, name, phone, mrn")
                                                   .in_("id", list({r["patient_id"] for r in rows}))))}
    out = []
    for r in rows:
        p = pats.get(r["patient_id"]) or {}
        if q and q.lower() not in (p.get("name") or "").lower() and q not in (p.get("mrn") or ""):
            continue
        out.append({**r, "patient_name": p.get("name"), "mrn": p.get("mrn"), "phone_masked": mask_phone(p.get("phone") or "")})
    return {"enrollments": out, "total": res.count if getattr(res, "count", None) is not None else len(out)}


# ── care plan (ADR-CC-05) ────────────────────────────────────────────────────

FREQ = ("OD", "BD", "TDS", "QID", "HS")
#: Controlled lines never get an adherence prompt (H1 / X / narcotic / psychotropic) — a closed list of generics,
#: matched on the signed drug name; the clinic's own rx_items schedule is used as well when the item maps.
CONTROLLED_GENERICS = (
    "alprazolam", "clonazepam", "lorazepam", "diazepam", "nitrazepam", "midazolam", "chlordiazepoxide", "zolpidem",
    "zopiclone", "tramadol", "tapentadol", "codeine", "morphine", "fentanyl", "buprenorphine", "pentazocine",
    "oxycodone", "pethidine", "methadone", "ketamine", "phenobarbital", "methylphenidate", "modafinil", "pregabalin",
    # Schedule H1 antimicrobials and anti-TB drugs
    "cefixime", "cefpodoxime", "cefuroxime", "ceftriaxone", "cefoperazone", "cefotaxime", "ceftazidime", "cefepime",
    "meropenem", "imipenem", "ertapenem", "doripenem", "moxifloxacin", "levofloxacin", "gemifloxacin", "linezolid",
    "colistin", "tigecycline", "isoniazid", "rifampicin", "pyrazinamide", "ethambutol", "bedaquiline", "delamanid",
    "clarithromycin", "azithromycin", "amoxicillin and clavulanic", "piperacillin", "ertapenem", "gemifloxacin",
)
_DISPLAY_ONLY_FORMS = ("injection", "inhaler", "spray", "patch", "ointment", "cream", "gel", "lotion", "powder")
_DISPLAY_ONLY_ROUTES = ("iv", "im", "sc", "inhalation", "topical", "rectal", "vaginal")
_TAPER = re.compile(r"\b(taper|tapering|reduce|alternate day|weekly|once a week|every week|sos|stat|as needed|if needed)\b",
                    re.I)


def _line_text(it: dict) -> str:
    parts = [it.get("drug_name"), it.get("strength"), it.get("dosage"), it.get("frequency"),
             (it.get("timing") or "").replace("_", " ") if it.get("timing") not in (None, "any") else None,
             f"for {it['duration_days']} days" if it.get("duration_days") else None, it.get("instructions")]
    return " · ".join(str(p) for p in parts if p)[:300]


def plan_item_for_line(it: dict, med_times: dict, start: date, controlled: set) -> dict:
    """One signed line → one plan item. Only an OD/BD/TDS/QID/HS oral-type line becomes an adherence prompt; SOS,
    STAT, custom ("1-0-1", "weekly"), tapers, injections, inhalers, insulin and every controlled drug are shown
    exactly as signed with no prompt (ADR-CC-05; methotrexate weekly never becomes daily, H3)."""
    freq = (it.get("frequency") or "").strip().upper()
    name = (it.get("drug_name") or "").lower()
    display_only = (
        freq not in FREQ or (it.get("formulation") in _DISPLAY_ONLY_FORMS) or (it.get("route") in _DISPLAY_ONLY_ROUTES)
        or "insulin" in name or bool(_TAPER.search(f"{it.get('instructions') or ''} {it.get('dosage') or ''}"))
        or any(g in name for g in CONTROLLED_GENERICS) or it.get("id") in controlled)
    item = {"kind": "medication", "source_prescription_item_id": it["id"], "display_text": _line_text(it),
            "starts_on": start.isoformat(),
            "ends_on": (start + timedelta(days=int(it["duration_days"]) - 1)).isoformat() if it.get("duration_days") else None}
    if display_only:
        return {**item, "prompt_mode": "display_only", "schedule": {}}
    return {**item, "prompt_mode": "adherence", "schedule": {"times": list(med_times.get(freq) or [])}}


#: What each pack asks for besides medicines: readings and due tests (lab items carry the pack's interval).
PACK_ITEMS = {
    "t2dm": [{"kind": "measure", "measure_code": "fbg", "prompt_mode": "reading", "display_text": "Fasting sugar reading",
              "schedule": {"every_days": 3, "time": "07:30"}}],
    "htn": [{"kind": "measure", "measure_code": "bp", "prompt_mode": "reading", "display_text": "Home BP reading",
             "schedule": {"weekly": ["mon", "thu"], "time": "08:30"}}],
    "phq9": [{"kind": "measure", "measure_code": "phq9", "prompt_mode": "reading", "display_text": "Wellbeing check (PHQ-9)",
              "schedule": {"every_days": 14, "time": "11:00"}}],
    "gad7": [{"kind": "measure", "measure_code": "gad7", "prompt_mode": "reading", "display_text": "Wellbeing check (GAD-7)",
              "schedule": {"every_days": 14, "time": "11:00"}}],
    "post_discharge": [{"kind": "measure", "measure_code": "symptoms", "prompt_mode": "reading",
                        "display_text": "How are you feeling after discharge?", "schedule": {"every_days": 2, "time": "10:00"}}],
}


async def _latest_signed_rx(clinic_id: str, patient_id: str, family_member_id: Optional[str]) -> Optional[dict]:
    q = scoped_query("opd_prescriptions", clinic_id, "id, signed_at, encounter_id, family_member_id").eq("patient_id", patient_id) \
        .eq("status", "signed")
    q = q.eq("family_member_id", family_member_id) if family_member_id else q.is_("family_member_id", "null")
    rows = _rows(await sb(q.order("signed_at", desc=True).limit(1)))
    return rows[0] if rows else None


async def build_items(clinic: dict, enrollment: dict, rx: Optional[dict]) -> list:
    cid = clinic["id"]
    today = today_ist()
    items: list = []
    if rx:
        lines = _rows(await sb(scoped_query("opd_prescription_items", cid).eq("prescription_id", rx["id"]).order("line_no")))
        controlled = await _controlled_lines(cid, lines)
        med_times = get_settings(clinic)["med_times"]
        items += [plan_item_for_line(ln, med_times, today, controlled) for ln in lines]
        enc = _rows(await sb(scoped_query("opd_encounters", cid, "follow_up_date").eq("id", rx["encounter_id"]).limit(1)))
        if enc and enc[0].get("follow_up_date"):
            items.append({"kind": "visit", "prompt_mode": "due_date", "display_text": "Follow-up visit",
                          "schedule": {"due_on": enc[0]["follow_up_date"]}})
    for t in PACK_ITEMS.get(enrollment["protocol_code"], []):
        items.append({**t, "starts_on": today.isoformat()})
    pack = cc_protocols.get(enrollment["protocol_code"])
    for measure in (getattr(pack, "DUE_LABS", {}) or {}):
        src = _rows(await sb(scoped_query("cc_measure_sources", cid, "lab_test_id").eq("measure_code", measure).limit(1)))
        items.append({"kind": "lab", "prompt_mode": "due_date", "measure_code": measure,
                      "display_text": f"{measure.upper()} test", "lab_test_id": src[0]["lab_test_id"] if src else None,
                      "schedule": {"interval_param": pack.DUE_LABS[measure]}})
    return items


async def _controlled_lines(clinic_id: str, lines: list) -> set:
    """Lines whose drug maps to a clinic rx_item with a controlled schedule (only for Pharmacy OS clinics)."""
    names = list({(ln.get("drug_name") or "").strip() for ln in lines if ln.get("drug_name")})
    if not names:
        return set()
    try:
        items = _rows(await sb(scoped_query("rx_items", clinic_id, "brand_name, schedule").in_("brand_name", names)
                               .in_("schedule", ["H1", "X", "narcotic", "psychotropic"])))
    except Exception:                                     # noqa: BLE001 - no Pharmacy OS on this clinic
        return set()
    bad = {i["brand_name"].strip().lower() for i in items}
    return {ln["id"] for ln in lines if (ln.get("drug_name") or "").strip().lower() in bad}


async def rebuild_plan(clinic: dict, enrollment: dict, built_by: str = "system") -> dict:
    rx = await _latest_signed_rx(clinic["id"], enrollment["patient_id"], enrollment.get("family_member_id"))
    items = await build_items(clinic, enrollment, rx)
    try:
        res = await sb(supabase.rpc("cc_install_plan", {
            "p_clinic_id": clinic["id"], "p_enrollment_id": enrollment["id"], "p_source_rx": rx["id"] if rx else None,
            "p_items": items, "p_built_by": built_by[:100], "p_today": today_ist().isoformat()}))
    except Exception as e:
        raise_for(e)
    return res.data or {}


async def plan_preview(clinic: dict, enrollment: dict) -> dict:
    rx = await _latest_signed_rx(clinic["id"], enrollment["patient_id"], enrollment.get("family_member_id"))
    return {"source_prescription": rx, "items": await build_items(clinic, enrollment, rx)}


async def current_plan(clinic_id: str, enrollment_id: str) -> Optional[dict]:
    plan = _rows(await sb(scoped_query("cc_care_plans", clinic_id).eq("enrollment_id", enrollment_id)
                          .eq("status", "active").limit(1)))
    if not plan:
        return None
    items = _rows(await sb(scoped_query("cc_plan_items", clinic_id).eq("plan_id", plan[0]["id"]).order("created_at")))
    return {**plan[0], "items": items}


async def rebuild_on_new_rx(clinic: dict) -> int:
    """W3 polling (no trigger, ADR-CC-13): an active enrollment whose person has a signed prescription newer than the
    plan's watermark gets a new plan version, atomically."""
    n = 0
    for e in await fetch_all(lambda: scoped_query("cc_enrollments", clinic["id"]).in_("status", ["active", "paused"]),
                             label="cc:rebuild"):
        rx = await _latest_signed_rx(clinic["id"], e["patient_id"], e.get("family_member_id"))
        if rx and (not e.get("last_rx_seen_at") or str(rx["signed_at"]) > str(e["last_rx_seen_at"])):
            try:
                await rebuild_plan(clinic, e)
                n += 1
            except Exception as ex:                       # noqa: BLE001 - one enrollment never stops the sweep
                logger.error(f"CC rebuild failed clinic={clinic['id']}: {type(ex).__name__}")
    return n


# ── observations (ADR-CC-12) ─────────────────────────────────────────────────

PLAUSIBLE = {"fbg": (20, 600), "ppbg": (20, 600), "rbg": (20, 600), "weight": (2, 300), "hba1c": (3.0, 20.0),
             "phq9": (0, 27), "gad7": (0, 21), "phq2": (0, 6), "tsh": (0.01, 150)}
UNITS = {"fbg": "mg/dL", "ppbg": "mg/dL", "rbg": "mg/dL", "bp": "mmHg", "weight": "kg", "hba1c": "%", "phq9": "score",
         "gad7": "score", "phq2": "score", "tsh": "mIU/L"}
DAILY_CAP = 20                                             # loophole X4: readings per person per day


def plausible(measure: str, v1: Optional[float], v2: Optional[float] = None) -> bool:
    if measure == "bp":
        return v1 is not None and v2 is not None and 60 <= v1 <= 260 and 30 <= v2 <= 160 and v1 > v2
    lo_hi = PLAUSIBLE.get(measure)
    return v1 is None or (lo_hi is None) or lo_hi[0] <= v1 <= lo_hi[1]


async def record_observation(clinic_id: str, enrollment_id: str, measure: str, *, value: Optional[float] = None,
                             value2: Optional[float] = None, code: Optional[str] = None, source: str,
                             prompt_id: Optional[str] = None, message_id: Optional[str] = None,
                             observed_at: Optional[str] = None, user=None, lab_result_value_id: Optional[str] = None,
                             corrects_id: Optional[str] = None, reason: Optional[str] = None) -> Optional[dict]:
    """Insert one observation; None when it is a duplicate webhook (unique source_message_id) or over the daily cap."""
    if source not in ("lab_result", "coordinator_entered"):
        start = datetime.combine(today_ist(), datetime.min.time()).isoformat() + "+05:30"
        recent = _rows(await sb(scoped_query("cc_observations", clinic_id, "id").eq("enrollment_id", enrollment_id)
                                .gte("created_at", start).limit(DAILY_CAP + 1)))
        if len(recent) >= DAILY_CAP:
            return None
    row = {"clinic_id": clinic_id, "enrollment_id": enrollment_id, "prompt_id": prompt_id, "measure_code": measure,
           "value_num": value, "value_num2": value2, "value_code": code, "unit": UNITS.get(measure),
           "observed_at": observed_at or _now(), "source": source, "source_message_id": message_id,
           "lab_result_value_id": lab_result_value_id, "entered_by": actor_id(user) if user else None,
           "entered_by_name": actor_name(user) if user else None, "corrects_id": corrects_id,
           "correction_reason": reason, "plausible": plausible(measure, value, value2)}
    try:
        # unscoped: insert_scoped_by_payload
        return _rows(await sb(supabase.table("cc_observations").insert(row)))[0]
    except Exception as e:
        if "uq_cc_observations_message" in str(e) or "uq_cc_observations_lab" in str(e):
            return None
        raise_for(e)


async def observations(clinic_id: str, enrollment_id: str, measure: Optional[str], since: Optional[str],
                       until: Optional[str]) -> list:
    q = scoped_query("cc_observations", clinic_id).eq("enrollment_id", enrollment_id)
    if measure:
        q = q.eq("measure_code", measure)
    if since:
        q = q.gte("observed_at", since)
    if until:
        q = q.lte("observed_at", until)
    return _rows(await sb(q.order("observed_at", desc=True).limit(1000)))


async def correct_observation(clinic_id: str, observation_id: str, value: Optional[float], value2: Optional[float],
                              code: Optional[str], reason: str, user) -> dict:
    old = _rows(await sb(scoped_query("cc_observations", clinic_id).eq("id", observation_id).limit(1)))
    if not old:
        raise HTTPException(status_code=404, detail="Reading not found.")
    o = old[0]
    out = await record_observation(clinic_id, o["enrollment_id"], o["measure_code"], value=value, value2=value2,
                                   code=code, source="coordinator_entered", observed_at=o["observed_at"], user=user,
                                   corrects_id=o["id"], reason=reason)
    if out is None:
        raise HTTPException(status_code=409, detail="Could not record the correction.")
    return out


# ── evaluation (ADR-CC-07) ───────────────────────────────────────────────────

def _d(x) -> date:
    return date.fromisoformat(str(x)[:10])


async def _labs(clinic_id: str, patient_id: str) -> list:
    """Validated LIS results mapped through cc_measure_sources (never an unvalidated value, PROTOCOLS §1)."""
    maps = _rows(await sb(scoped_query("cc_measure_sources", clinic_id)))
    if not maps:
        return []
    results = _rows(await sb(scoped_query("lab_results", clinic_id, "id, lab_test_id, status, validated_at, created_at")
                             .eq("patient_id", patient_id).eq("status", "validated").order("created_at", desc=True).limit(200)))
    out = []
    for r in results:
        for m in [m for m in maps if str(m["lab_test_id"]) == str(r["lab_test_id"])]:
            vq = scoped_query("lab_result_values", clinic_id, "id, numeric_value, flag, parameter_id, parameter_name") \
                .eq("result_id", r["id"])
            vals = _rows(await sb(vq))
            for v in vals:
                if m.get("parameter_id") and str(v.get("parameter_id")) != str(m["parameter_id"]):
                    continue
                if m.get("parameter_name") and (v.get("parameter_name") or "").lower() != m["parameter_name"].lower():
                    continue
                num = float(v["numeric_value"]) * float(m.get("unit_factor") or 1) if v.get("numeric_value") is not None else None
                out.append({"measure": m["measure_code"], "at": r.get("validated_at") or r["created_at"], "value": num,
                            "flag": v.get("flag"), "lab_result_value_id": v["id"]})
                break
    return out


async def last_verified_visit(clinic_id: str, patient_id: str) -> Optional[date]:
    """Check-in or a signed encounter — never appointments.status='completed' (ADR-CC-08)."""
    a = _rows(await sb(scoped_query("appointments", clinic_id, "checked_in_at").eq("patient_id", patient_id)
                       .not_.is_("checked_in_at", "null").order("checked_in_at", desc=True).limit(1)))
    e = _rows(await sb(scoped_query("opd_encounters", clinic_id, "signed_at").eq("patient_id", patient_id)
                       .eq("status", "signed").order("signed_at", desc=True).limit(1)))
    dates = [_d(x) for x in ((a[0]["checked_in_at"] if a else None), (e[0]["signed_at"] if e else None)) if x]
    return max(dates) if dates else None


async def build_inputs(clinic_id: str, enrollment: dict, today: Optional[date] = None) -> tuple:
    today = today or today_ist()
    since = (today - timedelta(days=120)).isoformat()
    obs_rows = [o for o in _rows(await sb(scoped_query("cc_observations", clinic_id).eq("enrollment_id", enrollment["id"])
                                          .gte("observed_at", since).limit(2000))) if o.get("plausible")]
    corrected = {o["corrects_id"] for o in obs_rows if o.get("corrects_id")}
    obs = tuple(Obs(measure=o["measure_code"], at=_d(o["observed_at"]),
                    value=float(o["value_num"]) if o.get("value_num") is not None else None,
                    value2=float(o["value_num2"]) if o.get("value_num2") is not None else None, code=o.get("value_code"))
                for o in obs_rows if o["id"] not in corrected)
    labs = await _labs(clinic_id, enrollment["patient_id"])
    lab_t = tuple(Lab(measure=x["measure"], at=_d(x["at"]), value=x["value"], flag=x["flag"]) for x in labs)
    unanswered = int(enrollment.get("unanswered_streak") or 0)
    base = enrollment.get("baseline") or {}
    extra = {k: (_d(v) if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else v)
             for k, v in (base.get("facts") or {}).items()}
    facts = Facts(enrolled_on=_d(enrollment.get("enrolled_at") or enrollment["created_at"]),
                  baseline={k: v for k, v in base.items() if isinstance(v, (int, float))}, extra=extra)
    inp = Inputs(today=today, observations=obs, labs=lab_t, facts=facts, unanswered_prompts=unanswered,
                 last_visit=await last_verified_visit(clinic_id, enrollment["patient_id"]))
    ids = [o["id"] for o in obs_rows]
    return inp, ids


async def evaluate_enrollment(clinic: dict, enrollment: dict, today: Optional[date] = None) -> dict:
    """Run the signed pack; store the evaluation once per inputs hash; open / update a case for watch / urgent.
    Returns {"tier", "reasons", "safety_card", "evaluation_id", "case_id"}."""
    cid = clinic["id"]
    act = (_rows(await sb(scoped_query("cc_protocol_activations", cid).eq("id", enrollment["activation_id"]).limit(1)))
           or [None])[0]
    pack = cc_protocols.get(enrollment["protocol_code"])
    if not act or not pack or act["status"] != "active":
        return {"tier": None, "reasons": [], "safety_card": None, "evaluation_id": None, "case_id": None}
    params = resolve_params(pack, act["params"] or {})
    inp, obs_ids = await build_inputs(cid, enrollment, today)
    ev = pack.evaluate(params, inp)
    digest = inputs_hash(pack, params, inp)
    row = {"clinic_id": cid, "enrollment_id": enrollment["id"], "activation_id": act["id"],
           "protocol_version": pack.VERSION, "tier": ev.tier, "reasons": ev.as_dict()["reasons"],
           "proposed": list(ev.proposed), "safety_card": ev.safety_card, "observation_ids": obs_ids[:500],
           "inputs_sha256": digest}
    try:
        # unscoped: insert_scoped_by_payload
        saved = _rows(await sb(supabase.table("cc_evaluations").insert(row)))
    except Exception as e:
        if "cc_evaluations_inputs_key" not in str(e):
            raise
        saved = _rows(await sb(scoped_query("cc_evaluations", cid, "id").eq("enrollment_id", enrollment["id"])
                               .eq("inputs_sha256", digest).limit(1)))      # a parallel run wrote it first
    ev_id = saved[0]["id"] if saved else None
    if ev_id:
        await sb(supabase.table("cc_enrollments").update({"last_evaluation_id": ev_id, "updated_at": _now()})
                 .eq("clinic_id", cid).eq("id", enrollment["id"]))
    case_id = None
    if ev.tier in ("watch", "urgent"):
        from app.services import cc_cases
        top = max(ev.reasons, key=lambda r: {"urgent": 2, "watch": 1}.get(r.tier, 0))
        case_id = await cc_cases.open_case(clinic, patient_id=enrollment["patient_id"],
                                           family_member_id=enrollment.get("family_member_id"),
                                           enrollment_id=enrollment["id"], reason=f"{enrollment['protocol_code']}:{top.code}"[:60],
                                           tier=ev.tier, evaluation_id=ev_id, branch_id=enrollment.get("branch_id"),
                                           summary={"reasons": ev.as_dict()["reasons"], "proposed": list(ev.proposed)})
    return {"tier": ev.tier, "reasons": ev.as_dict()["reasons"], "safety_card": ev.safety_card, "evaluation_id": ev_id,
            "case_id": case_id, "proposed": list(ev.proposed)}


# ── lifecycle (ADR-CC-01) ────────────────────────────────────────────────────

async def deactivation_preview(clinic_id: str) -> dict:
    active = _rows(await sb(scoped_query("cc_enrollments", clinic_id, "id").in_("status", ["invited", "active", "paused"])
                            .limit(10000)))
    cases = _rows(await sb(scoped_query("cc_cases", clinic_id, "id").in_("status", ["open", "acknowledged",
                                                                                    "awaiting_doctor", "actioned"])
                           .limit(10000)))
    queued = _rows(await sb(scoped_query("cc_prompts", clinic_id, "id").eq("status", "queued").limit(10000)))
    return {"active_enrollments": len(active), "open_cases": len(cases), "queued_prompts": len(queued),
            "blocking": bool(active)}


async def finish_wind_down(clinic_id: str) -> bool:
    """WIND_DOWN → OFF once no enrollment is active (called by the nightly job)."""
    if (await deactivation_preview(clinic_id))["blocking"]:
        return False
    await sb(supabase.table("clinics").update({"cc_state": "OFF"}).eq("id", clinic_id).eq("cc_state", "WIND_DOWN"))
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    return True


async def setup_checklist(clinic: dict) -> list:
    """W1 go-live checklist; each item says what is wrong and where to fix it."""
    cid = clinic["id"]
    s = get_settings(clinic)
    acts = await list_activations(cid)
    coords = _rows(await sb(scoped_query("clinic_admins", cid, "id, staff_role, permissions").limit(500)))
    has_coord = any(c.get("staff_role") == "CARE_COORDINATOR" or "CC_COORDINATE" in (c.get("permissions") or [])
                    for c in coords)
    maps = _rows(await sb(scoped_query("cc_measure_sources", cid, "id").limit(1)))
    cards = await list_cards(cid)
    from app.services import cc_channels
    tpl = (clinic.get("cc_settings") or {}).get("templates") or {}
    approved = {n for n, st in tpl.items() if st == "APPROVED"}
    legacy = _rows(await sb(scoped_query("prescriptions", cid, "id").eq("is_active", True).limit(1000)))
    limit = ((clinic.get("config") or {}).get("dental_message_limits") or {}).get("care")
    return [
        {"key": "reply_hours", "ok": bool(s.get("reply_hours")), "label": "Reply hours set", "fix": "settings"},
        {"key": "emergency_number", "ok": bool(s.get("emergency_number")), "label": "Emergency number on every card",
         "fix": "settings"},
        {"key": "templates", "ok": set(cc_channels.TEMPLATES) <= approved,
         "label": f"WhatsApp templates approved ({len(approved & set(cc_channels.TEMPLATES))}/{len(cc_channels.TEMPLATES)})",
         "fix": "templates"},
        {"key": "notice", "ok": True, "label": f"Consent notice {NOTICE_VERSION} (en / hi / te) — legal review is a "
                                               "go-live gate (O-7)", "fix": "notice"},
        {"key": "activation", "ok": any(a["status"] == "active" for a in acts), "label": "A doctor-signed protocol",
         "fix": "ccprotocols"},
        {"key": "coordinator", "ok": has_coord, "label": "At least one care coordinator login", "fix": "staff"},
        {"key": "care_limit", "ok": limit is not None, "label": "Monthly care message limit set by the owner",
         "fix": "owner"},
        {"key": "lab_mapping", "ok": bool(maps), "label": "Lab tests mapped to care measures (HbA1c, TSH…)",
         "fix": "measure_sources"},
        {"key": "cards", "ok": all(c["approved"] or c["kriya_default"] for c in cards if c["language"] == "en"),
         "label": "Safety card texts approved", "fix": "cards"},
        {"key": "legacy_reminders", "ok": not legacy, "label": f"{len(legacy)} old free-text medicine reminders are "
         "active — they reach patients only inside the 24-hour WhatsApp window (latent L2)", "fix": "prescriptions"},
    ]


async def measure_sources(clinic_id: str) -> list:
    return _rows(await sb(scoped_query("cc_measure_sources", clinic_id).order("measure_code")))


async def add_measure_source(clinic_id: str, body: dict) -> dict:
    try:
        # unscoped: insert_scoped_by_payload
        return _rows(await sb(supabase.table("cc_measure_sources").insert({"clinic_id": clinic_id, **body})))[0]
    except Exception as e:
        if "cc_measure_sources_key" in str(e):
            raise HTTPException(status_code=409, detail="This test is already mapped to that measure.")
        raise_for(e)


async def delete_measure_source(clinic_id: str, source_id: str) -> None:
    res = await sb(supabase.table("cc_measure_sources").delete().eq("clinic_id", clinic_id).eq("id", source_id))
    if not _rows(res):
        raise HTTPException(status_code=404, detail="Mapping not found.")


LOINC = {"4548-4": "hba1c", "1558-6": "fbg", "3016-3": "tsh", "13457-7": "ldl", "2160-0": "creatinine", "1742-6": "alt",
         "6690-2": "wbc", "718-7": "hb"}


async def seed_measure_sources(clinic_id: str) -> int:
    """Map lab tests whose LOINC code is known (HbA1c 4548-4, FBS 1558-6, TSH 3016-3, …); never overwrites."""
    tests = _rows(await sb(scoped_query("lab_tests", clinic_id, "id, loinc_code").not_.is_("loinc_code", "null").limit(1000)))
    n = 0
    for t in tests:
        m = LOINC.get((t.get("loinc_code") or "").strip())
        if m:
            try:
                await add_measure_source(clinic_id, {"lab_test_id": t["id"], "measure_code": m})
                n += 1
            except HTTPException:
                pass
    return n
