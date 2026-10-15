-- ============================================================================
-- Migration 110: atomic partial writes of clinics.config / branches.config (D-13)
-- ============================================================================
-- Every settings save (voice settings, clinic profile, payment settings, lab
-- collection hours, home collection, dental settings, platform budgets/limits)
-- read the whole config JSON, changed a few keys and wrote the whole object
-- back. Two saves close together, or one save working from the 30-second
-- tenant cache, silently undid each other's changes (lost update).
--
-- kriya_config_apply() applies only the keys a save changed (sets and removals,
-- as JSON paths) inside one UPDATE while the row is locked, so unrelated
-- changes never overwrite each other. The caller (app/services/config_store.py)
-- computes the change from what it read and what it wants to store.
--
-- Tenant: a branch row must belong to p_clinic_id; a clinics row must BE
-- p_clinic_id. Returns the stored config, or NULL when no such row exists.
-- No data changes. Idempotent. Rollback: migrations/rollback/110_down.sql.
-- ============================================================================

CREATE OR REPLACE FUNCTION kriya_config_apply(
    p_table     text,
    p_id        uuid,
    p_clinic_id uuid,
    p_sets      jsonb DEFAULT '[]'::jsonb,   -- [{"path": ["voice", "outbound", "daily_cap"], "value": 30}, ...]
    p_removes   jsonb DEFAULT '[]'::jsonb    -- [["lab_collection"], ...]
) RETURNS jsonb
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    cfg  jsonb;
    item jsonb;
    path text[];
    i    int;
BEGIN
    IF p_table = 'clinics' THEN
        SELECT config INTO cfg FROM clinics WHERE id = p_id AND id = p_clinic_id FOR UPDATE;
    ELSIF p_table = 'branches' THEN
        SELECT config INTO cfg FROM branches WHERE id = p_id AND clinic_id = p_clinic_id FOR UPDATE;
    ELSE
        RAISE EXCEPTION 'kriya_config_apply: unsupported table %', p_table;
    END IF;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF cfg IS NULL OR jsonb_typeof(cfg) <> 'object' THEN
        cfg := '{}'::jsonb;
    END IF;

    FOR item IN SELECT value FROM jsonb_array_elements(COALESCE(p_removes, '[]'::jsonb)) LOOP
        path := ARRAY(SELECT jsonb_array_elements_text(item));
        cfg := cfg #- path;
    END LOOP;

    FOR item IN SELECT value FROM jsonb_array_elements(COALESCE(p_sets, '[]'::jsonb)) LOOP
        path := ARRAY(SELECT jsonb_array_elements_text(item -> 'path'));
        IF COALESCE(array_length(path, 1), 0) = 0 THEN
            RAISE EXCEPTION 'kriya_config_apply: empty path';
        END IF;
        -- jsonb_set creates only the last key: make every parent an object first.
        FOR i IN 1 .. array_length(path, 1) - 1 LOOP
            IF jsonb_typeof(cfg #> path[1:i]) IS DISTINCT FROM 'object' THEN
                cfg := jsonb_set(cfg, path[1:i], '{}'::jsonb, true);
            END IF;
        END LOOP;
        cfg := jsonb_set(cfg, path, item -> 'value', true);
    END LOOP;

    IF p_table = 'clinics' THEN
        UPDATE clinics SET config = cfg WHERE id = p_id;
    ELSE
        UPDATE branches SET config = cfg WHERE id = p_id AND clinic_id = p_clinic_id;
    END IF;
    RETURN cfg;
END $$;

-- Supabase exposes public functions at /rest/v1/rpc to API roles by default: service role only.
DO $$
DECLARE f TEXT := 'kriya_config_apply(text,uuid,uuid,jsonb,jsonb)';
BEGIN
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', f);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', f);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', f);
    END IF;
END $$;
