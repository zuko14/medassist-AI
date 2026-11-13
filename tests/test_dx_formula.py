"""Formula engine (REPORT-TEMPLATES.md §1.1 / §3): vectors, safety, rounding, ordering."""

from decimal import Decimal

import pytest

from app.services import dx_formula as f

LIPID = [
    {"code": "TC", "formula": None}, {"code": "TG", "formula": None}, {"code": "HDL", "formula": None},
    {"code": "VLDL", "formula": "TG / 5", "decimal_places": 0, "rounding_mode": "half_up"},
    {"code": "LDL", "formula": "TC - HDL - TG / 5", "decimal_places": 0, "rounding_mode": "truncate"},
    {"code": "NONHDL", "formula": "TC - HDL", "decimal_places": 0, "rounding_mode": "half_up"},
    {"code": "TC_HDL", "formula": "TC / HDL", "decimal_places": 2, "rounding_mode": "half_up"},
    {"code": "LDL_HDL", "formula": "LDL / HDL", "decimal_places": 2, "rounding_mode": "half_up"},
]


def test_taiyo_vectors_exactly():
    assert f.evaluate_formula("28.7 * HBA1C - 46.7", {"HBA1C": 7.5}, 2) == 168.55
    out = f.compute_all(LIPID, {"TC": 134, "TG": 162, "HDL": 40})
    assert {k: v["value"] for k, v in out.items()} == {
        "VLDL": Decimal("32"), "LDL": Decimal("61"), "NONHDL": Decimal("94"),
        "TC_HDL": Decimal("3.35"), "LDL_HDL": Decimal("1.54")}
    # LDL/HDL is computed from the UNROUNDED LDL (61.6), not the displayed 61 (which would give 1.53)
    assert out["LDL"]["raw"] == Decimal("61.6")


def test_additional_vectors():
    assert f.evaluate_formula("TBIL - DBIL", {"TBIL": 1.2, "DBIL": 0.3}, 1) == 0.9
    assert f.evaluate_formula("ALB / (TP - ALB)", {"ALB": 4.2, "TP": 7.0}, 2) == 1.5
    assert f.evaluate_formula("TP - ALB", {"TP": 7.0, "ALB": 4.2}, 1) == 2.8          # globulin


def test_egfr_ckd_epi_2021_pinned():
    # 142 x min(Scr/k,1)^a x max(Scr/k,1)^-1.200 x 0.9938^age x 1.012[female]; k 0.7/0.9, a -0.241/-0.302
    male = f.evaluate_raw("egfr_ckd_epi_2021(CREAT)", {"CREAT": 1.0, "AGE_YEARS": 50, "IS_FEMALE": 0})
    female = f.evaluate_raw("egfr_ckd_epi_2021(CREAT)", {"CREAT": 0.8, "AGE_YEARS": 50, "IS_FEMALE": 1})
    assert abs(male - Decimal("91.69")) < Decimal("0.01")
    assert abs(female - Decimal("89.71")) < Decimal("0.01")
    assert f.evaluate_formula("egfr_ckd_epi_2021(CREAT)", {"CREAT": 1.0, "AGE_YEARS": 50, "IS_FEMALE": 0}, 0) == 92
    # below kappa the alpha branch applies
    low = f.evaluate_raw("egfr_ckd_epi_2021(CREAT)", {"CREAT": 0.6, "AGE_YEARS": 40, "IS_FEMALE": 1})
    assert abs(low - Decimal("116.30")) < Decimal("0.01")
    # missing age or sex: not computed, never guessed
    assert f.evaluate_raw("egfr_ckd_epi_2021(CREAT)", {"CREAT": 1.0, "IS_FEMALE": 0}) is None


def test_functions_and_iif():
    ctx = {"A": 9, "B": -4, "AGE_YEARS": 70, "IS_FEMALE": 1}
    assert f.evaluate_formula("sqrt(A) + abs(B) + max(A, B, 1) + min(A, B)", ctx) == 3 + 4 + 9 - 4
    assert f.evaluate_formula("pow(A, 0.5)", ctx) == 3
    assert f.evaluate_formula("A ** 2", ctx) == 81
    assert f.evaluate_formula("round(A / 7, 1)", ctx, 4) == 1.3
    assert f.evaluate_formula("iif(AGE_YEARS >= 65, 1, 0)", ctx) == 1
    assert f.evaluate_formula("iif(IS_FEMALE == 1, A * 0.85, A)", ctx) == 7.65
    assert f.evaluate_formula("log10(100) + ln(1)", ctx) == 2


