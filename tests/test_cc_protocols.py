"""Continuous Care protocol engine (P2; docs/continuous-care/PROTOCOLS.md, TEST-PLAN §2).

Every one of the 22 packs runs its vectors (tests/cc_vectors/<pack>.json: fires / does not fire / boundary); the
parameter bounds are enforced; the engine is pure and deterministic (200 seeded random inputs: same inputs hash →
same output); packs only ever propose action codes and only select card keys the clinic can approve.
"""

import json
import random
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services import cc, cc_protocols
from app.services.cc_protocols.base import ACTIONS, Facts, Inputs, Lab, Obs, ParamError, inputs_hash, resolve_params

VEC = Path(__file__).parent / "cc_vectors"
TODAY = date(2026, 11, 25)


def _d(v):
    return TODAY + timedelta(days=int(v[2:])) if isinstance(v, str) and v.startswith("d:") else v


def _inputs(c: dict) -> Inputs:
    extra = {k: ([(n, _d(x)) for n, x in v] if k == "due_vaccines" else _d(v)) for k, v in c["extra"].items()}
    obs = tuple(Obs(measure=o[0], at=TODAY + timedelta(days=o[1]), value=o[2] if len(o) > 2 else None,
                    value2=o[3] if len(o) > 3 else None, code=o[4] if len(o) > 4 else None) for o in c["obs"])
    labs = tuple(Lab(measure=x[0], at=TODAY + timedelta(days=x[1]), value=x[2], flag=x[3]) for x in c["labs"])
    return Inputs(today=TODAY, observations=obs, labs=labs, unanswered_prompts=c["unanswered"],
                  facts=Facts(enrolled_on=TODAY + timedelta(days=c["enrolled"]), baseline=c["baseline"], extra=extra),
                  last_visit=TODAY + timedelta(days=c["last_visit"]) if c["last_visit"] is not None else None)


VECTORS = [(json.loads(p.read_text(encoding="utf-8"))) for p in sorted(VEC.glob("*.json"))]
CASES = [(v["pack"], c) for v in VECTORS for c in v["cases"]]


def test_every_pack_has_vectors():
    assert len(cc_protocols.PACKS) == 22
    assert {v["pack"] for v in VECTORS} == set(cc_protocols.PACKS)
    for v in VECTORS:                                        # each pack has a firing and a quiet / boundary case
        assert any(c["expect"].get("has") for c in v["cases"]), v["pack"]
        assert any(c["expect"].get("not") or c["expect"].get("reasons") == [] for c in v["cases"]), v["pack"]


@pytest.mark.parametrize("pack_code,case", CASES, ids=[f"{p}:{c['name']}" for p, c in CASES])
def test_vector(pack_code, case):
    pack = cc_protocols.get(pack_code)
    ev = pack.evaluate(resolve_params(pack, case["params"]), _inputs(case))
    codes = [r.code for r in ev.reasons]
    exp = case["expect"]
    if "tier" in exp:
        assert ev.tier == exp["tier"], (codes, ev.tier)
    if "reasons" in exp:
        assert codes == exp["reasons"]
    for code in exp.get("has", []):
        assert code in codes, codes
    for code in exp.get("not", []):
        assert code not in codes, codes
    if "card" in exp:
        assert ev.safety_card == exp["card"]
    for a in exp.get("props", []):
        assert a in ev.proposed
    for code, detail in (exp.get("detail") or {}).items():
        got = next(r for r in ev.reasons if r.code == code).detail
        assert {k: got[k] for k in detail} == detail


def test_packs_are_well_formed():
    for code, p in cc_protocols.PACKS.items():
        assert p.CODE == code and p.VERSION and p.TITLE
        for name, prm in p.PARAMS.items():
            assert prm.min <= prm.default <= prm.max, (code, name)
            assert prm.unit and prm.source, (code, name)         # every threshold names its source
        assert set(p.CARDS) <= set(cc.CARD_KEYS), code           # a card the clinic can approve
        assert getattr(p, "PLAN", "any") in ("any", "derma", "eye", "dental", "ivf", "womenchild"), code


def test_parameters_outside_bounds_are_refused():
    t2 = cc_protocols.get("t2dm")
    assert resolve_params(t2, {})["hypo_mgdl"] == 70
    assert resolve_params(t2, {"hypo_mgdl": 54})["hypo_mgdl"] == 54.0
    for bad in ({"hypo_mgdl": 53.9}, {"hypo_mgdl": 81}, {"hypo_mgdl": True}, {"hypo_mgdl": "70"}, {"made_up": 1}):
        with pytest.raises(ParamError):
            resolve_params(t2, bad)


