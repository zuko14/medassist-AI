"""Women & child packs (PROTOCOLS.md §6; docs/specialty-os/WOMEN-CHILD.md). ANC cards are fixed texts and never
mention fetal sex (PCPNDT). Growth z-scores use the WHO LMS formula in code; vaccine schedules come from the
clinic's chosen schedule (app/services/sp_wc.py), never invented by the engine."""

import math
from datetime import timedelta

from ._pack import pack
from .base import Param, Reason, any_above, combine, item_answer, scored


def _anc(p, inp):
    reasons, proposed, card = [], [], None
    edd = inp.facts.extra.get("edd")
    if edd:
        weeks = 40 - (edd - inp.today).days / 7
        for name, lo, hi in (("dating_nt_scan", 11, 14), ("anomaly_scan", 18, 22), ("gdm_ogtt", 24, 28)):
            done = inp.facts.extra.get(f"{name}_done")
            if not done and weeks > hi:
                reasons.append(Reason(f"{name}_overdue", "watch", {"weeks": round(weeks, 1), "window": [lo, hi]}))
                proposed.append("order_lab" if name == "gdm_ogtt" else "book_review_visit")
        last = inp.last_visit
        gap = 28 if weeks < 28 else 14 if weeks < 36 else 7
        if last is None or (inp.today - last).days > gap + int(p["grace_days"]):
            reasons.append(Reason("anc_contact_due", "watch", {"weeks": round(weeks, 1)}))
            proposed.append("book_review_visit")
    high = any_above(inp, "bp", p["bp_sys"], 2) + any_above(inp, "bp", p["bp_dia"], 2, second=True)
    if high:
        reasons.append(Reason("bp_high_pregnancy", "urgent", {"sys": high[0].value, "dia": high[0].value2}))
        proposed.append("call_patient_now")
        card = "anc_danger_signs"
    danger = [o for o in item_answer(inp, "symptoms", 2) if o.code == "danger"]
    if danger:
        reasons.append(Reason("anc_danger", "urgent", {"date": danger[0].at.isoformat()}))
        proposed.append("call_patient_now")
        card = "anc_danger_signs"
    return combine(reasons, proposed, card)


def _pnc(p, inp):
    reasons, proposed = [], []
    delivered = inp.facts.extra.get("delivered_on")
    for d in (2, 3, 7, 42):
        due = delivered + timedelta(days=d) if delivered else None
        if due and inp.today > due + timedelta(days=int(p["grace_days"])) and (inp.last_visit is None or inp.last_visit < due):
            reasons.append(Reason(f"pnc_day{d}_missed", "watch", {"due": due.isoformat()}))
            proposed.append("book_review_visit")
            break
    epds = scored(inp, "phq9", 42)
    if epds and epds[0].value >= 10:
        reasons.append(Reason("postnatal_mood", "watch", {"score": epds[0].value}))
        proposed.append("book_review_visit")
    card = None
    danger = [o for o in item_answer(inp, "symptoms", 2) if o.code == "danger"]
    if danger:                                     # a danger sign is urgent (a case for a call), like anc / post_discharge
        reasons.append(Reason("postnatal_danger", "urgent", {"date": danger[0].at.isoformat()}))
        proposed.append("call_patient_now")
        card = "postnatal_danger"
    return combine(reasons, proposed, card)


def _imm(p, inp):
    """facts.extra.due_vaccines: [(name, due_date)] from the clinic's schedule minus doses given (sp_wc.py)."""
    reasons, proposed = [], []
    for name, due in inp.facts.extra.get("due_vaccines") or ():
        if inp.today > due + timedelta(days=int(p["grace_days"])):
            reasons.append(Reason("vaccine_overdue", "watch", {"vaccine": name, "due": due.isoformat()}))
            proposed.append("book_review_visit")
    return combine(reasons, proposed, None)


def zscore(value: float, l: float, m: float, s: float) -> float:
    """WHO LMS z-score: ((x/M)^L − 1) / (L·S), or ln(x/M)/S when L = 0."""
    return math.log(value / m) / s if l == 0 else ((value / m) ** l - 1) / (l * s)


def _growth(p, inp):
    z = inp.facts.extra.get("weight_for_age_z")
    if z is not None and z < p["z_cut"]:
        return combine([Reason("underweight_for_age", "watch", {"z": round(z, 2)})], ["book_review_visit"], None)
    return combine([], [], None)


ANC = pack("anc", "Antenatal care", {
    "bp_sys": Param(140, 130, 150, "mmHg", "MoHFW ANC guidelines 2010 / WHO ANC 2016"),
    "bp_dia": Param(90, 85, 100, "mmHg", "MoHFW ANC guidelines 2010"),
    "grace_days": Param(7, 3, 14, "days", "Kriya default"),
}, _anc, icd10=("Z34", "Z35", "O"), measures=("bp", "symptoms"), cards=("anc_danger_signs",), plan="womenchild")
POSTNATAL = pack("postnatal", "Postnatal care", {
    "grace_days": Param(2, 1, 7, "days", "MoHFW PNC (48 h, day 3, day 7, 6 weeks)"),
}, _pnc, cards=("postnatal_danger",), plan="womenchild")
IMMUNISATION = pack("child_immunisation", "Child immunisation", {
    "grace_days": Param(7, 0, 28, "days", "IAP 2023 / national UIP"),
}, _imm, cards=("vaccine_fever",), plan="womenchild")
GROWTH = pack("child_growth", "Child growth", {
    "z_cut": Param(-2, -3, -1, "SD", "WHO Child Growth Standards 2006"),
}, _growth, plan="womenchild")
PACKS = (ANC, POSTNATAL, IMMUNISATION, GROWTH)
