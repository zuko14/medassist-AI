"""Regression guard for KRIYA-TENANT-001 (production incident, 2026-09-01).

What happened
-------------
A super_admin opened the admin panel believing it showed a single test clinic.
The panel never sent ?clinic_id, so the server resolved the scope to the string
"default". Every /admin query then did:

    query = supabase.table("doctors").select("*")
    if effective_clinic_id != "default":
        query = query.eq("clinic_id", effective_clinic_id)

so "default" meant NO WHERE CLAUSE. The doctor list showed every tenant's
doctors at once; deleting the ones that "didn't belong" issued
DELETE ... WHERE id = ? with no tenant predicate and destroyed a live
customer's roster, which then vanished from that customer's WhatsApp bot.
The branch dropdown leaked the same way.

The invariant these tests pin
-----------------------------
/admin is a SINGLE-TENANT surface. enforce_clinic_access() resolves exactly one
real clinic or raises. It must never return "default", "", or None, because
every caller interpolates its result straight into a tenant predicate.
Cross-tenant work belongs to /platform, behind separate credentials.
"""

import ast
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.database import is_valid_clinic_scope
from app.routers.admin import (
    AdminUser,
    delete_doctor,
    enforce_clinic_access,
    get_branches,
    get_doctors,
    resolve_clinic_id_for_write,
)
from app.services.permissions import resolve_owned_branch

CLINIC_A = "11111111-1111-1111-1111-111111111111"
CLINIC_B = "22222222-2222-2222-2222-222222222222"

SUPER = AdminUser("kriyaai_superadmin", role="super_admin", clinic_id=None, user_id="env")
ADMIN_A = AdminUser("admin_a", role="clinic_admin", clinic_id=CLINIC_A, user_id="ua")


def _chain_mock(rows):
    """Self-chaining Supabase builder mock that records every .eq() applied."""
    calls = []
    chain = MagicMock()

    def _eq(col, val):
        calls.append((col, val))
        return chain

    chain.eq.side_effect = _eq
    for m in ("select", "delete", "update", "insert", "order", "limit", "in_", "is_", "neq"):
        getattr(chain, m).return_value = chain
    chain.execute.return_value = MagicMock(data=rows)

    sb = MagicMock()
    sb.table.return_value = chain
    return sb, calls


# ── The resolver itself ────────────────────────────────────────────────────


def test_unscoped_super_admin_is_refused_not_widened():
    """The exact call that caused the incident. Was: returns "default"."""
    with pytest.raises(HTTPException) as exc:
        enforce_clinic_access(SUPER, "default")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("sentinel", ["default", "", "none", "null", "  "])
def test_no_sentinel_ever_resolves_to_a_scope(sentinel):
    with pytest.raises(HTTPException):
        enforce_clinic_access(SUPER, sentinel)


def test_enforce_never_returns_an_unscoped_value():
    """Whatever comes back must be safe to put in .eq("clinic_id", ...)."""
    assert is_valid_clinic_scope(enforce_clinic_access(ADMIN_A, "default"))
    assert is_valid_clinic_scope(enforce_clinic_access(ADMIN_A, CLINIC_A))
    assert is_valid_clinic_scope(enforce_clinic_access(SUPER, CLINIC_B))


def test_clinic_admin_still_resolves_and_is_still_fenced():
    assert enforce_clinic_access(ADMIN_A, "default") == CLINIC_A
    with pytest.raises(HTTPException) as exc:
        enforce_clinic_access(ADMIN_A, CLINIC_B)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_write_resolver_does_not_guess_a_clinic():
    """It used to fall back to "the oldest clinic on the platform", writing one
    tenant's new doctors into another tenant."""
    with pytest.raises(HTTPException) as exc:
        await resolve_clinic_id_for_write(SUPER, "default")
    assert exc.value.status_code == 400


# ── The routes from the incident report ────────────────────────────────────


