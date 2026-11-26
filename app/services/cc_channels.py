"""Continuous Care — channels: templates, schedule materialisation, the care card dispatch, messaging budget, voice
enqueue (migration 147; docs/continuous-care/CHANNELS.md, ADR-CC-10 / ADR-CC-17). Zero-LLM: every patient-facing word is a
fixed, versioned text; nothing is generated.

Rules: consent live + WhatsApp opt-in + not opted out (STOP) before any care message; in-window → interactive card
(no quota), out of window → the clinic-approved utility template, which reserves one unit of quota kind 'care' and is
capped at the clinic's share of its Meta messaging tier (halved at quality MEDIUM, stopped at LOW); a failed send
releases the quota. Safety cards and replies to a patient's own message never reserve quota and are never capped.
The clinic's own WABA and number only (whatsapp_service refuses platform credentials in multi-tenant mode).
"""

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from app.database import fetch_all, sb, scoped_query, supabase
from app.services.cc import _now, _rows, get_settings
from app.utils.helpers import today_ist

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
QUOTA_KIND = "care"
DEFAULT_CARE_LIMIT = 2000

TEMPLATES = {
    "cc_consent_invite_v1": {
        "body": "Hello {{1}}. Dr {{2}} at {{3}} has invited you to the {{4}} care programme on WhatsApp: medicine reminders, "
                "reading check-ins and follow-up booking. This is an automated service; our care team reads replies {{5}}. "
                "How we use your data: {{6}}. You can stop any time by replying STOP CARE.",
        "example": ["Lakshmi", "Rao", "City Hospital", "Diabetes", "Mon-Sat 9 am-6 pm", "see the notice we gave you"],
        "buttons": ["Join", "Not now", "Talk to staff"]},
    "cc_care_card_v1": {
        "body": "Good morning {{1}}. Today's medicines from {{2}}: {{3}}. Did you take your {{4}} dose?",
        "example": ["Lakshmi", "City Hospital", "your morning medicines", "morning"],
        "buttons": ["Taken", "Missed", "Send reading"]},
    "cc_reading_request_v1": {
        "body": "{{1}}, {{2}} would like your {{3}} reading today. Tap below, then type the number.",
        "example": ["Lakshmi", "City Hospital", "fasting sugar"], "buttons": ["Send reading", "Not today"]},
    "cc_lab_due_v1": {
        "body": "{{1}}, your {{2}} test is due on {{3}}.", "example": ["Lakshmi", "HbA1c", "12 Dec"],
        "buttons": ["Book home sample", "Book at hospital", "Already done"]},
    "cc_visit_due_v1": {
        "body": "{{1}}, your follow-up with Dr {{2}} is due on {{3}}.", "example": ["Lakshmi", "Rao", "12 Dec"],
        "buttons": ["Book", "Call me"]},
    "cc_phq_start_v1": {
        "body": "{{1}}, it is time for your two-weekly wellbeing check from {{2}}. It has 9 short questions.",
        "example": ["Lakshmi", "City Hospital"], "buttons": ["Start", "Later"]},
    "cc_visit_booked_v1": {
        "body": "{{1}}, your follow-up with Dr {{2}} is booked for {{3}} at {{4}}, {{5}}. Booking ref {{6}}.",
        "example": ["Lakshmi", "Rao", "Tue 3 Dec", "10:20 am", "City Hospital", "BK-1234"], "buttons": ["Can't come"]},
    "cc_service_ended_v1": {
        "body": "{{1}}, the {{2}} WhatsApp care programme has ended. Please contact the hospital on {{3}} for your follow-up.",
        "example": ["Lakshmi", "City Hospital", "040 1234 5678"], "buttons": []},
}
#: Reply-button titles must fit Meta's 20-character limit in every language (UI spec §7.11, U10)
BUTTON_TITLES = {
    "taken": {"en": "Taken", "hi": "ले ली", "te": "వేసుకున్నాను"},
    "missed": {"en": "Missed", "hi": "छूट गई", "te": "మిస్ అయింది"},
    "read": {"en": "Send reading", "hi": "रीडिंग भेजें", "te": "రీడింగ్ పంపండి"},
    "skip": {"en": "Not today", "hi": "आज नहीं", "te": "ఈరోజు కాదు"},
    "join": {"en": "Join", "hi": "शामिल हों", "te": "చేరండి"},
    "no": {"en": "Not now", "hi": "अभी नहीं", "te": "ఇప్పుడు కాదు"},
    "staff": {"en": "Talk to staff", "hi": "स्टाफ से बात", "te": "సిబ్బందితో మాట్లాడు"},
    "stop": {"en": "Stop care messages", "hi": "केयर संदेश बंद", "te": "కేర్ సందేశాలు ఆపండి"},
}
TEXTS = {
    "type_number": {"en": "Please type just the number of your {measure} reading (for example 142).",
                    "hi": "कृपया अपनी {measure} रीडिंग का केवल अंक लिखें (जैसे 142)।",
                    "te": "దయచేసి మీ {measure} రీడింగ్ సంఖ్య మాత్రమే టైప్ చేయండి (ఉదా: 142)."},
    "type_bp": {"en": "Please type your BP like 130/80.", "hi": "कृपया अपना बीपी ऐसे लिखें: 130/80।",
                "te": "దయచేసి మీ బీపీని ఇలా టైప్ చేయండి: 130/80."},
    "check_number": {"en": "That number looks unusual. Please check it and type it again.",
                     "hi": "यह अंक असामान्य लगता है। कृपया जाँचकर फिर लिखें।",
                     "te": "ఈ సంఖ్య అసాధారణంగా ఉంది. దయచేసి సరిచూసి మళ్లీ టైప్ చేయండి."},
    "thanks": {"en": "Thank you, {name}. Your {measure} reading is saved for your care team.",
               "hi": "धन्यवाद, {name}। आपकी {measure} रीडिंग आपकी केयर टीम के लिए सेव हो गई है।",
               "te": "ధన్యవాదాలు, {name}. మీ {measure} రీడింగ్ మీ కేర్ బృందం కోసం సేవ్ అయింది."},
    "adh_thanks": {"en": "Thank you, noted.", "hi": "धन्यवाद, दर्ज कर लिया।", "te": "ధన్యవాదాలు, నమోదు చేశాం."},
    "joined": {"en": "Welcome to the care programme. Your first card comes at your next medicine time. Reply STOP CARE to stop.",
               "hi": "केयर कार्यक्रम में स्वागत है। पहला संदेश अगली दवा के समय आएगा। बंद करने के लिए STOP CARE लिखें।",
               "te": "కేర్ కార్యక్రమానికి స్వాగతం. మొదటి సందేశం మీ తదుపరి మందు సమయానికి వస్తుంది. ఆపడానికి STOP CARE అని పంపండి."},
    "declined": {"en": "No problem. You can ask the hospital to join any time.",
                 "hi": "कोई बात नहीं। आप कभी भी अस्पताल से जुड़ने को कह सकते हैं।",
                 "te": "పర్వాలేదు. మీరు ఎప్పుడైనా చేరమని ఆసుపత్రిని అడగవచ్చు."},
    "staff": {"en": "Our care team will call you during working hours.",
              "hi": "हमारी केयर टीम कार्य-समय में आपको कॉल करेगी।", "te": "మా కేర్ బృందం పని వేళల్లో మీకు కాల్ చేస్తుంది."},
    "stopped": {"en": "Care messages are stopped. Your hospital records are kept as before. You can rejoin at the hospital.",
                "hi": "केयर संदेश बंद कर दिए गए। आपके अस्पताल रिकॉर्ड पहले जैसे रहेंगे। आप अस्पताल में फिर से जुड़ सकते हैं।",
                "te": "కేర్ సందేశాలు ఆపేశాం. మీ ఆసుపత్రి రికార్డులు ఎప్పటిలాగే ఉంటాయి. ఆసుపత్రిలో మళ్లీ చేరవచ్చు."},
    "whose": {"en": "Whose reading is this?", "hi": "यह रीडिंग किसकी है?", "te": "ఈ రీడింగ్ ఎవరిది?"},
    "phq_q": {"en": "Over the last 2 weeks, how often: {q}", "hi": "पिछले 2 हफ़्तों में कितनी बार: {q}",
              "te": "గత 2 వారాల్లో ఎంత తరచుగా: {q}"},
    "phq_done": {"en": "Thank you for answering. Your care team will see your answers.",
                 "hi": "जवाब देने के लिए धन्यवाद। आपकी केयर टीम आपके जवाब देखेगी।",
                 "te": "సమాధానాలకు ధన్యవాదాలు. మీ కేర్ బృందం మీ సమాధానాలు చూస్తుంది."},
    "card_header": {"en": "💊 Today's medicines", "hi": "💊 आज की दवाइयाँ", "te": "💊 ఈరోజు మందులు"},
    "reading_card": {"en": "📈 Your care team would like your {measure} reading today.",
                     "hi": "📈 आपकी केयर टीम आज आपकी {measure} रीडिंग चाहती है।",
                     "te": "📈 మీ కేర్ బృందం ఈరోజు మీ {measure} రీడింగ్ కోరుకుంటోంది."},
    "footer": {"en": "Replies read {hours}. Emergency: {emergency}. Reply STOP CARE to stop.",
               "hi": "जवाब {hours} पढ़े जाते हैं। आपात: {emergency}। बंद करने के लिए STOP CARE लिखें।",
               "te": "సమాధానాలు {hours} చదువుతాం. అత్యవసరం: {emergency}. ఆపడానికి STOP CARE పంపండి."},
}
MEASURE_NAMES = {"fbg": {"en": "fasting sugar", "hi": "फास्टिंग शुगर", "te": "ఉపవాస షుగర్"},
                 "ppbg": {"en": "after-meal sugar", "hi": "खाने के बाद शुगर", "te": "భోజనం తర్వాత షుగర్"},
                 "rbg": {"en": "sugar", "hi": "शुगर", "te": "షుగర్"}, "bp": {"en": "BP", "hi": "बीपी", "te": "బీపీ"},
                 "weight": {"en": "weight", "hi": "वज़न", "te": "బరువు"},
                 "hba1c": {"en": "HbA1c", "hi": "HbA1c", "te": "HbA1c"}}


