-- ============================================================================
-- Rollback 119: Clinical Documents Engine
-- DESTROYS every recorded consent and issued certificate, and the frozen
-- letterhead / clinical snapshots of signed discharge summaries (the summaries
-- themselves and their signer stay). Take
-- pg_dump -t clinical_consents -t clinical_certificates -t ipd_discharge_summaries FIRST.
-- Refused while any consent or certificate exists: they are legal records
-- pre-119 code cannot show. The normal rollback is a code revert: pre-119 code
-- runs unchanged on 119.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.clinical_consents') IS NOT NULL AND EXISTS (SELECT 1 FROM clinical_consents))
       OR (to_regclass('public.clinical_certificates') IS NOT NULL AND EXISTS (SELECT 1 FROM clinical_certificates)) THEN
        RAISE EXCEPTION '119_down: consents or certificates exist';
    END IF;
END $$;

DROP TABLE IF EXISTS clinical_consents;
DROP TABLE IF EXISTS clinical_certificates;
DROP FUNCTION IF EXISTS docs_issue_certificate(uuid, jsonb, text);
DROP FUNCTION IF EXISTS docs_sign_discharge_summary(uuid, uuid, uuid, uuid, text, jsonb, jsonb);
DROP FUNCTION IF EXISTS docs_freeze_discharge_summary(uuid, uuid, jsonb, jsonb);
DROP FUNCTION IF EXISTS docs_guard_consent();
DROP FUNCTION IF EXISTS docs_guard_certificate();
DROP FUNCTION IF EXISTS docs_purging(uuid);

-- pre-119 definition, copied verbatim from 111_ipd_os_core.sql
CREATE OR REPLACE FUNCTION ipd_guard_discharge_summary() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'draft' OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM ipd_raise('ipd_record_locked:ipd_discharge_summaries');
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.admission_id <> OLD.admission_id OR NEW.version <> OLD.version
       OR NEW.supersedes_id IS DISTINCT FROM OLD.supersedes_id THEN
        PERFORM ipd_raise('ipd_identity_immutable:ipd_discharge_summaries');
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'signed' AND NOT ipd_in_tx(OLD.clinic_id) THEN
            PERFORM ipd_raise('ipd_sign_via_function_only');
        END IF;
        IF NEW.status = 'superseded' THEN PERFORM ipd_raise('ipd_bad_transition:draft->superseded'); END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    IF OLD.status = 'signed' AND NEW.status = 'superseded' AND ipd_in_tx(OLD.clinic_id)
       AND (to_jsonb(NEW) - 'status' - 'updated_at') = (to_jsonb(OLD) - 'status' - 'updated_at') THEN
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    PERFORM ipd_raise('ipd_record_locked:ipd_discharge_summaries');
    RETURN NULL;
END $$;
ALTER FUNCTION ipd_guard_discharge_summary() SET search_path = public, pg_temp;
DROP FUNCTION IF EXISTS docs_in_tx(uuid);

ALTER TABLE ipd_discharge_summaries
    DROP COLUMN IF EXISTS letterhead_snapshot, DROP COLUMN IF EXISTS clinical_snapshot,
    DROP COLUMN IF EXISTS pdf_storage_path, DROP COLUMN IF EXISTS delivery_status,
    DROP COLUMN IF EXISTS whatsapp_message_id, DROP COLUMN IF EXISTS last_sent_at, DROP COLUMN IF EXISTS send_count;
