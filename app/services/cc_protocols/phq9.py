"""phq9 — Depression (PROTOCOLS.md §3.4; MANAS / TEAMcare care-manager model). Sources: Kroenke et al. 2001; MANAS
PMC3227809; TEAMcare PMC3726010; Tele-MANAS (MoHFW). Each completed questionnaire is one observation: measure 'phq9',
value = total (0–27), code = 'q9=<0-3>'. Item 9 ≥ 1 is urgent and immediate, whatever the total."""

from .base import Param, Reason, combine, scored

CODE, VERSION, TITLE = "phq9", "1.0.0", "Depression (PHQ-9)"
ICD10 = ("F32", "F33")
MEASURES = ("phq9",)
DUE_LABS: dict = {}
CARDS = ("crisis_support",)
QUESTIONS = 9
PARAMS = {
    "severe_cut": Param(20, 15, 20, "score", "Kroenke 2001 (20–27 severe)"),
    "worse_delta": Param(5, 3, 7, "points", "Kroenke 2001 (5-point change is clinically meaningful)"),
    "weeks_check": Param(8, 6, 12, "weeks", "TEAMcare (treat-to-target review)"),
    "interval_days": Param(14, 14, 28, "days", "MANAS (two-weekly)"),
}


def item9(o) -> int:
    code = o.code or ""
    return int(code.split("=", 1)[1]) if code.startswith("q9=") and code[3:].isdigit() else 0


def evaluate(p, inp, measure="phq9"):
    reasons, proposed, card = [], [], None
    done = scored(inp, measure, None)
    latest = done[0] if done else None
    if latest and measure == "phq9" and item9(latest) >= 1:
        reasons.append(Reason("self_harm", "urgent", {"item9": item9(latest), "date": latest.at.isoformat()}))
        proposed.append("call_patient_now")
        card = "crisis_support"
    if latest and latest.value >= p["severe_cut"]:
        reasons.append(Reason("severe", "urgent", {"total": latest.value}))
        proposed += ["call_patient_now", "book_review_visit"]
    base = inp.facts.baseline.get(measure)
    if latest and base is not None and latest.value - base >= p["worse_delta"]:
        reasons.append(Reason("worsening", "watch", {"total": latest.value, "baseline": base}))
        proposed.append("book_review_visit")
    weeks = (inp.today - inp.facts.enrolled_on).days / 7
    if latest and base and weeks >= p["weeks_check"] and latest.value >= 10 and latest.value > base * 0.5:
        reasons.append(Reason("not_improving", "watch", {"total": latest.value, "baseline": base, "weeks": int(weeks)}))
        proposed.append("book_review_visit")
    if latest is None or (inp.today - latest.at).days > p["interval_days"]:
        reasons.append(Reason("due", "routine", {"last": latest.at.isoformat() if latest else None}))
    return combine(reasons, proposed, card)
