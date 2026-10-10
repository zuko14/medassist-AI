"""Real PostgreSQL test suite for Migration 103 (Kriya OPD OS core).

Verifies all database-level invariants, composite tenant FKs, slot uniqueness,
queue status expansion, record immutability triggers, gapless invoice sequencing,
append-only receipts, cashier shifts, MRN generation, RPC security, purge,
and 103_down rollback & re-apply.
"""

import concurrent.futures
import json
import os
import threading
import uuid
import psycopg2
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Test Data Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _create_clinic(cur, name="OPD Clinic"):
    cur.execute(
        """
        INSERT INTO clinics (name, whatsapp_number, plan, is_active, opd_state, opd_settings, opd_counters)
        VALUES (%s, %s, 'polyclinic', true, 'READY', '{}'::jsonb, '{}'::jsonb)
        RETURNING id;
        """,
        (name, "+9198" + uuid.uuid4().hex[:8]),
    )
    return str(cur.fetchone()[0])


def _create_doctor(cur, clinic_id, name="Dr. Sharma", reg_num="MCI/2015/12345"):
    cur.execute(
        """
        INSERT INTO doctors (clinic_id, name, department, specialization, registration_number, registration_council)
        VALUES (%s, %s, 'General Medicine', 'Physician', %s, 'Medical Council of India')
        RETURNING id;
        """,
        (clinic_id, name, reg_num),
    )
    return str(cur.fetchone()[0])


def _create_patient(cur, clinic_id, name="Rahul Verma", phone=None):
    if phone is None:
        phone = "+9191" + uuid.uuid4().hex[:8]
    cur.execute(
        """
        INSERT INTO patients (clinic_id, name, phone, age_years, age_recorded_on, gender)
        VALUES (%s, %s, %s, 32, CURRENT_DATE, 'male')
        RETURNING id;
        """,
        (clinic_id, name, phone),
    )
    return str(cur.fetchone()[0]), phone


def _create_appointment(cur, clinic_id, doctor_id, patient_phone, time_="10:00:00", is_walk_in=False, status="confirmed"):
    cur.execute(
        """
        INSERT INTO appointments (
            clinic_id, patient_phone, patient_name, doctor_id, doctor_name, department,
            appointment_date, appointment_time, status, queue_status, is_walk_in, booking_type
        )
        VALUES (
            %s, %s, 'Test Patient', %s, 'Dr. Sharma', 'General Medicine',
            '2026-10-10', %s, %s, 'waiting', %s, 'consultation'
        )
        RETURNING id;
        """,
        (clinic_id, patient_phone, doctor_id, time_, status, is_walk_in),
    )
    return str(cur.fetchone()[0])


def _create_staff(cur, clinic_id, username, doctor_id=None, role="staff", staff_role="DOCTOR"):
    cur.execute(
        """
        INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, doctor_id, is_active)
        VALUES (%s, %s, 'dummy_hash', %s, %s, %s, true)
        RETURNING id;
        """,
        (clinic_id, username + "_" + uuid.uuid4().hex[:6], role, staff_role, doctor_id),
    )
    return str(cur.fetchone()[0])


# ─────────────────────────────────────────────────────────────────────────────
# 1. Idempotency & Indexdef Check
# ─────────────────────────────────────────────────────────────────────────────


def test_103_idempotency_and_indexdef(real_pg_conn):
    """Applying 103_opd_os_core.sql a second time is a complete no-op (idempotent)."""
    cur = real_pg_conn.cursor()
    migration_path = os.path.join(os.path.dirname(__file__), "..", "migrations", "103_opd_os_core.sql")
    with open(migration_path, "r", encoding="utf-8") as f:
        sql = f.read()

    # Should execute without error
    cur.execute(sql)

    # Verify appointments slot indexes include 'is_walk_in = false'
    cur.execute(
        """
        SELECT count(*) FROM pg_indexes
        WHERE tablename = 'appointments'
          AND indexname IN ('uq_appointment_active_slot', 'uq_appointment_active_slot_unassigned')
          AND indexdef ILIKE '%is_walk_in = false%';
        """
    )
    assert cur.fetchone()[0] == 2


# ─────────────────────────────────────────────────────────────────────────────
# 2. Cross-Tenant Composite Foreign Key (23503)
# ─────────────────────────────────────────────────────────────────────────────


