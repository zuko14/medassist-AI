"""Migration 131 (Diagnostic OS reports, templates, delivery — DX-M7 — and P6b adoption) on real PostgreSQL.

Templates immutable once published; releases (snapshots, verify code, kinds, supersede,
content guard); one delivery per target with its own id; the decision table
(owner, payer routing, account rule, phone, referrer consent, dues + override);
the claim CAS before the sender; the outbox fed by receipts / invoice status; print
and share-link gates; OPD order adoption (internal visit, OPD bill untouched);
purge v7 and a guarded rollback.
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_115_orders import CBC as _cbc_item, _place, world  # noqa: F401  (fixture)
from tests.test_migration_dx_m3 import _err, _q, _register, lab  # noqa: F401  (fixture)
from tests.test_migration_dx_m6 import _approve, _enter, m6  # noqa: F401  (fixture)

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "131_dx_reports.sql", MIG / "rollback" / "131_rollback_dx_reports.sql"
LAYOUT = {"schema": 1, "body": {"font_pt": 9}}


def unwind_131(cur) -> None:
    """Inside an open transaction: purge every clinic with DX-M7 rows (or adopted orders),
    run the 131 rollback. Called first by unwind_130 (newest first)."""
    cur.execute("SELECT to_regclass('public.dx_report_releases') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT dx_purge_clinic(x.clinic_id) FROM (
                     SELECT clinic_id FROM dx_report_templates UNION SELECT clinic_id FROM dx_report_releases
                     UNION SELECT clinic_id FROM dx_delivery_overrides UNION SELECT clinic_id FROM dx_outbox
                     UNION SELECT clinic_id FROM opd_orders WHERE source <> 'dx' AND accession_id IS NOT NULL) x""")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _owner(s, mode):
    _q(s["c"], "UPDATE clinics SET dx_settings = jsonb_set(dx_settings, '{delivery_owner}', %s::jsonb) WHERE id = %s",
       json.dumps({s["branch"]: mode}), s["clinic"])


def _signed(s, test="CBC", value="14"):
    r = _enter(s, test=test, value=value)
    out = _approve(s, [r])
    assert out["signed"] == 1, out
    return r


def _release(s, ids=None, kind=None):
    return _q(s["c"], "SELECT dx_create_release(%s, %s, %s::uuid[], %s, %s, 'disp')", s["clinic"],
              s["visit"]["visit"]["id"], ids, kind, s["path"])


def _decide(s, release_id, target):
    cur = s["c"].cursor()
    cur.execute("SELECT allowed, reason FROM dx_delivery_decision(%s, %s, %s)", (s["clinic"], release_id, target))
    out = cur.fetchone()
    cur.close()
    return out


def _deliveries(s, release_id):
    cur = s["c"].cursor()
    cur.execute("SELECT target, status, reason, external_report_id, id::text FROM dx_release_deliveries "
                "WHERE release_id = %s ORDER BY target", (release_id,))
    out = {r[0]: {"status": r[1], "reason": r[2], "ext": r[3], "id": r[4]} for r in cur.fetchall()}
    cur.close()
    return out


def _rendered(s, rel):
    return _q(s["c"], "SELECT dx_set_release_render(%s, %s, true, %s, %s, 2)", s["clinic"], rel["id"],
              f"{s['clinic']}/releases/x.pdf", "a" * 64)


def _reeval(s):
    return _q(s["c"], "SELECT dx_reevaluate_deliveries(%s, %s)", s["clinic"], s["visit"]["visit"]["id"])


