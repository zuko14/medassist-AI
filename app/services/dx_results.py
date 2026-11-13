"""Diagnostic OS result rules (REPORT-TEMPLATES.md §2) — pure, deterministic.

P2 holds the two rules the catalogue defines: delta checks and auto-verification
eligibility. Result entry / verification / approval arrive in P6 (DX-38).
Zero-LLM (tests/test_dx_no_llm.py).
"""

from decimal import Decimal
from typing import Optional


def _dec(v) -> Optional[Decimal]:
    return None if v is None else (v if isinstance(v, Decimal) else Decimal(str(v)))


def delta_check(current_val, prev_val, delta_dir: Optional[str], delta_pct=None, delta_abs=None) -> Optional[dict]:
    """Compare a new value with the person's previous validated value of the same parameter.

    Returns None when no check applies (no previous value, no limit configured, no
    direction); otherwise {"delta_failed", "change_abs", "change_pct", "direction"}.
    A change counts only in the configured direction (increase | decrease | both);
    it fails when it exceeds the absolute limit or the percent limit (percent needs a
    non-zero previous value). Never blocks entry; it blocks batch approval and
    auto-verification only."""
    cur, prev = _dec(current_val), _dec(prev_val)
    pct_limit, abs_limit = _dec(delta_pct), _dec(delta_abs)
    if cur is None or prev is None or delta_dir not in ("increase", "decrease", "both") \
            or (pct_limit is None and abs_limit is None):
        return None
    change = cur - prev
    moved = (change > 0 if delta_dir == "increase" else change < 0 if delta_dir == "decrease" else change != 0)
    change_pct = None if prev == 0 else (abs(change) / abs(prev) * 100).quantize(Decimal("0.01"))
    failed = moved and ((abs_limit is not None and abs(change) > abs_limit)
                        or (pct_limit is not None and change_pct is not None and change_pct > pct_limit))
    return {"delta_failed": bool(failed), "change_abs": change, "change_pct": change_pct, "direction": delta_dir}


def auto_verify_eligible(result_values: list[dict]) -> bool:
    """True iff every value is 'normal' (none not_evaluated, low/high, critical or abnormal),
    none failed a delta check, none carries a free-text remark and none is an amendment.
    An empty result is never auto-verified."""
    if not result_values:
        return False
    return all(v.get("flag") == "normal" and not v.get("delta_failed") and not (v.get("remark") or "").strip()
               and not v.get("is_amendment") for v in result_values)