def t(key: str, lang: str, **kw) -> str:
    table = TEXTS[key]
    return table.get(lang if lang in table else "en").format(**kw)


def safe_name(name: Optional[str]) -> str:
    """Loophole X8: template variables carry letters, spaces and dots only, ≤ 40 characters."""
    return re.sub(r"[^\w .]", "", (name or "").split(" ")[0] if name else "", flags=re.UNICODE)[:40].strip() or "there"


def meta_payload(name: str, lang: str = "en") -> dict:
    tpl = TEMPLATES[name]
    comps: list = [{"type": "BODY", "text": tpl["body"], "example": {"body_text": [tpl["example"]]}}]
    if tpl["buttons"]:
        comps.append({"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": b} for b in tpl["buttons"]]})
    return {"name": name, "language": lang, "category": "UTILITY", "components": comps}


async def submit_templates(clinic: dict) -> list:
    """Create the CC templates in the clinic's own WhatsApp Business Account (dental submit_templates pattern)."""
    import httpx
    from app.services.dental_plans import _meta_account, _templates_url
    token, waba = _meta_account(clinic)
    out = []
    async with httpx.AsyncClient(timeout=20) as client:
        for name in TEMPLATES:
            try:
                r = await client.post(_templates_url(waba), json=meta_payload(name),
                                      headers={"Authorization": f"Bearer {token}"})
                data = r.json() if r.content else {}
                if r.status_code < 300:
                    out.append({"name": name, "result": "submitted", "status": data.get("status")})
                else:
                    err = data.get("error") or {}
                    msg = err.get("error_user_msg") or err.get("message") or f"HTTP {r.status_code}"
                    out.append({"name": name, "result": "exists" if "already exists" in str(msg).lower() else "error",
                                "detail": str(msg)[:300]})
            except Exception as e:                        # noqa: BLE001
                out.append({"name": name, "result": "error", "detail": f"Network error: {type(e).__name__}"})
    return out


