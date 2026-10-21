-- ============================================================================
-- Migration 112: AI receptionist appointment reminder calls
-- ============================================================================
-- Kriya calls a patient a configured number of minutes before a confirmed
-- appointment (clinics.config.voice.reminders, edited in the admin panel).
-- Reminder calls reuse voice_outbound_jobs with purpose 'appointment_reminder'.
--
-- context->>'reminder_key' = "<appointment id>|<date>T<time>|<minutes before>".
-- The unique index below makes "one reminder call per appointment slot per
-- offset" a database fact: overlapping sweeps, two workers or a restart can
-- never queue the same reminder twice. A rescheduled appointment has a new
-- key, so it gets its own reminder.
--
-- Additive + backward compatible: the purpose check is only widened; pre-112
-- code never writes the new purpose. Idempotent.
-- Rollback: migrations/rollback/112_down.sql.
-- ============================================================================

DO $$
DECLARE c TEXT;
BEGIN
    -- The 098 check was declared inline, so its name is generated: find it by definition.
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'voice_outbound_jobs'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%purpose%' LOOP
        EXECUTE format('ALTER TABLE voice_outbound_jobs DROP CONSTRAINT %I', c);
    END LOOP;
END $$;

ALTER TABLE voice_outbound_jobs ADD CONSTRAINT voice_outbound_jobs_purpose_check
    CHECK (purpose IN ('lead_followup', 'callback_request', 'appointment_reminder'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_reminder_once
    ON voice_outbound_jobs (clinic_id, (context->>'reminder_key'))
    WHERE purpose = 'appointment_reminder';
