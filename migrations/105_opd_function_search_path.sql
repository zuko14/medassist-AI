-- ============================================================================
-- Migration 105: pin search_path on every OPD function (Supabase lint 0011)
-- A function without a fixed search_path resolves unqualified names through
-- the caller's search_path, so a role able to create objects earlier on that
-- path could shadow a table or function the OPD triggers rely on.
-- No body changes: every OPD function uses only public tables and built-ins
-- (pg_catalog is always searched first). pg_temp goes last so temp objects
-- can never shadow anything. Metadata-only; safe to apply at any time.
-- ============================================================================

ALTER FUNCTION public.opd_next_counter(uuid, text)                          SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_format_number(text, integer, integer, integer)    SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_assign_mrn(uuid, uuid, uuid)                      SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_touch_updated_at()                                SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_purging(uuid)                                     SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_guard_signed_record()                             SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_guard_rx_items()                                  SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_guard_invoice_items()                             SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_recalc_invoice_subtotal()                         SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_guard_invoice()                                   SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_receipt_before_insert()                           SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_receipt_after_insert()                            SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_guard_append_only()                               SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_guard_shift()                                     SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_sign_encounter(uuid, uuid, uuid, uuid, jsonb)     SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_sign_prescription(uuid, uuid, uuid, uuid, jsonb, jsonb, jsonb, jsonb)
                                                                            SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_close_shift(uuid, uuid, uuid, integer, text)      SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_purge_clinic(uuid)                                SET search_path = public, pg_temp;
ALTER FUNCTION public.opd_guard_payment_exception()                         SET search_path = public, pg_temp;

-- Verify: no opd_* function left without a pinned search_path
DO $$
DECLARE v_missing TEXT;
BEGIN
    SELECT string_agg(p.proname, ', ') INTO v_missing
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname LIKE 'opd\_%'
       AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(p.proconfig, '{}')) c WHERE c LIKE 'search_path=%');
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION '105 verify: search_path not pinned on %', v_missing;
    END IF;
END $$;
