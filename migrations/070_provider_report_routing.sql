-- ============================================================
-- Migration 070: Provider-based lab report routing (TPA desk delivery)
--
-- Reports booked under an insurance/TPA panel must NOT go to the patient's
-- WhatsApp — they go to the diagnostic centre's TPA desk number, which files
-- the claim. The rule is stored on the MocDoc connector row's JSONB config so
-- it is per-clinic/per-branch and editable from the admin panel:
--
--     report_routing_providers  comma-separated provider names (substring match,
--                               case- and punctuation-insensitive)
--     report_routing_phone      E.164 desk number
--
-- No schema change is required — `integration_connectors.config` is JSONB.
-- This migration only seeds Accumx Diagnostics' TPA panels. It is idempotent
-- and touches nothing else; every other clinic keeps sending to patients.
-- ============================================================

DO $$
DECLARE
    seeded INTEGER;
BEGIN
    UPDATE integration_connectors ic
    SET config = COALESCE(ic.config, '{}'::jsonb) || jsonb_build_object(
            'report_routing_providers',
            'VMSC MEDIBUDDY, MD INDIA TPA, MDINDIA TPA, VMSC VISIT HEALTH TPA, '
            || 'ASSURE TPA, HEALTH ASSURE TPA, QUANTUM CORP HEALTH MUMBAI, '
            || 'VMSC MD INDIA LIC TPA',
            'report_routing_phone', '+919052024418'
        ),
        updated_at = now()
    FROM clinics c
    WHERE c.id = ic.clinic_id
      AND ic.connector_type = 'mocdoc'
      -- The tenant is spelled "Accumx Diagnostics" (no 'a' before the x). An
      -- ILIKE '%accumax%' predicate matched zero rows and silently left the
      -- connectors unrouted, so match the spelling loosely AND pin the id.
      AND (c.name ILIKE '%accum%x%' OR c.id = 'c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c')
      -- Never clobber a number an operator has already set by hand.
      AND COALESCE(ic.config ->> 'report_routing_phone', '') = '';

    GET DIAGNOSTICS seeded = ROW_COUNT;
    RAISE NOTICE 'Migration 070: seeded provider routing on % connector row(s)', seeded;

    IF seeded = 0 THEN
        RAISE NOTICE 'Migration 070: no unconfigured Accumx MocDoc connector found — '
            'set "TPA / Insurance Providers" and "TPA Desk WhatsApp Number" on the '
            'Connectors page of the admin panel instead.';
    END IF;
END $$;

-- ── Record Migration ────────────────────────────────────────────────────────
-- Recorded by scripts/migrate.py
