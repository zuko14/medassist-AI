-- ============================================================================
-- Rollback Migration 088: Client data portability + support inbox
-- ============================================================================
-- DESTRUCTIVE: drops every imported patient record and every support message.
-- Export them first (Admin -> Data & Support -> Export) if they must be kept.

ALTER TABLE platform_invoices DROP CONSTRAINT IF EXISTS platform_invoices_storage_addon_nonneg;
ALTER TABLE platform_invoices DROP COLUMN IF EXISTS storage_addon_paise;
DROP TABLE IF EXISTS support_messages CASCADE;
DROP TABLE IF EXISTS patient_records CASCADE;
DROP TABLE IF EXISTS patient_import_batches CASCADE;