async def refresh_template_statuses(clinic: dict) -> dict:
    """Read the CC templates' Meta statuses into cc_settings.templates (the dispatch reads only this)."""
    import httpx
    from app.services.dental_plans import _meta_account, _templates_url
    token, waba = _meta_account(clinic)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(_templates_url(waba), params={"fields": "name,status,quality_score", "limit": 200},
                             headers={"Authorization": f"Bearer {token}"})
    found = {x["name"]: x.get("status") for x in (r.json().get("data", []) if r.status_code < 300 else [])
             if x.get("name") in TEMPLATES}
    await _merge_settings(clinic, {"templates": {n: found.get(n, "NOT_SUBMITTED") for n in TEMPLATES}})
    return {n: found.get(n, "NOT_SUBMITTED") for n in TEMPLATES}


async def _merge_settings(clinic: dict, patch: dict) -> None:
    cur = clinic.get("cc_settings") or {}
    merged = {**cur, **patch}
    # unscoped: platform_admin
    await sb(supabase.table("clinics").update({"cc_settings": merged}).eq("id", clinic["id"]))
    clinic["cc_settings"] = merged
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()


def template_approved(clinic: dict, name: str) -> bool:
    return ((clinic.get("cc_settings") or {}).get("templates") or {}).get(name) == "APPROVED"


# ── messaging budget (ADR-CC-17) ─────────────────────────────────────────────

TIER_LIMITS = {"TIER_50": 50, "TIER_250": 250, "TIER_1K": 1000, "TIER_10K": 10000, "TIER_100K": 100000,
               "TIER_UNLIMITED": 1000000}


async def refresh_wa_health(clinic: dict) -> dict:
    """Daily Graph API read of the number's messaging tier and quality rating into cc_settings.wa_health."""
    import httpx
    from app.config import settings as app_settings
    cfg = clinic.get("config") or {}
    token, number_id = cfg.get("meta_access_token"), cfg.get("whatsapp_phone_number_id")
    if not token or not number_id:
        return {}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"https://graph.facebook.com/{app_settings.whatsapp_api_version}/{number_id}",
                             params={"fields": "messaging_limit_tier,quality_rating"},
                             headers={"Authorization": f"Bearer {token}"})
    if r.status_code >= 300:
        return {}
    d = r.json()
    health = {"tier": d.get("messaging_limit_tier"), "quality": (d.get("quality_rating") or "").upper() or None,
              "read_at": _now()}
    await _merge_settings(clinic, {"wa_health": health})
    if health["quality"] == "RED" or health["quality"] == "LOW":
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("admin_notifications").insert({
            "clinic_id": clinic["id"], "admin_id": None, "is_read": False,
            "title": "[Care] WhatsApp quality is LOW — care templates paused",
            "message": "Meta rated this number's quality LOW. Care templates are paused so appointment reminders, "
                       "reports and payment links keep working. In-window care replies continue."}))
    return health


