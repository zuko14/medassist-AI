"""CI Query Scoping Linter (KA-P2-11).

Tenant isolation in this platform is enforced ENTIRELY in application code.
Migration 049 defines tenant_isolation_* RLS policies, but they are inert:
app/database.py:36 builds the client from the service_role key, which holds
BYPASSRLS, and nothing ever connects as kriya_app or sets app.clinic_id. The
database will not catch a single missing predicate. Activating RLS is T5.4.

That makes this linter the only automated tenant-isolation guard, so it has to
check the query, not a comment.

What changed, and why
---------------------
The previous version asserted that a line containing `supabase.table(` had the
string `# unscoped:` on it or the line above. It never looked at the query, so
adding the comment was sufficient to pass — and one annotation
(app/routers/admin.py, KA-P1-05) read "tenant-scoped operation with verified
clinic authorization" on a genuinely unscoped cross-tenant read.

It also scanned app/routers/** ONLY. app/services/** — the conversation,
payment, lab-report and queue layers, i.e. most of the query volume — was
never linted at all. Turning that on surfaced 86 tenant-table queries with no
clinic predicate and no annotation.

Those 86 have since been burned down to zero (2026-08-31). Two thirds were
never violations at all: the analyzer could not see the conditional-scoping
idiom, where the predicate lands in a later statement

    query = supabase.table("appointments").select("*")
    if clinic_id != "default":
        query = query.eq("clinic_id", clinic_id)

which _scoped_by_later_reassignment now recognises — narrowly, by requiring the
SAME variable to be reassigned from an expression carrying the predicate, so a
scoped query cannot launder an unscoped sibling in the same function. The
remainder were reviewed one by one and annotated with a structural reason from
ALLOWED_REASONS: a unique row key, a Meta/Razorpay callback id, a per-tenant
sweep, or an INSERT whose payload carries clinic_id.

The RATCHET stays: the counts may go down, never up. A new unscoped query fails
immediately.
"""

import ast
import pathlib

import pytest

# The tenant-table list is imported, not copied. Three divergent copies existed
# (app/database.py: 26 tables, app/services/tenant_scoped_client.py: 15, and one
# here: 17). This one was missing doctor_branches — the join table behind the
# 2026-09-01 incident's doctor/branch mix-up — plus admin_notifications,
# broadcasts, outbound_message_ledger and five others, so queries against them
# were never linted at all. app/database.py is the single source of truth.
from app.tenancy import TENANT_OWNED_TABLES  # noqa: E402


# The ONLY reasons that justify omitting a clinic_id predicate. Free text is
# no longer accepted for NEW annotations — see ROUTERS_LEGACY_ANNOTATIONS.
ALLOWED_REASONS = {
    # A username is unique platform-wide and the clinic is the RESULT of the
    # lookup, so it cannot be a predicate of it.
    "global_auth_lookup",
    # Schedulers and reapers that iterate every tenant, taking the clinic from
    # each row.
    "platform_sweep",
    # Callbacks keyed on an identifier that is globally unique and issued by an
    # external provider (Meta wamid, Razorpay payment id).
    "meta_callback_by_unique_id",
    # Backfills and schema maintenance.
    "migration_backfill",
    # NOTE: "conditional_scope_below" was removed on 2026-09-01. It blessed
    # `if effective_clinic_id != "default": q = q.eq("clinic_id", ...)`, which
    # is precisely the shape that shipped a query with NO tenant predicate
    # whenever the scope was the sentinel — the cross-tenant incident. Apply
    # the predicate unconditionally; a bad scope must match zero rows.
    # Platform-owner endpoints that legitimately span tenants; these sit behind
    # the separate owner credential, not clinic-admin auth.
    "platform_admin",
    # The predicate is a globally unique primary key (a UUID) for a row this
    # code already holds, having fetched it under a scoped or sweep query.
    # Reaching another tenant's row would require guessing its UUID, and the
    # routes that accept an id from a caller re-check clinic ownership.
    "unique_row_key",
    # An INSERT carries its tenant in the row payload; there is no filter to
    # put a predicate on. Where clinic_id is set conditionally, it is because
    # the tenant is genuinely not yet resolved at that point (webhook ingest
    # before phone_number_id lookup), and the row is attributed later.
    "insert_scoped_by_payload",
}

# ── Ratchets ────────────────────────────────────────────────────────────────
# These may go DOWN, never up.
#
# 2026-08-31: app/services burned down from 86 to 0. Two thirds of that came
# from teaching the linter the conditional-scoping idiom it could not see
# (_scoped_by_later_reassignment); the rest were reviewed individually and
# carry a structural reason from ALLOWED_REASONS. Routers' free-text legacy
# annotations fell 88 -> 55 for the same analyzer reason.
#: Bare = a query on a tenant-owned table with no clinic_id predicate AND no
#: annotation. This must stay at zero; there is no legitimate reason to leave
#: one un-annotated.
TOTAL_BARE_BASELINE = 0
#: Free-text annotations predating ALLOWED_REASONS, held so the enum can be
#: adopted without a flag-day rewrite of every call site. May fall, never rise.
TOTAL_LEGACY_ANNOTATIONS = 54

