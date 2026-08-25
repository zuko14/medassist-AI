"""Real PostgreSQL transactional invariant test suite.

Tests critical database constraints, partial unique indexes, triggers,
foreign key cascades, and concurrent operations against real PostgreSQL.
"""

import concurrent.futures
import psycopg2
import pytest


def get_default_clinic_id(cur) -> str:
    """Fetch seeded clinic ID or create one for test."""
    cur.execute("SELECT id FROM clinics LIMIT 1;")
    row = cur.fetchone()
    if row:
        return str(row[0])
    cur.execute("""
    INSERT INTO clinics (name, whatsapp_number, plan, is_active)
    VALUES ('Invariant Test Clinic', '+919999999999', 'essential', true)
    RETURNING id;
    """)
    return str(cur.fetchone()[0])


def test_01_migration_completeness_and_tracking(real_pg_conn):
    """Invariant 1: All migrations are applied and recorded in schema_migrations."""
    cur = real_pg_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM schema_migrations;")
    count = cur.fetchone()[0]
    assert count >= 45, f"Expected at least 45 migrations, got {count}"

    cur.execute("SELECT name, checksum FROM schema_migrations WHERE checksum IS NULL OR length(checksum) != 64;")
    bad_rows = cur.fetchall()
    assert len(bad_rows) == 0, f"Found migrations with invalid checksums: {bad_rows}"


def test_02_idx_unique_active_slot_prevents_double_booking(real_pg_conn, clean_db):
    """Invariant 2: idx_unique_active_slot blocks concurrent double-booking on same slot."""
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)

    # Insert first confirmed booking
    cur.execute("""
    INSERT INTO appointments (
        clinic_id, patient_phone, patient_name, department, doctor_name,
        appointment_date, appointment_time, status
    ) VALUES (
        %s, '+919876543210', 'Patient One', 'Cardiology', 'Dr. Sharma',
        '2026-09-01', '10:00:00', 'confirmed'
    );
    """, (clinic_id,))

    # Attempt second booking for exact same doctor, date, time
    with pytest.raises(psycopg2.errors.UniqueViolation) as exc_info:
        cur.execute("""
        INSERT INTO appointments (
            clinic_id, patient_phone, patient_name, department, doctor_name,
            appointment_date, appointment_time, status
        ) VALUES (
            %s, '+919876543211', 'Patient Two', 'Cardiology', 'Dr. Sharma',
            '2026-09-01', '10:00:00', 'pending_payment'
        );
        """, (clinic_id,))

    assert "idx_unique_active_slot" in str(exc_info.value) or "unique" in str(exc_info.value).lower()


def test_03_idx_unique_active_slot_permits_cancelled_or_expired(real_pg_conn, clean_db):
    """Invariant 3: Once a slot is cancelled or expired, a new booking is allowed."""
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)

    # Insert and cancel
    cur.execute("""
    INSERT INTO appointments (
        clinic_id, patient_phone, patient_name, department, doctor_name,
        appointment_date, appointment_time, status
    ) VALUES (
        %s, '+919876543210', 'Patient One', 'Cardiology', 'Dr. Sharma',
        '2026-09-01', '11:00:00', 'cancelled'
    );
    """, (clinic_id,))

    # Insert new booking on same slot - must succeed because previous is cancelled
    cur.execute("""
    INSERT INTO appointments (
        clinic_id, patient_phone, patient_name, department, doctor_name,
        appointment_date, appointment_time, status
    ) VALUES (
        %s, '+919876543211', 'Patient Two', 'Cardiology', 'Dr. Sharma',
        '2026-09-01', '11:00:00', 'confirmed'
    ) RETURNING id;
    """, (clinic_id,))
    new_id = cur.fetchone()[0]
    assert new_id is not None