def template_budget(clinic: dict) -> Optional[int]:
    """Care templates allowed per rolling 24 h; None = unknown tier (fall back to the quota alone); 0 = stopped."""
    s = get_settings(clinic)
    health = s.get("wa_health") or {}
    tier = TIER_LIMITS.get(health.get("tier") or "")
    if tier is None:
        return None
    share = int(s.get("care_tier_share") or 40)
    q = (health.get("quality") or "").upper()
    if q in ("LOW", "RED"):
        return 0
    if q in ("MEDIUM", "YELLOW"):
        share = share // 2
    return tier * share // 100


async def templates_sent_24h(clinic_id: str) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return len(_rows(await sb(scoped_query("cc_prompts", clinic_id, "id").eq("channel", "whatsapp_template")
                              .gte("sent_at", since).limit(100000))))


async def reserve(clinic: dict) -> bool:
    from app.services.dental_plans import current_month
    limit = ((clinic.get("config") or {}).get("dental_message_limits") or {}).get("care", DEFAULT_CARE_LIMIT)
    try:
        res = await sb(supabase.rpc("reserve_message_quota", {"p_clinic_id": clinic["id"], "p_month": current_month(),
                                                               "p_kind": QUOTA_KIND, "p_limit": limit}))
        used = res.data if isinstance(res.data, int) and not isinstance(res.data, bool) else -1
    except Exception as e:                                # noqa: BLE001 - fail closed on spend
        logger.error(f"CC_QUOTA_RESERVE_FAILED clinic={clinic.get('id')}: {type(e).__name__}")
        return False
    return used >= 0


async def release(clinic: dict) -> None:
    from app.services.dental_plans import current_month
    try:
        await sb(supabase.rpc("release_message_quota", {"p_clinic_id": clinic["id"], "p_month": current_month(),
                                                         "p_kind": QUOTA_KIND}))
    except Exception as e:                                # noqa: BLE001
        logger.error(f"CC_QUOTA_RELEASE_FAILED clinic={clinic.get('id')}: {type(e).__name__}")


# ── eligibility ──────────────────────────────────────────────────────────────

def sent_stop(patient: dict) -> bool:
    """The patient replied STOP (conversation.py sets opted_in False AND opted_out_at; START sets opted_in True).
    A plain opted_in False is only the column default for a counter-registered patient who never chatted — the care
    WhatsApp opt-in is recorded in cc_consents.wa_optin_at instead (W2 step 0, VERIFICATION D7)."""
    return patient.get("opted_in") is False and bool(patient.get("opted_out_at"))


async def reach(clinic: dict, enrollment: dict) -> tuple:
    """(phone, lang, person_name, failure_code) — failure_code None means we may send a care message."""
    from app.services.cc import live_consent
    cid = clinic["id"]
    if enrollment["status"] != "active":
        return None, "en", None, "opted_out"
    p = _rows(await sb(scoped_query("patients", cid, "id, name, phone, opted_in, opted_out_at")
                       .eq("id", enrollment["patient_id"]).limit(1)))
    if not p or not p[0].get("phone"):
        return None, "en", None, "opted_out"
    if sent_stop(p[0]):                                   # STOP (consent.accepts_engagement)
        return None, "en", None, "opted_out"
    consent = await live_consent(cid, enrollment["patient_id"], enrollment.get("family_member_id"))
    if not consent or not consent.get("granted_at"):
        return None, "en", None, "opted_out"
    if not consent.get("wa_optin_at"):
        return None, "en", None, "no_optin"
    name = p[0].get("name")
    if enrollment.get("family_member_id"):
        fm = _rows(await sb(scoped_query("family_members", cid, "full_name").eq("id", enrollment["family_member_id"]).limit(1)))
        name = fm[0]["full_name"] if fm else name
    return p[0]["phone"], enrollment.get("language") or "en", name, None


# ── schedule materialisation (CC-29) ─────────────────────────────────────────

