"""Eye packs (PROTOCOLS.md §6; docs/specialty-os/EYE.md). Drops are shown verbatim; tapers are display-only."""

from datetime import timedelta

from ._pack import pack
from .base import Param, Reason, combine, days_since, item_answer


def _cataract(p, inp):
    reasons, proposed, card = [], [], None
    op = inp.facts.extra.get("surgery_on")
    for key, day in (("day1", p["day1"]), ("week1", p["week1"]), ("month1", p["month1"])):
        due = op + timedelta(days=int(day)) if op else None
        if due and inp.today > due + timedelta(days=1) and (inp.last_visit is None or inp.last_visit < due):
            reasons.append(Reason(f"visit_{key}_missed", "watch", {"due": due.isoformat()}))
            proposed.append("book_review_visit")
    danger = [o for o in item_answer(inp, "symptoms", 2) if o.code == "danger"]
    if danger:
        reasons.append(Reason("eye_danger", "urgent", {"date": danger[0].at.isoformat()}))
        proposed.append("call_patient_now")
        card = "eye_post_op_danger"
    return combine(reasons, proposed, card)


def _anti_vegf(p, inp):
    reasons, proposed = [], []
    nxt = inp.facts.extra.get("next_injection_on")
    if nxt and inp.today > nxt + timedelta(days=int(p["grace_days"])) and (inp.last_visit is None or inp.last_visit < nxt):
        reasons.append(Reason("injection_missed", "watch", {"due": nxt.isoformat(), "priority": 1}))
        proposed.append("book_review_visit")
    return combine(reasons, proposed, None)


def _glaucoma(p, inp):
    reasons, proposed = [], []
    since = days_since(inp.facts.extra.get("last_iop_on"), inp.today)
    if since is None or since > p["iop_interval"]:
        reasons.append(Reason("iop_review_due", "watch", {"days_since": since}))
        proposed.append("book_review_visit")
    missed = sum(1 for o in inp.observations if o.measure == "adherence" and o.code == "missed"
                 and (inp.today - o.at).days <= 7)
    if missed >= 3:
        reasons.append(Reason("missed_drops", "watch", {"missed_7d": missed}))
        proposed.append("call_patient_now")
    return combine(reasons, proposed, None)


def _dr(p, inp):
    since = days_since(inp.facts.extra.get("last_fundus_on"), inp.today)
    if since is None or since > p["interval"]:
        return combine([Reason("fundus_due", "routine", {"days_since": since})], ["book_review_visit"], None)
    return combine([], [], None)


POST_CATARACT = pack("eye_post_cataract", "After cataract surgery", {
    "day1": Param(1, 1, 2, "days", "AIOS cataract PPP"), "week1": Param(7, 5, 10, "days", "AIOS cataract PPP"),
    "month1": Param(30, 21, 45, "days", "AIOS cataract PPP"),
}, _cataract, icd10=("H25", "H26"), cards=("eye_post_op_danger",), plan="eye")
ANTI_VEGF = pack("eye_anti_vegf", "Anti-VEGF injection series", {
    "grace_days": Param(3, 0, 14, "days", "the retina specialist's schedule"),
}, _anti_vegf, icd10=("H35.3", "E11.3"), cards=("sudden_vision_change",), plan="eye")
GLAUCOMA = pack("eye_glaucoma", "Glaucoma follow-up", {
    "iop_interval": Param(120, 90, 180, "days", "AIOS glaucoma PPP (IOP every 3–6 months)"),
}, _glaucoma, icd10=("H40",), plan="eye")
DR_SCREEN = pack("eye_dr_screen", "Diabetic retinopathy screening", {
    "interval": Param(365, 180, 730, "days", "ADA Standards of Care 2025 §12"),
}, _dr, icd10=("E11",), plan="any")
PACKS = (POST_CATARACT, ANTI_VEGF, GLAUCOMA, DR_SCREEN)
