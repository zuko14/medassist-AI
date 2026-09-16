# Task 1 — Migration 077 (schema), rollback, real-Postgres tests

**Files:**
- Create: `migrations/077_specialty_plans_and_treatments.sql`
- Create: `migrations/rollback/077_down.sql`
- Modify: `migrations/verify_supabase_schema.sql` (table list near line 60–72; critical columns list near line 369)
- Test: `tests/test_specialty_migration_077.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - clinics/plan_tiers accept `derma`, `eye`, `dental`, `ivf`;
  - table `specialty_treatments`;
  - table `treatment_doctors`;
  - `appointments.treatment_id UUID NULL → specialty_treatments(id) ON DELETE SET NULL`;
  - `appointments.treatment_name TEXT NULL`.

---

- [x] **Step 0: Branch and baseline**

```bash
git checkout main
git pull
git checkout -b feat/specialty-plans
```
Run the baseline (must pass before you change anything; record the result in the session doc):
```bash
pytest tests/test_plan_features.py tests/test_lab_tests_unique_name_migration.py tests/test_lint_unscoped_queries.py -q
```
Then run the orphan check from `00-global-constraints.md` §E.

- [x] **Step 1: Write the failing test** — create `tests/test_specialty_migration_077.py`:

```python
"""Migration 077 against a real PostgreSQL (pgserver), never Supabase.

The properties that matter most can only be proven by the database itself:
  * a treatment-tagged consultation is STILL blocked by uq_appointment_active_slot
    (the whole reason booking_type is not widened);
  * deleting a treatment keeps the booking's treatment_name;
  * existing plan values keep working after the CHECK is widened.
"""

import uuid

import psycopg2
import pytest


@pytest.fixture
def clinic(real_pg_conn):
    cur = real_pg_conn.cursor()
    tag = f"m077-{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) "
        "VALUES (%s, %s, 'derma', true) RETURNING id;",
        (f"{tag} Skin Clinic", "+9190001" + uuid.uuid4().hex[:5]),
    )
    clinic_id = str(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO doctors (clinic_id, name, department, specialization) "
        "VALUES (%s, %s, 'Dermatology', 'Dermatologist') RETURNING id;",
        (clinic_id, f"Dr. {tag}"),
    )
    doctor_id = str(cur.fetchone()[0])
    yield {"conn": real_pg_conn, "clinic_id": clinic_id, "doctor_id": doctor_id, "tag": tag}
    cur.execute("DELETE FROM appointments WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM treatment_doctors WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM specialty_treatments WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM doctors WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM clinics WHERE id = %s;", (clinic_id,))
    cur.close()


def _treatment(c, name="Hair PRP Therapy", **extra):
    cur = c["conn"].cursor()
    cols = {"clinic_id": c["clinic_id"], "name": name, "category": "Hair & Scalp", **extra}
    keys = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    cur.execute(
        f"INSERT INTO specialty_treatments ({keys}) VALUES ({marks}) RETURNING id;",
        list(cols.values()),
    )
    return str(cur.fetchone()[0])


def _appointment(c, treatment_id=None, treatment_name=None, time="10:00:00", status="confirmed"):
    cur = c["conn"].cursor()
    cur.execute(
        "INSERT INTO appointments (clinic_id, patient_phone, department, doctor_name, doctor_id, "
        "appointment_date, appointment_time, status, treatment_id, treatment_name) "
        "VALUES (%s, '+919000000077', 'Dermatology', 'Dr. X', %s, CURRENT_DATE + 3, %s, %s, %s, %s) "
        "RETURNING id;",
        (c["clinic_id"], c["doctor_id"], time, status, treatment_id, treatment_name),
    )
    return str(cur.fetchone()[0])


@pytest.mark.parametrize("plan", ["derma", "eye", "dental", "ivf", "soloclinic", "diagstream",
                                  "diagbooking", "essential", "polyclinic", "enterprise"])
def test_every_plan_value_is_accepted(real_pg_conn, plan):
    cur = real_pg_conn.cursor()
    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, %s, true) RETURNING id;",
        (f"m077 plan {plan}", "+9190002" + uuid.uuid4().hex[:5], plan),
    )
    cid = cur.fetchone()[0]
    cur.execute("DELETE FROM clinics WHERE id = %s;", (cid,))


def test_unknown_plan_is_still_rejected(real_pg_conn):
    cur = real_pg_conn.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('bad', %s, 'dentistry', true);",
            ("+9190003" + uuid.uuid4().hex[:5],),
        )


