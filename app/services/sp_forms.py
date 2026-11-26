"""Specialty OS — S2 forms engine (migration 151; docs/specialty-os/GAP-ANALYSIS.md §2, EYE.md §4, DENTAL.md §4).

Forms are code-defined and versioned (FORMS below): a form is a pure validator that returns the cleaned data and the
only values Kriya computes — arithmetic on what the clinician entered (logMAR from Snellen, spherical equivalent,
mean K, PASI, DMFT / dmft, mean pocket depth). Nothing is inferred, suggested or diagnosed; no AI. A form belongs to one
OPD encounter and is frozen by the database the moment the encounter is signed (sp_form_freeze). The prescription PDF
prints a forms block only when rows exist (byte-identical otherwise — tests/test_sp_forms.py golden).
"""

import math
import re
from typing import Any, Callable, Optional

from fastapi import HTTPException

SNELLEN = ("6/5", "6/6", "6/9", "6/12", "6/18", "6/24", "6/36", "6/60", "5/60", "4/60", "3/60", "2/60", "1/60")
LOW_VISION = ("CF", "HM", "PL", "NPL")
NEAR = ("N5", "N6", "N8", "N10", "N12", "N18", "N24", "N36")
FDI_ADULT = tuple(f"{q}{n}" for q in (1, 2, 3, 4) for n in range(1, 9))
FDI_PRIMARY = tuple(f"{q}{n}" for q in (5, 6, 7, 8) for n in range(1, 6))
TOOTH_STATUS = ("present", "missing", "implant", "crown", "rct", "bridge_pontic", "unerupted")
SURFACES = ("M", "O", "I", "D", "B", "L")
SURFACE_STATE = ("caries", "filled", "fractured", "sealant")
PLANNED = ("filling", "rct", "crown", "extraction", "implant", "scaling", "bridge", "veneer", "sealant")
DERMA_PROCEDURES = ("laser_hair_reduction", "q_switch", "prp", "chemical_peel", "mnrf", "hydrafacial", "cryotherapy",
                    "electrocautery", "other")
PASI_REGIONS = {"head": 0.1, "upper": 0.2, "trunk": 0.3, "lower": 0.4}


class FormError(ValueError):
    pass


# ── primitives ───────────────────────────────────────────────────────────────

def _obj(v, path) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise FormError(f"{path}: expected an object")
    return v


def _only(d: dict, keys, path):
    extra = set(d) - set(keys)
    if extra:
        raise FormError(f"{path}: unknown field(s) {', '.join(sorted(extra))}")


def _num(v, path, lo, hi, step: Optional[float] = None, integer=False) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(str(v).strip())
        except ValueError:
            raise FormError(f"{path}: not a number") from None
    if math.isnan(v) or not lo <= v <= hi:
        raise FormError(f"{path}: must be between {lo} and {hi}")
    if integer:
        if v != int(v):
            raise FormError(f"{path}: must be a whole number")
        return int(v)
    if step and abs(round(v / step) * step - v) > 1e-6:
        raise FormError(f"{path}: must be in steps of {step}")
    return round(float(v), 2)


def _enum(v, path, allowed) -> Optional[str]:
    if v is None or v == "":
        return None
    if v not in allowed:
        raise FormError(f"{path}: must be one of {', '.join(allowed)}")
    return v


def _text(v, path, n=120) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        raise FormError(f"{path}: expected text")
    v = re.sub(r"\s+", " ", v).strip()
    if len(v) > n:
        raise FormError(f"{path}: at most {n} characters")
    return v or None


def _time(v, path) -> Optional[str]:
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", v):
        raise FormError(f"{path}: use HH:MM")
    return v


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != {} and v != []}


def logmar(snellen: Optional[str]) -> Optional[float]:
    """logMAR of a Snellen fraction (6/60 → 1.0); low-vision notations have no logMAR here (shown as entered)."""
    if not snellen or "/" not in snellen:
        return None
    num, den = (float(x) for x in snellen.split("/"))
    return round(math.log10(den / num), 2) + 0.0


