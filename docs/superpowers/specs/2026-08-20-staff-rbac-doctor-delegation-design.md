# Staff RBAC & Delegated Doctor/Holiday/Schedule Management — Design

## Problem

Today `clinic_admins.role` is a flat 3-value field: `super_admin` / `clinic_admin` / `staff`.
`require_admin` (in `app/routers/admin.py`) 403s *any* `role="staff"` account off every
mutating admin endpoint — staff can only reach read-only + front-desk endpoints gated by
plain `verify_credentials` (check-in, queue call-next, view appointments/patients/lab-reports).

Client Admins have no way to delegate doctor management, holiday management, doctor
schedule/slot/fee editing, or branch-scoped operations to trusted staff. This design adds
a permission system so a Client Admin can grant specific staff specific operational
capabilities, scoped to a branch where relevant, fully enforced server-side.

## Current architecture (relevant facts)

- **Auth**: HTTP Basic, re-verified against `clinic_admins` on every request (no JWT/session
  token). Permission changes take effect on the very next request — no token invalidation
  or cache-busting mechanism needed.
- **Doctors**: one `doctors` row holds identity + specialty + schedule config
  (`morning_start/end`, `evening_start/end`, `slot_duration_minutes`) + `consultation_fee`
  (plain int, no history/versioning — no other admin tier has this either). All edited
  through **one** `POST /admin/doctors` / `PUT /admin/doctors/{id}` form — there is no
  separate schedule, slot, or fee endpoint.
- **Branch assignment**: `doctor_branches` junction table (`doctor_id`, `branch_id`,
  `session`), managed via `/admin/branches/{branch_id}/doctors[...]` endpoints.
- **Holidays** (`hospital_holidays`) and **leaves** (`doctor_leaves`) are both
  clinic-scoped (`clinic_id` column, already multi-tenant-safe) but **not** branch-scoped —
  there is no `branch_id` column on either table today.
- **Audit logging** (`admin_audit_logs` + `log_admin_action()`) already exists and is used
  on the endpoints this design touches.
- Staff creation UI is username/password only.

## Decisions (confirmed with user)

- **Field-level permission splitting on the doctor edit form: rejected (Option A chosen).**
  Doctor schedule/slots/fee are fields on the single doctor edit endpoint — one coarse
  `DOCTORS_UPDATE` permission governs the whole form. `DOCTOR_SCHEDULE_UPDATE` /
  `DOCTOR_SLOTS_UPDATE` / `DOCTOR_FEES_UPDATE` are not created as separately-enforced
  permissions (no payload diffing/partial rejection). Revisit only if a real need for
  fee-only or schedule-only staff editing surfaces.
- **Fee versioning: out of scope.** `consultation_fee` keeps its current overwrite-in-place
  behavior for staff too — no other admin tier has effective-dated fee history today, so
  adding it here would be a new feature for everyone, not a delegation concern.
- **Scope of this pass**: staff/role/permission management + doctor CRUD + doctor↔branch
  assignment + doctor leaves + hospital holidays. Appointment/patient/lab/prescription
  endpoints already work for staff via `verify_credentials` (unaffected) or remain
  `require_admin`-only (unaffected) — not touched in this pass. The permission mechanism
  is generic, so extending it to those areas later is a small follow-up, not a rewrite.
- **Branch scoping applies where the resource is actually branch-linked** (doctors via
  `doctor_branches`, leaves via doctor→branch lookup). Holidays have no branch dimension in
  the schema — a branch-scoped staff member granted `HOLIDAYS_CREATE` acts clinic-wide for
  that action; this is a schema fact, not a security gap, and is called out in the staff
  edit UI copy ("holidays apply to the whole clinic").

## Data model changes

`migrations/036_staff_permissions.sql` — additive, non-destructive:

```sql
ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS staff_role TEXT;
ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS permissions TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id);
CREATE INDEX IF NOT EXISTS idx_clinic_admins_branch ON clinic_admins(branch_id);
```

No backfill of permissions — existing `role='staff'` rows get `permissions = '{}'`
(the default), which reproduces their *current* behavior exactly (they can do nothing
admin-tier today). Nothing silently expands or contracts. Client Admin must explicitly
grant permissions post-upgrade.

