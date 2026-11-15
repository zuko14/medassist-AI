"""Diagnostic OS dashboard and MIS (P9, DX-54): real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

Six visits are driven to known stages (registered, collected, received, entered, entered with a critical,
signed + released and held for dues) plus one rejected tube; the dashboard funnel must reconcile with the
Lab Worklist, and TAT / volume / revenue / rejection / referrer MIS with what was done. Role and date-range
boundaries, CSV export (audit, formula-safe cells) and the pure helpers are checked too.
"""

import uuid
from datetime import date, timedelta

import pytest

from tests.test_dx_reports_e2e import _pay, _signed_visit, rep  # noqa: F401  (fixture)
from tests.test_dx_results_e2e import _as, _collect_receive, _order, _save, _visit, bench  # noqa: F401
from tests.test_dx_visits_e2e import dxw  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _backdate(w, visit_id, hours):
    cur = w["c"].cursor()
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (w["clinic"],))
    cur.execute("UPDATE dx_accessions SET created_at = now() - make_interval(hours => %s) WHERE id = %s",
                (hours, visit_id))
    cur.execute("COMMIT")


@pytest.fixture
def lab(rep, monkeypatch):  # noqa: F811
    """rep + one visit per stage; returns (w, visits)."""
    from app.services import dx

    async def no_notice(*_a, **_k):
        return True
    monkeypatch.setattr(dx, "notify_recollection", no_notice)
    w, c, U, uid = rep, rep["clinic"], rep["api"].U, lambda: str(uuid.uuid4())
    w["u"].update({"mis": U("mis", "staff", c, uid(), ["DX_REPORTS_MIS"], staff_role="STAFF"),
                   "misbill": U("misbill", "staff", c, uid(), ["DX_REPORTS_MIS", "DX_BILLING"], staff_role="STAFF")})
    ref = _as(w, "desk").call("POST", "/admin/dx/referrers", c, {"name": "=HYPERLINK(evil)", "qualification": "MD"})
    assert ref.status_code == 201, ref.text
    V = {"reg": _visit(w, ["hba1c"])}
    V["col"] = _visit(w, ["lipid"], referrer_id=ref.json()["id"])
    _collect_receive(w, V["col"], receive=False)
    _backdate(w, V["col"]["visit"]["id"], 5)                         # lipid TAT is 240 min: breached
    V["rcv"] = _visit(w, ["hba1c"])
    _collect_receive(w, V["rcv"])
    for key, value in (("ent", "14"), ("crit", "35")):              # CBC critical high is 30
        V[key] = _visit(w, ["cbc"])
        _collect_receive(w, V[key])
        assert _save(w, _order(V[key], "CBC"), {"CBC": value}).status_code == 200
    V["signed"], _ = _signed_visit(w, ("cbc",))                       # unpaid: every target held for dues
    V["rej"] = _visit(w, ["cbc"])
    _collect_receive(w, V["rej"])
    r = _as(w, "tech").call("POST", f"/admin/dx/samples/{V['rej']['samples'][0]['id']}/reject", c,
                            {"reason_code": "HEMOLYSED", "recollect": True})
    assert r.status_code == 200, r.text
    _pay(w, V["rcv"])                                                 # cash 45,000 on a DX bill
    w["ref"] = ref.json()
    return w, V


def _get(w, who, path, **q):
    r = _as(w, who).call("GET", path, w["clinic"], **q)
    assert r.status_code == 200, r.text
    return r.json()


def test_dashboard_reconciles_with_the_worklist(lab):
    w, V = lab
    d = _get(w, "misbill", "/admin/dx/dashboard")
    assert d["funnel"] == {"registered": 2, "collected": 1, "in_transit": 0, "received": 1, "entered": 2,
                           "verified": 0, "approved": 0, "released": 1}
    # the bench's own counts, open visits of the last 7 days (the backdated one may be yesterday in IST)
    counts = _get(w, "tech", "/admin/dx/worklist", open_only=True)["counts"]
    released = _get(w, "tech", "/admin/dx/worklist")["counts"]["released"]
    assert d["funnel"]["registered"] == counts["pending_collection"] + counts["awaiting_payment"]
    assert d["funnel"]["received"] == counts["pending_entry"]
    assert d["funnel"]["entered"] == counts["pending_approval"] + counts["pending_verification"]
    assert d["funnel"]["collected"] + d["funnel"]["in_transit"] == counts["pending_receipt"]
    assert d["funnel"]["released"] == released
    a = d["alerts"]
    assert a["critical_open"] == 1 and a["tat_breaches"] == 1 and a["rejections_today"] == 1
    assert a["held_dues"] == {"visits": 1, "dues_paise": 35000}
    assert d["revenue"]["cash"] == 45000 and d["revenue"]["collected"] == 45000
    assert d["revenue"]["billed_paise"] == 45000 and d["revenue"]["dues_paise"] == 0
    assert d["sites"] and "phone" not in str(d)
    # a technician sees the funnel, never money
    t = _get(w, "tech", "/admin/dx/dashboard")
    assert t["funnel"] == d["funnel"] and "revenue" not in t
    # a past day: only that day's visits, none here
    y = _get(w, "tech", "/admin/dx/dashboard", date=(date.today() - timedelta(days=30)).isoformat())
    assert sum(y["funnel"].values()) == 0 and y["open_visits_included"] is False


