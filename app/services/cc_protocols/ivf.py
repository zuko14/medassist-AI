"""IVF packs (PROTOCOLS.md §6; docs/specialty-os/IVF.md; SECURITY H4). The trigger time is copied verbatim from the
doctor's entry — the engine never computes it; an unacknowledged trigger is urgent (coordinator call)."""

from datetime import timedelta

from ._pack import pack
from .base import Param, Reason, combine, item_answer, latest_lab


def _cycle(p, inp):
    reasons, proposed, card = [], [], None
    trig = inp.facts.extra.get("trigger_on")
    if trig and not inp.facts.extra.get("trigger_acknowledged") and inp.today >= trig - timedelta(days=1):
        reasons.append(Reason("trigger_not_acknowledged", "urgent", {"trigger": inp.facts.extra.get("trigger_text")}))
        proposed.append("call_patient_now")
    transfer = inp.facts.extra.get("transfer_on")
    if transfer:
        due = transfer + timedelta(days=int(p["beta_day"]))
        hcg = latest_lab(inp, "beta_hcg")
        if inp.today > due and (hcg is None or hcg.at < transfer):
            reasons.append(Reason("beta_hcg_due", "watch", {"due": due.isoformat()}))
            proposed.append("order_lab")
    danger = [o for o in item_answer(inp, "symptoms", 2) if o.code == "danger"]
    if danger:
        reasons.append(Reason("ohss_signs", "urgent", {"date": danger[0].at.isoformat()}))
        proposed.append("call_patient_now")
        card = "ohss_danger"
    return combine(reasons, proposed, card)


def _cryo(p, inp):
    expires = inp.facts.extra.get("storage_expires_on")
    if expires and (expires - inp.today).days <= p["notice_days"]:
        return combine([Reason("storage_renewal_due", "watch", {"expires_on": expires.isoformat()})], ["call_patient_now"], None)
    return combine([], [], None)


CYCLE = pack("ivf_cycle", "IVF cycle", {
    "beta_day": Param(14, 9, 16, "days after transfer", "ISAR / ESHRE practice"),
}, _cycle, icd10=("N97",), measures=("symptoms",), cards=("ohss_danger",), plan="ivf")
CRYO = pack("ivf_cryo_renewal", "Cryo storage renewal", {
    "notice_days": Param(60, 30, 120, "days", "ART (Regulation) Act 2021 storage limits"),
}, _cryo, plan="ivf")
PACKS = (CYCLE, CRYO)