# ── eye ──────────────────────────────────────────────────────────────────────

def _eye_side(d, path) -> tuple[dict, dict]:
    d = _obj(d, path)
    _only(d, ("va", "refraction", "iop", "slit_lamp", "fundus"), path)
    va = _obj(d.get("va"), f"{path}.va")
    _only(va, ("distance_unaided", "distance_aided", "pinhole", "near"), f"{path}.va")
    far = SNELLEN + LOW_VISION
    va_c = _drop_none({k: _enum(va.get(k), f"{path}.va.{k}", far) for k in ("distance_unaided", "distance_aided", "pinhole")}
                      | {"near": _enum(va.get("near"), f"{path}.va.near", NEAR)})
    rf = _obj(d.get("refraction"), f"{path}.refraction")
    _only(rf, ("method", "sph", "cyl", "axis", "add"), f"{path}.refraction")
    rf_c = _drop_none({"method": _enum(rf.get("method"), f"{path}.refraction.method", ("auto", "subjective", "cyclo")),
                       "sph": _num(rf.get("sph"), f"{path}.refraction.sph", -30, 30, 0.25),
                       "cyl": _num(rf.get("cyl"), f"{path}.refraction.cyl", -10, 10, 0.25),
                       "axis": _num(rf.get("axis"), f"{path}.refraction.axis", 0, 180, integer=True),
                       "add": _num(rf.get("add"), f"{path}.refraction.add", 0, 4, 0.25)})
    if rf_c.get("cyl") and rf_c.get("axis") is None:
        raise FormError(f"{path}.refraction.axis: needed when there is a cylinder")
    iop = _obj(d.get("iop"), f"{path}.iop")
    _only(iop, ("mmhg", "method", "time"), f"{path}.iop")
    iop_c = _drop_none({"mmhg": _num(iop.get("mmhg"), f"{path}.iop.mmhg", 0, 80, integer=True),
                        "method": _enum(iop.get("method"), f"{path}.iop.method", ("nct", "applanation", "icare")),
                        "time": _time(iop.get("time"), f"{path}.iop.time")})
    sl = _obj(d.get("slit_lamp"), f"{path}.slit_lamp")
    _only(sl, ("lids", "conj", "cornea", "ac", "iris", "lens"), f"{path}.slit_lamp")
    fu = _obj(d.get("fundus"), f"{path}.fundus")
    _only(fu, ("cdr", "macula", "vessels", "periphery"), f"{path}.fundus")
    clean = _drop_none({"va": va_c, "refraction": rf_c, "iop": iop_c,
                        "slit_lamp": _drop_none({k: _text(sl.get(k), f"{path}.slit_lamp.{k}") for k in sl}),
                        "fundus": _drop_none({"cdr": _num(fu.get("cdr"), f"{path}.fundus.cdr", 0, 1, 0.05),
                                              **{k: _text(fu.get(k), f"{path}.fundus.{k}") for k in ("macula", "vessels",
                                                                                                     "periphery")}})})
    comp = _drop_none({f"logmar_{k}": logmar(v) for k, v in va_c.items() if k != "near"})
    if rf_c.get("sph") is not None:
        comp["sph_equivalent"] = round(rf_c["sph"] + (rf_c.get("cyl") or 0) / 2, 3)
    return clean, comp


def eye_exam_v1(data: dict, ctx: dict) -> tuple[dict, dict]:
    _only(data, ("od", "os", "pd_mm", "lens_advice", "diagnosis"), "")
    od, c_od = _eye_side(data.get("od"), "od")
    os_, c_os = _eye_side(data.get("os"), "os")
    dx = data.get("diagnosis") or []
    if not isinstance(dx, list) or len(dx) > 6:
        raise FormError("diagnosis: up to 6 entries")
    dx_c = []
    for i, x in enumerate(dx):
        x = _obj(x, f"diagnosis[{i}]")
        _only(x, ("icd10", "eye"), f"diagnosis[{i}]")
        code = str(x.get("icd10") or "").upper().strip()
        if not re.fullmatch(r"[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?", code):
            raise FormError(f"diagnosis[{i}].icd10: not an ICD-10 code")
        dx_c.append({"icd10": code, "eye": _enum(x.get("eye"), f"diagnosis[{i}].eye", ("od", "os", "ou")) or "ou"})
    clean = _drop_none({"od": od, "os": os_, "pd_mm": _num(data.get("pd_mm"), "pd_mm", 40, 80, 0.5),
                        "lens_advice": _text(data.get("lens_advice"), "lens_advice", 200), "diagnosis": dx_c})
    return clean, _drop_none({"od": c_od, "os": c_os})