@pytest.mark.asyncio
async def test_doctor_list_is_refused_rather_than_spanning_tenants():
    """Symptom 1: the test clinic's panel listed the other clinic's doctors."""
    sb, _ = _chain_mock([])
    with patch("app.routers.admin.supabase", sb):
        with pytest.raises(HTTPException) as exc:
            await get_doctors(clinic_id="default", user=SUPER)
    assert exc.value.status_code == 400
    sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_branch_list_is_refused_rather_than_spanning_tenants():
    """Symptom 2: adding a doctor in the test panel offered another clinic's branches."""
    sb, _ = _chain_mock([])
    with patch("app.routers.admin.supabase", sb):
        with pytest.raises(HTTPException) as exc:
            await get_branches(clinic_id="default", user=SUPER)
    assert exc.value.status_code == 400
    sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_doctor_delete_is_refused_without_a_clinic():
    """Symptom 3, the destructive one: DELETE by id with no tenant predicate."""
    sb, _ = _chain_mock([{}])
    with patch("app.routers.admin.supabase", sb):
        with pytest.raises(HTTPException) as exc:
            await delete_doctor(
                "doc-belonging-to-another-clinic", clinic_id="default", user=SUPER
            )
    assert exc.value.status_code == 400
    sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_doctor_delete_always_carries_a_clinic_predicate():
    """Even when scoped, the DELETE must be fenced by clinic_id, not id alone."""
    sb, calls = _chain_mock([{}])
    with patch("app.routers.admin.supabase", sb), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock), \
         patch("app.routers.admin.invalidate_doctor_cache"):
        await delete_doctor("doc-1", clinic_id=CLINIC_B, user=SUPER)

    assert ("clinic_id", CLINIC_B) in calls, (
        f"DELETE ran without a tenant predicate; predicates applied: {calls}"
    )


def test_super_admin_cannot_reach_another_tenants_branch():
    """resolve_owned_branch() used to skip its IDOR check for super_admin."""
    sb, _ = _chain_mock([{"id": "br-1", "clinic_id": CLINIC_B}])
    with patch("app.database.supabase", sb):
        with pytest.raises(HTTPException) as exc:
            resolve_owned_branch(SUPER, "br-1", CLINIC_A)
    assert exc.value.status_code == 404


# ── The idiom itself must stay gone ────────────────────────────────────────

#: The only two structural reasons a query may legitimately widen past one
#: tenant. Both are entry points where the tenant is genuinely not known yet,
#: and both must say so on the line, so `grep "unscoped:"` finds every one.
WIDENING_REASONS = (
    # A nightly job that aggregates across every tenant for the platform
    # operator; it takes the clinic from each row rather than filtering by one.
    "platform_sweep",
    # A provider callback keyed on a globally unique id the provider issued
    # (Razorpay payment id, Meta wamid) before the tenant has been resolved.
    "meta_callback_by_unique_id",
)


def _annotated_widening(lines, lineno, window=12):
    """True if a reviewed `# unscoped: <reason>` annotation covers this site."""
    for i in range(max(0, lineno - window), min(len(lines), lineno)):
        if "# unscoped:" in lines[i]:
            reason = lines[i].split("# unscoped:", 1)[1].strip()
            if reason in WIDENING_REASONS:
                return True
    return False



def test_no_conditional_tenant_predicate_remains_in_the_codebase():
    """`if clinic != "default": q = q.eq("clinic_id", clinic)` is fail-OPEN.

    Apply the predicate unconditionally instead: a bad scope must then match
    zero rows rather than every row.

    The signature of the bug is that the `if` guards on the SCOPE VALUE itself
    ("do I have a clinic?"), so when the answer is no the predicate is dropped
    and the query widens to every tenant. A clinic predicate applied inside an
    `if` that tests something else (`if body.branch_id:`, `if active_only:`) is
    fine — it is unconditional within its branch, and that is why this check
    compares the guard against the value being filtered on.
    """
    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            guard = ast.unparse(node.test)
            for stmt in ast.walk(node):
                if not isinstance(stmt, ast.Assign):
                    continue
                for sub in ast.walk(stmt.value):
                    if not (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in ("eq", "in_")
                        and len(sub.args) == 2
                        and isinstance(sub.args[0], ast.Constant)
                        and sub.args[0].value == "clinic_id"
                    ):
                        continue
                    value = ast.unparse(sub.args[1])
                    # Does the guard test the very value being filtered on?
                    if not re.search(r"(?<![\w.])" + re.escape(value) + r"(?![\w])", guard):
                        continue
                    if _annotated_widening(lines, stmt.lineno):
                        continue
                    offenders.append(f"{path}:{stmt.lineno} -> if {guard}")
    assert not offenders, (
        "Tenant predicate guarded by a test on the scope value itself. That is "
        "fail-open, and is what caused KRIYA-TENANT-001 -- when the guard is "
        "false the query loses its tenant filter entirely. Apply the predicate "
        "unconditionally: " + "; ".join(offenders)
    )