def due_times(item: dict, day: date) -> list:
    """The IST datetimes a plan item is due on `day` (pure)."""
    sch = item.get("schedule") or {}
    start = date.fromisoformat(str(item["starts_on"])[:10])
    end = date.fromisoformat(str(item["ends_on"])[:10]) if item.get("ends_on") else None
    if day < start or (end and day > end):
        return []
    if item["prompt_mode"] == "adherence":
        times = sch.get("times") or []
    elif item["prompt_mode"] == "reading":
        if sch.get("every_days") and (day - start).days % int(sch["every_days"]) != 0:
            return []
        if sch.get("weekly") and day.strftime("%a").lower()[:3] not in sch["weekly"]:
            return []
        times = [sch.get("time") or "08:00"]
    else:
        return []
    return [datetime.combine(day, time(*map(int, x.split(":"))), tzinfo=IST) for x in times]


async def materialize(clinic: dict, day: Optional[date] = None) -> int:
    """Nightly: tomorrow's due items for every active enrollment's active plan (idempotent upsert)."""
    cid = clinic["id"]
    day = day or (today_ist() + timedelta(days=1))
    n = 0
    for e in await fetch_all(lambda: scoped_query("cc_enrollments", cid, "id").eq("status", "active"), label="cc:mat"):
        plan = _rows(await sb(scoped_query("cc_care_plans", cid, "id").eq("enrollment_id", e["id"]).eq("status", "active")
                              .limit(1)))
        if not plan:
            continue
        items = _rows(await sb(scoped_query("cc_plan_items", cid).eq("plan_id", plan[0]["id"])
                               .in_("prompt_mode", ["adherence", "reading"])))
        rows = [{"clinic_id": cid, "enrollment_id": e["id"], "plan_item_id": it["id"], "due_at": dt.isoformat(),
                 "kind": it["kind"]} for it in items for dt in due_times(it, day)]
        if rows:
            # unscoped: insert_scoped_by_payload
            res = await sb(supabase.table("cc_schedule_items").upsert(rows, on_conflict="clinic_id,plan_item_id,due_at",
                                                                      ignore_duplicates=True))
            n += len(_rows(res))
    return n


# ── the dispatch (W4) ────────────────────────────────────────────────────────

def in_card_window(clinic: dict, now: datetime) -> bool:
    w = get_settings(clinic)["card_window"]
    hhmm = now.astimezone(IST).strftime("%H:%M")
    return w["from"] <= hhmm <= w["to"]


async def dispatch(clinic: dict, now: Optional[datetime] = None) -> dict:
    """Every 5 min (CAS-locked): bundle each enrollment's due items into one card per due time."""
    from app.services.cc_hooks import record_unanswered
    cid = clinic["id"]
    now = now or datetime.now(timezone.utc)
    stats = {"sent": 0, "skipped": 0, "failed": 0}
    if not in_card_window(clinic, now):
        return stats
    due = _rows(await sb(scoped_query("cc_schedule_items", cid).eq("status", "due").lte("due_at", now.isoformat())
                         .gte("due_at", (now - timedelta(hours=3)).isoformat()).order("due_at").limit(500)))
    by: dict = {}
    for d in due:
        by.setdefault((d["enrollment_id"], d["due_at"]), []).append(d)
    for (enr_id, _due_at), group in by.items():
        e = _rows(await sb(scoped_query("cc_enrollments", cid).eq("id", enr_id).limit(1)))
        if not e:
            continue
        out = await send_card(clinic, e[0], group)
        stats[out] = stats.get(out, 0) + 1
    # cards nobody answered by the end of the day count towards the voice fallback / not-responding case
    await record_unanswered(clinic, now)
    return stats


async def send_card(clinic: dict, enrollment: dict, group: list) -> str:
    from app.services.whatsapp import whatsapp_service
    cid = clinic["id"]
    phone, lang, name, failure = await reach(clinic, enrollment)
    items = _rows(await sb(scoped_query("cc_plan_items", cid).in_("id", [g["plan_item_id"] for g in group])))
    reading = next((i for i in items if i["prompt_mode"] == "reading"), None)
    kind = ("phq9" if reading and reading["measure_code"] in ("phq9",) else "gad7" if reading and reading["measure_code"] == "gad7"
            else "reading" if reading else "adherence")
    prompt = {"clinic_id": cid, "enrollment_id": enrollment["id"], "schedule_item_ids": [g["id"] for g in group],
              "kind": kind, "measure_code": reading["measure_code"] if reading else None, "phone": phone or "+910000000000",
              "channel": "whatsapp_interactive", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()}
    if failure:
        await _record_prompt({**prompt, "status": "failed", "failure_code": failure})
        await _mark(cid, group, "skipped")
        return "skipped"
    window = await whatsapp_service._can_send_freeform(clinic, phone)
    if not window:
        tpl = "cc_reading_request_v1" if kind == "reading" else "cc_phq_start_v1" if kind in ("phq9", "gad7") else "cc_care_card_v1"
        budget = template_budget(clinic)
        code = None
        if not template_approved(clinic, tpl):
            code = "template_unapproved"
        elif budget is not None and await templates_sent_24h(cid) >= budget:
            code = "budget"
        elif not await reserve(clinic):
            code = "quota"
        if code:
            await _record_prompt({**prompt, "channel": "whatsapp_template", "template_name": tpl, "status": "failed",
                                  "failure_code": code})
            await _mark(cid, group, "missed")
            return "skipped"
        row = await _record_prompt({**prompt, "channel": "whatsapp_template", "template_name": tpl, "quota_reserved": True})
        ok, mid = await _send_template(clinic, phone, tpl, lang, name, row, reading, items)
        if not ok:
            await release(clinic)
            await _update_prompt(cid, row["id"], {"status": "failed", "failure_code": "meta_error", "quota_reserved": False})
            await _mark(cid, group, "missed")
            return "failed"
        await _update_prompt(cid, row["id"], {"status": "sent", "sent_at": _now(), "wa_message_id": mid})
        await _mark(cid, group, "prompted")
        return "sent"
    row = await _record_prompt(prompt)
    ok = await _send_interactive(clinic, phone, lang, name, row, reading, items)
    if not ok:
        await _update_prompt(cid, row["id"], {"status": "failed", "failure_code": "meta_error"})
        await _mark(cid, group, "missed")
        return "failed"
    await _update_prompt(cid, row["id"], {"status": "sent", "sent_at": _now()})
    await _mark(cid, group, "prompted")
    return "sent"