Why not a normalized `roles`/`permissions`/`role_permissions` join-table system: each
clinic needs a handful of named presets, not shared custom roles across tenants — one
array column checked in-process avoids extra joins on the already-per-request DB lookup
and is trivially auditable (the resolved grant is stored, not computed at read time).

## Permission set (scoped to real endpoints)

Defined as constants in new `app/services/permissions.py`:

```
DOCTORS_CREATE, DOCTORS_UPDATE, DOCTORS_DELETE       # DOCTORS_VIEW not needed — already open to all logged-in staff
DOCTOR_BRANCH_ASSIGN                                  # assign/reassign/remove doctor <-> branch
DOCTOR_LEAVES_CREATE, DOCTOR_LEAVES_DELETE
HOLIDAYS_CREATE, HOLIDAYS_DELETE
STAFF_VIEW, STAFF_CREATE, STAFF_UPDATE                # manage other staff (capped — see below)
```

### Role presets (`ROLE_PRESETS` in the same module)

| `staff_role`             | Grants |
|---------------------------|--------|
| `STAFF` (default)         | `{}` — identical to today's staff behavior |
| `RECEPTIONIST` / `FRONT_DESK` | `{}` — label only; front-desk ops already work via existing `verify_credentials` endpoints |
| `DOCTOR_SCHEDULE_MANAGER`  | `DOCTORS_UPDATE`, `DOCTOR_BRANCH_ASSIGN`, `DOCTOR_LEAVES_CREATE`, `DOCTOR_LEAVES_DELETE`, `HOLIDAYS_CREATE`, `HOLIDAYS_DELETE` |
| `BRANCH_MANAGER`           | all of the above + `DOCTORS_CREATE`, `DOCTORS_DELETE`, `STAFF_VIEW` |
| `CUSTOM_ROLE`              | `{}` — admin builds the grant entirely from explicit extra permissions |

`APPOINTMENT_MANAGER` / `LAB_OPERATOR` / `PHARMACY_OPERATOR` are accepted as `staff_role`
values (label/UI only, for parity with the requested role list) but resolve to `{}` extra
grants since there's no backend gate for those areas yet — safe default, not a dead end
(same mechanism extends to them later).

Client Admin can add **extra permissions** on top of the preset at staff-creation/edit
time (`role preset ∪ extra_permissions`), never beyond their own authority: a `staff`
account with `STAFF_CREATE` can only grant permissions/branch scope that is a subset of
its own resolved `permissions`, and can never set `staff_role` to anything requiring
`clinic_admin`/`super_admin` tier. Enforced server-side in the create/update handler,
never trusted from the request body alone.

## Backend enforcement

`require_permission(perm: str)` dependency factory in `app/services/permissions.py`:

```python
def require_permission(perm: str):
    # NOTE: depends on verify_credentials, NOT require_admin — require_admin
    # unconditionally 403s every role="staff" account, which would make
    # delegated permissions unreachable.
    async def _dep(user: AdminUser = Depends(verify_credentials)) -> AdminUser:
        if user.role in ("super_admin", "clinic_admin"):
            return user
        if perm not in (user.permissions or []):
            raise HTTPException(403, f"Missing permission: {perm}")
        return user
    return _dep
```

`AdminUser` gains `permissions: list[str]` and `branch_id: Optional[str]`, populated in
`verify_credentials` from the `clinic_admins` row (already fetched there — no extra query).

Branch-scope check (`app/services/permissions.py::enforce_branch_scope`): for staff with
`user.branch_id` set, resolve the target resource's branch (via `doctor_branches` for
doctor/leave writes) and 403 if it doesn't match. `clinic_admin`/`super_admin` are never
branch-restricted (unchanged today).

### Endpoints converted from `Depends(require_admin)` → `Depends(require_permission(...))`