def test_103_cross_tenant_composite_fk_violation(real_pg_conn):
    """Encounter with clinic A and patient of clinic B fails with FK violation 23503."""
    cur = real_pg_conn.cursor()
    c_a = _create_clinic(cur, "Tenant A")
    c_b = _create_clinic(cur, "Tenant B")
    try:
        doc_a = _create_doctor(cur, c_a)
        pat_b, _ = _create_patient(cur, c_b)
        apt_a = _create_appointment(cur, c_a, doc_a, "+919876543210")

        with pytest.raises(psycopg2.errors.ForeignKeyViolation) as exc:
            cur.execute(
                """
                INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id, status)
                VALUES (%s, %s, %s, %s, 'draft');
                """,
                (c_a, apt_a, pat_b, doc_a),
            )
        assert "23503" in str(exc.value.pgcode) or "opd_encounters_patient_fk" in str(exc.value)
    finally:
        cur.execute("DELETE FROM clinics WHERE id IN (%s, %s);", (c_a, c_b))


# ─────────────────────────────────────────────────────────────────────────────
# 3. Slot Guards: Two Bookings Collide, Walk-in Allowed
# ─────────────────────────────────────────────────────────────────────────────


def test_103_slot_guards_ignore_walk_ins(real_pg_conn):
    """Two scheduled bookings collide on uq_appointment_active_slot; walk-in allowed at same time."""
    cur = real_pg_conn.cursor()
    c = _create_clinic(cur, "Slot Test Clinic")
    try:
        doc = _create_doctor(cur, c)
        _create_appointment(cur, c, doc, "+919876543210", time_="10:00:00", is_walk_in=False)

        # Same doctor, same date, same time -> unique violation
        with pytest.raises(psycopg2.errors.UniqueViolation) as exc:
            _create_appointment(cur, c, doc, "+919876543211", time_="10:00:00", is_walk_in=False)
        assert "uq_appointment_active_slot" in str(exc.value)

        # Walk-in at the exact same minute succeeds
        walk_in_id = _create_appointment(cur, c, doc, "+919876543212", time_="10:00:00", is_walk_in=True)
        assert walk_in_id is not None
    finally:
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))


# ─────────────────────────────────────────────────────────────────────────────
# 4. Queue Status Check: 8 Valid Accepted, Invalid Rejected
# ─────────────────────────────────────────────────────────────────────────────


def test_103_queue_status_check(real_pg_conn):
    """appointments_queue_status_check accepts 8 valid states (including 'done') and rejects others."""
    cur = real_pg_conn.cursor()
    c = _create_clinic(cur, "Queue Status Clinic")
    try:
        doc = _create_doctor(cur, c)
        apt_id = _create_appointment(cur, c, doc, "+919876543210", time_="11:00:00")

        valid_statuses = [
            "registered", "vitals_pending", "waiting", "in_consultation",
            "billing", "completed", "cancelled", "done"
        ]
        for st in valid_statuses:
            cur.execute("UPDATE appointments SET queue_status = %s WHERE id = %s;", (st, apt_id))

        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE appointments SET queue_status = 'invalid_stage' WHERE id = %s;", (apt_id,))
    finally:
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))


# ─────────────────────────────────────────────────────────────────────────────
# 5. Encounter Immutability Triggers & Supersede Flow
# ─────────────────────────────────────────────────────────────────────────────


