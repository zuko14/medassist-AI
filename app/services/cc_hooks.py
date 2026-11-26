"""Continuous Care — the WhatsApp conversation hook (ADR-CC-09 revised; docs/continuous-care/CHANNELS.md §4).

Called from app/services/conversation.py inside the locked handler, for clinics with care_access only, in ONE block
outside the `message_type == "button"` branch, so template quick replies ('button') and in-window taps ('interactive')
both reach it. It claims exactly two things and nothing else:
  1. a tap whose id starts with `cc:` (the tap must belong to this clinic AND this sender's phone);
  2. a typed message that is a strict reading (whole message: glucose `^\\d{2,3}$`, BP `^\\d{2,3}\\s*[/\\- ]\\s*\\d{2,3}$`,
     HbA1c / weight `^\\d{1,3}(\\.\\d)?$`, Devanagari / Telugu digits normalised) while the session is None / idle /
     main_menu AND this phone has an open reading prompt (≤ 12 h, unanswered) for a measure the pattern fits.
Plus the typed "STOP CARE" withdrawal. No conversation state is written; no job touches `conversations`. Any exception
inside returns False, so a CC bug can never swallow a patient's message (X18). Zero-LLM.
"""

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import sb, scoped_query, supabase
from app.services import cc as core
from app.services import cc_channels as ch
from app.services.cc import _now, _rows
from app.utils.validators import mask_phone

logger = logging.getLogger(__name__)
RESTING = (None, "", "idle", "main_menu")
OPEN_PROMPT = ["sent", "delivered", "read", "awaiting_value"]
_DIGITS = str.maketrans("०१२३४५६७८९౦౧౨౩౪౫౬౭౮౯", "01234567890123456789")
GLUCOSE = re.compile(r"^\d{2,3}$")
BP = re.compile(r"^(\d{2,3})\s*[/\- ]\s*(\d{2,3})$")
DECIMAL = re.compile(r"^\d{1,3}(\.\d)?$")
STOP_CARE = {"stop care", "stopcare", "केयर बंद", "केयर बंद करो", "కేర్ ఆపండి", "care band karo", "care aapandi"}
PHQ9 = ("Little interest or pleasure in doing things", "Feeling down, depressed, or hopeless",
        "Trouble falling or staying asleep, or sleeping too much", "Feeling tired or having little energy",
        "Poor appetite or overeating",
        "Feeling bad about yourself — or that you are a failure or have let yourself or your family down",
        "Trouble concentrating on things, such as reading the newspaper or watching television",
        "Moving or speaking so slowly that other people could have noticed, or the opposite — being so fidgety or "
        "restless that you have been moving around a lot more than usual",
        "Thoughts that you would be better off dead, or of hurting yourself in some way")
GAD7 = ("Feeling nervous, anxious, or on edge", "Not being able to stop or control worrying",
        "Worrying too much about different things", "Trouble relaxing", "Being so restless that it is hard to sit still",
        "Becoming easily annoyed or irritable", "Feeling afraid, as if something awful might happen")
ANSWERS = {"en": ("Not at all", "Several days", "More than half the days", "Nearly every day"),
           "hi": ("बिल्कुल नहीं", "कुछ दिन", "आधे से ज़्यादा दिन", "लगभग हर दिन"),
           "te": ("అస్సలు లేదు", "కొన్ని రోజులు", "సగం కంటే ఎక్కువ రోజులు", "దాదాపు ప్రతిరోజూ")}


def normalise(text: str) -> str:
    return unicodedata.normalize("NFC", (text or "")).translate(_DIGITS).strip()


def parse(text: str, measure: str, unit: str = "mg/dL") -> Optional[tuple]:
    """(value, value2) when the WHOLE message is a reading for `measure`, else None (pure)."""
    s = normalise(text)
    if measure == "bp":
        m = BP.fullmatch(s)
        return (float(m.group(1)), float(m.group(2))) if m else None
    if measure in ("fbg", "ppbg", "rbg"):
        if unit == "mmol/L":
            return (round(float(s) * 18.0, 0), None) if re.fullmatch(r"^\d{1,2}(\.\d)?$", s) else None
        return (float(s), None) if GLUCOSE.fullmatch(s) else None
    if measure in ("weight", "hba1c"):
        return (float(s), None) if DECIMAL.fullmatch(s) else None
    return None


