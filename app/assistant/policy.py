"""Kriya Ketu policy: pre-check, preview, plan hash (architecture.md §3 steps 7-9, §5; KK-FR-020..023).

The pre-check mirrors the endpoints' own rules (require_permission semantics,
require_admin, the route's feature gate) so a refusal can be explained up front.
It never replaces them: the dispatched endpoint is still the authority, and
branch scope is enforced there per record (enforce_branch_scope).

The plan hash covers the action, its params, every planned write (method, path,
query, body) and the preview's snapshot and count. Confirm recomputes it: any
change in what would be written, or in the data the preview read, means a new
preview instead of an execution.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from app.assistant.errors import EmptyPlan, PlanError  # noqa: F401  (re-exported: callers use policy.*)
from app.assistant.registry import ADMIN_ROLES, REGISTRY, ActionSpec

PREVIEW_MINUTES = 5


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: Optional[str] = None   # forbidden | feature_disabled
    message: str = ""


def _label(permission: str) -> str:
    return permission.replace("_", " ").title()


def precheck(spec: ActionSpec, user, clinic: dict) -> Decision:
    if spec.feature is not None and not spec.feature(clinic):
        return Decision(False, "feature_disabled", "That isn't switched on for this clinic.")
    is_admin = user.role in ADMIN_ROLES
    if not is_admin and (user.role not in spec.roles or spec.perm_source == "role"):
        return Decision(False, "forbidden", "Only a clinic admin can do that.")
    if spec.perm_source in ("dependency", "handler") and not is_admin \
            and not {spec.permission, *spec.also} & set(user.permissions or []):
        return Decision(False, "forbidden",
                        f"You need the {_label(spec.permission)} permission for that. Ask your clinic admin.")
    return Decision(True)


def permitted_actions(user, clinic: dict) -> list[str]:
    """The actions this user may run here: the LLM's enum (it can never name anything else)."""
    return [a for a, spec in REGISTRY.items() if precheck(spec, user, clinic).allowed]


def plan_hash(action_id: str, params: dict, steps: list, snapshot, affected: int) -> str:
    canonical = json.dumps({
        "action": action_id, "params": params, "affected": affected, "snapshot": snapshot,
        "steps": [{"method": s.call.method.upper(), "path": s.call.path, "query": s.call.query, "json": s.call.json}
                  for s in steps],
    }, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def build_plan(spec: ActionSpec, params: dict, read) -> tuple[list, dict, str]:
    """(steps, preview, plan_hash). Reads only: `read` dispatches GETs as the caller.
    Steps come from the params, or from the preview when they depend on what it read (e.g. the
    ids of the leave rows to remove); either way they are in the hash."""
    if spec.risk == "H":  # SEC-18: a hand-off is never planned or executed
        raise PlanError(f"{spec.id}: H actions are never executed")
    preview = await spec.preview(read, params) if spec.preview else {}
    preview = {"title": spec.summary, "bullets": [], "rows": [], "warnings": [], "snapshot": None, **preview}
    steps = preview.pop("steps", None)
    steps = spec.call(params) if steps is None else steps
    preview.setdefault("affected", len(steps))
    if preview["affected"] > spec.max_items:
        raise PlanError(f"{spec.id}: {preview['affected']} items exceeds the limit of {spec.max_items}")
    return steps, preview, plan_hash(spec.id, params, steps, preview["snapshot"], preview["affected"])
