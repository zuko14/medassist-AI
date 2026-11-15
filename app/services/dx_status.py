"""Patient-facing Diagnostic OS visit status for the voice receptionist and WhatsApp chat (P9, DX-56/57).

KRIYA-KETU §2–§3, ADR-DX-10: enums and timestamps only. This module never reads a result value,
flag, reference range or interpretation — it reads tube, visit and delivery states, the test's TAT
target and the visit's dues. The texts are fixed templates (no model writes them).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import sb, scoped_query, supabase
from app.services import dx, dx_billing, dx_reports
from app.services.dx_b2b import _in, _rows
from app.utils.helpers import IST

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 30
LINK_THROTTLE = timedelta(minutes=10)
PAYER_REASONS = ("payer_routed", "account_rule")

TEXTS = {
    "registered": {
        "en": "Your lab visit {visit} is registered; the sample has not been collected yet.",
        "hi": "आपकी लैब विज़िट {visit} रजिस्टर हो गई है; सैंपल अभी लिया नहीं गया है।",
        "te": "మీ ల్యాబ్ విజిట్ {visit} నమోదు అయింది; శాంపిల్ ఇంకా తీసుకోలేదు.",
    },
    "processing": {
        "en": "Your sample was collected at {collected}; results are expected by {expected}.",
        "hi": "आपका सैंपल {collected} पर लिया गया; रिपोर्ट {expected} तक आने की उम्मीद है।",
        "te": "మీ శాంపిల్ {collected}కి తీసుకున్నాం; రిపోర్ట్ {expected} లోపు వస్తుంది.",
    },
    "partial": {
        "en": "Some of your results are ready and have been sent; the rest are expected by {expected}.",
        "hi": "आपकी कुछ रिपोर्ट तैयार होकर भेज दी गई हैं; बाकी {expected} तक आएंगी।",
        "te": "మీ కొన్ని రిపోర్టులు రెడీ అయి పంపించాం; మిగతావి {expected} లోపు వస్తాయి.",
    },
    "held_dues": {
        "en": "Your test report is ready, but a payment of {amount} is pending. "
              "The report is sent as soon as it is paid.",
        "hi": "आपकी टेस्ट रिपोर्ट तैयार है, लेकिन {amount} का भुगतान बाकी है। भुगतान होते ही रिपोर्ट भेज दी जाएगी।",
        "te": "మీ టెస్ట్ రిపోర్ట్ రెడీగా ఉంది, కానీ {amount} చెల్లింపు పెండింగ్‌లో ఉంది. చెల్లించగానే రిపోర్ట్ పంపిస్తాం.",
    },
    "recollect": {
        "en": "Your sample could not be tested ({reason}) and must be collected again. There is no extra charge.",
        "hi": "आपका सैंपल टेस्ट नहीं हो सका ({reason}); दोबारा सैंपल देना होगा। कोई अतिरिक्त शुल्क नहीं है।",
        "te": "మీ శాంపిల్ టెస్ట్ చేయలేకపోయాం ({reason}); మళ్లీ శాంపిల్ ఇవ్వాలి. అదనపు ఛార్జీ లేదు.",
    },
    "sent_to_account": {
        "en": "Your test report has been sent directly to {account}.",
        "hi": "आपकी टेस्ट रिपोर्ट सीधे {account} को भेज दी गई है।",
        "te": "మీ టెస్ట్ రిపోర్ట్ నేరుగా {account}కి పంపించాం.",
    },
    "sent": {
        "en": "Your test report was sent to your WhatsApp on {sent}.",
        "hi": "आपकी टेस्ट रिपोर्ट {sent} को आपके WhatsApp पर भेज दी गई थी।",
        "te": "మీ టెస్ట్ రిపోర్ట్ {sent}న మీ WhatsAppకి పంపించాం.",
    },
    "ready_desk": {
        "en": "Your test report is ready. Please contact the reception to collect it.",
        "hi": "आपकी टेस्ट रिपोर्ट तैयार है। कृपया रिसेप्शन से संपर्क करें।",
        "te": "మీ టెస్ట్ రిపోర్ట్ రెడీగా ఉంది. దయచేసి రిసెప్షన్‌ను సంప్రదించండి.",
    },
}


def _ts(v) -> Optional[datetime]:
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(IST) if v else None


def _clock(t: Optional[datetime], now: datetime) -> str:
    if t is None:
        return "-"
    hm = t.strftime("%I:%M %p").lstrip("0")
    return hm if t.date() == now.astimezone(IST).date() else f"{t.day} {t.strftime('%b')}, {hm}"


def status_text(st: dict, lang: str = "en") -> str:
    """The fixed sentence for a status in en / hi / te (anything else: English)."""
    lang = (lang or "en")[:2]
    t = TEXTS[st["state"]]
    return t.get(lang, t["en"]).format(**st.get("params", {}))


async def get_patient_dx_status(clinic_id: str, phone: str, now: Optional[datetime] = None) -> Optional[dict]:
    """The latest DX visit (last 30 days) of the patient (or a family member) registered on `phone`, as
    {state, accession_id, accession_number, patient_name, params, balance_paise, text}. None = no visit.
    State precedence: recollect > held_dues > sent_to_account > sent / ready_desk > partial > processing >
    registered."""
    now = now or datetime.now(timezone.utc)
    pats = await _rows(scoped_query("patients", clinic_id, "id").eq("phone", phone))
    if not pats or not phone:
        return None
    since = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
    visits = await _rows(scoped_query("dx_accessions", clinic_id, "id, accession_number, branch_id, patient_snapshot, "
                                      "b2b_account_id, priority, status, created_at")
                         .in_("patient_id", [p["id"] for p in pats]).neq("status", "cancelled")
                         .gte("created_at", since).order("created_at", desc=True).limit(1))
    if not visits:
        return None
    v = visits[0]
    vid = v["id"]
    samples = await _rows(scoped_query("dx_samples", clinic_id, "status, collected_at, recollect_of, "
                                       "rejection_reason_code").eq("accession_id", vid))
    deliveries = await _rows(scoped_query("dx_release_deliveries", clinic_id, "target, status, reason, sent_at")
                             .eq("accession_id", vid).order("created_at", desc=True))
    patient = next((d for d in deliveries if d["target"] == "patient"), None)
    out = {"accession_id": vid, "accession_number": v["accession_number"],
           "patient_name": (v.get("patient_snapshot") or {}).get("name"), "visit_status": v["status"],
           "balance_paise": 0, "params": {"visit": v["accession_number"]}}
    redraw = [s for s in samples if s.get("recollect_of") and s["status"] == "pending_collection"]
    if redraw:
        rejected = next((s for s in samples if s["status"] == "rejected"), {})
        labels = {r["code"]: r["label"] for r in await dx.rejection_reasons(clinic_id, active_only=False)}
        reason = labels.get(rejected.get("rejection_reason_code"), "sample problem")
        return _done(out, "recollect", reason=reason.lower())
    if any(d["status"] == "held_dues" for d in deliveries):
        out["balance_paise"] = await dx_reports.visit_dues_paise(clinic_id, vid)
        return _done(out, "held_dues", amount=f"₹{out['balance_paise'] / 100:,.0f}")
    if patient and patient["status"] == "blocked" and patient.get("reason") in PAYER_REASONS and v.get("b2b_account_id"):
        acc = await _rows(scoped_query("dx_b2b_accounts", clinic_id, "name").eq("id", v["b2b_account_id"]).limit(1))
        return _done(out, "sent_to_account", account=(acc[0]["name"] if acc else "your hospital / payer"))
    if v["status"] == "reported":
        if patient and patient["status"] == "sent":
            return _done(out, "sent", sent=_clock(_ts(patient["sent_at"]), now))
        return _done(out, "ready_desk")
    expected = _clock(await _expected_at(clinic_id, v), now)
    if v["status"] == "partially_reported":
        return _done(out, "partial", expected=expected)
    collected = [_ts(s["collected_at"]) for s in samples if s.get("collected_at")]
    if collected:
        return _done(out, "processing", collected=_clock(min(collected), now), expected=expected)
    return _done(out, "registered")


def _done(out: dict, state: str, **params) -> dict:
    out["state"] = state
    out["params"].update(params)
    out["text"] = status_text(out)
    return out


async def _expected_at(clinic_id: str, visit: dict) -> Optional[datetime]:
    """Registration time + the longest TAT target of the visit's tests (STAT target for urgent / STAT)."""
    items = [i for i in await _rows(scoped_query("dx_accession_items", clinic_id, "lab_test_id, kind, status")
                                    .eq("accession_id", visit["id"])) if i["status"] == "active" and i["kind"] != "fee"]
    tests = await _in("lab_tests", clinic_id, "id, tat_minutes, stat_tat_minutes", "id", [i["lab_test_id"] for i in items])
    urgent = visit.get("priority") in ("stat", "urgent")
    mins = [(t.get("stat_tat_minutes") if urgent else None) or t.get("tat_minutes") for t in tests]
    mins = [m for m in mins if m]
    return _ts(visit["created_at"]) + timedelta(minutes=max(mins)) if mins else None


