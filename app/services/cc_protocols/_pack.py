"""A pack object for modules that hold several small packs (the specialty packs, PROTOCOLS.md §6)."""

from types import SimpleNamespace


def pack(code, title, params, evaluate, icd10=(), measures=(), due_labs=None, cards=(), plan="any", version="1.0.0"):
    return SimpleNamespace(CODE=code, VERSION=version, TITLE=title, PARAMS=params, evaluate=evaluate, ICD10=tuple(icd10),
                           MEASURES=tuple(measures), DUE_LABS=dict(due_labs or {}), CARDS=tuple(cards), PLAN=plan)
