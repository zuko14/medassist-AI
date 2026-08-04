-- Migration 015: Attribute processed_messages to a clinic for platform-wide
-- message-volume analytics (Platform Owner Dashboard, /platform/activity).
--
-- Nullable + ON DELETE SET NULL so existing rows and any in-flight deploy
-- that hasn't picked up the clinic_id-aware acquire() code path are unaffected.

ALTER TABLE processed_messages
    ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_processed_messages_clinic_id ON processed_messages(clinic_id);