def eye_biometry_v1(data: dict, ctx: dict) -> tuple[dict, dict]:
    _only(data, ("od", "os"), "")
    clean, comp = {}, {}
    for side in ("od", "os"):
        d = _obj(data.get(side), side)
        _only(d, ("axial_length_mm", "k1", "k2", "acd_mm", "formula", "iol_model", "iol_power"), side)
        c = _drop_none({"axial_length_mm": _num(d.get("axial_length_mm"), f"{side}.axial_length_mm", 15, 40, 0.01),
                        "k1": _num(d.get("k1"), f"{side}.k1", 30, 60, 0.01), "k2": _num(d.get("k2"), f"{side}.k2", 30, 60, 0.01),
                        "acd_mm": _num(d.get("acd_mm"), f"{side}.acd_mm", 1, 6, 0.01),
                        "formula": _enum(d.get("formula"), f"{side}.formula", ("srk_t", "barrett", "haigis", "hoffer_q",
                                                                             "holladay_1")),
                        "iol_model": _text(d.get("iol_model"), f"{side}.iol_model", 80),
                        "iol_power": _num(d.get("iol_power"), f"{side}.iol_power", -10, 40, 0.5)})
        if c:
            clean[side] = c
        if c.get("k1") and c.get("k2"):
            comp[side] = {"mean_k": round((c["k1"] + c["k2"]) / 2, 2)}
    return clean, comp


# ── dental ───────────────────────────────────────────────────────────────────

def dental_chart_v1(data: dict, ctx: dict) -> tuple[dict, dict]:
    _only(data, ("dentition", "teeth", "notes"), "")
    dentition = _enum(data.get("dentition"), "dentition", ("adult", "primary", "mixed")) or "adult"
    age = ctx.get("age_years")
    allowed = {"adult": FDI_ADULT, "primary": FDI_PRIMARY, "mixed": FDI_ADULT + FDI_PRIMARY}[dentition]
    if dentition != "adult" and age is not None and age >= 14 and not ctx.get("staff_confirmed_primary"):
        raise FormError("dentition: primary teeth for a patient aged 14 or more need staff confirmation")
    teeth = _obj(data.get("teeth"), "teeth")
    clean_t: dict = {}
    for code, t in teeth.items():
        if code not in allowed:
            raise FormError(f"teeth.{code}: not an FDI tooth of the {dentition} dentition")
        t = _obj(t, f"teeth.{code}")
        _only(t, ("status", "surfaces", "mobility", "planned"), f"teeth.{code}")
        surf = _obj(t.get("surfaces"), f"teeth.{code}.surfaces")
        for s, st in surf.items():
            if s not in SURFACES:
                raise FormError(f"teeth.{code}.surfaces.{s}: surface must be one of {', '.join(SURFACES)}")
            _enum(st, f"teeth.{code}.surfaces.{s}", SURFACE_STATE)
        planned = t.get("planned") or []
        if not isinstance(planned, list) or any(p not in PLANNED for p in planned) or len(set(planned)) != len(planned):
            raise FormError(f"teeth.{code}.planned: each of {', '.join(PLANNED)} at most once")
        c = _drop_none({"status": _enum(t.get("status"), f"teeth.{code}.status", TOOTH_STATUS) or "present",
                        "surfaces": dict(sorted(surf.items())), "planned": planned,
                        "mobility": _num(t.get("mobility"), f"teeth.{code}.mobility", 0, 3, integer=True)})
        if c["status"] == "missing" and (c.get("surfaces") or c.get("mobility")):
            raise FormError(f"teeth.{code}: a missing tooth has no surfaces or mobility")
        clean_t[code] = c
    decayed = sum(1 for t in clean_t.values() if "caries" in (t.get("surfaces") or {}).values())
    missing = sum(1 for t in clean_t.values() if t["status"] == "missing")
    filled = sum(1 for t in clean_t.values() if "filled" in (t.get("surfaces") or {}).values()
                 and "caries" not in (t.get("surfaces") or {}).values())
    planned_teeth = sorted(k for k, t in clean_t.items() if t.get("planned"))
    clean = _drop_none({"dentition": dentition, "teeth": dict(sorted(clean_t.items())),
                        "notes": _text(data.get("notes"), "notes", 500)})
    return clean, {"dmft": {"d": decayed, "m": missing, "f": filled, "total": decayed + missing + filled},
                   "planned_teeth": planned_teeth}


