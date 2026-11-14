"""Report templates (DX-44): starters, CRUD through the routes (real Postgres), publish / retire /
immutability, preview with the sample visit, assets, branch-pinned reach, and the full release-time
cascade (visit override -> white-label account -> branch -> clinic -> built-in) in SQL.
"""

import io
import json
import uuid

import pdfplumber
import pytest

from app.services import dx_templates
from tests.test_dx_visits_e2e import api, dxw  # noqa: F401  (fixtures)
from tests.test_migration_dx_m3 import _q, lab  # noqa: F401  (fixture)
from tests.test_migration_dx_m6 import m6  # noqa: F401  (fixture)
from tests.test_migration_dx_m7 import _account, _release, _set_visit, _signed, m7  # noqa: F401  (fixture)



def _png(w=200, h=60) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (240, 140, 40)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def tw(dxw, monkeypatch):  # noqa: F811
    store = {}

    class _Bucket:
        def upload(self, path, data, opts):
            store[path] = data

        def download(self, path):
            return store[path]

    class _Storage:
        def from_(self, bucket):
            return _Bucket()
    monkeypatch.setattr(type(dx_templates.supabase), "storage", property(lambda self: _Storage()), raising=False)
    U, c = dxw["api"].U, dxw["clinic"]
    other = str(_q(dxw["c"], "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Kukatpally') RETURNING id", c))
    dxw["u"]["pinned"] = U("bradmin", "staff", c, str(uuid.uuid4()), ["DX_ADMIN"], staff_role="PROCESSING_ADMIN",
                           branch_id=other)
    dxw.update(store=store, other_branch=other)
    return dxw


def _as(w, who):
    return w["api"].as_(w["u"][who])


def test_starters_validate():
    for name in dx_templates.STARTERS:
        layout = dx_templates.starter_layout(name)
        assert layout["schema"] == 1 and layout["verification"]["qr"] is True
    assert dx_templates.starter_layout("minimal")["signatures"]["placement"] == "end"
    assert dx_templates.snapshot_layout({"source": "builtin", "layout": None})["header"]["composed"]["band"] is True


def test_template_crud_publish_retire_and_preview(tw):
    c = tw["clinic"]
    assert _as(tw, "tech").call("GET", "/admin/dx/templates", c).status_code == 403
    admin = _as(tw, "admin")
    r = admin.call("POST", "/admin/dx/templates", c, {"name": "Main letterhead", "starter": "classic_band",
                                                      "layout": {"header": {"composed": {"accent_hex": "#2E86AB"}}}})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["status"] == "draft" and t["version"] == 1 and t["layout"]["header"]["composed"]["accent_hex"] == "#2E86AB"
    bad = admin.call("POST", "/admin/dx/templates", c, {"name": "Bad", "layout": {"disclaimer": "<script>x</script>"}})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "dx_template_invalid"
    assert admin.call("POST", "/admin/dx/templates", c, {"name": "Bad", "layout": {"tested_at": False}}).status_code == 422
    upd = admin.call("PUT", f"/admin/dx/templates/{t['id']}", c, {"layout": {"body": {"font_pt": 10}}})
    assert upd.status_code == 200 and upd.json()["layout"]["body"]["font_pt"] == 10
    pv = admin.client.post(f"/admin/dx/templates/{t['id']}/preview", params={"clinic_id": c})
    assert pv.status_code == 200 and pv.content[:5] == b"%PDF-" and pv.headers["cache-control"] == "no-store"
    with pdfplumber.open(io.BytesIO(pv.content)) as doc:
        text = doc.pages[0].extract_text()
    assert "Mrs. Lakshmi Devi" in text and "GLYCOSYLATED HAEMOGLOBIN" in text and "Desk Lab" in text
    pub = admin.call("POST", f"/admin/dx/templates/{t['id']}/publish", c)
    assert pub.status_code == 200 and pub.json()["status"] == "published"
    locked = admin.call("PUT", f"/admin/dx/templates/{t['id']}", c, {"layout": {}})
    assert locked.status_code == 409 and locked.json()["detail"]["code"] == "dx_template_published"
    assert admin.call("POST", f"/admin/dx/templates/{t['id']}/publish", c).status_code == 409
    # an edit is a new version of the same name
    v2 = admin.call("POST", "/admin/dx/templates", c, {"name": "Main letterhead", "starter": "minimal"})
    assert v2.status_code == 201 and v2.json()["version"] == 2
    assert admin.call("POST", f"/admin/dx/templates/{t['id']}/retire", c).json()["status"] == "retired"
    assert admin.call("POST", f"/admin/dx/templates/{v2.json()['id']}/retire", c).status_code == 409   # a draft
    listed = admin.call("GET", "/admin/dx/templates", c).json()
    assert [x["version"] for x in listed["templates"]] == [2, 1] and set(listed["starters"]) == set(dx_templates.STARTERS)
    assert admin.call("GET", f"/admin/dx/templates/{uuid.uuid4()}", c).status_code == 404