def looks_like_reading(text: str) -> bool:
    s = normalise(text)
    return bool(GLUCOSE.fullmatch(s) or BP.fullmatch(s) or DECIMAL.fullmatch(s))


# ── typed text ───────────────────────────────────────────────────────────────

async def handle_typed(manager, clinic: dict, phone: str, state: Optional[str], text: str, message_id: Optional[str]) -> bool:
    try:
        low = " ".join(normalise(text).lower().split())
        if low in STOP_CARE:
            return await _stop_care_typed(manager, clinic, phone)
        digits_only = bool(re.fullmatch(r"[\d\s/.\-]{1,12}", normalise(text)))
        if state not in RESTING or not (looks_like_reading(text) or digits_only):
            return False
        cid = clinic["id"]
        since = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        prompts = _rows(await sb(scoped_query("cc_prompts", cid).eq("phone", phone).in_("status", OPEN_PROMPT)
                                 .eq("kind", "reading").gte("sent_at", since).order("sent_at", desc=True).limit(5)))
        unit = core.get_settings(clinic).get("glucose_unit") or "mg/dL"
        fitting = [p for p in prompts if p.get("measure_code") and parse(text, p["measure_code"], unit)]
        if not fitting:
            # only numbers, right after the patient tapped "Send reading": ask again (twice, then 'unparsed')
            waiting = [p for p in prompts if p["status"] == "awaiting_value" and p.get("measure_code")]
            if digits_only and waiting:
                lang = await _lang(clinic, waiting[0]["enrollment_id"])
                return await _reask(manager, clinic, phone, waiting[0], lang)
            return False                                   # not ours: the normal pipeline answers it, unchanged
        enrollments = {p["enrollment_id"] for p in fitting}
        if len(enrollments) > 1:
            return await _ask_whose(manager, clinic, phone, fitting, normalise(text))
        return await _store_reading(manager, clinic, phone, fitting[0], text, message_id, unit)
    except Exception as e:                                # noqa: BLE001 - X18: never swallow the message
        logger.error(f"CC typed hook failed clinic={clinic.get('id')} phone={mask_phone(phone)}: {type(e).__name__}")
        return False


async def _store_reading(manager, clinic, phone, prompt, text, message_id, unit) -> bool:
    cid = clinic["id"]
    measure = prompt["measure_code"]
    v1, v2 = parse(text, measure, unit)
    lang = await _lang(clinic, prompt["enrollment_id"])
    if not core.plausible(measure, v1, v2):
        return await _reask(manager, clinic, phone, prompt, lang)
    obs = await core.record_observation(cid, prompt["enrollment_id"], measure, value=v1, value2=v2, source="whatsapp_typed",
                                        prompt_id=prompt["id"], message_id=message_id)
    await ch._update_prompt(cid, prompt["id"], {"status": "answered", "answered_at": _now()})
    await _answered(cid, prompt)
    if obs is None:
        return True                                        # duplicate webhook: already stored and answered
    e = (_rows(await sb(scoped_query("cc_enrollments", cid).eq("id", prompt["enrollment_id"]).limit(1))) or [None])[0]
    out = await core.evaluate_enrollment(clinic, e) if e else {}
    if out.get("safety_card"):
        await ch.send_safety(clinic, phone, lang, out["safety_card"])
    name = (await ch.reach(clinic, e))[2] if e else None
    await manager.whatsapp.send_text(clinic, phone, ch.t("thanks", lang, name=ch.safe_name(name), measure=ch.MEASURE_NAMES.get(
        measure, {}).get(lang, measure)), _source="cc_hook", _window_open=True)
    return True