def dental_perio_v1(data: dict, ctx: dict) -> tuple[dict, dict]:
    _only(data, ("teeth",), "")
    teeth = _obj(data.get("teeth"), "teeth")
    clean, depths, bop = {}, [], []
    for code, t in teeth.items():
        if code not in FDI_ADULT:
            raise FormError(f"teeth.{code}: not an adult FDI tooth")
        t = _obj(t, f"teeth.{code}")
        _only(t, ("pd", "bop", "recession"), f"teeth.{code}")
        pd = t.get("pd") or []
        if not isinstance(pd, list) or len(pd) != 6:
            raise FormError(f"teeth.{code}.pd: six sites (MB, B, DB, ML, L, DL)")
        pd_c = [_num(x, f"teeth.{code}.pd[{i}]", 0, 15, integer=True) for i, x in enumerate(pd)]
        b = t.get("bop") or [False] * 6
        if not isinstance(b, list) or len(b) != 6 or any(not isinstance(x, bool) for x in b):
            raise FormError(f"teeth.{code}.bop: six true / false values")
        rec = t.get("recession") or [0] * 6
        if not isinstance(rec, list) or len(rec) != 6:
            raise FormError(f"teeth.{code}.recession: six sites")
        rec_c = [_num(x, f"teeth.{code}.recession[{i}]", 0, 15, integer=True) for i, x in enumerate(rec)]
        clean[code] = {"pd": pd_c, "bop": b, "recession": rec_c}
        depths += [x for x in pd_c if x is not None]
        bop += b
    comp = {"mean_pd_mm": round(sum(depths) / len(depths), 1) if depths else None,
            "bop_pct": round(100 * sum(bop) / len(bop)) if bop else None,
            "sites_ge_5mm": sum(1 for x in depths if x >= 5)}
    return {"teeth": dict(sorted(clean.items()))}, _drop_none(comp)


# ── derma ────────────────────────────────────────────────────────────────────

def derma_procedure_v1(data: dict, ctx: dict) -> tuple[dict, dict]:
    keys = ("procedure", "device", "wavelength_nm", "fluence_j_cm2", "spot_mm", "pulse_ms", "shots", "area", "fitzpatrick",
            "test_patch", "adverse_event", "session_no")
    _only(data, keys, "")
    clean = _drop_none({
        "procedure": _enum(data.get("procedure"), "procedure", DERMA_PROCEDURES),
        "device": _text(data.get("device"), "device", 80),
        "wavelength_nm": _num(data.get("wavelength_nm"), "wavelength_nm", 300, 11000, integer=True),
        "fluence_j_cm2": _num(data.get("fluence_j_cm2"), "fluence_j_cm2", 0, 200, 0.1),
        "spot_mm": _num(data.get("spot_mm"), "spot_mm", 1, 30, 0.5),
        "pulse_ms": _num(data.get("pulse_ms"), "pulse_ms", 0, 1000, 0.1),
        "shots": _num(data.get("shots"), "shots", 0, 100000, integer=True),
        "area": _text(data.get("area"), "area", 120),
        "fitzpatrick": _enum(data.get("fitzpatrick"), "fitzpatrick", ("I", "II", "III", "IV", "V", "VI")),
        "test_patch": _enum(data.get("test_patch"), "test_patch", ("not_done", "negative", "positive")),
        "adverse_event": _text(data.get("adverse_event"), "adverse_event", 300),
        "session_no": _num(data.get("session_no"), "session_no", 1, 100, integer=True)})
    if not clean.get("procedure"):
        raise FormError("procedure: required")
    if clean.get("test_patch") == "positive" and not clean.get("adverse_event"):
        raise FormError("adverse_event: describe the positive test patch")
    return clean, {}