def test_specialty_plan_tiers_are_seeded(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute(
        "SELECT plan_name, included_messages_month FROM plan_tiers "
        "WHERE plan_name IN ('derma','eye','dental','ivf') ORDER BY plan_name;"
    )
    rows = cur.fetchall()
    assert [r[0] for r in rows] == ["dental", "derma", "eye", "ivf"]
    assert all(r[1] == 2500 for r in rows)


def test_treatment_name_is_unique_per_clinic_ignoring_case_and_padding(clinic):
    _treatment(clinic, "Chemical Peel")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _treatment(clinic, "  chemical peel ")


def test_short_name_longer_than_24_is_rejected(clinic):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _treatment(clinic, "Laser Hair Reduction", short_name="L" * 25)


def test_negative_price_is_rejected(clinic):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _treatment(clinic, "Tooth Filling", price_from_paise=-1)


def test_treatment_doctor_link_is_unique_and_cascades(clinic):
    tid = _treatment(clinic)
    cur = clinic["conn"].cursor()
    cur.execute(
        "INSERT INTO treatment_doctors (clinic_id, treatment_id, doctor_id) VALUES (%s, %s, %s);",
        (clinic["clinic_id"], tid, clinic["doctor_id"]),
    )
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            "INSERT INTO treatment_doctors (clinic_id, treatment_id, doctor_id) VALUES (%s, %s, %s);",
            (clinic["clinic_id"], tid, clinic["doctor_id"]),
        )
    cur.execute("DELETE FROM specialty_treatments WHERE id = %s;", (tid,))
    cur.execute("SELECT COUNT(*) FROM treatment_doctors WHERE treatment_id = %s;", (tid,))
    assert cur.fetchone()[0] == 0


def test_deleting_a_treatment_keeps_the_booking_and_its_name(clinic):
    tid = _treatment(clinic)
    appt = _appointment(clinic, tid, "Hair PRP Therapy")
    cur = clinic["conn"].cursor()
    cur.execute("DELETE FROM specialty_treatments WHERE id = %s;", (tid,))
    cur.execute("SELECT treatment_id, treatment_name, booking_type FROM appointments WHERE id = %s;", (appt,))
    treatment_id, treatment_name, booking_type = cur.fetchone()
    assert treatment_id is None
    assert treatment_name == "Hair PRP Therapy"
    assert booking_type == "consultation"


def test_treatment_tag_does_not_bypass_the_double_booking_index(clinic):
    """THE invariant. Two active bookings for one doctor at one minute must
    collide whether or not they carry a treatment."""
    t1 = _treatment(clinic, "Hair PRP Therapy")
    t2 = _treatment(clinic, "Acne Treatment")
    _appointment(clinic, t1, "Hair PRP Therapy")
    with pytest.raises(psycopg2.errors.UniqueViolation) as exc:
        _appointment(clinic, t2, "Acne Treatment")
    assert "uq_appointment_active_slot" in str(exc.value)


def test_untagged_and_tagged_bookings_also_collide(clinic):
    t1 = _treatment(clinic, "Chemical Peel")
    _appointment(clinic)  # plain consultation, no treatment
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _appointment(clinic, t1, "Chemical Peel")


def test_booking_type_constraint_is_untouched(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'appointments_booking_type_check';"
    )
    definition = cur.fetchone()[0]
    assert "treatment" not in definition
    assert "consultation" in definition and "lab_test" in definition
```

- [x] **Step 2: Run it and confirm it fails**

```bash
pytest tests/test_specialty_migration_077.py -q
```
Expected: errors such as `relation "specialty_treatments" does not exist`, or a `CheckViolation` for plan `derma`.

- [x] **Step 3: Create `migrations/077_specialty_plans_and_treatments.sql`** (exact content):

```sql
-- ============================================================================
-- Migration 077: Specialty hospital plans + treatments catalogue
-- ============================================================================
-- Adds four plans (derma, eye, dental, ivf), a per-clinic treatments
-- catalogue, the doctors who perform each treatment, and two nullable tag
-- columns on appointments.
--
-- WHY appointments.booking_type IS NOT WIDENED
-- A treatment booking is a consultation slot with a doctor. The slot
-- uniqueness indexes (migration 064), the time-required CHECK (039), the
-- doctor_id guards and the reminder jobs all key on
-- booking_type = 'consultation'. A new booking_type would silently opt those
-- rows out of double-booking protection. The treatment is a TAG on a
-- consultation, never a new booking type.
--
-- PURELY ADDITIVE: no existing row is updated; existing constraints other than
-- the two plan CHECKs are untouched. Re-runnable.
-- ============================================================================

-- Fail fast instead of queueing behind a long transaction and stalling live
-- bookings while waiting for the appointments lock.
SET LOCAL lock_timeout = '5s';

-- ── 1. Widen the plan CHECK constraints (same approach as migration 072) ────
DO $$
DECLARE
    con RECORD;
    bad_count INT;
