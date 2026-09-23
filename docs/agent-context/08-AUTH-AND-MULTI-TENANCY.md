# 08 - AUTHENTICATION & MULTI-TENANCY ARCHITECTURE

This document details the identity verification, role-based authorization, branch scoping, and multi-tenant isolation mechanisms implemented in KriyaAI.

---

## 1. THE CENTRAL MULTI-TENANCY FACT: APPLICATION-LEVEL ENFORCEMENT

> [!CAUTION]
> **CRITICAL ARCHITECTURAL FACT**: The backend application connects to Supabase PostgreSQL using `SUPABASE_SERVICE_ROLE_KEY`. In PostgreSQL, `service_role` possesses the `BYPASSRLS` attribute. Therefore, **all PostgreSQL Row Level Security (RLS) policies (such as those in migration `049_force_row_level_security.sql`) are completely bypassed by backend queries.**
>
> Multi-tenant data isolation in KriyaAI is **100% enforced in application code**. If an application query omits `.eq("clinic_id", clinic_id)` on a tenant-owned table, the database returns all tenants' rows.

---

## 2. TENANT-OWNED TABLES & VALIDATION PRIMITIVES

Defined in [`app/tenancy.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/tenancy.py):

### `TENANT_OWNED_TABLES` (Frozenset of 30 Tables)
`appointments`, `patients`, `lab_reports`, `lab_tests`, `doctors`, `branches`, `doctor_leaves`, `hospital_holidays`, `clinic_admins`, `integration_connectors`, `connector_failed_reports`, `conversations`, `inbound_messages`, `processed_messages`, `family_members`, `payment_events`, `failed_messages`, `prescriptions`, `prescription_reminder_sends`, `broadcasts`, `admin_notifications`, `outbound_message_ledger`, `connector_audit_log`, `integration_processed_reports`, `analytics_events`, `clinic_daily_usage`, `specialty_treatments`, `treatment_doctors`, `ai_usage_ledger`, `catalogue_import_previews`, `weekly_insights_summaries`.

*(Note: `doctor_branches` is deliberately excluded as it is a pure junction table without a `clinic_id` column; its isolation is transitive through `doctor_id` and `branch_id`).*

### Sentinel Value Blacklist (`_NON_SCOPES`)
```python
_NON_SCOPES = ("default", "none", "null", "", "all", "system", "*", "undefined")
```
- **Historical Context (Incident 2026-09-01)**: The sentinel `"default"` historically meant "no clinic specified". Treating it as a wildcard caused an incident where a `super_admin` deleting doctors inadvertently deleted rows across all tenant clinics.
- **Rule**: `is_valid_clinic_scope(clinic_id)` returns `False` for any sentinel in `_NON_SCOPES`. An invalid scope must raise an HTTP 400/403 error and **must never default to an unfiltered query**.

---

## 3. TENANT RESOLUTION ENGINE (WHATSAPP INBOUND)

- **File**: [`app/services/tenant.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/tenant.py)
- **Function**: `resolve_tenant(display_phone_number, phone_number_id)`

### Resolution Cascade (Strict Priority Order)
1. **`phone_number_id` Lookup (Primary)**: Queries `clinics.phone_number_id` or `clinics.config->'meta_phone_number_id'`. This is an immutable Meta internal ID and is unaffected by number formatting variations.
2. **Normalized Phone Lookup (Secondary)**: Normalizes incoming recipient phone to E.164 (`_normalize_e164`) and queries `clinics.whatsapp_number`.
3. **Sandbox Fallback (Testing)**: Routes test and demo phone numbers to the designated sandbox clinic (`is_sandbox=True`).
4. **Single-Tenant Fallback**: If and only if the database contains exactly **one** active clinic, routes incoming traffic to that clinic.
5. **Zero-Clinic Bootstrap**: If the database contains **zero** clinics, constructs a synthetic in-memory clinic using environment variables (`SETTINGS.hospital_name`, `SETTINGS.meta_phone_number_id`).

### Failure Behavior
- **Fail-Closed**: If more than one active clinic exists and the receiving number cannot be resolved, the function raises `TenantNotFound`. The incoming message is dropped, and no tenant data is touched.
- **Cache TTL**: In-memory LRU cache (`OrderedDict`) capped at 2,000 entries with a 30-second TTL (`CACHE_TTL_SECONDS`).

---

## 4. CLINIC ADMIN AUTHENTICATION & ACCESS CONTROL

- **File**: [`app/routers/admin.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/admin.py)

### Authentication Flow
1. **Credentials**: Stored in `admin_users` table with password hashes generated via `bcrypt.hashpw(password, bcrypt.gensalt(rounds=12))`.
2. **Rate Limiting**: `_check_login_rate_limit()` tracks failed login attempts by username and IP address, enforcing temporary lockouts on brute-force attempts.
3. **Session Tokens**: Successful authentication generates a cryptographically random 32-byte hex token stored in table `admin_sessions(token, user_id, clinic_id, expires_at)`.
4. **Session Lifetime**: 24 hours. The token is transmitted via HTTP-only cookie (`admin_session`) or Bearer header (`Authorization: Bearer <token>`).

### Role Hierarchy & Permissions
- `super_admin`: Platform engineer; can access any clinic but **must explicitly specify `?clinic_id=<uuid>`**. Cannot execute wildcard cross-tenant queries.
- `admin`: Clinic administrative owner; full access within their assigned `clinic_id` (doctors, pricing, staff, settings, connectors, finances).
- `staff`: Front-desk operator; limited to patient bookings, queue tokens, check-in, and lab report handling. Blocked from financial and configuration routes.
- `doctor`: Medical practitioner; access to assigned patient appointments, queue call-next, and prescriptions.
- `receptionist`: Check-in and queue token assignment.

### Enforcement Helper: `enforce_clinic_access(user, requested_clinic_id)`
```python
# Pseudo-logic implemented in admin.py
if user.role != "super_admin" and user.clinic_id != requested_clinic_id:
    raise HTTPException(status_code=403, detail="Tenant boundary violation attempt")
if user.role == "super_admin" and not is_valid_clinic_scope(requested_clinic_id):
    raise HTTPException(status_code=400, detail="No clinic selected. Super-admin must pass ?clinic_id=<uuid>")
return requested_clinic_id
```

### Multi-Branch Scoping: `enforce_branch_scope(user, branch_id)`
- If a staff member has an assigned `user.branch_id`:
  - They are strictly restricted from viewing or altering appointments, doctors, leaves, or operating hours belonging to other branches within the same clinic.
  - Attempting to pass another `branch_id` raises HTTP 403.

---

## 5. PLATFORM OWNER AUTHENTICATION

- **File**: [`app/routers/platform.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/platform.py)
- **Dependency**: `verify_owner_credentials`
- **Mechanism**: Dedicated HTTP Basic Authentication checking `settings.owner_username` and `settings.owner_password`.
- **Isolation from Clinic Admin**: Platform owner authentication is **completely independent** from `admin_users`. A clinic admin cannot access `/platform/*` endpoints, and owner credentials cannot be used on `/admin/*` routes without an explicitly specified clinic tenant.
- **Fail-Safe**: If `OWNER_USERNAME` or `OWNER_PASSWORD` is unconfigured, all `/platform/*` endpoints return HTTP 503 Service Unavailable.
