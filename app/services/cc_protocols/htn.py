"""htn — Hypertension, IHCI-style (PROTOCOLS.md §3.2). Sources: India Hypertension Control Initiative (WHO SEARO 2021);
IHCI outcomes PMC10328822; step-up PMC10959138; ISH 2020. Home BP sys/dia in mmHg. The clinic's own step protocol is
text the doctor sees ("protocol step due") — the engine never names a drug or dose."""

from .base import Param, Reason, any_above, any_below, combine, days_since, mean_over, missed_doses

CODE, VERSION, TITLE = "htn", "1.0.0", "Hypertension"
ICD10 = ("I10", "I11", "I12", "I13", "I15")
MEASURES = ("bp", "adherence")
DUE_LABS: dict = {}
CARDS = ("bp_very_high_symptoms", "low_bp_dizzy", "visit_due")
_IHCI = "IHCI (WHO SEARO 2021)"
PARAMS = {
    "crisis_sys": Param(180, 160, 180, "mmHg", "ISH 2020"),
    "crisis_dia": Param(110, 100, 110, "mmHg", "ISH 2020"),
    "target_sys": Param(140, 130, 140, "mmHg", _IHCI),
    "target_dia": Param(90, 80, 90, "mmHg", _IHCI),
    "low_sys": Param(90, 85, 100, "mmHg", "ISH 2020"),
    "review_days": Param(35, 28, 90, "days", _IHCI + " (monthly review)"),
    "miss_n": Param(3, 2, 5, "doses / 7 d", "Kriya deck p.12"),
}


def evaluate(p, inp):
    reasons, proposed, card = [], [], None
    crisis = ([o for o in any_above(inp, "bp", p["crisis_sys"], 2)]
              + [o for o in any_above(inp, "bp", p["crisis_dia"], 2, second=True)])
    if crisis:
        o = crisis[0]
        reasons.append(Reason("crisis_range", "urgent", {"sys": o.value, "dia": o.value2, "date": o.at.isoformat()}))
        proposed.append("call_patient_now")
        card = "bp_very_high_symptoms" if any(x.at == inp.today for x in crisis) else None
    ms, md = mean_over(inp, "bp", 7, 4), mean_over(inp, "bp", 7, 4, second=True)
    if (ms is not None and ms >= p["target_sys"]) or (md is not None and md >= p["target_dia"]):
        reasons.append(Reason("uncontrolled", "watch", {"mean_sys": ms, "mean_dia": md, "protocol_step_due": True}))
        proposed.append("book_review_visit")
    low = any_below(inp, "bp", p["low_sys"], 3)
    if low:
        reasons.append(Reason("low_bp", "watch", {"sys": low[0].value, "date": low[0].at.isoformat()}))
        proposed.append("call_patient_now")
        card = card or ("low_bp_dizzy" if any(x.at == inp.today for x in low) else None)
    since = days_since(inp.last_visit, inp.today)
    if since is None or since > p["review_days"]:
        reasons.append(Reason("review_overdue", "watch", {"days_since_visit": since}))
        proposed.append("book_review_visit")
        card = card or "visit_due"
    missed = missed_doses(inp, 7)
    if missed >= p["miss_n"]:
        reasons.append(Reason("missed_doses", "watch", {"missed_7d": missed}))
        proposed.append("call_patient_now")
    return combine(reasons, proposed, card)
