"""rx_tax (Python) == migration 138 (SQL): TEST-PLAN §4.1 / §4.2 vectors and a property test (RX-30)."""

import json
import random

import pytest

from app.services.rx_tax import (
    calculate_loose_unit_price, gst_split_inclusive, prescribed_units, price_lines, round_off_to_rupee, spread,
)

RATES = (0, 500, 1200, 1800, 2800, 4000)


@pytest.mark.parametrize("gross,rate,out", [
    (10500, 500, (10000, 250, 250)), (11200, 1200, (10000, 600, 600)), (11800, 1800, (10000, 900, 900)),
    (9999, 500, (9523, 238, 238)), (1, 500, (1, 0, 0)), (10000, 0, (10000, 0, 0)),
])
def test_split_vectors(gross, rate, out):
    assert gst_split_inclusive(gross, rate) == out
    assert gst_split_inclusive(gross, rate, has_gstin=False) == (gross, 0, 0)


def test_loose_price_vectors_and_bound():
    assert calculate_loose_unit_price(12000, 10, 3) == 3600
    assert calculate_loose_unit_price(9999, 15, 7) == 4666
    for mrp, pack in ((9999, 15), (12345, 7), (1, 10), (100000, 30)):
        whole = sum(calculate_loose_unit_price(mrp, pack, 1) for _ in range(pack))
        assert abs(whole - mrp) <= pack // 2 + 1                         # TEST-PLAN §4.2 bound
        for units in range(1, pack + 1):
            assert calculate_loose_unit_price(mrp, pack, units) <= mrp * units // pack + 1   # the CHECK


def test_frequency_map_closed():
    assert prescribed_units("1", "TDS", 5, "tablet") == 15
    assert prescribed_units("1/2", "bd", 10, "tablet") == 10
    assert prescribed_units("2 tabs", "1-0-1", 3, "capsule") == 12
    assert prescribed_units("1", "SOS", 5, "tablet") is None
    assert prescribed_units("10 ml", "TDS", 5, "syrup") is None
    assert prescribed_units("as directed", "BD", 5, "tablet") is None
    assert prescribed_units("1", "OD", None, "tablet") is None


def test_spread_and_preview():
    assert spread(1001, [3600, 8400]) == [300, 701] and spread(0, [5, 5]) == [0, 0] and spread(3, []) == []
    assert round_off_to_rupee(10999) == 1 and round_off_to_rupee(11049) == -49 and round_off_to_rupee(11050) == 50
    p = price_lines([{"mrp_pack_paise": 12000, "pack_size": 10, "qty_units": 3, "tax_rate_bps": 1200},
                     {"mrp_pack_paise": 12000, "pack_size": 10, "qty_units": 7, "tax_rate_bps": 1200}], 1001)
    assert (p["gross_paise"], p["discount_paise"], p["round_off_paise"], p["net_paise"]) == (12000, 1001, 1, 11000)
    assert p["taxable_paise"] + p["cgst_paise"] + p["sgst_paise"] == 12000 - 1001


def test_python_equals_sql_property(real_pg_conn):
    """10⁴ random amounts per rate (TEST-PLAN asks 10⁵ over the suite's lifetime; seeded, deterministic)."""
    rng = random.Random(138)
    cases = [(rng.randint(1, 10 ** 8), rate) for rate in RATES for _ in range(10 ** 4 // len(RATES))]
    cur = real_pg_conn.cursor()
    cur.execute("""SELECT json_agg(json_build_array(x.g, x.r, s.taxable, s.cgst, s.sgst, rx_unit_price((x.g %% 100000000 + 1)::int, 10, 7)))
                     FROM json_to_recordset(%s::json) AS x(g bigint, r int), rx_gst_split_inclusive(x.g, x.r, true) s""",
                (json.dumps([{"g": g, "r": r} for g, r in cases]),))
    for g, r, t, c, s, price in cur.fetchone()[0]:
        assert (t, c, s) == gst_split_inclusive(g, r), (g, r)
        assert t + c + s == g and c >= 0 and s >= 0
        assert price == calculate_loose_unit_price(g % 100000000 + 1, 10, 7)
    for dosage, freq, days, form in (("1", "TDS", 5, "tablet"), ("1/2", "BD", 7, "tablet"), ("0.5", "QID", 3, "tablet"),
                                     ("2", "1-1-1", 30, "capsule"), ("1", "SOS", 3, "tablet"), ("5 ml", "BD", 5, "syrup")):
        cur.execute("SELECT rx_rx_units(%s, %s, %s, %s)", (dosage, freq, days, form))
        assert cur.fetchone()[0] == prescribed_units(dosage, freq, days, form)
