-- ============================================================================
-- Migration 115: Investigation & procedure orders (G1)
-- ============================================================================
-- A doctor's "CBC, X-ray chest" becomes ONE row per line in opd_orders. That
-- row is the lab / radiology worklist entry and the source of the bill line, so
-- nothing is retyped at the counter or in the lab.
--
--   OPD  doctor orders on the encounter (opd_place_orders) -> a line on the
--        visit's draft bill (opd_bill_orders); "billed" when that bill is issued.
--   IPD  an investigation / procedure row in ipd_orders is mirrored here by a
--        trigger, with a PROPOSED ipd_charges row when the test has a price.
--
-- Worklist: ordered -> (billed) -> sample_collected -> [in_progress -> resulted]
-- -> completed, or cancelled. in_progress/resulted are the LIS steps (G2); the
-- transition guard already accepts them.
--
-- Invariants held here, not only in the API:
--   * an order belongs to one clinic and one encounter OR one IPD order;
--   * price and item are snapshotted at order time and never change;
--   * at most one live (non-void) bill line per order (row lock + check);
--   * billed <-> ordered moves only with the bill (issue / void);
--   * an order on a paid or issued bill cannot be cancelled: refund + void first;
--   * a specimen-bound test cannot be completed before its sample is collected.
--
-- The bill line points at the order (opd_invoice_items.order_id), not the other
-- way round: a draft save (opd_save_invoice_draft) replaces every line, so a
-- link stored on the order would dangle after each save. The function is
-- redefined below to carry order_id through.
--
-- Additive: one table, one column, functions and triggers. Pre-115 code never
-- writes order_id, and the redefined draft-save treats a missing order_id as
-- NULL, exactly as before. Idempotent.
-- Rollback: migrations/rollback/115_investigation_and_procedure_orders.down.sql.
-- ============================================================================