SCORAD_INTENSITY = ("erythema", "oedema", "oozing", "excoriation", "lichenification", "dryness")
MASI_REGIONS = {"forehead": 0.3, "right_malar": 0.3, "left_malar": 0.3, "chin": 0.1}
SALT_REGIONS = {"vertex": 0.40, "right": 0.18, "left": 0.18, "posterior": 0.24}


def derma_exam_v1(data: dict, ctx: dict) -> tuple[dict, dict]:
    """Severity scores are arithmetic on the clinician's entered components only (no image is read):
    PASI  = Σ region weight (head .1, upper .2, trunk .3, lower .4) × (erythema + induration + desquamation) × area 0–6
    SCORAD = extent % / 5 + 7 × (Σ six intensity items 0–3) / 2 + pruritus VAS 0–10 + sleep-loss VAS 0–10   (0–103)
    MASI  = Σ region weight (forehead .3, each malar .3, chin .1) × area 0–6 × (darkness 0–4 + homogeneity 0–4)   (0–48)
    SALT  = .40 vertex + .18 right + .18 left + .24 posterior, each the % of that scalp area with hair loss  (0–100)"""
    _only(data, ("pasi", "scorad", "masi", "salt", "lesions", "notes"), "")
    comp: dict = {}
    pasi_in = _obj(data.get("pasi"), "pasi")
    _only(pasi_in, PASI_REGIONS, "pasi")
    pasi_c, total = {}, 0.0
    for region, w in PASI_REGIONS.items():
        r = _obj(pasi_in.get(region), f"pasi.{region}")
        if not r:
            continue
        _only(r, ("erythema", "induration", "desquamation", "area"), f"pasi.{region}")
        c = {k: _num(r.get(k), f"pasi.{region}.{k}", 0, 4, integer=True) or 0 for k in ("erythema", "induration",
                                                                                         "desquamation")}
        c["area"] = _num(r.get("area"), f"pasi.{region}.area", 0, 6, integer=True) or 0
        pasi_c[region] = c
        total += w * (c["erythema"] + c["induration"] + c["desquamation"]) * c["area"]
    if pasi_c:
        comp["pasi"] = round(total, 1)
    sc_in = _obj(data.get("scorad"), "scorad")
    sc_c: dict = {}
    if sc_in:
        _only(sc_in, ("extent_pct", "intensity", "pruritus", "sleep_loss"), "scorad")
        it = _obj(sc_in.get("intensity"), "scorad.intensity")
        _only(it, SCORAD_INTENSITY, "scorad.intensity")
        sc_c = {"extent_pct": _num(sc_in.get("extent_pct"), "scorad.extent_pct", 0, 100, 0.5) or 0,
                "intensity": {k: _num(it.get(k), f"scorad.intensity.{k}", 0, 3, integer=True) or 0 for k in SCORAD_INTENSITY},
                "pruritus": _num(sc_in.get("pruritus"), "scorad.pruritus", 0, 10, 0.5) or 0,
                "sleep_loss": _num(sc_in.get("sleep_loss"), "scorad.sleep_loss", 0, 10, 0.5) or 0}
        b = sum(sc_c["intensity"].values())
        comp["scorad"] = round(sc_c["extent_pct"] / 5 + 7 * b / 2 + sc_c["pruritus"] + sc_c["sleep_loss"], 1)
        comp["scorad_objective"] = round(sc_c["extent_pct"] / 5 + 7 * b / 2, 1)
    masi_in = _obj(data.get("masi"), "masi")
    _only(masi_in, MASI_REGIONS, "masi")
    masi_c, mt = {}, 0.0
    for region, w in MASI_REGIONS.items():
        r = _obj(masi_in.get(region), f"masi.{region}")
        if not r:
            continue
        _only(r, ("area", "darkness", "homogeneity"), f"masi.{region}")
        c = {"area": _num(r.get("area"), f"masi.{region}.area", 0, 6, integer=True) or 0,
             "darkness": _num(r.get("darkness"), f"masi.{region}.darkness", 0, 4, integer=True) or 0,
             "homogeneity": _num(r.get("homogeneity"), f"masi.{region}.homogeneity", 0, 4, integer=True) or 0}
        masi_c[region] = c
        mt += w * c["area"] * (c["darkness"] + c["homogeneity"])
    if masi_c:
        comp["masi"] = round(mt, 1)
    salt_in = _obj(data.get("salt"), "salt")
    _only(salt_in, SALT_REGIONS, "salt")
    salt_c = {k: _num(salt_in.get(k), f"salt.{k}", 0, 100, 0.5) for k in SALT_REGIONS if salt_in.get(k) is not None}
    if salt_c:
        comp["salt"] = round(sum(SALT_REGIONS[k] * salt_c.get(k, 0) for k in SALT_REGIONS), 1)
    clean = _drop_none({"pasi": pasi_c, "scorad": sc_c, "masi": masi_c, "salt": salt_c,
                        "lesions": _text(data.get("lesions"), "lesions", 500),
                        "notes": _text(data.get("notes"), "notes", 500)})
    return clean, comp


