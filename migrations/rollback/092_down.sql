-- Rollback Migration 092: WhatsApp Leads. The 'lead_interest' analytics rows are
-- left in place (ordinary analytics_events rows; the 12-month purge removes them).
DROP FUNCTION IF EXISTS public.admin_whatsapp_leads(UUID, TEXT, TEXT, TEXT, INTEGER, INTEGER, INTEGER);
DROP INDEX IF EXISTS idx_analytics_lead_interest;