def test_103_signed_encounter_immutability(real_pg_conn):
    """Signed encounters cannot be updated (clinical fields) or deleted; superseded only via RPC."""
    cur = real_pg_conn.cursor()
    c = _create_clinic(cur, "Clinical Immutability Clinic")
    try:
        doc = _create_doctor(cur, c)
        admin_id = _create_staff(cur, c, "dr_user", doctor_id=doc)
        pat, phone = _create_patient(cur, c)
        apt = _create_appointment(cur, c, doc, phone, time_="11:30:00")

        # Create draft encounter
        cur.execute(
            """
            INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id, status, chief_complaints)
            VALUES (%s, %s, %s, %s, 'draft', 'Fever')
            RETURNING id;
            """,
            (c, apt, pat, doc),
        )
        enc_id = str(cur.fetchone()[0])

        # Sign encounter via RPC
        signer_snap = json.dumps({"name": "Dr. Sharma", "registration_number": "MCI/2015/12345"})
        cur.execute(
            "SELECT id, status FROM opd_sign_encounter(%s, %s, %s, %s, %s::jsonb);",
            (c, enc_id, admin_id, doc, signer_snap),
        )
        row = cur.fetchone()
        assert row[1] == "signed"

        # UPDATE clinical field on signed encounter raises exception
        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute("UPDATE opd_encounters SET chief_complaints = 'Severe Fever' WHERE id = %s;", (enc_id,))
        assert "opd_record_locked" in str(exc.value)

        # DELETE signed encounter raises exception
        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute("DELETE FROM opd_encounters WHERE id = %s;", (enc_id,))
        assert "opd_record_locked" in str(exc.value)

        # Amendment: create version 2 draft superseding version 1
        cur.execute(
            """
            INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id, version, supersedes_id, status, chief_complaints)
            VALUES (%s, %s, %s, %s, 2, %s, 'draft', 'Fever and Cough')
            RETURNING id;
            """,
            (c, apt, pat, doc, enc_id),
        )
        enc_v2 = str(cur.fetchone()[0])

        # Sign v2: v1 becomes superseded, v2 becomes signed
        cur.execute(
            "SELECT id, status FROM opd_sign_encounter(%s, %s, %s, %s, %s::jsonb);",
            (c, enc_v2, admin_id, doc, signer_snap),
        )
        assert cur.fetchone()[1] == "signed"

        cur.execute("SELECT status FROM opd_encounters WHERE id = %s;", (enc_id,))
        assert cur.fetchone()[0] == "superseded"
    finally:
        cur.execute("SELECT opd_purge_clinic(%s);", (c,))
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Prescription Items Immutability & Delivery Columns Update
# ─────────────────────────────────────────────────────────────────────────────