def _footer(clinic: dict, lang: str) -> str:
    from app.config import settings as app_settings
    from app.services.tenant import get_clinic_contact
    s = get_settings(clinic)
    hours = f"{s['reply_hours']['days']} {s['reply_hours']['from']}–{s['reply_hours']['to']}"
    emergency = s.get("emergency_number") or get_clinic_contact(clinic, "emergency_number",
                                                                app_settings.hospital_emergency_number) or "112"
    return t("footer", lang, hours=hours, emergency=emergency)


async def _send_interactive(clinic, phone, lang, name, prompt, reading, items) -> bool:
    from app.services.whatsapp import whatsapp_service
    if reading and reading["measure_code"] in ("phq9", "gad7"):
        from app.services.cc_hooks import send_phq_question
        await _update_prompt(clinic["id"], prompt["id"], {"status": "awaiting_value", "state": {"answers": []}})
        return await send_phq_question(clinic, phone, lang, prompt, 0)
    meds = [i["display_text"] for i in items if i["prompt_mode"] == "adherence"]
    who = f"For {safe_name(name)}: " if name else ""
    if reading:
        body = t("reading_card", lang, measure=MEASURE_NAMES.get(reading["measure_code"], {}).get(lang, reading["measure_code"]))
        buttons = [{"id": f"cc:read:{prompt['id']}", "title": BUTTON_TITLES["read"][lang]},
                   {"id": f"cc:skip:{prompt['id']}", "title": BUTTON_TITLES["skip"][lang]}]
    else:
        body = t("card_header", lang) + "\n" + "\n".join(f"• {m}" for m in meds)
        buttons = [{"id": f"cc:adh:{prompt['id']}:taken", "title": BUTTON_TITLES["taken"][lang]},
                   {"id": f"cc:adh:{prompt['id']}:missed", "title": BUTTON_TITLES["missed"][lang]},
                   {"id": f"cc:off:{prompt['enrollment_id']}", "title": BUTTON_TITLES["stop"][lang]}]
    return bool(await whatsapp_service.send_interactive_buttons(
        clinic, phone, f"{who}{body}\n\n{_footer(clinic, lang)}"[:1024], buttons, header=(clinic.get("name") or "")[:60],
        _source="cc_card"))


