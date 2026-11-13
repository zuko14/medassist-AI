"""Migration 126 (Diagnostic OS catalogue, DX-M2) against real PostgreSQL.

Department / container / signatory keys and checks (2-digit container codes),
composite FKs (no cross-tenant link), the component guard (kinds, depth,
self), parameters only on tests, approval digest + CAS approval, every
clinical edit clearing approval (machine_code excepted), existing lab tests and
LIS ranges unaffected, lab-test delete still works, purge v2, and a guarded
rollback + idempotent re-apply. HTTP behaviour: tests/test_dx_catalogue_api.py.
"""

import json
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "126_dx_catalogue.sql", MIG / "rollback" / "126_rollback_dx_catalogue.sql"


def unwind_126(cur) -> None:
    """Inside an open transaction: purge every clinic with DX catalogue rows, clear
    approvals, run the 126 rollback. Called first by unwind_125 (newest first)."""
    cur.execute("SELECT to_regclass('public.dx_test_components') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT dx_purge_clinic(x.clinic_id) FROM (
                     SELECT clinic_id FROM dx_test_components UNION SELECT clinic_id FROM dx_signatories
                     UNION SELECT clinic_id FROM dx_containers UNION SELECT clinic_id FROM dx_departments) x""")
    cur.execute("UPDATE lab_tests SET approved_at = NULL, approved_by = NULL, approval_digest = NULL "
                "WHERE approved_at IS NOT NULL")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _q(conn, sql, *args):
    cur = conn.cursor()
    cur.execute(sql, args)
    out = cur.fetchone()[0] if cur.description else None
    cur.close()
    return out


def _err(conn, sql, *args) -> str:
    with pytest.raises(psycopg2.Error) as e:
        _q(conn, sql, *args)
    return str(e.value)


@pytest.fixture
def lab(real_pg_conn):
    c = real_pg_conn
    clinic = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('Cat Lab', %s, "
                       "'diagstream', true) RETURNING id", "+9174" + uuid.uuid4().hex[:8]))
    dept = str(_q(c, "INSERT INTO dx_departments (clinic_id, code, name, report_heading) VALUES "
                     "(%s, 'BIO', 'Biochemistry', 'CLINICAL BIOCHEMISTRY') RETURNING id", clinic))
    cont = str(_q(c, "INSERT INTO dx_containers (clinic_id, code, name, colour_hex, sample_type) VALUES "
                     "(%s, '01', 'Plain / Red top', '#C0392B', 'serum') RETURNING id", clinic))
    doctor = str(_q(c, """INSERT INTO doctors (clinic_id, name, department, specialization, registration_number,
                          is_active) VALUES (%s, 'Dr. Rao', 'Lab', 'Pathology', 'TSMC/1/1', true) RETURNING id""",
                    clinic))

    def test(name, kind="test", **kw):
        cols = {"clinic_id": clinic, "name": name, "price_paise": 30000, "kind": kind, **kw}
        keys = ", ".join(cols)
        return str(_q(c, f"INSERT INTO lab_tests ({keys}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                      *cols.values()))

    lipid = test("Lipid Profile", department_id=dept, container_id=cont, test_code="LIPID")
    for name, code, hi in (("Total Cholesterol", "TC", 200), ("HDL", "HDL", 60)):
        _q(c, """INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, unit, normal_high, parameter_code)
                 VALUES (%s, %s, %s, 'mg/dL', %s, %s)""", clinic, lipid, name, hi, code)
    return {"c": c, "clinic": clinic, "dept": dept, "cont": cont, "doctor": doctor, "lipid": lipid, "test": test}


def _approve(s, test_id=None):
    t = test_id or s["lipid"]
    digest = _q(s["c"], "SELECT dx_test_digest(%s, %s)", s["clinic"], t)
    return _q(s["c"], "SELECT dx_approve_test(%s, %s, %s, %s, 'Dr. Rao')", s["clinic"], t, digest, s["doctor"])


def _approved(s, test_id=None):
    return _q(s["c"], "SELECT approved_at IS NOT NULL FROM lab_tests WHERE id = %s", test_id or s["lipid"])


# ── tables and keys ──────────────────────────────────────────────────────────

def test_container_codes_are_two_digits_and_unique(lab):
    c, clinic = lab["c"], lab["clinic"]
    for bad in ("1", "001", "A1"):
        assert "dx_containers_code_check" in _err(c, "INSERT INTO dx_containers (clinic_id, code, name, colour_hex, "
                                                     "sample_type) VALUES (%s, %s, 'X tube', '#FFFFFF', 'serum')",
                                                  clinic, bad)
    assert "dx_containers_clinic_code_key" in _err(c, "INSERT INTO dx_containers (clinic_id, code, name, colour_hex, "
                                                      "sample_type) VALUES (%s, '01', 'Dup', '#FFFFFF', 'serum')", clinic)
    assert "dx_containers_colour_hex_check" in _err(c, "INSERT INTO dx_containers (clinic_id, code, name, colour_hex, "
                                                       "sample_type) VALUES (%s, '07', 'EDTA', 'purple', 'whole_blood')",
                                                    clinic)
    assert "dx_departments_code_check" in _err(c, "INSERT INTO dx_departments (clinic_id, code, name, report_heading) "
                                                  "VALUES (%s, 'bio1', 'Bad', 'BAD')", clinic)


def test_signatory_keys_and_cross_tenant_links_refused(lab):
    c, clinic = lab["c"], lab["clinic"]
    _q(c, "INSERT INTO dx_signatories (clinic_id, doctor_id, designation, qualification_text) "
          "VALUES (%s, %s, 'Consultant Pathologist', 'MD Pathology')", clinic, lab["doctor"])
    assert "uq_dx_signatories_doctor_all" in _err(c, "INSERT INTO dx_signatories (clinic_id, doctor_id, designation, "
                                                     "qualification_text) VALUES (%s, %s, 'Again', 'MD')",
                                                  clinic, lab["doctor"])
    _q(c, "INSERT INTO dx_signatories (clinic_id, doctor_id, department_id, designation, qualification_text) "
          "VALUES (%s, %s, %s, 'HOD Biochemistry', 'MD Biochemistry')", clinic, lab["doctor"], lab["dept"])
    other = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('Other', %s, "
                      "'diagstream', true) RETURNING id", "+9173" + uuid.uuid4().hex[:8]))
    assert "dx_signatories_doctor_fk" in _err(c, "INSERT INTO dx_signatories (clinic_id, doctor_id, designation, "
                                                 "qualification_text) VALUES (%s, %s, 'Leak', 'MD')", other, lab["doctor"])
    leak = str(_q(c, "INSERT INTO lab_tests (clinic_id, name, price_paise) VALUES (%s, 'Other test', 100) "
                     "RETURNING id", other))
    assert "lab_tests_container_fk" in _err(c, "UPDATE lab_tests SET container_id = %s WHERE id = %s",
                                            lab["cont"], leak)


def test_existing_lab_tests_keep_their_defaults(lab):
    c = lab["c"]
    t = str(_q(c, "INSERT INTO lab_tests (clinic_id, name, price_paise) VALUES (%s, 'Plain booking test', 50000) "
                  "RETURNING id", lab["clinic"]))
    row = _q(c, "SELECT to_jsonb(t) FROM lab_tests t WHERE id = %s", t)
    assert (row["kind"], row["gender_restriction"], row["dx_result_mode"], row["approved_at"]) == \
           ("test", "all", "parameters", None)
    p = _q(c, """INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, normal_low, normal_high)
                 VALUES (%s, %s, 'Hb', 13, 17) RETURNING to_jsonb(lab_test_parameters)""", lab["clinic"], t)
    assert (p["low_exclusive"], p["high_exclusive"], p["decimal_places"], p["rounding_mode"]) == \
           (False, False, 2, "half_up")
    # a department in use cannot be deleted; deactivate instead
    assert "lab_tests_department_fk" in _err(c, "DELETE FROM dx_departments WHERE id = %s", lab["dept"])
    assert "lab_tests_dx_check" in _err(c, "UPDATE lab_tests SET test_code = 'bad code' WHERE id = %s", t)


def test_test_code_unique_per_clinic_and_branch(lab):
    assert "uq_lab_tests_test_code" in _err(lab["c"], "INSERT INTO lab_tests (clinic_id, name, price_paise, test_code) "
                                                      "VALUES (%s, 'Lipid copy', 100, 'LIPID')", lab["clinic"])


# ── components ───────────────────────────────────────────────────────────────

def test_component_guard_kinds_depth_and_self(lab):
    c, clinic, t = lab["c"], lab["clinic"], lab["test"]
    hb, tsh = t("Hb"), t("TSH")
    panel, package = t("Thyroid Panel", "panel"), t("Executive Package", "package")
    ins = "INSERT INTO dx_test_components (clinic_id, parent_test_id, child_test_id) VALUES (%s, %s, %s)"
    _q(c, ins, clinic, panel, tsh)
    _q(c, ins, clinic, package, panel)
    _q(c, ins, clinic, package, hb)
    assert "dx_component_parent_kind" in _err(c, ins, clinic, hb, tsh)            # a test holds nothing
    assert "dx_component_depth" in _err(c, ins, clinic, panel, t("Other panel", "panel"))
    assert "dx_component_depth" in _err(c, ins, clinic, package, t("Other pkg", "package"))
    assert "dx_component_self" in _err(c, ins, clinic, panel, panel)
    assert "dx_test_components_pair_key" in _err(c, ins, clinic, panel, tsh)
    # kind is locked once grouped; a group cannot hold parameters
    assert "dx_kind_locked" in _err(c, "UPDATE lab_tests SET kind = 'test' WHERE id = %s", panel)
    assert "dx_parameters_on_group" in _err(c, "INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name) "
                                               "VALUES (%s, %s, 'X')", clinic, panel)
    assert "dx_parameters_on_group" in _err(c, "UPDATE lab_tests SET kind = 'panel' WHERE id = %s", lab["lipid"])


def test_deleting_a_lab_test_still_works_and_drops_its_links(lab):
    c, clinic, t = lab["c"], lab["clinic"], lab["test"]
    panel, tsh = t("Thyroid Panel", "panel"), t("TSH")
    _q(c, "INSERT INTO dx_test_components (clinic_id, parent_test_id, child_test_id) VALUES (%s, %s, %s)",
       clinic, panel, tsh)
    _approve(lab, panel)
    _q(c, "DELETE FROM lab_tests WHERE id = %s", tsh)                    # child delete: parent approval cleared
    assert _q(c, "SELECT count(*) FROM dx_test_components WHERE parent_test_id = %s", panel) == 0
    assert not _approved(lab, panel)
    _q(c, "DELETE FROM lab_tests WHERE id = %s", lab["lipid"])           # parent with parameters, approved
    assert _q(c, "SELECT count(*) FROM lab_test_parameters WHERE lab_test_id = %s", lab["lipid"]) == 0


# ── approval ─────────────────────────────────────────────────────────────────

def test_approval_is_a_digest_cas(lab):
    c = lab["c"]
    seen = _q(c, "SELECT dx_test_digest(%s, %s)", lab["clinic"], lab["lipid"])
    assert len(seen) == 64 and seen == _q(c, "SELECT dx_test_digest(%s, %s)", lab["clinic"], lab["lipid"])
    _q(c, "UPDATE lab_test_parameters SET normal_high = 190 WHERE lab_test_id = %s AND parameter_code = 'TC'",
       lab["lipid"])
    assert "dx_test_changed" in _err(c, "SELECT dx_approve_test(%s, %s, %s, %s, 'Dr. Rao')", lab["clinic"],
                                     lab["lipid"], seen, lab["doctor"])
    out = _approve(lab)
    assert out["approved_by"] == lab["doctor"] and out["approval_digest"] != seen
    assert _approved(lab)
    assert "dx_test_not_found" in _err(c, "SELECT dx_approve_test(%s, %s, 'x', NULL, 'x')", lab["clinic"],
                                       str(uuid.uuid4()))


@pytest.mark.parametrize("edit", [
    "UPDATE lab_test_parameters SET unit = 'mmol/L' WHERE lab_test_id = %(t)s AND parameter_code = 'TC'",
    "UPDATE lab_test_parameters SET formula = 'TC - HDL' WHERE lab_test_id = %(t)s AND parameter_code = 'HDL'",
    "UPDATE lab_test_parameters SET reference_text = 'Desirable < 200' WHERE lab_test_id = %(t)s",
    "INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name) SELECT clinic_id, id, 'LDL' FROM lab_tests WHERE id = %(t)s",
    "DELETE FROM lab_test_parameters WHERE lab_test_id = %(t)s AND parameter_code = 'HDL'",
    "UPDATE lab_tests SET interpretation = 'Fasting 12 h' WHERE id = %(t)s",
    "UPDATE lab_tests SET container_id = NULL WHERE id = %(t)s",
])
def test_every_clinical_edit_clears_approval(lab, edit):
    _approve(lab)
    cur = lab["c"].cursor()
    cur.execute(edit, {"t": lab["lipid"]})
    assert not _approved(lab)
    assert _q(lab["c"], "SELECT approval_digest FROM lab_tests WHERE id = %s", lab["lipid"]) is None


def test_price_and_machine_code_edits_keep_approval(lab):
    _approve(lab)
    _q(lab["c"], "UPDATE lab_tests SET price_paise = 45000, is_active = true WHERE id = %s", lab["lipid"])
    _q(lab["c"], "UPDATE lab_test_parameters SET machine_code = 'CHOL' WHERE lab_test_id = %s", lab["lipid"])
    assert _approved(lab)


def test_component_change_clears_the_groups_approval(lab):
    c, clinic, t = lab["c"], lab["clinic"], lab["test"]
    panel = t("Liver Panel", "panel")
    _q(c, "INSERT INTO dx_test_components (clinic_id, parent_test_id, child_test_id) VALUES (%s, %s, %s)",
       clinic, panel, t("SGPT"))
    _approve(lab, panel)
    _q(c, "INSERT INTO dx_test_components (clinic_id, parent_test_id, child_test_id) VALUES (%s, %s, %s)",
       clinic, panel, t("SGOT"))
    assert not _approved(lab, panel)


def test_concurrent_edit_and_approval_never_leave_a_stale_approval(lab, real_postgres_uri):
    """Edit and approve race 10 times: the test ends approved only if its digest is the current one."""
    for i in range(10):
        _q(lab["c"], "UPDATE lab_tests SET approved_at = NULL, approved_by = NULL, approval_digest = NULL "
                     "WHERE id = %s", lab["lipid"])
        seen = _q(lab["c"], "SELECT dx_test_digest(%s, %s)", lab["clinic"], lab["lipid"])

        def approve():
            conn = psycopg2.connect(real_postgres_uri)
            conn.autocommit = True
            try:
                _q(conn, "SELECT dx_approve_test(%s, %s, %s, %s, 'Dr')", lab["clinic"], lab["lipid"], seen,
                   lab["doctor"])
            except psycopg2.Error:
                pass
            finally:
                conn.close()

        def edit():
            conn = psycopg2.connect(real_postgres_uri)
            conn.autocommit = True
            try:
                _q(conn, "UPDATE lab_test_parameters SET normal_high = %s WHERE lab_test_id = %s "
                         "AND parameter_code = 'TC'", 150 + i, lab["lipid"])
            finally:
                conn.close()

        ts = [threading.Thread(target=approve), threading.Thread(target=edit)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        row = _q(lab["c"], "SELECT jsonb_build_object('d', approval_digest, 'now', dx_test_digest(clinic_id, id)) "
                           "FROM lab_tests WHERE id = %s", lab["lipid"])
        assert row["d"] is None or row["d"] == row["now"], i


# ── purge, rollback ──────────────────────────────────────────────────────────

def test_purge_v2_removes_catalogue_and_keeps_lab_tests(lab):
    c, clinic = lab["c"], lab["clinic"]
    _q(c, "INSERT INTO dx_signatories (clinic_id, doctor_id, designation, qualification_text) "
          "VALUES (%s, %s, 'Pathologist', 'MD')", clinic, lab["doctor"])
    panel = lab["test"]("Panel", "panel")
    _q(c, "INSERT INTO dx_test_components (clinic_id, parent_test_id, child_test_id) VALUES (%s, %s, %s)",
       clinic, panel, lab["lipid"])
    out = _q(c, "SELECT dx_purge_clinic(%s)", clinic)
    assert out["components"] == 1
    for t in ("dx_test_components", "dx_signatories", "dx_containers", "dx_departments"):
        assert _q(c, f"SELECT count(*) FROM {t} WHERE clinic_id = %s", clinic) == 0
    assert _q(c, "SELECT container_id FROM lab_tests WHERE id = %s", lab["lipid"]) is None


def test_rollback_refuses_catalogue_rows_then_reapplies(lab):
    c = lab["c"]
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "126_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_126(cur)
        cur.execute("SELECT to_regclass('public.dx_test_components'), to_regclass('public.dx_departments')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'lab_tests' "
                    "AND column_name IN ('kind', 'approved_at')")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT dx_purge_clinic(%s)", (lab["clinic"],))      # v1 body is back
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                         AND proname LIKE 'dx\\_%%'
                         AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x WHERE x LIKE 'search_path=%%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")


def test_rls_forced_on_catalogue_tables(lab):
    for t in ("dx_departments", "dx_containers", "dx_signatories", "dx_test_components"):
        assert _q(lab["c"], "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", t)


def test_json_payload_digest_ignores_inactive_bands(lab):
    before = _q(lab["c"], "SELECT dx_test_digest(%s, %s)", lab["clinic"], lab["lipid"])
    _q(lab["c"], """INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, is_active, options)
                    VALUES (%s, %s, 'Old band', false, NULL)""", lab["clinic"], lab["lipid"])
    assert _q(lab["c"], "SELECT dx_test_digest(%s, %s)", lab["clinic"], lab["lipid"]) == before
    assert json.dumps(before)
