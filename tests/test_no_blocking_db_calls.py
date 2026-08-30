"""RT-09 — no blocking PostgREST call may run on the event loop (KA-P1-03 / T5.1).

supabase-py 2.x's `create_client()` returns the SYNCHRONOUS client, so every
`.execute()` is a blocking httpx request. Calling one directly inside an
`async def` freezes the whole event loop for the duration of the round-trip:
within each of the four production processes (2 Render instances x 2 uvicorn
workers) FastAPI's concurrency was nullified and requests were served strictly
one at a time.

407 call sites were converted to `await sb(builder)`
(app/database.py:sb -> asyncio.to_thread). This test is the ratchet that keeps
them converted.

It is a STATIC check on purpose. No test in this suite blocks on real I/O —
every one of them mocks Supabase and returns instantly — so a reintroduced
blocking call is invisible to the entire behavioural suite. That is exactly why
the defect survived to production in the first place.
"""

import ast
import pathlib

import pytest

SCANNED_DIRS = ("app", "connectors")

# Sync functions that still call .execute() directly from async callers.
#
# Was 15. The PersistentRateLimiter methods (app/utils/security.py) accounted
# for 9 of them and ran on EVERY admin, platform and integration request; their
# call sites are now wrapped in asyncio.to_thread, so they no longer occupy the
# event loop. That is why _all_offloaded_names() has to look tree-wide: the
# methods are defined in one module and offloaded in four others.
#
# Remaining, ranked by how often they run:
#   app/services/payment.py     — _log_payment_event / _log_payment_event_raw,
#                                 audit-ledger writes on the payment webhook path
#   app/services/permissions.py — resolve_owned_branch, per branch-scoped request
#   app/services/scheduler.py   — _burn_followup, per follow-up row (off the
#                                 request path entirely)
#
# These are lower-frequency than the limiter and each needs its callers changed,
# so they are held rather than rushed. This number may go DOWN, never up.
KNOWN_SYNC_BLOCKING = 6


def _parent_map(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(node, parents):
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return cur
        cur = parents.get(cur)
    return None


def _offloaded_function_names(tree):
    """Names of functions handed to to_thread / run_in_executor in this module.

    The common pattern is a nested sync helper defined and then passed by
    reference:

        def _insert():
            return supabase.table(...).insert(...).execute()
        await asyncio.to_thread(_insert)

    Its .execute() is NOT lexically inside the to_thread call, so a purely
    structural check would score it as blocking. It is not — it runs on a
    worker thread.
    """
    names = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("to_thread", "run_in_executor")
        ):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name):
                names.add(arg.id)
            elif isinstance(arg, ast.Attribute):
                names.add(arg.attr)
    return names


def _inside_thread_offload(node, parents, offloaded_names):
    """True if this call already runs off the event loop."""
    cur = parents.get(node)
    while cur is not None:
        # Lexically inside the offload call: to_thread(lambda: q.execute())
        if (
            isinstance(cur, ast.Call)
            and isinstance(cur.func, ast.Attribute)
            and cur.func.attr in ("to_thread", "run_in_executor")
        ):
            return True
        # Inside a named helper that is passed to one.
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name in offloaded_names
        cur = parents.get(cur)
    return False


def _all_offloaded_names():
    """Every function name handed to to_thread / run_in_executor, tree-wide.

    Collected across ALL scanned files, not per file: the offload happens at
    the call site while the function is usually defined elsewhere. The rate
    limiter is the case that matters — PersistentRateLimiter's methods live in
    app/utils/security.py, and the wrapping happens in app/routers/admin.py,
    app/routers/platform.py, app/services/payment.py and the CallMedex router.
    A per-file scan scores those methods as blocking when they are not.

    Slightly over-approximate by name: a method offloaded anywhere counts as
    offloaded everywhere. Accepted deliberately — the alternative is call-graph
    resolution, and the ratchet below is the real guard against drift.
    """
    names = set()
    for directory in SCANNED_DIRS:
        root = pathlib.Path(directory)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "/tests/" in path.as_posix():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            names |= _offloaded_function_names(tree)
    return names


