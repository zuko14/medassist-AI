"""Dermatology packs (PROTOCOLS.md §6; docs/specialty-os/DERMA.md). Doses are shown verbatim and never prompted daily:
methotrexate's weekly frequency is a custom frequency, so the plan builder makes it display-only (ADR-CC-05, H3)."""

from datetime import timedelta

from ._pack import pack
from .base import Param, Reason, combine, days_since, days_since_lab, latest_lab, scored


def _iso(p, inp):
    reasons, proposed, card = [], [], None
    if days_since(inp.last_visit, inp.today) is None or days_since(inp.last_visit, inp.today) > p["review_days"]:
        reasons.append(Reason("monthly_review_due", "watch", {"last_visit": inp.last_visit.isoformat() if inp.last_visit else None}))
        proposed.append("book_review_visit")
    for lab in ("alt", "lipid_total"):
        since = days_since_lab(inp, lab)
        if since is None or since > p["lab_interval"]:
            reasons.append(Reason(f"{lab}_due", "watch", {"days_since": since}))
            proposed.append("order_lab")
    if inp.facts.extra.get("can_become_pregnant"):
        since = days_since_lab(inp, "beta_hcg")
        if since is None or since > 30:
            reasons.append(Reason("pregnancy_test_due", "urgent" if since is None else "watch", {"days_since": since}))
            proposed.append("order_lab")
    phq2 = scored(inp, "phq2", 31)
    if phq2 and phq2[0].value >= 3:
        reasons.append(Reason("mood_change", "urgent", {"phq2": phq2[0].value}))
        proposed += ["call_patient_now", "refer"]
        card = "mood_change"
    return combine(reasons, proposed, card)


def _mtx(p, inp):
    reasons, proposed, card = [], [], None
    started = inp.facts.extra.get("started_on") or inp.facts.enrolled_on
    interval = p["early_interval"] if (inp.today - started).days < 3 * p["early_interval"] else p["late_interval"]
    for lab in ("wbc", "alt", "creatinine"):
        since = days_since_lab(inp, lab)
        if since is None or since > interval:
            reasons.append(Reason(f"{lab}_due", "watch", {"days_since": since, "interval": interval}))
            proposed.append("order_lab")
    alt, wbc = latest_lab(inp, "alt"), latest_lab(inp, "wbc")
    if (alt and alt.flag in ("high", "critical_high")) or (wbc and wbc.flag in ("low", "critical_low")):
        reasons.append(Reason("lab_flag", "urgent", {"alt": alt.flag if alt else None, "wbc": wbc.flag if wbc else None}))
        proposed.append("call_patient_now")
        card = "mtx_warning_signs"
    return combine(reasons, proposed, card)


def _series(p, inp):
    done = int(inp.facts.extra.get("sessions_done") or 0)
    total = int(inp.facts.extra.get("sessions_planned") or 0)
    last = inp.facts.extra.get("last_session_on")
    reasons, proposed = [], []
    if total and done < total and last and inp.today > last + timedelta(days=int(p["every_days"]) + int(p["grace_days"])):
        reasons.append(Reason("session_overdue", "watch", {"session": done + 1, "of": total, "last": last.isoformat()}))
        proposed.append("book_review_visit")
    return combine(reasons, proposed, None)


ISOTRETINOIN = pack("derma_isotretinoin", "Isotretinoin course", {
    "review_days": Param(30, 28, 45, "days", "Indian Association of Dermatologists consensus (monthly review)"),
    "lab_interval": Param(30, 30, 90, "days", "IADVL isotretinoin consensus 2020"),
}, _iso, icd10=("L70",), measures=("phq2",), cards=("mood_change",), plan="derma")
METHOTREXATE = pack("derma_methotrexate", "Methotrexate monitoring", {
    "early_interval": Param(28, 14, 42, "days", "BAD methotrexate guideline 2016 (fortnightly–monthly at start)"),
    "late_interval": Param(84, 56, 120, "days", "BAD methotrexate guideline 2016 (every 3 months when stable)"),
}, _mtx, icd10=("L40",), cards=("mtx_warning_signs",), plan="derma")
PROCEDURE_SERIES = pack("derma_procedure_series", "Procedure session series", {
    "every_days": Param(28, 7, 90, "days", "the treating doctor's interval (catalogue)"),
    "grace_days": Param(7, 0, 21, "days", "Kriya default"),
}, _series, plan="derma")
PACKS = (ISOTRETINOIN, METHOTREXATE, PROCEDURE_SERIES)