-- ── 1. Orders / worklist rows ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS opd_orders (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id                UUID NULL,
    source                   TEXT NOT NULL CHECK (source IN ('opd', 'ipd')),
    encounter_id             UUID NULL,
    appointment_id           UUID NULL,
    admission_id             UUID NULL,
    ipd_order_id             UUID NULL,
    patient_id               UUID NOT NULL,
    family_member_id         UUID NULL,
    doctor_id                UUID NOT NULL,
    order_type               TEXT NOT NULL CHECK (order_type IN ('investigation', 'procedure')),
    catalog_type             TEXT NOT NULL CHECK (catalog_type IN ('lab_test', 'service_catalog', 'custom')),
    catalog_reference_id     UUID NULL,   -- lab_tests.id when catalog_type = 'lab_test' (reference only)
    catalog_code             TEXT NULL CHECK (char_length(catalog_code) <= 40),
    item_name                TEXT NOT NULL CHECK (char_length(btrim(item_name)) BETWEEN 1 AND 200),
    sample_type              TEXT NULL CHECK (char_length(sample_type) <= 60),
    urgency                  TEXT NOT NULL DEFAULT 'routine' CHECK (urgency IN ('routine', 'urgent', 'stat')),
    clinical_notes           TEXT NULL CHECK (char_length(clinical_notes) <= 1000),
    unit_price_paise         INTEGER NOT NULL DEFAULT 0 CHECK (unit_price_paise BETWEEN 0 AND 100000000),
    quantity                 SMALLINT NOT NULL DEFAULT 1 CHECK (quantity BETWEEN 1 AND 99),
    status                   TEXT NOT NULL DEFAULT 'ordered'
                             CHECK (status IN ('ordered', 'billed', 'sample_collected', 'in_progress',
                                               'resulted', 'completed', 'cancelled')),
    cancellation_reason      TEXT NULL CHECK (char_length(btrim(cancellation_reason)) BETWEEN 3 AND 300),
    cancelled_at             TIMESTAMPTZ NULL,
    cancelled_by             UUID NULL,
    specimen_barcode         TEXT NULL CHECK (specimen_barcode ~ '^[A-Za-z0-9._/-]{3,64}$'),
    sample_collected_at      TIMESTAMPTZ NULL,
    sample_collected_by      UUID NULL,   -- clinic_admins.id; no FK: staff rows can be deleted, the record stays
    sample_collected_by_name TEXT NULL CHECK (char_length(sample_collected_by_name) <= 100),
    resulted_at              TIMESTAMPTZ NULL,
    resulted_by              UUID NULL,
    resulted_by_name         TEXT NULL CHECK (char_length(resulted_by_name) <= 100),
    result_summary           TEXT NULL CHECK (char_length(result_summary) <= 4000),
    ordered_by               UUID NULL,
    ordered_by_name          TEXT NOT NULL CHECK (char_length(btrim(ordered_by_name)) BETWEEN 1 AND 100),
    idempotency_key          TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_orders_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_orders_source_shape CHECK (
        (source = 'opd' AND encounter_id IS NOT NULL AND appointment_id IS NOT NULL
                        AND admission_id IS NULL AND ipd_order_id IS NULL)
     OR (source = 'ipd' AND admission_id IS NOT NULL AND ipd_order_id IS NOT NULL
                        AND encounter_id IS NULL AND appointment_id IS NULL)),
    CONSTRAINT opd_orders_catalog_shape CHECK (
        (catalog_type = 'lab_test' AND catalog_reference_id IS NOT NULL)
     OR (catalog_type = 'service_catalog' AND catalog_code IS NOT NULL)
     OR catalog_type = 'custom'),
    CONSTRAINT opd_orders_cancel_consistent CHECK (
        (status = 'cancelled') = (cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL)),
    CONSTRAINT opd_orders_sample_consistent CHECK (
        (sample_collected_at IS NULL) = (specimen_barcode IS NULL)
        AND (status NOT IN ('sample_collected', 'in_progress', 'resulted') OR sample_collected_at IS NOT NULL)),
    CONSTRAINT opd_orders_result_consistent CHECK (
        status NOT IN ('resulted', 'completed') OR resulted_at IS NOT NULL),
    CONSTRAINT opd_orders_encounter_fk   FOREIGN KEY (clinic_id, encounter_id)
        REFERENCES opd_encounters (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT opd_orders_appointment_fk FOREIGN KEY (clinic_id, appointment_id)   REFERENCES appointments (clinic_id, id),
    CONSTRAINT opd_orders_admission_fk   FOREIGN KEY (clinic_id, admission_id)     REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT opd_orders_ipd_order_fk   FOREIGN KEY (clinic_id, ipd_order_id)
        REFERENCES ipd_orders (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT opd_orders_patient_fk     FOREIGN KEY (clinic_id, patient_id)       REFERENCES patients (clinic_id, id),
    CONSTRAINT opd_orders_family_fk      FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id),
    CONSTRAINT opd_orders_doctor_fk      FOREIGN KEY (clinic_id, doctor_id)        REFERENCES doctors (clinic_id, id),
    CONSTRAINT opd_orders_branch_fk      FOREIGN KEY (clinic_id, branch_id)        REFERENCES branches (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_orders_idempotency
    ON opd_orders (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
-- one worklist row per IPD order line
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_orders_ipd_order
    ON opd_orders (clinic_id, ipd_order_id) WHERE ipd_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opd_orders_encounter   ON opd_orders (clinic_id, encounter_id);
CREATE INDEX IF NOT EXISTS idx_opd_orders_appointment ON opd_orders (clinic_id, appointment_id) WHERE appointment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opd_orders_patient     ON opd_orders (clinic_id, patient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opd_orders_worklist    ON opd_orders (clinic_id, status, urgency, created_at)
    WHERE status IN ('ordered', 'billed', 'sample_collected', 'in_progress');

-- ── 2. Bill line -> order link ─────────────────────────────────────────────
ALTER TABLE opd_invoice_items ADD COLUMN IF NOT EXISTS order_id UUID NULL;
ALTER TABLE opd_invoice_items DROP CONSTRAINT IF EXISTS opd_invoice_items_order_fk;
ALTER TABLE opd_invoice_items ADD  CONSTRAINT opd_invoice_items_order_fk
    FOREIGN KEY (clinic_id, order_id) REFERENCES opd_orders (clinic_id, id);
CREATE INDEX IF NOT EXISTS idx_opd_invoice_items_order
    ON opd_invoice_items (clinic_id, order_id) WHERE order_id IS NOT NULL;

-- ── 3. Order guard: content immutable, status moves only along the workflow ─
CREATE OR REPLACE FUNCTION opd_guard_order() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_mutable TEXT[] := ARRAY['status', 'updated_at', 'cancellation_reason', 'cancelled_at', 'cancelled_by',
                              'specimen_barcode', 'sample_collected_at', 'sample_collected_by',
                              'sample_collected_by_name', 'resulted_at', 'resulted_by', 'resulted_by_name',
                              'result_summary'];
    v_ok BOOLEAN;
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- only the owner's clinic purge (cascading from encounters / IPD orders)
        IF opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'opd_append_only:opd_orders' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status IN ('completed', 'cancelled') THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF (to_jsonb(NEW) - v_mutable) IS DISTINCT FROM (to_jsonb(OLD) - v_mutable) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.sample_collected_at IS NOT NULL
       AND (NEW.specimen_barcode, NEW.sample_collected_at, NEW.sample_collected_by, NEW.sample_collected_by_name)
           IS DISTINCT FROM
           (OLD.specimen_barcode, OLD.sample_collected_at, OLD.sample_collected_by, OLD.sample_collected_by_name) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.resulted_at IS NOT NULL
       AND (NEW.resulted_at, NEW.resulted_by, NEW.resulted_by_name, NEW.result_summary)
           IS DISTINCT FROM (OLD.resulted_at, OLD.resulted_by, OLD.resulted_by_name, OLD.result_summary) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    v_ok := CASE OLD.status
        WHEN 'ordered'          THEN NEW.status IN ('billed', 'sample_collected', 'completed', 'cancelled')
        WHEN 'billed'           THEN NEW.status IN ('ordered', 'sample_collected', 'completed', 'cancelled')
        WHEN 'sample_collected' THEN NEW.status IN ('in_progress', 'resulted', 'completed', 'cancelled')
        WHEN 'in_progress'      THEN NEW.status IN ('resulted', 'completed', 'cancelled')
        WHEN 'resulted'         THEN NEW.status = 'completed'
        ELSE false END;
    IF NOT v_ok THEN
        RAISE EXCEPTION 'opd_order_bad_transition:%->%', OLD.status, NEW.status USING ERRCODE = 'P0001';
    END IF;
    -- billing state follows the bill (opd_invoice_orders_billing), never a direct write
    IF NEW.status IN ('ordered', 'billed') AND OLD.status IN ('ordered', 'billed')
       AND current_setting('kriya.opd_order_billing', true) IS DISTINCT FROM OLD.clinic_id::TEXT THEN
        RAISE EXCEPTION 'opd_order_billing_is_derived' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = 'completed' AND OLD.status IN ('ordered', 'billed') AND OLD.sample_type IS NOT NULL THEN
        RAISE EXCEPTION 'opd_order_sample_required' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_opd_orders_guard ON opd_orders;
CREATE TRIGGER trg_opd_orders_guard BEFORE UPDATE OR DELETE ON opd_orders
    FOR EACH ROW EXECUTE FUNCTION opd_guard_order();
DROP TRIGGER IF EXISTS trg_opd_orders_touch ON opd_orders;
CREATE TRIGGER trg_opd_orders_touch BEFORE UPDATE ON opd_orders
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();

-- ── 4. Bill line guard: an order is billed once, on its own patient's bill ──
CREATE OR REPLACE FUNCTION opd_guard_invoice_item_order() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE o opd_orders; v opd_invoices;
BEGIN
    IF NEW.order_id IS NULL OR opd_purging(NEW.clinic_id) THEN RETURN NEW; END IF;
    IF TG_OP = 'UPDATE' AND NEW.order_id IS NOT DISTINCT FROM OLD.order_id THEN RETURN NEW; END IF;
    -- the row lock serialises two lines racing for the same order
    SELECT * INTO o FROM opd_orders WHERE id = NEW.order_id AND clinic_id = NEW.clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_order_not_found' USING ERRCODE = 'P0002'; END IF;
    IF o.source <> 'opd' THEN RAISE EXCEPTION 'opd_order_not_billable:ipd' USING ERRCODE = 'P0001'; END IF;
    IF o.status = 'cancelled' THEN RAISE EXCEPTION 'opd_order_not_billable:cancelled' USING ERRCODE = 'P0001'; END IF;
    SELECT * INTO v FROM opd_invoices WHERE id = NEW.invoice_id AND clinic_id = NEW.clinic_id;
    IF v.patient_id <> o.patient_id OR v.family_member_id IS DISTINCT FROM o.family_member_id
       OR v.admission_id IS NOT NULL OR (v.appointment_id IS NOT NULL AND v.appointment_id <> o.appointment_id) THEN
        RAISE EXCEPTION 'opd_order_wrong_invoice' USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (SELECT 1 FROM opd_invoice_items it
                 JOIN opd_invoices i ON i.id = it.invoice_id AND i.clinic_id = it.clinic_id
                WHERE it.clinic_id = NEW.clinic_id AND it.order_id = NEW.order_id AND it.id <> NEW.id
                  AND i.status <> 'void') THEN
        RAISE EXCEPTION 'opd_order_already_billed' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_opd_inv_items_order ON opd_invoice_items;
CREATE TRIGGER trg_opd_inv_items_order BEFORE INSERT OR UPDATE OF order_id ON opd_invoice_items
    FOR EACH ROW EXECUTE FUNCTION opd_guard_invoice_item_order();

-- ── 5. Issue / void of a bill moves its orders ordered <-> billed ──────────
CREATE OR REPLACE FUNCTION opd_invoice_orders_billing() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = OLD.status THEN RETURN NULL; END IF;
    PERFORM set_config('kriya.opd_order_billing', NEW.clinic_id::TEXT, true);
    IF OLD.status = 'draft' AND NEW.status IN ('issued', 'partially_paid', 'paid') THEN
        UPDATE opd_orders o SET status = 'billed'
          FROM opd_invoice_items it
         WHERE it.clinic_id = NEW.clinic_id AND it.invoice_id = NEW.id AND it.order_id = o.id
           AND o.clinic_id = NEW.clinic_id AND o.status = 'ordered';
    ELSIF NEW.status = 'void' THEN
        UPDATE opd_orders o SET status = 'ordered'
          FROM opd_invoice_items it
         WHERE it.clinic_id = NEW.clinic_id AND it.invoice_id = NEW.id AND it.order_id = o.id
           AND o.clinic_id = NEW.clinic_id AND o.status = 'billed';
    END IF;
    PERFORM set_config('kriya.opd_order_billing', '', true);
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_opd_invoices_orders ON opd_invoices;
CREATE TRIGGER trg_opd_invoices_orders AFTER UPDATE OF status ON opd_invoices
    FOR EACH ROW EXECUTE FUNCTION opd_invoice_orders_billing();

-- ── 6. Put a visit's unbilled orders on its draft bill ─────────────────────
-- Target: the visit's own draft bill; once that bill is issued, a supplementary
-- draft for the same visit (appointment_id NULL, encounter_id = the visit's
-- encounter) - the consult fee is usually paid before tests are ordered.
-- p_create = false only fills a draft that already exists. The appointment row
-- lock serialises every bill / cancel of one visit.
CREATE OR REPLACE FUNCTION opd_bill_orders(
    p_clinic_id UUID, p_appointment_id UUID, p_order_ids UUID[], p_create BOOLEAN, p_actor_id UUID)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    a RECORD; v_visit opd_invoices; v_target UUID; v_created BOOLEAN := false;
    v_ids UUID[]; v_line INTEGER; o opd_orders; v_enc UUID;
BEGIN
    SELECT id, patient_id, family_member_id, branch_id INTO a
      FROM appointments WHERE id = p_appointment_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;

    SELECT * INTO v_visit FROM opd_invoices
     WHERE clinic_id = p_clinic_id AND appointment_id = p_appointment_id AND status <> 'void' FOR UPDATE;
    IF FOUND AND v_visit.status = 'draft' THEN
        v_target := v_visit.id;
    ELSE
        SELECT i.id INTO v_target FROM opd_invoices i
         WHERE i.clinic_id = p_clinic_id AND i.appointment_id IS NULL AND i.admission_id IS NULL
           AND i.status = 'draft' AND i.patient_id = a.patient_id
           AND i.family_member_id IS NOT DISTINCT FROM a.family_member_id
           AND i.encounter_id IN (SELECT e.id FROM opd_encounters e
                                   WHERE e.clinic_id = p_clinic_id AND e.appointment_id = p_appointment_id)
         ORDER BY i.created_at, i.id LIMIT 1 FOR UPDATE;
    END IF;

    PERFORM 1 FROM opd_orders
     WHERE clinic_id = p_clinic_id AND appointment_id = p_appointment_id AND status = 'ordered'
       AND (p_order_ids IS NULL OR id = ANY (p_order_ids))
     ORDER BY id FOR UPDATE;
    -- a fresh statement (READ COMMITTED) sees lines a concurrent caller just committed
    SELECT array_agg(o2.id ORDER BY o2.created_at, o2.id) INTO v_ids FROM opd_orders o2
     WHERE o2.clinic_id = p_clinic_id AND o2.appointment_id = p_appointment_id AND o2.status = 'ordered'
       AND (p_order_ids IS NULL OR o2.id = ANY (p_order_ids))
       AND NOT EXISTS (SELECT 1 FROM opd_invoice_items it
                         JOIN opd_invoices i ON i.id = it.invoice_id AND i.clinic_id = it.clinic_id
                        WHERE it.clinic_id = p_clinic_id AND it.order_id = o2.id AND i.status <> 'void');
    IF v_ids IS NULL THEN
        RETURN jsonb_build_object('invoice_id', v_target, 'added', 0, 'created', false, 'pending', 0);
    END IF;

    IF v_target IS NULL THEN
        IF NOT p_create THEN
            RETURN jsonb_build_object('invoice_id', NULL, 'added', 0, 'created', false,
                                      'pending', cardinality(v_ids));
        END IF;
        IF v_visit.id IS NULL THEN
            RAISE EXCEPTION 'opd_visit_invoice_missing' USING ERRCODE = 'P0001';
        END IF;
        SELECT e.id INTO v_enc FROM opd_encounters e
         WHERE e.clinic_id = p_clinic_id AND e.appointment_id = p_appointment_id
         ORDER BY e.version DESC LIMIT 1;
        INSERT INTO opd_invoices (clinic_id, branch_id, appointment_id, encounter_id, patient_id, family_member_id,
                                  status, subtotal_paise, discount_paise, paid_paise, patient_snapshot, notes, created_by)
        VALUES (p_clinic_id, a.branch_id, NULL, v_enc, a.patient_id, a.family_member_id, 'draft', 0, 0, 0,
                v_visit.patient_snapshot, 'Investigations and procedures ordered in consultation', p_actor_id)
        RETURNING id INTO v_target;
        v_created := true;
    END IF;

    SELECT COALESCE(max(line_no), 0) INTO v_line FROM opd_invoice_items
     WHERE clinic_id = p_clinic_id AND invoice_id = v_target;
    IF v_line + cardinality(v_ids) > 100 THEN
        RAISE EXCEPTION 'opd_invoice_too_many_lines' USING ERRCODE = 'P0001';
    END IF;
    FOR o IN SELECT * FROM opd_orders WHERE clinic_id = p_clinic_id AND id = ANY (v_ids) ORDER BY created_at, id LOOP
        v_line := v_line + 1;
        INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, catalog_code, lab_test_id,
                                       description, quantity, unit_price_paise, order_id)
        VALUES (p_clinic_id, v_target, v_line,
                CASE o.order_type WHEN 'investigation' THEN 'diagnostic' ELSE 'procedure' END,
                o.catalog_code, CASE WHEN o.catalog_type = 'lab_test' THEN o.catalog_reference_id END,
                o.item_name, o.quantity, o.unit_price_paise, o.id);
    END LOOP;
    RETURN jsonb_build_object('invoice_id', v_target, 'added', cardinality(v_ids), 'created', v_created,
                              'pending', 0);
END $$;

-- ── 7. Place a consult's orders: one transaction, idempotent per click ─────
-- Prices and sample types come from the clinic's own catalogue here, never
-- from the client (custom lines excepted). Line n carries key '<key>:<n>', so
-- a retried click - in any worker, any number of times at once - inserts once.
CREATE OR REPLACE FUNCTION opd_place_orders(
    p_clinic_id UUID, p_encounter_id UUID, p_items JSONB, p_idempotency_key TEXT,
    p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    e opd_encounters; r JSONB; n INTEGER := 0; v_id UUID; v_ids UUID[] := '{}';
    v_name TEXT; v_price INTEGER; v_sample TEXT; v_code TEXT; v_ref UUID; v_cat TEXT;
    v_catalog JSONB; v_billing JSONB; v_prior UUID;
BEGIN
    IF p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9_-]{8,72}$' THEN
        RAISE EXCEPTION 'opd_order_bad_key' USING ERRCODE = 'P0001';
    END IF;
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) NOT BETWEEN 1 AND 30 THEN
        RAISE EXCEPTION 'opd_order_empty' USING ERRCODE = 'P0001';
    END IF;
    SELECT * INTO e FROM opd_encounters WHERE id = p_encounter_id AND clinic_id = p_clinic_id FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;

    SELECT encounter_id INTO v_prior FROM opd_orders
     WHERE clinic_id = p_clinic_id AND idempotency_key = p_idempotency_key || ':1';
    IF FOUND THEN
        IF v_prior <> p_encounter_id THEN RAISE EXCEPTION 'opd_order_key_reused' USING ERRCODE = 'P0001'; END IF;
        RETURN jsonb_build_object('replayed', true, 'orders',
            (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.created_at, x.id) FROM opd_orders x
              WHERE x.clinic_id = p_clinic_id AND left(x.idempotency_key, length(p_idempotency_key) + 1)
                                                  = p_idempotency_key || ':'));
    END IF;
    IF e.status = 'superseded' THEN RAISE EXCEPTION 'opd_encounter_superseded' USING ERRCODE = 'P0001'; END IF;

    SELECT COALESCE(opd_settings -> 'service_catalog', '[]'::jsonb) INTO v_catalog
      FROM clinics WHERE id = p_clinic_id;
    IF jsonb_typeof(v_catalog) IS DISTINCT FROM 'array' THEN v_catalog := '[]'::jsonb; END IF;

    FOR r IN SELECT value FROM jsonb_array_elements(p_items) LOOP
        n := n + 1;
        v_cat := r ->> 'catalog_type';
        v_ref := NULL; v_code := NULL; v_name := NULL; v_price := NULL; v_sample := NULL;
        IF v_cat = 'lab_test' THEN
            v_ref := NULLIF(r ->> 'catalog_reference_id', '')::UUID;
            SELECT t.name, t.price_paise, t.sample_type INTO v_name, v_price, v_sample
              FROM lab_tests t WHERE t.id = v_ref AND t.clinic_id = p_clinic_id AND t.is_active;
            IF NOT FOUND THEN RAISE EXCEPTION 'opd_order_catalog_not_found:%', n USING ERRCODE = 'P0001'; END IF;
        ELSIF v_cat = 'service_catalog' THEN
            v_code := r ->> 'catalog_code';
            SELECT c ->> 'name', (c ->> 'price_paise')::INTEGER INTO v_name, v_price
              FROM jsonb_array_elements(v_catalog) c
             WHERE c ->> 'code' = v_code AND COALESCE((c ->> 'active')::BOOLEAN, true)
             LIMIT 1;
            IF NOT FOUND THEN RAISE EXCEPTION 'opd_order_catalog_not_found:%', n USING ERRCODE = 'P0001'; END IF;
            v_sample := NULLIF(btrim(r ->> 'sample_type'), '');
        ELSIF v_cat = 'custom' THEN
            v_name := btrim(r ->> 'item_name');
            v_price := COALESCE((r ->> 'unit_price_paise')::INTEGER, 0);
            v_sample := NULLIF(btrim(r ->> 'sample_type'), '');
        ELSE
            RAISE EXCEPTION 'opd_order_bad_catalog:%', n USING ERRCODE = 'P0001';
        END IF;

        v_id := NULL;
        INSERT INTO opd_orders (clinic_id, branch_id, source, encounter_id, appointment_id, patient_id,
                                family_member_id, doctor_id, order_type, catalog_type, catalog_reference_id,
                                catalog_code, item_name, sample_type, urgency, clinical_notes, unit_price_paise,
                                quantity, ordered_by, ordered_by_name, idempotency_key, created_at)
        VALUES (p_clinic_id, e.branch_id, 'opd', e.id, e.appointment_id, e.patient_id, e.family_member_id,
                e.doctor_id, r ->> 'order_type', v_cat, v_ref, v_code, left(v_name, 200), left(v_sample, 60),
                COALESCE(r ->> 'urgency', 'routine'), NULLIF(btrim(r ->> 'clinical_notes'), ''),
                COALESCE(v_price, 0), COALESCE((r ->> 'quantity')::SMALLINT, 1), p_actor_id,
                -- clock_timestamp: lines of one call keep the doctor's order on the bill and worklist
                left(p_actor_name, 100), p_idempotency_key || ':' || n, clock_timestamp())
        ON CONFLICT (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
        RETURNING id INTO v_id;
        IF v_id IS NULL THEN
            -- a concurrent call with this key committed first: its orders are the answer
            IF n = 1 THEN
                RETURN jsonb_build_object('replayed', true, 'orders',
                    (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.created_at, x.id) FROM opd_orders x
                      WHERE x.clinic_id = p_clinic_id AND left(x.idempotency_key, length(p_idempotency_key) + 1)
                                                          = p_idempotency_key || ':'));
            END IF;
            RAISE EXCEPTION 'opd_order_key_reused' USING ERRCODE = 'P0001';
        END IF;
        v_ids := v_ids || v_id;
    END LOOP;

    v_billing := opd_bill_orders(p_clinic_id, e.appointment_id, v_ids, false, p_actor_id);
    RETURN jsonb_build_object('replayed', false, 'billing', v_billing, 'orders',
        (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.created_at, x.id) FROM opd_orders x
          WHERE x.clinic_id = p_clinic_id AND x.id = ANY (v_ids)));
END $$;

-- ── 8. Cancel an OPD order ─────────────────────────────────────────────────
-- Draft bill: its line is removed in the same transaction. Issued or paid bill:
-- refused - the money path is refund at the counter, then void the bill (which
-- returns the order to 'ordered'), then cancel. Lock order matches the bill
-- writers (appointment -> invoice -> order) so the two cannot deadlock.
CREATE OR REPLACE FUNCTION opd_cancel_order(
    p_clinic_id UUID, p_order_id UUID, p_reason TEXT, p_actor_id UUID)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE o opd_orders; v_inv opd_invoices; v_line UUID; v_line_inv UUID; v_line_total INTEGER; v_removed BOOLEAN := false;
BEGIN
    SELECT * INTO o FROM opd_orders WHERE id = p_order_id AND clinic_id = p_clinic_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_order_not_found' USING ERRCODE = 'P0002'; END IF;
    IF o.source <> 'opd' THEN RAISE EXCEPTION 'opd_order_ipd_managed' USING ERRCODE = 'P0001'; END IF;
    PERFORM 1 FROM appointments WHERE id = o.appointment_id AND clinic_id = p_clinic_id FOR UPDATE;

    SELECT it.invoice_id INTO v_line_inv FROM opd_invoice_items it
      JOIN opd_invoices i ON i.id = it.invoice_id AND i.clinic_id = it.clinic_id
     WHERE it.clinic_id = p_clinic_id AND it.order_id = p_order_id AND i.status <> 'void' LIMIT 1;
    IF v_line_inv IS NOT NULL THEN
        SELECT * INTO v_inv FROM opd_invoices WHERE id = v_line_inv AND clinic_id = p_clinic_id FOR UPDATE;
    END IF;
    SELECT * INTO o FROM opd_orders WHERE id = p_order_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF o.status = 'cancelled' THEN
        RETURN jsonb_build_object('order', to_jsonb(o), 'removed_line', false, 'replayed', true);
    END IF;
    IF o.status IN ('resulted', 'completed') THEN
        RAISE EXCEPTION 'opd_order_not_cancellable:%', o.status USING ERRCODE = 'P0001';
    END IF;

    -- re-read under the locks: a draft save may have replaced the lines meanwhile
    v_line := NULL;
    SELECT it.id, it.invoice_id, it.line_total_paise INTO v_line, v_line_inv, v_line_total
      FROM opd_invoice_items it
      JOIN opd_invoices i ON i.id = it.invoice_id AND i.clinic_id = it.clinic_id
     WHERE it.clinic_id = p_clinic_id AND it.order_id = p_order_id AND i.status <> 'void' LIMIT 1;
    IF v_line IS NOT NULL THEN
        IF v_inv.id IS DISTINCT FROM v_line_inv THEN
            RAISE EXCEPTION 'opd_order_bill_changed' USING ERRCODE = 'P0001';
        END IF;
        IF v_inv.status IN ('paid', 'partially_paid') THEN
            RAISE EXCEPTION 'opd_order_paid' USING ERRCODE = 'P0001';
        END IF;
        IF v_inv.status <> 'draft' THEN
            RAISE EXCEPTION 'opd_order_on_issued_invoice' USING ERRCODE = 'P0001';
        END IF;
        IF v_inv.discount_paise > v_inv.subtotal_paise - v_line_total THEN
            RAISE EXCEPTION 'opd_order_discount_exceeds' USING ERRCODE = 'P0001';
        END IF;
        DELETE FROM opd_invoice_items WHERE id = v_line AND clinic_id = p_clinic_id;
        v_removed := true;
    END IF;

    UPDATE opd_orders SET status = 'cancelled', cancelled_at = now(), cancelled_by = p_actor_id,
                          cancellation_reason = btrim(p_reason)
     WHERE id = p_order_id AND clinic_id = p_clinic_id
    RETURNING * INTO o;
    RETURN jsonb_build_object('order', to_jsonb(o), 'removed_line', v_removed, 'replayed', false,
                              'invoice_id', v_line_inv);
END $$;

-- ── 9. Draft-bill save (114) carries the order link through ────────────────
CREATE OR REPLACE FUNCTION opd_save_invoice_draft(
    p_clinic_id UUID, p_invoice_id UUID, p_expected_updated_at TIMESTAMPTZ, p_items JSONB,
    p_discount_paise INTEGER, p_discount_reason TEXT, p_notes TEXT)
RETURNS SETOF opd_invoices LANGUAGE plpgsql AS $$
DECLARE i opd_invoices; r JSONB; n INTEGER := 0;
BEGIN
    SELECT * INTO i FROM opd_invoices WHERE id = p_invoice_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;
    IF i.status <> 'draft' THEN RAISE EXCEPTION 'opd_not_draft:%', i.status USING ERRCODE = 'P0001'; END IF;
    IF i.updated_at IS DISTINCT FROM p_expected_updated_at THEN
        RAISE EXCEPTION 'opd_stale_draft' USING ERRCODE = 'P0001';
    END IF;
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION 'opd_invoice_empty' USING ERRCODE = 'P0001';
    END IF;
    -- the subtotal follows the lines (opd_recalc_invoice_subtotal); clear the discount first
    UPDATE opd_invoices SET discount_paise = 0 WHERE id = p_invoice_id AND clinic_id = p_clinic_id;
    DELETE FROM opd_invoice_items WHERE clinic_id = p_clinic_id AND invoice_id = p_invoice_id;
    FOR r IN SELECT value FROM jsonb_array_elements(p_items) LOOP
        n := n + 1;
        INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, catalog_code, doctor_id,
                                       lab_test_id, description, quantity, unit_price_paise, discount_paise,
                                       order_id)
        VALUES (p_clinic_id, p_invoice_id, n, r ->> 'item_type', NULLIF(r ->> 'catalog_code', ''),
                NULLIF(r ->> 'doctor_id', '')::UUID, NULLIF(r ->> 'lab_test_id', '')::UUID, r ->> 'description',
                (r ->> 'quantity')::SMALLINT, (r ->> 'unit_price_paise')::INTEGER,
                COALESCE((r ->> 'discount_paise')::INTEGER, 0), NULLIF(r ->> 'order_id', '')::UUID);
    END LOOP;
    RETURN QUERY
    UPDATE opd_invoices
       SET discount_paise = p_discount_paise,
           discount_reason = CASE WHEN p_discount_paise > 0 THEN p_discount_reason END,
           notes = p_notes
     WHERE id = p_invoice_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;

