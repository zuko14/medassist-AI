# Staff RBAC & Delegated Doctor/Holiday/Schedule Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Client Admin create staff accounts with a named role and a delegated
permission set (optionally branch-scoped) so trusted staff can manage doctors, doctor↔branch
assignment, doctor leaves, and hospital holidays — enforced server-side, fully backward
compatible with today's all-or-nothing staff accounts.

**Architecture:** Add `staff_role` / `permissions TEXT[]` / `branch_id` columns to the
existing `clinic_admins` table (no new RBAC tables). Add a `require_permission(perm)`
FastAPI dependency (parallel to the existing `require_admin`) and swap it onto the doctor,
doctor-branch-assignment, leave, and holiday write endpoints in `app/routers/admin.py`.
Extend staff creation/edit endpoints and the admin frontend to set/display role, permissions,
and branch scope.

**Tech Stack:** FastAPI, Supabase (Postgres) via `supabase-py` client, Pydantic, pytest +
pytest-asyncio, vanilla JS admin panel (`admin/index.html`).

**Spec:** `docs/superpowers/specs/2026-08-20-staff-rbac-doctor-delegation-design.md`

## Global Constraints

- No new join tables — extend `clinic_admins` with `staff_role`, `permissions TEXT[]`, `branch_id`.
- Existing `role='staff'` rows get `permissions = '{}'` (empty) on migration — no behavior change until an admin explicitly grants permissions.
- `require_permission` depends on `verify_credentials`, never on `require_admin` (which unconditionally 403s staff).
- `DOCTORS_UPDATE` is the single permission gating the whole doctor edit form (name/specialty/schedule/slots/fee) — no field-level splitting.
- A staff account with `STAFF_CREATE`/`STAFF_UPDATE` can never grant a permission or branch scope beyond its own resolved `permissions`/`branch_id`, and can never set another account's `role` to `clinic_admin`/`super_admin`.
- All existing `require_admin`-gated endpoints not explicitly listed for conversion stay `require_admin`-only (payments, connectors, branches CRUD, profile, bookings, prescriptions).
- Every new/changed mutating endpoint calls `log_admin_action(...)` (existing helper).
- Must not break `tests/test_admin_staff_role.py`, `tests/test_admin_staff_accounts.py`, `tests/test_rbac.py` (all read/patch `app/routers/admin.py`).

---

### Task 1: Migration — add role/permission/branch columns to `clinic_admins`

**Files:**
- Create: `migrations/036_staff_permissions.sql`

**Interfaces:**
- Produces: columns `clinic_admins.staff_role TEXT`, `clinic_admins.permissions TEXT[] NOT NULL DEFAULT '{}'`, `clinic_admins.branch_id UUID REFERENCES branches(id)` — consumed by Task 3 onward.

- [ ] **Step 1: Write the migration**

```sql
-- Migration 036: Staff role, delegated permissions, and branch scope
-- Additive only — existing rows get permissions = '{}' (no behavior change;
-- staff accounts today are already blocked from every admin-tier endpoint,
-- so an empty grant list reproduces that exactly).

ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS staff_role TEXT;
ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS permissions TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id);

CREATE INDEX IF NOT EXISTS idx_clinic_admins_branch ON clinic_admins(branch_id);

-- Verify
SELECT id, username, role, staff_role, permissions, branch_id
FROM clinic_admins ORDER BY created_at;
```

- [ ] **Step 2: Sanity-check idempotency**

Confirm every statement uses `IF NOT EXISTS` (columns/index) so re-running the file is a
no-op, matching the pattern of every other migration in `migrations/`.

- [ ] **Step 3: Commit**

```bash
git add migrations/036_staff_permissions.sql
git commit -m "feat(db): add staff_role, permissions, branch_id to clinic_admins"
```

---

### Task 2: Permission constants, role presets, and core helpers

**Files:**
- Create: `app/services/permissions.py`
- Test: `tests/test_permissions.py`

**Interfaces:**
- Produces:
  - `PERMISSIONS: frozenset[str]` — the valid permission strings
  - `STAFF_ROLES: frozenset[str]` — valid `staff_role` values
  - `ROLE_PRESETS: dict[str, list[str]]` — `staff_role` → default permission grants
  - `resolve_permissions(staff_role: str, extra_permissions: list[str]) -> list[str]`
  - `cap_permissions_to_authority(requested: list[str], granter_permissions: list[str], granter_role: str) -> list[str]`
  - `validate_staff_role(staff_role: str) -> str` (raises `ValueError` if unknown)
- Consumes: nothing (leaf module).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_permissions.py
import pytest

from app.services.permissions import (
    PERMISSIONS,
    ROLE_PRESETS,
    STAFF_ROLES,
    cap_permissions_to_authority,
    resolve_permissions,
    validate_staff_role,
)


def test_permissions_constants_are_nonempty_and_unique():
    assert len(PERMISSIONS) > 0
    assert len(PERMISSIONS) == len(set(PERMISSIONS))


def test_role_presets_only_reference_known_permissions():
    for role, perms in ROLE_PRESETS.items():
        assert role in STAFF_ROLES
        for p in perms:
            assert p in PERMISSIONS


def test_resolve_permissions_merges_preset_and_extras():
    result = resolve_permissions("DOCTOR_SCHEDULE_MANAGER", ["HOLIDAYS_CREATE"])
    assert "DOCTORS_UPDATE" in result
    assert "HOLIDAYS_CREATE" in result
    assert len(result) == len(set(result))  # deduped


def test_resolve_permissions_rejects_unknown_extra_permission():
    with pytest.raises(ValueError):
        resolve_permissions("STAFF", ["NOT_A_REAL_PERMISSION"])


def test_resolve_permissions_default_staff_role_has_no_grants():
    assert resolve_permissions("STAFF", []) == []


def test_validate_staff_role_rejects_unknown_role():
    with pytest.raises(ValueError):
        validate_staff_role("SUPER_ADMIN")


def test_validate_staff_role_accepts_known_role():
    assert validate_staff_role("RECEPTIONIST") == "RECEPTIONIST"


def test_cap_permissions_to_authority_for_clinic_admin_allows_anything_valid():
    capped = cap_permissions_to_authority(
        requested=["DOCTORS_CREATE", "HOLIDAYS_CREATE"],
        granter_permissions=[],
        granter_role="clinic_admin",
    )
    assert set(capped) == {"DOCTORS_CREATE", "HOLIDAYS_CREATE"}


def test_cap_permissions_to_authority_for_staff_strips_ungranted_permissions():
    capped = cap_permissions_to_authority(
        requested=["DOCTORS_CREATE", "HOLIDAYS_CREATE"],
        granter_permissions=["DOCTORS_CREATE"],
        granter_role="staff",
    )
    assert capped == ["DOCTORS_CREATE"]
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_permissions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.permissions'`

- [ ] **Step 3: Implement `app/services/permissions.py`**

