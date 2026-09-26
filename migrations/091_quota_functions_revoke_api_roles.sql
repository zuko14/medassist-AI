-- ============================================================================
-- Migration 091: Close the quota RPCs to the public API roles
-- ============================================================================
-- Migration 089 created reserve_message_quota / release_message_quota as
-- SECURITY DEFINER and revoked EXECUTE from PUBLIC. On Supabase that is not
-- enough: the platform's default privileges ALSO grant EXECUTE on every new
-- function in `public` to `anon` and `authenticated` directly, so anyone with
-- the public anon key could call /rest/v1/rpc/reserve_message_quota and
-- inflate or reset a clinic's dental message counters (Supabase linter
-- 0028 / 0029).
--
-- Only the backend calls these, with the service_role key. Revoke the API
-- roles explicitly and make sure service_role keeps EXECUTE.
-- Idempotent; safe with the current build running (the app uses service_role).
-- ============================================================================

REVOKE ALL ON FUNCTION public.reserve_message_quota(UUID, TEXT, TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.release_message_quota(UUID, TEXT, TEXT) FROM PUBLIC;

DO $$
DECLARE r TEXT;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON FUNCTION public.reserve_message_quota(UUID, TEXT, TEXT, INTEGER) FROM %I', r);
            EXECUTE format('REVOKE ALL ON FUNCTION public.release_message_quota(UUID, TEXT, TEXT) FROM %I', r);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION public.reserve_message_quota(UUID, TEXT, TEXT, INTEGER) TO service_role;
        GRANT EXECUTE ON FUNCTION public.release_message_quota(UUID, TEXT, TEXT) TO service_role;
    END IF;
END $$;
