-- ============================================================================
-- Migration 129: Kriya Diagnostic OS logistics and outsourcing (DX-M5)
-- Reference-lab partners, routing rules (which site or partner runs a test),
-- transit batches with a manifest, and the functions that move tubes between
-- sites: create / add / dispatch / receive. Registration (dx_insert_lines)
-- now resolves a destination per test and splits tubes by destination;
-- rejection carries the destination to the replacement tube.
--
-- Design: docs/diagnostic-os/DATA-MODEL.md §6, ADR-DX-12, WORKFLOWS W6 / W13.
-- Additive: four new tables, two NULL columns (opd_orders, dx_samples), new
-- functions, dx_insert_lines / dx_reject_sample / dx_purge_clinic (v5)
-- replaced. With no routing rule the destination is exactly the pre-129 one,
-- so nothing an OPD / IPD / non-DX clinic (or a DX clinic without rules) sees changes.
-- ============================================================================

-- ── 1. Tables ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_outsource_partners (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    name                TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 120),
    legal_name          TEXT NULL CHECK (char_length(legal_name) <= 200),
    address             TEXT NULL CHECK (char_length(address) <= 500),
    registration_no     TEXT NULL CHECK (char_length(registration_no) <= 80),
    nabl_certificate_no TEXT NULL CHECK (char_length(nabl_certificate_no) <= 80),
    contact_phone       TEXT NULL CHECK (char_length(contact_phone) <= 20),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_outsource_partners_clinic_id_id_key UNIQUE (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_outsource_partners_name ON dx_outsource_partners (clinic_id, lower(btrim(name)));

-- precedence (ADR-DX-12): test rule > department rule > site default; then a rule for
-- this site before an any-site rule; then the lowest priority number
CREATE TABLE IF NOT EXISTS dx_routing_rules (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    from_site_id         UUID NULL,                     -- NULL = any collection site
    lab_test_id          UUID NULL,
    department_id        UUID NULL,                     -- both NULL = the site default
    to_site_id           UUID NULL,
    outsource_partner_id UUID NULL,
    priority             SMALLINT NOT NULL DEFAULT 100 CHECK (priority BETWEEN 1 AND 999),
    is_active            BOOLEAN NOT NULL DEFAULT true,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_routing_rules_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_routing_rules_match_shape CHECK (num_nonnulls(lab_test_id, department_id) <= 1),
    CONSTRAINT dx_routing_rules_target_shape CHECK (num_nonnulls(to_site_id, outsource_partner_id) = 1),
    CONSTRAINT dx_routing_rules_from_fk FOREIGN KEY (clinic_id, from_site_id) REFERENCES dx_sites (clinic_id, id),
    CONSTRAINT dx_routing_rules_test_fk FOREIGN KEY (clinic_id, lab_test_id)
        REFERENCES lab_tests (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT dx_routing_rules_department_fk FOREIGN KEY (clinic_id, department_id)
        REFERENCES dx_departments (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT dx_routing_rules_to_fk FOREIGN KEY (clinic_id, to_site_id) REFERENCES dx_sites (clinic_id, id),
    CONSTRAINT dx_routing_rules_partner_fk FOREIGN KEY (clinic_id, outsource_partner_id)
        REFERENCES dx_outsource_partners (clinic_id, id)
);
-- two live rules that would tie (same site, same match, same priority) are refused
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_routing_rules_tie ON dx_routing_rules (clinic_id,
    COALESCE(from_site_id, '00000000-0000-0000-0000-000000000000'::UUID),
    COALESCE(lab_test_id, '00000000-0000-0000-0000-000000000000'::UUID),
    COALESCE(department_id, '00000000-0000-0000-0000-000000000000'::UUID), priority) WHERE is_active;

CREATE TABLE IF NOT EXISTS dx_batches (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    batch_year           SMALLINT NOT NULL,
    batch_seq            INTEGER NOT NULL CHECK (batch_seq > 0),
    batch_number         TEXT GENERATED ALWAYS AS (opd_format_number('TRN-', batch_year, batch_seq, 5)) STORED,
    from_site_id         UUID NOT NULL,
    to_site_id           UUID NULL,
    outsource_partner_id UUID NULL,
    status               TEXT NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open', 'dispatched', 'received', 'received_with_discrepancy', 'cancelled')),
    carrier_name         TEXT NULL CHECK (char_length(carrier_name) <= 100),
    dispatch_temp_c      NUMERIC(4,1) NULL CHECK (dispatch_temp_c BETWEEN -90 AND 60),
    receipt_temp_c       NUMERIC(4,1) NULL CHECK (receipt_temp_c BETWEEN -90 AND 60),
    dispatched_at        TIMESTAMPTZ NULL,
    dispatched_by        UUID NULL,
    received_at          TIMESTAMPTZ NULL,
    received_by          UUID NULL,
    notes                TEXT NULL CHECK (char_length(notes) <= 1000),
    created_by           UUID NULL,
    created_by_name      TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_batches_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_batches_number_key UNIQUE (clinic_id, batch_year, batch_seq),
    CONSTRAINT dx_batches_target_shape CHECK (num_nonnulls(to_site_id, outsource_partner_id) = 1),
    CONSTRAINT dx_batches_from_fk FOREIGN KEY (clinic_id, from_site_id) REFERENCES dx_sites (clinic_id, id),
    CONSTRAINT dx_batches_to_fk FOREIGN KEY (clinic_id, to_site_id) REFERENCES dx_sites (clinic_id, id),
    CONSTRAINT dx_batches_partner_fk FOREIGN KEY (clinic_id, outsource_partner_id)
        REFERENCES dx_outsource_partners (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_dx_batches_out ON dx_batches (clinic_id, from_site_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dx_batches_in ON dx_batches (clinic_id, to_site_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS dx_batch_samples (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id     UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    batch_id      UUID NOT NULL,
    sample_id     UUID NOT NULL,
    receipt       TEXT NULL CHECK (receipt IN ('received', 'missing', 'damaged')),
    receipt_notes TEXT NULL CHECK (char_length(receipt_notes) <= 500),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),     -- manifest in scan order
    CONSTRAINT dx_batch_samples_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_batch_samples_pair_key UNIQUE (clinic_id, batch_id, sample_id),
    CONSTRAINT dx_batch_samples_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES dx_batches (clinic_id, id)
        ON DELETE CASCADE,
    CONSTRAINT dx_batch_samples_sample_fk FOREIGN KEY (clinic_id, sample_id) REFERENCES dx_samples (clinic_id, id)
);
-- a tube is in at most one live (not yet received) batch
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_sample_one_live_batch ON dx_batch_samples (clinic_id, sample_id)
    WHERE receipt IS NULL;

-- the destination of a tube and of each order on it (W13: outsourced => processing site NULL)
ALTER TABLE dx_samples ADD COLUMN IF NOT EXISTS outsource_partner_id UUID NULL;
ALTER TABLE dx_samples DROP CONSTRAINT IF EXISTS dx_samples_partner_fk;
ALTER TABLE dx_samples ADD CONSTRAINT dx_samples_partner_fk FOREIGN KEY (clinic_id, outsource_partner_id)
    REFERENCES dx_outsource_partners (clinic_id, id);
ALTER TABLE dx_samples DROP CONSTRAINT IF EXISTS dx_samples_destination_shape;
ALTER TABLE dx_samples ADD CONSTRAINT dx_samples_destination_shape
    CHECK (num_nonnulls(processing_site_id, outsource_partner_id) <= 1);
ALTER TABLE opd_orders ADD COLUMN IF NOT EXISTS outsource_partner_id UUID NULL;
ALTER TABLE opd_orders DROP CONSTRAINT IF EXISTS opd_orders_dx_partner_fk;
ALTER TABLE opd_orders ADD CONSTRAINT opd_orders_dx_partner_fk FOREIGN KEY (clinic_id, outsource_partner_id)
    REFERENCES dx_outsource_partners (clinic_id, id);

-- ── 2. Guards and touch triggers ───────────────────────────────────────────
-- Batches and manifests change only inside dx_* functions (the 127 guard); partners and
-- rules are configuration, written by the service.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_batches', 'dx_batch_samples'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_guard', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION dx_guard_tx()',
                       'trg_' || t || '_guard', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['dx_outsource_partners', 'dx_routing_rules', 'dx_batches'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_touch', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()',
                       'trg_' || t || '_touch', t);
    END LOOP;
END $$;

-- ── 3. Routing resolution (ADR-DX-12) ──────────────────────────────────────
-- {to_site_id, outsource_partner_id, rule_id, matched: test|department|site|fallback|none}.
-- Fallback (no rule): the registration site if it processes, else the clinic's only
-- processing site — exactly the pre-129 behaviour. Rules to an inactive target are skipped.
CREATE OR REPLACE FUNCTION dx_resolve_routing(p_clinic_id UUID, p_from_site_id UUID, p_test_id UUID,
                                              p_department_id UUID) RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE r dx_routing_rules; v_site UUID; v_dept UUID := p_department_id;
BEGIN
    IF v_dept IS NULL AND p_test_id IS NOT NULL THEN
        SELECT department_id INTO v_dept FROM lab_tests WHERE clinic_id = p_clinic_id AND id = p_test_id;
    END IF;
    SELECT x.* INTO r FROM dx_routing_rules x
      LEFT JOIN dx_sites ts ON ts.clinic_id = x.clinic_id AND ts.id = x.to_site_id
      LEFT JOIN dx_outsource_partners op ON op.clinic_id = x.clinic_id AND op.id = x.outsource_partner_id
     WHERE x.clinic_id = p_clinic_id AND x.is_active
       AND (x.from_site_id IS NULL OR x.from_site_id = p_from_site_id)
       AND ((x.lab_test_id = p_test_id) OR (x.lab_test_id IS NULL AND x.department_id = v_dept)
            OR (x.lab_test_id IS NULL AND x.department_id IS NULL))
       AND COALESCE(ts.is_active AND ts.kind <> 'collection', op.is_active)
     ORDER BY CASE WHEN x.lab_test_id IS NOT NULL THEN 0 WHEN x.department_id IS NOT NULL THEN 1 ELSE 2 END,
              (x.from_site_id IS NULL), x.priority, x.created_at
     LIMIT 1;
    IF FOUND THEN
        RETURN jsonb_build_object('to_site_id', r.to_site_id, 'outsource_partner_id', r.outsource_partner_id,
            'rule_id', r.id, 'matched', CASE WHEN r.lab_test_id IS NOT NULL THEN 'test'
                                             WHEN r.department_id IS NOT NULL THEN 'department' ELSE 'site' END);
    END IF;
    SELECT id INTO v_site FROM dx_sites WHERE clinic_id = p_clinic_id AND id = p_from_site_id AND kind <> 'collection';
    IF v_site IS NULL THEN
        SELECT CASE WHEN count(*) = 1 THEN min(id::TEXT)::UUID END INTO v_site
          FROM dx_sites WHERE clinic_id = p_clinic_id AND is_active AND kind <> 'collection';
    END IF;
    RETURN jsonb_build_object('to_site_id', v_site, 'outsource_partner_id', NULL, 'rule_id', NULL,
                              'matched', CASE WHEN v_site IS NULL THEN 'none' ELSE 'fallback' END);
END $$;

-- ── 4. Registration: tubes split by destination (replaces the 127 version) ─
-- Specimen orders join a pending tube of the same container AND destination, else a new
-- tube: the visit's shared sequence for the first tube of a container, a fresh sequence
-- for a repeat (ADR-DX-05). Outsourced orders carry the partner (NABL "performed at").
CREATE OR REPLACE FUNCTION dx_insert_lines(p_clinic_id UUID, p_accession_id UUID, p_lines JSONB, p_actor_id UUID,
                                           p_actor_name TEXT) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    a dx_accessions; l JSONB; tst JSONB; v_item UUID; v_sample UUID; v_seq INTEGER; v_collect UUID; v_process UUID;
    v_partner UUID; v_route JSONB; v_n INTEGER := 0; v_cont_code TEXT;
BEGIN
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id;
    SELECT id INTO v_collect FROM dx_sites WHERE clinic_id = p_clinic_id AND branch_id = a.branch_id AND is_active;
    IF v_collect IS NULL THEN PERFORM dx_raise('dx_no_collection_site'); END IF;
    FOR l IN SELECT * FROM jsonb_array_elements(p_lines) LOOP
        INSERT INTO dx_accession_items (clinic_id, accession_id, lab_test_id, kind, fee_code, source_appointment_id,
                                        item_name, test_code, mrp_paise, price_paise, discount_paise,
                                        patient_share_paise, payer_share_paise, price_source)
        VALUES (p_clinic_id, p_accession_id, NULLIF(l ->> 'lab_test_id', '')::UUID, l ->> 'kind', l ->> 'fee_code',
                NULLIF(l ->> 'source_appointment_id', '')::UUID, left(l ->> 'item_name', 200), l ->> 'test_code',
                (l ->> 'mrp_paise')::INTEGER, (l ->> 'price_paise')::INTEGER, (l ->> 'discount_paise')::INTEGER,
                (l ->> 'patient_share_paise')::INTEGER, (l ->> 'payer_share_paise')::INTEGER, l ->> 'price_source')
        RETURNING id INTO v_item;
        v_n := v_n + 1;
        FOR tst IN SELECT * FROM jsonb_array_elements(l -> 'tests') LOOP
            v_sample := NULL;
            v_route := dx_resolve_routing(p_clinic_id, v_collect, (tst ->> 'lab_test_id')::UUID, NULL);
            v_process := NULLIF(v_route ->> 'to_site_id', '')::UUID;
            v_partner := NULLIF(v_route ->> 'outsource_partner_id', '')::UUID;
            IF tst ->> 'container_id' IS NOT NULL THEN
                IF v_process IS NULL AND v_partner IS NULL THEN PERFORM dx_raise('dx_no_processing_site'); END IF;
                SELECT id INTO v_sample FROM dx_samples
                 WHERE clinic_id = p_clinic_id AND accession_id = p_accession_id AND status = 'pending_collection'
                   AND container_id = (tst ->> 'container_id')::UUID
                   AND processing_site_id IS NOT DISTINCT FROM v_process
                   AND outsource_partner_id IS NOT DISTINCT FROM v_partner
                 ORDER BY created_at LIMIT 1;
                IF v_sample IS NULL THEN
                    IF NOT EXISTS (SELECT 1 FROM dx_samples WHERE clinic_id = p_clinic_id AND accession_id = p_accession_id
                                                              AND container_id = (tst ->> 'container_id')::UUID)
                       AND a.sample_seq IS NOT NULL THEN
                        v_seq := a.sample_seq;
                    ELSE
                        v_seq := dx_next_sample_seq(p_clinic_id, a.accession_year);
                    END IF;
                    v_cont_code := tst ->> 'container_code';
                    INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode, barcode_source,
                                            tube_seq, collection_site_id, processing_site_id, outsource_partner_id)
                    VALUES (p_clinic_id, p_accession_id, (tst ->> 'container_id')::UUID, tst ->> 'sample_type',
                            lpad((a.accession_year % 100)::TEXT, 2, '0') || v_cont_code || lpad(v_seq::TEXT, 8, '0'),
                            'generated', v_seq, v_collect, v_process, v_partner)
                    RETURNING id INTO v_sample;
                END IF;
            END IF;
            INSERT INTO opd_orders (clinic_id, branch_id, source, accession_id, accession_item_id, dx_sample_id,
                                    patient_id, family_member_id, order_type, catalog_type, catalog_reference_id,
                                    item_name, sample_type, urgency, clinical_notes, unit_price_paise, ordered_by,
                                    ordered_by_name, outsource_partner_id)
            VALUES (p_clinic_id, a.branch_id, 'dx', p_accession_id, v_item, v_sample, a.patient_id, a.family_member_id,
                    'investigation', 'lab_test', (tst ->> 'lab_test_id')::UUID, left(tst ->> 'name', 200),
                    CASE WHEN v_sample IS NULL THEN NULL ELSE left(tst ->> 'sample_type', 60) END,
                    a.priority, a.clinical_notes, 0, p_actor_id, left(p_actor_name, 100), v_partner)
            ON CONFLICT DO NOTHING;     -- a package listing a test twice explodes it once
        END LOOP;
    END LOOP;
    RETURN v_n;
END $$;

-- ── 5. Reject (+ recollect): the replacement keeps the destination ─────────
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
                                collection_site_id, processing_site_id, outsource_partner_id, recollect_of)
        VALUES (p_clinic_id, a.id, s.container_id, s.sample_type,
                CASE WHEN v_seq IS NULL THEN NULL
                     ELSE lpad((a.accession_year % 100)::TEXT, 2, '0') || v_code || lpad(v_seq::TEXT, 8, '0') END,
                s.barcode_source, v_seq, s.collection_site_id, s.processing_site_id, s.outsource_partner_id, s.id)
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
                                    unit_price_paise, ordered_by, ordered_by_name, outsource_partner_id)
            VALUES (p_clinic_id, o.branch_id, 'dx', o.accession_id, o.accession_item_id, v_new, o.id, o.patient_id,
                    o.family_member_id, o.order_type, o.catalog_type, o.catalog_reference_id, o.item_name, o.sample_type,
                    o.urgency, o.clinical_notes, 0, p_actor_id, left(COALESCE(p_actor_name, 'system'), 100),
                    o.outsource_partner_id);
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

-- ── 6. Batches (W6) ────────────────────────────────────────────────────────
-- Read model: data-minimised by construction (name, age, sex, MRN, tests; never phone,
-- address or money — SECURITY §4).
CREATE OR REPLACE FUNCTION dx_batch_json(p_clinic_id UUID, p_batch_id UUID) RETURNS JSONB LANGUAGE sql STABLE AS $$
    SELECT to_jsonb(b) || jsonb_build_object(
        'from_site_name', fs.display_name, 'to_site_name', ts.display_name, 'partner_name', op.name,
        'samples', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'id', s.id, 'barcode', s.barcode, 'status', s.status, 'collected_at', s.collected_at,
                'received_at', s.received_at, 'rejection_reason_code', s.rejection_reason_code,
                'receipt', bs.receipt, 'receipt_notes', bs.receipt_notes, 'added_at', bs.created_at,
                'container_code', c.code, 'container_name', c.name, 'colour_hex', c.colour_hex,
                'stability_room_hours', c.stability_room_hours, 'stability_cold_hours', c.stability_cold_hours,
                'accession_id', a.id, 'accession_number', a.accession_number, 'priority', a.priority,
                'patient_name', a.patient_snapshot ->> 'name', 'mrn', a.patient_snapshot ->> 'mrn',
                'gender', a.patient_snapshot ->> 'gender', 'age_years', a.patient_snapshot -> 'age_years',
                'tests', COALESCE((SELECT jsonb_agg(o.item_name ORDER BY o.created_at) FROM opd_orders o
                                    WHERE o.clinic_id = s.clinic_id AND o.dx_sample_id = s.id
                                      AND o.status <> 'cancelled'), '[]'::jsonb))
                ORDER BY bs.created_at, bs.id)
              FROM dx_batch_samples bs
              JOIN dx_samples s ON s.clinic_id = bs.clinic_id AND s.id = bs.sample_id
              JOIN dx_containers c ON c.clinic_id = s.clinic_id AND c.id = s.container_id
              JOIN dx_accessions a ON a.clinic_id = s.clinic_id AND a.id = s.accession_id
             WHERE bs.clinic_id = b.clinic_id AND bs.batch_id = b.id), '[]'::jsonb))
      FROM dx_batches b
      JOIN dx_sites fs ON fs.clinic_id = b.clinic_id AND fs.id = b.from_site_id
      LEFT JOIN dx_sites ts ON ts.clinic_id = b.clinic_id AND ts.id = b.to_site_id
      LEFT JOIN dx_outsource_partners op ON op.clinic_id = b.clinic_id AND op.id = b.outsource_partner_id
     WHERE b.clinic_id = p_clinic_id AND b.id = p_batch_id