def _bill_paid(s):
    """The visit's bill issued and settled through a payment-link receipt (the unchanged OPD path)."""
    c, vid = s["c"], s["visit"]["visit"]["id"]
    inv = _q(c, "SELECT dx_bill_visit(%s, %s, NULL, 'cashier')", s["clinic"], vid)["invoice_id"]
    _q(c, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", inv)
    total = _q(c, "SELECT total_paise FROM opd_invoices WHERE id = %s", inv)
    _q(c, """INSERT INTO opd_receipts (clinic_id, invoice_id, mode, amount_paise, gateway, gateway_payment_id)
             VALUES (%s, %s, 'payment_link', %s, 'razorpay', %s)""", s["clinic"], inv, total, "pay_" + uuid.uuid4().hex)
    return inv


@pytest.fixture
def m7(m6):
    m6["c"].cursor().execute("UPDATE clinics SET dx_settings = dx_settings - 'delivery_owner' WHERE id = %s",
                             (m6["clinic"],))
    return m6


# ── templates ────────────────────────────────────────────────────────────────

def test_published_template_is_immutable(m7):
    c, clinic = m7["c"], m7["clinic"]
    t = str(_q(c, "INSERT INTO dx_report_templates (clinic_id, owner_kind, name, layout) VALUES (%s, 'clinic', 'Main', "
                  "%s::jsonb) RETURNING id", clinic, json.dumps(LAYOUT)))
    _q(c, "UPDATE dx_report_templates SET layout = %s::jsonb WHERE id = %s", json.dumps({**LAYOUT, "x": 1}), t)
    assert "dx_template_publish_via_function" in _err(c, """INSERT INTO dx_report_templates (clinic_id, owner_kind, name,
        layout, status, published_at) VALUES (%s, 'clinic', 'Sneaky', '{}', 'published', now())""", clinic)
    assert "dx_template_not_published" in _err(c, "UPDATE dx_report_templates SET status = 'retired' WHERE id = %s", t)
    pub = _q(c, "SELECT dx_publish_template(%s, %s, NULL)", clinic, t)
    assert pub["status"] == "published" and pub["published_at"]
    assert "dx_template_published" in _err(c, "UPDATE dx_report_templates SET layout = '{}' WHERE id = %s", t)
    assert "dx_template_published" in _err(c, "DELETE FROM dx_report_templates WHERE id = %s", t)
    assert "dx_template_published" in _err(c, "SELECT dx_publish_template(%s, %s, NULL)", clinic, t)
    _q(c, "UPDATE dx_report_templates SET status = 'retired' WHERE id = %s", t)
    assert "dx_template_published" in _err(c, "UPDATE dx_report_templates SET status = 'published' WHERE id = %s", t)
    # clinic templates have no owner id; versions of one name are unique (NULL owner included)
    assert "dx_report_templates_owner_shape" in _err(c, """INSERT INTO dx_report_templates (clinic_id, owner_kind, owner_id,
        name, layout) VALUES (%s, 'clinic', %s, 'X1', '{}')""", clinic, m7["branch"])
    assert "dx_report_templates_version_key" in _err(c, """INSERT INTO dx_report_templates (clinic_id, owner_kind, name,
        layout) VALUES (%s, 'clinic', 'Main', '{}')""", clinic)


def test_template_cascade_order(m7):
    c, clinic = m7["c"], m7["clinic"]

    def tpl(kind, owner, name):
        t = str(_q(c, "INSERT INTO dx_report_templates (clinic_id, owner_kind, owner_id, name, layout) VALUES "
                      "(%s, %s, %s, %s, %s::jsonb) RETURNING id", clinic, kind, owner, name, json.dumps(LAYOUT)))
        _q(c, "SELECT dx_publish_template(%s, %s, NULL)", clinic, t)
        return t

    _signed(m7)
    assert _release(m7)["template_snapshot"]["source"] == "builtin"
    clinic_t = tpl("clinic", None, "Clinic default")
    branch_t = tpl("branch", m7["branch"], "Branch")
    _signed(m7, "Lipid Profile", "180")
    r2 = _release(m7)
    assert r2["template_snapshot"]["template_id"] == branch_t and r2["template_snapshot"]["source"] == "branch"
    assert clinic_t != branch_t


# ── releases ────────────────────────────────────────────────────────────────

def test_release_kinds_codes_and_supersede(m7):
    c, clinic = m7["c"], m7["clinic"]
    assert "dx_release_empty" in _err(c, "SELECT dx_create_release(%s, %s, NULL, NULL, NULL, 'x')", clinic,
                                      m7["visit"]["visit"]["id"])
    cbc = _signed(m7)
    r1 = _release(m7)
    assert r1["kind"] == "partial" and r1["release_no"] == 1 and len(r1["verify_code"]) == 10
    assert set(r1["verify_code"]) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") and r1["status"] == "rendering"
    assert r1["letterhead_snapshot"]["site"]["display_name"] == "Main lab"
    assert [d["target"] for d in r1["deliveries"]] == ["patient"]
    assert r1["deliveries"][0]["external_report_id"] == f"dx-{r1['id']}-p"
    assert "dx_release_nothing_new" in _err(c, "SELECT dx_create_release(%s, %s, NULL, NULL, NULL, 'x')", clinic,
                                            m7["visit"]["visit"]["id"])
    _signed(m7, "Lipid Profile", "180")
    assert "dx_release_kind_mismatch:final" in _err(c, "SELECT dx_create_release(%s, %s, NULL, 'partial', NULL, 'x')",
                                                    clinic, m7["visit"]["visit"]["id"])
    r2 = _release(m7)
    assert r2["kind"] == "final" and r2["release_no"] == 2 and len(r2["result_ids"]) == 2
    assert _q(c, "SELECT status FROM dx_accessions WHERE id = %s", m7["visit"]["visit"]["id"]) == "reported"
    old = _q(c, "SELECT to_jsonb(r) FROM dx_report_releases r WHERE id = %s", r1["id"])
    assert old["status"] == "superseded" and old["superseded_by"] == r2["id"]
    assert _decide(m7, r1["id"], "patient") == (False, "release_superseded")
    # amendment: a new CBC version replaces the released one
    amended = _q(c, "SELECT dx_record_results(%s, %s, %s::jsonb, '[]', false, 'transcription error', %s, 'tech')",
                 clinic, m7["orders"]["CBC"]["id"],
                 json.dumps([{"parameter_id": m7["param"]["cbc"], "parameter_name": "CBC", "numeric_value": "15",
                              "flag": "normal"}]), m7["tech"])
    assert _approve(m7, [amended])["signed"] == 1
    r3 = _release(m7)
    assert r3["kind"] == "amended" and cbc["id"] not in r3["result_ids"]
    assert _q(c, "SELECT status FROM dx_report_releases WHERE id = %s", r2["id"]) == "superseded"
    # content never changes; render state moves once
    c.cursor().execute("SELECT set_config('kriya.dx_tx', %s, false)", (clinic,))
    try:                                                     # even inside a DX transaction
        assert "dx_release_immutable" in _err(c, "UPDATE dx_report_releases SET kind = 'final' WHERE id = %s", r3["id"])
    finally:
        c.cursor().execute("SELECT set_config('kriya.dx_tx', '', false)")
    assert "dx_managed:dx_report_releases" in _err(c, "UPDATE dx_report_releases SET status = 'rendered' WHERE id = %s",
                                                   r3["id"])
    done = _rendered(m7, r3)
    assert done["status"] == "rendered" and _rendered(m7, r3)["pdf_sha256"] == "a" * 64       # replay
    assert "dx_append_only:dx_release_results" in _err(c, "DELETE FROM dx_release_results WHERE release_id = %s", r3["id"])


# ── the decision table and the sender's claim ───────────────────────────────

def test_dues_hold_then_payment_releases_and_doubt_holds_again(m7):
    c, clinic = m7["c"], m7["clinic"]
    _signed(m7)
    _signed(m7, "Lipid Profile", "180")
    rel = _release(m7)
    pid = _deliveries(m7, rel["id"])["patient"]["id"]
    assert _deliveries(m7, rel["id"])["patient"]["status"] == "blocked"                 # default owner: connector
    assert _decide(m7, rel["id"], "patient") == (False, "owner_connector")
    _owner(m7, "shadow")
    assert _decide(m7, rel["id"], "patient") == (False, "held_shadow")
    assert _decide(m7, rel["id"], "print_patient") == (False, "held_dues")              # shadow prints, dues still bind
    _owner(m7, "dx")
    assert _q(c, "SELECT dx_requeue_delivery(%s, %s)", clinic, pid)["status"] == "held_dues"
    assert _decide(m7, rel["id"], "account") == (False, "account_rule")                 # no B2B account on this visit
    before = _q(c, "SELECT count(*) FROM dx_outbox WHERE clinic_id = %s", clinic)
    inv = _bill_paid(m7)
    assert _q(c, "SELECT status FROM opd_invoices WHERE id = %s", inv) == "paid"
    assert _q(c, "SELECT count(*) FROM dx_outbox WHERE clinic_id = %s AND processed_at IS NULL", clinic) > before
    claimed = _q(c, "SELECT dx_outbox_claim(1000)")
    assert {"clinic_id": clinic, "accession_id": m7["visit"]["visit"]["id"]} in claimed
    assert _reeval(m7) == []                                                            # queued, not yet rendered
    assert _deliveries(m7, rel["id"])["patient"]["status"] == "queued"
    assert _q(c, "SELECT dx_claim_delivery(%s, %s)", clinic, pid) is False              # waits for the PDF
    _rendered(m7, rel)
    assert _reeval(m7) == [pid]
    assert _q(c, "SELECT dx_claim_delivery(%s, %s)", clinic, pid) is True
    assert _q(c, "SELECT dx_claim_delivery(%s, %s)", clinic, pid) is False              # CAS: one worker wins
    sent = _q(c, "SELECT dx_mark_delivery_result(%s, %s, true, %s, NULL)", clinic, pid, str(uuid.uuid4()))
    assert sent["status"] == "sent" and sent["sent_at"] and sent["attempts"] == 1
    assert "dx_delivery_already_sent" in _err(c, "SELECT dx_requeue_delivery(%s, %s)", clinic, pid)
    # money in doubt after payment (doorstep cash not handed over): the sent copy stays sent, the rest is held
    _guc(m7, "INSERT INTO dx_field_collections (clinic_id, accession_id, amount_paise, mode, collected_by, "
             "collected_by_name, collected_at, idempotency_key) VALUES (%s, %s, 5000, 'cash', %s, 'phleb', now(), %s)",
         clinic, m7["visit"]["visit"]["id"], m7["tech"], uuid.uuid4().hex)
    assert _decide(m7, rel["id"], "patient") == (False, "held_dues")
    assert _decide(m7, rel["id"], "share_link") == (False, "held_dues")
    assert _deliveries(m7, rel["id"])["patient"]["status"] == "sent"
    assert _q(c, "SELECT dx_log_print(%s, %s, NULL, 'desk')", clinic, rel["id"]) == {"allowed": False,
                                                                                     "reason": "held_dues"}
    assert _q(c, "SELECT count(*) FROM dx_print_log WHERE release_id = %s", rel["id"]) == 0


def test_override_releases_the_dues_hold_only(m7):
    c, clinic = m7["c"], m7["clinic"]
    _owner(m7, "dx")
    _signed(m7)
    rel = _release(m7)
    assert _deliveries(m7, rel["id"])["patient"]["status"] == "held_dues"
    vid = m7["visit"]["visit"]["id"]
    # a voided bill leaves the item unbilled: still dues; both status moves were queued for re-evaluation
    before = _q(c, "SELECT count(*) FROM dx_outbox WHERE accession_id = %s", vid)
    inv = _q(c, "SELECT dx_bill_visit(%s, %s, NULL, 'cashier')", clinic, vid)["invoice_id"]
    _q(c, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", inv)
    _q(c, "UPDATE opd_invoices SET status = 'void', voided_at = now(), void_reason = 'wrong patient' WHERE id = %s", inv)
    assert _q(c, "SELECT count(*) FROM dx_outbox WHERE accession_id = %s", vid) == before + 2
    assert _reeval(m7) == [] and _decide(m7, rel["id"], "patient") == (False, "held_dues")
    assert "dx_override_reason" in _err(c, "SELECT dx_create_delivery_override(%s, %s, 'short', NULL, %s, 'admin')",
                                        clinic, vid, m7["path"])
    assert "dx_actor_required" in _err(c, "SELECT dx_create_delivery_override(%s, %s, 'staff member, paid later', "
                                          "NULL, NULL, 'admin')", clinic, vid)
    ov = _q(c, "SELECT dx_create_delivery_override(%s, %s, 'staff member, bill on salary', NULL, %s, 'admin')",
            clinic, vid, m7["path"])
    d = _q(c, "SELECT to_jsonb(d) FROM dx_release_deliveries d WHERE release_id = %s", rel["id"])
    assert d["status"] == "queued" and d["override_id"] == ov["id"]
    assert "dx_append_only:dx_delivery_overrides" in _err(c, "UPDATE dx_delivery_overrides SET reason = 'xxxxxxxxxxxx' "
                                                             "WHERE id = %s", ov["id"])
    # an override never bypasses the other rules
    _owner(m7, "connector")
    assert _decide(m7, rel["id"], "patient") == (False, "owner_connector")
    _owner(m7, "dx")
    printed = _q(c, "SELECT dx_log_print(%s, %s, NULL, 'desk')", clinic, rel["id"])
    assert printed["allowed"] is True and printed["override_id"] == ov["id"] and printed["watermark"] is None


def _account(s, **cols):
    keys = ", ".join(cols)
    vals = ", ".join(["%s"] * len(cols))
    return str(_q(s["c"], f"INSERT INTO dx_b2b_accounts (clinic_id, {keys}) VALUES (%s, {vals}) RETURNING id",
                  s["clinic"], *cols.values()))


def _guc(s, sql, *args):
    """Test-only: a write that P8 / home-collection functions will make (bypasses the dx_tx guard)."""
    cur = s["c"].cursor()
    cur.execute("SELECT set_config('kriya.dx_tx', %s, false)", (s["clinic"],))
    cur.execute(sql, args)
    cur.execute("SELECT set_config('kriya.dx_tx', '', false)")
    cur.close()


def _set_visit(s, **cols):
    """Test-only: a visit shape the P8 billing modes will produce (b2b_split needs DX-M8 pricing)."""
    cur = s["c"].cursor()
    cur.execute("SELECT set_config('kriya.dx_tx', %s, false)", (s["clinic"],))
    sets = ", ".join(f"{k} = %s" for k in cols)
    cur.execute(f"UPDATE dx_accessions SET {sets} WHERE id = %s", (*cols.values(), s["visit"]["visit"]["id"]))
    cur.execute("SELECT set_config('kriya.dx_tx', '', false)")
    cur.close()


def test_tpa_visit_never_targets_patient(m7):
    _owner(m7, "dx")
    acc = _account(m7, name="MD India TPA", kind="insurance_tpa", report_delivery="account",
                   desk_whatsapp="+919800000001")
    _set_visit(m7, b2b_account_id=acc, billing_mode="internal")
    _signed(m7)
    rel = _release(m7)
    d = _deliveries(m7, rel["id"])
    assert set(d) == {"account"} and d["account"]["status"] == "queued" and d["account"]["ext"].endswith("-a")
    for target in ("patient", "print_patient", "share_link", "referrer"):
        assert _decide(m7, rel["id"], target) == (False, "payer_routed")
    assert rel["routing_snapshot"]["account"]["kind"] == "insurance_tpa"


def test_copay_visit(m7):
    _owner(m7, "dx")
    acc = _account(m7, name="Wellness Corp", kind="corporate", report_delivery="both", desk_whatsapp="+919800000002",
                   patient_copy_authorised_at="2026-11-01T00:00:00Z", patient_copy_reason="employee asked for a copy")
    _set_visit(m7, b2b_account_id=acc, billing_mode="b2b_split")
    _signed(m7)
    rel = _release(m7)
    d = _deliveries(m7, rel["id"])
    assert d["account"]["status"] == "queued"                    # the payer's copy goes at once
    assert d["patient"]["status"] == "held_dues"                 # the patient side waits for the co-pay


def test_patient_phone_and_referrer_rules(m7):
    c, clinic = m7["c"], m7["clinic"]
    _owner(m7, "dx")
    phone = "+9198" + str(uuid.uuid4().int)[:8]
    _q(c, "UPDATE patients SET phone = %s WHERE id = %s", phone, m7["patient"])
    acc = _account(m7, name="City Hospital", kind="hospital", report_delivery="both", desk_whatsapp=phone)
    ref = str(_q(c, "INSERT INTO dx_referrers (clinic_id, kind, name, phone, report_copy) VALUES "
                    "(%s, 'doctor', 'Dr. Anuradha', '+919811111111', 'whatsapp') RETURNING id", clinic))
    _set_visit(m7, b2b_account_id=acc, billing_mode="internal", referrer_id=ref)
    _signed(m7)
    rel = _release(m7)
    assert _decide(m7, rel["id"], "patient") == (False, "no_patient_phone")      # the desk number is not the patient's
    assert _decide(m7, rel["id"], "referrer") == (False, "no_referrer_consent")  # no consent on the visit
    _set_visit(m7, delivery_prefs=json.dumps({"consent_at": "2026-11-15T10:00:00+05:30"}))
    assert _decide(m7, rel["id"], "referrer") == (True, None)
    assert {"patient", "referrer", "account"} == set(_deliveries(m7, rel["id"]))


def test_share_link_attempts_and_print_watermark(m7):
    c, clinic = m7["c"], m7["clinic"]
    _owner(m7, "dx")
    _signed(m7)
    rel = _release(m7)
    token = uuid.uuid4().hex * 2
    assert _q(c, "SELECT dx_create_share_link(%s, %s, %s, 72, NULL)", clinic, rel["id"], token)["reason"] == "held_dues"
    _q(c, "SELECT dx_create_delivery_override(%s, %s, 'known patient, paying tomorrow', NULL, %s, 'admin')",
       clinic, m7["visit"]["visit"]["id"], m7["path"])
    link = _q(c, "SELECT dx_create_share_link(%s, %s, %s, 72, NULL)", clinic, rel["id"], token)
    assert link["allowed"] is True
    states = [_q(c, "SELECT dx_share_take_attempt(%s)", token)["state"] for _ in range(6)]
    assert states == ["ok"] * 5 + ["locked"]
    assert _q(c, "SELECT dx_share_take_attempt(%s)", "c" * 64)["state"] == "not_found"
    assert _q(c, "SELECT count(*) FROM dx_events WHERE accession_id = %s AND kind = 'share_unlock_attempt'",
              m7["visit"]["visit"]["id"]) == 5
    _owner(m7, "shadow")
    assert _q(c, "SELECT dx_log_print(%s, %s, NULL, 'desk')", clinic, rel["id"])["watermark"].startswith("SHADOW")


# ── P6b: OPD orders on the DX bench ─────────────────────────────────────────

@pytest.fixture
def opd_on_dx(world):
    """An OPD clinic with DX ACTIVE, a site, an approved CBC test with a tube, a placed OPD order."""
    c, clinic = world["c"], world["clinic"]
    _q(c, "UPDATE clinics SET dx_state = 'ACTIVE', features = features || '{\"dx_enabled\": true}' WHERE id = %s", clinic)
    branch = str(_q(c, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Main') RETURNING id", clinic))
    _q(c, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) VALUES (%s, %s, 'OPD', 'Hospital lab')",
       clinic, branch)
    edta = str(_q(c, "INSERT INTO dx_containers (clinic_id, code, name, colour_hex, sample_type) VALUES "
                     "(%s, '07', 'EDTA', '#8E44AD', 'whole_blood') RETURNING id", clinic))
    dept = str(_q(c, "INSERT INTO dx_departments (clinic_id, code, name, report_heading) VALUES (%s, 'HEM', 'Haem', "
                     "'HAEMATOLOGY') RETURNING id", clinic))
    _q(c, "UPDATE lab_tests SET container_id = %s, department_id = %s, test_code = 'CBC' WHERE id = %s", edta, dept,
       world["cbc"])
    _q(c, "INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, parameter_code) VALUES (%s, %s, 'Hb', 'HB')",
       clinic, world["cbc"])
    _q(c, "UPDATE doctors SET registration_number = 'R/9' WHERE id = %s", world["doctor"])
    d = _q(c, "SELECT dx_test_digest(%s, %s)", clinic, world["cbc"])
    _q(c, "SELECT dx_approve_test(%s, %s, %s, %s, 'Dr')", clinic, world["cbc"], d, world["doctor"])
    placed = _place(c, world, [_cbc_item(world)])
    world["order"] = placed["orders"][0]["id"] if "orders" in placed else placed[0]["id"]
    world["branch"] = branch
    return world


def test_adopt_opd_order_onto_the_bench(opd_on_dx):
    w, c, clinic = opd_on_dx, opd_on_dx["c"], opd_on_dx["clinic"]
    inv_before = _q(c, "SELECT count(*) FROM opd_invoice_items WHERE order_id = %s", w["order"])
    out = _q(c, "SELECT dx_adopt_orders(%s, %s::uuid[], NULL, 'lab')", clinic, [w["order"]])
    assert out["adopted"] == 1 and out["skipped"] == []
    v = out["visits"][0]
    assert v["visit"]["billing_mode"] == "internal" and v["visit"]["source"] == "opd"
    assert v["visit"]["referrer_snapshot"]["name"] == "Dr. Rao"
    assert v["items"][0]["patient_share_paise"] == 0 and v["items"][0]["payer_share_paise"] == 0
    assert len(v["samples"]) == 1 and v["samples"][0]["barcode"][2:4] == "07"
    o = _q(c, "SELECT to_jsonb(o) FROM opd_orders o WHERE id = %s", w["order"])
    assert o["source"] == "opd" and o["encounter_id"] == w["enc"] and o["accession_id"] == v["visit"]["id"]
    assert o["dx_sample_id"] == v["samples"][0]["id"]
    assert _q(c, "SELECT count(*) FROM opd_invoice_items WHERE order_id = %s", w["order"]) == inv_before
    # replay is a no-op; the OPD record cannot be re-linked or edited by anyone else
    again = _q(c, "SELECT dx_adopt_orders(%s, %s::uuid[], NULL, 'lab')", clinic, [w["order"]])
    assert again["adopted"] == 0 and again["skipped"] == [w["order"]]
    assert "opd_record_locked" in _err(c, "UPDATE opd_orders SET accession_id = NULL WHERE id = %s", w["order"])
    # the internal visit's lines belong to adoption only
    assert "dx_internal_visit" in _err(c, "SELECT dx_cancel_visit(%s, %s, 'not needed', NULL, 'x')", clinic,
                                       v["visit"]["id"])
    assert "dx_internal_visit" in _err(c, "SELECT dx_add_items(%s, %s, %s::jsonb, 'adopt-add-01', 0, NULL, 'desk')",
                                       clinic, v["visit"]["id"], json.dumps([{"lab_test_id": w["cbc"]}]))
    # the tube flows through the DX bench: collect, receive, enter (the gate now keys on the visit)
    _q(c, "SELECT dx_collect_samples(%s, %s, %s::jsonb, 'centre', NULL, 'col', %s)", clinic, v["visit"]["id"],
       json.dumps([{"id": v["samples"][0]["id"]}]), uuid.uuid4().hex)
    site = str(_q(c, "SELECT id FROM dx_sites WHERE clinic_id = %s", clinic))
    _q(c, "SELECT dx_receive_sample(%s, %s, %s, NULL, 'rcv')", clinic, v["samples"][0]["barcode"], site)
    pid = str(_q(c, "SELECT id FROM lab_test_parameters WHERE lab_test_id = %s", w["cbc"]))
    r = _q(c, "SELECT dx_record_results(%s, %s, %s::jsonb, '[]', false, NULL, NULL, 'tech')", clinic, w["order"],
           json.dumps([{"parameter_id": pid, "parameter_name": "Hb", "numeric_value": "13", "flag": "normal"}]))
    assert r["status"] == "entered"
    assert _q(c, "SELECT status FROM opd_orders WHERE id = %s", w["order"]) == "in_progress"
    # purge unlinks the OPD order and keeps it (and its result) with the encounter
    out = _q(c, "SELECT dx_purge_clinic(%s)", clinic)
    assert out["adopted_orders_unlinked"] == 1 and out["visits"] == 1
    o = _q(c, "SELECT to_jsonb(o) FROM opd_orders o WHERE id = %s", w["order"])
    assert o["accession_id"] is None and o["status"] == "in_progress"
    assert _q(c, "SELECT count(*) FROM lab_results WHERE order_id = %s", w["order"]) == 1


def test_adopt_refusals(opd_on_dx, lab):
    w, c = opd_on_dx, opd_on_dx["c"]
    assert "lis_order_not_found" in _err(c, "SELECT dx_adopt_orders(%s, %s::uuid[], NULL, 'lab')", w["clinic"],
                                         [str(uuid.uuid4())])
    assert "dx_adopt_orders_invalid" in _err(c, "SELECT dx_adopt_orders(%s, '{}'::uuid[], NULL, 'lab')", w["clinic"])
    dx_order = _register(lab, ["cbc"])["orders"][0]["id"]       # another clinic's DX order is not found here
    assert "lis_order_not_found" in _err(c, "SELECT dx_adopt_orders(%s, %s::uuid[], NULL, 'lab')", w["clinic"],
                                         [dx_order])
    assert "dx_order_not_adoptable" in _err(c, "SELECT dx_adopt_orders(%s, %s::uuid[], NULL, 'lab')", lab["clinic"],
                                            [dx_order])
    _q(c, "UPDATE clinics SET dx_state = 'WIND_DOWN' WHERE id = %s", w["clinic"])
    assert "dx_not_active" in _err(c, "SELECT dx_adopt_orders(%s, %s::uuid[], NULL, 'lab')", w["clinic"], [w["order"]])


# ── purge, rollback ─────────────────────────────────────────────────────────

def test_purge_v7_removes_report_rows(m7):
    c, clinic = m7["c"], m7["clinic"]
    _owner(m7, "dx")
    _signed(m7)
    rel = _release(m7)
    _q(c, "SELECT dx_create_delivery_override(%s, %s, 'known patient, paying tomorrow', NULL, %s, 'admin')",
       clinic, m7["visit"]["visit"]["id"], m7["path"])
    _q(c, "SELECT dx_log_print(%s, %s, NULL, 'desk')", clinic, rel["id"])
    t = str(_q(c, "INSERT INTO dx_report_templates (clinic_id, owner_kind, name, layout) VALUES (%s, 'clinic', 'Tpl', "
                  "'{}') RETURNING id", clinic))
    _q(c, "SELECT dx_publish_template(%s, %s, NULL)", clinic, t)
    out = _q(c, "SELECT dx_purge_clinic(%s)", clinic)
    assert out["releases"] == 1 and out["templates"] == 1
    for tbl in ("dx_report_templates", "dx_report_releases", "dx_release_results", "dx_release_deliveries",
                "dx_delivery_overrides", "dx_print_log", "dx_share_links", "dx_outbox"):
        assert _q(c, f"SELECT count(*) FROM {tbl} WHERE clinic_id = %s", clinic) == 0


def test_rollback_refuses_report_rows_then_reapplies(m7):
    _signed(m7)
    _release(m7)
    cur = m7["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "131_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_131(cur)
        cur.execute("SELECT to_regclass('public.dx_report_releases'), "
                    "(SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_dx_outbox_receipt'), "
                    "position($s$o.source <> 'dx'$s$ in pg_get_functiondef('dx_verify_result(uuid,uuid,timestamptz,"
                    "text,text,uuid,text)'::regprocedure)) > 0")
        assert cur.fetchone() == (None, 0, True)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'dx\\_%%'
                         AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x WHERE x LIKE 'search_path=%%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