def test_assets_are_content_addressed_under_the_clinic(tw):
    c = tw["clinic"]
    api = _as(tw, "admin")
    raw = _png()
    up = api.client.post("/admin/dx/templates/assets", params={"clinic_id": c}, files={"file": ("logo.png", raw)})
    assert up.status_code == 201, up.text
    path = up.json()["path"]
    assert path.startswith(f"{c}/dx-assets/templates/") and path.endswith(".png") and tw["store"][path] == raw
    assert api.client.post("/admin/dx/templates/assets", params={"clinic_id": c},
                           files={"file": ("x.gif", b"GIF89a....")}).status_code == 415
    assert api.client.post("/admin/dx/templates/assets", params={"clinic_id": c},
                           files={"file": ("wide.png", _png(3200, 10))}).status_code == 422
    t = api.call("POST", "/admin/dx/templates", c, {"name": "Logo", "layout": {"header": {"composed": {"logo_asset": path}}}})
    assert t.status_code == 201, t.text
    pv = api.client.post(f"/admin/dx/templates/{t.json()['id']}/preview", params={"clinic_id": c})
    with pdfplumber.open(io.BytesIO(pv.content)) as doc:
        assert len(doc.pages[0].images) == 1
    foreign = f"{uuid.uuid4()}/dx-assets/templates/{'a' * 64}.png"
    assert api.call("POST", "/admin/dx/templates", c, {"name": "Foreign", "layout": {
        "header": {"composed": {"logo_asset": foreign}}}}).status_code == 422


def test_branch_pinned_admin_manages_only_its_branch(tw):
    c = tw["clinic"]
    pinned = _as(tw, "pinned")
    assert pinned.call("POST", "/admin/dx/templates", c, {"name": "Clinic wide"}).status_code == 403
    assert pinned.call("POST", "/admin/dx/templates", c, {"name": "Other", "owner_kind": "branch",
                                                          "owner_id": tw["branch"]}).status_code == 403
    own = pinned.call("POST", "/admin/dx/templates", c, {"name": "Kukatpally", "owner_kind": "branch",
                                                         "owner_id": tw["other_branch"]})
    assert own.status_code == 201, own.text
    other = _as(tw, "admin").call("POST", "/admin/dx/templates", c, {"name": "Main", "owner_kind": "branch",
                                                                     "owner_id": tw["branch"]}).json()
    pinned = _as(tw, "pinned")
    assert pinned.call("GET", f"/admin/dx/templates/{other['id']}", c).status_code == 404
    names = {t["name"] for t in pinned.call("GET", "/admin/dx/templates", c).json()["templates"]}
    assert names == {"Kukatpally"}
    assert _as(tw, "admin").call("POST", "/admin/dx/templates", c, {"name": "Ghost", "owner_kind": "branch",
                                                                    "owner_id": str(uuid.uuid4())}).status_code == 404


def test_full_cascade_order(m7):  # noqa: F811
    """visit override -> white-label account template -> branch -> clinic -> built-in (ADR-DX-08)."""
    c, clinic = m7["c"], m7["clinic"]

    def tpl(kind, owner, name):
        t = str(_q(c, "INSERT INTO dx_report_templates (clinic_id, owner_kind, owner_id, name, layout) VALUES "
                      "(%s, %s, %s, %s, '{}') RETURNING id", clinic, kind, owner, name))
        _q(c, "SELECT dx_publish_template(%s, %s, NULL)", clinic, t)
        return t

    acc = _account(m7, name="City Hospital", kind="hospital", report_delivery="both", white_label=True)
    tpl("clinic", None, "Clinic")
    branch_t = tpl("branch", m7["branch"], "Branch")
    acc_t = tpl("b2b_account", acc, "City Hospital letterhead")
    visit_t = tpl("clinic", None, "One-off")
    sources = []
    for test, step in (("CBC", None), ("Lipid Profile", "account"), ("CBC", "override")):
        if step == "account":
            _set_visit(m7, b2b_account_id=acc, billing_mode="internal")
        if step == "override":
            _set_visit(m7, template_override_id=visit_t)
        if test == "CBC" and step == "override":
            amended = _q(c, "SELECT dx_record_results(%s, %s, %s::jsonb, '[]', false, 'transcription error', %s, 'tech')",
                         clinic, m7["orders"]["CBC"]["id"],
                         json.dumps([{"parameter_id": m7["param"]["cbc"], "parameter_name": "CBC", "numeric_value": "15",
                                      "flag": "normal"}]), m7["tech"])
            from tests.test_migration_dx_m6 import _approve
            _approve(m7, [amended])
        else:
            _signed(m7, test, "14")
        snap = _release(m7)["template_snapshot"]
        sources.append((snap["source"], snap["template_id"]))
    assert sources == [("branch", branch_t), ("account", acc_t), ("visit", visit_t)]
    # retired override and account templates fall through to the branch
    _q(c, "UPDATE dx_report_templates SET status = 'retired' WHERE id IN (%s, %s)", acc_t, visit_t)
    from tests.test_migration_dx_m6 import _approve
    again = _q(c, "SELECT dx_record_results(%s, %s, %s::jsonb, '[]', false, 'recalculated value', %s, 'tech')",
               clinic, m7["orders"]["Lipid Profile"]["id"],
               json.dumps([{"parameter_id": m7["param"]["lipid"], "parameter_name": "Lipid Profile",
                            "numeric_value": "15", "flag": "normal"}]), m7["tech"])
    _approve(m7, [again])
    snap = _release(m7)["template_snapshot"]
    assert (snap["source"], snap["template_id"]) == ("branch", branch_t)