# ── camp screening (S9; stored on sp_camp_registrations.screening, not on an encounter) ─

def camp_screening_v1(data: dict, ctx: dict) -> tuple[dict, dict]:
    _only(data, ("va_od", "va_os", "bp_sys", "bp_dia", "rbs_mg_dl", "dental_caries", "notes"), "")
    far = SNELLEN + LOW_VISION
    clean = _drop_none({"va_od": _enum(data.get("va_od"), "va_od", far), "va_os": _enum(data.get("va_os"), "va_os", far),
                        "bp_sys": _num(data.get("bp_sys"), "bp_sys", 60, 260, integer=True),
                        "bp_dia": _num(data.get("bp_dia"), "bp_dia", 30, 160, integer=True),
                        "rbs_mg_dl": _num(data.get("rbs_mg_dl"), "rbs_mg_dl", 20, 800, integer=True),
                        "dental_caries": data.get("dental_caries") if isinstance(data.get("dental_caries"), bool) else None,
                        "notes": _text(data.get("notes"), "notes", 300)})
    return clean, _drop_none({"logmar_od": logmar(clean.get("va_od")), "logmar_os": logmar(clean.get("va_os"))})


#: code → (version, pack that must be on, validator, title on the PDF)
FORMS: dict[str, tuple[int, str, Callable[[dict, dict], tuple[dict, dict]], str]] = {
    "eye_exam_v1": (1, "eye", eye_exam_v1, "Eye examination"),
    "eye_biometry_v1": (1, "eye", eye_biometry_v1, "Biometry"),
    "dental_chart_v1": (1, "dental", dental_chart_v1, "Dental chart"),
    "dental_perio_v1": (1, "dental", dental_perio_v1, "Periodontal chart"),
    "derma_procedure_v1": (1, "derma", derma_procedure_v1, "Procedure record"),
    "derma_exam_v1": (1, "derma", derma_exam_v1, "Skin examination"),
}


def validate(form_code: str, data: Any, ctx: Optional[dict] = None) -> tuple[dict, dict]:
    if form_code not in FORMS and form_code != "camp_screening_v1":
        raise HTTPException(status_code=404, detail="Unknown form.")
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Form data must be an object.")
    fn = camp_screening_v1 if form_code == "camp_screening_v1" else FORMS[form_code][2]
    try:
        return fn(data, ctx or {})
    except FormError as e:
        raise HTTPException(status_code=422, detail={"code": "sp_form_invalid", "message": str(e)}) from None