# Scan the WHOLE application, not two subdirectories. app/database.py,
# app/main.py and connectors/ were never linted at all — connectors/runner.py
# alone holds 11 tenant-table queries that no guard had ever looked at.
SCANNED_DIRS = ("app", "connectors")


def _table_name(call):
    """Extract the literal table name from supabase.table("x")."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "table":
        return None
    if not isinstance(func.value, ast.Name) or func.value.id != "supabase":
        return None
    if call.args and isinstance(call.args[0], ast.Constant):
        return call.args[0].value
    return None


def _has_clinic_predicate(node) -> bool:
    """True if any .eq("clinic_id", ...) / .in_ / .match appears in the tree."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if not isinstance(func, ast.Attribute) or func.attr not in ("eq", "in_", "match"):
            continue
        for arg in sub.args:
            if isinstance(arg, ast.Constant) and arg.value == "clinic_id":
                return True
            if isinstance(arg, ast.Dict):
                for key in arg.keys:
                    if isinstance(key, ast.Constant) and key.value == "clinic_id":
                        return True
    return False


def _annotation_for(lines, lineno):
    """Reason text annotated on the call's line or the three above it."""
    for i in range(max(0, lineno - 4), lineno):
        if "# unscoped:" in lines[i]:
            return lines[i].split("# unscoped:", 1)[1].strip()
    return None


def _enclosing_function(tree, node):
    """Innermost FunctionDef containing node, if any."""
    best = None
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node in list(ast.walk(fn)):
            if best is None or fn.lineno >= best.lineno:
                best = fn
    return best


def _assign_target_names(stmt):
    """Names assigned by `stmt`, e.g. ['query'] for `query = supabase.table(..)`."""
    if not isinstance(stmt, ast.Assign):
        return []
    names = []
    for t in stmt.targets:
        if isinstance(t, ast.Name):
            names.append(t.id)
    return names


def _scoped_by_later_reassignment(tree, node):
    """True for the conditional-scoping idiom the AST cannot see in one statement:

        query = supabase.table("appointments").select("*")
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)

    The predicate is real, it just lands in a different statement. Matching is
    deliberately narrow on two axes:

    1. The SAME variable the query was assigned to must be reassigned from an
       expression carrying .eq("clinic_id", ...), inside the same function. A
       clinic predicate merely appearing somewhere else in the function does
       NOT count -- that would let one scoped query launder an unscoped
       sibling.

    2. The reassignment must be UNCONDITIONAL. The idiom shown above is the
       one that caused the 2026-09-01 cross-tenant incident: when clinic_id
       was the "default" sentinel the `if` did not fire, the query shipped
       with no WHERE clause, and a super_admin read -- and DELETED -- every
       tenant's doctors from one clinic's admin panel. This linter had been
       taught to treat that shape as scoped, so it reported zero violations
       throughout. A predicate that an `if` can skip is not a predicate;
       apply it unconditionally, and let a bad scope match zero rows.
    """
    fn = _enclosing_function(tree, node)
    if fn is None:
        return False

    target_names = []
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and node in list(ast.walk(stmt)):
            target_names.extend(_assign_target_names(stmt))
    if not target_names:
        return False

    # Statements that only execute when some condition holds. A predicate
    # applied in here is NOT a predicate — see the docstring below.
    # A `try:` BODY is not conditional (it always runs), so only the handlers
    # and else-clause count; likewise `finally` always runs.
    conditional = set()

    def _mark(stmts):
        for st in stmts:
            for sub in ast.walk(st):
                conditional.add(id(sub))

    for branch in ast.walk(fn):
        if isinstance(branch, ast.If):
            _mark(branch.body)
            _mark(branch.orelse)
        elif isinstance(branch, ast.While):
            _mark(branch.body)
            _mark(branch.orelse)
        elif isinstance(branch, ast.Try):
            _mark(branch.handlers)
            _mark(branch.orelse)

    for stmt in ast.walk(fn):
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(n in target_names for n in _assign_target_names(stmt)):
            continue
        if stmt.value is node or node in list(ast.walk(stmt.value)):
            continue  # the original assignment, not a re-scoping of it
        if id(stmt) in conditional:
            continue  # KRIYA-TENANT-001: a conditional predicate is no predicate
        if _has_clinic_predicate(stmt.value):
            return True
    return False


