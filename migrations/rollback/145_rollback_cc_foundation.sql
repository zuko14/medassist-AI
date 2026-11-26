-- ============================================================================
-- Rollback 145: Continuous Care foundation (CC-M1)
-- Run the later CC rollbacks first (149 → 146). Refused while a voice job uses purpose 'care_checkin' or a quota row
-- uses kind 'care' (the old CHECKs could not hold them), or while any cc_* row exists: purge each CC clinic first with
-- SELECT cc_purge_clinic('<clinic_id>') (owner action). The normal rollback is a code revert.
-- ============================================================================

CREATE OR REPLACE FUNCTION pg_temp.kriya_has_rows(t TEXT, w TEXT) RETURNS BOOLEAN LANGUAGE plpgsql AS $f$
DECLARE r BOOLEAN;
BEGIN
    IF to_regclass('public.' || t) IS NULL THEN RETURN false; END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I %s)', t, COALESCE(w, '')) INTO r;
    RETURN r;
END $f$;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM voice_outbound_jobs WHERE purpose = 'care_checkin') THEN
        RAISE EXCEPTION '145_down: care_checkin voice jobs exist';
    END IF;
    IF EXISTS (SELECT 1 FROM clinic_message_quota_usage WHERE kind = 'care') THEN
        RAISE EXCEPTION '145_down: care quota rows exist';
    END IF;
    IF (pg_temp.kriya_has_rows('cc_consents', NULL))
       OR (pg_temp.kriya_has_rows('cc_protocol_activations', NULL)) THEN
        RAISE EXCEPTION '145_down: cc rows exist; run cc_purge_clinic for each CC clinic first';
    END IF;
END $$;

DROP FUNCTION IF EXISTS cc_purge_clinic(uuid);
DROP TABLE IF EXISTS cc_cards;
DROP TABLE IF EXISTS cc_consents;
-- kriya_guard_refs() belongs to migration 144 (its rollback drops it); 145 never drops it
DROP TABLE IF EXISTS cc_protocol_activations;
DROP INDEX IF EXISTS idx_voice_jobs_care;
ALTER TABLE voice_outbound_jobs DROP CONSTRAINT IF EXISTS voice_outbound_jobs_purpose_check;
ALTER TABLE voice_outbound_jobs ADD CONSTRAINT voice_outbound_jobs_purpose_check
    CHECK (purpose IN ('lead_followup', 'callback_request', 'appointment_reminder'));
ALTER TABLE clinic_message_quota_usage DROP CONSTRAINT IF EXISTS clinic_message_quota_usage_kind_check;
ALTER TABLE clinic_message_quota_usage ADD CONSTRAINT clinic_message_quota_usage_kind_check
    CHECK (kind IN ('patient', 'doctor', 'review'));
DROP INDEX IF EXISTS idx_clinics_cc_on;
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_cc_settings_object, DROP CONSTRAINT IF EXISTS clinics_cc_state_check,
    DROP COLUMN IF EXISTS cc_settings, DROP COLUMN IF EXISTS cc_state;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '145_cc_foundation.sql';