async def dues_payment_link(clinic_id: str, clinic: dict, accession_id: str,
                            now: Optional[datetime] = None) -> dict:
    """The balance payment link of a visit held for dues: {url, amount_paise, fresh}. A link made in the
    last 10 minutes for the same amount is reused (fresh=False), so a caller asking twice is not sent two."""
    now = now or datetime.now(timezone.utc)
    inv = await dx_billing.open_invoice(clinic_id, accession_id)
    balance = dx_billing._balance(inv)
    made = _ts(inv.get("payment_link_created_at"))
    if inv.get("payment_link_url") and inv.get("payment_link_amount_paise") == balance and made \
            and now - made < LINK_THROTTLE:
        return {"url": inv["payment_link_url"], "amount_paise": balance, "fresh": False}
    link = await dx_billing.payment_link(clinic_id, clinic, accession_id)
    return {"url": link.get("url"),
            "amount_paise": link["amount_paise"], "fresh": True}


async def request_recollection(clinic_id: str, phone: str, accession_number: str, where: str) -> bool:
    """A note for the lab desk: the patient asked for a redraw at home / at the centre. Never raises."""
    try:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("admin_notifications").insert({
            "clinic_id": clinic_id, "admin_id": None,
            "title": f"Recollection: {accession_number} ({'home visit' if where == 'home' else 'centre visit'})",
            "message": f"The patient on {phone} asked to give a fresh sample "
                       f"{'at home — please call to fix a time' if where == 'home' else 'at the centre'}.",
            "is_read": False}))
        return True
    except Exception as e:  # noqa: BLE001 - the patient's reply must still go out
        logger.error(f"DX_RECOLLECTION_REQUEST_FAILED type={type(e).__name__}")
        return False
