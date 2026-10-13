# ADR-004 — Execute by dispatching existing /admin endpoints in-process; verify by read-back

Status: **Proposed** · Date: 2026-10-13

## Context
The brief forbids duplicated business logic and parallel permission systems. Audit facts:
- Much of the write logic lives **inside route handlers** in `app/routers/admin.py` (e.g. `create_leave`:
  branch-doctor check, date-range expansion, insert, audit). There is no service layer for these.
- Authorization is spread across dependencies: `verify_credentials` (which also applies
  `_OPD_ROLE_DENIED_ROUTES` **by request path regex**, corporate-viewer and phlebotomist confinement),
  `require_permission`, `require_admin`, `enforce_clinic_access`, `enforce_branch_scope` inside handlers.
- `httpx>=0.26` is already a dependency and provides `ASGITransport`.
- The voice receptionist proves the "write via existing services, then read back and record a proof"
  pattern (`app/voice/tools.py`).

## Options
1. **Call domain services directly** from the assistant. The logic is in the handlers for many actions,
   so it would be duplicated, and every dependency check would need re-implementing.
2. **Call handler functions as Python** (e.g. `await create_leave(leave=…, user=…)`). This skips
   `Depends(...)` and the path-based confinement, so the auth would need re-implementing per action.
3. **Refactor handlers into services first**, then call the services. Clean, but a large, risky change
   across a 9.7k-line production router before any user value.
4. **Dispatch the real HTTP route in-process** via `httpx.ASGITransport(app)` with the caller's own
   session cookie or Basic header.

## Decision
Option 4. Every write is followed by a declared **verification read** (an existing GET or a scoped
read-back). Writes are never auto-retried. An uncertain result is `UNKNOWN_OUTCOME` → re-verify.

## Reasons
- Zero duplication: validation, permission, tenant, branch, OPD role confinement, CAS guards and the
  endpoint's own audit row all run exactly as they do for a click in the panel.
- Revocations that end the session are enforced at execution time.
- No new dependency and no network hop.

## Consequences
- Middlewares run on the inner request too (correlation, CORS, security headers). This is harmless; the
  correlation id is forwarded.
- `request.client.host` inside the inner request is the transport's fake client, so audit IPs for
  assistant writes come from `assistant_runs.ip_address` (DP-10 decides whether to also teach
  `log_admin_action` via a non-HTTP scope marker).
- Error bodies must be mapped. Some endpoints return raw `str(e)` (D-03).
- The action registry must stay in sync with route permissions, so a drift test introspects the routes.

## Risks
- Endpoints whose response doesn't reveal the outcome need a separate read-back (declared per action).
- Endpoint behaviour changes silently change the assistant. Mitigated by the per-action tests in CI.
- Session permission snapshots (D-09) mean permission *edits* are not enforced until re-login. This is
  inherited from the panel and fixed in KK-P0-04.

## Verification
EXE-01..13, SEC-04/05/10, the registry drift test.

## Revisit when
Handlers are refactored into services for other reasons (option 3 becomes free), or dispatch overhead
shows up in profiles (unlikely at admin request rates).