async def _reask(manager, clinic, phone, prompt, lang) -> bool:
    """An unusual or unreadable number: ask once more; the second miss marks the prompt 'unparsed' (no value stored)."""
    reasks = int((prompt.get("state") or {}).get("reasks") or 0) + 1
    patch = {"state": {**(prompt.get("state") or {}), "reasks": reasks}}
    if reasks >= 2:
        patch["status"] = "unparsed"
    await ch._update_prompt(clinic["id"], prompt["id"], patch)
    await manager.whatsapp.send_text(clinic, phone, ch.t("check_number", lang), _source="cc_hook", _window_open=True)
    return True


async def _ask_whose(manager, clinic, phone, prompts, value) -> bool:
    cid = clinic["id"]
    buttons = []
    for p in prompts[:3]:
        await ch._update_prompt(cid, p["id"], {"state": {**(p.get("state") or {}), "pending_value": value}})
        e = (_rows(await sb(scoped_query("cc_enrollments", cid).eq("id", p["enrollment_id"]).limit(1))) or [None])[0]
        name = (await ch.reach(clinic, e))[2] if e else "?"
        buttons.append({"id": f"cc:who:{p['id']}", "title": ch.safe_name(name)[:20]})
    await manager.whatsapp.send_interactive_buttons(clinic, phone, ch.t("whose", "en"), buttons, _source="cc_hook")
    return True


async def _stop_care_typed(manager, clinic, phone) -> bool:
    cid = clinic["id"]
    pats = _rows(await sb(scoped_query("patients", cid, "id").eq("phone", phone).limit(5)))
    done = False
    for p in pats:
        for c in _rows(await sb(scoped_query("cc_consents", cid, "id").eq("patient_id", p["id"]).is_("withdrawn_at", "null"))):
            await core.withdraw(cid, c["id"], "stop_keyword")
            done = True
    if not done:
        return False                                       # no care consent: STOP CARE means nothing to CC
    await manager.whatsapp.send_text(clinic, phone, ch.t("stopped", "en"), _source="cc_hook", _window_open=True)
    return True


# ── taps ─────────────────────────────────────────────────────────────────────

TAP = re.compile(r"^cc:(adh|read|skip|consent|staff|off|phq|who|lab|visit):([0-9a-f-]{36})(?::([a-z0-9_]+))?(?::([0-3]))?$")


async def _owns(clinic_id: str, phone: str, patient_id: str) -> bool:
    rows = _rows(await sb(scoped_query("patients", clinic_id, "phone").eq("id", patient_id).limit(1)))
    return bool(rows) and rows[0].get("phone") == phone


async def _prompt(clinic_id: str, prompt_id: str) -> Optional[dict]:
    rows = _rows(await sb(scoped_query("cc_prompts", clinic_id).eq("id", prompt_id).limit(1)))
    return rows[0] if rows else None


async def _enrollment(clinic_id: str, enrollment_id: str) -> Optional[dict]:
    rows = _rows(await sb(scoped_query("cc_enrollments", clinic_id).eq("id", enrollment_id).limit(1)))
    return rows[0] if rows else None


async def _lang(clinic: dict, enrollment_id: str) -> str:
    e = await _enrollment(clinic["id"], enrollment_id)
    return (e or {}).get("language") or "en"