def _random_inputs(rng: random.Random, pack) -> Inputs:
    measures = ["fbg", "ppbg", "rbg", "bp", "phq9", "gad7", "phq2", "symptoms", "adherence"]
    obs = []
    for _ in range(rng.randint(0, 25)):
        m = rng.choice(measures)
        at = TODAY - timedelta(days=rng.randint(0, 40))
        if m == "bp":
            obs.append(Obs("bp", at, rng.randint(80, 220), rng.randint(50, 130)))
        elif m in ("symptoms", "adherence"):
            obs.append(Obs(m, at, code=rng.choice(["danger", "none"] if m == "symptoms" else ["taken", "missed"])))
        elif m in ("phq9", "gad7", "phq2"):
            obs.append(Obs(m, at, rng.randint(0, 27 if m == "phq9" else 21 if m == "gad7" else 6), code=f"q9={rng.randint(0, 3)}"))
        else:
            obs.append(Obs(m, at, rng.randint(30, 500)))
    labs = tuple(Lab(m, TODAY - timedelta(days=rng.randint(0, 400)), rng.uniform(0.1, 12),
                     rng.choice(["normal", "high", "low", None])) for m in rng.sample(
        ["hba1c", "tsh", "alt", "wbc", "creatinine", "lipid_total", "beta_hcg"], rng.randint(0, 4)))
    extra = {k: TODAY + timedelta(days=rng.randint(-200, 60)) for k in (
        "last_eye_review", "levothyroxine_changed_on", "discharged_on", "started_on", "surgery_on", "next_injection_on",
        "last_iop_on", "last_fundus_on", "debonded_on", "placed_on", "trigger_on", "transfer_on", "storage_expires_on",
        "delivered_on", "last_session_on") if rng.random() < 0.5}
    if rng.random() < 0.3:
        extra["edd"] = TODAY + timedelta(days=rng.randint(0, 270))
    return Inputs(today=TODAY, observations=tuple(obs), labs=labs, unanswered_prompts=rng.randint(0, 5),
                  facts=Facts(enrolled_on=TODAY - timedelta(days=rng.randint(0, 200)),
                              baseline={"phq9": rng.randint(0, 27), "gad7": rng.randint(0, 21)}, extra=extra),
                  last_visit=TODAY - timedelta(days=rng.randint(0, 200)) if rng.random() < 0.8 else None)


@pytest.mark.parametrize("pack_code", sorted(cc_protocols.PACKS))
def test_deterministic_and_action_codes_only(pack_code):
    """200 seeded cases per pack: the same inputs hash gives the same output; order of observations is irrelevant;
    proposals are action codes only (ADR-CC-04) and the tier is the maximum over the reasons."""
    pack = cc_protocols.get(pack_code)
    params = resolve_params(pack, {})
    rng = random.Random(f"cc-{pack_code}")
    rank = {"stable": 0, "routine": 0, "watch": 1, "urgent": 2}
    for _ in range(200):
        inp = _random_inputs(rng, pack)
        a, b = pack.evaluate(params, inp), pack.evaluate(params, inp)
        assert a == b and inputs_hash(pack, params, inp) == inputs_hash(pack, params, inp)
        shuffled = list(inp.observations)
        rng.shuffle(shuffled)
        c = pack.evaluate(params, Inputs(**{**inp.__dict__, "observations": tuple(shuffled)}))
        assert c.tier == a.tier and {r.code for r in c.reasons} == {r.code for r in a.reasons}
        assert set(a.proposed) <= set(ACTIONS)
        assert a.tier == max((r.tier for r in a.reasons), key=lambda t: rank[t], default="stable").replace("routine", "stable")
        assert a.safety_card is None or a.safety_card in cc.CARD_KEYS
        json.dumps(a.as_dict())                                  # storable as cc_evaluations.reasons


def test_inputs_hash_changes_with_params_and_inputs():
    t2 = cc_protocols.get("t2dm")
    inp = Inputs(today=TODAY, observations=(Obs("fbg", TODAY, 120),))
    h = inputs_hash(t2, resolve_params(t2, {}), inp)
    assert h != inputs_hash(t2, resolve_params(t2, {"hypo_mgdl": 60}), inp)
    assert h != inputs_hash(t2, resolve_params(t2, {}), Inputs(today=TODAY, observations=(Obs("fbg", TODAY, 121),)))
    assert asdict(inp)["today"] == TODAY


def test_who_lms_zscore():
    from app.services.cc_protocols.womenchild import zscore
    assert zscore(10, 0, 10, 0.1) == 0
    assert round(zscore(9, 0, 10, 0.1), 4) == round(__import__("math").log(0.9) / 0.1, 4)
    assert round(zscore(12, 1, 10, 0.1), 6) == 2.0
