-- Migration 048: Distributed Scheduler Job Locks
-- Prevents duplicate job execution across multi-instance deployments (Docker/K8s/Cloud Run)

CREATE TABLE IF NOT EXISTS scheduler_locks (
    job_name TEXT PRIMARY KEY,
    locked_by TEXT NOT NULL,
    locked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduler_locks_expires ON scheduler_locks(expires_at);

-- Enable RLS for scheduler_locks
ALTER TABLE scheduler_locks ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies 
        WHERE tablename = 'scheduler_locks' AND policyname = 'Full access for service_role'
    ) THEN
        CREATE POLICY "Full access for service_role" ON scheduler_locks FOR ALL TO service_role USING (true);
    END IF;
END
$$;

-- RPC function to atomically acquire or refresh a distributed job lock
CREATE OR REPLACE FUNCTION acquire_scheduler_lock(
    p_job_name TEXT,
    p_locked_by TEXT,
    p_lease_seconds INT
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    v_acquired BOOLEAN := FALSE;
BEGIN
    INSERT INTO public.scheduler_locks (job_name, locked_by, locked_at, expires_at)
    VALUES (p_job_name, p_locked_by, NOW(), NOW() + (p_lease_seconds || ' seconds')::INTERVAL)
    ON CONFLICT (job_name) DO UPDATE
    SET locked_by = p_locked_by,
        locked_at = NOW(),
        expires_at = NOW() + (p_lease_seconds || ' seconds')::INTERVAL
    WHERE public.scheduler_locks.expires_at < NOW();

    IF FOUND THEN
        v_acquired := TRUE;
    END IF;

    RETURN v_acquired;
END;
$$;

-- RPC function to release a distributed job lock
CREATE OR REPLACE FUNCTION release_scheduler_lock(
    p_job_name TEXT,
    p_locked_by TEXT
) RETURNS VOID
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
BEGIN
    DELETE FROM public.scheduler_locks
    WHERE job_name = p_job_name AND locked_by = p_locked_by;
END;
$$;

-- Revoke public execution and grant strictly to backend service_role
REVOKE EXECUTE ON FUNCTION acquire_scheduler_lock(TEXT, TEXT, INT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION acquire_scheduler_lock(TEXT, TEXT, INT) TO service_role;

REVOKE EXECUTE ON FUNCTION release_scheduler_lock(TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION release_scheduler_lock(TEXT, TEXT) TO service_role;
