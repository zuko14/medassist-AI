"""/admin/sp route matrix (lane SP): the route set is pinned; every route refuses a clinic without Specialty OS (403,
or FastAPI request validation first) BEFORE any SP table is read; pack routes refuse a clinic without that pack; the
role matrix (SP_CLINICAL / SP_DESK / SP_ADMIN derived from the OPD duties) holds; a branch-less super admin without a
clinic never reaches a handler.
"""

import uuid

import pytest

from app.main import app
from tests.sp_world import sp_world
from tests.test_ipd_api_e2e import _clinic, api  # noqa: F401  (fixture)
from tests.test_rx_zero_impact import spy  # noqa: F401  (fixture)

SP_ROUTES = {
    ("POST", "/admin/sp/anc/pregnancies"),
    ("POST", "/admin/sp/anc/pregnancies/{pregnancy_id}/delivery"),
    ("GET", "/admin/sp/anc/pregnancies/{pregnancy_id}/timeline"),
    ("POST", "/admin/sp/anc/pregnancies/{pregnancy_id}/visits"),
    ("POST", "/admin/sp/camp-registrations/{registration_id}/screen"),
    ("GET", "/admin/sp/camps"),
    ("POST", "/admin/sp/camps"),
    ("POST", "/admin/sp/camps/{camp_id}/registrations"),
    ("GET", "/admin/sp/camps/{camp_id}/report"),
    ("GET", "/admin/sp/care-packs"),
    ("GET", "/admin/sp/compliance"),
    ("POST", "/admin/sp/compliance"),
    ("PATCH", "/admin/sp/compliance/{item_id}"),
    ("POST", "/admin/sp/consents"),
    ("GET", "/admin/sp/dental/lab-orders"),
    ("POST", "/admin/sp/dental/lab-orders"),
    ("POST", "/admin/sp/dental/lab-orders/{order_id}/move"),
    ("GET", "/admin/sp/encounters/{encounter_id}/forms"),
    ("DELETE", "/admin/sp/encounters/{encounter_id}/forms/{form_code}"),
    ("PUT", "/admin/sp/encounters/{encounter_id}/forms/{form_code}"),
    ("GET", "/admin/sp/encounters/{encounter_id}/imaging"),
    ("POST", "/admin/sp/encounters/{encounter_id}/imaging"),
    ("GET", "/admin/sp/estimates"),
    ("POST", "/admin/sp/estimates"),
    ("POST", "/admin/sp/estimates/{estimate_id}/convert"),
    ("POST", "/admin/sp/estimates/{estimate_id}/convert-ipd"),
    ("POST", "/admin/sp/estimates/{estimate_id}/respond"),
    ("POST", "/admin/sp/estimates/{estimate_id}/send"),
    ("GET", "/admin/sp/forms/{form_id}/spectacle-rx.pdf"),
    ("GET", "/admin/sp/immunisation"),
    ("POST", "/admin/sp/immunisation/doses"),
    ("GET", "/admin/sp/instalments/due"),
    ("GET", "/admin/sp/invoices/{invoice_id}/instalments"),
    ("PUT", "/admin/sp/invoices/{invoice_id}/instalments"),
    ("GET", "/admin/sp/ivf/art-register.csv"),
    ("GET", "/admin/sp/ivf/couples"),
    ("POST", "/admin/sp/ivf/couples"),
    ("POST", "/admin/sp/ivf/cycles"),
    ("GET", "/admin/sp/ivf/cycles/{cycle_id}"),
    ("PUT", "/admin/sp/ivf/cycles/{cycle_id}/days"),
    ("POST", "/admin/sp/ivf/cycles/{cycle_id}/embryos"),
    ("POST", "/admin/sp/ivf/cycles/{cycle_id}/release"),
    ("POST", "/admin/sp/ivf/cycles/{cycle_id}/trigger"),
    ("POST", "/admin/sp/ivf/cycles/{cycle_id}/trigger/ack"),
    ("POST", "/admin/sp/ivf/cycles/{cycle_id}/trigger/send"),
    ("POST", "/admin/sp/ivf/cycles/{cycle_id}/witness"),
    ("GET", "/admin/sp/ivf/witness/verify"),
    ("POST", "/admin/sp/media/consents"),
    ("POST", "/admin/sp/media/consents/{consent_id}/withdraw"),
    ("POST", "/admin/sp/media/{media_id}/delete"),
    ("POST", "/admin/sp/media/{media_id}/share"),
    ("GET", "/admin/sp/optical/board"),
    ("GET", "/admin/sp/optical/items"),
    ("POST", "/admin/sp/optical/items"),
    ("GET", "/admin/sp/optical/items/{item_id}/moves"),
    ("POST", "/admin/sp/optical/items/{item_id}/stock"),
    ("POST", "/admin/sp/optical/jobs"),
    ("POST", "/admin/sp/optical/jobs/{job_id}/move"),
    ("POST", "/admin/sp/package-consumptions/{consumption_id}/reverse"),
    ("POST", "/admin/sp/package-sales"),
    ("POST", "/admin/sp/package-sales/{sale_id}/consume"),
    ("POST", "/admin/sp/package-sales/{sale_id}/whatsapp-balance"),
    ("GET", "/admin/sp/packages"),
    ("POST", "/admin/sp/packages"),
    ("PATCH", "/admin/sp/packages/{package_id}"),
    ("GET", "/admin/sp/patients/{patient_id}/forms/{form_code}"),
    ("GET", "/admin/sp/patients/{patient_id}/media"),
    ("POST", "/admin/sp/patients/{patient_id}/media"),
    ("GET", "/admin/sp/patients/{patient_id}/media/compare"),
    ("GET", "/admin/sp/patients/{patient_id}/package-sales"),
    ("GET", "/admin/sp/patients/{patient_id}/pregnancies"),
    ("GET", "/admin/sp/status"),
    ("GET", "/admin/sp/wc/birth-registrations"),
    ("GET", "/admin/sp/wc/gov-ids"),
    ("PUT", "/admin/sp/wc/gov-ids"),
    ("GET", "/admin/sp/wc/growth"),
    ("POST", "/admin/sp/wc/growth"),
    ("POST", "/admin/sp/wc/newborns/{newborn_id}/registration"),
    ("GET", "/admin/sp/wc/nicu/{admission_id}"),
    ("POST", "/admin/sp/wc/nicu/{admission_id}/entries"),
    ("GET", "/admin/sp/wc/rch.csv"),
    ("GET", "/admin/sp/wc/uwin.csv"),
}
SP_TABLE_PREFIXES = ("sp_", "opd_encounter_forms", "ivf_", "obs_", "imm_")