async def _send_template(clinic, phone, tpl, lang, name, prompt, reading, items) -> tuple:
    from app.services.whatsapp import whatsapp_service
    show = get_settings(clinic).get("card_show_medicine_names")
    meds = ", ".join(i["display_text"].split(" · ")[0] for i in items if i["prompt_mode"] == "adherence")
    hosp = (clinic.get("name") or "the hospital")[:60]
    if tpl == "cc_care_card_v1":
        params = [safe_name(name), hosp, meds[:120] if show and meds else "your medicines", "today's"]
        payloads = [f"cc:adh:{prompt['id']}:taken", f"cc:adh:{prompt['id']}:missed", f"cc:read:{prompt['id']}"]
    elif tpl == "cc_reading_request_v1":
        params = [safe_name(name), hosp, MEASURE_NAMES.get((reading or {}).get("measure_code"), {}).get("en", "reading")]
        payloads = [f"cc:read:{prompt['id']}", f"cc:skip:{prompt['id']}"]
    else:
        params = [safe_name(name), hosp]
        payloads = [f"cc:phq:{prompt['id']}:start", f"cc:skip:{prompt['id']}"]
    comps = [{"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}] + [
        {"type": "button", "sub_type": "quick_reply", "index": str(i), "parameters": [{"type": "payload", "payload": pl}]}
        for i, pl in enumerate(payloads)]
    cap: dict = {}
    ok = await whatsapp_service.send_template(clinic, phone, tpl, language=lang, components=comps, _source="cc_card",
                                              _capture=cap)
    return bool(ok), cap.get("meta_message_id")


async def _record_prompt(row: dict) -> dict:
    # unscoped: insert_scoped_by_payload
    return _rows(await sb(supabase.table("cc_prompts").insert(row)))[0]


async def _update_prompt(clinic_id: str, prompt_id: str, patch: dict) -> None:
    await sb(supabase.table("cc_prompts").update({**patch, "updated_at": _now()}).eq("clinic_id", clinic_id).eq("id", prompt_id))


async def _mark(clinic_id: str, group: list, status: str) -> None:
    await sb(supabase.table("cc_schedule_items").update({"status": status}).eq("clinic_id", clinic_id)
             .in_("id", [g["id"] for g in group]).eq("status", "due"))


# ── one-off messages (consent invite, visit booked, service ended, safety) ───

async def send_consent_invite(clinic: dict, enrollment: dict, doctor_name: str) -> dict:
    """W2: only after a recorded WhatsApp opt-in (VERIFICATION D7) — never a cold template."""
    from app.services.cc import live_consent, notice
    from app.services.whatsapp import whatsapp_service
    cid = clinic["id"]
    consent = await live_consent(cid, enrollment["patient_id"], enrollment.get("family_member_id"))
    if not consent or not consent.get("wa_optin_at"):
        return {"sent": False, "reason": "no_optin"}
    p = _rows(await sb(scoped_query("patients", cid, "name, phone, opted_in, opted_out_at")
                       .eq("id", enrollment["patient_id"]).limit(1)))
    if not p or sent_stop(p[0]):
        return {"sent": False, "reason": "opted_out"}
    lang = enrollment.get("language") or "en"
    s = get_settings(clinic)
    hours = f"{s['reply_hours']['days']} {s['reply_hours']['from']}–{s['reply_hours']['to']}"
    phone = p[0]["phone"]
    title = enrollment["protocol_code"].replace("_", " ")
    buttons = [{"id": f"cc:consent:{enrollment['id']}:yes", "title": BUTTON_TITLES["join"][lang]},
               {"id": f"cc:consent:{enrollment['id']}:no", "title": BUTTON_TITLES["no"][lang]},
               {"id": f"cc:staff:{enrollment['id']}", "title": BUTTON_TITLES["staff"][lang]}]
    row = await _record_prompt({"clinic_id": cid, "enrollment_id": enrollment["id"], "kind": "consent_invite",
                                "channel": "whatsapp_interactive", "phone": phone})
    if await whatsapp_service._can_send_freeform(clinic, phone):
        text = notice(clinic, lang)["text"]
        ok = await whatsapp_service.send_interactive_buttons(clinic, phone, text[:1024], buttons,
                                                             header=(clinic.get("name") or "")[:60], _source="cc_consent")
        mid = None
    else:
        if not template_approved(clinic, "cc_consent_invite_v1") or not await reserve(clinic):
            await _update_prompt(cid, row["id"], {"status": "failed", "failure_code": "template_unapproved"})
            return {"sent": False, "reason": "template_unapproved"}
        comps = [{"type": "body", "parameters": [{"type": "text", "text": x} for x in (
            safe_name(p[0].get("name")), doctor_name[:40], (clinic.get("name") or "")[:60], title, hours,
            "the notice we gave you")]}] + [
            {"type": "button", "sub_type": "quick_reply", "index": str(i), "parameters": [{"type": "payload", "payload": b["id"]}]}
            for i, b in enumerate(buttons)]
        cap: dict = {}
        ok = await whatsapp_service.send_template(clinic, phone, "cc_consent_invite_v1", language=lang, components=comps,
                                                  _source="cc_consent", _capture=cap)
        mid = cap.get("meta_message_id")
        if not ok:
            await release(clinic)
        await _update_prompt(cid, row["id"], {"channel": "whatsapp_template", "template_name": "cc_consent_invite_v1",
                                              "quota_reserved": bool(ok)})
    await _update_prompt(cid, row["id"], {"status": "sent" if ok else "failed", "sent_at": _now() if ok else None,
                                          "failure_code": None if ok else "meta_error", "wa_message_id": mid})
    return {"sent": bool(ok)}


async def send_visit_booked(clinic: dict, enrollment: dict, appt: dict) -> bool:
    """book_appointment sends nothing, so CC confirms the booking itself — once (VERIFICATION D5)."""
    from app.services.whatsapp import whatsapp_service
    phone, lang, name, failure = await reach(clinic, enrollment)
    if failure:
        return False
    when = datetime.fromisoformat(str(appt["appointment_date"])[:10])
    day = when.strftime("%a %-d %b")
    hhmm = datetime.strptime(str(appt["appointment_time"])[:5], "%H:%M").strftime("%-I:%M %p").lower()
    doctor = (appt.get("doctor_name") or "").replace("Dr. ", "").replace("Dr ", "")
    if await whatsapp_service._can_send_freeform(clinic, phone):
        return bool(await whatsapp_service.send_text(
            clinic, phone, f"✅ {safe_name(name)}, your follow-up with Dr {doctor} is booked for {day} at {hhmm}, "
                           f"{clinic.get('name') or ''}. Booking ref {appt.get('booking_ref') or ''}.", _source="cc_visit"))
    if not template_approved(clinic, "cc_visit_booked_v1") or not await reserve(clinic):
        return False
    comps = [{"type": "body", "parameters": [{"type": "text", "text": str(x)} for x in (
        safe_name(name), doctor[:40] or "the doctor", day, hhmm, (clinic.get("name") or "")[:60],
        appt.get("booking_ref") or "—")]},
             {"type": "button", "sub_type": "quick_reply", "index": "0",
              "parameters": [{"type": "payload", "payload": f"cc:visit:{appt['id']}:cant"}]}]
    ok = await whatsapp_service.send_template(clinic, phone, "cc_visit_booked_v1", language=lang, components=comps,
                                              _source="cc_visit")
    if not ok:
        await release(clinic)
    return bool(ok)


async def send_service_ended(clinic: dict) -> int:
    """W12: one message to each active enrollment when the owner winds CC down."""
    from app.services.whatsapp import whatsapp_service
    from app.services.tenant import get_clinic_contact
    cid = clinic["id"]
    n = 0
    for e in await fetch_all(lambda: scoped_query("cc_enrollments", cid).in_("status", ["active", "paused"]), label="cc:end"):
        phone, lang, name, failure = await reach(clinic, {**e, "status": "active"})
        phone_no = get_clinic_contact(clinic, "phone", clinic.get("whatsapp_number") or "")
        if failure:                                    # cannot be messaged: still ended, so the wind-down can finish
            ok = False
        elif await whatsapp_service._can_send_freeform(clinic, phone):
            ok = await whatsapp_service.send_text(clinic, phone, f"{safe_name(name)}, the {clinic.get('name') or ''} WhatsApp "
                                                  f"care programme has ended. Please contact the hospital on {phone_no} for "
                                                  "your follow-up.", _source="cc_end")
        elif template_approved(clinic, "cc_service_ended_v1"):
            ok = await whatsapp_service.send_template(clinic, phone, "cc_service_ended_v1", language=lang, components=[
                {"type": "body", "parameters": [{"type": "text", "text": x} for x in (
                    safe_name(name), (clinic.get("name") or "")[:60], phone_no or "—")]}], _source="cc_end")
        else:
            ok = False
        n += bool(ok)
        await sb(supabase.table("cc_enrollments").update({"status": "ended_by_clinic", "ended_at": _now(),
                                                          "end_reason": "care programme switched off", "updated_at": _now()})
                 .eq("clinic_id", cid).eq("id", e["id"]))
    return n


async def send_safety(clinic: dict, phone: str, lang: str, card_key: str) -> bool:
    """In the same request as the reading (ADR-CC-07): an in-window reply, never quota, never capped. A card with no
    approved text falls back to the Kriya default only for crisis_support / hypo_now (PROTOCOLS §4)."""
    from app.services.cc import card_text
    from app.services.whatsapp import whatsapp_service
    text = await card_text(clinic, card_key, lang)
    if not text:
        logger.error(f"CC safety card {card_key} has no approved text clinic={clinic.get('id')}")
        return False
    return bool(await whatsapp_service.send_text(clinic, phone, text, _source="cc_safety", _window_open=True))


# ── voice (W5, ADR-CC-16) ────────────────────────────────────────────────────

async def enqueue_voice(clinic: dict, enrollment: dict, reason: str = "fallback") -> Optional[str]:
    """One care check-in call through the existing voice outbound rules (eligibility, window, do-not-call)."""
    from app.services.tenant import ai_receptionist_enabled
    from app.voice import outbound
    if not ai_receptionist_enabled(clinic):
        return None
    phone, lang, name, failure = await reach(clinic, enrollment)
    if failure:
        return None
    from app.services.cc_voice import due_items
    items = await due_items(clinic, enrollment)
    if not items:
        return None
    try:
        job = await outbound.enqueue(clinic, phone, "care_checkin", {
            "care": {"enrollment_id": enrollment["id"], "language": lang, "first_name": safe_name(name),
                     "reason": reason, "items": items}}, "cc")
    except outbound.OutboundRefused:
        return None
    await sb(supabase.table("cc_enrollments").update({"last_voice_attempt_at": _now(), "updated_at": _now()})
             .eq("clinic_id", clinic["id"]).eq("id", enrollment["id"]))
    return job.get("id")