async def handle_tap(manager, clinic: dict, phone: str, tap_id: str, message_id: Optional[str]) -> bool:
    """True when the id is a cc: tap (handled, whatever the outcome); False for anything else."""
    if not (tap_id or "").startswith("cc:"):
        return False
    try:
        m = TAP.fullmatch(tap_id)
        if not m:
            logger.warning(f"CC tap rejected (shape) clinic={clinic.get('id')} phone={mask_phone(phone)}")
            return True
        kind, ref, arg, arg2 = m.groups()
        cid = clinic["id"]
        if kind in ("consent", "staff", "off"):
            e = await _enrollment(cid, ref)
            if not e or not await _owns(cid, phone, e["patient_id"]):
                logger.warning(f"CC tap rejected (owner) clinic={cid} phone={mask_phone(phone)}")
                return True
            return await _enrollment_tap(manager, clinic, phone, kind, e, arg)
        if kind in ("lab", "visit"):
            return await _request_tap(manager, clinic, phone, kind, ref, arg)
        p = await _prompt(cid, ref)
        e = await _enrollment(cid, p["enrollment_id"]) if p else None
        if not p or not e or not await _owns(cid, phone, e["patient_id"]):
            logger.warning(f"CC tap rejected (owner) clinic={cid} phone={mask_phone(phone)}")
            return True
        lang = e.get("language") or "en"
        if kind == "adh":
            code = arg if arg in ("taken", "missed") else None
            if not code:
                return True
            obs = await core.record_observation(cid, e["id"], "adherence", code=code, source="whatsapp_button",
                                                prompt_id=p["id"], message_id=message_id)
            if obs is not None and p["status"] in OPEN_PROMPT:
                await ch._update_prompt(cid, p["id"], {"status": "answered", "answered_at": _now()})
                await _answered(cid, p)
                if code == "missed":
                    await core.evaluate_enrollment(clinic, e)
            await manager.whatsapp.send_text(clinic, phone, ch.t("adh_thanks", lang), _source="cc_hook", _window_open=True)
        elif kind == "read":
            if p["status"] in OPEN_PROMPT:
                await ch._update_prompt(cid, p["id"], {"status": "awaiting_value", "kind": "reading"})
            key = "type_bp" if p.get("measure_code") == "bp" else "type_number"
            await manager.whatsapp.send_text(clinic, phone, ch.t(key, lang, measure=ch.MEASURE_NAMES.get(
                p.get("measure_code") or "rbg", {}).get(lang, "sugar")), _source="cc_hook", _window_open=True)
        elif kind == "skip":
            if p["status"] in OPEN_PROMPT:
                await ch._update_prompt(cid, p["id"], {"status": "answered", "answered_at": _now()})
                await sb(supabase.table("cc_schedule_items").update({"status": "skipped"}).eq("clinic_id", cid)
                         .in_("id", p.get("schedule_item_ids") or ["00000000-0000-0000-0000-000000000000"]))
            await manager.whatsapp.send_text(clinic, phone, ch.t("adh_thanks", lang), _source="cc_hook", _window_open=True)
        elif kind == "who":
            value = (p.get("state") or {}).get("pending_value")
            if value:
                unit = core.get_settings(clinic).get("glucose_unit") or "mg/dL"
                return await _store_reading(manager, clinic, phone, p, value, message_id, unit)
        elif kind == "phq":
            return await _phq(manager, clinic, phone, p, e, arg, arg2, message_id)
        return True
    except Exception as ex:                               # noqa: BLE001 - X18
        logger.error(f"CC tap failed clinic={clinic.get('id')} phone={mask_phone(phone)}: {type(ex).__name__}")
        return True


async def _enrollment_tap(manager, clinic, phone, kind, e, arg) -> bool:
    cid = clinic["id"]
    lang = e.get("language") or "en"
    if kind == "consent":
        if arg == "yes" and e["status"] == "invited":
            consent = await core.grant_consent(clinic, e["patient_id"], e.get("family_member_id"), "whatsapp_button",
                                               lang, None, "account holder" if e.get("family_member_id") else None)
            await core.activate(clinic, e, consent)
            await manager.whatsapp.send_text(clinic, phone, ch.t("joined", lang), _source="cc_hook", _window_open=True)
        elif arg == "no" and e["status"] in ("invited", "awaiting_doctor"):
            await sb(supabase.table("cc_enrollments").update({"status": "withdrawn", "ended_at": _now(),
                                                              "end_reason": "declined", "updated_at": _now()})
                     .eq("clinic_id", cid).eq("id", e["id"]).in_("status", ["invited", "awaiting_doctor"]))
            await manager.whatsapp.send_text(clinic, phone, ch.t("declined", lang), _source="cc_hook", _window_open=True)
    elif kind == "staff":
        from app.services import cc_cases
        await cc_cases.open_case(clinic, patient_id=e["patient_id"], family_member_id=e.get("family_member_id"),
                                 enrollment_id=e["id"], reason="patient_asked_for_staff", tier="routine",
                                 branch_id=e.get("branch_id"))
        await manager.whatsapp.send_text(clinic, phone, ch.t("staff", lang), _source="cc_hook", _window_open=True)
    elif kind == "off":
        if e.get("consent_id"):
            await core.withdraw(cid, e["consent_id"], "whatsapp_button")
        await manager.whatsapp.send_text(clinic, phone, ch.t("stopped", lang), _source="cc_hook", _window_open=True)
    return True


