-- Migration 050: Store the Meta WhatsApp Business Account id per clinic
-- ─────────────────────────────────────────────────────────────────
-- Purpose: Template approval can only be read from the WABA
--          (GET /{waba_id}/message_templates), not from the phone number id.
--          Without config.meta_waba_id, scripts/whatsapp_doctor.py skips step
--          [3] and a PENDING template looks like a healthy account while every
--          lab report delivery silently fails (2026-08-25 outage).
--
--          The id is not discoverable from the token: debug_token returns no
--          target_ids for unrestricted System User tokens, and Meta rejects
--          the whatsapp_business_account field on the phone number node. It
--          has to be entered at onboarding (platform UI / POST /admin/clinics)
--          or backfilled here.
-- ─────────────────────────────────────────────────────────────────

-- Accumx Diagnostics — WABA id read from WhatsApp Manager.
UPDATE clinics
SET config = config || jsonb_build_object('meta_waba_id', '1702889104159864')
WHERE id = 'c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c'
  AND COALESCE(config->>'meta_waba_id', '') = '';

-- Remaining clinics: fill in from WhatsApp Manager > Account tools, e.g.
--   UPDATE clinics
--   SET config = config || jsonb_build_object('meta_waba_id', '<waba id>')
--   WHERE id = '<clinic id>'
--     AND COALESCE(config->>'meta_waba_id', '') = '';
-- or PATCH /admin/clinics/<id> {"meta_waba_id": "<waba id>"}.

-- ─────────────────────────────────────────────────────────────────
-- Rollback:
--   UPDATE clinics SET config = config - 'meta_waba_id';
-- ─────────────────────────────────────────────────────────────────