```python
"""Delegated staff permission model — extends the existing
super_admin/clinic_admin/staff tier in app.routers.admin with granular,
server-enforced grants for staff accounts. See
docs/superpowers/specs/2026-08-20-staff-rbac-doctor-delegation-design.md.
"""

PERMISSIONS = frozenset({
    "DOCTORS_CREATE",
    "DOCTORS_UPDATE",
    "DOCTORS_DELETE",
    "DOCTOR_BRANCH_ASSIGN",
    "DOCTOR_LEAVES_CREATE",
    "DOCTOR_LEAVES_DELETE",
    "HOLIDAYS_CREATE",
    "HOLIDAYS_DELETE",
    "STAFF_VIEW",
    "STAFF_CREATE",
    "STAFF_UPDATE",
})

STAFF_ROLES = frozenset({
    "STAFF",
    "RECEPTIONIST",
    "FRONT_DESK",
    "APPOINTMENT_MANAGER",
    "LAB_OPERATOR",
    "PHARMACY_OPERATOR",
    "DOCTOR_SCHEDULE_MANAGER",
    "BRANCH_MANAGER",
    "CUSTOM_ROLE",
})

_DOCTOR_SCHEDULE_GRANTS = [
    "DOCTORS_UPDATE",
    "DOCTOR_BRANCH_ASSIGN",
    "DOCTOR_LEAVES_CREATE",
    "DOCTOR_LEAVES_DELETE",
    "HOLIDAYS_CREATE",
    "HOLIDAYS_DELETE",
]

ROLE_PRESETS: dict[str, list[str]] = {
    "STAFF": [],
    "RECEPTIONIST": [],
    "FRONT_DESK": [],
    "APPOINTMENT_MANAGER": [],
    "LAB_OPERATOR": [],
    "PHARMACY_OPERATOR": [],
    "DOCTOR_SCHEDULE_MANAGER": list(_DOCTOR_SCHEDULE_GRANTS),
    "BRANCH_MANAGER": _DOCTOR_SCHEDULE_GRANTS + ["DOCTORS_CREATE", "DOCTORS_DELETE", "STAFF_VIEW"],
    "CUSTOM_ROLE": [],
}


def validate_staff_role(staff_role: str) -> str:
    """Raise ValueError if staff_role isn't a recognized value."""
    if staff_role not in STAFF_ROLES:
        raise ValueError(f"Unknown staff_role: {staff_role!r}")
    return staff_role


def resolve_permissions(staff_role: str, extra_permissions: list[str]) -> list[str]:
    """Merge a role's preset grants with explicit extra grants.

    Raises ValueError if staff_role or any extra permission isn't recognized.
    """
    validate_staff_role(staff_role)
    for p in extra_permissions:
        if p not in PERMISSIONS:
            raise ValueError(f"Unknown permission: {p!r}")
    merged = list(dict.fromkeys(ROLE_PRESETS[staff_role] + list(extra_permissions)))
    return merged


def cap_permissions_to_authority(
    requested: list[str], granter_permissions: list[str], granter_role: str
) -> list[str]:
    """A granter can never hand out a permission they don't hold themselves.

    clinic_admin/super_admin hold full tenant authority — any valid permission
    passes through. A staff granter (has STAFF_CREATE/STAFF_UPDATE) is capped
    to the intersection of what they requested and what they themselves hold.
    """
    if granter_role in ("clinic_admin", "super_admin"):
        return [p for p in requested if p in PERMISSIONS]
    granter_set = set(granter_permissions or [])
    return [p for p in requested if p in granter_set]
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_permissions.py -v`
Expected: PASS (all 8 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/permissions.py tests/test_permissions.py
git commit -m "feat(rbac): add permission constants, role presets, and authority-capping helpers"
```

---

### Task 3: Wire `AdminUser` + `verify_credentials` to carry permissions/branch_id

**Files:**
- Modify: `app/routers/admin.py:49-79` (`AdminUser` class), `app/routers/admin.py:142-213` (`verify_credentials`)
- Test: `tests/test_permissions.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `AdminUser.permissions: list[str]`, `AdminUser.branch_id: Optional[str]` — consumed by Task 4 (`require_permission`) and every later task.

- [ ] **Step 1: Write failing test**

```python
# append to tests/test_permissions.py
from unittest.mock import MagicMock, patch

from app.routers.admin import AdminUser, verify_credentials


def test_admin_user_defaults_permissions_and_branch_to_empty():
    user = AdminUser("frontdesk", role="staff", clinic_id="clinic-1")
    assert user.permissions == []
    assert user.branch_id is None


def test_admin_user_carries_explicit_permissions_and_branch():
    user = AdminUser(
        "frontdesk", role="staff", clinic_id="clinic-1",
        permissions=["DOCTORS_UPDATE"], branch_id="branch-1",
    )
    assert user.permissions == ["DOCTORS_UPDATE"]
    assert user.branch_id == "branch-1"


@pytest.mark.asyncio
async def test_verify_credentials_populates_permissions_from_db_row():
    request = MagicMock()
    request.client.host = "127.0.0.1"
    creds = MagicMock(username="frontdesk1", password="password1")

    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{
            "id": "s1",
            "username": "frontdesk1",
            "password_hash": "$2b$dummy",
            "role": "staff",
            "clinic_id": "clinic-1",
            "permissions": ["DOCTORS_UPDATE"],
            "branch_id": "branch-1",
        }]
    )

    with patch("app.routers.admin.supabase", mock_sb), \
         patch("app.routers.admin.check_password_hash", return_value=True), \
         patch("app.routers.admin.login_rate_limiter") as rl:
        rl.check_and_record.return_value = False
        user = await verify_credentials(request=request, credentials=creds)

    assert user.permissions == ["DOCTORS_UPDATE"]
    assert user.branch_id == "branch-1"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_permissions.py -v -k "admin_user or verify_credentials_populates"`
Expected: FAIL — `AdminUser.__new__() got an unexpected keyword argument 'permissions'`

- [ ] **Step 3: Read the current `AdminUser` and `verify_credentials` implementations in full**

Read `app/routers/admin.py:49-213` before editing to match exact current field list, the
exact shape of the DB-row branch (line ~176) and the env-fallback super-admin branch (line
~197), and the exact `login_rate_limiter` usage — this plan's snippets below show the target
end state, not a diff; adapt line numbers to whatever they've drifted to since this plan was
written.

- [ ] **Step 4: Update `AdminUser.__new__` and `verify_credentials`**

Extend `AdminUser`:

```python
class AdminUser(str):
    """Authenticated admin user with RBAC role, clinic scope, staff user ID,
    delegated permissions, and optional branch scope."""

    username: str
    role: str
    clinic_id: Optional[str]
    user_id: Optional[str]
    permissions: list[str]
    branch_id: Optional[str]

    def __new__(
        cls,
        username: str,
        role: str = "clinic_admin",
        clinic_id: Optional[str] = None,
        user_id: Optional[str] = None,
        permissions: Optional[list[str]] = None,
        branch_id: Optional[str] = None,
    ):
        obj = super().__new__(cls, username)
        obj.username = username
        obj.role = role
        obj.clinic_id = clinic_id
        obj.user_id = user_id
        obj.permissions = permissions or []
        obj.branch_id = branch_id
        return obj
```

(`can_access_clinic` is unchanged.)

In `verify_credentials`, the DB-row branch that builds `AdminUser(...)` becomes:

```python
return AdminUser(
    username=user_row["username"],
    role=user_row.get("role", "clinic_admin"),
    clinic_id=user_row.get("clinic_id"),
    user_id=user_row.get("id"),
    permissions=user_row.get("permissions") or [],
    branch_id=user_row.get("branch_id"),
)
```

The env-fallback super-admin branch is unchanged (super_admin never needs
`permissions`/`branch_id` — `require_permission` short-circuits on role).

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_permissions.py -v`
Expected: PASS (all tests from Task 2 + Task 3)

- [ ] **Step 6: Run full existing admin test files to confirm no regression**

Run: `pytest tests/test_rbac.py tests/test_admin_staff_role.py tests/test_admin_staff_accounts.py -v`
Expected: PASS (all previously-passing tests still pass — `AdminUser(...)` calls without
the new kwargs still work because they're optional with safe defaults)

- [ ] **Step 7: Commit**

```bash
git add app/routers/admin.py tests/test_permissions.py
git commit -m "feat(rbac): carry permissions and branch_id on AdminUser from clinic_admins row"
```

---

### Task 4: `require_permission` dependency + branch-scope helper

**Files:**
- Modify: `app/services/permissions.py`
- Test: `tests/test_permissions.py` (append)

**Interfaces:**
- Consumes: `AdminUser` (Task 3), `app.routers.admin.verify_credentials` (imported inside the function to avoid a circular import — `admin.py` will import `permissions.py`, not vice versa).
- Produces: `require_permission(perm: str) -> Callable` (FastAPI dependency factory), `enforce_branch_scope(user: AdminUser, resource_branch_id: Optional[str]) -> None` (raises 403 on mismatch) — consumed by Task 6 onward.

- [ ] **Step 1: Write failing test**

```python
# append to tests/test_permissions.py
from fastapi import HTTPException

from app.services.permissions import enforce_branch_scope, require_permission


def _staff(permissions=None, branch_id=None):
    return AdminUser("s1", role="staff", clinic_id="clinic-1", permissions=permissions or [], branch_id=branch_id)


def _clinic_admin():
    return AdminUser("a1", role="clinic_admin", clinic_id="clinic-1")


@pytest.mark.asyncio
async def test_require_permission_allows_clinic_admin_regardless_of_grants():
    dep = require_permission("DOCTORS_CREATE")
    result = await dep(user=_clinic_admin())
    assert result.role == "clinic_admin"


@pytest.mark.asyncio
async def test_require_permission_allows_staff_with_grant():
    dep = require_permission("DOCTORS_CREATE")
    result = await dep(user=_staff(permissions=["DOCTORS_CREATE"]))
    assert result.role == "staff"


@pytest.mark.asyncio
async def test_require_permission_rejects_staff_without_grant():
    dep = require_permission("DOCTORS_CREATE")
    with pytest.raises(HTTPException) as exc:
        await dep(user=_staff(permissions=["HOLIDAYS_CREATE"]))
    assert exc.value.status_code == 403


def test_enforce_branch_scope_passes_for_tenant_wide_staff():
    enforce_branch_scope(_staff(branch_id=None), resource_branch_id="branch-1")  # no raise


def test_enforce_branch_scope_passes_when_branches_match():
    enforce_branch_scope(_staff(branch_id="branch-1"), resource_branch_id="branch-1")  # no raise


def test_enforce_branch_scope_rejects_mismatched_branch():
    with pytest.raises(HTTPException) as exc:
        enforce_branch_scope(_staff(branch_id="branch-1"), resource_branch_id="branch-2")
    assert exc.value.status_code == 403


def test_enforce_branch_scope_never_restricts_clinic_admin():
    enforce_branch_scope(_clinic_admin(), resource_branch_id="branch-2")  # no raise
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_permissions.py -v -k "require_permission or enforce_branch_scope"`
Expected: FAIL — `ImportError: cannot import name 'require_permission'`

- [ ] **Step 3: Implement in `app/services/permissions.py`**

```python
from typing import TYPE_CHECKING, Optional

from fastapi import Depends, HTTPException

if TYPE_CHECKING:
    from app.routers.admin import AdminUser


def require_permission(perm: str):
    """FastAPI dependency factory. NOTE: depends on verify_credentials, NOT
    require_admin — require_admin unconditionally 403s every role="staff"
    account, which would make delegated permissions unreachable."""

    async def _dep(user=Depends(_verify_credentials_dep())) -> "AdminUser":
        if user.role in ("super_admin", "clinic_admin"):
            return user
        if perm not in (user.permissions or []):
            raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
        return user

    return _dep


def _verify_credentials_dep():
    """Lazy import to avoid a circular import (app.routers.admin imports this module)."""
    from app.routers.admin import verify_credentials

    return verify_credentials


def enforce_branch_scope(user, resource_branch_id: Optional[str]) -> None:
    """A staff account scoped to one branch (user.branch_id is set) may only
    act on resources belonging to that branch. Tenant-wide staff
    (branch_id is None) and clinic_admin/super_admin are unrestricted."""
    if user.role in ("super_admin", "clinic_admin"):
        return
    if not getattr(user, "branch_id", None):
        return
    if resource_branch_id and str(resource_branch_id) != str(user.branch_id):
        raise HTTPException(
            status_code=403,
            detail="This action is outside your assigned branch.",
        )
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_permissions.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add app/services/permissions.py tests/test_permissions.py
git commit -m "feat(rbac): add require_permission dependency and branch-scope enforcement"
```

---

### Task 5: Extend `StaffCreate`, `create_staff` — role, permissions, branch, authority capping

**Files:**
- Modify: `app/routers/admin.py:359-426` (`StaffCreate` model, `list_staff`, `create_staff`)
- Test: `tests/test_admin_staff_accounts.py` (append)

**Interfaces:**
- Consumes: `resolve_permissions`, `cap_permissions_to_authority`, `validate_staff_role` from `app.services.permissions` (Task 2); `require_permission` (Task 4); `resolve_clinic_id_for_write` (existing).
- Produces: `POST /admin/staff` accepts `staff_role`, `extra_permissions`, `branch_id`; `GET /admin/staff` returns `staff_role`, `permissions`, `branch_id` per row — consumed by Task 7 (frontend) and Task 6 (`PUT /admin/staff/{id}`).

- [ ] **Step 1: Read `app/routers/admin.py:355-460` in full**

Confirm exact current bodies of `StaffCreate`, `list_staff`, `create_staff`, `toggle_staff`
before editing (line numbers may have drifted since this plan was authored).

- [ ] **Step 2: Write failing tests**

```python
# append to tests/test_admin_staff_accounts.py

def _staff_with_perms(permissions, branch_id=None, clinic_id="clinic-1"):
    return AdminUser("delegator", role="staff", clinic_id=clinic_id, user_id="s-del", permissions=permissions, branch_id=branch_id)


@pytest.mark.asyncio
async def test_create_staff_resolves_role_preset_permissions():
    from app.routers.admin import StaffCreate as SC
    body = SC(username="sched1", password="password1", staff_role="DOCTOR_SCHEDULE_MANAGER")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "new-2", "username": "sched1", "role": "staff"}]
    )

    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        await create_staff(body=body, request=_mock_request(), user=_clinic_admin("clinic-1"))

    insert_call = mock_sb.table.return_value.insert.call_args[0][0]
    assert "DOCTORS_UPDATE" in insert_call["permissions"]
    assert insert_call["staff_role"] == "DOCTOR_SCHEDULE_MANAGER"


