-- ============================================================================
-- Migration 128: Kriya Diagnostic OS collection and rejection (DX-M4)
-- Rejection reasons, doorstep money (field collections), the label print log,
-- the sample guard, and the functions that move a tube: bind a pre-printed
-- label, collect, reject (+ recollect), receive at the same site, store;
-- record / hand over doorstep cash.
--
-- Design: docs/diagnostic-os/DATA-MODEL.md §5, ADR-DX-05, AUDIT A7/A8/A14/A28.
-- Additive: three new tables, new functions, one new trigger on dx_samples,
-- dx_purge_clinic v4. Nothing an OPD / IPD / non-DX clinic uses changes.
-- ============================================================================

-- ── 1. Tables ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_rejection_reasons (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    code                  TEXT NOT NULL CHECK (code ~ '^[A-Z_]{3,30}$'),
    label                 TEXT NOT NULL CHECK (char_length(btrim(label)) BETWEEN 2 AND 120),
    requires_recollection BOOLEAN NOT NULL DEFAULT true,
    is_active             BOOLEAN NOT NULL DEFAULT true,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_rejection_reasons_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_rejection_reasons_clinic_code_key UNIQUE (clinic_id, code)
);

-- money a phlebotomist took at the doorstep (AUDIT A14); a receipt only at handover
CREATE TABLE IF NOT EXISTS dx_field_collections (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    accession_id      UUID NOT NULL,
    amount_paise      INTEGER NOT NULL CHECK (amount_paise > 0 AND amount_paise <= 100000000),
    mode              TEXT NOT NULL CHECK (mode IN ('cash', 'upi_to_clinic')),
    reference         TEXT NULL CHECK (char_length(reference) <= 80),
    collected_by      UUID NOT NULL,
    collected_by_name TEXT NOT NULL CHECK (char_length(btrim(collected_by_name)) BETWEEN 1 AND 100),
    collected_at      TIMESTAMPTZ NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending_handover'
                      CHECK (status IN ('pending_handover', 'handed_over', 'disputed')),
    receipt_id        UUID NULL,
    handed_over_to    UUID NULL,
    handed_over_at    TIMESTAMPTZ NULL,
    dispute_note      TEXT NULL CHECK (char_length(dispute_note) <= 500),
    idempotency_key   TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_field_collections_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_field_collections_idem_key UNIQUE (clinic_id, idempotency_key),
    CONSTRAINT dx_field_collections_upi_ref CHECK (mode = 'cash' OR reference IS NOT NULL),
    CONSTRAINT dx_field_collections_handover_shape CHECK (
        (status = 'handed_over') = (receipt_id IS NOT NULL AND handed_over_at IS NOT NULL)),
    CONSTRAINT dx_field_collections_accession_fk FOREIGN KEY (clinic_id, accession_id)
        REFERENCES dx_accessions (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_dx_field_collections_status ON dx_field_collections (clinic_id, status, collected_at);
CREATE INDEX IF NOT EXISTS idx_dx_field_collections_accession ON dx_field_collections (clinic_id, accession_id);

-- every print and reprint (mislabel investigations, AUDIT A28); append-only
CREATE TABLE IF NOT EXISTS dx_label_prints (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    accession_id    UUID NOT NULL,
    sample_ids      UUID[] NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('per_sample', 'per_test', 'register')),
    copies          SMALLINT NOT NULL DEFAULT 1 CHECK (copies BETWEEN 1 AND 10),
    is_reprint      BOOLEAN NOT NULL DEFAULT false,
    printed_by      UUID NULL,
    printed_by_name TEXT NOT NULL CHECK (char_length(btrim(printed_by_name)) BETWEEN 1 AND 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_label_prints_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_label_prints_accession_fk FOREIGN KEY (clinic_id, accession_id) REFERENCES dx_accessions (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_dx_label_prints_accession ON dx_label_prints (clinic_id, accession_id, created_at);

-- ── 2. Guards ──────────────────────────────────────────────────────────────
-- Field collections change only inside dx_* functions (the 127 guard).
DROP TRIGGER IF EXISTS trg_dx_field_collections_guard ON dx_field_collections;
CREATE TRIGGER trg_dx_field_collections_guard BEFORE INSERT OR UPDATE OR DELETE ON dx_field_collections
    FOR EACH ROW EXECUTE FUNCTION dx_guard_tx();

CREATE OR REPLACE FUNCTION dx_guard_append_only() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND dx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
    PERFORM dx_raise('dx_append_only:' || TG_TABLE_NAME);
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_dx_label_prints_guard ON dx_label_prints;
CREATE TRIGGER trg_dx_label_prints_guard BEFORE UPDATE OR DELETE ON dx_label_prints
    FOR EACH ROW EXECUTE FUNCTION dx_guard_append_only();

-- Sample guard (DATA-MODEL §5): status / barcode / collection / receipt / rejection
-- columns move only inside dx_* functions; a barcode never changes once set; rows go
-- only on purge. Kept even though trg_dx_samples_guard covers whole-row writes today.
CREATE OR REPLACE FUNCTION dx_guard_sample() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_clinic UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
BEGIN
    IF dx_purging(v_clinic) THEN RETURN COALESCE(NEW, OLD); END IF;
    IF TG_OP = 'DELETE' THEN PERFORM dx_raise('dx_managed:dx_samples'); END IF;
    IF OLD.barcode IS NOT NULL AND NEW.barcode IS DISTINCT FROM OLD.barcode THEN
        PERFORM dx_raise('dx_barcode_immutable');
    END IF;
    IF NOT dx_in_tx(v_clinic) AND (
         (NEW.status, NEW.barcode, NEW.collected_at, NEW.collected_by, NEW.collected_by_name, NEW.collection_mode,
          NEW.received_at, NEW.received_by, NEW.received_by_name, NEW.received_site_id,
          NEW.rejection_reason_code, NEW.rejection_notes, NEW.rejected_at, NEW.rejected_by)
         IS DISTINCT FROM
         (OLD.status, OLD.barcode, OLD.collected_at, OLD.collected_by, OLD.collected_by_name, OLD.collection_mode,
          OLD.received_at, OLD.received_by, OLD.received_by_name, OLD.received_site_id,
          OLD.rejection_reason_code, OLD.rejection_notes, OLD.rejected_at, OLD.rejected_by)) THEN
        PERFORM dx_raise('dx_managed:dx_samples');
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;
DROP TRIGGER IF EXISTS trg_dx_guard_sample ON dx_samples;
CREATE TRIGGER trg_dx_guard_sample BEFORE UPDATE OR DELETE ON dx_samples
    FOR EACH ROW EXECUTE FUNCTION dx_guard_sample();

-- ── 3. Rejection reasons: defaults per DX clinic ───────────────────────────
CREATE OR REPLACE FUNCTION dx_seed_rejection_reasons(p_clinic_id UUID) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE n INTEGER;
BEGIN
    INSERT INTO dx_rejection_reasons (clinic_id, code, label, requires_recollection)
    SELECT p_clinic_id, r.code, r.label, r.recollect FROM (VALUES
        ('HEMOLYSED', 'Haemolysed sample', true), ('CLOTTED', 'Clotted sample', true),
        ('LIPEMIC', 'Lipaemic sample', true), ('INSUFFICIENT_QTY', 'Insufficient quantity', true),
        ('WRONG_CONTAINER', 'Wrong container', true), ('UNLABELLED', 'Unlabelled tube', true),
        ('MISLABELLED', 'Mislabelled tube', true), ('LEAKED', 'Leaked in transit', true),
        ('TRANSIT_DELAY', 'Transit delay beyond stability', true),
        ('TEMPERATURE_BREACH', 'Temperature breach', true), ('OTHER', 'Other', true)) AS r (code, label, recollect)
    ON CONFLICT (clinic_id, code) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;
SELECT dx_seed_rejection_reasons(id) FROM clinics WHERE dx_state IN ('ACTIVE', 'WIND_DOWN');

-- ── 4. Helpers ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION dx_sample_json(p_clinic_id UUID, p_sample_id UUID) RETURNS JSONB LANGUAGE sql STABLE AS $$
    SELECT to_jsonb(s) || jsonb_build_object('container_code', c.code, 'container_name', c.name,
                                             'colour_hex', c.colour_hex)
      FROM dx_samples s JOIN dx_containers c ON c.clinic_id = s.clinic_id AND c.id = s.container_id
     WHERE s.clinic_id = p_clinic_id AND s.id = p_sample_id
$$;

-- What the patient still owes on a patient-billed visit: unbilled shares of active
-- items plus the balance of issued bills, less doorstep money awaiting handover.
CREATE OR REPLACE FUNCTION dx_visit_due(p_clinic_id UUID, p_accession_id UUID) RETURNS INTEGER LANGUAGE sql STABLE AS $$
    SELECT (COALESCE((SELECT sum(i.patient_share_paise) FROM dx_accession_items i
                       WHERE i.clinic_id = p_clinic_id AND i.accession_id = p_accession_id AND i.status = 'active'
                         AND NOT EXISTS (SELECT 1 FROM opd_invoice_items l JOIN opd_invoices v
                                           ON v.clinic_id = l.clinic_id AND v.id = l.invoice_id
                                          WHERE l.clinic_id = i.clinic_id AND l.dx_accession_item_id = i.id
                                            AND v.status NOT IN ('draft', 'void'))), 0)
          + COALESCE((SELECT sum(GREATEST(0, v.total_paise - v.credited_paise - v.paid_paise)) FROM opd_invoices v
                       WHERE v.clinic_id = p_clinic_id AND v.accession_id = p_accession_id
                         AND v.status IN ('issued', 'partially_paid')), 0)
          - COALESCE((SELECT sum(f.amount_paise) FROM dx_field_collections f
                       WHERE f.clinic_id = p_clinic_id AND f.accession_id = p_accession_id
                         AND f.status = 'pending_handover'), 0))::INTEGER
$$;

-- ── 5. Bind a pre-printed / external label ─────────────────────────────────
CREATE OR REPLACE FUNCTION dx_bind_barcode(p_clinic_id UUID, p_sample_id UUID, p_barcode TEXT, p_actor_id UUID,
                                           p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE s dx_samples; v_code TEXT := btrim(COALESCE(p_barcode, ''));
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF v_code !~ '^[A-Za-z0-9._/-]{3,64}$' THEN PERFORM dx_raise('dx_barcode_invalid'); END IF;
    SELECT * INTO s FROM dx_samples WHERE clinic_id = p_clinic_id AND id = p_sample_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_sample_not_found'); END IF;
    IF s.barcode = v_code THEN RETURN dx_sample_json(p_clinic_id, s.id); END IF;       -- replay
    IF s.barcode_source = 'generated' OR s.barcode IS NOT NULL THEN PERFORM dx_raise('dx_barcode_already_set'); END IF;
    IF s.status <> 'pending_collection' THEN PERFORM dx_raise('dx_sample_not_pending'); END IF;
    -- shortcut: no dx_barcode_stock range check (dx_barcode_not_issued) until DX-M8 adds the stock table
    IF EXISTS (SELECT 1 FROM dx_samples WHERE clinic_id = p_clinic_id AND barcode = v_code) THEN
        PERFORM dx_raise('dx_barcode_in_use');
    END IF;
    UPDATE dx_samples SET barcode = v_code WHERE clinic_id = p_clinic_id AND id = s.id;
    INSERT INTO dx_events (clinic_id, accession_id, sample_id, kind, actor_id, actor_name, details)
    VALUES (p_clinic_id, s.accession_id, s.id, 'barcode_bound', p_actor_id, left(p_actor_name, 100),
            jsonb_build_object('barcode', v_code));
    RETURN dx_sample_json(p_clinic_id, s.id);
END $$;

-- ── 6. Collect ─────────────────────────────────────────────────────────────
-- p_samples: [{id | sample_id, barcode?, volume_ml?}]. A barcode given for a tube that
-- already has one must match it (scan check); for an unbound pre-printed tube it binds.
CREATE OR REPLACE FUNCTION dx_collect_samples(p_clinic_id UUID, p_accession_id UUID, p_samples JSONB,
                                              p_collection_mode TEXT, p_actor_id UUID, p_actor_name TEXT,
                                              p_idempotency_key TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a dx_accessions; r JSONB; s dx_samples; v_sid UUID; v_code TEXT; v_ids UUID[] := '{}';
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF NULLIF(p_idempotency_key, '') IS NULL THEN PERFORM dx_raise('dx_idempotency_required'); END IF;
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_visit_not_found'); END IF;
    IF EXISTS (SELECT 1 FROM dx_events WHERE clinic_id = p_clinic_id AND accession_id = a.id AND kind = 'collected'
                                         AND details ->> 'idempotency_key' = p_idempotency_key) THEN
        RETURN dx_visit_json(p_clinic_id, a.id) || jsonb_build_object('replayed', true);
    END IF;
    IF a.status = 'cancelled' THEN PERFORM dx_raise('dx_visit_closed'); END IF;
    IF COALESCE(p_collection_mode, '') NOT IN ('centre', 'home', 'ward', 'partner') THEN
        PERFORM dx_raise('dx_collection_mode_invalid');
    END IF;
    IF jsonb_typeof(p_samples) IS DISTINCT FROM 'array' OR jsonb_array_length(p_samples) = 0 THEN
        PERFORM dx_raise('dx_no_samples');
    END IF;
    IF a.billing_mode = 'patient'
       AND COALESCE((SELECT (dx_settings ->> 'collect_before_payment')::BOOLEAN FROM clinics WHERE id = p_clinic_id), false)
       AND dx_visit_due(p_clinic_id, a.id) > 0 THEN
        PERFORM dx_raise('dx_awaiting_payment');
    END IF;
    FOR r IN SELECT * FROM jsonb_array_elements(p_samples) LOOP
        v_sid := NULLIF(COALESCE(r ->> 'id', r ->> 'sample_id'), '')::UUID;
        SELECT * INTO s FROM dx_samples WHERE clinic_id = p_clinic_id AND id = v_sid AND accession_id = a.id FOR UPDATE;
        IF NOT FOUND THEN PERFORM dx_raise('dx_sample_not_found'); END IF;
        IF s.status <> 'pending_collection' OR s.id = ANY (v_ids) THEN PERFORM dx_raise('dx_sample_not_pending'); END IF;
        v_code := NULLIF(btrim(COALESCE(r ->> 'barcode', '')), '');
        IF v_code IS NOT NULL AND s.barcode IS NOT NULL AND v_code <> s.barcode THEN
            PERFORM dx_raise('dx_barcode_mismatch');
        END IF;
        IF s.barcode IS NULL THEN
            IF v_code IS NULL THEN PERFORM dx_raise('dx_barcode_required'); END IF;
            PERFORM dx_bind_barcode(p_clinic_id, s.id, v_code, p_actor_id, p_actor_name);
            s.barcode := v_code;
        END IF;
        UPDATE dx_samples SET status = 'collected', collected_at = now(), collected_by = p_actor_id,
                              collected_by_name = left(p_actor_name, 100), collection_mode = p_collection_mode,
                              volume_ml = COALESCE(NULLIF(r ->> 'volume_ml', '')::NUMERIC, volume_ml)
         WHERE clinic_id = p_clinic_id AND id = s.id;
        UPDATE opd_orders SET status = 'sample_collected', specimen_barcode = s.barcode, sample_collected_at = now(),
                              sample_collected_by = p_actor_id, sample_collected_by_name = left(p_actor_name, 100)
         WHERE clinic_id = p_clinic_id AND dx_sample_id = s.id AND status IN ('ordered', 'billed');
        v_ids := v_ids || s.id;
    END LOOP;
    IF a.status = 'registered' THEN
        UPDATE dx_accessions SET status = 'in_process' WHERE clinic_id = p_clinic_id AND id = a.id;
    END IF;
    PERFORM dx_log(p_clinic_id, a.id, 'collected', p_actor_id, p_actor_name,
                   jsonb_build_object('idempotency_key', p_idempotency_key, 'sample_ids', to_jsonb(v_ids),
                                      'mode', p_collection_mode));
    RETURN dx_visit_json(p_clinic_id, a.id) || jsonb_build_object('replayed', false);
END $$;

-- ── 7. Reject (+ recollect), ADR-DX-05 ─────────────────────────────────────
CREATE OR REPLACE FUNCTION dx_reject_sample(p_clinic_id UUID, p_sample_id UUID, p_reason_code TEXT, p_notes TEXT,
                                            p_recollect BOOLEAN, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a dx_accessions; s dx_samples; rr dx_rejection_reasons; o opd_orders; v_new UUID; v_seq INTEGER;
        v_code TEXT; v_n INTEGER := 0;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO s FROM dx_samples WHERE clinic_id = p_clinic_id AND id = p_sample_id;
    IF NOT FOUND THEN PERFORM dx_raise('dx_sample_not_found'); END IF;
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = s.accession_id FOR UPDATE;  -- visit -> tube
    SELECT * INTO s FROM dx_samples WHERE clinic_id = p_clinic_id AND id = p_sample_id FOR UPDATE;
    IF s.status = 'rejected' THEN                                                   -- double click: same result
        RETURN dx_visit_json(p_clinic_id, a.id) || jsonb_build_object('replayed', true);
    END IF;
    IF s.status NOT IN ('collected', 'in_transit', 'received', 'stored') THEN PERFORM dx_raise('dx_sample_not_rejectable'); END IF;
    SELECT * INTO rr FROM dx_rejection_reasons WHERE clinic_id = p_clinic_id AND code = upper(btrim(p_reason_code))
                                                 AND is_active;
    IF NOT FOUND THEN PERFORM dx_raise('dx_rejection_reason_unknown'); END IF;
    IF rr.code = 'OTHER' AND char_length(btrim(COALESCE(p_notes, ''))) < 3 THEN PERFORM dx_raise('dx_rejection_notes_required'); END IF;
    -- a signed result is amended, never rejected away
    IF EXISTS (SELECT 1 FROM opd_orders o2 WHERE o2.clinic_id = p_clinic_id AND o2.dx_sample_id = s.id
                 AND (o2.status IN ('resulted', 'completed')
                      OR EXISTS (SELECT 1 FROM lab_results lr WHERE lr.clinic_id = o2.clinic_id AND lr.order_id = o2.id
                                                                AND lr.status IN ('validated', 'amended')))) THEN
        PERFORM dx_raise('dx_sample_results_signed');
    END IF;
    UPDATE dx_samples SET status = 'rejected', rejection_reason_code = rr.code,
                          rejection_notes = left(NULLIF(btrim(p_notes), ''), 500), rejected_at = now(), rejected_by = p_actor_id
     WHERE clinic_id = p_clinic_id AND id = s.id;
    IF p_recollect THEN
        -- a recollection takes a fresh sequence (AUDIT A7); a pre-printed / external tube waits for its new label
        IF s.barcode_source = 'generated' THEN
            v_seq := dx_next_sample_seq(p_clinic_id, a.accession_year);
            SELECT code INTO v_code FROM dx_containers WHERE clinic_id = p_clinic_id AND id = s.container_id;
        END IF;
        INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode, barcode_source, tube_seq,
                                collection_site_id, processing_site_id, recollect_of)
        VALUES (p_clinic_id, a.id, s.container_id, s.sample_type,
                CASE WHEN v_seq IS NULL THEN NULL
                     ELSE lpad((a.accession_year % 100)::TEXT, 2, '0') || v_code || lpad(v_seq::TEXT, 8, '0') END,
                s.barcode_source, v_seq, s.collection_site_id, s.processing_site_id, s.id)
        RETURNING id INTO v_new;
    END IF;
    FOR o IN SELECT * FROM opd_orders WHERE clinic_id = p_clinic_id AND dx_sample_id = s.id
                                         AND status IN ('ordered', 'billed', 'sample_collected', 'in_progress')
              ORDER BY created_at, id LOOP
        UPDATE opd_orders SET status = 'cancelled', cancelled_at = now(), cancelled_by = p_actor_id,
                              cancellation_reason = 'sample_rejected:' || rr.code
         WHERE clinic_id = p_clinic_id AND id = o.id;
        IF v_new IS NOT NULL THEN
            INSERT INTO opd_orders (clinic_id, branch_id, source, accession_id, accession_item_id, dx_sample_id,
                                    replaces_order_id, patient_id, family_member_id, order_type, catalog_type,
                                    catalog_reference_id, item_name, sample_type, urgency, clinical_notes,
                                    unit_price_paise, ordered_by, ordered_by_name)
            VALUES (p_clinic_id, o.branch_id, 'dx', o.accession_id, o.accession_item_id, v_new, o.id, o.patient_id,
                    o.family_member_id, o.order_type, o.catalog_type, o.catalog_reference_id, o.item_name, o.sample_type,
                    o.urgency, o.clinical_notes, 0, p_actor_id, left(COALESCE(p_actor_name, 'system'), 100));
        END IF;
        v_n := v_n + 1;
    END LOOP;
    INSERT INTO dx_events (clinic_id, accession_id, sample_id, kind, actor_id, actor_name, details)
    VALUES (p_clinic_id, a.id, s.id, 'rejected', p_actor_id, left(p_actor_name, 100),
            jsonb_build_object('reason_code', rr.code, 'reason', rr.label, 'notes', p_notes, 'orders_cancelled', v_n,
                               'replacement_sample_id', v_new));
    RETURN dx_visit_json(p_clinic_id, a.id)
        || jsonb_build_object('replayed', false, 'rejected_sample_id', s.id, 'replacement_sample_id', v_new,
                              'reason_label', rr.label);
END $$;

-- ── 8. Receive at the same site, store ─────────────────────────────────────
CREATE OR REPLACE FUNCTION dx_receive_sample(p_clinic_id UUID, p_barcode TEXT, p_site_id UUID, p_actor_id UUID,
                                             p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE s dx_samples;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO s FROM dx_samples WHERE clinic_id = p_clinic_id AND barcode = btrim(p_barcode) FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_sample_not_found'); END IF;
    IF s.status = 'received' AND s.received_site_id = p_site_id THEN
        RETURN dx_sample_json(p_clinic_id, s.id) || jsonb_build_object('replayed', true);
    END IF;
    IF s.status <> 'collected' THEN PERFORM dx_raise('dx_sample_not_collected'); END IF;
    -- direct receipt only where the tube was drawn at the lab that runs it (other sites: P5 batches)
    IF s.processing_site_id IS DISTINCT FROM p_site_id OR s.collection_site_id IS DISTINCT FROM p_site_id THEN
        PERFORM dx_raise('dx_sample_wrong_site');
    END IF;
    UPDATE dx_samples SET status = 'received', received_at = now(), received_by = p_actor_id,
                          received_by_name = left(p_actor_name, 100), received_site_id = p_site_id
     WHERE clinic_id = p_clinic_id AND id = s.id;
    INSERT INTO dx_events (clinic_id, accession_id, sample_id, kind, actor_id, actor_name, details)
    VALUES (p_clinic_id, s.accession_id, s.id, 'received', p_actor_id, left(p_actor_name, 100),
            jsonb_build_object('site_id', p_site_id, 'direct', true));
    RETURN dx_sample_json(p_clinic_id, s.id) || jsonb_build_object('replayed', false);
END $$;

CREATE OR REPLACE FUNCTION dx_mark_sample_stored(p_clinic_id UUID, p_sample_id UUID, p_location TEXT, p_actor_id UUID,
                                                 p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE s dx_samples; v_loc TEXT := NULLIF(btrim(COALESCE(p_location, '')), '');
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF v_loc IS NULL OR char_length(v_loc) > 80 THEN PERFORM dx_raise('dx_storage_location_invalid'); END IF;
    SELECT * INTO s FROM dx_samples WHERE clinic_id = p_clinic_id AND id = p_sample_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_sample_not_found'); END IF;
    IF s.status NOT IN ('received', 'stored') THEN PERFORM dx_raise('dx_sample_not_received'); END IF;
    IF s.status = 'stored' AND s.storage_location = v_loc THEN RETURN dx_sample_json(p_clinic_id, s.id); END IF;
    UPDATE dx_samples SET status = 'stored', storage_location = v_loc WHERE clinic_id = p_clinic_id AND id = s.id;
    INSERT INTO dx_events (clinic_id, accession_id, sample_id, kind, actor_id, actor_name, details)
    VALUES (p_clinic_id, s.accession_id, s.id, 'stored', p_actor_id, left(p_actor_name, 100),
            jsonb_build_object('location', v_loc));
    RETURN dx_sample_json(p_clinic_id, s.id);
END $$;

-- ── 9. Doorstep money (AUDIT A14) ──────────────────────────────────────────
CREATE OR REPLACE FUNCTION dx_record_field_collection(p_clinic_id UUID, p_accession_id UUID, p_amount_paise INTEGER,
                                                      p_mode TEXT, p_reference TEXT, p_actor_id UUID, p_actor_name TEXT,
                                                      p_idempotency_key TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a dx_accessions; f dx_field_collections;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF NULLIF(p_idempotency_key, '') IS NULL THEN PERFORM dx_raise('dx_idempotency_required'); END IF;
    IF p_actor_id IS NULL THEN PERFORM dx_raise('dx_actor_required'); END IF;
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_visit_not_found'); END IF;
    SELECT * INTO f FROM dx_field_collections WHERE clinic_id = p_clinic_id AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF f.accession_id <> a.id THEN PERFORM dx_raise('dx_idempotency_reused'); END IF;
        RETURN to_jsonb(f) || jsonb_build_object('replayed', true);
    END IF;
    IF a.status = 'cancelled' THEN PERFORM dx_raise('dx_visit_closed'); END IF;
    IF a.billing_mode <> 'patient' THEN PERFORM dx_raise('dx_not_patient_billing'); END IF;
    IF p_amount_paise IS NULL OR p_amount_paise <= 0 OR p_amount_paise > dx_visit_due(p_clinic_id, a.id) THEN
        PERFORM dx_raise('dx_field_amount_over_due');
    END IF;
    INSERT INTO dx_field_collections (clinic_id, accession_id, amount_paise, mode, reference, collected_by,
                                      collected_by_name, collected_at, idempotency_key)
    VALUES (p_clinic_id, a.id, p_amount_paise, p_mode, NULLIF(btrim(p_reference), ''), p_actor_id,
            left(COALESCE(p_actor_name, 'phlebotomist'), 100), now(), p_idempotency_key)
    RETURNING * INTO f;
    PERFORM dx_log(p_clinic_id, a.id, 'field_collection', p_actor_id, p_actor_name,
                   jsonb_build_object('field_collection_id', f.id, 'amount_paise', f.amount_paise, 'mode', f.mode));
    RETURN to_jsonb(f) || jsonb_build_object('replayed', false);
END $$;

-- The receipt is created first by opd_billing.create_receipt (key 'fc:<id>') on the
-- cashier's shift; this stamps it. Re-running after a crash in between heals it.
CREATE OR REPLACE FUNCTION dx_handover_field_collection(p_clinic_id UUID, p_id UUID, p_receipt_id UUID,
                                                        p_actor_id UUID, p_actor_name TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE f dx_field_collections; rc RECORD;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO f FROM dx_field_collections WHERE clinic_id = p_clinic_id AND id = p_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_field_collection_not_found'); END IF;
    IF f.status = 'handed_over' THEN
        IF f.receipt_id <> p_receipt_id THEN PERFORM dx_raise('dx_field_collection_handed_over'); END IF;
        RETURN to_jsonb(f) || jsonb_build_object('replayed', true);
    END IF;
    IF f.status <> 'pending_handover' THEN PERFORM dx_raise('dx_field_collection_disputed'); END IF;
    SELECT r.amount_paise, r.idempotency_key, v.accession_id INTO rc
      FROM opd_receipts r JOIN opd_invoices v ON v.clinic_id = r.clinic_id AND v.id = r.invoice_id
     WHERE r.clinic_id = p_clinic_id AND r.id = p_receipt_id;
    IF NOT FOUND OR rc.accession_id IS DISTINCT FROM f.accession_id OR rc.amount_paise <> f.amount_paise
       OR rc.idempotency_key IS DISTINCT FROM 'fc:' || f.id THEN
        PERFORM dx_raise('dx_handover_receipt_invalid');
    END IF;
    UPDATE dx_field_collections SET status = 'handed_over', receipt_id = p_receipt_id, handed_over_to = p_actor_id,
                                    handed_over_at = now()
     WHERE clinic_id = p_clinic_id AND id = f.id
    RETURNING * INTO f;
    PERFORM dx_log(p_clinic_id, f.accession_id, 'field_collection_handed_over', p_actor_id, p_actor_name,
                   jsonb_build_object('field_collection_id', f.id, 'receipt_id', p_receipt_id, 'amount_paise', f.amount_paise));
    RETURN to_jsonb(f) || jsonb_build_object('replayed', false);
END $$;

-- ── 10. Purge v4 (adds label prints, field collections, rejection reasons) ──
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    DELETE FROM dx_label_prints WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_field_collections WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('field_collections', n);
    DELETE FROM opd_orders WHERE clinic_id = p_clinic_id AND source = 'dx'; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('orders', n);
    DELETE FROM dx_events WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_samples WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accession_items WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accession_bookings WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accessions WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('visits', n);
    UPDATE family_members SET dx_b2b_account_id = NULL WHERE clinic_id = p_clinic_id AND dx_b2b_account_id IS NOT NULL;
    DELETE FROM dx_referrers WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_rejection_reasons WHERE clinic_id = p_clinic_id;
    PERFORM set_config('kriya.opd_purge_clinic', '', true);
    DELETE FROM dx_test_components WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('components', n);
    DELETE FROM dx_signatories WHERE clinic_id = p_clinic_id;
    UPDATE lab_tests SET department_id = NULL, container_id = NULL
     WHERE clinic_id = p_clinic_id AND (department_id IS NOT NULL OR container_id IS NOT NULL);
    DELETE FROM dx_containers  WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_departments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_sites WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('sites', n);
    UPDATE clinics SET dx_state = 'OFF' WHERE id = p_clinic_id;
    PERFORM set_config('kriya.dx_purge_clinic', '', true);
    RETURN v;
END $$;

-- ── 11. RLS parity + API-role lockout ──────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_rejection_reasons', 'dx_field_collections', 'dx_label_prints']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_all_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                           'service_role_all_' || t, t);
        END IF;
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'tenant_isolation_' || t, t);
        EXECUTE format($p$CREATE POLICY %I ON %I FOR ALL
                         USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
                         WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)$p$,
                       'tenant_isolation_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
        END IF;
    END LOOP;
END $$;
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'public' AND p.proname LIKE 'dx\_%'
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', r.sig);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', r.sig);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', r.sig);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', r.sig);
        END IF;
        EXECUTE format('ALTER FUNCTION %s SET search_path = public, pg_temp', r.sig);
    END LOOP;
END $$;

-- ── 12. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_tables WHERE schemaname = 'public'
          AND tablename IN ('dx_rejection_reasons', 'dx_field_collections', 'dx_label_prints')) <> 3 THEN
        RAISE EXCEPTION '128 verify: DX collection tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_dx_guard_sample', 'trg_dx_field_collections_guard',
          'trg_dx_label_prints_guard')) <> 3 THEN
        RAISE EXCEPTION '128 verify: DX collection guards missing';
    END IF;
    IF to_regprocedure('dx_collect_samples(uuid,uuid,jsonb,text,uuid,text,text)') IS NULL
       OR to_regprocedure('dx_reject_sample(uuid,uuid,text,text,boolean,uuid,text)') IS NULL
       OR to_regprocedure('dx_bind_barcode(uuid,uuid,text,uuid,text)') IS NULL
       OR to_regprocedure('dx_receive_sample(uuid,text,uuid,uuid,text)') IS NULL
       OR to_regprocedure('dx_mark_sample_stored(uuid,uuid,text,uuid,text)') IS NULL
       OR to_regprocedure('dx_record_field_collection(uuid,uuid,integer,text,text,uuid,text,text)') IS NULL
       OR to_regprocedure('dx_handover_field_collection(uuid,uuid,uuid,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '128 verify: DX collection functions missing';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname IN ('dx_rejection_reasons', 'dx_field_collections', 'dx_label_prints')
                 AND relkind = 'r' AND NOT (relrowsecurity AND relforcerowsecurity)) THEN
        RAISE EXCEPTION '128 verify: RLS not forced on DX collection tables';
    END IF;
END $$;