$$;

CREATE OR REPLACE FUNCTION dx_create_batch(p_clinic_id UUID, p_from_site_id UUID, p_to_site_id UUID,
                                           p_outsource_partner_id UUID, p_carrier_name TEXT, p_temp_c NUMERIC,
                                           p_actor_id UUID, p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v_year INTEGER; v_seq INTEGER; v_id UUID;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF num_nonnulls(p_to_site_id, p_outsource_partner_id) <> 1 THEN PERFORM dx_raise('dx_batch_destination_invalid'); END IF;
    IF NOT EXISTS (SELECT 1 FROM dx_sites WHERE clinic_id = p_clinic_id AND id = p_from_site_id AND is_active) THEN
        PERFORM dx_raise('dx_site_not_found');
    END IF;
    IF p_to_site_id IS NOT NULL THEN
        IF p_to_site_id = p_from_site_id THEN PERFORM dx_raise('dx_batch_same_site'); END IF;
        IF NOT EXISTS (SELECT 1 FROM dx_sites WHERE clinic_id = p_clinic_id AND id = p_to_site_id AND is_active
                                                AND kind <> 'collection') THEN
            PERFORM dx_raise('dx_batch_destination_invalid');
        END IF;
    ELSIF NOT EXISTS (SELECT 1 FROM dx_outsource_partners WHERE clinic_id = p_clinic_id AND id = p_outsource_partner_id
                                                           AND is_active) THEN
        PERFORM dx_raise('dx_batch_destination_invalid');
    END IF;
    v_year := EXTRACT(YEAR FROM (now() + interval '5 hours 30 minutes'))::INTEGER;
    v_seq := opd_next_counter(p_clinic_id, 'dx_batch:' || v_year);
    INSERT INTO dx_batches (clinic_id, batch_year, batch_seq, from_site_id, to_site_id, outsource_partner_id,
                            carrier_name, dispatch_temp_c, created_by, created_by_name)
    VALUES (p_clinic_id, v_year, v_seq, p_from_site_id, p_to_site_id, p_outsource_partner_id,
            NULLIF(btrim(p_carrier_name), ''), p_temp_c, p_actor_id, left(COALESCE(p_actor_name, 'system'), 100))
    RETURNING id INTO v_id;
    RETURN dx_batch_json(p_clinic_id, v_id);
END $$;

-- Only collected tubes drawn at the batch's site whose destination is the batch's.
CREATE OR REPLACE FUNCTION dx_batch_add(p_clinic_id UUID, p_batch_id UUID, p_barcodes TEXT[], p_actor_id UUID,
                                        p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE b dx_batches; s dx_samples; v_code TEXT; v_added INTEGER := 0;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO b FROM dx_batches WHERE clinic_id = p_clinic_id AND id = p_batch_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_batch_not_found'); END IF;
    IF b.status <> 'open' THEN PERFORM dx_raise('dx_batch_not_open'); END IF;
    IF COALESCE(cardinality(p_barcodes), 0) = 0 THEN PERFORM dx_raise('dx_no_samples'); END IF;
    FOR v_code IN SELECT btrim(x) FROM unnest(p_barcodes) x WHERE btrim(x) <> '' LOOP   -- scan order; repeats skip below
        SELECT * INTO s FROM dx_samples WHERE clinic_id = p_clinic_id AND barcode = v_code FOR UPDATE;
        IF NOT FOUND THEN PERFORM dx_raise('dx_sample_not_found:' || v_code); END IF;
        CONTINUE WHEN EXISTS (SELECT 1 FROM dx_batch_samples WHERE clinic_id = p_clinic_id AND batch_id = b.id
                                                               AND sample_id = s.id);         -- scanned twice
        IF s.status <> 'collected' THEN PERFORM dx_raise('dx_sample_not_collected:' || v_code); END IF;
        IF s.collection_site_id <> b.from_site_id THEN PERFORM dx_raise('dx_batch_wrong_origin:' || v_code); END IF;
        IF s.processing_site_id IS DISTINCT FROM b.to_site_id
           OR s.outsource_partner_id IS DISTINCT FROM b.outsource_partner_id THEN
            PERFORM dx_raise('dx_batch_wrong_destination:' || v_code);
        END IF;
        IF EXISTS (SELECT 1 FROM dx_batch_samples WHERE clinic_id = p_clinic_id AND sample_id = s.id AND receipt IS NULL) THEN
            PERFORM dx_raise('dx_sample_in_live_batch:' || v_code);
        END IF;
        INSERT INTO dx_batch_samples (clinic_id, batch_id, sample_id) VALUES (p_clinic_id, b.id, s.id);
        v_added := v_added + 1;
    END LOOP;
    RETURN dx_batch_json(p_clinic_id, b.id) || jsonb_build_object('added', v_added);
END $$;

CREATE OR REPLACE FUNCTION dx_dispatch_batch(p_clinic_id UUID, p_batch_id UUID, p_temp_c NUMERIC, p_notes TEXT,
                                             p_actor_id UUID, p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE b dx_batches; s dx_samples;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO b FROM dx_batches WHERE clinic_id = p_clinic_id AND id = p_batch_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_batch_not_found'); END IF;
    IF b.status = 'dispatched' THEN RETURN dx_batch_json(p_clinic_id, b.id) || jsonb_build_object('replayed', true); END IF;
    IF b.status <> 'open' THEN PERFORM dx_raise('dx_batch_not_open'); END IF;
    IF NOT EXISTS (SELECT 1 FROM dx_batch_samples bs JOIN dx_samples x ON x.clinic_id = bs.clinic_id AND x.id = bs.sample_id
                    WHERE bs.clinic_id = p_clinic_id AND bs.batch_id = b.id AND x.status = 'collected') THEN
        PERFORM dx_raise('dx_batch_empty');
    END IF;
    UPDATE dx_batches SET status = 'dispatched', dispatched_at = now(), dispatched_by = p_actor_id,
                          dispatch_temp_c = COALESCE(p_temp_c, dispatch_temp_c),
                          notes = COALESCE(NULLIF(btrim(p_notes), ''), notes)
     WHERE clinic_id = p_clinic_id AND id = b.id
    RETURNING * INTO b;
    FOR s IN SELECT x.* FROM dx_batch_samples bs JOIN dx_samples x ON x.clinic_id = bs.clinic_id AND x.id = bs.sample_id
              WHERE bs.clinic_id = p_clinic_id AND bs.batch_id = b.id AND x.status = 'collected' FOR UPDATE OF x LOOP
        UPDATE dx_samples SET status = 'in_transit' WHERE clinic_id = p_clinic_id AND id = s.id;
        INSERT INTO dx_events (clinic_id, accession_id, sample_id, kind, actor_id, actor_name, details)
        VALUES (p_clinic_id, s.accession_id, s.id, 'dispatched', p_actor_id, left(p_actor_name, 100),
                jsonb_build_object('batch_id', b.id, 'batch_number', b.batch_number, 'to_site_id', b.to_site_id,
                                   'outsource_partner_id', b.outsource_partner_id, 'temp_c', b.dispatch_temp_c));
    END LOOP;
    RETURN dx_batch_json(p_clinic_id, b.id) || jsonb_build_object('replayed', false);
END $$;

-- Receipt = the lab's accession step: scanned -> received at the batch's site; not scanned
-- -> missing; rejected on the way -> damaged; scanned but not on the manifest -> reported,
-- never received.
CREATE OR REPLACE FUNCTION dx_receive_batch(p_clinic_id UUID, p_batch_id UUID, p_scanned_barcodes TEXT[],
                                            p_temp_c NUMERIC, p_discrepancy_notes TEXT, p_actor_id UUID,
                                            p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE b dx_batches; r RECORD; v_scanned TEXT[]; v_received TEXT[] := '{}'; v_missing TEXT[] := '{}';
        v_damaged TEXT[] := '{}'; v_unexpected TEXT[];
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO b FROM dx_batches WHERE clinic_id = p_clinic_id AND id = p_batch_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_batch_not_found'); END IF;
    IF b.status IN ('received', 'received_with_discrepancy') THEN
        RETURN dx_batch_json(p_clinic_id, b.id) || jsonb_build_object('replayed', true);
    END IF;
    IF b.status <> 'dispatched' THEN PERFORM dx_raise('dx_batch_not_dispatched'); END IF;
    SELECT COALESCE(array_agg(DISTINCT btrim(x)), '{}') INTO v_scanned FROM unnest(p_scanned_barcodes) x WHERE btrim(x) <> '';
    FOR r IN SELECT bs.id AS row_id, x.id, x.accession_id, x.barcode, x.status, x.rejection_reason_code
               FROM dx_batch_samples bs JOIN dx_samples x ON x.clinic_id = bs.clinic_id AND x.id = bs.sample_id
              WHERE bs.clinic_id = p_clinic_id AND bs.batch_id = b.id
              ORDER BY bs.created_at, bs.id FOR UPDATE OF x LOOP
        IF r.status = 'rejected' THEN
            UPDATE dx_batch_samples SET receipt = 'damaged', receipt_notes = 'rejected: ' || r.rejection_reason_code
             WHERE id = r.row_id;
            v_damaged := v_damaged || r.barcode;
        ELSIF r.barcode = ANY (v_scanned) AND r.status = 'in_transit' THEN
            UPDATE dx_batch_samples SET receipt = 'received' WHERE id = r.row_id;
            UPDATE dx_samples SET status = 'received', received_at = now(), received_by = p_actor_id,
                                  received_by_name = left(p_actor_name, 100), received_site_id = b.to_site_id
             WHERE clinic_id = p_clinic_id AND id = r.id;
            INSERT INTO dx_events (clinic_id, accession_id, sample_id, kind, actor_id, actor_name, details)
            VALUES (p_clinic_id, r.accession_id, r.id, 'received', p_actor_id, left(p_actor_name, 100),
                    jsonb_build_object('batch_id', b.id, 'batch_number', b.batch_number, 'site_id', b.to_site_id,
                                       'outsource_partner_id', b.outsource_partner_id, 'temp_c', p_temp_c));
            v_received := v_received || r.barcode;
        ELSE
            UPDATE dx_batch_samples SET receipt = 'missing' WHERE id = r.row_id;
            INSERT INTO dx_events (clinic_id, accession_id, sample_id, kind, actor_id, actor_name, details)
            VALUES (p_clinic_id, r.accession_id, r.id, 'missing_in_transit', p_actor_id, left(p_actor_name, 100),
                    jsonb_build_object('batch_id', b.id, 'batch_number', b.batch_number));
            v_missing := v_missing || r.barcode;
        END IF;
    END LOOP;
    SELECT COALESCE(array_agg(x ORDER BY x), '{}') INTO v_unexpected FROM unnest(v_scanned) x
     WHERE NOT EXISTS (SELECT 1 FROM dx_batch_samples bs JOIN dx_samples s ON s.clinic_id = bs.clinic_id AND s.id = bs.sample_id
                        WHERE bs.clinic_id = p_clinic_id AND bs.batch_id = b.id AND s.barcode = x);
    UPDATE dx_batches SET status = CASE WHEN cardinality(v_missing) + cardinality(v_damaged) > 0
                                        THEN 'received_with_discrepancy' ELSE 'received' END,
                          received_at = now(), received_by = p_actor_id, receipt_temp_c = p_temp_c,
                          notes = NULLIF(concat_ws(E'\n', notes, NULLIF(btrim(p_discrepancy_notes), '')), '')
     WHERE clinic_id = p_clinic_id AND id = b.id;
    RETURN dx_batch_json(p_clinic_id, b.id) || jsonb_build_object('replayed', false,
        'discrepancy', jsonb_build_object('received', to_jsonb(v_received), 'missing', to_jsonb(v_missing),
                                          'damaged', to_jsonb(v_damaged), 'unexpected', to_jsonb(v_unexpected)));
END $$;

-- ── 7. Purge v5 (adds batches, manifests, routing rules, partners) ─────────
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    DELETE FROM dx_label_prints WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_field_collections WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('field_collections', n);
    DELETE FROM dx_batch_samples WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_batches WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('batches', n);
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
    DELETE FROM dx_routing_rules WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('routing_rules', n);
    DELETE FROM dx_outsource_partners WHERE clinic_id = p_clinic_id;
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

-- ── 8. RLS parity + API-role lockout ───────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_outsource_partners', 'dx_routing_rules', 'dx_batches', 'dx_batch_samples']
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

-- ── 9. Verify ──────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_tables WHERE schemaname = 'public'
          AND tablename IN ('dx_outsource_partners', 'dx_routing_rules', 'dx_batches', 'dx_batch_samples')) <> 4 THEN
        RAISE EXCEPTION '129 verify: DX logistics tables missing';
    END IF;
    IF to_regclass('public.uq_dx_sample_one_live_batch') IS NULL OR to_regclass('public.uq_dx_routing_rules_tie') IS NULL THEN
        RAISE EXCEPTION '129 verify: DX logistics indexes missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'opd_orders'
                     AND column_name = 'outsource_partner_id') THEN
        RAISE EXCEPTION '129 verify: opd_orders.outsource_partner_id missing';
    END IF;
    IF to_regprocedure('dx_resolve_routing(uuid,uuid,uuid,uuid)') IS NULL
       OR to_regprocedure('dx_create_batch(uuid,uuid,uuid,uuid,text,numeric,uuid,text)') IS NULL
       OR to_regprocedure('dx_batch_add(uuid,uuid,text[],uuid,text)') IS NULL
       OR to_regprocedure('dx_dispatch_batch(uuid,uuid,numeric,text,uuid,text)') IS NULL
       OR to_regprocedure('dx_receive_batch(uuid,uuid,text[],numeric,text,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '129 verify: DX logistics functions missing';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname IN ('dx_outsource_partners', 'dx_routing_rules', 'dx_batches',
                                                        'dx_batch_samples')
                 AND relkind = 'r' AND NOT (relrowsecurity AND relforcerowsecurity)) THEN
        RAISE EXCEPTION '129 verify: RLS not forced on DX logistics tables';
    END IF;
END $$;