@pytest.mark.parametrize("formula, ctx", [
    ("A / B", {"A": 1, "B": 0}),          # division by zero
    ("A + B", {"A": 1}),                  # missing input
    ("sqrt(A)", {"A": -1}),               # domain error
    ("ln(0)", {}),
    ("pow(0, -1)", {}),
    ("pow(10, 400)", {}),                 # overflow
])
def test_not_computable_returns_none(formula, ctx):
    assert f.evaluate_formula(formula, ctx) is None


@pytest.mark.parametrize("formula, token", [
    ("__import__('os').system('x')", "__import__"),
    ("A.real", "Attribute"),
    ("A[0]", "Subscript"),
    ("lambda: 1", "Lambda"),
    ("'abc'", "'abc'"),
    ("open('x')", "open"),
    ("A if B else C", "IfExp"),
    ("A > B", ">"),
    ("A ** B", "**"),
    ("A ** 10", "**"),
    ("min(A=1)", "min"),
    ("sqrt(A, B)", "sqrt"),
    ("True + 1", "True"),
    ("[1, 2]", "List"),
    ("A +", ""),
    ("", ""),
    ("1e20", "1e+20"),
])
def test_unsafe_or_invalid_formulas_are_refused(formula, token):
    with pytest.raises(f.FormulaError) as e:
        f.extract_dependencies(formula)
    assert e.value.code == "dx_formula_invalid"
    assert token in e.value.token or not token


def test_no_eval_or_exec_in_the_engine():
    import ast
    import inspect
    calls = {n.func.id for n in ast.walk(ast.parse(inspect.getsource(f)))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"eval", "exec", "compile", "__import__"}


def test_dependencies_unknown_codes_and_cycles():
    assert f.extract_dependencies("TC - HDL - TG / 5 + AGE_YEARS * 0") == ["HDL", "TC", "TG"]
    assert f.validate_formula("TC - HDL", ["TC", "HDL"]) == ["HDL", "TC"]
    with pytest.raises(f.FormulaError) as e:
        f.validate_formula("TC - LDLX", ["TC", "HDL"])
    assert e.value.token == "LDLX"
    assert f.find_cycle({"A": "B + 1", "B": "C * 2", "C": "A - 1"}) == ["A", "B", "C", "A"]
    assert f.find_cycle({"A": "A + 1"}) == ["A", "A"]
    assert f.find_cycle({c["code"]: c["formula"] for c in LIPID if c["formula"]}) is None
    with pytest.raises(f.FormulaError):
        f.evaluation_order({"A": "B", "B": "A"})
    order = f.evaluation_order({"LDL_HDL": "LDL / HDL", "LDL": "TC - HDL - TG / 5"})
    assert order.index("LDL") < order.index("LDL_HDL")


def test_rounding_modes():
    assert f.round_value(Decimal("2.345"), 2, "half_up") == Decimal("2.35")
    assert f.round_value(Decimal("2.345"), 2, "half_even") == Decimal("2.34")
    assert f.round_value(Decimal("2.349"), 2, "truncate") == Decimal("2.34")
    assert f.round_value(Decimal("-2.349"), 2, "truncate") == Decimal("-2.34")
    assert f.round_value(Decimal("61.6"), 0, "truncate") == Decimal("61")
    with pytest.raises(ValueError):
        f.round_value(Decimal("1"), 6)


def test_missing_input_leaves_dependents_uncomputed():
    out = f.compute_all(LIPID, {"TC": 134, "HDL": 40})       # TG not entered
    assert out["VLDL"]["value"] is None and out["LDL"]["value"] is None and out["LDL_HDL"]["value"] is None
    assert out["NONHDL"]["value"] == Decimal("94")