@pytest.mark.asyncio
async def test_create_staff_validates_branch_belongs_to_own_clinic():
    from app.routers.admin import StaffCreate as SC
    body = SC(username="sched1", password="password1", branch_id="branch-x")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    # branch lookup returns no matching row for this clinic
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await create_staff(body=body, request=_mock_request(), user=_clinic_admin("clinic-1"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_create_staff_caps_extra_permissions_to_granters_own_authority():
    from app.routers.admin import StaffCreate as SC
    body = SC(
        username="sched2", password="password1",
        extra_permissions=["DOCTORS_CREATE", "STAFF_CREATE"],
    )
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "new-3", "username": "sched2", "role": "staff"}]
    )

    # granter is staff with only DOCTORS_CREATE — must not be able to grant STAFF_CREATE
    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        await create_staff(
            body=body, request=_mock_request(),
            user=_staff_with_perms(["DOCTORS_CREATE", "STAFF_CREATE"]),
        )

    insert_call = mock_sb.table.return_value.insert.call_args[0][0]
    assert insert_call["permissions"] == ["DOCTORS_CREATE"]
```

- [ ] **Step 3: Run tests, verify they fail**

Run: `pytest tests/test_admin_staff_accounts.py -v -k "resolves_role_preset or validates_branch or caps_extra"`
Expected: FAIL — `StaffCreate` has no field `staff_role` / `create_staff` doesn't set `permissions`

- [ ] **Step 4: Implement**

Extend `StaffCreate`:

```python
class StaffCreate(BaseModel):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)
    staff_role: str = "STAFF"
    extra_permissions: list[str] = Field(default_factory=list)
    branch_id: Optional[str] = None
