"""t2dm — Type 2 diabetes (pilot pack; PROTOCOLS.md §3.1). Sources: ADA Standards of Care 2025 §6; RSSDI–ESI 2022;
ICMR T2DM guidelines 2018. Glucose in mg/dL (fbg / ppbg / rbg), HbA1c from the LIS (validated results only)."""

from .base import Param, Reason, any_above, any_below, combine, days_since_lab, k_of_last_n, latest_lab, missed_doses

CODE, VERSION, TITLE = "t2dm", "1.0.0", "Type 2 diabetes"
ICD10 = ("E11",)
MEASURES = ("fbg", "ppbg", "rbg", "hba1c", "adherence")
DUE_LABS = {"hba1c": "a1c_interval"}
CARDS = ("hypo_now", "high_sugar_symptoms", "lab_due", "eye_check_due")
_ADA = "ADA Standards of Care 2025 §6"
PARAMS = {
    "hypo_mgdl": Param(70, 54, 80, "mg/dL", _ADA + " (level 1 < 70, level 2 < 54)"),
    "very_high_mgdl": Param(400, 300, 450, "mg/dL", "RSSDI–ESI 2022"),
    "high_mgdl": Param(250, 180, 300, "mg/dL", "RSSDI–ESI 2022"),
    "fbg_target_max": Param(130, 110, 160, "mg/dL", _ADA),
    "a1c_target": Param(7.0, 6.5, 8.5, "%", _ADA + " (individualised ~7 %)"),
    "a1c_high": Param(9.0, 8.5, 10.0, "%", "ICMR T2DM guidelines 2018"),
    "a1c_interval": Param(100, 80, 200, "days", _ADA + " (twice a year at target, quarterly otherwise)"),
    "miss_n": Param(3, 2, 5, "doses / 7 d", "Kriya deck p.12 (three missed doses)"),
    "eye_interval": Param(365, 180, 730, "days", _ADA + " §12 retinopathy screening"),
}
GLUCOSE = ("fbg", "ppbg", "rbg")


def _glucose(inp, fn, *a):
    return [o for m in GLUCOSE for o in fn(inp, m, *a)]


def evaluate(p, inp):
    reasons, proposed, card = [], [], None
    low = _glucose(inp, any_below, p["hypo_mgdl"], 7)
    if low:
        severe = any(o.value < 54 for o in low) or len(low) >= 2
        reasons.append(Reason("hypo", "urgent" if severe else "watch", {"values": [o.value for o in low[:3]],
                                                                         "dates": [o.at.isoformat() for o in low[:3]]}))
        proposed.append("call_patient_now")
        card = "hypo_now" if any(o.at == inp.today for o in low) else card
    very = _glucose(inp, any_above, p["very_high_mgdl"], 2)
    if very:
        reasons.append(Reason("very_high", "urgent", {"value": very[0].value, "date": very[0].at.isoformat()}))
        proposed.append("call_patient_now")
        card = card or ("high_sugar_symptoms" if any(o.at == inp.today for o in very) else None)
    high = _glucose(inp, any_above, p["high_mgdl"], 7)
    if len(high) >= 2 and not very:
        reasons.append(Reason("high_x2", "watch", {"values": [o.value for o in high[:3]]}))
        proposed += ["recheck_in_days", "book_review_visit"]
    fasting = k_of_last_n(inp, "fbg", lambda o: o.value > p["fbg_target_max"], 3, 5)
    if fasting:
        reasons.append(Reason("fasting_above_target", "watch", {"values": [o.value for o in fasting],
                                                                "target": p["fbg_target_max"]}))
        proposed.append("book_review_visit")
    a1c = latest_lab(inp, "hba1c")
    if a1c and a1c.value is not None:
        if a1c.value >= p["a1c_high"]:
            reasons.append(Reason("a1c_very_high", "watch", {"value": a1c.value, "date": a1c.at.isoformat(), "priority": 1}))
            proposed.append("book_review_visit")
        elif a1c.value >= p["a1c_target"]:
            reasons.append(Reason("a1c_above_target", "watch", {"value": a1c.value, "date": a1c.at.isoformat()}))
            proposed.append("book_review_visit")
    since = days_since_lab(inp, "hba1c")
    at_target = a1c is not None and a1c.value is not None and a1c.value < p["a1c_target"]
    interval = p["a1c_interval"] * (2 if at_target else 1)
    if since is None or since > interval:
        reasons.append(Reason("a1c_overdue", "watch", {"days_since": since, "interval": interval}))
        proposed += ["order_lab", "book_home_sample"]
        card = card or "lab_due"
    missed = missed_doses(inp, 7)
    if missed >= p["miss_n"]:
        reasons.append(Reason("missed_doses", "watch", {"missed_7d": missed}))
        proposed.append("call_patient_now")
    eye = inp.facts.extra.get("last_eye_review")
    if eye is None or (inp.today - eye).days > p["eye_interval"]:
        reasons.append(Reason("eye_screen_due", "routine", {"last": eye.isoformat() if eye else None}))
        proposed.append("refer")
    return combine(reasons, proposed, card)