BEGIN
    SELECT COUNT(*) INTO bad_count FROM clinics
    WHERE plan NOT IN ('soloclinic', 'diagstream', 'diagbooking',
                       'essential', 'polyclinic', 'enterprise',
                       'derma', 'eye', 'dental', 'ivf');
    IF bad_count > 0 THEN
        RAISE EXCEPTION 'Found % clinics with an unexpected plan value — resolve before migrating', bad_count;
    END IF;

    FOR con IN
        SELECT pg_constraint.conname
        FROM pg_constraint
        JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
        WHERE pg_class.relname = 'clinics'
          AND pg_constraint.contype = 'c'
          AND pg_get_constraintdef(pg_constraint.oid) LIKE '%plan%'
    LOOP
        EXECUTE format('ALTER TABLE clinics DROP CONSTRAINT %I', con.conname);
    END LOOP;

    ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check
        CHECK (plan IN ('soloclinic', 'diagstream', 'diagbooking',
                        'essential', 'polyclinic', 'enterprise',
                        'derma', 'eye', 'dental', 'ivf'));

    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'plan_tiers') THEN
        FOR con IN
            SELECT pg_constraint.conname
            FROM pg_constraint
            JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
            WHERE pg_class.relname = 'plan_tiers'
              AND pg_constraint.contype = 'c'
              AND pg_get_constraintdef(pg_constraint.oid) LIKE '%plan_name%'
        LOOP
            EXECUTE format('ALTER TABLE plan_tiers DROP CONSTRAINT %I', con.conname);
        END LOOP;

        ALTER TABLE plan_tiers ADD CONSTRAINT plan_tiers_plan_name_check
            CHECK (plan_name IN ('soloclinic', 'diagstream', 'diagbooking',
                                 'essential', 'polyclinic', 'enterprise',
                                 'derma', 'eye', 'dental', 'ivf'));
    END IF;
END $$;

-- Quotas mirror 'essential' (2,500 messages). Price 0 until the owner sets it
-- from the platform dashboard (PUT /platform/plan-tiers/{plan_name}).
INSERT INTO plan_tiers (plan_name, display_name, monthly_price_paise, included_messages_month, overage_price_paise)
VALUES
    ('derma',  'Dermatology & Hair', 0, 2500, 0),
    ('eye',    'Eye Hospital',       0, 2500, 0),
    ('dental', 'Dental Clinic',      0, 2500, 0),
    ('ivf',    'IVF & Fertility',    0, 2500, 0)
ON CONFLICT (plan_name) DO NOTHING;

