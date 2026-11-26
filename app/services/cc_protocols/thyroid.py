"""thyroid — Hypothyroidism on levothyroxine (PROTOCOLS.md §3.3). Source: ATA Guidelines for Hypothyroidism 2014.
`facts.extra.levothyroxine_changed_on` is set by the plan builder when a new signed prescription's levothyroxine line
text differs from the previous one (an exact text comparison of signed lines — never a parsed dose)."""

from .base import Param, Reason, combine, days_since_lab, latest_lab

CODE, VERSION, TITLE = "thyroid", "1.0.0", "Hypothyroidism"
ICD10 = ("E03", "E89.0")
MEASURES = ("tsh", "adherence")
DUE_LABS = {"tsh": "stable_interval"}
CARDS = ("empty_stomach_timing",)
_ATA = "ATA Hypothyroidism Guidelines 2014"
PARAMS = {
    "post_change_days": Param(49, 42, 70, "days", _ATA + " (TSH 6–8 weeks after a dose change)"),
    "stable_interval": Param(365, 180, 400, "days", _ATA + " (annual when stable)"),
}


def evaluate(p, inp):
    reasons, proposed = [], []
    lab = latest_lab(inp, "tsh")
    changed = inp.facts.extra.get("levothyroxine_changed_on")
    if changed and (lab is None or lab.at < changed) and (inp.today - changed).days > p["post_change_days"]:
        reasons.append(Reason("tsh_after_change", "watch", {"changed_on": changed.isoformat()}))
        proposed.append("order_lab")
    since = days_since_lab(inp, "tsh")
    if since is None or since > p["stable_interval"]:
        reasons.append(Reason("tsh_overdue", "routine", {"days_since": since}))
        proposed.append("order_lab")
    if lab and lab.flag in ("high", "low", "critical_high", "critical_low"):
        reasons.append(Reason("tsh_out_of_range", "watch", {"value": lab.value, "flag": lab.flag, "date": lab.at.isoformat()}))
        proposed.append("book_review_visit")
    return combine(reasons, proposed, None)