def _execute_calls():
    """Yield (path, lineno, is_async_context, is_offloaded) for each .execute()."""
    global_offloaded = _all_offloaded_names()
    for directory in SCANNED_DIRS:
        root = pathlib.Path(directory)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "/tests/" in path.as_posix():
                continue
            source = path.read_text(encoding="utf-8")
            if ".execute()" not in source:
                continue
            tree = ast.parse(source, filename=str(path))
            parents = _parent_map(tree)
            offloaded_names = _offloaded_function_names(tree) | global_offloaded
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"
                ):
                    continue
                if node.args or node.keywords:
                    continue  # psycopg2-style cur.execute(sql), not PostgREST
                fn = _enclosing_function(node, parents)
                yield (
                    path,
                    node.lineno,
                    isinstance(fn, ast.AsyncFunctionDef),
                    _inside_thread_offload(node, parents, offloaded_names),
                )


def test_rt09_no_blocking_execute_inside_async_def():
    """The hard rule: nothing blocking may sit directly on the event loop."""
    offenders = [
        f"{path}:{lineno}"
        for path, lineno, is_async, offloaded in _execute_calls()
        if is_async and not offloaded
    ]

    assert not offenders, (
        f"{len(offenders)} blocking .execute() call(s) run directly on the event "
        f"loop. Wrap the query with `await sb(...)` from app.database:\n"
        f"    res = await sb(supabase.table('x').select('*').eq('clinic_id', c))\n"
        + "\n".join(f"  - {o}" for o in offenders)
    )


def test_rt09b_sync_blocking_callers_do_not_grow():
    """Ratchet on the sync functions that still block.

    These cannot be fixed by wrapping alone — each needs to become async and
    have its callers updated — so they are grandfathered at their current count.
    """
    remaining = [
        f"{path}:{lineno}"
        for path, lineno, is_async, offloaded in _execute_calls()
        if not is_async and not offloaded
    ]

    assert len(remaining) <= KNOWN_SYNC_BLOCKING, (
        f"{len(remaining)} blocking .execute() calls in sync functions "
        f"(ratchet {KNOWN_SYNC_BLOCKING}). A NEW one adds an event-loop stall "
        f"whenever an async caller invokes it:\n"
        + "\n".join(f"  - {r}" for r in remaining)
    )


def test_rt09c_ratchet_is_not_stale():
    """If sync blockers were converted, lower the ratchet in the same commit."""
    remaining = [
        1
        for _, _, is_async, offloaded in _execute_calls()
        if not is_async and not offloaded
    ]
    assert len(remaining) == KNOWN_SYNC_BLOCKING, (
        f"sync blocking count is {len(remaining)}, ratchet says "
        f"{KNOWN_SYNC_BLOCKING} — update KNOWN_SYNC_BLOCKING"
    )


@pytest.mark.asyncio
async def test_sb_helper_runs_off_the_event_loop():
    """sb() must actually leave the loop thread, not just look asynchronous."""
    import threading

    from app.database import sb

    loop_thread = threading.get_ident()
    seen = {}

    class FakeBuilder:
        def execute(self):
            seen["thread"] = threading.get_ident()
            return "result"

    result = await sb(FakeBuilder())

    assert result == "result", "sb() must return the builder's result unchanged"
    assert seen["thread"] != loop_thread, (
        "sb() executed the query on the event loop thread — the whole point is "
        "that it does not"
    )


@pytest.mark.asyncio
async def test_sb_propagates_exceptions_unchanged():
    """Error semantics must be identical to a direct .execute().

    Every call site's error handling — the 23505 duplicate detection in
    book_appointment, is_slot_conflict, the fail-closed branches in
    message_queue — depends on the original exception arriving intact.
    """
    from app.database import sb

    class Boom(Exception):
        pass

    class FailingBuilder:
        def execute(self):
            raise Boom("duplicate key value violates unique constraint")

    with pytest.raises(Boom, match="duplicate key value"):
        await sb(FailingBuilder())