async def _request_tap(manager, clinic, phone, kind, ref, arg) -> bool:
    """Lab / visit buttons ask the coordinator to book (a case) — CC never books from a tap by itself."""
    from app.services import cc_cases
    cid = clinic["id"]
    pats = _rows(await sb(scoped_query("patients", cid, "id").eq("phone", phone).limit(1)))
    if not pats:
        return True
    enr = _rows(await sb(scoped_query("cc_enrollments", cid).eq("patient_id", pats[0]["id"]).eq("status", "active").limit(1)))
    if not enr:
        return True
    reason = {"home": "lab_home_sample_requested", "visit": "lab_visit_requested", "done": "lab_reported_done",
              "book": "visit_requested", "cant": "visit_cannot_come"}.get(arg or "", "patient_request")
    await cc_cases.open_case(clinic, patient_id=pats[0]["id"], enrollment_id=enr[0]["id"], reason=reason, tier="routine",
                             branch_id=enr[0].get("branch_id"))
    await manager.whatsapp.send_text(clinic, phone, ch.t("staff", enr[0].get("language") or "en"), _source="cc_hook",
                                     _window_open=True)
    return True


# ── PHQ-9 / GAD-7 (in-window only; item 9 is immediate) ──────────────────────

async def _question_text(clinic: dict, measure: str, n: int, lang: str) -> str:
    """A clinic-approved validated translation when present (cards catalogue key phq9_q<n>), else the English item."""
    items = PHQ9 if measure == "phq9" else GAD7
    approved = await core.card_text(clinic, f"{measure}_q{n + 1}", lang) if lang != "en" else None
    return approved or items[n]


async def send_phq_question(clinic: dict, phone: str, lang: str, prompt: dict, n: int) -> bool:
    from app.services.whatsapp import whatsapp_service
    measure = prompt.get("measure_code") or prompt.get("kind") or "phq9"
    q = await _question_text(clinic, measure, n, lang)
    answers = ANSWERS.get(lang, ANSWERS["en"])
    rows = [{"id": f"cc:phq:{prompt['id']}:q{n + 1}:{v}", "title": answers[v][:24], "description": ""} for v in range(4)]
    body = f"({n + 1}/{9 if measure == 'phq9' else 7}) " + ch.t("phq_q", lang, q=q)
    return bool(await whatsapp_service.send_interactive_list(clinic, phone, body[:1024], "Answer", [{"title": "Answer",
                                                                                                  "rows": rows}]))


async def _phq(manager, clinic, phone, p, e, arg, arg2, message_id) -> bool:
    cid = clinic["id"]
    lang = e.get("language") or "en"
    measure = p.get("measure_code") or "phq9"
    total = 9 if measure == "phq9" else 7
    state = dict(p.get("state") or {})
    answers = list(state.get("answers") or [])
    if arg == "start":
        await ch._update_prompt(cid, p["id"], {"status": "awaiting_value", "state": {"answers": []}})
        await send_phq_question(clinic, phone, lang, {**p, "measure_code": measure}, 0)
        return True
    if not (arg or "").startswith("q") or arg2 is None or p["status"] not in OPEN_PROMPT:
        return True
    n = int(arg[1:]) - 1
    if n != len(answers):                                  # a stale or repeated tap: ignore, re-ask the current one
        await send_phq_question(clinic, phone, lang, {**p, "measure_code": measure}, len(answers))
        return True
    answers.append(int(arg2))
    if len(answers) < total:
        await ch._update_prompt(cid, p["id"], {"state": {**state, "answers": answers}})
        await send_phq_question(clinic, phone, lang, {**p, "measure_code": measure}, len(answers))
        return True
    await ch._update_prompt(cid, p["id"], {"status": "answered", "answered_at": _now(), "state": {**state, "answers": answers}})
    await _answered(cid, p)
    code = f"q9={answers[8]}" if measure == "phq9" else None
    await core.record_observation(cid, e["id"], measure, value=float(sum(answers)), code=code, source="whatsapp_button",
                                  prompt_id=p["id"], message_id=message_id)
    out = await core.evaluate_enrollment(clinic, e)
    if out.get("safety_card"):
        await ch.send_safety(clinic, phone, lang, out["safety_card"])
    await manager.whatsapp.send_text(clinic, phone, ch.t("phq_done", lang), _source="cc_hook", _window_open=True)
    return True


