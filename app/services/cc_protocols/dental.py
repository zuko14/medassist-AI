"""Dental packs (PROTOCOLS.md §6; docs/specialty-os/DENTAL.md). Reuse the dental treatment plans' sittings; never
touch the dental message quotas (care has its own quota kind)."""

from datetime import timedelta

from ._pack import pack
from .base import Param, Reason, combine, days_since


def _recall(p, inp):
    since = days_since(inp.last_visit, inp.today)
    if since is None or since > p["recall_days"]:
        return combine([Reason("recall_due", "routine", {"days_since": since})], ["book_review_visit"], None)
    return combine([], [], None)


def _ortho(p, inp):
    reasons, proposed = [], []
    since = days_since(inp.last_visit, inp.today)
    if since is None or since > p["adjust_days"]:
        reasons.append(Reason("adjustment_due", "watch", {"days_since": since}))
        proposed.append("book_review_visit")
    debond = inp.facts.extra.get("debonded_on")
    if debond and inp.today > debond + timedelta(days=int(p["retainer_check_days"])) and (
            inp.last_visit is None or inp.last_visit < debond + timedelta(days=int(p["retainer_check_days"]))):
        reasons.append(Reason("retainer_check_due", "routine", {"debonded_on": debond.isoformat()}))
        proposed.append("book_review_visit")
    return combine(reasons, proposed, None)


def _implant(p, inp):
    reasons, proposed = [], []
    placed = inp.facts.extra.get("placed_on")
    for key, day in (("suture", p["suture_days"]), ("three_month", 90), ("annual", 365)):
        due = placed + timedelta(days=int(day)) if placed else None
        if due and inp.today > due + timedelta(days=3) and (inp.last_visit is None or inp.last_visit < due):
            reasons.append(Reason(f"{key}_review_due", "watch" if key == "suture" else "routine", {"due": due.isoformat()}))
            proposed.append("book_review_visit")
    return combine(reasons, proposed, None)


RECALL = pack("dental_recall", "Hygiene recall", {
    "recall_days": Param(180, 90, 365, "days", "IDA recall guidance (3–12 months by risk)"),
}, _recall, plan="dental")
ORTHO = pack("dental_ortho", "Orthodontic adjustments", {
    "adjust_days": Param(35, 28, 56, "days", "the orthodontist's interval"),
    "retainer_check_days": Param(90, 30, 180, "days", "the orthodontist's interval"),
}, _ortho, plan="dental")
IMPLANT = pack("dental_implant_review", "Implant reviews", {
    "suture_days": Param(8, 7, 10, "days", "standard implant protocol"),
}, _implant, cards=("post_extraction_care",), plan="dental")
PACKS = (RECALL, ORTHO, IMPLANT)
