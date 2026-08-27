-- ============================================================================
-- Create schema_migrations tracking table and back-fill all 56 applied migrations.
-- Run this ONCE in Supabase SQL Editor.
-- ============================================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum VARCHAR(64) NOT NULL DEFAULT 'manual-apply'
);

INSERT INTO schema_migrations (name, checksum) VALUES
    ('001_initial_schema.sql', 'manual-apply'),
    ('002_lab_reports.sql', 'manual-apply'),
    ('002_security_tables.sql', 'manual-apply'),
    ('003_multi_tenant.sql', 'manual-apply'),
    ('004_seed_first_clinic.sql', 'manual-apply'),
    ('005_processed_messages.sql', 'manual-apply'),
    ('006_alter_clinics_plan.sql', 'manual-apply'),
    ('007_data_retention.sql', 'manual-apply'),
    ('008_payments.sql', 'manual-apply'),
    ('009_integration_connectors.sql', 'manual-apply'),
    ('010_branches.sql', 'manual-apply'),
    ('011_clinic_admins.sql', 'manual-apply'),
    ('012_connector_failed_reports.sql', 'manual-apply'),
    ('013_fix_branch_rls.sql', 'manual-apply'),
    ('014_admin_audit_logs.sql', 'manual-apply'),
    ('015_processed_messages_clinic_id.sql', 'manual-apply'),
    ('016_fix_plan_constraint.sql', 'manual-apply'),
    ('017_doctor_slot_config.sql', 'manual-apply'),
    ('018_health_checkin_columns.sql', 'manual-apply'),
    ('019_appointment_queue_tokens.sql', 'manual-apply'),
    ('020_family_members.sql', 'manual-apply'),
    ('021_unique_queue_token.sql', 'manual-apply'),
    ('022_razorpay_payment_link_id.sql', 'manual-apply'),
    ('023_atomic_rate_limit_rpc.sql', 'manual-apply'),
    ('024_webhook_security_events.sql', 'manual-apply'),
    ('025_connector_branch_scope.sql', 'manual-apply'),
    ('026_lab_reports_callmedex_attribution.sql', 'manual-apply'),
    ('027_callmedex_whatsapp_settings.sql', 'manual-apply'),
    ('028_core_tables_rls.sql', 'manual-apply'),
    ('029_doctor_branch_assignment.sql', 'manual-apply'),
    ('030_branch_locality_upgrade.sql', 'manual-apply'),
    ('031_flexible_shift_cleanup.sql', 'manual-apply'),
    ('032_outbound_message_ledger.sql', 'manual-apply'),
    ('033_plan_tiers_and_pricing.sql', 'manual-apply'),
    ('034_broadcasts_notifications_clinic_deletion.sql', 'manual-apply'),
    ('035_fix_multi_tenant_unique_constraints.sql', 'manual-apply'),
    ('036_staff_permissions.sql', 'manual-apply'),
    ('037_lab_reports_status_lifecycle.sql', 'manual-apply'),
    ('038_lab_tests_table.sql', 'manual-apply'),
    ('039_appointments_lab_test_booking.sql', 'manual-apply'),
    ('040_lab_reports_matched_booking.sql', 'manual-apply'),
    ('041_lab_report_delivery_receipts.sql', 'manual-apply'),
    ('042_connector_audit_log_counts.sql', 'manual-apply'),
    ('043_tenant_routing_upgrade.sql', 'manual-apply'),
    ('044_lab_reports_nullable_file_path.sql', 'manual-apply'),
    ('045_lab_reports_retry_tracking.sql', 'manual-apply'),
    ('046_add_refund_columns.sql', 'manual-apply'),
    ('047_durable_inbound_messages.sql', 'manual-apply'),
    ('048_scheduler_locks.sql', 'manual-apply'),
    ('049_force_row_level_security.sql', 'manual-apply'),
    ('050_clinic_meta_waba_id.sql', 'manual-apply'),
    ('051_clinic_admin_scope_constraint.sql', 'manual-apply'),
    ('052_booking_ref_per_tenant.sql', 'manual-apply'),
    ('053_appointments_stale_holds_index.sql', 'manual-apply'),
    ('054_payment_events_and_slot_index.sql', 'manual-apply'),
    ('055_drop_global_booking_ref_constraint.sql', 'manual-apply')
ON CONFLICT (name) DO NOTHING;

-- Verify: should show 56 rows
SELECT count(*) AS total_migrations_recorded,
       'schema_migrations created and populated' AS status
FROM schema_migrations;