# ── printable lines (prescription PDF block, spectacle Rx) ───────────────────

def _fmt(v: Optional[float], sign=True) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}" if sign else f"{v:g}"


def spectacle_rows(eye: dict) -> list[tuple[str, str, str, str, str]]:
    """(eye, SPH, CYL, AXIS, ADD) for OD and OS from a validated eye_exam_v1."""
    out = []
    for side, label in (("od", "OD (R)"), ("os", "OS (L)")):
        r = (eye.get(side) or {}).get("refraction") or {}
        out.append((label, _fmt(r.get("sph")), _fmt(r.get("cyl")), str(r["axis"]) if r.get("axis") is not None else "—",
                    _fmt(r.get("add")) if r.get("add") else "—"))
    return out


def pdf_lines(form: dict) -> list[str]:
    code, d, c = form["form_code"], form.get("data") or {}, form.get("computed") or {}
    if code == "eye_exam_v1":
        lines = []
        for side, label in (("od", "OD"), ("os", "OS")):
            s = d.get(side) or {}
            va = s.get("va") or {}
            iop = s.get("iop") or {}
            parts = [f"VA {va.get('distance_unaided', '—')}" + (f" / aided {va['distance_aided']}" if va.get("distance_aided") else "")]
            r = s.get("refraction") or {}
            if r:
                parts.append(f"Rx {_fmt(r.get('sph'))} / {_fmt(r.get('cyl'))} x {r.get('axis', '—')}"
                             + (f" add {_fmt(r.get('add'))}" if r.get("add") else ""))
            if iop.get("mmhg") is not None:
                parts.append(f"IOP {iop['mmhg']} mmHg" + (f" ({iop['method'].upper()})" if iop.get("method") else ""))
            if (s.get("fundus") or {}).get("cdr") is not None:
                parts.append(f"CDR {s['fundus']['cdr']}")
            lines.append(f"{label}: " + "; ".join(parts))
        if d.get("diagnosis"):
            lines.append("Diagnosis: " + ", ".join(f"{x['icd10']} ({x['eye'].upper()})" for x in d["diagnosis"]))
        return lines
    if code == "eye_biometry_v1":
        return [f"{s.upper()}: AL {v.get('axial_length_mm', '—')} mm, K {v.get('k1', '—')}/{v.get('k2', '—')}, "
                f"IOL {v.get('iol_model', '—')} {_fmt(v.get('iol_power'))} D" for s, v in d.items()]
    if code == "dental_chart_v1":
        dm = c.get("dmft") or {}
        out = [f"{d.get('dentition', 'adult').title()} dentition — D {dm.get('d', 0)} M {dm.get('m', 0)} F {dm.get('f', 0)}"]
        planned = [f"{k}: {', '.join(t['planned'])}" for k, t in (d.get("teeth") or {}).items() if t.get("planned")]
        if planned:
            out.append("Planned: " + "; ".join(planned))
        return out
    if code == "dental_perio_v1":
        return [f"Mean pocket depth {c.get('mean_pd_mm', '—')} mm, bleeding {c.get('bop_pct', '—')} %, "
                f"sites ≥ 5 mm: {c.get('sites_ge_5mm', 0)}"]
    if code == "derma_procedure_v1":
        keys = (("procedure", ""), ("wavelength_nm", " nm"), ("fluence_j_cm2", " J/cm²"), ("spot_mm", " mm spot"),
                ("shots", " shots"), ("area", ""), ("fitzpatrick", ""))
        return [", ".join(f"{str(d[k]).replace('_', ' ')}{u}" for k, u in keys if d.get(k) is not None)]
    if code == "derma_exam_v1":
        scores = [f"{k.upper()} {c[k]}" for k in ("pasi", "scorad", "masi", "salt") if k in c]
        return [" · ".join(scores), *([d["lesions"]] if d.get("lesions") else [])]
    return []