def test_103_prescription_items_immutability(real_pg_conn):
    """Prescription items are locked when prescription is signed; delivery columns remain updatable."""
    cur = real_pg_conn.cursor()
    c = _create_clinic(cur, "Prescription Clinic")
    try:
        doc = _create_doctor(cur, c)
        admin_id = _create_staff(cur, c, "dr_rx_user", doctor_id=doc)
        pat, phone = _create_patient(cur, c)
        apt = _create_appointment(cur, c, doc, phone, time_="12:00:00")

        # Create encounter and sign it
        signer_snap = json.dumps({"name": "Dr. Sharma", "registration_number": "MCI/2015/12345"})
        cur.execute(
            """
            INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id, status)
            VALUES (%s, %s, %s, %s, 'draft') RETURNING id;
            """,
            (c, apt, pat, doc),
        )
        enc_id = str(cur.fetchone()[0])
        cur.execute("SELECT * FROM opd_sign_encounter(%s, %s, %s, %s, %s::jsonb);",
                    (c, enc_id, admin_id, doc, signer_snap))

        # Create draft prescription + items
        cur.execute(
            """
            INSERT INTO opd_prescriptions (clinic_id, encounter_id, appointment_id, patient_id, doctor_id, status)
            VALUES (%s, %s, %s, %s, %s, 'draft') RETURNING id;
            """,
            (c, enc_id, apt, pat, doc),
        )
        rx_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO opd_prescription_items (clinic_id, prescription_id, line_no, drug_name, formulation, dosage, frequency)
            VALUES (%s, %s, 1, 'Paracetamol', 'tablet', '650mg', 'TDS') RETURNING id;
            """,
            (c, rx_id),
        )
        item_id = str(cur.fetchone()[0])

        # Sign prescription via RPC
        cur.execute(
            """
            SELECT * FROM opd_sign_prescription(
                %s, %s, %s, %s, %s::jsonb,
                '{"clinic_name":"Prescription Clinic"}'::jsonb,
                '{"patient_name":"Rahul Verma"}'::jsonb,
                '[]'::jsonb
            );
            """,
            (c, rx_id, admin_id, doc, signer_snap),
        )
        assert cur.fetchone() is not None

        # Modifying prescription items now raises exception
        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute("UPDATE opd_prescription_items SET dosage = '500mg' WHERE id = %s;", (item_id,))
        assert "opd_record_locked" in str(exc.value)

        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute("DELETE FROM opd_prescription_items WHERE id = %s;", (item_id,))
        assert "opd_record_locked" in str(exc.value)

        # Delivery columns on opd_prescriptions CAN still be updated
        cur.execute(
            """
            UPDATE opd_prescriptions
            SET delivery_status = 'sent', whatsapp_message_id = 'wamid.123', send_count = 1
            WHERE id = %s;
            """,
            (rx_id,),
        )
        cur.execute("SELECT delivery_status, send_count FROM opd_prescriptions WHERE id = %s;", (rx_id,))
        assert cur.fetchone() == ("sent", 1)
    finally:
        cur.execute("SELECT opd_purge_clinic(%s);", (c,))
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))


# ─────────────────────────────────────────────────────────────────────────────
# 7. Invoices: paid_paise is Derived & Gapless Concurrency
# ─────────────────────────────────────────────────────────────────────────────


def test_103_invoice_paid_derived_and_gapless_concurrency(real_postgres_uri, real_pg_conn):
    """Direct update of paid_paise is rejected; concurrent issuing yields gapless INV-YYYY-00001..20."""
    cur = real_pg_conn.cursor()
    c = _create_clinic(cur, "Invoice Gapless Clinic")
    try:
        pat, phone = _create_patient(cur, c)

        # Create an invoice with items
        cur.execute(
            """
            INSERT INTO opd_invoices (clinic_id, patient_id, status)
            VALUES (%s, %s, 'draft') RETURNING id;
            """,
            (c, pat),
        )
        inv_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, quantity, unit_price_paise)
            VALUES (%s, %s, 1, 'consultation', 'Doctor Consult', 1, 50000);
            """,
            (c, inv_id),
        )

        # Issue the invoice
        cur.execute("UPDATE opd_invoices SET status = 'issued' WHERE id = %s RETURNING invoice_number, invoice_seq;", (inv_id,))
        inv_num, seq = cur.fetchone()
        assert seq == 1
        assert "INV-" in inv_num and "-00001" in inv_num

        # Direct update of paid_paise raises opd_paid_is_derived
        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute("UPDATE opd_invoices SET paid_paise = 10000 WHERE id = %s;", (inv_id,))
        assert "opd_paid_is_derived" in str(exc.value)

        # Test concurrent issuing across 19 more invoices (total 20)
        draft_ids = []
        for i in range(2, 21):
            cur.execute(
                "INSERT INTO opd_invoices (clinic_id, patient_id, status) VALUES (%s, %s, 'draft') RETURNING id;",
                (c, pat),
            )
            d_id = str(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, quantity, unit_price_paise)
                VALUES (%s, %s, 1, 'consultation', 'Consult', 1, 10000);
                """,
                (c, d_id),
            )
            draft_ids.append(d_id)

        issued_sequences = []

        def _issue_worker(d_id):
            conn = psycopg2.connect(real_postgres_uri)
            conn.autocommit = True
            try:
                k = conn.cursor()
                k.execute("UPDATE opd_invoices SET status = 'issued' WHERE id = %s RETURNING invoice_seq;", (d_id,))
                return k.fetchone()[0]
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            issued_sequences = list(executor.map(_issue_worker, draft_ids))

        all_sequences = sorted([seq] + issued_sequences)
        assert all_sequences == list(range(1, 21))
    finally:
        cur.execute("SELECT opd_purge_clinic(%s);", (c,))
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))


# ─────────────────────────────────────────────────────────────────────────────
# 8. Receipts: Append-Only, Overpayment Blocked, Refund Decrements
# ─────────────────────────────────────────────────────────────────────────────


def test_103_receipts_append_only_and_balance_transitions(real_pg_conn):
    """Receipts are append-only; overpayment raises check violation; refunds decrement paid_paise."""
    cur = real_pg_conn.cursor()
    c = _create_clinic(cur, "Receipts Ledger Clinic")
    try:
        pat, phone = _create_patient(cur, c)
        admin_id = _create_staff(cur, c, "cashier_user", role="staff", staff_role="CASHIER")

        # Open shift
        cur.execute(
            """
            INSERT INTO opd_cashier_shifts (clinic_id, cashier_admin_id, cashier_name, status, opening_float_paise)
            VALUES (%s, %s, 'Cashier User', 'open', 50000) RETURNING id;
            """,
            (c, admin_id),
        )
        shift_id = str(cur.fetchone()[0])

        # Invoice for 50000 paise (Rs 500)
        cur.execute(
            "INSERT INTO opd_invoices (clinic_id, patient_id, status) VALUES (%s, %s, 'draft') RETURNING id;",
            (c, pat),
        )
        inv_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, quantity, unit_price_paise)
            VALUES (%s, %s, 1, 'consultation', 'Consultation', 1, 50000);
            """,
            (c, inv_id),
        )
        cur.execute("UPDATE opd_invoices SET status = 'issued' WHERE id = %s;", (inv_id,))

        # Overpayment: 60000 paise on 50000 invoice raises CheckViolation
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO opd_receipts (clinic_id, invoice_id, shift_id, kind, mode, amount_paise, received_by_admin_id)
                VALUES (%s, %s, %s, 'payment', 'cash', 60000, %s);
                """,
                (c, inv_id, shift_id, admin_id),
            )

        # Valid payment: partial 30000 paise
        cur.execute(
            """
            INSERT INTO opd_receipts (clinic_id, invoice_id, shift_id, kind, mode, amount_paise, received_by_admin_id)
            VALUES (%s, %s, %s, 'payment', 'cash', 30000, %s) RETURNING id;
            """,
            (c, inv_id, shift_id, admin_id),
        )
        rct_id = str(cur.fetchone()[0])

        cur.execute("SELECT status, paid_paise FROM opd_invoices WHERE id = %s;", (inv_id,))
        assert cur.fetchone() == ("partially_paid", 30000)

        # Append-only check: UPDATE or DELETE receipt raises exception
        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute("UPDATE opd_receipts SET amount_paise = 20000 WHERE id = %s;", (rct_id,))
        assert "opd_append_only" in str(exc.value)

        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute("DELETE FROM opd_receipts WHERE id = %s;", (rct_id,))
        assert "opd_append_only" in str(exc.value)

        # Refund 10000 paise
        cur.execute(
            """
            INSERT INTO opd_receipts (clinic_id, invoice_id, shift_id, kind, mode, amount_paise, received_by_admin_id, reason)
            VALUES (%s, %s, %s, 'refund', 'cash', 10000, %s, 'Patient discount adjustment');
            """,
            (c, inv_id, shift_id, admin_id),
        )
        cur.execute("SELECT status, paid_paise FROM opd_invoices WHERE id = %s;", (inv_id,))
        assert cur.fetchone() == ("partially_paid", 20000)
    finally:
        cur.execute("SELECT opd_purge_clinic(%s);", (c,))
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))


# ─────────────────────────────────────────────────────────────────────────────
# 9. Shifts: Receipt into Closed Shift Fails; Shift Close Calculation
# ─────────────────────────────────────────────────────────────────────────────


def test_103_shifts_receipt_into_closed_and_close_shift(real_pg_conn):
    """Receipt into closed shift fails with opd_shift_not_open; opd_close_shift balances correctly."""
    cur = real_pg_conn.cursor()
    c = _create_clinic(cur, "Shift Calculations Clinic")
    try:
        pat, phone = _create_patient(cur, c)
        admin_id = _create_staff(cur, c, "cashier_close", role="staff", staff_role="CASHIER")

        # Open shift with 50000 opening float (Rs 500)
        cur.execute(
            """
            INSERT INTO opd_cashier_shifts (clinic_id, cashier_admin_id, cashier_name, status, opening_float_paise)
            VALUES (%s, %s, 'Cashier Close', 'open', 50000) RETURNING id;
            """,
            (c, admin_id),
        )
        shift_id = str(cur.fetchone()[0])

        # Invoice for 30000
        cur.execute("INSERT INTO opd_invoices (clinic_id, patient_id, status) VALUES (%s, %s, 'draft') RETURNING id;", (c, pat))
        inv_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, quantity, unit_price_paise)
            VALUES (%s, %s, 1, 'consultation', 'Consult', 1, 30000);
            """,
            (c, inv_id),
        )
        cur.execute("UPDATE opd_invoices SET status = 'issued' WHERE id = %s;", (inv_id,))

        # Payment of 30000 cash
        cur.execute(
            """
            INSERT INTO opd_receipts (clinic_id, invoice_id, shift_id, kind, mode, amount_paise, received_by_admin_id)
            VALUES (%s, %s, %s, 'payment', 'cash', 30000, %s);
            """,
            (c, inv_id, shift_id, admin_id),
        )
        # Refund of 5000 cash
        cur.execute(
            """
            INSERT INTO opd_receipts (clinic_id, invoice_id, shift_id, kind, mode, amount_paise, received_by_admin_id, reason)
            VALUES (%s, %s, %s, 'refund', 'cash', 5000, %s, 'Adjust');
            """,
            (c, inv_id, shift_id, admin_id),
        )

        # Expected: 50000 (float) + 30000 (payment) - 5000 (refund) = 75000
        cur.execute(
            "SELECT expected_cash_paise, declared_cash_paise, variance_paise FROM opd_close_shift(%s, %s, %s, 75000, 'Shift balanced');",
            (c, shift_id, admin_id),
        )
        exp, dec, var = cur.fetchone()
        assert exp == 75000
        assert dec == 75000
        assert var == 0

        # Attempting receipt into closed shift raises opd_shift_not_open
        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute(
                """
                INSERT INTO opd_receipts (clinic_id, invoice_id, shift_id, kind, mode, amount_paise, received_by_admin_id)
                VALUES (%s, %s, %s, 'payment', 'cash', 5000, %s);
                """,
                (c, inv_id, shift_id, admin_id),
            )
        assert "opd_shift_not_open" in str(exc.value)
    finally:
        cur.execute("SELECT opd_purge_clinic(%s);", (c,))
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))


