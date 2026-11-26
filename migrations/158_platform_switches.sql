-- ============================================================================
-- Migration 158: platform-wide owner switches. First switch: 'callmedex' — the
-- master on/off for every connection between CallMedex and Kriya (inbound APIs,
-- report jobs, outbound calls and callbacks, WhatsApp routing of the CallMedex
-- number). Inserted OFF: only the platform owner switches it on, from the owner
-- console (PUT /platform/callmedex/status). Platform-level, no clinic_id.
-- ============================================================================

CREATE TABLE IF NOT EXISTS platform_switches (
    key         TEXT PRIMARY KEY CHECK (key ~ '^[a-z_]{2,40}$'),
    enabled     BOOLEAN NOT NULL DEFAULT false,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NULL
);

INSERT INTO platform_switches (key, enabled, updated_by) VALUES ('callmedex', false, 'migration_158')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE platform_switches ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_switches FORCE ROW LEVEL SECURITY;
DO $$
BEGIN
    DROP POLICY IF EXISTS service_role_all_platform_switches ON platform_switches;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE POLICY service_role_all_platform_switches ON platform_switches FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE platform_switches FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE platform_switches FROM authenticated;
    END IF;
END $$;

-- ── 158 verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM platform_switches WHERE key = 'callmedex') THEN
        RAISE EXCEPTION '158 verify: callmedex switch row missing';
    END IF;
END $$;
