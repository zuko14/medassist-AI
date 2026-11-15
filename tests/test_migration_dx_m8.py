"""Migration 132 (Diagnostic OS B2B money — DX-M8) on real PostgreSQL.

Rate-card pricing and co-pay shares; one charge per item (never twice) and one
reversal per cancelled item; prepaid balance and on-hold refusals; the append-only
ledger; payments (idempotent, UTR for NEFT); statements (gapless B2B-YYYY-NNNNN,
totals = linked rows, opening = previous closing, no overlap, two parallel issues ->
one wins, void); automatic holds; ageing; barcode stock (no overlap, bind check);
patient-billed visits unchanged; purge v8 and a guarded rollback.
"""

import json
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_dx_m3 import _err, _q, _register, lab  # noqa: F401  (fixture)

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "132_dx_b2b.sql", MIG / "rollback" / "132_rollback_dx_b2b.sql"


def unwind_132(cur) -> None:
    """Inside an open transaction: purge every clinic with DX-M8 rows, run the 132
    rollback. Called first by unwind_131 (newest first)."""
    from tests.test_migration_dx_m9 import unwind_133
    unwind_133(cur)   # newest first: 133's purge v9 patches v8
    cur.execute("SELECT to_regclass('public.dx_b2b_ledger') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT dx_purge_clinic(x.clinic_id) FROM (
                     SELECT clinic_id FROM dx_b2b_ledger UNION SELECT clinic_id FROM dx_b2b_statements
                     UNION SELECT clinic_id FROM dx_rate_cards UNION SELECT clinic_id FROM dx_barcode_stock
                     UNION SELECT clinic_id FROM clinic_admins WHERE dx_b2b_account_id IS NOT NULL) x""")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _account(s, mode="b2b_credit", **cols):
    data = {"name": "City Hospital", "kind": "hospital", "default_billing_mode": mode, "desk_whatsapp": "+919800000001",
            **cols}
    keys = ", ".join(data)
    marks = ", ".join(["%s"] * len(data))
    return str(_q(s["c"], f"INSERT INTO dx_b2b_accounts (clinic_id, {keys}) VALUES (%s, {marks}) RETURNING id",
                  s["clinic"], *data.values()))


def _card(s, prices: dict, account=None, **cols):
    card = str(_q(s["c"], "INSERT INTO dx_rate_cards (clinic_id, name, kind) VALUES (%s, %s, 'b2b') RETURNING id",
                  s["clinic"], cols.get("name", "Contract")))
    for key, price in prices.items():
        _q(s["c"], "INSERT INTO dx_rate_card_items (clinic_id, rate_card_id, lab_test_id, price_paise) VALUES (%s, %s, %s, %s)",
           s["clinic"], card, s["T"][key], price)
    if account:
        _q(s["c"], "UPDATE dx_b2b_accounts SET rate_card_id = %s WHERE id = %s", card, account)
    return card


def _b2b(s, account, items, mode="b2b_credit", **extra):
    return _register(s, items, b2b_account_id=account, billing_mode=mode, source="b2b", **extra)


def _ledger(s, account):
    cur = s["c"].cursor()
    cur.execute("SELECT entry_type, amount_paise, accession_item_id::text, statement_id::text FROM dx_b2b_ledger "
                "WHERE account_id = %s ORDER BY created_at, id", (account,))
    out = cur.fetchall()
    cur.close()
    return out


def _balance(s, account):
    cur = s["c"].cursor()
    cur.execute("SELECT charges_paise, credits_paise, balance_paise, overdue_amount_paise, oldest_unpaid_date "
                "FROM dx_b2b_balance(%s, %s)", (s["clinic"], account))
    out = cur.fetchone()
    cur.close()
    return out


def _pay(s, account, amount, mode="cash", ref=None, key=None):
    return _q(s["c"], "SELECT dx_b2b_record_payment(%s, %s, %s, %s, %s, %s, NULL, 'acct')", s["clinic"], account, amount,
              mode, ref, key or uuid.uuid4().hex)


def _backdated(s, account, entry_type, amount, days_ago, mode=None):
    """A ledger row dated in the past (an imported opening balance, an old payment)."""
    cur = s["c"].cursor()
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (s["clinic"],))
    cur.execute("""INSERT INTO dx_b2b_ledger (clinic_id, account_id, entry_type, amount_paise, payment_mode, created_by_name,
                   created_at) VALUES (%s, %s, %s, %s, %s, 'import', now() - make_interval(days => %s))""",
                (s["clinic"], account, entry_type, amount, mode, days_ago))
    cur.execute("COMMIT")
    cur.close()


def _statement(s, account, frm, to, conn=None):
    return _q(conn or s["c"], "SELECT dx_b2b_issue_statement(%s, %s, %s, %s, NULL, 'acct')", s["clinic"], account, frm, to)


TODAY = date.today


# ── pricing, charges ─────────────────────────────────────────────────────────

def test_credit_visit_charges_at_rate_card(lab):
    acc = _account(lab)
    _card(lab, {"cbc": 20000}, account=acc)
    v = _b2b(lab, acc, ["cbc", "lipid"])
    items = {i["item_name"]: i for i in v["items"]}
    assert (items["CBC"]["price_paise"], items["CBC"]["price_source"], items["CBC"]["patient_share_paise"],
            items["CBC"]["payer_share_paise"]) == (20000, "rate_card", 0, 20000)
    assert (items["Lipid Profile"]["price_source"], items["Lipid Profile"]["payer_share_paise"]) == ("mrp", 55000)
    assert _q(lab["c"], "SELECT rate_card_id::text FROM dx_accession_items WHERE id = %s", items["CBC"]["id"])
    assert sorted(r[:2] for r in _ledger(lab, acc)) == [("charge", 20000), ("charge", 55000)]
    assert _balance(lab, acc)[:3] == (75000, 0, 75000)
    # no patient bill for a credit visit
    assert "dx_not_patient_billing" in _err(lab["c"], "SELECT dx_bill_visit(%s, %s, NULL, 'x')", lab["clinic"],
                                            v["visit"]["id"])
    # add items: charged too, at the card price
    _q(lab["c"], "SELECT dx_add_items(%s, %s, %s::jsonb, 'k-add-1', 10, NULL, 'desk')", lab["clinic"], v["visit"]["id"],
       json.dumps([{"lab_test_id": lab["T"]["hba1c"]}]))
    assert len(_ledger(lab, acc)) == 3


def test_patient_visit_unchanged_no_ledger(lab):
    acc = _account(lab, mode="patient")
    v = _register(lab, ["cbc"])
    assert v["items"][0]["price_source"] == "mrp" and v["items"][0]["patient_share_paise"] == 35000
    v2 = _register(lab, ["cbc"], b2b_account_id=acc)            # an account visit billed to the patient
    assert v2["items"][0]["payer_share_paise"] == 0
    assert _q(lab["c"], "SELECT count(*) FROM dx_b2b_ledger WHERE clinic_id = %s", lab["clinic"]) == 0
    assert "dx_b2b_account_required" in _err(lab["c"], "SELECT dx_quote(%s, %s::jsonb)", lab["clinic"], json.dumps(
        {"branch_id": lab["branch"], "patient_id": lab["patient"], "billing_mode": "b2b_credit",
         "items": [{"lab_test_id": lab["T"]["cbc"]}]}))


def test_copay_percent_and_fixed(lab):
    pct = _account(lab, mode="b2b_split", name="TPA One", kind="insurance_tpa", report_delivery="account",
                   copay_mode="percent", copay_value=1000)
    v = _b2b(lab, pct, ["cbc", "lipid"], mode="b2b_split")      # 10 %: 3500 + 5500
    shares = [(i["patient_share_paise"], i["payer_share_paise"]) for i in v["items"]]
    assert shares == [(3500, 31500), (5500, 49500)]
    bill = _q(lab["c"], "SELECT dx_bill_visit(%s, %s, NULL, 'cashier')", lab["clinic"], v["visit"]["id"])
    assert _q(lab["c"], "SELECT total_paise FROM opd_invoices WHERE id = %s", bill["invoice_id"]) == 9000
    assert sorted(r[1] for r in _ledger(lab, pct)) == [31500, 49500]
    # floor per item, remainder to the payer: 33.33 % of 35000
    _q(lab["c"], "UPDATE dx_b2b_accounts SET copay_value = 3333 WHERE id = %s", pct)
    odd = _b2b(lab, pct, ["cbc"], mode="b2b_split")["items"][0]
    assert (odd["patient_share_paise"], odd["payer_share_paise"]) == (11665, 23335)

    fixed = _account(lab, mode="b2b_split", name="Corp Fixed", kind="corporate", report_delivery="account",
                     copay_mode="fixed", copay_value=40000)
    f = _b2b(lab, fixed, ["cbc", "lipid"], mode="b2b_split")    # 400 rupees: all of CBC, then 5000 of lipid
    assert [(i["patient_share_paise"], i["payer_share_paise"]) for i in f["items"]] == [(35000, 0), (5000, 50000)]
    assert [r[1] for r in _ledger(lab, fixed)] == [50000]       # a zero payer share posts nothing
    added = _q(lab["c"], "SELECT dx_add_items(%s, %s, %s::jsonb, 'k-add-fixed', 10, NULL, 'desk')", lab["clinic"],
               f["visit"]["id"], json.dumps([{"lab_test_id": lab["T"]["hba1c"]}]))
    hb = next(i for i in added["items"] if i["item_name"] == "HbA1c")
    assert (hb["patient_share_paise"], hb["payer_share_paise"]) == (0, 45000)   # co-pay already used up
    for bad in ({"copay_mode": "percent", "copay_value": None}, {"copay_mode": "none", "copay_value": 10},
                {"copay_mode": "percent", "copay_value": 10001}):
        assert "dx_b2b_copay" in _err(lab["c"], "UPDATE dx_b2b_accounts SET copay_mode = %s, copay_value = %s WHERE id = %s",
                                      bad["copay_mode"], bad["copay_value"], fixed)


def test_prepaid_balance_and_hold_refusals(lab):
    acc = _account(lab, mode="b2b_prepaid")
    assert "dx_b2b_prepaid_insufficient" in _err(lab["c"], "SELECT dx_register_visit(%s, %s::jsonb, NULL, 'desk')",
                                                 lab["clinic"], json.dumps({
        "branch_id": lab["branch"], "patient_id": lab["patient"], "idempotency_key": uuid.uuid4().hex,
        "b2b_account_id": acc, "billing_mode": "b2b_prepaid", "items": [{"lab_test_id": lab["T"]["cbc"]}]}))
    top = _pay(lab, acc, 50000, mode="upi", ref="UTR123")
    assert top["entry_type"] == "prepaid_topup"
    _b2b(lab, acc, ["cbc"], mode="b2b_prepaid")                 # 35000 of 50000
    assert _balance(lab, acc)[2] == -15000
    assert "dx_b2b_prepaid_insufficient" in _err(lab["c"], "SELECT dx_add_items(%s, %s, %s::jsonb, 'k-pp', 10, NULL, 'd')",
                                                 lab["clinic"], _q(lab["c"], "SELECT accession_id FROM dx_b2b_ledger "
                                                                              "WHERE account_id = %s AND entry_type = 'charge'", acc),
                                                 json.dumps([{"lab_test_id": lab["T"]["lipid"]}]))
    credit = _account(lab, name="Held Clinic")
    _q(lab["c"], "SELECT dx_b2b_set_hold(%s, %s, 'on_hold', 'manual', 'cheque bounced', NULL, 'acct')", lab["clinic"], credit)
    assert "dx_b2b_on_hold" in _err(lab["c"], "SELECT dx_register_visit(%s, %s::jsonb, NULL, 'desk')", lab["clinic"],
                                    json.dumps({"branch_id": lab["branch"], "patient_id": lab["patient"],
                                                "idempotency_key": uuid.uuid4().hex, "b2b_account_id": credit,
                                                "billing_mode": "b2b_credit", "items": [{"lab_test_id": lab["T"]["cbc"]}]}))


def test_cancel_reverses_once(lab):
    acc = _account(lab)
    v = _b2b(lab, acc, ["cbc", "lipid", "hba1c"])
    cbc = next(i for i in v["items"] if i["item_name"] == "CBC")
    for _ in range(2):
        _q(lab["c"], "SELECT dx_cancel_item(%s, %s, 'ordered twice', NULL, 'desk')", lab["clinic"], cbc["id"])
    rev = [r for r in _ledger(lab, acc) if r[0] == "charge_reversal"]
    assert rev == [("charge_reversal", 35000, cbc["id"], None)]
    _q(lab["c"], "SELECT dx_cancel_visit(%s, %s, 'patient left', NULL, 'desk')", lab["clinic"], v["visit"]["id"])
    assert sum(1 for r in _ledger(lab, acc) if r[0] == "charge_reversal") == 3
    assert _balance(lab, acc)[2] == 0


def test_ledger_append_only_and_unique_charge(lab):
    acc = _account(lab)
    v = _b2b(lab, acc, ["cbc"])
    row = _q(lab["c"], "SELECT id FROM dx_b2b_ledger WHERE account_id = %s", acc)
    assert "dx_managed:dx_b2b_ledger" in _err(lab["c"], """INSERT INTO dx_b2b_ledger (clinic_id, account_id, entry_type,
        amount_paise, created_by_name) VALUES (%s, %s, 'write_off', 100, 'x')""", lab["clinic"], acc)
    assert "dx_managed:dx_b2b_ledger" in _err(lab["c"], "UPDATE dx_b2b_ledger SET amount_paise = 1 WHERE id = %s", row)
    assert "dx_ledger_append_only" in _err(lab["c"], "DELETE FROM dx_b2b_ledger WHERE id = %s", row)
    # a second charge for the same item is impossible, even from the posting function
    assert "uq_dx_ledger_charge" in _err(lab["c"], "SELECT dx_b2b_post_charge(%s, %s, %s, %s, 100, NULL, 'x')",
                                         lab["clinic"], acc, v["visit"]["id"], v["items"][0]["id"])


def test_payments_idempotent_and_reference(lab):
    acc = _account(lab)
    assert "dx_payment_reference_required" in _err(lab["c"], "SELECT dx_b2b_record_payment(%s, %s, 100, 'neft', NULL, "
                                                             "'k-neft-1', NULL, 'a')", lab["clinic"], acc)
    p1 = _pay(lab, acc, 10000, mode="neft", ref="UTR9", key="pay-key-0001")
    p2 = _pay(lab, acc, 10000, mode="neft", ref="UTR9", key="pay-key-0001")
    assert (p1["entry_type"], p1["replayed"], p2["replayed"], p2["id"]) == ("payment", False, True, p1["id"])
    assert "dx_idempotency_reused" in _err(lab["c"], "SELECT dx_b2b_record_payment(%s, %s, 999, 'cash', NULL, "
                                                     "'pay-key-0001', NULL, 'a')", lab["clinic"], acc)
    assert _balance(lab, acc)[2] == -10000


# ── statements ───────────────────────────────────────────────────────────────

def test_statement_totals_numbering_and_chain(lab):
    acc = _account(lab)
    _backdated(lab, acc, "opening_balance", 100000, 40)
    _backdated(lab, acc, "payment", 30000, 35, mode="cash")
    first = _statement(lab, acc, TODAY() - timedelta(days=45), TODAY() - timedelta(days=31))
    year = (TODAY() - timedelta(days=31)).year
    assert first["statement_number"] == f"B2B-{year}-{first['statement_seq']:05d}"
    assert (first["opening_paise"], first["total_charges_paise"], first["total_credits_paise"],
            first["closing_paise"]) == (0, 100000, 30000, 70000)
    v = _b2b(lab, acc, ["cbc", "lipid"])
    _q(lab["c"], "SELECT dx_cancel_item(%s, %s, 'not needed', NULL, 'desk')", lab["clinic"], v["items"][1]["id"])
    _pay(lab, acc, 5000)
    second = _statement(lab, acc, TODAY() - timedelta(days=30), TODAY())
    # opening = previous closing; totals = the rows it linked
    assert second["opening_paise"] == first["closing_paise"]
    assert (second["total_charges_paise"], second["total_credits_paise"]) == (90000, 55000 + 5000)
    linked = _q(lab["c"], """SELECT jsonb_build_object('dr', sum(amount_paise) FILTER (WHERE entry_type = 'charge'),
                               'n', count(*)) FROM dx_b2b_ledger WHERE statement_id = %s""", second["id"])
    assert linked == {"dr": 90000, "n": 4}
    assert second["closing_paise"] == _balance(lab, acc)[2] == 70000 + 90000 - 60000
    assert second["statement_seq"] == first["statement_seq"] + 1 if year == TODAY().year else True
    assert "dx_statement_overlap" in _err(lab["c"], "SELECT dx_b2b_issue_statement(%s, %s, %s, %s, NULL, 'a')",
                                          lab["clinic"], acc, TODAY() - timedelta(days=2), TODAY())
    assert "dx_statement_period_future" in _err(lab["c"], "SELECT dx_b2b_issue_statement(%s, %s, %s, %s, NULL, 'a')",
                                                lab["clinic"], acc, TODAY(), TODAY() + timedelta(days=1))
    # a statement never changes; the PDF path can be set
    assert "dx_statement_immutable" in _err(lab["c"], "UPDATE dx_b2b_statements SET closing_paise = 1 WHERE id = %s",
                                            second["id"])
    _q(lab["c"], "UPDATE dx_b2b_statements SET pdf_storage_path = 'x.pdf' WHERE id = %s", second["id"])
    # void: only the newest; its rows return to unbilled and are re-issued under a new number
    assert "dx_statement_not_latest" in _err(lab["c"], "SELECT dx_b2b_void_statement(%s, %s, 'wrong', NULL, 'a')",
                                             lab["clinic"], first["id"])
    void = _q(lab["c"], "SELECT dx_b2b_void_statement(%s, %s, 'wrong rates', NULL, 'a')", lab["clinic"], second["id"])
    assert void["status"] == "void"
    assert _q(lab["c"], "SELECT count(*) FROM dx_b2b_ledger WHERE statement_id = %s", second["id"]) == 0
    again = _statement(lab, acc, TODAY() - timedelta(days=30), TODAY())
    assert again["closing_paise"] == second["closing_paise"] and again["statement_seq"] > second["statement_seq"]


def test_two_parallel_statements_one_wins(lab, real_postgres_uri):
    acc = _account(lab)
    _b2b(lab, acc, ["cbc"])
    results, barrier = [], threading.Barrier(2)

    def issue():
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        barrier.wait()
        try:
            results.append(_statement(lab, acc, TODAY() - timedelta(days=6), TODAY(), conn=conn))
        except psycopg2.Error as e:
            results.append(str(e))
        finally:
            conn.close()

    threads = [threading.Thread(target=issue) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ok = [r for r in results if isinstance(r, dict)]
    assert len(ok) == 1 and any("dx_statement_overlap" in str(r) or "uq_dx_b2b_statement_period" in str(r)
                                for r in results if not isinstance(r, dict))
    # the failed issue left no gap in the series
    assert _q(lab["c"], "SELECT count(*) FROM dx_b2b_statements WHERE clinic_id = %s", lab["clinic"]) == 1


# ── holds, ageing ────────────────────────────────────────────────────────────

def test_hold_evaluation_limit_overdue_and_manual(lab):
    limit = _account(lab, name="Limit Hospital", credit_limit_paise=30000)
    _b2b(lab, limit, ["cbc"])                                    # 35000 > 30000
    overdue = _account(lab, name="Slow Payer", credit_days=15)
    _backdated(lab, overdue, "opening_balance", 20000, 20)
    manual = _account(lab, name="Manual Hold", credit_limit_paise=0)
    _q(lab["c"], "SELECT dx_b2b_set_hold(%s, %s, 'on_hold', 'manual', 'dispute', NULL, 'a')", lab["clinic"], manual)
    no_auto = _account(lab, name="No Auto", credit_limit_paise=1, auto_hold=False)
    _backdated(lab, no_auto, "opening_balance", 500, 1)
    changes = _q(lab["c"], "SELECT dx_b2b_evaluate_holds(%s)", lab["clinic"])
    assert {(c["account_id"], c["hold_kind"]) for c in changes} == {(limit, "limit"), (overdue, "overdue")}
    assert _balance(lab, overdue)[3:] == (20000, TODAY() - timedelta(days=20))
    st = lambda a: _q(lab["c"], "SELECT status || '/' || COALESCE(hold_kind, '') FROM dx_b2b_accounts WHERE id = %s", a)  # noqa: E731
    assert (st(limit), st(overdue), st(manual), st(no_auto)) == ("on_hold/limit", "on_hold/overdue", "on_hold/manual",
                                                                  "active/")
    assert _q(lab["c"], "SELECT dx_b2b_evaluate_holds(%s)", lab["clinic"]) == []      # stable
    _pay(lab, limit, 10000)
    _pay(lab, overdue, 20000)
    lifted = _q(lab["c"], "SELECT dx_b2b_evaluate_holds(%s)", lab["clinic"])
    assert {c["account_id"] for c in lifted} == {limit, overdue} and st(manual) == "on_hold/manual"
    assert "dx_hold_kind_invalid" in _err(lab["c"], "SELECT dx_b2b_set_hold(%s, %s, 'on_hold', NULL, 'x', NULL, 'a')",
                                          lab["clinic"], limit)


def test_ageing_buckets_fifo(lab):
    acc = _account(lab, credit_days=30)
    _backdated(lab, acc, "opening_balance", 10000, 100)          # 70 days past due
    _backdated(lab, acc, "opening_balance", 20000, 45)           # 15 days past due
    _backdated(lab, acc, "opening_balance", 30000, 5)            # not due
    _pay(lab, acc, 4000)                                          # settles the oldest first
    row = next(r for r in _q(lab["c"], "SELECT dx_b2b_ageing(%s)", lab["clinic"]) if r["account_id"] == acc)
    assert (row["current_paise"], row["d1_30_paise"], row["d31_60_paise"], row["d61_90_paise"],
            row["d90_plus_paise"], row["balance_paise"]) == (30000, 20000, 0, 6000, 0, 56000)


# ── barcode stock ────────────────────────────────────────────────────────────

def test_barcode_stock_overlap_and_bind_check(lab):
    acc = _account(lab)
    _q(lab["c"], "INSERT INTO dx_barcode_stock (clinic_id, account_id, prefix, range_from, range_to) VALUES "
                 "(%s, %s, 'CH', 1000, 1999)", lab["clinic"], acc)
    assert "dx_barcode_stock_overlap" in _err(lab["c"], "INSERT INTO dx_barcode_stock (clinic_id, site_id, prefix, "
                                                        "range_from, range_to) VALUES (%s, %s, 'CH', 1500, 2500)",
                                              lab["clinic"], lab["site"])
    _q(lab["c"], "INSERT INTO dx_barcode_stock (clinic_id, site_id, prefix, range_from, range_to) VALUES "
                 "(%s, %s, 'CH', 2000, 2999)", lab["clinic"], lab["site"])
    assert "dx_barcode_stock_owner" in _err(lab["c"], "INSERT INTO dx_barcode_stock (clinic_id, prefix, range_from, "
                                                      "range_to) VALUES (%s, 'ZZ', 1, 2)", lab["clinic"])
    assert "dx_barcode_stock_immutable" in _err(lab["c"], "UPDATE dx_barcode_stock SET range_to = 5000 WHERE clinic_id = %s",
                                                lab["clinic"])
    v = _b2b(lab, acc, ["cbc"])
    c = lab["c"]
    cur = c.cursor()
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (lab["clinic"],))
    cur.execute("""INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode_source,
                   collection_site_id, processing_site_id) VALUES (%s, %s, %s, 'serum', 'preprinted', %s, %s) RETURNING id""",
                (lab["clinic"], v["visit"]["id"], lab["serum"], lab["site"], lab["site"]))
    sid = str(cur.fetchone()[0])
    cur.execute("COMMIT")
    assert "dx_barcode_not_issued" in _err(c, "SELECT dx_bind_barcode(%s, %s, 'XX1500', NULL, 'n')", lab["clinic"], sid)
    assert "dx_barcode_not_issued" in _err(c, "SELECT dx_bind_barcode(%s, %s, 'CH3000', NULL, 'n')", lab["clinic"], sid)
    assert _q(c, "SELECT dx_bind_barcode(%s, %s, 'ch1500', NULL, 'n')", lab["clinic"], sid)["barcode"] == "ch1500"


def test_bind_without_stock_is_unchecked(lab):
    v = _register(lab, ["cbc"])
    cur = lab["c"].cursor()
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (lab["clinic"],))
    cur.execute("""INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode_source,
                   collection_site_id, processing_site_id) VALUES (%s, %s, %s, 'serum', 'preprinted', %s, %s) RETURNING id""",
                (lab["clinic"], v["visit"]["id"], lab["serum"], lab["site"], lab["site"]))
    sid = str(cur.fetchone()[0])
    cur.execute("COMMIT")
    code = "ANY" + uuid.uuid4().hex[:8]
    assert _q(lab["c"], "SELECT dx_bind_barcode(%s, %s, %s, NULL, 'n')", lab["clinic"], sid, code)["barcode"] == code


# ── purge, rollback ──────────────────────────────────────────────────────────

def test_purge_v8_removes_b2b_rows(lab):
    c, clinic = lab["c"], lab["clinic"]
    acc = _account(lab)
    _card(lab, {"cbc": 100}, account=acc)
    _b2b(lab, acc, ["cbc"])
    _statement(lab, acc, TODAY() - timedelta(days=1), TODAY())
    _q(c, "INSERT INTO dx_barcode_stock (clinic_id, account_id, prefix, range_from, range_to) VALUES (%s, %s, 'P', 1, 9)",
       clinic, acc)
    _q(c, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, dx_b2b_account_id) VALUES "
          "(%s, %s, 'x', 'staff', 'B2B_PARTNER', %s)", clinic, "p_" + uuid.uuid4().hex[:8], acc)
    out = _q(c, "SELECT dx_purge_clinic(%s)", clinic)
    assert (out["ledger_rows"], out["statements"], out["partner_logins"], out["rate_cards"]) == (1, 1, 1, 1)
    for tbl in ("dx_b2b_ledger", "dx_b2b_statements", "dx_barcode_stock", "dx_rate_cards", "dx_rate_card_items",
                "dx_b2b_accounts"):
        assert _q(c, f"SELECT count(*) FROM {tbl} WHERE clinic_id = %s", clinic) == 0


def test_rollback_refuses_b2b_rows_then_reapplies(lab):
    acc = _account(lab)
    _b2b(lab, acc, ["cbc"])
    cur = lab["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "132_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_132(cur)
        cur.execute("SELECT to_regclass('public.dx_b2b_ledger'), "
                    "position('dx_b2b_post_visit_charges' in pg_get_functiondef('dx_register_visit(uuid,jsonb,uuid,text)'"
                    "::regprocedure)), position('dx_b2b_not_enabled' in pg_get_functiondef('dx_price_items(jsonb,jsonb)'"
                    "::regprocedure)) > 0")
        assert cur.fetchone() == (None, 0, True)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname = 'dx_register_visit' "
                    "AND prosrc LIKE '%%dx_b2b_post_visit_charges%%dx_b2b_post_visit_charges%%'")
        assert cur.fetchone()[0] == 0          # re-running never patches twice
        cur.execute("""SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'dx\\_%%'
                         AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x WHERE x LIKE 'search_path=%%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