def test_mis_tat_volumes_rejections_referrers(lab):
    w, V = lab
    rng = {"start_date": (date.today() - timedelta(days=1)).isoformat(),
           "end_date": (date.today() + timedelta(days=1)).isoformat()}
    tat = _get(w, "mis", "/admin/dx/mis/tat", **rng)
    by = {r["name"]: r for r in tat["tests"]}
    assert by["Lipid Profile"]["open_breached"] == 1 and by["Lipid Profile"]["breach_percent"] == 100.0
    cbc = by["CBC"]
    assert cbc["tests"] == 4 and cbc["finished"] == 1 and cbc["median_minutes"] == cbc["p90_minutes"] >= 0
    assert {d["name"] for d in tat["departments"]} == {"Bio"}
    vol = _get(w, "mis", "/admin/dx/mis/volumes", **rng)
    assert vol["totals"] == {"visits": 7, "tests": 7, "samples": 8, "rejected": 1}
    assert len(vol["days"]) == 3
    rej = _get(w, "mis", "/admin/dx/mis/rejections", **rng)
    assert rej["rejected"] == 1 and rej["collected"] == 6 and rej["rate_percent"] == 16.67
    assert rej["by_reason"][0]["id"] == "HEMOLYSED" and rej["by_site"][0]["rejected"] == 1
    refs = _get(w, "mis", "/admin/dx/mis/referrers", **rng)["referrers"]
    assert refs == [{"referrer_id": w["ref"]["id"], "name": "=HYPERLINK(evil)", "visits": 1, "tests": 1}]
    rev = _get(w, "misbill", "/admin/dx/mis/revenue", **rng)
    assert rev["totals"]["cash"] == rev["totals"]["collected"] == 45000 and rev["totals"]["b2b_credit"] == 0
    assert sum(d["billed_paise"] for d in rev["by_department"]) == 45000 + 55000 + 45000 + 4 * 35000
    assert rev["by_branch"][0]["collected"] == 45000


def test_csv_export_is_audited_and_formula_safe(lab, monkeypatch):
    from app.routers import dx as dx_router
    w, _ = lab
    audits = []

    async def record(**kw):
        audits.append(kw)
    monkeypatch.setattr(dx_router, "log_admin_action", record)
    rng = {"start_date": date.today().isoformat(), "end_date": date.today().isoformat()}
    r = _as(w, "mis").call("GET", "/admin/dx/mis/export.csv", w["clinic"], report="referrers", **rng)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines() == ["referrer,visits,tests", "'=HYPERLINK(evil),1,1"]
    assert "attachment" in r.headers["content-disposition"]
    visits = _as(w, "mis").call("GET", "/admin/dx/mis/export.csv", w["clinic"], report="visits", **rng).text
    assert visits.startswith("visit_no,date,source") and "LAB-" in visits and "Asha" not in visits
    assert [(a["action"], a["details"]["report"]) for a in audits] == [("dx_mis_export", "referrers"),
                                                                       ("dx_mis_export", "visits")]


def test_role_and_range_boundaries(lab):
    w, _ = lab
    c, today = w["clinic"], date.today()
    rng = {"start_date": today.isoformat(), "end_date": today.isoformat()}
    assert _as(w, "tech").call("GET", "/admin/dx/mis/tat", c, **rng).status_code == 403         # no MIS grant
    assert _as(w, "mis").call("GET", "/admin/dx/mis/revenue", c, **rng).status_code == 403      # MIS without billing
    assert _as(w, "mis").call("GET", "/admin/dx/mis/export.csv", c, report="revenue", **rng).status_code == 403
    assert _as(w, "nobody").call("GET", "/admin/dx/dashboard", c).status_code == 403
    assert "revenue" in _get(w, "mis", "/admin/dx/dashboard")                                  # MIS is a money view
    too_long = {"start_date": (today - timedelta(days=366)).isoformat(), "end_date": today.isoformat()}
    assert _as(w, "mis").call("GET", "/admin/dx/mis/volumes", c, **too_long).status_code == 422
    backwards = {"start_date": today.isoformat(), "end_date": (today - timedelta(days=1)).isoformat()}
    assert _as(w, "mis").call("GET", "/admin/dx/mis/tat", c, **backwards).status_code == 422
    assert _as(w, "mis").call("GET", "/admin/dx/mis/tat", c, start_date="2026-02-31",
                              end_date="2026-03-01").status_code == 422
    assert _as(w, "mis").call("GET", "/admin/dx/mis/export.csv", c, report="patients", **rng).status_code == 422
    assert _as(w, "mis").call("GET", "/admin/dx/dashboard", c, site_id=str(uuid.uuid4())).status_code == 404


def test_pure_helpers():
    from app.services import dx_mis
    assert dx_mis.percentile([], 90) is None
    assert dx_mis.percentile([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 90) == 90
    assert dx_mis.percentile([5], 50) == 5
    assert [dx_mis._cell(v) for v in ("=1+1", "+9", "-x", "@a", "ok", None, 3)] == ["'=1+1", "'+9", "'-x", "'@a",
                                                                                  "ok", "", "3"]
    o = lambda st, sample=None, verified=False: {"stage": st, "sample": sample, "result": {"verified": verified}}  # noqa
    assert dx_mis.funnel_key(o("awaiting_payment")) == "registered"
    assert dx_mis.funnel_key(o("pending_receipt", {"status": "in_transit"})) == "in_transit"
    assert dx_mis.funnel_key(o("pending_receipt", {"status": "collected"})) == "collected"
    assert dx_mis.funnel_key(o("pending_approval", verified=True)) == "verified"
    assert dx_mis.funnel_key(o("pending_approval")) == "entered"
    assert dx_mis.funnel_key(o("cancelled")) is None
    with pytest.raises(Exception):
        dx_mis.check_range(date(2026, 1, 1), date(2027, 1, 2))
    dx_mis.check_range(date(2026, 1, 1), date(2026, 12, 31))
