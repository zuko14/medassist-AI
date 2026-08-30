-- ============================================================================
-- Migration 063: Appointment completion lifecycle (unblocks patient follow-ups)
--
-- `appointments.status` has allowed 'completed' since migration 001 and
-- analytics counts it, but NO code path ever wrote it. SchedulerService.
-- send_followups() filters on status='completed', so the post-visit follow-up
-- -- Kriya AI's main differentiator over a plain chatbot -- matched zero rows
-- every single day since launch.
--
-- completed_at records when the auto-complete job closed the visit, so a
-- follow-up offset can be measured from the visit rather than re-derived.
-- ============================================================================

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

-- The auto-complete job scans for confirmed appointments whose date has passed.
CREATE INDEX IF NOT EXISTS idx_appointments_pending_completion
    ON appointments(clinic_id, appointment_date)
    WHERE status = 'confirmed';

-- The follow-up job scans a short lookback window of completed visits that
-- have not been followed up yet.
CREATE INDEX IF NOT EXISTS idx_appointments_followup_due
    ON appointments(clinic_id, appointment_date)
    WHERE status = 'completed' AND followup_sent = FALSE;

-- ── Record migration ──
-- NOTE: schema_migrations is written by scripts/migrate.py:124 with the
-- file's SHA256. checksum is NOT NULL (scripts/migrate.py:60), so a
-- self-INSERT here omits it and aborts the migration on any fresh
-- database. Migrations must not record themselves.