def _app_sp_routes():
    flat = []
    for r in app.routes:
        flat.extend(getattr(getattr(r, "original_router", None), "routes", None) or [r])
    return {(m, r.path) for r in flat if getattr(r, "path", "").startswith("/admin/sp")
            for m in (getattr(r, "methods", None) or set()) if m not in ("HEAD", "OPTIONS")}


def _sp(paths):
    return [p for p in paths if p.rsplit("/", 1)[-1].startswith(SP_TABLE_PREFIXES)]


def _concrete(path):
    out = path.replace("{form_code}", "eye_exam_v1")
    for part in [p.strip("{}") for p in out.split("/") if p.startswith("{")]:
        out = out.replace("{" + part + "}", str(uuid.uuid4()))
    return out


def test_route_set_is_pinned():
    assert _app_sp_routes() == SP_ROUTES


def test_every_sp_route_refuses_a_clinic_without_the_module_before_any_sp_read(api, spy):  # noqa: F811
    clinic = _clinic(api.conn)                                   # OPD on, polyclinic, Specialty OS never enabled
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    refused = by_body = 0
    for method, path in sorted(SP_ROUTES):
        before = len(spy)
        r = api.call(method, _concrete(path), clinic, {} if method in ("POST", "PATCH", "PUT") else None)
        assert r.status_code in (403, 422), (method, path, r.status_code, r.text)
        assert not _sp(spy[before:]), (method, path, _sp(spy[before:]))
        if r.status_code == 422:
            assert isinstance(r.json()["detail"], list), (method, path)
            by_body += 1
        else:
            assert "not enabled" in r.text, (method, path, r.text)
            refused += 1
    assert refused + by_body == len(SP_ROUTES) and refused >= 30          # every GET and most writes hit the gate