# ─────────────────────────────────────────────────────────────────────────────
# 10. MRN Uniqueness Across Patients & Family Members
# ─────────────────────────────────────────────────────────────────────────────


def test_103_mrn_uniqueness_and_family_validation(real_pg_conn):
    """MRN is unique across patients and family_members; family member of another account holder fails."""
    cur = real_pg_conn.cursor()
    c = _create_clinic(cur, "MRN Clinic")
    try:
        pat_1, phone_1 = _create_patient(cur, c, name="Holder 1")
        pat_2, phone_2 = _create_patient(cur, c, name="Holder 2")

        # Assign MRN to Patient 1
        cur.execute("SELECT opd_assign_mrn(%s, %s);", (c, pat_1))
        mrn_1 = cur.fetchone()[0]
        assert mrn_1 == "MRN-000001"

        # Create family member under Patient 1
        cur.execute(
            """
            INSERT INTO family_members (clinic_id, primary_phone, full_name, relationship)
            VALUES (%s, %s, 'Child 1', 'child') RETURNING id;
            """,
            (c, phone_1),
        )
        fm_1 = str(cur.fetchone()[0])

        # Assign MRN to family member 1
        cur.execute("SELECT opd_assign_mrn(%s, %s, %s);", (c, pat_1, fm_1))
        mrn_fm = cur.fetchone()[0]
        assert mrn_fm == "MRN-000002"

        # Patient 2 trying to claim family member of Patient 1 raises opd_family_member_not_found
        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute("SELECT opd_assign_mrn(%s, %s, %s);", (c, pat_2, fm_1))
        assert "opd_family_member_not_found" in str(exc.value)
    finally:
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))


