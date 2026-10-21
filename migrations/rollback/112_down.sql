-- Rollback Migration 112. Reminder jobs are removed first so the narrowed check can be re-added.
-- Normal rollback is the admin toggle (voice.reminders.enabled = false) + code revert;
-- pre-112 code runs unchanged on a 112 schema.
DROP INDEX IF EXISTS uq_voice_reminder_once;
DELETE FROM voice_outbound_jobs WHERE purpose = 'appointment_reminder';
ALTER TABLE voice_outbound_jobs DROP CONSTRAINT IF EXISTS voice_outbound_jobs_purpose_check;
ALTER TABLE voice_outbound_jobs ADD CONSTRAINT voice_outbound_jobs_purpose_check
    CHECK (purpose IN ('lead_followup', 'callback_request'));