```

Rewrite `create_staff` to resolve/validate/cap before insert, and swap its dependency from
`require_admin` to `require_permission("STAFF_CREATE")`:

```python
@router.post("/staff")
async def create_staff(
    body: StaffCreate,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("STAFF_CREATE")),
):
    """Create a staff login scoped to this clinic, with a role preset and
    optional extra delegated permissions/branch scope. Role is always
    'staff' at the tier level — this endpoint can never mint clinic_admin/
    super_admin accounts. Grants are capped to the caller's own authority."""
    from app.services.permissions import (
        cap_permissions_to_authority,
        resolve_permissions,
        validate_staff_role,
    )

    effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)

    try:
        validate_staff_role(body.staff_role)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        resolved_permissions = resolve_permissions(body.staff_role, body.extra_permissions)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    final_permissions = cap_permissions_to_authority(
        requested=resolved_permissions,
        granter_permissions=user.permissions,
        granter_role=user.role,
    )

    if body.branch_id:
        branch_check = (
            supabase.table("branches")
            .select("id")
            .eq("id", body.branch_id)
            .eq("clinic_id", effective_clinic_id)
            .execute()
        )
        if not branch_check.data:
            raise HTTPException(
                status_code=400, detail="Selected branch does not belong to your clinic."
            )

    existing = (
        supabase.table("clinic_admins")
        .select("id")
        .eq("username", body.username)
        .execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="Username already exists")

    result = (
        supabase.table("clinic_admins")
        .insert(
            {
                "username": body.username,
                "password_hash": hash_password(body.password),
                "role": "staff",
                "staff_role": body.staff_role,
                "permissions": final_permissions,
                "branch_id": body.branch_id,
                "clinic_id": effective_clinic_id,
                "is_active": True,
            }
        )
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create staff account")

    await log_admin_action(
        user=user,
        action="create_staff",
        resource_type="clinic_admin",
        resource_id=result.data[0]["id"],
        details={"staff_role": body.staff_role, "permissions": final_permissions, "branch_id": body.branch_id},
        ip_address=request.client.host if request.client else "unknown",
    )
    return {"success": True, "staff": result.data[0]}
```

`clinic_admin`/`super_admin` still pass unconditionally (unchanged behavior); a `staff`
account can now only reach this endpoint if explicitly granted `STAFF_CREATE`.

Update `list_staff` to select the new columns and swap its dependency to
`require_permission("STAFF_VIEW")`:

```python
@router.get("/staff")
async def list_staff(
    clinic_id: str = "default", user: AdminUser = Depends(require_permission("STAFF_VIEW"))
):
    """List staff accounts for this clinic (no password hashes)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    query = supabase.table("clinic_admins").select(
        "id, username, role, staff_role, permissions, branch_id, is_active, created_at"
    ).eq("role", "staff")
    if effective_clinic_id != "default":
        query = query.eq("clinic_id", effective_clinic_id)
    result = query.order("created_at", desc=True).execute()
    return {"staff": result.data or []}
```

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_admin_staff_accounts.py -v`
Expected: PASS — all pre-existing tests in this file AND the 3 new ones

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin.py tests/test_admin_staff_accounts.py
git commit -m "feat(staff): resolve role presets, cap delegated permissions to granter authority"
```

---

### Task 6: `PUT /admin/staff/{id}` — edit role/permissions/branch

**Files:**
- Modify: `app/routers/admin.py` (add new endpoint near `toggle_staff`)
- Test: `tests/test_admin_staff_accounts.py` (append)

**Interfaces:**
- Consumes: `resolve_permissions`, `cap_permissions_to_authority`, `validate_staff_role` (Task 2), `require_permission` (Task 4), `enforce_clinic_access` (existing).
- Produces: `PUT /admin/staff/{staff_id}` — request body `StaffUpdate` (`staff_role`, `extra_permissions`, `branch_id`, `is_active`, all optional/partial).

- [ ] **Step 1: Write failing tests**

```python
# append to tests/test_admin_staff_accounts.py
from app.routers.admin import StaffUpdate, update_staff


@pytest.mark.asyncio
async def test_update_staff_changes_role_and_permissions():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "s1", "clinic_id": "clinic-1", "role": "staff", "permissions": [], "branch_id": None}]
    )
    mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "s1", "staff_role": "DOCTOR_SCHEDULE_MANAGER"}]
    )

    body = StaffUpdate(staff_role="DOCTOR_SCHEDULE_MANAGER")
    with patch("app.routers.admin.supabase", mock_sb), patch("app.routers.admin.log_admin_action"):
        result = await update_staff(staff_id="s1", body=body, request=_mock_request(), user=_clinic_admin("clinic-1"))

    assert result["success"] is True
    update_call = mock_sb.table.return_value.update.call_args[0][0]
    assert "DOCTORS_UPDATE" in update_call["permissions"]


@pytest.mark.asyncio
async def test_update_staff_rejects_cross_clinic_target():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "s2", "clinic_id": "clinic-2", "role": "staff", "permissions": [], "branch_id": None}]
    )

    body = StaffUpdate(staff_role="RECEPTIONIST")
    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await update_staff(staff_id="s2", body=body, request=_mock_request(), user=_clinic_admin("clinic-1"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_staff_rejects_non_staff_target():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "a1", "clinic_id": "clinic-1", "role": "clinic_admin", "permissions": [], "branch_id": None}]
    )

    body = StaffUpdate(staff_role="RECEPTIONIST")
    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await update_staff(staff_id="a1", body=body, request=_mock_request(), user=_clinic_admin("clinic-1"))
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_admin_staff_accounts.py -v -k update_staff`
Expected: FAIL — `ImportError: cannot import name 'StaffUpdate'`

- [ ] **Step 3: Read `toggle_staff`'s current implementation and `enforce_clinic_access` in full**

Confirms the exact cross-clinic-rejection status code pattern (`403` per
`test_toggle_staff_rejects_cross_clinic_access`) to mirror in `update_staff`.

- [ ] **Step 4: Implement**

Add after `toggle_staff`:

```python
class StaffUpdate(BaseModel):
    staff_role: Optional[str] = None
    extra_permissions: Optional[list[str]] = None
    branch_id: Optional[str] = None
    is_active: Optional[bool] = None


@router.put("/staff/{staff_id}")
async def update_staff(
    staff_id: str,
    body: StaffUpdate,
    request: Request,
    user: AdminUser = Depends(require_permission("STAFF_UPDATE")),
):
    """Edit an existing staff account's role, delegated permissions, branch
    scope, or active status. Grants are re-capped to the caller's own
    authority on every edit — a staff granter can never escalate a target
    account past their own permission ceiling."""
    from app.services.permissions import (
        cap_permissions_to_authority,
        resolve_permissions,
        validate_staff_role,
    )

    res = (
        supabase.table("clinic_admins")
        .select("id, clinic_id, role, permissions, branch_id")
        .eq("id", staff_id)
        .execute()
    )
    if not res.data or res.data[0]["role"] != "staff":
        raise HTTPException(status_code=404, detail="Staff account not found")
    target = res.data[0]

    enforce_clinic_access(user, target["clinic_id"] or "default")

    update_data: dict = {}
    if body.is_active is not None:
        update_data["is_active"] = body.is_active

    if body.staff_role is not None or body.extra_permissions is not None:
        new_role = body.staff_role or "CUSTOM_ROLE"
        try:
            validate_staff_role(new_role)
            resolved = resolve_permissions(new_role, body.extra_permissions or [])
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        update_data["staff_role"] = new_role
        update_data["permissions"] = cap_permissions_to_authority(
            requested=resolved, granter_permissions=user.permissions, granter_role=user.role,
        )

    if body.branch_id is not None:
        if body.branch_id:
            branch_check = (
                supabase.table("branches")
                .select("id")
                .eq("id", body.branch_id)
                .eq("clinic_id", target["clinic_id"])
                .execute()
            )
            if not branch_check.data:
                raise HTTPException(
                    status_code=400, detail="Selected branch does not belong to this clinic."
                )
        update_data["branch_id"] = body.branch_id or None

    if not update_data:
        raise HTTPException(status_code=400, detail="No changes provided")

    result = supabase.table("clinic_admins").update(update_data).eq("id", staff_id).execute()

    await log_admin_action(
        user=user,
        action="update_staff",
        resource_type="clinic_admin",
        resource_id=staff_id,
        details={"before": {"permissions": target["permissions"], "branch_id": target["branch_id"]}, "after": update_data},
        ip_address=request.client.host if request.client else "unknown",
    )
    return {"success": True, "staff": result.data[0] if result.data else None}
```

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_admin_staff_accounts.py -v`
Expected: PASS (all tests in file)

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin.py tests/test_admin_staff_accounts.py
git commit -m "feat(staff): add PUT /admin/staff/{id} to edit role, permissions, and branch scope"
```

---

### Task 7: `GET /admin/me` returns permissions/branch_id/staff_role

**Files:**
- Modify: `app/routers/admin.py` (`get_current_admin`, the handler for `GET /admin/me`)
- Test: `tests/test_admin_me.py` (new)

**Interfaces:**
- Consumes: `AdminUser.permissions`/`branch_id` (Task 3).
- Produces: `/admin/me` JSON now includes `permissions`, `branch_id`, `staff_role` — consumed by Task 12 (frontend UI gating). `staff_role` is fetched fresh from DB since `AdminUser` doesn't carry it (only `permissions`/`branch_id` do, per Task 3 — avoids widening `AdminUser` further for a field only the `/me` screen needs).

- [ ] **Step 1: Read the current `GET /admin/me` handler in full**

Locate it via `Grep` for `"/me"` in `app/routers/admin.py` and read the function in full to
capture its exact current return shape before editing.

- [ ] **Step 2: Write failing test**

```python
# tests/test_admin_me.py
import pytest
from unittest.mock import MagicMock, patch

from app.routers.admin import AdminUser, get_current_admin


@pytest.mark.asyncio
async def test_get_current_admin_includes_permissions_and_branch():
    user = AdminUser("s1", role="staff", clinic_id="clinic-1", user_id="s1", permissions=["DOCTORS_UPDATE"], branch_id="branch-1")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"staff_role": "DOCTOR_SCHEDULE_MANAGER"}]
    )
    with patch("app.routers.admin.supabase", mock_sb):
        result = await get_current_admin(user=user)

    assert result["permissions"] == ["DOCTORS_UPDATE"]
    assert result["branch_id"] == "branch-1"
    assert result["staff_role"] == "DOCTOR_SCHEDULE_MANAGER"
```

(Replace `get_current_admin` with the actual function name found in Step 1 if it differs.)

- [ ] **Step 3: Run test, verify it fails**

Run: `pytest tests/test_admin_me.py -v`
Expected: FAIL — `KeyError: 'permissions'`

- [ ] **Step 4: Implement**

Merge into the existing return dict (do not replace it — preserve every field already
returned today):

```python
@router.get("/me")
async def get_current_admin(user: AdminUser = Depends(verify_credentials)):
    staff_role = None
    if user.role == "staff" and user.user_id:
        row = (
            supabase.table("clinic_admins")
            .select("staff_role")
            .eq("id", user.user_id)
            .execute()
        )
        if row.data:
            staff_role = row.data[0].get("staff_role")

    return {
        # ...existing fields unchanged...
        "permissions": user.permissions,
        "branch_id": user.branch_id,
        "staff_role": staff_role,
    }
```

- [ ] **Step 5: Run test, verify it passes**

Run: `pytest tests/test_admin_me.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin.py tests/test_admin_me.py
git commit -m "feat(staff): expose permissions, branch_id, staff_role on GET /admin/me"
```

---

### Task 8: Delegate doctor CRUD endpoints

**Files:**
- Modify: `app/routers/admin.py` (`create_doctor`, `update_doctor`, `delete_doctor`)
- Test: `tests/test_admin_doctor_delegation.py` (new)

**Interfaces:**
- Consumes: `require_permission`, `enforce_branch_scope` (Task 4).
- Produces: staff with `DOCTORS_CREATE`/`DOCTORS_UPDATE`/`DOCTORS_DELETE` can call these endpoints; branch-scoped staff are rejected when the target doctor isn't assigned to their branch.

- [ ] **Step 1: Read `create_doctor`, `update_doctor`, `delete_doctor` in full**

Find them via `Grep` for `def create_doctor|def update_doctor|def delete_doctor` in
`app/routers/admin.py` and read each in full (including `_apply_slot_config` call sites) to
capture exact current bodies before editing.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_admin_doctor_delegation.py
import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from app.routers.admin import AdminUser, DoctorCreate, DoctorUpdate, create_doctor, update_doctor, delete_doctor


def _staff(permissions, branch_id=None, clinic_id="clinic-1"):
    return AdminUser("s1", role="staff", clinic_id=clinic_id, user_id="s1", permissions=permissions, branch_id=branch_id)


@pytest.mark.asyncio
async def test_create_doctor_rejects_staff_without_permission():
    body = DoctorCreate(name="Dr. X", specialization="ENT", department="ENT")
    with patch("app.routers.admin.supabase", MagicMock()):
        with pytest.raises(HTTPException) as exc:
            await create_doctor(doctor=body, user=_staff(permissions=[]))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_doctor_allows_staff_with_permission():
    body = DoctorCreate(name="Dr. X", specialization="ENT", department="ENT")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"id": "clinic-1"}]
    )
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1", "name": "Dr. X"}]
    )
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

    with patch("app.routers.admin.supabase", mock_sb):
        result = await create_doctor(doctor=body, user=_staff(permissions=["DOCTORS_CREATE"]))
    assert result["name"] == "Dr. X"


@pytest.mark.asyncio
async def test_update_doctor_rejects_branch_scoped_staff_for_other_branch():
    body = DoctorUpdate(name="Dr. Y")
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"doctor_id": "doc-1", "branch_id": "branch-2"}]
    )
    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await update_doctor(
                doctor_id="doc-1", doctor=body,
                user=_staff(permissions=["DOCTORS_UPDATE"], branch_id="branch-1"),
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_doctor_rejects_staff_without_permission():
    with patch("app.routers.admin.supabase", MagicMock()):
        with pytest.raises(HTTPException) as exc:
            await delete_doctor(doctor_id="doc-1", user=_staff(permissions=[]))
    assert exc.value.status_code == 403
```

(Adapt each test's mock chain to whatever the real function signature/queries turn out to be
from Step 1 — these tests express the required behavior; the exact mock plumbing must match
the real implementation found by reading the file.)

- [ ] **Step 3: Run tests, verify they fail**

Run: `pytest tests/test_admin_doctor_delegation.py -v`
Expected: FAIL — endpoints still gated by `Depends(require_admin)`, so staff with the right
permission still gets rejected (403 for the wrong reason) or the branch check doesn't exist
yet (no 403 raised where one is expected)

- [ ] **Step 4: Implement**

1. Change `create_doctor`'s `user: AdminUser = Depends(require_admin)` →
   `Depends(require_permission("DOCTORS_CREATE"))`.
2. Change `update_doctor`'s dependency → `Depends(require_permission("DOCTORS_UPDATE"))`,
   and immediately after resolving `effective_clinic_id`, add a branch-scope check:

```python
from app.services.permissions import enforce_branch_scope

db = supabase.table("doctor_branches").select("branch_id").eq("doctor_id", doctor_id).execute()
if db.data:
    enforce_branch_scope(user, db.data[0]["branch_id"])
```

3. Change `delete_doctor`'s dependency → `Depends(require_permission("DOCTORS_DELETE"))`
   with the same branch-scope check added before the delete.

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_admin_doctor_delegation.py -v`
Expected: PASS

- [ ] **Step 6: Run full doctor-related regression**

Run: `pytest tests/ -v -k "doctor or staff_role"`
Expected: PASS (no unrelated doctor test broken — e.g. slot generation, department mapping)

- [ ] **Step 7: Commit**

```bash
git add app/routers/admin.py tests/test_admin_doctor_delegation.py
git commit -m "feat(rbac): delegate doctor create/update/delete to permissioned staff, branch-scoped"
```

---

### Task 9: Delegate doctor↔branch assignment endpoints

**Files:**
- Modify: `app/routers/admin.py` (`assign_doctor_to_branch`, `remove_doctor_from_branch`, `update_doctor_branch_session`, `get_branch_doctors`)
- Test: `tests/test_admin_doctor_delegation.py` (append)

**Interfaces:**
- Consumes: `require_permission`, `enforce_branch_scope` (Task 4).
- Produces: staff with `DOCTOR_BRANCH_ASSIGN` can call these; branch-scoped staff limited to `branch_id == user.branch_id` (the path parameter itself, not a DB lookup, since these endpoints take `branch_id` directly).

- [ ] **Step 1: Read `app/routers/admin.py`'s doctor-branch-assignment section in full**

Find via `Grep` for `def assign_doctor_to_branch|def remove_doctor_from_branch|def update_doctor_branch_session|def get_branch_doctors` and read each in full to capture exact current bodies/signatures before editing.

- [ ] **Step 2: Write failing tests**

```python
# append to tests/test_admin_doctor_delegation.py
from app.routers.admin import DoctorBranchAssign, assign_doctor_to_branch, remove_doctor_from_branch


@pytest.mark.asyncio
async def test_assign_doctor_to_branch_rejects_staff_for_other_branch():
    body = DoctorBranchAssign(doctor_id="doc-1", session="both")
    with patch("app.routers.admin.supabase", MagicMock()):
        with pytest.raises(HTTPException) as exc:
            await assign_doctor_to_branch(
                branch_id="branch-2", body=body,
                user=_staff(permissions=["DOCTOR_BRANCH_ASSIGN"], branch_id="branch-1"),
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_assign_doctor_to_branch_allows_staff_for_own_branch():
    body = DoctorBranchAssign(doctor_id="doc-1", session="both")
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"doctor_id": "doc-1", "branch_id": "branch-1", "session": "both"}]
    )
    with patch("app.routers.admin.supabase", mock_sb):
        result = await assign_doctor_to_branch(
            branch_id="branch-1", body=body,
            user=_staff(permissions=["DOCTOR_BRANCH_ASSIGN"], branch_id="branch-1"),
        )
    assert result["branch_id"] == "branch-1"


@pytest.mark.asyncio
async def test_remove_doctor_from_branch_rejects_without_permission():
    with patch("app.routers.admin.supabase", MagicMock()):
        with pytest.raises(HTTPException) as exc:
            await remove_doctor_from_branch(branch_id="branch-1", doctor_id="doc-1", user=_staff(permissions=[]))
    assert exc.value.status_code == 403
```

(Adapt mock chains/return shapes to the real implementation read in Step 1.)

- [ ] **Step 3: Run tests, verify they fail**

Run: `pytest tests/test_admin_doctor_delegation.py -v -k branch`
Expected: FAIL (endpoints still `require_admin`-only)

- [ ] **Step 4: Implement**

Change the dependency on `assign_doctor_to_branch`, `remove_doctor_from_branch`,
`update_doctor_branch_session` from `Depends(require_admin)` to
`Depends(require_permission("DOCTOR_BRANCH_ASSIGN"))`, and add
`enforce_branch_scope(user, branch_id)` as the first line of each handler body (the path
parameter IS the resource's branch here — no DB lookup needed, unlike doctor create/update).
Also swap `get_branch_doctors` (read) to `Depends(require_permission("DOCTOR_BRANCH_ASSIGN"))`
for consistency — read access to branch doctor lists is low-risk; reuse the same permission
rather than inventing an unrequested `_VIEW` variant.

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_admin_doctor_delegation.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin.py tests/test_admin_doctor_delegation.py
git commit -m "feat(rbac): delegate doctor-branch assignment to permissioned, branch-scoped staff"
```

---

### Task 10: Delegate doctor leave endpoints

**Files:**
- Modify: `app/routers/admin.py` (`create_leave`, `delete_leave`)
- Test: `tests/test_admin_holiday_leave_delegation.py` (new)

**Interfaces:**
- Consumes: `require_permission`, `enforce_branch_scope` (Task 4).
- Produces: staff with `DOCTOR_LEAVES_CREATE`/`DOCTOR_LEAVES_DELETE` can call these; branch
  resolved via `doctors.name == leave.doctor_name` → `doctor_branches.branch_id` (leaves are
  keyed by `doctor_name`, not `doctor_id` — existing schema, not changed here).

- [ ] **Step 1: Read `get_leaves`, `create_leave`, `delete_leave` in full**

Find via `Grep` for `def create_leave|def delete_leave` in `app/routers/admin.py` and read
each in full, plus the `LeaveCreate` Pydantic model, before editing.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_admin_holiday_leave_delegation.py
import pytest
from datetime import date
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from app.routers.admin import AdminUser, LeaveCreate, create_leave, delete_leave


def _staff(permissions, branch_id=None, clinic_id="clinic-1"):
    return AdminUser("s1", role="staff", clinic_id=clinic_id, user_id="s1", permissions=permissions, branch_id=branch_id)


@pytest.mark.asyncio
async def test_create_leave_rejects_staff_without_permission():
    body = LeaveCreate(doctor_name="Dr. X", leave_date=date(2026, 9, 1), leave_type="full")
    with patch("app.routers.admin.supabase", MagicMock()):
        with pytest.raises(HTTPException) as exc:
            await create_leave(leave=body, user=_staff(permissions=[]))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_leave_rejects_branch_scoped_staff_for_other_branch_doctor():
    body = LeaveCreate(doctor_name="Dr. X", leave_date=date(2026, 9, 1), leave_type="full")
    mock_sb = MagicMock()
    # doctor lookup by name -> doctor id -> doctor_branches
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1"}]
    )
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"branch_id": "branch-2"}]
    )
    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await create_leave(leave=body, user=_staff(permissions=["DOCTOR_LEAVES_CREATE"], branch_id="branch-1"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_leave_rejects_staff_without_permission():
    with patch("app.routers.admin.supabase", MagicMock()):
        with pytest.raises(HTTPException) as exc:
            await delete_leave(leave_id="l1", user=_staff(permissions=[]))
    assert exc.value.status_code == 403
```

(Adapt mock chains to the real query shapes read in Step 1.)

- [ ] **Step 3: Run tests, verify they fail**

Run: `pytest tests/test_admin_holiday_leave_delegation.py -v -k leave`
Expected: FAIL (endpoints still `require_admin`-only)

- [ ] **Step 4: Implement**

1. Change `create_leave`'s dependency → `Depends(require_permission("DOCTOR_LEAVES_CREATE"))`.
   Immediately after resolving `effective_clinic_id`, for branch-scoped staff only, resolve
   and check:

```python
if user.role == "staff" and user.branch_id:
    doc = supabase.table("doctors").select("id").eq("name", leave.doctor_name).execute()
    if doc.data:
        db = supabase.table("doctor_branches").select("branch_id").eq("doctor_id", doc.data[0]["id"]).execute()
        if db.data:
            from app.services.permissions import enforce_branch_scope
            enforce_branch_scope(user, db.data[0]["branch_id"])
```

2. Change `delete_leave`'s dependency → `Depends(require_permission("DOCTOR_LEAVES_DELETE"))`.
   Before deleting, look up the leave row's `doctor_name` (`.select("doctor_name").eq("id",
   leave_id)`), then apply the same doctor→branch→`enforce_branch_scope` check as in
   `create_leave` for branch-scoped staff, so delete gets identical branch protection to create.

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_admin_holiday_leave_delegation.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin.py tests/test_admin_holiday_leave_delegation.py
git commit -m "feat(rbac): delegate doctor leave create/delete to permissioned, branch-scoped staff"
```

---

### Task 11: Delegate holiday endpoints

**Files:**
- Modify: `app/routers/admin.py` (`create_holiday`, `delete_holiday`)
- Test: `tests/test_admin_holiday_leave_delegation.py` (append)

**Interfaces:**
- Consumes: `require_permission` (Task 4).
- Produces: staff with `HOLIDAYS_CREATE`/`HOLIDAYS_DELETE` can call these — clinic-wide (no
  branch dimension in `hospital_holidays`, per spec's explicit scope decision).

- [ ] **Step 1: Read `get_holidays`, `create_holiday`, `delete_holiday` in full**

Find via `Grep` for `def create_holiday|def delete_holiday` in `app/routers/admin.py` and
read each in full before editing.

- [ ] **Step 2: Write failing tests**

```python
# append to tests/test_admin_holiday_leave_delegation.py
from datetime import date

from app.routers.admin import create_holiday, delete_holiday


@pytest.mark.asyncio
async def test_create_holiday_rejects_staff_without_permission():
    with patch("app.routers.admin.supabase", MagicMock()):
        with pytest.raises(HTTPException) as exc:
            await create_holiday(holiday_date=date(2026, 10, 2), name="Test Holiday", user=_staff(permissions=[]))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_holiday_allows_staff_with_permission():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"id": "clinic-1"}]
    )
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"holiday_date": "2026-10-02", "name": "Test Holiday"}]
    )
    with patch("app.routers.admin.supabase", mock_sb):
        result = await create_holiday(
            holiday_date=date(2026, 10, 2), name="Test Holiday",
            user=_staff(permissions=["HOLIDAYS_CREATE"]),
        )
    assert result["name"] == "Test Holiday"


@pytest.mark.asyncio
async def test_delete_holiday_rejects_staff_without_permission():
    with patch("app.routers.admin.supabase", MagicMock()):
        with pytest.raises(HTTPException) as exc:
            await delete_holiday(holiday_date="2026-10-02", user=_staff(permissions=[]))
    assert exc.value.status_code == 403
```

(Adapt each test's params/mocks to the actual signatures found in Step 1 — e.g. if
`create_holiday`/`delete_holiday` take a Pydantic body instead of query params, construct
that body accordingly.)

- [ ] **Step 3: Run tests, verify they fail**

Run: `pytest tests/test_admin_holiday_leave_delegation.py -v -k holiday`
Expected: FAIL (endpoints still `require_admin`-only)

- [ ] **Step 4: Implement**

Change `create_holiday`'s dependency → `Depends(require_permission("HOLIDAYS_CREATE"))` and
`delete_holiday`'s dependency → `Depends(require_permission("HOLIDAYS_DELETE"))`. No branch
check (holidays are clinic-wide by schema).

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_admin_holiday_leave_delegation.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin.py tests/test_admin_holiday_leave_delegation.py
git commit -m "feat(rbac): delegate holiday create/delete to permissioned staff"
```

---

### Task 12: Frontend — staff creation/edit form (role, branch, extra permissions)

**Files:**
- Modify: `admin/index.html` (staff panel markup, `createStaff()`, `loadStaff()`)

**Interfaces:**
- Consumes: `GET /admin/me` (permissions/branch_id/staff_role — Task 7), `GET /admin/staff`, `POST /admin/staff`, `PUT /admin/staff/{id}` (Tasks 5-6), `GET /admin/branches` (existing).
- Produces: updated staff-management UI section.

- [ ] **Step 1: Read the current staff panel markup and JS in full**

Use `Grep` for `createStaff|loadStaff|toggleStaff` in `admin/index.html` and read the
surrounding markup/script in full before editing, to match existing CSS classes, the
`api()`/`apiPost()`/`apiPut()` helper conventions, and the `msg()`/`toast()`/`esc()` utility
functions already used elsewhere in the file. Also read wherever the existing `/admin/me`
call lives (needed for Task 13 too).

- [ ] **Step 2: Add role/branch/permissions fields to the create-staff form**

Add a `<select id="f-staffRole">` (options: STAFF, RECEPTIONIST, DOCTOR_SCHEDULE_MANAGER,
BRANCH_MANAGER, CUSTOM_ROLE — matching `STAFF_ROLES`), a `<select id="f-staffBranch">`
populated from the existing branches list (`"" = All branches"`), and a checkbox group
`#f-staffExtraPermissions` rendered from the `permissions` array returned by `/admin/me`
(only permissions the logged-in admin already holds are offered — UI reflects backend
authority, doesn't grant it. `clinic_admin`/`super_admin` are shown the full `PERMISSIONS`
list since `require_permission` treats those roles as unconditionally authorized).

- [ ] **Step 3: Update `createStaff()` to submit the new fields**

```javascript
async function createStaff() {
    const username = document.getElementById('f-staffUsername').value.trim();
    const password = document.getElementById('f-staffPassword').value;
    const staff_role = document.getElementById('f-staffRole').value;
    const branch_id = document.getElementById('f-staffBranch').value || null;
    const extra_permissions = Array.from(
        document.querySelectorAll('#f-staffExtraPermissions input:checked')
    ).map(el => el.value);

    if (username.length < 3) { msg('staffMsg', 'Username must be at least 3 characters', true); return; }
    if (!password || password.length < 8) { msg('staffMsg', 'Password must be at least 8 characters', true); return; }

    try {
        await apiPost('/admin/staff', { username, password, staff_role, branch_id, extra_permissions });
        msg('staffMsg', `✅ Staff login created — Username: <strong>${esc(username)}</strong>, Password: <strong>${esc(password)}</strong>. Copy these now and hand them to your staff member.`);
        document.getElementById('f-staffUsername').value = '';
        document.getElementById('f-staffPassword').value = '';
        loadStaff();
    } catch (e) {
        msg('staffMsg', 'Error: ' + e.message, true);
    }
}
```

- [ ] **Step 4: Add an edit action to `loadStaff()`'s row rendering**

Each staff row gets an "Edit" button opening a small inline form (or reusing the create
form in edit mode) pre-filled with the row's `staff_role`/`branch_id`/`permissions`, calling
`apiPut('/admin/staff/' + id, { staff_role, extra_permissions, branch_id })` on save, then
`loadStaff()` again to refresh the row.

- [ ] **Step 5: Manual verification**

Start the app locally per the existing dev workflow, log in as a `clinic_admin`, create a
staff account with `DOCTOR_SCHEDULE_MANAGER` role and a branch, verify the staff list shows
the new columns, edit the account, verify the change persists after reload.

- [ ] **Step 6: Commit**

```bash
git add admin/index.html
git commit -m "feat(staff-ui): add role, branch, and delegated-permission controls to staff form"
```

---

### Task 13: Frontend — permission-gated doctor/holiday/leave controls

**Files:**
- Modify: `admin/index.html` (doctor/holiday/leave panel action buttons and the existing `/admin/me` bootstrap call)

**Interfaces:**
- Consumes: `/admin/me` response's `permissions` array (Task 7), cached in a module-level
  `let currentAdminPermissions = []` populated once on page load by extending the existing
  `/admin/me` call found in Task 12 Step 1 (do not add a second call).

- [ ] **Step 1: Read the current doctor/holiday/leave panel markup and JS in full**

Use `Grep` for the doctor/holiday/leave "Add"/"Edit"/"Delete" button handlers and
`loadDoctors()`/`loadHolidays()`/`loadLeaves()` in `admin/index.html`, and read each
surrounding block in full before editing.

- [ ] **Step 2: Extend the existing `/admin/me` bootstrap call to cache permissions**

```javascript
let currentAdminPermissions = [];
let currentAdminRole = null;

// inside the existing /admin/me handler, after the response is parsed:
currentAdminPermissions = me.permissions || [];
currentAdminRole = me.role;

function hasPermission(perm) {
    return currentAdminRole === 'clinic_admin' || currentAdminRole === 'super_admin' || currentAdminPermissions.includes(perm);
}
```

- [ ] **Step 3: Gate each control**

Wrap each action button's visibility (not just its handler — the design's UI-authorization
section requires the control to not appear, not merely to fail silently on click) in
`hasPermission('DOCTORS_CREATE')` etc., matching the permission each button's underlying
endpoint now requires (Tasks 8-11): doctor Add/Edit → `DOCTORS_CREATE`/`DOCTORS_UPDATE`,
doctor Delete → `DOCTORS_DELETE`, branch-assign controls → `DOCTOR_BRANCH_ASSIGN`, leave
Add/Delete → `DOCTOR_LEAVES_CREATE`/`DOCTOR_LEAVES_DELETE`, holiday Add/Delete →
`HOLIDAYS_CREATE`/`HOLIDAYS_DELETE`.

- [ ] **Step 4: Manual verification**

Log in as a staff account with only `HOLIDAYS_CREATE`; verify doctor Add/Edit/Delete buttons
are hidden but holiday Add is visible; attempt a direct `fetch('/admin/doctors', {method:
'POST', ...})` from the browser console while logged in as that staff account and confirm
the backend still returns 403 (UI hiding is not the enforcement — Task 8 is).

- [ ] **Step 5: Commit**

```bash
git add admin/index.html
git commit -m "feat(staff-ui): gate doctor/holiday/leave management controls by delegated permission"
```

---

### Task 14: Full regression pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -v`
Expected: PASS — every previously-passing test still passes (in particular
`tests/test_rbac.py`, `tests/test_admin_staff_role.py`, `tests/test_admin_staff_accounts.py`,
plus every other existing `tests/test_admin_*.py` and `tests/test_platform*.py` file), plus
every new test file added in Tasks 2-11.

- [ ] **Step 2: Manually verify unaffected flows per the spec's verification plan**

- `clinic_admin`/`super_admin` can still do everything they could before (payments,
  connectors, branches CRUD, profile, bookings, prescriptions — untouched endpoints).
- A `staff` account created before this migration (no `permissions` row value — defaults to
  `'{}'`) still gets 403 on every doctor/holiday/leave/staff-management endpoint until an
  admin explicitly grants permissions — no accidental privilege expansion.
- Front-desk staff flows (check-in, queue call-next, view appointments/patients/lab-reports)
  are unchanged — those endpoints still use plain `verify_credentials`, untouched by this plan.
- WhatsApp patient-facing booking flow is unaffected — no changes to `app/routers/webhook.py`,
  `app/services/conversation.py`, or the WhatsApp-side doctor/slot read path.

- [ ] **Step 3: Report completion**

Summarize per the original request's "Final Report" sections (A-H): changed files, DB
changes, authorization model, frontend changes, backend changes, security verification
performed, regression verification performed, remaining risks (note: production Supabase
migration still needs to be run manually — this plan only adds the `.sql` file, per
existing repo convention of manual migration application).
