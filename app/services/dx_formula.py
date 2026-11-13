"""Calculated lab parameters (REPORT-TEMPLATES.md §3): a small, safe formula language.

Parsed with ast.parse(mode="eval") and walked against a whitelist; nothing is
ever eval'd or exec'd. Names are parameter codes of the same test plus the
patient context AGE_YEARS, AGE_DAYS, IS_FEMALE, IS_MALE (0/1).

  operators  + - * /, unary + -, ** (constant exponent |n| <= 4; use pow() beyond)
  compare    < <= > >= == != (only inside iif)
  functions  min max abs sqrt ln log10 pow round iif egfr_ckd_epi_2021

+ - * / run in Decimal (28 digits); transcendental functions in float, brought
back to Decimal at 12 significant digits. Division by zero, a missing input or
a domain error yields None (the parameter is shown as not computed). Formulas
consume UNROUNDED upstream values; only the displayed value is rounded.

Pure and deterministic: no I/O, no AI (tests/test_dx_no_llm.py).
"""

import ast
import math
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, InvalidOperation
from typing import Optional

CONTEXT_NAMES = frozenset({"AGE_YEARS", "AGE_DAYS", "IS_FEMALE", "IS_MALE"})
FUNCTIONS = {"min": (1, 10), "max": (1, 10), "abs": (1, 1), "sqrt": (1, 1), "ln": (1, 1), "log10": (1, 1),
             "pow": (2, 2), "round": (1, 2), "iif": (3, 3), "egfr_ckd_epi_2021": (1, 1)}
ROUNDING = {"half_up": ROUND_HALF_UP, "half_even": ROUND_HALF_EVEN, "truncate": ROUND_DOWN}
MAX_LEN, MAX_NODES, MAX_CONST = 500, 200, Decimal("1e12")
_CTX = Context(prec=28)
_BIN = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Pow: "**"}
_CMP = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}


class FormulaError(ValueError):
    """dx_formula_invalid: the formula is refused at save time; `token` names the culprit."""

    code = "dx_formula_invalid"

    def __init__(self, message: str, token: str = ""):
        super().__init__(message)
        self.token = token


class _NotComputed(Exception):
    """Runtime only: division by zero, missing input, domain error."""


# ── validation ──────────────────────────────────────────────────────────────