-- ── 10. IPD: investigation / procedure orders reach the worklist and bill ──
CREATE OR REPLACE FUNCTION ipd_order_to_worklist() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE a RECORD; v_name TEXT; v_price INTEGER; v_sample TEXT;
BEGIN
    IF NEW.order_type NOT IN ('investigation', 'procedure') THEN RETURN NULL; END IF;
    SELECT patient_id, family_member_id, branch_id INTO a
      FROM ipd_admissions WHERE id = NEW.admission_id AND clinic_id = NEW.clinic_id;
    IF NEW.lab_test_id IS NOT NULL THEN
        SELECT t.name, t.price_paise, t.sample_type INTO v_name, v_price, v_sample
          FROM lab_tests t WHERE t.id = NEW.lab_test_id AND t.clinic_id = NEW.clinic_id;
    END IF;
    INSERT INTO opd_orders (clinic_id, branch_id, source, admission_id, ipd_order_id, patient_id, family_member_id,
                            doctor_id, order_type, catalog_type, catalog_reference_id, item_name, sample_type,
                            urgency, clinical_notes, unit_price_paise, ordered_by, ordered_by_name, created_at)
    VALUES (NEW.clinic_id, a.branch_id, 'ipd', NEW.admission_id, NEW.id, a.patient_id, a.family_member_id,
            NEW.ordered_by_doctor_id, NEW.order_type,
            CASE WHEN v_name IS NOT NULL THEN 'lab_test' ELSE 'custom' END,
            CASE WHEN v_name IS NOT NULL THEN NEW.lab_test_id END,
            left(COALESCE(v_name, NEW.description), 200), left(v_sample, 60), NEW.priority,
            left(NEW.instructions, 1000), COALESCE(v_price, 0), NEW.ordered_by, left(NEW.ordered_by_name, 100),
            NEW.ordered_at)
    ON CONFLICT (clinic_id, ipd_order_id) WHERE ipd_order_id IS NOT NULL DO NOTHING;
    -- a priced test is a PROPOSED charge: billing staff post or void it as with every other charge
    IF COALESCE(v_price, 0) > 0 THEN
        INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date, quantity,
                                 unit_price_paise, source, source_ref, created_by, created_by_name)
        VALUES (NEW.clinic_id, NEW.admission_id, NEW.order_type, left(v_name, 200),
                (now() AT TIME ZONE 'Asia/Kolkata')::DATE, 1, v_price, 'order', 'order:' || NEW.id,
                NEW.ordered_by, left(NEW.ordered_by_name, 100))
        ON CONFLICT (clinic_id, admission_id, source_ref) WHERE source_ref IS NOT NULL AND status <> 'void' DO NOTHING;
    END IF;
    RETURN NULL;
