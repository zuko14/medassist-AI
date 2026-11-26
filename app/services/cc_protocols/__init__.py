"""Continuous Care protocol packs — versioned code (ADR-CC-06). `PACKS` maps code → pack; pure, zero-LLM."""

from . import dental, derma, eye, gad7, htn, ivf, phq9, post_discharge, t2dm, thyroid, womenchild

CORE = (t2dm, htn, thyroid, phq9, gad7, post_discharge)
PACKS = {p.CODE: p for p in (*CORE, *derma.PACKS, *eye.PACKS, *dental.PACKS, *ivf.PACKS, *womenchild.PACKS)}


def get(code: str):
    return PACKS.get(code)
