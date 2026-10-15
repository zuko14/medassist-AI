# KK-P0-15 hardening checklist — doctor leave endpoints (KK-P4-02, PB-01)

Audited 2026-10-14 by reading the routes in `app/routers/admin.py` and the leave sweep in
`app/services/scheduler.py`. Same columns as [r0-reads.md](r0-reads.md), plus write checks.

| Endpoint | Action | Perm | Tenant | Branch | Bounds | Idempotent / CAS | Audit row | 5xx | Off-loop | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| `GET /admin/leaves?doctor=` | read-back + preview | none | ✔ | doctor names (not PHI) | ✔ 2,000 rows, exact `doctor` match | — | — | ✔ fixed text | ✔ | Used for preview and verify |
| `POST /admin/leaves` | `doctor_leave.create` (R2) | DOCTOR_LEAVES_CREATE (dependency) | ✔ `resolve_clinic_id_for_write` | ✔ pinned staff: doctor must be at their branch (403) | ✔ range ≤ 92 days, else 422 (D-26 fixed; Ketu sends ≤ 31 days) | ✔ dates already on leave are skipped, a half day is widened in place (D-11 fix) — a repeat is harmless | ✔ `create_leave` with skipped/widened lists | ✔ fixed text; 409 names the doctor only | ✔ | Registered |
| `DELETE /admin/leaves/{id}` | `doctor_leave.delete` (R2) | DOCTOR_LEAVES_DELETE (dependency) | ✔ delete scoped to the clinic | ✔ pinned staff: doctor's branch checked | ✔ one id | ✔ deleting a missing id is a no-op (it returns success, so Kriya verifies by read-back, never by the response) | ✔ `delete_leave` | ✔ | ✔ | Registered |
| `GET /admin/appointments` | `doctor_leave.impact` (R0) + create preview | none | ✔ | ✔ `_branch_kw` — a pinned user sees only their branch's bookings, so the preview says its count covers their branch only | ✔ (see r0-reads) | — | — | ✔ | ✔ | Registered |
| `GET /admin/doctors`, `GET /admin/me` | doctor slot, half-day slots, pinned check | none | ✔ | — | ✔ | — | — | ✔ | ✔ | Used |

Observations (not defects): the pinned-staff check in `DELETE /admin/leaves/{id}` reads the leave row by id
without a clinic filter before the clinic-scoped delete; it only compares branches and returns nothing,
so no data crosses tenants. `POST /admin/leaves` cancels today's/tomorrow's bookings in the background
(`process_new_leaves`, same lock as the 08:00 sweep); the leave row itself is what Kriya Ketu verifies.