def _parse(formula: str) -> ast.Expression:
    if not isinstance(formula, str) or not formula.strip():
        raise FormulaError("The formula is empty.")
    if len(formula) > MAX_LEN:
        raise FormulaError(f"The formula is longer than {MAX_LEN} characters.")
    try:
        tree = ast.parse(formula.strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError("The formula is not valid.", (e.text or "").strip()[: 40]) from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise FormulaError("The formula is too long.")
    return tree


def _check(node: ast.AST, names: set, in_iif_cond: bool = False) -> None:
    if isinstance(node, ast.Expression):
        return _check(node.body, names)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError("Only numbers are allowed as constants.", repr(node.value)[:40])
        if abs(Decimal(str(node.value))) > MAX_CONST:
            raise FormulaError("The constant is too large.", str(node.value))
        return
    if isinstance(node, ast.Name):
        names.add(node.id)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _check(node.operand, names)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        if isinstance(node.op, ast.Pow):
            exp = node.right
            if isinstance(exp, ast.UnaryOp) and isinstance(exp.op, ast.USub):
                exp = exp.operand
            if not (isinstance(exp, ast.Constant) and isinstance(exp.value, (int, float))
                    and not isinstance(exp.value, bool) and abs(exp.value) <= 4):
                raise FormulaError("Use pow(x, y) for exponents other than a constant up to 4.", "**")
        _check(node.left, names)
        return _check(node.right, names)
    if isinstance(node, ast.Compare):
        if not in_iif_cond:
            raise FormulaError("Comparisons are only allowed as the first argument of iif().",
                               _CMP.get(type(node.ops[0]), "?"))
        if len(node.ops) != 1 or type(node.ops[0]) not in _CMP:
            raise FormulaError("Use one comparison per iif() condition.")
        _check(node.left, names)
        return _check(node.comparators[0], names)
    if isinstance(node, ast.Call):
        fn = node.func.id if isinstance(node.func, ast.Name) else None
        if fn not in FUNCTIONS:
            raise FormulaError("Unknown function.", ast.unparse(node.func)[:40])
        if node.keywords:
            raise FormulaError("Keyword arguments are not allowed.", fn)
        lo, hi = FUNCTIONS[fn]
        if not lo <= len(node.args) <= hi:
            raise FormulaError(f"{fn}() takes {lo}" + (f" to {hi}" if hi != lo else "") + " argument(s).", fn)
        for i, a in enumerate(node.args):
            _check(a, names, in_iif_cond=(fn == "iif" and i == 0))
        return
    raise FormulaError("This construct is not allowed in a formula.", type(node).__name__)


def extract_dependencies(formula: str) -> list[str]:
    """The parameter codes a formula reads (context names excluded), sorted.
    Raises FormulaError if the formula is not in the whitelist."""
    names: set = set()
    _check(_parse(formula), names)
    return sorted(names - CONTEXT_NAMES)


def validate_formula(formula: str, known_codes) -> list[str]:
    """extract_dependencies + every name must be a known parameter code of the test."""
    deps = extract_dependencies(formula)
    unknown = [d for d in deps if d not in set(known_codes)]
    if unknown:
        raise FormulaError("Unknown parameter code.", unknown[0])
    return deps


def find_cycle(formulas: dict[str, str]) -> Optional[list[str]]:
    """A dependency cycle among calculated parameters (code -> formula), e.g. ['A', 'B', 'A'], or None."""
    deps = {code: [d for d in extract_dependencies(f) if d in formulas] for code, f in formulas.items()}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> Optional[list[str]]:
        state[n] = 1
        stack.append(n)
        for d in deps[n]:
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if d not in state and (found := visit(d)):
                return found
        stack.pop()
        state[n] = 2
        return None

    for code in sorted(deps):
        if code not in state and (found := visit(code)):
            return found
    return None


def evaluation_order(formulas: dict[str, str]) -> list[str]:
    """Calculated codes in dependency order (topological); FormulaError on a cycle."""
    cycle = find_cycle(formulas)
    if cycle:
        raise FormulaError("Calculated parameters depend on each other in a loop.", " -> ".join(cycle))
    deps = {c: [d for d in extract_dependencies(f) if d in formulas] for c, f in formulas.items()}
    out: list[str] = []
    while len(out) < len(deps):
        for c in sorted(deps):
            if c not in out and all(d in out for d in deps[c]):
                out.append(c)
    return out


# ── evaluation ──────────────────────────────────────────────────────────────

def _dec(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _from_float(x: float) -> Decimal:
    if math.isnan(x) or math.isinf(x):
        raise _NotComputed()
    return Decimal(format(x, ".12g"))


def egfr_ckd_epi_2021(scr_mg_dl: Decimal, age_years: Decimal, is_female: bool) -> Decimal:
    """CKD-EPI 2021 race-free creatinine equation (mL/min/1.73 m²)."""
    scr, age = float(scr_mg_dl), float(age_years)
    if scr <= 0 or age <= 0:
        raise _NotComputed()
    kappa, alpha = (0.7, -0.241) if is_female else (0.9, -0.302)
    r = scr / kappa
    v = 142 * min(r, 1) ** alpha * max(r, 1) ** -1.200 * 0.9938 ** age * (1.012 if is_female else 1)
    return _from_float(v)


def _eval(node: ast.AST, env: dict) -> Decimal:
    if isinstance(node, ast.Constant):
        return _dec(node.value)
    if isinstance(node, ast.Name):
        v = env.get(node.id)
        if v is None:
            raise _NotComputed()
        return _dec(v)
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, env), _eval(node.right, env)
        op = type(node.op)
        if op is ast.Add:
            return _CTX.add(a, b)
        if op is ast.Sub:
            return _CTX.subtract(a, b)
        if op is ast.Mult:
            return _CTX.multiply(a, b)
        if op is ast.Div:
            if b == 0:
                raise _NotComputed()
            return _CTX.divide(a, b)
        return _pow(a, b)
    if isinstance(node, ast.Call):
        fn = node.func.id
        if fn == "iif":
            cond = node.args[0]
            left, right = _eval(cond.left, env), _eval(cond.comparators[0], env)
            ok = {ast.Lt: left < right, ast.LtE: left <= right, ast.Gt: left > right, ast.GtE: left >= right,
                  ast.Eq: left == right, ast.NotEq: left != right}[type(cond.ops[0])]
            return _eval(node.args[1] if ok else node.args[2], env)
        args = [_eval(a, env) for a in node.args]
        if fn == "min":
            return min(args)
        if fn == "max":
            return max(args)
        if fn == "abs":
            return abs(args[0])
        if fn == "round":
            places = int(args[1]) if len(args) > 1 else 0
            return args[0].quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if fn == "pow":
            return _pow(*args)
        if fn == "sqrt":
            if args[0] < 0:
                raise _NotComputed()
            return _from_float(math.sqrt(float(args[0])))
        if fn in ("ln", "log10"):
            if args[0] <= 0:
                raise _NotComputed()
            return _from_float((math.log if fn == "ln" else math.log10)(float(args[0])))
        if fn == "egfr_ckd_epi_2021":
            age = env.get("AGE_YEARS")
            if age is None or env.get("IS_FEMALE") is None:
                raise _NotComputed()
            return egfr_ckd_epi_2021(args[0], _dec(age), bool(int(env["IS_FEMALE"])))
    raise _NotComputed()   # unreachable for validated trees


def _pow(a: Decimal, b: Decimal) -> Decimal:
    try:
        if a == 0 and b < 0:
            raise _NotComputed()
        return _from_float(math.pow(float(a), float(b)))
    except (OverflowError, ValueError):
        raise _NotComputed() from None


def round_value(value: Decimal, decimal_places: int = 2, rounding_mode: str = "half_up") -> Decimal:
    """Display/storage rounding: decimal_places 0–4, mode half_up | half_even | truncate."""
    if rounding_mode not in ROUNDING or not 0 <= decimal_places <= 4:
        raise ValueError("invalid rounding")
    return _dec(value).quantize(Decimal(1).scaleb(-decimal_places), rounding=ROUNDING[rounding_mode])


def evaluate_raw(formula: str, context: dict) -> Optional[Decimal]:
    """Unrounded value (what downstream formulas consume), or None if not computable.
    Raises FormulaError if the formula itself is not allowed."""
    tree = _parse(formula)
    _check(tree, set())
    try:
        return _eval(tree.body, context)
    except (_NotComputed, InvalidOperation, ArithmeticError):
        return None


def evaluate_formula(formula: str, context: dict, decimal_places: int = 2,
                     rounding_mode: str = "half_up") -> Optional[float]:
    """The displayed value, rounded; None if not computable."""
    raw = evaluate_raw(formula, context)
    return None if raw is None else float(round_value(raw, decimal_places, rounding_mode))


def compute_all(params: list[dict], inputs: dict, patient: Optional[dict] = None) -> dict[str, dict]:
    """Evaluate every calculated parameter of one test in dependency order.

    params: [{"code", "formula", "decimal_places", "rounding_mode"}] (formula None = entered value);
    inputs: entered values by code; patient: AGE_YEARS / AGE_DAYS / IS_FEMALE / IS_MALE.
    Returns {code: {"raw": Decimal | None, "value": Decimal | None}} for calculated codes."""
    formulas = {p["code"]: p["formula"] for p in params if p.get("formula") and p.get("code")}
    meta = {p["code"]: p for p in params if p.get("code")}
    env = {**{k: v for k, v in (patient or {}).items() if k in CONTEXT_NAMES},
           **{k: _dec(v) for k, v in inputs.items() if v is not None}}
    out: dict[str, dict] = {}
    for code in evaluation_order(formulas):
        raw = evaluate_raw(formulas[code], env)
        if raw is not None:
            env[code] = raw
        m = meta[code]
        out[code] = {"raw": raw, "value": None if raw is None else
                     round_value(raw, int(m.get("decimal_places", 2)), m.get("rounding_mode") or "half_up")}
    return out
