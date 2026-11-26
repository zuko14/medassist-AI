"""gad7 — Anxiety (PROTOCOLS.md §3.4, same shape as phq9; bands 5/10/15). Source: Spitzer et al. 2006."""

from . import phq9
from .base import Param

CODE, VERSION, TITLE = "gad7", "1.0.0", "Anxiety (GAD-7)"
ICD10 = ("F41.1",)
MEASURES = ("gad7",)
DUE_LABS: dict = {}
CARDS: tuple = ()
QUESTIONS = 7
PARAMS = {
    "severe_cut": Param(15, 15, 18, "score", "Spitzer 2006 (15–21 severe)"),
    "worse_delta": Param(5, 3, 7, "points", "Spitzer 2006"),
    "weeks_check": Param(8, 6, 12, "weeks", "TEAMcare"),
    "interval_days": Param(14, 14, 28, "days", "MANAS (two-weekly)"),
}


def evaluate(p, inp):
    return phq9.evaluate(p, inp, measure="gad7")
