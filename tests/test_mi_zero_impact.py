"""Data import zero impact (MI-10): OFF by default and never via the wildcard; every /admin/mi route refuses a clinic
without the opt-in before any mi_* read; the existing 088 patient import, its export and /admin/me send no mi_*
query; consent is never set."""

import uuid

from app.services.tenant import OPT_IN_FEATURES, has_feature, mi_access
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_mi_route_matrix import MI_ROUTES
from tests.test_rx_zero_impact import spy  # noqa: F401  (fixture)


def _mi(paths):
    return [p for p in paths if p.rsplit("/", 1)[-1].startswith("mi_")]


def test_off_by_default_and_wildcard_never_enables():
    assert "data_import" in OPT_IN_FEATURES
    ent = {"plan": "enterprise", "account_type": "tenant", "features": {}}
    assert has_feature(ent, "data_import") is True and mi_access(ent) is False
    assert mi_access({**ent, "features": {"data_import": True}}) is True
    assert mi_access({**ent, "features": {"data_import": True}, "account_type": "corporate_partner"}) is False
    assert mi_access(None) is False


def test_every_route_refuses_before_any_mi_read(api, spy):  # noqa: F811
    clinic = _clinic(api.conn)
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    for method, path in sorted(MI_ROUTES):
        concrete = path.replace("{kind}", "payers")
        for part in ("batch_id", "patient_id", "match_id", "due_id"):
            concrete = concrete.replace("{" + part + "}", str(uuid.uuid4()))
        before = len(spy)
        kw = {"params": {"clinic_id": clinic, "q": "abcd"}}
        if path.endswith("dry-run"):
            kw["files"] = {"file": ("x.csv", b"a,b\n1,2\n", "text/csv")}
        elif method in ("POST", "PUT"):
            kw["json"] = {"patient_record_id": str(uuid.uuid4()), "patient_id": str(uuid.uuid4()),
                          "status": "written_off", "legacy_text": "abc", "icd10_code": "I10"}
        r = api.client.request(method, concrete, **kw)
        assert r.status_code in (403, 422), (method, path, r.status_code, r.text)
        if r.status_code == 403:
            assert "not enabled" in r.text, (method, path)
        assert not _mi(spy[before:]), (method, path)


def test_existing_patient_import_and_me_send_no_mi_query(api, spy):  # noqa: F811
    clinic = _clinic(api.conn)
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    del spy[:]
    me = api.client.get("/admin/me", params={"clinic_id": clinic}).json()
    assert me["mi_access"] is False
    api.client.post("/admin/data/patient-records/import", params={"clinic_id": clinic},
                    files={"file": ("p.csv", b"Name,Phone\nAsha,9000000001\n", "text/csv")})
    assert not _mi(spy)
    assert _q(api.conn, "SELECT count(*) FROM patients WHERE clinic_id = %s", clinic) == 0