END $$;

-- Discontinued IPD order: its worklist row is cancelled; its charge is voided
-- unless the sample was already taken (work done) or the charge is on the bill.
CREATE OR REPLACE FUNCTION ipd_order_end_to_worklist() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_reason TEXT; v_collected BOOLEAN;
BEGIN
    IF NOT (OLD.status = 'active' AND NEW.status = 'discontinued')
       OR NEW.order_type NOT IN ('investigation', 'procedure') THEN
        RETURN NULL;
    END IF;
    v_reason := CASE WHEN char_length(btrim(COALESCE(NEW.end_reason, ''))) >= 3 THEN left(btrim(NEW.end_reason), 270)
                     ELSE 'Order discontinued' END;
    SELECT sample_collected_at IS NOT NULL INTO v_collected
      FROM opd_orders WHERE clinic_id = NEW.clinic_id AND ipd_order_id = NEW.id;
    UPDATE opd_orders SET status = 'cancelled', cancelled_at = now(), cancelled_by = NEW.ended_by,
                          cancellation_reason = v_reason
     WHERE clinic_id = NEW.clinic_id AND ipd_order_id = NEW.id
       AND status IN ('ordered', 'billed', 'sample_collected', 'in_progress');
    IF NOT COALESCE(v_collected, false) THEN
        UPDATE ipd_charges SET status = 'void', voided_at = now(), voided_by = NEW.ended_by,
                               void_reason = 'Order discontinued: ' || v_reason
         WHERE clinic_id = NEW.clinic_id AND admission_id = NEW.admission_id AND source = 'order'
           AND source_ref = 'order:' || NEW.id AND status IN ('proposed', 'posted') AND invoice_id IS NULL;
    END IF;
    RETURN NULL;