| Endpoint | Permission |
|---|---|
| `POST /admin/doctors` | `DOCTORS_CREATE` |
| `PUT /admin/doctors/{id}` | `DOCTORS_UPDATE` + branch check |
| `DELETE /admin/doctors/{id}` | `DOCTORS_DELETE` + branch check |
| `POST/PUT/DELETE /admin/branches/{branch_id}/doctors[...]` | `DOCTOR_BRANCH_ASSIGN` + `branch_id` must equal staff's own branch if scoped |
| `POST /admin/leaves` | `DOCTOR_LEAVES_CREATE` + branch check (resolve doctor_name → doctor → branch) |
| `DELETE /admin/leaves/{id}` | `DOCTOR_LEAVES_DELETE` + branch check |
| `POST /admin/holidays` | `HOLIDAYS_CREATE` |
| `DELETE /admin/holidays/{date}` | `HOLIDAYS_DELETE` |
| `GET/POST /admin/staff`, new `PUT /admin/staff/{id}` | `STAFF_VIEW` / `STAFF_CREATE` / `STAFF_UPDATE` (still `clinic_admin`+ by default; staff only if explicitly granted, capped to own authority) |

All other existing `require_admin` endpoints (branches CRUD, payments, connectors,
profile, bookings, prescriptions) are **unchanged** — still admin-tier only, matching
today's behavior and the spec's own permission matrix (which doesn't list branch/finance
delegation).

## New/changed endpoints

- `PUT /admin/staff/{id}` (new): edit `staff_role`, `permissions` (extra grants), `branch_id`,
  `is_active`. Validates branch belongs to caller's clinic, caps grants to caller's own
  permission ceiling, audit-logs before/after.
- `POST /admin/staff` (changed): accepts `staff_role`, `extra_permissions: list[str] = []`,
  `branch_id: Optional[str] = None` in addition to existing `username`/`password`. Resolves
  and stores the final `permissions` array server-side.
- `GET /admin/staff` (changed): response includes `staff_role`, `permissions`, `branch_id`.
- `GET /admin/me` (changed): includes `permissions`, `branch_id`, `staff_role` so the
  frontend can gate UI. Backend enforcement is authoritative regardless.

## Audit logging

Reuse existing `log_admin_action()` / `admin_audit_logs` — add calls in: staff create,
staff update (role/permission/branch change — log before/after), staff
activate/deactivate (already logged), doctor create/update/delete (already logged),
doctor-branch assign/unassign (add), leave create/delete (add), holiday create/delete
(add). No new table.

## Frontend (`admin/index.html`)

- Staff creation form: add `staff_role` dropdown, `branch_id` dropdown (optional —
  "All branches"), and an expandable "extra permissions" checkbox list (only showing
  permissions the *current logged-in admin* actually holds, sourced from `/admin/me`).
- Staff list table: show role/branch/permission summary per row; add an "Edit" action
  opening the same form pre-filled, wired to `PUT /admin/staff/{id}`.
- Existing doctor/holiday/leave management panels: no structural change — just wrap the
  action buttons (`Add Doctor`, `Edit`, `Delete`, `Add Holiday`, `Add Leave`, branch-assign
  controls) in permission checks against `window.currentAdminPermissions` (populated from
  `/admin/me` on load), consistent with how the panel already conditionally shows
  clinic-admin-only sections.

## Security verification plan

- Staff without a permission gets 403 on the corresponding endpoint (direct API call, not
  just UI hidden).
- Staff attempts to `PUT /admin/staff/{own_id}` with `permissions` including something
  they don't currently hold → rejected (capped to own ceiling), tested explicitly.
- Staff with `branch_id` set attempts to edit/delete a doctor or leave belonging to a
  different branch, or POST a doctor-branch-assignment with a different `branch_id` →
  403.
- Cross-tenant: staff/doctor/leave/holiday IDs from another clinic → existing
  `enforce_clinic_access`/`resolve_clinic_id_for_write` IDOR checks still apply unchanged;
  new checks are additive on top.
- Existing `clinic_admin`/`super_admin` flows, front-desk staff flows (check-in, queue,
  lab reports), and WhatsApp/patient-facing flows are untouched — re-verify manually
  post-implementation per the plan's test list.

## Out of scope / explicitly deferred

- Field-level permission splitting on the doctor form (fee-only / schedule-only staff).
- Effective-dated/versioned consultation fees.
- Branch-scoping holidays (would require a schema change to `hospital_holidays` — not
  requested independent of this delegation work).
- Delegating appointment/patient/prescription/lab/branch-CRUD/payment/connector
  management to staff (mechanism supports it later; not wired in this pass).
