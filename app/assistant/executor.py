"""Kriya Ketu executor: dispatch EXISTING /admin endpoints in-process, verify by read-back (ADR-004).

The real route runs exactly as for a click in the panel (verify_credentials,
OPD/corporate/phlebotomist confinement, require_permission, enforce_clinic_access,
validation, the endpoint's own audit row), because the request carries only the
caller's own session cookie or Authorization header.

Rules that never bend:
- one dispatch per write, never retried; a timeout or 5xx on a write is
  UNKNOWN_OUTCOME and is settled by a read-back (reverify), not by writing again;
- a timeout never cancels the endpoint: it keeps running to completion (so its
  audit row is written) and we stop waiting for it;
- the endpoint's own error text is never forwarded; status codes map to fixed messages;
- response bodies are never stored in assistant_runs.steps (PHI); only evidence lines.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.assistant import store
from app.assistant.registry import prohibited_path
from app.utils.async_tasks import spawn_background_task
from app.utils.correlation import get_correlation_id

logger = logging.getLogger(__name__)

DISPATCH_TIMEOUT = 20.0
MAX_REVERIFY = 3
SESSION_COOKIE = "kriya_admin_session"  # app.routers.admin.ADMIN_SESSION_COOKIE (not imported: avoids a cycle)

_FIXED = {
    "invalid": "Kriya couldn't do that: some details were not accepted.",
    "session_expired": "Your session has ended. Please sign in again.",
    "forbidden": "You don't have permission to do that.",
    "not_found": "That record was not found. It may have been changed or removed.",
    "conflict": "That was already handled, or it changed since you looked.",
    "too_large": "That file is too large.",
    "rate_limited": "Too many requests. Please wait a moment.",
    "failed": "Kriya couldn't do that.",
    "server_error": "The server had a problem. Kriya will check whether it went through.",
    "timeout": "This is taking longer than expected. Kriya will check whether it went through.",
    "unavailable": "Kriya couldn't reach the service. Please try again.",
}
_BY_STATUS = {400: "invalid", 401: "session_expired", 403: "forbidden", 404: "not_found", 409: "conflict",
              413: "too_large", 422: "invalid", 429: "rate_limited"}


@dataclass(frozen=True)
class CallerAuth:
    """The caller's own credentials, forwarded unchanged. Nothing else is."""
    cookie: Optional[str]
    authorization: Optional[str]

    @classmethod
    def from_request(cls, request) -> "CallerAuth":
        return cls(cookie=request.cookies.get(SESSION_COOKIE), authorization=request.headers.get("authorization"))


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    query: dict = field(default_factory=dict)
    json: Optional[dict] = None


@dataclass(frozen=True)
class Outcome:
    status: str                 # "ok" | "failed" | "unknown"
    http_status: Optional[int]
    error_code: Optional[str]
    message: str
    data: Any = None


Read = Callable[[Call], Awaitable[Outcome]]
#: verify(read, write_response_or_None, call) -> (True written / False not written / None can't tell, evidence)
Verify = Callable[[Read, Any, Call], Awaitable[tuple[Optional[bool], str]]]


@dataclass(frozen=True)
class Step:
    label: str
    call: Call
    verify: Optional[Verify] = None   # required for writes
    optional: bool = False            # reads only: a failure is recorded and the run goes on (a briefing section)


def _fail(code: str, http_status: Optional[int], is_write: bool) -> Outcome:
    # A read can't have changed anything, so it is never UNKNOWN.
    uncertain = is_write and code in ("server_error", "timeout", "unavailable")
    return Outcome("unknown" if uncertain else "failed", http_status, code, _FIXED[code])


async def dispatch(call: Call, auth: CallerAuth, clinic_id: str, run_id: str, *, app=None,
                   timeout: float = DISPATCH_TIMEOUT) -> Outcome:
    """Send one request to the app in-process as the caller. Never retries."""
    if prohibited_path(call.path):
        raise ValueError(f"refusing to dispatch {call.path}")
    if app is None:
        from app.main import app  # lazy: app.main imports the assistant router
    headers = {"X-Correlation-ID": get_correlation_id(), "X-Kriya-Assistant-Run": run_id}
    if auth.cookie:
        headers["Cookie"] = f"{SESSION_COOKIE}={auth.cookie}"
    if auth.authorization:
        headers["Authorization"] = auth.authorization
    params = {**call.query, "clinic_id": clinic_id}  # the session's scope always wins
    is_write = call.method.upper() != "GET"

    async def _send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://kriya.internal") as client:
            return await client.request(call.method, call.path, params=params, json=call.json, headers=headers)

    task = spawn_background_task(_send(), name=f"assistant-dispatch-{run_id}")
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        logger.warning(f"ASSISTANT_DISPATCH_TIMEOUT run={run_id} {call.method} {call.path}")
        return _fail("timeout", None, is_write)
    try:
        resp = task.result()
    except Exception as e:
        logger.error(f"ASSISTANT_DISPATCH_ERROR run={run_id} {call.method} {call.path}: {type(e).__name__}")
        return _fail("unavailable", None, is_write)

    if resp.is_success:
        try:
            data = resp.json() if resp.content else None
        except ValueError:
            data = None
        return Outcome("ok", resp.status_code, None, "", data)
    if resp.status_code >= 500:
        return _fail("server_error", resp.status_code, is_write)
    return _fail(_BY_STATUS.get(resp.status_code, "failed"), resp.status_code, is_write)


def _record(step: Step) -> dict:
    return {"label": step.label, "method": step.call.method, "path": step.call.path, "status": "pending",
            "http_status": None, "error_code": None, "evidence": None, "reverify_attempts": 0}