END $$;

-- A completed IPD worklist row completes its ward order.
CREATE OR REPLACE FUNCTION opd_order_completed_to_ipd() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source = 'ipd' AND NEW.status = 'completed' AND OLD.status <> 'completed' THEN
        UPDATE ipd_orders SET status = 'completed', ended_at = now(), ended_by = NEW.resulted_by
         WHERE clinic_id = NEW.clinic_id AND id = NEW.ipd_order_id AND status = 'active';
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_ipd_orders_worklist ON ipd_orders;
CREATE TRIGGER trg_ipd_orders_worklist AFTER INSERT ON ipd_orders
    FOR EACH ROW EXECUTE FUNCTION ipd_order_to_worklist();
DROP TRIGGER IF EXISTS trg_ipd_orders_worklist_end ON ipd_orders;
CREATE TRIGGER trg_ipd_orders_worklist_end AFTER UPDATE OF status ON ipd_orders
    FOR EACH ROW EXECUTE FUNCTION ipd_order_end_to_worklist();
DROP TRIGGER IF EXISTS trg_opd_orders_complete_ipd ON opd_orders;
CREATE TRIGGER trg_opd_orders_complete_ipd AFTER UPDATE OF status ON opd_orders
    FOR EACH ROW EXECUTE FUNCTION opd_order_completed_to_ipd();