def test_04_appointments_status_check_constraint(real_pg_conn, clean_db):
    """Invariant 4: appointments status check constraint allows valid statuses and rejects invalid ones."""
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)

    valid_statuses = [
        'confirmed', 'cancelled', 'rescheduled', 'completed',
        'no_show', 'pending_payment', 'expired', 'refunded', 'pending_review'
    ]

    for idx, status in enumerate(valid_statuses):
        cur.execute("""
        INSERT INTO appointments (
            clinic_id, patient_phone, department, doctor_name,
            appointment_date, appointment_time, status
        ) VALUES (
            %s, '+919876543210', 'General', 'Dr. General',
            '2026-09-02', %s, %s
        );
        """, (clinic_id, f"{10 + idx:02d}:00:00", status))

    # Test that illegal status raises CheckViolation
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""
        INSERT INTO appointments (
            clinic_id, patient_phone, department, doctor_name,
            appointment_date, appointment_time, status
        ) VALUES (
            %s, '+919876543210', 'General', 'Dr. General',
            '2026-09-02', '22:00:00', 'illegal_fake_status'
        );
        """, (clinic_id,))

    # PROOF OF P0-1: 'refunded_late_payment' is written by payment.py but rejected by DB schema
    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("""
        INSERT INTO appointments (
            clinic_id, patient_phone, department, doctor_name,
            appointment_date, appointment_time, status
        ) VALUES (
            %s, '+919876543210', 'General', 'Dr. General',
            '2026-09-02', '23:00:00', 'refunded_late_payment'
        );
        """, (clinic_id,))
    err_msg = str(exc_info.value).lower()
    assert "check" in err_msg or "value too long" in err_msg or "truncation" in err_msg


def test_05_payment_events_immutability_trigger(real_pg_conn, clean_db):
    """Invariant 5: payment_events table is append-only; UPDATE and DELETE are blocked by trigger."""
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)

    # First create a booking for the foreign key
    cur.execute("""
    INSERT INTO appointments (
        clinic_id, patient_phone, department, doctor_name,
        appointment_date, appointment_time, status
    ) VALUES (
        %s, '+919876543210', 'Cardiology', 'Dr. Sharma',
        '2026-09-04', '15:00:00', 'pending_payment'
    ) RETURNING id;
    """, (clinic_id,))
    booking_id = cur.fetchone()[0]

    # Insert payment event
    cur.execute("""
    INSERT INTO payment_events (
        booking_id, event_type, raw_payload
    ) VALUES (
        %s, 'payment.captured', '{"test": true}'::jsonb
    ) RETURNING id;
    """, (booking_id,))
    event_id = cur.fetchone()[0]

    # Attempt UPDATE -> trigger must raise exception
    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("UPDATE payment_events SET event_type = 'refund.completed' WHERE id = %s;", (event_id,))
    assert "append-only" in str(exc_info.value).lower()

    # Attempt DELETE -> trigger must raise exception
    with pytest.raises(psycopg2.Error) as exc_info:
        cur.execute("DELETE FROM payment_events WHERE id = %s;", (event_id,))
    assert "append-only" in str(exc_info.value).lower()


def test_06_idx_unique_queue_token_collision_prevention(real_pg_conn, clean_db):
    """Invariant 6: idx_unique_queue_token prevents duplicate tokens for same doctor on same date."""
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)

    cur.execute("""
    INSERT INTO appointments (
        clinic_id, patient_phone, department, doctor_name,
        appointment_date, appointment_time, status, token_number
    ) VALUES (
        %s, '+919876543210', 'Cardiology', 'Dr. Sharma',
        '2026-09-05', '09:00:00', 'confirmed', 5
    );
    """, (clinic_id,))

    # Second patient with token 5 for same doctor and date must raise UniqueViolation
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("""
        INSERT INTO appointments (
            clinic_id, patient_phone, department, doctor_name,
            appointment_date, appointment_time, status, token_number
        ) VALUES (
            %s, '+919876543211', 'Cardiology', 'Dr. Sharma',
            '2026-09-05', '09:30:00', 'confirmed', 5
        );
        """, (clinic_id,))


def test_07_queue_token_null_is_unconstrained(real_pg_conn, clean_db):
    """Invariant 7: Multiple un-checked-in appointments (token_number=NULL) can coexist."""
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)

    for i in range(3):
        cur.execute("""
        INSERT INTO appointments (
            clinic_id, patient_phone, department, doctor_name,
            appointment_date, appointment_time, status, token_number
        ) VALUES (
            %s, '+919876543210', 'Cardiology', 'Dr. Sharma',
            '2026-09-05', %s, 'confirmed', NULL
        );
        """, (clinic_id, f"1{4+i}:00:00"))


def test_08_multi_tenant_cascade_delete(real_pg_conn, clean_db):
    """Invariant 8: Deleting a clinic cascades cleanly to appointments, doctors, and branches."""
    cur = real_pg_conn.cursor()

    # Create distinct test clinic
    cur.execute("""
    INSERT INTO clinics (name, whatsapp_number, plan, is_active)
    VALUES ('Temp Cascade Clinic', '+918888888888', 'essential', true)
    RETURNING id;
    """)
    temp_clinic_id = cur.fetchone()[0]

    # Insert branch, doctor, appointment
    cur.execute("""
    INSERT INTO branches (clinic_id, name) VALUES (%s, 'Temp Branch') RETURNING id;
    """, (temp_clinic_id,))
    branch_id = cur.fetchone()[0]

    cur.execute("""
    INSERT INTO doctors (clinic_id, name, department, specialization)
    VALUES (%s, 'Temp Doctor', 'General', 'General Physician') RETURNING id;
    """, (temp_clinic_id,))
    doctor_id = cur.fetchone()[0]

    cur.execute("""
    INSERT INTO appointments (
        clinic_id, branch_id, patient_phone, department, doctor_name,
        appointment_date, appointment_time, status
    ) VALUES (%s, %s, '+918888888888', 'General', 'Temp Doctor', '2026-09-10', '10:00:00', 'confirmed');
    """, (temp_clinic_id, branch_id))

    # Delete clinic
    cur.execute("DELETE FROM clinics WHERE id = %s;", (temp_clinic_id,))

    # Verify cascading deletion
    cur.execute("SELECT COUNT(*) FROM branches WHERE id = %s;", (branch_id,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT COUNT(*) FROM doctors WHERE id = %s;", (doctor_id,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT COUNT(*) FROM appointments WHERE clinic_id = %s;", (temp_clinic_id,))
    assert cur.fetchone()[0] == 0


def test_09_transaction_rollback_preserves_consistency(real_pg_conn, clean_db):
    """Invariant 9: Transaction rollback on failure leaves zero orphaned state."""
    clinic_id = get_default_clinic_id(real_pg_conn.cursor())

    try:
        # Create non-autocommit transaction
        conn2 = psycopg2.connect(real_pg_conn.dsn)
        conn2.autocommit = False
        cur2 = conn2.cursor()

        cur2.execute("""
        INSERT INTO appointments (
            clinic_id, patient_phone, department, doctor_name,
            appointment_date, appointment_time, status
        ) VALUES (%s, '+919999999901', 'Cardiology', 'Dr. Rollback', '2026-09-15', '10:00:00', 'confirmed')
        RETURNING id;
        """, (clinic_id,))

        # Force a database failure
        cur2.execute("INSERT INTO appointments (id, status) VALUES ('invalid-uuid-format', 'confirmed');")
        conn2.commit()
    except Exception:
        conn2.rollback()
    finally:
        conn2.close()

    # Assert row was rolled back
    cur = real_pg_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM appointments WHERE doctor_name = 'Dr. Rollback';")
    assert cur.fetchone()[0] == 0


def test_10_concurrent_booking_race_condition(real_postgres_uri, clean_db):
    """Invariant 10: Under 10 concurrent racing workers for 1 slot, exactly 1 succeeds."""
    conn = psycopg2.connect(real_postgres_uri)
    conn.autocommit = True
    clinic_id = get_default_clinic_id(conn.cursor())
    conn.close()

    target_slot = "10:30:00"
    target_date = "2026-09-20"
    target_doctor = "Dr. Concurrent"

    def attempt_booking(worker_idx):
        try:
            w_conn = psycopg2.connect(real_postgres_uri)
            w_conn.autocommit = True
            w_cur = w_conn.cursor()
            w_cur.execute("""
            INSERT INTO appointments (
                clinic_id, patient_phone, department, doctor_name,
                appointment_date, appointment_time, status
            ) VALUES (
                %s, %s, 'Cardiology', %s, %s, %s, 'pending_payment'
            ) RETURNING id;
            """, (
                clinic_id,
                f"+9198765432{worker_idx:02d}",
                target_doctor,
                target_date,
                target_slot
            ))
            res = w_cur.fetchone()
            w_conn.close()
            return ("SUCCESS", res[0], worker_idx)
        except psycopg2.errors.UniqueViolation:
            return ("SLOT_TAKEN", None, worker_idx)
        except Exception as e:
            return ("ERROR", str(e), worker_idx)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt_booking, i) for i in range(10)]
        results = [f.result() for f in futures]

    successes = [r for r in results if r[0] == "SUCCESS"]
    slot_takens = [r for r in results if r[0] == "SLOT_TAKEN"]

    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}: {results}"
    assert len(slot_takens) == 9, f"Expected 9 slot_taken rejections, got {len(slot_takens)}"

    # Double check database row count
    check_conn = psycopg2.connect(real_postgres_uri)
    check_cur = check_conn.cursor()
    check_cur.execute("""
    SELECT COUNT(*) FROM appointments
    WHERE clinic_id = %s AND doctor_name = %s AND appointment_date = %s AND appointment_time = %s;
    """, (clinic_id, target_doctor, target_date, target_slot))
    assert check_cur.fetchone()[0] == 1
    check_conn.close()


def test_11_compare_and_set_payment_confirmation(real_postgres_uri, clean_db):
    """Invariant 11: Compare-and-set atomic payment confirmation guarantees single winner."""
    conn = psycopg2.connect(real_postgres_uri)
    conn.autocommit = True
    cur = conn.cursor()
    clinic_id = get_default_clinic_id(cur)

    # Insert pending booking
    cur.execute("""
    INSERT INTO appointments (
        clinic_id, patient_phone, department, doctor_name,
        appointment_date, appointment_time, status
    ) VALUES (
        %s, '+919876543210', 'Cardiology', 'Dr. CAS',
        '2026-09-22', '14:00:00', 'pending_payment'
    ) RETURNING id;
    """, (clinic_id,))
    booking_id = cur.fetchone()[0]
    conn.close()

    def attempt_confirm(worker_idx):
        w_conn = psycopg2.connect(real_postgres_uri)
        w_conn.autocommit = True
        w_cur = w_conn.cursor()
        w_cur.execute("""
        UPDATE appointments
        SET status = 'confirmed', updated_at = NOW()
        WHERE id = %s AND status = 'pending_payment'
        RETURNING id;
        """, (booking_id,))
        row = w_cur.fetchone()
        w_conn.close()
        return bool(row)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(attempt_confirm, i) for i in range(5)]
        results = [f.result() for f in futures]

    assert results.count(True) == 1, f"Expected exactly 1 CAS winner, got {results.count(True)}"
    assert results.count(False) == 4


def test_12_processed_messages_idempotency(real_pg_conn, clean_db):
    """Invariant 12: processed_messages enforces UNIQUE(message_id)."""
    cur = real_pg_conn.cursor()
    wamid = "wamid.HBgLMTIzNDU2Nzg5MA=="

    cur.execute("INSERT INTO processed_messages (message_id) VALUES (%s);", (wamid,))

    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("INSERT INTO processed_messages (message_id) VALUES (%s);", (wamid,))


def test_13_rate_limits_key_uniqueness(real_pg_conn, clean_db):
    """Invariant 13: rate_limits enforces unique index on key."""
    cur = real_pg_conn.cursor()
    key = "192.168.1.100"

    cur.execute("INSERT INTO rate_limits (key, attempts) VALUES (%s, 1);", (key,))

    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("INSERT INTO rate_limits (key, attempts) VALUES (%s, 2);", (key,))


def test_14_doctor_branches_many_to_many_uniqueness(real_pg_conn, clean_db):
    """Invariant 14: doctor_branches enforces UNIQUE(doctor_id, branch_id)."""
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)

    cur.execute("INSERT INTO branches (clinic_id, name) VALUES (%s, 'Branch A') RETURNING id;", (clinic_id,))
    branch_id = cur.fetchone()[0]

    cur.execute("""
    INSERT INTO doctors (clinic_id, name, department, specialization)
    VALUES (%s, 'Dr. M2M', 'General', 'Physician') RETURNING id;
    """, (clinic_id,))
    doctor_id = cur.fetchone()[0]

    cur.execute("INSERT INTO doctor_branches (doctor_id, branch_id) VALUES (%s, %s);", (doctor_id, branch_id))

    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("INSERT INTO doctor_branches (doctor_id, branch_id) VALUES (%s, %s);", (doctor_id, branch_id))


def test_15_inbound_messages_durable_unique_constraint(real_pg_conn, clean_db):
    """Invariant 15: inbound_messages enforces UNIQUE(message_id) and valid status constraint."""
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)
    wamid = "wamid.DURABLE_PG_TEST_001"

    cur.execute("""
    INSERT INTO inbound_messages (message_id, clinic_id, phone, status)
    VALUES (%s, %s, '+919999999999', 'received');
    """, (wamid, clinic_id))

    # Duplicate message_id must be rejected
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("""
        INSERT INTO inbound_messages (message_id, clinic_id, phone, status)
        VALUES (%s, %s, '+919999999999', 'received');
        """, (wamid, clinic_id))

    # Invalid status must be rejected by check constraint
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""
        INSERT INTO inbound_messages (message_id, clinic_id, phone, status)
        VALUES ('wamid.INVALID_STATUS', %s, '+919999999999', 'invalid_status_enum');
        """, (clinic_id,))


def test_16_scheduler_locks_mutual_exclusion_and_takeover(real_pg_conn, clean_db):
    """Invariant 16: scheduler_locks table enforces mutual exclusion on job_name."""
    cur = real_pg_conn.cursor()

    cur.execute("""
    INSERT INTO scheduler_locks (job_name, locked_by, locked_at, expires_at)
    VALUES ('24h_reminders', 'replica-1', NOW(), NOW() + interval '2 minutes');
    """)

    # Concurrent insert by replica-2 must fail on PRIMARY KEY (job_name)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("""
        INSERT INTO scheduler_locks (job_name, locked_by, locked_at, expires_at)
        VALUES ('24h_reminders', 'replica-2', NOW(), NOW() + interval '2 minutes');
        """)