# ─────────────────────────────────────────────────────────────────────────────
# 11. Format Number Functionality (No Truncation)
# ─────────────────────────────────────────────────────────────────────────────


def test_103_opd_format_number(real_pg_conn):
    """opd_format_number pads up to width, and does not truncate larger sequences."""
    cur = real_pg_conn.cursor()
    cur.execute("SELECT opd_format_number('INV-', 2026, 123456, 5);")
    assert cur.fetchone()[0] == "INV-2026-123456"

    cur.execute("SELECT opd_format_number('INV-', 2026, 42, 5);")
    assert cur.fetchone()[0] == "INV-2026-00042"

    cur.execute("SELECT opd_format_number('MRN-', NULL, 7, 6);")
    assert cur.fetchone()[0] == "MRN-000007"


# ─────────────────────────────────────────────────────────────────────────────
# 12. RPCs Reject Foreign Clinic IDs
# ─────────────────────────────────────────────────────────────────────────────


def test_103_rpcs_refuse_cross_tenant_calls(real_pg_conn):
    """RPCs reject operation when p_clinic_id does not match the resource clinic_id."""
    cur = real_pg_conn.cursor()
    c_a = _create_clinic(cur, "RPC Tenant A")
    c_b = _create_clinic(cur, "RPC Tenant B")
    try:
        doc_a = _create_doctor(cur, c_a)
        admin_a = _create_staff(cur, c_a, "dr_a", doctor_id=doc_a)
        pat_a, phone_a = _create_patient(cur, c_a)
        apt_a = _create_appointment(cur, c_a, doc_a, phone_a)

        cur.execute(
            """
            INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id, status)
            VALUES (%s, %s, %s, %s, 'draft') RETURNING id;
            """,
            (c_a, apt_a, pat_a, doc_a),
        )
        enc_a = str(cur.fetchone()[0])

        signer_snap = json.dumps({"name": "Dr. Sharma", "registration_number": "MCI/2015/12345"})
        # Call with c_b -> raises opd_not_found
        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute("SELECT opd_sign_encounter(%s, %s, %s, %s, %s::jsonb);",
                        (c_b, enc_a, admin_a, doc_a, signer_snap))
        assert "opd_not_found" in str(exc.value)
    finally:
        cur.execute("SELECT opd_purge_clinic(%s);", (c_a,))
        cur.execute("DELETE FROM clinics WHERE id IN (%s, %s);", (c_a, c_b))


