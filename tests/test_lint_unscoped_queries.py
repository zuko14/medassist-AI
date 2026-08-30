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

Most of those 86 are probably fine (message_queue keys on a globally unique
wamid; lab_reports keys on a unique external_report_id), but none has been
reviewed. Failing on all 86 today would block every unrelated change, so they
are held by a RATCHET: the count may not grow. New unscoped queries fail
immediately; the existing ones are a burn-down list. Lower SERVICES_BARE_BASELINE
as they are reviewed — the test fails if you lower it too far, so the number
cannot drift back up.
"""

import ast
import pathlib

import pytest

# Tables that carry a clinic_id column and therefore need a tenant predicate.
# Mirrors TENANT_OWNED_TABLES in app/services/tenant_scoped_client.py.
TENANT_OWNED_TABLES = {
    "appointments",
    "patients",
    "lab_reports",
    "lab_tests",
    "doctors",
    "branches",
    "doctor_leaves",
    "hospital_holidays",
    "clinic_admins",
    "integration_connectors",
    "connector_failed_reports",
    "conversations",
    "inbound_messages",
    "processed_messages",
    "prescriptions",
    "family_members",
    "payment_events",
}

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
    # The predicate is applied conditionally just below, typically
    # `if effective_clinic_id != "default"`.
    "conditional_scope_below",
    # Platform-owner endpoints that legitimately span tenants; these sit behind
    # the separate owner credential, not clinic-admin auth.
    "platform_admin",
}

# ── Ratchets ────────────────────────────────────────────────────────────────
# Measured on the tree at the time of the 2026-08-30 audit remediation.
# These may go DOWN, never up.
ROUTERS_BARE_BASELINE = 0     # no predicate, no annotation
SERVICES_BARE_BASELINE = 86
# Free-text annotations predating ALLOWED_REASONS, held so the enum can be
# adopted without a flag-day rewrite of 88 call sites.
ROUTERS_LEGACY_ANNOTATIONS = 88
SERVICES_LEGACY_ANNOTATIONS = 1

SCANNED_DIRS = ("app/routers", "app/services")


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


def test_no_new_unscoped_router_queries():
    """Routers are at zero bare queries and must stay there."""
    bare, _, _ = _scan("app/routers")
    assert len(bare) <= ROUTERS_BARE_BASELINE, (
        f"{len(bare)} query/queries on tenant-owned tables in app/routers/** "
        f"have no clinic_id predicate and no '# unscoped: <reason>' "
        f"annotation (baseline {ROUTERS_BARE_BASELINE}):\n"
        + "\n".join(f"  - {v}" for v in bare)
    )


def test_unscoped_service_queries_do_not_grow():
    """Ratchet on app/services/**, which the old linter never scanned.

    86 pre-existing unscoped queries are grandfathered. Adding an 87th fails.
    """
    bare, _, _ = _scan("app/services")
    assert len(bare) <= SERVICES_BARE_BASELINE, (
        f"{len(bare)} unscoped tenant-table queries in app/services/** — the "
        f"ratchet is {SERVICES_BARE_BASELINE}. A NEW query on a tenant-owned "
        f"table needs either .eq('clinic_id', ...) or an explicit "
        f"'# unscoped: <reason>' drawn from {sorted(ALLOWED_REASONS)}:\n"
        + "\n".join(f"  - {v}" for v in bare)
    )


def test_ratchet_baselines_are_not_stale():
    """If violations were fixed, lower the baseline in the same commit.

    Without this the ratchet silently loosens: a later change could reintroduce
    exactly as many unscoped queries as were removed.
    """
    routers_bare, routers_legacy, _ = _scan("app/routers")
    services_bare, services_legacy, _ = _scan("app/services")

    assert len(routers_bare) == ROUTERS_BARE_BASELINE, (
        f"app/routers bare count is {len(routers_bare)}, baseline says "
        f"{ROUTERS_BARE_BASELINE} — update ROUTERS_BARE_BASELINE"
    )
    assert len(services_bare) == SERVICES_BARE_BASELINE, (
        f"app/services bare count is {len(services_bare)}, baseline says "
        f"{SERVICES_BARE_BASELINE} — update SERVICES_BARE_BASELINE"
    )
    assert len(routers_legacy) <= ROUTERS_LEGACY_ANNOTATIONS, (
        f"free-text '# unscoped:' annotations in app/routers grew to "
        f"{len(routers_legacy)} (baseline {ROUTERS_LEGACY_ANNOTATIONS}). New "
        f"annotations must use a reason from {sorted(ALLOWED_REASONS)}."
    )
    assert len(services_legacy) <= SERVICES_LEGACY_ANNOTATIONS, (
        f"free-text '# unscoped:' annotations in app/services grew to "
        f"{len(services_legacy)} (baseline {SERVICES_LEGACY_ANNOTATIONS})."
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