def _enclosing_statement(tree, node):
    """Largest enclosing statement, so the whole builder chain is visible."""
    best = None
    for stmt in ast.walk(tree):
        if not isinstance(stmt, (ast.Assign, ast.Expr, ast.Return, ast.If, ast.With)):
            continue
        if node in list(ast.walk(stmt)):
            if best is None or stmt.lineno >= best.lineno:
                best = stmt
    return best or node


def _scan(directory):
    """Return (bare, legacy_annotated, bad_reason) location lists."""
    bare, legacy, bad_reason = [], [], []
    root = pathlib.Path(directory)
    if not root.exists():
        return bare, legacy, bad_reason

    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            table = _table_name(node)
            if table is None or table not in TENANT_OWNED_TABLES:
                continue

            if _has_clinic_predicate(_enclosing_statement(tree, node)):
                continue

            if _scoped_by_later_reassignment(tree, node):
                continue

            location = f"{path}:{node.lineno}: supabase.table({table!r})"
            reason = _annotation_for(lines, node.lineno)
            if reason is None:
                bare.append(location)
            elif reason in ALLOWED_REASONS:
                continue
            else:
                legacy.append(f"{location} — {reason!r}")
                bad_reason.append(location)

    return bare, legacy, bad_reason


def _scan_all():
    """Scan every root in SCANNED_DIRS and merge the results.

    SCANNED_DIRS used to be declared and then ignored: the tests hardcoded
    "app/routers" and "app/services", so app/database.py, app/main.py and the
    whole connectors/ package were never linted by anything. That is a
    constant that looks like coverage without providing any.
    """
    bare, legacy, bad = [], [], []
    for d in SCANNED_DIRS:
        b, l, r = _scan(d)
        bare += b
        legacy += l
        bad += r
    return bare, legacy, bad


def test_no_unscoped_tenant_queries_anywhere():
    """Zero bare queries across the entire application. No exceptions.

    Every query on a tenant-owned table either carries a clinic_id predicate
    or says, on the line, which structural reason exempts it.
    """
    bare, _, _ = _scan_all()
    assert len(bare) <= TOTAL_BARE_BASELINE, (
        f"{len(bare)} query/queries on tenant-owned tables across "
        f"{SCANNED_DIRS} have no clinic_id predicate and no "
        f"'# unscoped: <reason>' annotation (baseline {TOTAL_BARE_BASELINE}). "
        f"Add the predicate, or an explicit reason from "
        f"{sorted(ALLOWED_REASONS)}:" + "".join(f"\n  - {v}" for v in bare)
    )


def test_ratchet_baselines_are_not_stale():
    """If violations were fixed, lower the baseline in the same commit.

    Without this the ratchet silently loosens: a later change could reintroduce
    exactly as many unscoped queries as were removed.
    """
    bare, legacy, _ = _scan_all()

    assert len(bare) == TOTAL_BARE_BASELINE, (
        f"bare count is {len(bare)}, baseline says {TOTAL_BARE_BASELINE} "
        f"— update TOTAL_BARE_BASELINE in the same commit"
    )
    assert len(legacy) <= TOTAL_LEGACY_ANNOTATIONS, (
        f"free-text '# unscoped:' annotations grew to {len(legacy)} "
        f"(baseline {TOTAL_LEGACY_ANNOTATIONS}). New annotations must use a "
        f"reason from {sorted(ALLOWED_REASONS)}."
    )


def test_a_bare_annotation_no_longer_satisfies_the_linter():
    """Guard the guard.

    The old linter passed on any line containing '# unscoped:'. That is exactly
    how KA-P1-05 shipped: the annotation claimed the query was tenant-scoped
    and nothing checked. A free-text reason must now be classified as legacy,
    never as compliant.
    """
    src = (
        "supabase = None\n"
        "def f(x):\n"
        "    # unscoped: tenant-scoped operation with verified clinic authorization\n"
        '    return supabase.table("doctors").select("*").eq("id", x).execute()\n'
    )
    tree = ast.parse(src)
    lines = src.splitlines()
    call = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _table_name(n) == "doctors"
    )

    assert not _has_clinic_predicate(_enclosing_statement(tree, call)), (
        "the predicate detector reported clinic scoping on a query that only "
        "filters by id"
    )
    reason = _annotation_for(lines, call.lineno)
    assert reason is not None
    assert reason not in ALLOWED_REASONS, (
        "a free-text annotation was accepted as compliant — the linter is a "
        "rubber stamp again"
    )


def test_predicate_detector_recognises_a_scoped_query():
    """The converse: a genuinely scoped query must not be reported."""
    src = (
        "supabase = None\n"
        "def f(cid, x):\n"
        '    return supabase.table("doctors").select("*").eq("clinic_id", cid)'
        '.eq("id", x).execute()\n'
    )
    tree = ast.parse(src)
    call = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _table_name(n) == "doctors"
    )
    assert _has_clinic_predicate(_enclosing_statement(tree, call))