# ─────────────────────────────────────────────────────────────────────────────
# 13. Purge Clinic Enables Full Teardown
# ─────────────────────────────────────────────────────────────────────────────


def test_103_opd_purge_clinic_and_deletion(real_pg_conn):
    """Without purge, deleting a clinic with signed encounters fails; opd_purge_clinic clears data."""
    cur = real_pg_conn.cursor()
    c = _create_clinic(cur, "Purge Clinic")
    try:
        doc = _create_doctor(cur, c)
        admin_id = _create_staff(cur, c, "dr_purge", doctor_id=doc)
        pat, phone = _create_patient(cur, c)
        apt = _create_appointment(cur, c, doc, phone)

        # Create signed encounter
        cur.execute(
            "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id, status) VALUES (%s, %s, %s, %s, 'draft') RETURNING id;",
            (c, apt, pat, doc),
        )
        enc = str(cur.fetchone()[0])
        signer_snap = json.dumps({"name": "Dr. Sharma", "registration_number": "MCI/2015/12345"})
        cur.execute("SELECT opd_sign_encounter(%s, %s, %s, %s, %s::jsonb);", (c, enc, admin_id, doc, signer_snap))

        # Attempting DELETE FROM clinics fails because signed encounter delete trigger blocks it
        with pytest.raises(psycopg2.InternalError) as exc:
            cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))
        assert "opd_record_locked" in str(exc.value)

        # Call opd_purge_clinic
        cur.execute("SELECT opd_purge_clinic(%s);", (c,))
        res = cur.fetchone()[0]
        assert res.get("encounters") == 1

        # Now clinic deletion succeeds cleanly
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))
        cur.execute("SELECT count(*) FROM clinics WHERE id = %s;", (c,))
        assert cur.fetchone()[0] == 0
    except Exception:
        cur.execute("SELECT opd_purge_clinic(%s);", (c,))
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))
        raise


# ─────────────────────────────────────────────────────────────────────────────
# 14. 103 Rollback and Re-Apply
# ─────────────────────────────────────────────────────────────────────────────


def test_103_down_rollback_and_reapply(real_pg_conn):
    """103_down.sql drops OPD core cleanly; 103_opd_os_core.sql re-applies without errors."""
    cur = real_pg_conn.cursor()
    down_path = os.path.join(os.path.dirname(__file__), "..", "migrations", "rollback", "103_down.sql")
    up_path = os.path.join(os.path.dirname(__file__), "..", "migrations", "103_opd_os_core.sql")

    with open(down_path, "r", encoding="utf-8") as f:
        down_sql = f.read()
    with open(up_path, "r", encoding="utf-8") as f:
        up_sql = f.read()

    # Apply rollback
    cur.execute(down_sql)

    # Verify OPD tables are gone
    cur.execute(
        """
        SELECT count(*) FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename IN ('opd_encounters', 'opd_prescriptions', 'opd_invoices', 'opd_receipts', 'opd_cashier_shifts');
        """
    )
    assert cur.fetchone()[0] == 0

    # Re-apply migration 103
    cur.execute(up_sql)

    # Verify OPD tables are restored
    cur.execute(
        """
        SELECT count(*) FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename IN ('opd_encounters', 'opd_prescriptions', 'opd_invoices', 'opd_receipts', 'opd_cashier_shifts');
        """
    )
    assert cur.fetchone()[0] == 5