-- ── 11. Function hygiene + API-role lockout (103/114 pattern) ──────────────
ALTER FUNCTION opd_guard_order() SET search_path = public, pg_temp;
ALTER FUNCTION opd_guard_invoice_item_order() SET search_path = public, pg_temp;
ALTER FUNCTION opd_invoice_orders_billing() SET search_path = public, pg_temp;
ALTER FUNCTION opd_bill_orders(uuid, uuid, uuid[], boolean, uuid) SET search_path = public, pg_temp;
ALTER FUNCTION opd_place_orders(uuid, uuid, jsonb, text, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION opd_cancel_order(uuid, uuid, text, uuid) SET search_path = public, pg_temp;
ALTER FUNCTION opd_save_invoice_draft(uuid, uuid, timestamptz, jsonb, integer, text, text) SET search_path = public, pg_temp;
ALTER FUNCTION ipd_order_to_worklist() SET search_path = public, pg_temp;
ALTER FUNCTION ipd_order_end_to_worklist() SET search_path = public, pg_temp;
ALTER FUNCTION opd_order_completed_to_ipd() SET search_path = public, pg_temp;

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY[
        'opd_bill_orders(uuid,uuid,uuid[],boolean,uuid)',
        'opd_place_orders(uuid,uuid,jsonb,text,uuid,text)',
        'opd_cancel_order(uuid,uuid,text,uuid)',
        'opd_save_invoice_draft(uuid,uuid,timestamptz,jsonb,integer,text,text)']
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', f);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', f);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', f);
        END IF;
    END LOOP;