def _stopped(records: list, failed_state: str) -> str:
    """Final state when a step did not complete: earlier writes stay in effect."""
    return "PARTIALLY_COMPLETED" if any(r["status"] == "done" for r in records) else failed_state


async def _finish(run: dict, from_states: tuple, state: str, records: list, error_code: Optional[str]) -> dict:
    for r in records:
        if r["status"] == "pending":
            r["status"] = "skipped"
    row = await store.transition(run["clinic_id"], run["username"], run["id"], from_states, state,
                                 steps=records, error_code=error_code)
    logger.info(f"ASSISTANT_RUN run={run['id']} state={state} error={error_code}")
    return row or await store.get_run(run["clinic_id"], run["username"], run["id"]) or run


async def execute(run: dict, steps: list[Step], auth: CallerAuth, *, app=None,
                  timeout: float = DISPATCH_TIMEOUT) -> tuple[dict, list[Outcome]]:
    """Run the steps of a run already claimed for EXECUTING (by CAS). Stops at the first
    step that fails or can't be confirmed. Returns (final row, outcomes so far)."""
    if run.get("state") != "EXECUTING":
        raise ValueError("run must be claimed (EXECUTING) before execution")
    if any(s.call.method.upper() != "GET" and s.verify is None for s in steps):
        raise ValueError("every write step needs a read-back verify")
    if any(s.optional and s.call.method.upper() != "GET" for s in steps):
        raise ValueError("only a read can be optional")
    cid, user, rid = run["clinic_id"], run["username"], run["id"]

    async def read(call: Call) -> Outcome:
        if call.method.upper() != "GET":
            raise ValueError("verification must only read")
        return await dispatch(call, auth, cid, rid, app=app, timeout=timeout)

    records = [_record(s) for s in steps]
    outcomes: list[Outcome] = []
    state, error_code = "COMPLETED_VERIFIED", None
    for step, rec in zip(steps, records):
        out = await dispatch(step.call, auth, cid, rid, app=app, timeout=timeout)
        outcomes.append(out)
        rec.update(http_status=out.http_status, error_code=out.error_code)
        if out.status == "failed" and step.optional:  # a read changed nothing; the presenter shows the gap
            rec["status"] = "skipped"
            continue
        if out.status == "failed":
            rec["status"], error_code = "failed", out.error_code
            state = _stopped(records, "FAILED")
            break
        if out.status == "unknown":
            rec["status"], state, error_code = "unknown", "UNKNOWN_OUTCOME", out.error_code
            break
        if step.verify is not None:
            ok, rec["evidence"] = await step.verify(read, out.data, step.call)
            if ok is not True:
                rec["status"], state = "unknown", "UNKNOWN_OUTCOME"
                error_code = "verify_mismatch" if ok is False else "verify_unavailable"
                break
        rec["status"] = "done"
        # Progress for the UI; also keeps updated_at fresh so the 2-minute sweep never fires mid-playbook.
        if not await store.transition(cid, user, rid, ("EXECUTING",), "EXECUTING", steps=records):
            state, error_code = _stopped(records, "FAILED"), "interrupted"
            break
    return await _finish(run, ("EXECUTING", "UNKNOWN_OUTCOME"), state, records, error_code), outcomes


async def reverify(run: dict, steps: list[Step], auth: CallerAuth, *, app=None,
                   timeout: float = DISPATCH_TIMEOUT) -> dict:
    """One read-back attempt for the step whose outcome is unknown. Never writes."""
    if run.get("state") != "UNKNOWN_OUTCOME":
        raise ValueError("only an UNKNOWN_OUTCOME run can be re-verified")
    cid, rid = run["clinic_id"], run["id"]
    records = [dict(r) for r in run.get("steps") or []] or [_record(s) for s in steps]
    # The unknown step, or the one in flight when the sweep fired (saved as "pending").
    idx = next((i for i, r in enumerate(records) if r["status"] == "unknown"),
               next((i for i, r in enumerate(records) if r["status"] == "pending"), None))
    if idx is None:
        return await _finish(run, ("UNKNOWN_OUTCOME",), "NEEDS_HUMAN", records, "no_step")
    rec, step = records[idx], steps[idx]

    if step.verify is None:  # an interrupted read changed nothing
        rec["status"] = "failed"
        return await _finish(run, ("UNKNOWN_OUTCOME",), _stopped(records, "FAILED"), records, "interrupted")

    async def read(call: Call) -> Outcome:
        if call.method.upper() != "GET":
            raise ValueError("verification must only read")
        return await dispatch(call, auth, cid, rid, app=app, timeout=timeout)

    ok, rec["evidence"] = await step.verify(read, None, step.call)
    rec["reverify_attempts"] = rec.get("reverify_attempts", 0) + 1
    if ok is True:
        rec["status"] = "done"
        all_done = all(r["status"] == "done" for r in records)
        return await _finish(run, ("UNKNOWN_OUTCOME",), "COMPLETED_VERIFIED" if all_done else "PARTIALLY_COMPLETED",
                             records, None)
    if ok is False:
        rec["status"] = "failed"
        return await _finish(run, ("UNKNOWN_OUTCOME",), _stopped(records, "FAILED"), records, "not_written")
    state = "NEEDS_HUMAN" if rec["reverify_attempts"] >= MAX_REVERIFY else "UNKNOWN_OUTCOME"
    return await _finish(run, ("UNKNOWN_OUTCOME",), state, records, rec.get("error_code"))