-- ── 2. Treatments catalogue ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS specialty_treatments (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    category          TEXT NOT NULL CHECK (char_length(btrim(category)) BETWEEN 1 AND 60),
    name              TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
    short_name        TEXT CHECK (short_name IS NULL OR char_length(short_name) <= 24),
    description       TEXT CHECK (description IS NULL OR char_length(description) <= 400),
    description_hi    TEXT CHECK (description_hi IS NULL OR char_length(description_hi) <= 600),
    description_te    TEXT CHECK (description_te IS NULL OR char_length(description_te) <= 600),
    concerns          TEXT CHECK (concerns IS NULL OR char_length(concerns) <= 500),
    duration_minutes  INTEGER CHECK (duration_minutes IS NULL OR duration_minutes BETWEEN 5 AND 1440),
    price_from_paise  INTEGER NOT NULL DEFAULT 0 CHECK (price_from_paise >= 0),
    prep_instructions TEXT CHECK (prep_instructions IS NULL OR char_length(prep_instructions) <= 600),
    is_active         BOOLEAN NOT NULL DEFAULT true,
    display_order     INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT 'custom' CHECK (source IN ('custom', 'starter')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One treatment name per clinic, compared the way the app compares
-- (strip + lowercase), matching the lab_tests rule from migration 076.
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_treatment_name_per_clinic
    ON specialty_treatments (clinic_id, lower(btrim(name)));

CREATE INDEX IF NOT EXISTS idx_specialty_treatments_clinic_active
    ON specialty_treatments (clinic_id, is_active, category);

ALTER TABLE specialty_treatments ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_specialty_treatments" ON specialty_treatments;
CREATE POLICY "service_role_all_specialty_treatments" ON specialty_treatments
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ── 3. Doctors who perform a treatment ───────────────────────────────────────
-- Carries clinic_id (unlike doctor_branches) so every query can be tenant
-- scoped directly and the table can sit in TENANT_OWNED_TABLES. That the
-- doctor belongs to the same clinic is verified by the admin API before insert.
CREATE TABLE IF NOT EXISTS treatment_doctors (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    treatment_id UUID NOT NULL REFERENCES specialty_treatments(id) ON DELETE CASCADE,
    doctor_id    UUID NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (treatment_id, doctor_id)
);

CREATE INDEX IF NOT EXISTS idx_treatment_doctors_clinic_treatment
    ON treatment_doctors (clinic_id, treatment_id);
CREATE INDEX IF NOT EXISTS idx_treatment_doctors_doctor
    ON treatment_doctors (doctor_id);

ALTER TABLE treatment_doctors ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_treatment_doctors" ON treatment_doctors;
CREATE POLICY "service_role_all_treatment_doctors" ON treatment_doctors
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ── 4. Tag columns on appointments ───────────────────────────────────────────
-- Nullable, no default: a metadata-only change, no table rewrite. ON DELETE
-- SET NULL keeps booking history when an admin deletes a treatment;
-- treatment_name is stored at booking time exactly like lab_test_name.
ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS treatment_id UUID REFERENCES specialty_treatments(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS treatment_name TEXT;

-- ── Verify ───────────────────────────────────────────────────────────────────
SELECT 'migration_077_complete' AS status;
```

- [x] **Step 4: Run the test and confirm it passes**

```bash
pytest tests/test_specialty_migration_077.py -q
```
Expected: all tests PASS. If `test_every_plan_value_is_accepted` fails on a NOT NULL column of `clinics`, add that column to the test INSERT with a harmless value. Do not change the migration.

- [x] **Step 5: Create `migrations/rollback/077_down.sql`** (exact content):

```sql
-- Rollback 077: remove specialty plans and the treatments catalogue.
--
-- ORDER: roll the APPLICATION back first. Code from this release selects
-- appointments.treatment_name; running it against a schema without the
-- column breaks the Payments and Insights pages.
--
-- DATA LOSS WARNING: dropping appointments.treatment_id / treatment_name
-- permanently removes which treatment each booking was for. Export first:
--   COPY (SELECT id, clinic_id, treatment_id, treatment_name FROM appointments
--         WHERE treatment_id IS NOT NULL OR treatment_name IS NOT NULL)
--   TO STDOUT WITH CSV HEADER;
-- The bookings themselves (slots, payments, refunds) are unaffected.

DO $$
DECLARE n INT;
BEGIN
    SELECT COUNT(*) INTO n FROM clinics WHERE plan IN ('derma', 'eye', 'dental', 'ivf');
    IF n > 0 THEN
        RAISE EXCEPTION '% clinic(s) are on a specialty plan — move them to another plan before rolling back 077', n;
    END IF;
END $$;

ALTER TABLE appointments DROP COLUMN IF EXISTS treatment_name;
ALTER TABLE appointments DROP COLUMN IF EXISTS treatment_id;

DROP TABLE IF EXISTS treatment_doctors;
DROP TABLE IF EXISTS specialty_treatments;

DELETE FROM plan_tiers WHERE plan_name IN ('derma', 'eye', 'dental', 'ivf');

ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_plan_check;
ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check
    CHECK (plan IN ('soloclinic', 'diagstream', 'diagbooking',
                    'essential', 'polyclinic', 'enterprise'));

ALTER TABLE plan_tiers DROP CONSTRAINT IF EXISTS plan_tiers_plan_name_check;
ALTER TABLE plan_tiers ADD CONSTRAINT plan_tiers_plan_name_check
    CHECK (plan_name IN ('soloclinic', 'diagstream', 'diagbooking',
                         'essential', 'polyclinic', 'enterprise'));
```

- [x] **Step 6: Update `migrations/verify_supabase_schema.sql`**

a) In the `v_tables` array, replace:
```sql
        'admin_notifications', 'lab_tests', 'meta_pricing_config',
        'inbound_messages', 'scheduler_locks'
```
with:
```sql
        'admin_notifications', 'lab_tests', 'meta_pricing_config',
        'inbound_messages', 'scheduler_locks',
        'specialty_treatments', 'treatment_doctors'
```
b) In the CRITICAL COLUMNS `VALUES` list, directly after the line `            ('appointments', 'completed_at',          '063'),` add:
```sql
            ('appointments', 'treatment_id',          '077'),
            ('appointments', 'treatment_name',        '077'),
```

- [x] **Step 7: Re-run the tests plus the existing real-Postgres tests**

```bash
pytest tests/test_specialty_migration_077.py tests/test_lab_tests_unique_name_migration.py tests/test_real_postgres_invariants.py -q
```
Expected: all PASS. Then run the orphan check.

- [x] **Step 8: Commit**

```bash
git add migrations/077_specialty_plans_and_treatments.sql migrations/rollback/077_down.sql migrations/verify_supabase_schema.sql tests/test_specialty_migration_077.py
git commit -m "feat(db): migration 077 — specialty plans, treatments catalogue, appointment treatment tag

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