END $$;

-- RLS parity (049/103 pattern); the service_role connection bypasses it, the
-- application's clinic predicate is the boundary (app/tenancy.py).
DO $$
BEGIN
    ALTER TABLE opd_orders ENABLE ROW LEVEL SECURITY;
    ALTER TABLE opd_orders FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS service_role_all_opd_orders ON opd_orders;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE POLICY service_role_all_opd_orders ON opd_orders FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
    DROP POLICY IF EXISTS tenant_isolation_opd_orders ON opd_orders;
    CREATE POLICY tenant_isolation_opd_orders ON opd_orders FOR ALL
        USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
        WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE opd_orders FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE opd_orders FROM authenticated;
    END IF;
END $$;

-- ── 12. Verify (fails the transaction if anything is missing) ──────────────
DO $$
BEGIN
    IF to_regclass('public.opd_orders') IS NULL THEN
        RAISE EXCEPTION '115 verify: opd_orders missing';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conrelid = 'opd_orders'::regclass AND conname IN (
          'opd_orders_clinic_id_id_key', 'opd_orders_source_shape', 'opd_orders_encounter_fk',
          'opd_orders_ipd_order_fk', 'opd_orders_patient_fk', 'opd_orders_doctor_fk',
          'opd_orders_cancel_consistent', 'opd_orders_sample_consistent')) <> 8 THEN
        RAISE EXCEPTION '115 verify: opd_orders constraints missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'opd_invoice_items_order_fk') THEN
        RAISE EXCEPTION '115 verify: opd_invoice_items.order_id link missing';
    END IF;
    IF (SELECT count(*) FROM pg_indexes WHERE tablename = 'opd_orders'
          AND indexname IN ('uq_opd_orders_idempotency', 'uq_opd_orders_ipd_order')) <> 2 THEN
        RAISE EXCEPTION '115 verify: opd_orders unique indexes missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN (
          'trg_opd_orders_guard', 'trg_opd_orders_touch', 'trg_opd_inv_items_order', 'trg_opd_invoices_orders',
          'trg_ipd_orders_worklist', 'trg_ipd_orders_worklist_end', 'trg_opd_orders_complete_ipd')) <> 7 THEN
        RAISE EXCEPTION '115 verify: triggers missing';
    END IF;
    IF to_regprocedure('opd_place_orders(uuid,uuid,jsonb,text,uuid,text)') IS NULL
       OR to_regprocedure('opd_bill_orders(uuid,uuid,uuid[],boolean,uuid)') IS NULL
       OR to_regprocedure('opd_cancel_order(uuid,uuid,text,uuid)') IS NULL THEN
        RAISE EXCEPTION '115 verify: functions missing';
    END IF;
END $$;
