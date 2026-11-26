"""WHO growth z-scores (SP-WC-04) — arithmetic on WHO's published LMS tables (app/services/sp_growth_tables.py).

z = ((y / M)^L − 1) / (L × S), with L, M, S interpolated linearly between whole months (age in days / 30.4375). For
weight-for-age and BMI-for-age, WHO's restricted method applies beyond ±3 SD (the distance between SD2 and SD3 is used
outside it); length/height and head circumference use the plain formula (L = 1). Length / height (WHO 2006): under
24 months the table is recumbent length — a standing height there gets +0.7 cm; from 24 months it is standing height
— a recumbent length there gets −0.7 cm. Ranges: weight 0–120 months, length/height and BMI 0–228, head 0–60;
outside a range the indicator is not computed. Nothing is interpreted: the doctor reads the z-score.
"""

import math
from datetime import date
from typing import Optional

from app.services.sp_growth_tables import LMS

DAYS_PER_MONTH = 30.4375
INDICATORS = ("wfa", "lhfa", "hcfa", "bfa")
RESTRICTED = ("wfa", "bfa")


def age_months(dob: date, on: date) -> float:
    return (on - dob).days / DAYS_PER_MONTH


def lms(indicator: str, sex: str, months: float) -> Optional[tuple[float, float, float]]:
    table = LMS.get(f"{indicator}_{'boys' if sex == 'male' else 'girls'}")
    if not table or months < 0 or months > max(table):
        return None
    lo = int(math.floor(months))
    if lo == months or lo + 1 not in table:
        return table.get(lo)
    f = months - lo
    a, b = table[lo], table[lo + 1]
    return tuple(x + (y - x) * f for x, y in zip(a, b))


def _y_at(z: float, L: float, M: float, S: float) -> float:
    return M * (1 + L * S * z) ** (1 / L) if L else M * math.exp(S * z)


def zscore(indicator: str, sex: str, months: float, value: float) -> Optional[float]:
    p = lms(indicator, sex, months)
    if p is None or value is None or value <= 0:
        return None
    L, M, S = p
    z = math.log(value / M) / S if L == 0 else ((value / M) ** L - 1) / (L * S)
    if indicator in RESTRICTED and abs(z) > 3:
        if z > 3:
            sd3, sd2 = _y_at(3, L, M, S), _y_at(2, L, M, S)
            z = 3 + (value - sd3) / (sd3 - sd2)
        else:
            sd3, sd2 = _y_at(-3, L, M, S), _y_at(-2, L, M, S)
            z = -3 + (value - sd3) / (sd2 - sd3)
    return round(z, 2)


def measurement_z(sex: str, dob: date, on: date, weight_kg: Optional[float] = None, length_cm: Optional[float] = None,
                  position: str = "recumbent", head_cm: Optional[float] = None) -> dict:
    """All computable z-scores for one visit. sex: 'male' | 'female' (other / undisclosed → none computed)."""
    if sex not in ("male", "female"):
        return {}
    m = age_months(dob, on)
    out: dict = {"age_months": round(m, 2)}
    if weight_kg:
        out["wfa"] = zscore("wfa", sex, m, weight_kg)
    if length_cm:
        adj = length_cm + (0.7 if position == "standing" and m < 24 else -0.7 if position == "recumbent" and m >= 24 else 0)
        out["lhfa"] = zscore("lhfa", sex, m, adj)
        if weight_kg:
            out["bmi"] = round(weight_kg / (adj / 100) ** 2, 2)
            out["bfa"] = zscore("bfa", sex, m, out["bmi"])
    if head_cm:
        out["hcfa"] = zscore("hcfa", sex, m, head_cm)
    return {k: v for k, v in out.items() if v is not None}


def band(z: Optional[float]) -> Optional[str]:
    """Neutral display band for a chip (not a diagnosis): within ±2, beyond ±2, beyond ±3."""
    if z is None:
        return None
    return "beyond_3" if abs(z) > 3 else "beyond_2" if abs(z) > 2 else "within_2"
