-- ============================================================================
-- Migration 099: per-test delivery tracking for MocDoc partial approvals
-- ============================================================================
-- A lab approves 5 of an order's 10 tests; the connector sends those 5 and
-- records the order as processed. The other 5, approved hours later, were
-- never sent: every dedup layer keys on the order id (VAM_ReportNo).
--
-- test_names records which tests each delivered PDF held, so the connector
-- can send only the tests approved since (as a top-up with its own id,
-- VAM_ReportNo_u<hash>). NULL = delivered before this column existed.
--
-- Additive, nullable, no backfill, no lock beyond a metadata-only ALTER.
-- ============================================================================

ALTER TABLE integration_processed_reports
    ADD COLUMN IF NOT EXISTS test_names JSONB;
