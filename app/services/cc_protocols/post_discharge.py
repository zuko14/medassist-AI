"""post_discharge — 30 days after an IPD discharge (PROTOCOLS.md §3.5). Day-2 symptom check (fixed list, measure
'symptoms', code 'danger' or 'none'), day-7 review visit (cc_discharge_followups or the default), discharge-medicine
adherence. Any danger symptom → urgent + post_op_danger."""

from datetime import timedelta

from .base import Param, Reason, combine, item_answer, missed_doses

CODE, VERSION, TITLE = "post_discharge", "1.0.0", "30 days after discharge"
ICD10: tuple = ()
MEASURES = ("symptoms", "adherence")
DUE_LABS: dict = {}
CARDS = ("post_op_danger",)
SYMPTOMS = ("fever", "wound redness or discharge", "breathlessness", "uncontrolled pain", "reduced urine")
PARAMS = {
    "review_day": Param(7, 5, 14, "days after discharge", "Kriya default (clinic policy)"),
    "grace_days": Param(3, 1, 7, "days", "Kriya default"),
    "miss_n": Param(3, 2, 5, "doses / 7 d", "Kriya default"),
}


def evaluate(p, inp):
    reasons, proposed, card = [], [], None
    danger = [o for o in item_answer(inp, "symptoms", 3) if o.code == "danger"]
    if danger:
        reasons.append(Reason("danger_symptom", "urgent", {"date": danger[0].at.isoformat()}))
        proposed.append("call_patient_now")
        card = "post_op_danger" if danger[0].at == inp.today else None
    discharged = inp.facts.extra.get("discharged_on")
    review_on = inp.facts.extra.get("follow_up_on") or (discharged + timedelta(days=int(p["review_day"])) if discharged else None)
    if review_on and inp.today > review_on + timedelta(days=int(p["grace_days"])) and (
            inp.last_visit is None or inp.last_visit < (discharged or review_on)):
        reasons.append(Reason("review_missed", "watch", {"due": review_on.isoformat()}))
        proposed.append("book_review_visit")
    if missed_doses(inp, 7) >= p["miss_n"]:
        reasons.append(Reason("missed_doses", "watch", {"missed_7d": missed_doses(inp, 7)}))
        proposed.append("call_patient_now")
    return combine(reasons, proposed, card)
