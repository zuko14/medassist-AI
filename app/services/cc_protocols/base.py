"""Continuous Care protocol engine — pure, deterministic, zero-LLM (docs/continuous-care/PROTOCOLS.md §1–§2).

No I/O, no clock, no AI: `today` is passed in. A pack is a module with CODE, VERSION, TITLE, PARAMS (each with
default / min / max / unit / source), ICD10 prefixes (radar candidates), DUE_LABS and `evaluate(params, inputs)`
built only from the primitives below. The tier is the maximum over the rules that fired; every fired rule is a
reason with its values. Packs never change a medicine or dose (ADR-CC-04): `proposed` holds action codes only.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable, Literal, Mapping, Optional

Tier = Literal["stable", "watch", "urgent"]
TIER_RANK = {"stable": 0, "routine": 0, "watch": 1, "urgent": 2}
ACTIONS = ("book_review_visit", "order_lab", "book_home_sample", "recheck_in_days", "continue_plan",
           "call_patient_now", "refer")


@dataclass(frozen=True)
class Param:
    default: float
    min: float
    max: float
    unit: str
    source: str


@dataclass(frozen=True)
class Obs:
    measure: str
    at: date
    value: Optional[float] = None
    value2: Optional[float] = None
    code: Optional[str] = None


@dataclass(frozen=True)
class Lab:
    measure: str
    at: date
    value: Optional[float] = None
    flag: Optional[str] = None          # the lab's own flag: normal / low / high / critical_low / critical_high


@dataclass(frozen=True)
class Facts:
    enrolled_on: date
    baseline: Mapping = field(default_factory=dict)
    extra: Mapping = field(default_factory=dict)       # pack facts entered by the doctor (EDD, transfer date, DOB…)


@dataclass(frozen=True)
class Inputs:
    today: date
    observations: tuple = ()        # plausible only, any order
    labs: tuple = ()
    facts: Facts = Facts(enrolled_on=date(2000, 1, 1))
    unanswered_prompts: int = 0
    last_visit: Optional[date] = None   # last VERIFIED visit (check-in / signed encounter)


@dataclass(frozen=True)
class Reason:
    code: str
    tier: str
    detail: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class Evaluation:
    tier: Tier
    reasons: tuple
    proposed: tuple
    safety_card: Optional[str]
    next_check: Optional[date] = None

    def as_dict(self) -> dict:
        return {"tier": self.tier, "reasons": [asdict(r) for r in self.reasons], "proposed": list(self.proposed),
                "safety_card": self.safety_card, "next_check": self.next_check.isoformat() if self.next_check else None}


class ParamError(ValueError):
    pass


def resolve_params(pack, given: Mapping) -> dict:
    """The pack's defaults overlaid with `given`; anything outside [min, max] or unknown is refused."""
    out = {k: p.default for k, p in pack.PARAMS.items()}
    for k, v in (given or {}).items():
        if k not in pack.PARAMS:
            raise ParamError(f"{k} is not a parameter of {pack.CODE}")
        p = pack.PARAMS[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not p.min <= v <= p.max:
            raise ParamError(f"{k} must be between {p.min:g} and {p.max:g} {p.unit}")
        out[k] = float(v)
    return out


def inputs_hash(pack, params: Mapping, inp: Inputs) -> str:
    blob = json.dumps({"pack": pack.CODE, "version": pack.VERSION, "params": dict(sorted(params.items())),
                       "inputs": asdict(inp)}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── primitives (the only vocabulary a pack may use) ───────────────────────────

def _order(o: Obs) -> tuple:
    # newest first; same-day ties broken by value then code so the result never depends on input order
    return (o.at, o.value if o.value is not None else float("-inf"), o.code or "")


def _window(obs: Iterable[Obs], measure: str, today: date, days: Optional[int], valued: bool = True) -> list:
    lo = today - timedelta(days=days) if days is not None else date.min
    return sorted((o for o in obs if o.measure == measure and lo <= o.at <= today and (o.value is not None or not valued)),
                  key=_order, reverse=True)


def any_below(inp: Inputs, measure: str, x: float, days: int) -> list:
    return [o for o in _window(inp.observations, measure, inp.today, days) if o.value < x]


def any_above(inp: Inputs, measure: str, x: float, days: int, second: bool = False) -> list:
    return [o for o in _window(inp.observations, measure, inp.today, days)
            if (o.value2 if second else o.value) is not None and (o.value2 if second else o.value) >= x]


def k_of_last_n(inp: Inputs, measure: str, pred: Callable[[Obs], bool], k: int, n: int) -> list:
    last = _window(inp.observations, measure, inp.today, None)[:n]
    hits = [o for o in last if pred(o)]
    return hits if len(last) >= k and len(hits) >= k else []


def mean_over(inp: Inputs, measure: str, days: int, m: int, second: bool = False) -> Optional[float]:
    vals = [(o.value2 if second else o.value) for o in _window(inp.observations, measure, inp.today, days)]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if len(vals) >= m else None


def latest_lab(inp: Inputs, measure: str) -> Optional[Lab]:
    labs = sorted((x for x in inp.labs if x.measure == measure and x.at <= inp.today), key=lambda x: x.at, reverse=True)
    return labs[0] if labs else None


def days_since_lab(inp: Inputs, measure: str) -> Optional[int]:
    lab = latest_lab(inp, measure)
    return (inp.today - lab.at).days if lab else None


def missed_doses(inp: Inputs, days: int) -> int:
    lo = inp.today - timedelta(days=days)
    return sum(1 for o in inp.observations if o.measure == "adherence" and o.code == "missed" and lo <= o.at <= inp.today)


def item_answer(inp: Inputs, measure: str, days: Optional[int]) -> list:
    """Every answer in the window, newest first — including coded answers without a number (symptoms 'danger')."""
    return _window(inp.observations, measure, inp.today, days, valued=False)


def scored(inp: Inputs, measure: str, days: Optional[int]) -> list:
    """Answers that carry a score (questionnaire totals), newest first."""
    return _window(inp.observations, measure, inp.today, days)


def days_since(d: Optional[date], today: date) -> Optional[int]:
    return (today - d).days if d else None


# ── combination ─────────────────────────────────────────────────────────────

def combine(reasons: list, proposed: list, card: Optional[str], next_check: Optional[date] = None) -> Evaluation:
    tier = "stable"
    for r in reasons:
        t = "stable" if r.tier == "routine" else r.tier
        if TIER_RANK[t] > TIER_RANK[tier]:
            tier = t
    seen, props = set(), []
    for a in proposed:
        assert a in ACTIONS, a                           # ADR-CC-04: action codes only, never a drug or dose
        if a not in seen:
            seen.add(a)
            props.append(a)
    return Evaluation(tier=tier, reasons=tuple(reasons), proposed=tuple(props), safety_card=card, next_check=next_check)


def first_card(*cards: Optional[str]) -> Optional[str]:
    return next((c for c in cards if c), None)