PACK_ROUTES = {"eye": ("GET", "/admin/sp/optical/board"), "dental": ("GET", "/admin/sp/dental/lab-orders"),
               "ivf": ("GET", "/admin/sp/ivf/couples")}


@pytest.mark.parametrize("pack", sorted(PACK_ROUTES))
def test_pack_routes_need_their_pack(api, pack):  # noqa: F811
    w = sp_world(api, plan="multispecialty", packs=())                      # shared blocks on, no pack
    api.as_(w.u.admin)
    method, path = PACK_ROUTES[pack]
    r = api.call(method, path, w.clinic)
    assert r.status_code == 403 and "specialty module" in r.text
    assert api.call("GET", "/admin/sp/status", w.clinic).json()["packs"][pack] is False


# role → (forms: SP_CLINICAL, packages list: SP_DESK, package create: SP_ADMIN)
ROLES = {
    "clinic_admin": (200, 200, 201), "DOCTOR": (200, 200, 403), "NURSE": (200, 200, 403), "FRONT_DESK": (403, 403, 403),
    "CASHIER": (403, 403, 403), "PHARMACIST": (403, 403, 403), "LAB_TECH": (403, 403, 403),
}
PRESET = {"FRONT_DESK": ["OPD_FRONT_DESK"], "CASHIER": ["OPD_BILLING"]}      # what ROLE_PRESETS give these logins


@pytest.mark.parametrize("role", sorted(ROLES))
def test_role_matrix(api, role):  # noqa: F811
    from tests.sp_world import encounter
    w = sp_world(api)
    enc, _ = encounter(w)
    api.as_(w.u.admin)
    assert api.call("PUT", "/admin/opd/setup/settings", w.clinic, {"service_catalog": [
        {"code": "LASIK", "name": "LASIK", "item_type": "procedure", "price_paise": 100}]}).status_code == 200
    user = api.U("admin", "clinic_admin", w.clinic, str(uuid.uuid4())) if role == "clinic_admin" else \
        api.U(role.lower(), "staff", w.clinic, str(uuid.uuid4()), [], staff_role=role)
    api.as_(user)
    form = api.call("PUT", f"/admin/sp/encounters/{enc}/forms/eye_exam_v1", w.clinic, {"data": {}}).status_code
    lst = api.call("GET", "/admin/sp/packages", w.clinic).status_code
    new = api.call("POST", "/admin/sp/packages", w.clinic, {"code": "L" + uuid.uuid4().hex[:5].upper(), "name": "LASIK",
                                                            "specialty": "eye", "service_code": "LASIK",
                                                            "sessions_total": 1}).status_code
    assert (form, lst, new) == ROLES[role]


@pytest.mark.parametrize("role", sorted(PRESET))
def test_desk_presets_confer_sp_desk(api, role):  # noqa: F811
    w = sp_world(api)
    api.as_(api.U(role.lower(), "staff", w.clinic, str(uuid.uuid4()), PRESET[role], staff_role=role))
    assert api.call("GET", "/admin/sp/packages", w.clinic).status_code == 200
    assert api.call("GET", "/admin/sp/estimates", w.clinic).status_code == 200


def test_opd_grant_alone_confers_the_sp_action(api):  # noqa: F811
    w = sp_world(api)
    api.as_(api.U("x", "staff", w.clinic, str(uuid.uuid4()), ["OPD_ADMIN"], staff_role="LAB_TECH"))
    assert api.call("GET", "/admin/sp/compliance", w.clinic).status_code == 403          # OPD_ADMIN is not SP_DESK
    assert api.call("POST", "/admin/sp/compliance", w.clinic, {"kind": "fire_noc", "label": "Fire NOC",
                                                               "valid_until": "2027-01-01"}).status_code == 201
