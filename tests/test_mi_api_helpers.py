"""Shared helpers for the data-import route suites (not a test module of its own)."""

import uuid
from pathlib import Path

FX = Path(__file__).parent / "fixtures" / "mi"


def mi_clinic(api, opd=True):
    from tests.test_ipd_api_e2e import _clinic
    clinic = _clinic(api.conn, opd=opd)
    assert api.owner_toggle(clinic, "data_import", True).status_code == 200
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    return clinic


def upload(api, clinic, kind, content, name="f.csv", preset="generic"):
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    return api.client.post(f"/admin/mi/{kind}/dry-run", params={"clinic_id": clinic, "preset": preset},
                           files={"file": (name, data, "text/csv")})


def run(api, clinic, kind, content, preset="generic", **apply_body):
    """dry run + apply; returns (dry, applied)."""
    d = upload(api, clinic, kind, content, preset=preset)
    assert d.status_code == 200, d.text
    a = api.call("POST", f"/admin/mi/{kind}/apply/{d.json()['id']}", clinic, apply_body or None)
    return d.json(), a
