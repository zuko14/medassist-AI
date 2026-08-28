# Tenant Isolation Architecture — KA-08

## Current Architecture (Post-Remediation)

### Application Boundary (Primary)

The running MediAssist AI application connects to Supabase using the
`service_role` key, which has `BYPASSRLS` privileges. This means:

> **Row-Level Security policies in the database do NOT protect against
> application-layer tenant isolation failures.**

Tenant isolation is enforced by the Python application layer:

1. **`AdminUser.can_access_clinic()`** — every admin API endpoint verifies
   the authenticated user's `clinic_id` against the target resource's
   `clinic_id` before executing queries.

2. **`scoped_query()` / `.eq("clinic_id", ...)`** — every database query that
   returns tenant-owned data includes a `clinic_id` predicate.

3. **Webhook routing** — inbound webhooks (Meta, Razorpay) resolve the tenant
   from the URL path or verified signature before processing.

4. **Scheduler scoping** — scheduled jobs (prescription reminders, hold
   expiry) iterate per-clinic and scope all queries.

### Database Boundary (Secondary / Defense-in-Depth)

RLS policies exist in migrations 049/050 and protect against:

- Direct Supabase dashboard queries by non-service-role users
- Any future integration that uses a scoped API key
- PostgREST access via the `anon` or `authenticated` roles

These policies are NOT the primary application boundary.

### Strategic Future Architecture

**Option A** (Phase 3+ initiative): Migrate to a `kriya_app` database role
that does NOT have `BYPASSRLS`. This would make RLS the primary boundary,
providing database-level tenant isolation even if the application layer has
a bug.

This requires:
- Creating and configuring the `kriya_app` role
- Setting `app.current_clinic_id` session variable on each connection
- Updating all RLS policies to use `current_setting('app.current_clinic_id')`
- Comprehensive migration testing
- Connection pool configuration changes

**Do not partially implement Option A.** Either the database role is fully
functional with RLS as the primary boundary, or the application layer
remains the primary boundary with RLS as defense-in-depth.

## Verification

The test suite includes a comprehensive tenant isolation matrix that
verifies every tenant-owned resource query includes a `clinic_id`
predicate. See `tests/test_tenant_isolation_matrix.py`.
