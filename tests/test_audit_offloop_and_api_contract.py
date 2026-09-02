"""Regression guards for the 2026-09-02 forensic production audit.

Two defect classes were found and fixed:

DOMAIN 5 — event-loop starvation. Five sync helpers performed blocking
PostgREST round-trips and were invoked from 38 async call sites. The worst,
_log_payment_event, sits on the Razorpay webhook path: every payment event
froze the worker's whole event loop for a network round-trip. The ratchet in
tests/test_no_blocking_db_calls.py had grandfathered them at 6; they are now 0
and these tests keep them there.

DOMAIN 3 — the admin panel's shared fetch helpers. api() discarded the
server's explanation and threw a bare status code; every helper stringified a
FastAPI 422 `detail` list to "[object Object]"; and nothing handled 401, so an
expired session left the panel open and silently dead on every click.
"""

import ast
import inspect
import pathlib
import re

import pytest

ADMIN_HTML = pathlib.Path("admin/index.html")


def _html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


# ── Domain 5: the converted helpers are coroutines and go through sb() ───────

def _payment_service_method(name):
    from app.services.payment import PaymentService

    return getattr(PaymentService, name)


@pytest.mark.parametrize(
    "name", ["_log_payment_event", "_log_payment_event_raw"]
)
def test_payment_audit_writers_are_offloaded(name):
    """Both sit on the Razorpay webhook path — 25 call sites between them."""
    fn = _payment_service_method(name)
    assert inspect.iscoroutinefunction(fn), f"{name} must be async"
    src = inspect.getsource(fn)
    assert "await sb(" in src, f"{name} must execute off the loop"
    assert ".execute()" not in src, f"{name} still blocks the loop"


def test_burn_followup_is_offloaded():
    from app.services.scheduler import SchedulerService

    fn = SchedulerService._burn_followup
    assert inspect.iscoroutinefunction(fn)
    src = inspect.getsource(fn)
    assert "await sb(" in src and ".execute()" not in src


def test_resolve_owned_branch_is_offloaded():
    from app.services.permissions import resolve_owned_branch

    assert inspect.iscoroutinefunction(resolve_owned_branch)
    src = inspect.getsource(resolve_owned_branch)
    assert "await sb(" in src and ".execute()" not in src


def test_every_caller_awaits_the_converted_helpers():
    """A missed await is silent: the coroutine never runs, so the audit row is
    simply never written and nothing raises."""
    names = (
        "_log_payment_event_raw",
        "_log_payment_event",
        "_burn_followup",
        "resolve_owned_branch",
    )
    for path in (
        pathlib.Path("app/services/payment.py"),
        pathlib.Path("app/services/scheduler.py"),
        pathlib.Path("app/routers/admin.py"),
    ):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            called = (
                f.attr if isinstance(f, ast.Attribute)
                else f.id if isinstance(f, ast.Name)
                else None
            )
            if called not in names:
                continue
            line = lines[node.lineno - 1]
            col = node.col_offset
            prefix = line[:col]
            assert prefix.rstrip().endswith("await") or "await " in prefix, (
                f"{path}:{node.lineno} calls {called}() without await — "
                f"the coroutine would never run:\n    {line.strip()}"
            )


def test_no_new_sync_db_helper_is_introduced():
    """Reachability guard behind the ratchet: a sync function doing DB I/O
    stalls the loop the moment any async caller invokes it."""
    sync_db_fns = set()
    for path in pathlib.Path("app").rglob("*.py"):
        if "integrations" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        class V(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_AsyncFunctionDef(self, n):
                self.stack.append(("async", n))
                self.generic_visit(n)
                self.stack.pop()

            def visit_FunctionDef(self, n):
                self.stack.append(("sync", n))
                self.generic_visit(n)
                self.stack.pop()

            def visit_Call(self, n):
                f = n.func
                if isinstance(f, ast.Attribute) and f.attr == "execute" and not n.args:
                    if self.stack and self.stack[-1][0] == "sync":
                        sync_db_fns.add(self.stack[-1][1].name)
                self.generic_visit(n)

        V().visit(tree)

    # Deliberately sync: each is dispatched INTO an executor, or runs in a
    # sync FastAPI dependency that Starlette already threads.
    allowed = {
        "execute",            # tenant_scoped_client passthrough
        "_sync_fetch_holiday",
        "_sync_fetch_leave",
        "_sync_fetch_booked",
        "is_rate_limited",
        "check_and_record",
        "record_attempt",
        "remaining_attempts",
        "reset",
        "_insert",
    }
    leaked = sync_db_fns - allowed
    assert not leaked, (
        f"New sync DB helper(s) {sorted(leaked)}: if async code calls these, "
        f"the event loop stalls for a network round-trip"
    )


# ── Domain 3: the panel's shared fetch helpers ───────────────────────────────

def test_all_api_helpers_share_one_error_path():
    html = _html()
    assert "async function apiFail(r)" in html
    assert html.count("await apiFail(r)") == 4, (
        "api, apiPost, apiPut and apiDel must all funnel failures through apiFail"
    )
    assert "throw new Error(r.status)" not in html, (
        "A bare status code discards the server's explanation"
    )


def _api_fail_body() -> str:
    m = re.search(r"async function apiFail\(r\) \{(.*?)\n\}", _html(), re.S)
    assert m, "apiFail not found"
    return m.group(1)


def test_expired_session_returns_the_user_to_login():
    """Sessions expire (migration 067). Before this, a 401 left the panel open
    and every later click failed with no explanation."""
    body = _api_fail_body()
    assert "r.status === 401" in body
    assert "forceRelogin(" in body


def test_validation_errors_are_rendered_as_text_not_object_object():
    """FastAPI sends a 422 `detail` as a list of objects; detailText() flattens
    it. The helpers used raw data.detail and rendered '[object Object]'."""
    assert "detailText(data)" in _api_fail_body()


def test_helpers_apifail_depends_on_still_exist():
    html = _html()
    assert "function detailText(data)" in html
    assert "function forceRelogin(message)" in html