# ── bookkeeping ──────────────────────────────────────────────────────────────

async def _answered(clinic_id: str, prompt: dict) -> None:
    ids = prompt.get("schedule_item_ids") or []
    if ids:
        await sb(supabase.table("cc_schedule_items").update({"status": "answered"}).eq("clinic_id", clinic_id).in_("id", ids))
    await sb(supabase.table("cc_enrollments").update({"unanswered_streak": 0, "updated_at": _now()})
             .eq("clinic_id", clinic_id).eq("id", prompt["enrollment_id"]))


async def record_unanswered(clinic: dict, now: datetime) -> None:
    """Prompts past their 12 h expiry become 'expired'; three in a row → one voice attempt per 7 days (W4/W5), else a
    not_responding case."""
    from app.services import cc_cases
    cid = clinic["id"]
    stale = _rows(await sb(scoped_query("cc_prompts", cid, "id, enrollment_id").in_("status", ["sent", "delivered", "read"])
                           .lt("expires_at", now.isoformat()).limit(500)))
    for p in stale:
        done = _rows(await sb(supabase.table("cc_prompts").update({"status": "expired", "updated_at": _now()})
                              .eq("clinic_id", cid).eq("id", p["id"]).in_("status", ["sent", "delivered", "read"])))
        if not done:
            continue
        e = await _enrollment(cid, p["enrollment_id"])
        if not e or e["status"] != "active":
            continue
        streak = int(e.get("unanswered_streak") or 0) + 1
        await sb(supabase.table("cc_enrollments").update({"unanswered_streak": streak, "updated_at": _now()})
                 .eq("clinic_id", cid).eq("id", e["id"]))
        if streak >= 3:
            last = e.get("last_voice_attempt_at")
            recent = last and datetime.fromisoformat(str(last).replace("Z", "+00:00")) > now - timedelta(days=7)
            job = None
            if e.get("channel_pref") in ("voice", "both", "whatsapp") and core.get_settings(clinic).get("voice_fallback") \
                    and not recent:
                job = await ch.enqueue_voice(clinic, e)
            if not job:
                await cc_cases.open_case(clinic, patient_id=e["patient_id"], family_member_id=e.get("family_member_id"),
                                         enrollment_id=e["id"], reason="not_responding", tier="watch",
                                         branch_id=e.get("branch_id"), summary={"unanswered": streak})


async def self_harm_signal(clinic: dict, phone: str) -> None:
    """CC-L1 with CC on: every enrollment of this phone gets an urgent case (the crisis card was already sent)."""
    from app.services import cc_cases
    cid = clinic["id"]
    for p in _rows(await sb(scoped_query("patients", cid, "id").eq("phone", phone).limit(5))):
        enr = _rows(await sb(scoped_query("cc_enrollments", cid).eq("patient_id", p["id"]).in_("status", ["active", "paused"])))
        for e in enr or [None]:
            await cc_cases.open_case(clinic, patient_id=p["id"], enrollment_id=(e or {}).get("id"),
                                     family_member_id=(e or {}).get("family_member_id"), reason="self_harm_message",
                                     tier="urgent", branch_id=(e or {}).get("branch_id"),
                                     summary={"source": "whatsapp_message"})
