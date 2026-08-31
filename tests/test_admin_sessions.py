"""Server-side admin sessions: creation, resolution, expiry, revocation (AUDIT-P1-2).

HTTP Basic has no server-side state, so a credential in someone's hands could
not be cut off short of rotating the password for everyone. These tests pin the
properties that fix is supposed to provide.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers.admin import (
    ADMIN_SESSION_COOKIE,
    AdminUser,
    _hash_session_token,
    create_admin_session,
    resolve_admin_session,
    verify_credentials,
)


def _session_row(**overrides):
    row = {
        "token_hash": "irrelevant",
        "username": "drpatel",
        "role": "clinic_admin",
        "clinic_id": "clinic-1",
        "user_id": "user-1",
        "branch_id": None,
        "staff_role": None,
        "permissions": ["APPOINTMENTS_VIEW"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
        "revoked_at": None,
    }
    row.update(overrides)
    return row


def _select_returning(rows):
    """Mock supabase for table(..).select(..).eq(..).is_(..).execute()."""
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.execute.return_value = MagicMock(
        data=rows
    )
    return mock_sb


def _request(cookies=None):
    req = MagicMock()
    req.cookies = cookies or {}
    req.client = MagicMock(host="203.0.113.9")
    req.headers = {"user-agent": "pytest"}
    return req


def test_token_hash_is_not_the_token():
    """Only the hash is stored, so a leaked backup yields no usable token."""
    token = "s3cret-token"
    h = _hash_session_token(token)
    assert h != token
    assert len(h) == 64
    assert _hash_session_token(token) == h


@pytest.mark.asyncio
async def test_create_session_stores_only_the_hash():
    user = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "sess-1"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        token = await create_admin_session(user, "203.0.113.9", "pytest")

    assert token
    stored = mock_sb.table.return_value.insert.call_args[0][0]
    assert stored["token_hash"] == _hash_session_token(token)
    assert token not in str(stored), "raw token must never be persisted"
    assert stored["username"] == "drpatel"


@pytest.mark.asyncio
async def test_create_session_returns_none_when_table_missing():
    """Migration 067 not applied must degrade to HTTP Basic, not lock admins out."""
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception(
        'relation "admin_sessions" does not exist'
    )
    user = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")

    with patch("app.routers.admin.supabase", mock_sb):
        assert await create_admin_session(user, "ip", "ua") is None


@pytest.mark.asyncio
async def test_resolve_live_session_returns_user():
    with patch("app.routers.admin.supabase", _select_returning([_session_row()])):
        user = await resolve_admin_session("tok")
    assert user is not None
    assert user.username == "drpatel"
    assert user.clinic_id == "clinic-1"
    assert user.permissions == ["APPOINTMENTS_VIEW"]


@pytest.mark.asyncio
async def test_resolve_expired_session_returns_none():
    expired = _session_row(
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    )
    with patch("app.routers.admin.supabase", _select_returning([expired])):
        assert await resolve_admin_session("tok") is None


@pytest.mark.asyncio
async def test_resolve_revoked_session_returns_none():
    """Revoked rows are filtered by the query itself, so nothing comes back."""
    with patch("app.routers.admin.supabase", _select_returning([])):
        assert await resolve_admin_session("tok") is None


@pytest.mark.asyncio
async def test_dead_cookie_does_not_fall_back_to_basic():
    """The revocation guarantee: a killed session must not silently re-auth.

    If verify_credentials fell through to HTTP Basic here, revoking a session
    would achieve nothing for a browser that still holds the password.
    """
    from fastapi import HTTPException

    with patch(
        "app.routers.admin.resolve_admin_session", new_callable=AsyncMock, return_value=None
    ):
        with pytest.raises(HTTPException) as exc:
            await verify_credentials(
                request=_request({ADMIN_SESSION_COOKIE: "revoked-token"}),
                credentials=None,
            )
    assert exc.value.status_code == 401
    assert "sign in again" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_valid_cookie_authenticates_without_credentials():
    session_user = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    with patch(
        "app.routers.admin.resolve_admin_session",
        new_callable=AsyncMock,
        return_value=session_user,
    ):
        got = await verify_credentials(
            request=_request({ADMIN_SESSION_COOKIE: "good-token"}), credentials=None
        )
    assert got.username == "drpatel"


@pytest.mark.asyncio
async def test_no_cookie_and_no_credentials_is_401():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await verify_credentials(request=_request(), credentials=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_basic_auth_still_works_without_a_cookie():
    """Existing API clients and scripts must keep authenticating."""
    from fastapi.security import HTTPBasicCredentials

    expected = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    with patch(
        "app.routers.admin._authenticate_password",
        new_callable=AsyncMock,
        return_value=expected,
    ) as auth_pw:
        got = await verify_credentials(
            request=_request(),
            credentials=HTTPBasicCredentials(username="drpatel", password="pw"),
        )
    assert got.username == "drpatel"
    auth_pw.assert_awaited_once()
