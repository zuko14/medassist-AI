"""S1 clinical media vault (SP-S1-01) on real Postgres (storage mocked: the bucket is Supabase Storage).

No photo without a live photo consent; EXIF (GPS / device) is stripped before storage; the path is clinic- and
patient-scoped (DB CHECK); list / compare return short-lived signed URLs only; sharing on WhatsApp needs an explicit
staff action AND a consent that allows sharing AND an opted-in patient; delete hides the row and removes the file;
DPDP erasure removes the person's photos, their consents and the files (sp_purge_patient); access is audited.
"""

import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from tests.sp_world import sp_world
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (40, 30), (200, 120, 90))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"                                         # Make (device)
    exif[0x0132] = "2026:11:25 10:00:00"                                # DateTime
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


@pytest.fixture
def vault(api, monkeypatch):  # noqa: F811
    from app.services import sp
    real = sp.supabase
    bucket = MagicMock()
    bucket.create_signed_urls.side_effect = lambda paths, secs: [{"path": p, "signedURL": f"https://s/{p}?t=1"} for p in paths]
    storage = MagicMock()
    storage.from_.return_value = bucket
    monkeypatch.setattr(sp, "supabase", SimpleNamespace(table=real.table, rpc=real.rpc, storage=storage))
    w = sp_world(api, plan="derma", packs=("derma",))
    return SimpleNamespace(w=w, bucket=bucket)


def _upload(api, w, data, **form):
    return api.client.post(f"/admin/sp/patients/{w.patient}/media", params={"clinic_id": w.clinic},
                           files={"file": ("p.jpg", data, "image/jpeg")}, data={"context": "derma", **form})


def test_consent_first_then_upload_strips_exif(api, vault):  # noqa: F811
    w, bucket = vault.w, vault.bucket
    api.as_(w.u.doctor)
    r = _upload(api, w, _jpeg_with_gps())
    assert r.status_code == 409 and "consent" in r.text
    c = api.call("POST", "/admin/sp/media/consents", w.clinic, {"patient_id": w.patient, "signer_name": "Lakshmi Devi"})
    assert c.status_code == 201, c.text
    assert c.json()["allow_share"] is False
    r = _upload(api, w, _jpeg_with_gps(), angle="left_45", session_no="2", body_site="Left cheek")
    assert r.status_code == 201, r.text
    path, stored, _ = bucket.upload.call_args.args
    assert path.startswith(f"{w.clinic}/clinical-media/{w.patient}/") and path.endswith(".jpg")
    assert b"PhoneMaker" not in stored and not Image.open(io.BytesIO(stored)).getexif()
    assert "storage_path" not in r.json() and r.json()["sha256"]
    assert _upload(api, w, b"not an image").status_code == 415
    api.as_(w.u.desk)
    assert _upload(api, w, _jpeg_with_gps()).status_code == 403                     # front desk does not take photos


def test_list_compare_share_delete_and_erasure(api, vault, monkeypatch):  # noqa: F811
    from app.services.whatsapp import whatsapp_service
    w, bucket = vault.w, vault.bucket
    api.as_(w.u.doctor)
    api.call("POST", "/admin/sp/media/consents", w.clinic, {"patient_id": w.patient, "signer_name": "Lakshmi Devi"})
    a = _upload(api, w, _jpeg_with_gps(), session_no="1").json()["id"]
    b = _upload(api, w, _jpeg_with_gps(), session_no="3").json()["id"]
    listed = api.call("GET", f"/admin/sp/patients/{w.patient}/media", w.clinic).json()["media"]
    assert len(listed) == 2 and all(m["url"].startswith("https://s/") and "storage_path" not in m for m in listed)
    cmp = api.call("GET", f"/admin/sp/patients/{w.patient}/media/compare", w.clinic, before=b, after=a).json()
    assert cmp["before"]["id"] == a and cmp["after"]["id"] == b                       # oldest first whatever the order
    assert api.call("GET", f"/admin/sp/patients/{w.patient}/media/compare", w.clinic, before=a,
                    after=str(uuid.uuid4())).status_code == 404
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(whatsapp_service, "send_document", send)
    r = api.call("POST", f"/admin/sp/media/{a}/share", w.clinic)
    assert r.status_code == 409 and "does not allow sharing" in r.text and not send.await_count
    api.call("POST", "/admin/sp/media/consents", w.clinic, {"patient_id": w.patient, "signer_name": "Lakshmi Devi",
                                                            "allow_share": True})
    c = _upload(api, w, _jpeg_with_gps()).json()["id"]                                # under the sharing consent
    _q(w.conn, "UPDATE patients SET opted_in = false WHERE id = %s", w.patient)
    assert api.call("POST", f"/admin/sp/media/{c}/share", w.clinic).status_code == 409   # not opted in
    _q(w.conn, "UPDATE patients SET opted_in = true WHERE id = %s", w.patient)
    r = api.call("POST", f"/admin/sp/media/{c}/share", w.clinic)
    assert r.status_code == 200, r.text
    assert send.await_count == 1
    assert _q(w.conn, "SELECT shared_by_name IS NOT NULL FROM sp_media WHERE id = %s", c)
    assert api.call("POST", f"/admin/sp/media/{b}/delete", w.clinic, {"reason": "blurred"}).status_code == 200
    assert bucket.remove.call_count == 1
    assert len(api.call("GET", f"/admin/sp/patients/{w.patient}/media", w.clinic).json()["media"]) == 2
    assert _q(w.conn, "SELECT count(*) FROM admin_audit_logs WHERE action = 'SP_MEDIA_VIEW' AND resource_id = %s",
              w.patient) >= 1
    # DPDP erasure: every photo row + consent of the person goes, and the files are removed
    import asyncio

    from app.services import sp
    out = asyncio.run(sp.erase_patient(w.clinic, w.patient))
    assert out["media"] == 3 and out["media_consents"] == 2 and "paths" not in out
    assert len(bucket.remove.call_args.args[0]) == 3
    assert _q(w.conn, "SELECT count(*) FROM sp_media WHERE patient_id = %s", w.patient) == 0


def test_withdrawn_consent_blocks_new_photos(api, vault):  # noqa: F811
    w = vault.w
    api.as_(w.u.nurse)
    cid = api.call("POST", "/admin/sp/media/consents", w.clinic, {"patient_id": w.patient, "signer_name": "L D"}).json()["id"]
    assert api.call("POST", f"/admin/sp/media/consents/{cid}/withdraw", w.clinic, {"reason": "patient asked"}).status_code == 200
    assert _upload(api, w, _jpeg_with_gps()).status_code == 409
