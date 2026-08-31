-- Migration 067: Server-Side Admin Sessions with Revocation (AUDIT-P1-2)
--
-- HTTP Basic Auth carries the credential on every request and has no
-- server-side state, so there is no way to terminate a session, expire an idle
-- one, or cut off a stolen credential short of rotating the password for
-- everyone. This table gives the admin panel a revocable, expiring session.
--
-- Only the SHA-256 of the token is stored. A database read (or a leaked
-- backup) therefore does not yield a usable session token.
--
-- Idempotent. Safe to run on a live database.

CREATE TABLE IF NOT EXISTS admin_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash TEXT UNIQUE NOT NULL,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    -- Snapshot of the authorization context at login. Deliberately NOT a
    -- foreign key to clinic_admins: the env-credential super admin has no row
    -- there, and a session must survive long enough to be audited even if the
    -- underlying account is deleted.
    clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE,
    user_id TEXT,
    branch_id UUID,
    staff_role TEXT,
    permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    ip_address TEXT,
    user_agent TEXT
);

-- The hot path: every authenticated admin request looks a session up by hash.
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_sessions_token_hash
    ON admin_sessions(token_hash);

-- "Revoke every session for this user" after a password change or offboarding.
CREATE INDEX IF NOT EXISTS idx_admin_sessions_username_active
    ON admin_sessions(username) WHERE revoked_at IS NULL;

-- Expiry sweep.
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires_at
    ON admin_sessions(expires_at);

CREATE INDEX IF NOT EXISTS idx_admin_sessions_clinic_id
    ON admin_sessions(clinic_id) WHERE clinic_id IS NOT NULL;

-- Sessions are never read through the tenant-scoped path; service_role only.
ALTER TABLE admin_sessions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role access for admin_sessions" ON admin_sessions;
CREATE POLICY "Service role access for admin_sessions" ON admin_sessions
    FOR ALL TO service_role USING (true) WITH CHECK (true);
