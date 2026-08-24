"""Meta reports credential failures as HTTP 500 + OAuthException code 1.

Those must be terminal, not retried — retrying burnt ~15s per message and
kept a broken token invisible for hours.
"""

import httpx
import pytest

from app.services.whatsapp import MetaAuthError, WhatsAppService, _is_auth_error

AUTH_500 = {
    "error": {
        "message": "An unknown error has occurred.",
        "code": 1,
        "type": "OAuthException",
        "fbtrace_id": "A4D0mQVxGXNL5BFlU2LH-GN",
    }
}
TRANSIENT_500 = {"error": {"message": "Service temporarily unavailable", "code": 2, "type": "HttpException"}}


def test_classifier():
    assert _is_auth_error(AUTH_500["error"])
    assert _is_auth_error({"code": 190})
    assert not _is_auth_error(TRANSIENT_500["error"])
    assert not _is_auth_error({})


@pytest.mark.asyncio
async def test_make_request_does_not_retry_auth_error(monkeypatch):
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            calls.append(url)
            return httpx.Response(500, json=AUTH_500, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    clinic = {"id": "c1", "config": {"meta_access_token": "t", "meta_phone_number_id": "p"}}

    with pytest.raises(MetaAuthError):
        await WhatsAppService()._make_request(clinic, "messages", {"type": "text"})
    assert len(calls) == 1, "auth errors must not be retried"


@pytest.mark.asyncio
async def test_upload_media_does_not_retry_auth_error(monkeypatch):
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            calls.append(url)
            return httpx.Response(500, json=AUTH_500, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    clinic = {"id": "c1", "config": {"meta_access_token": "t", "meta_phone_number_id": "p"}}

    with pytest.raises(MetaAuthError):
        await WhatsAppService().upload_media(clinic, b"%PDF-1.4", "r.pdf", "application/pdf")
    assert len(calls) == 1, "auth errors must not be retried"
