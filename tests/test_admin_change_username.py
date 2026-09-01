"""Self-service username change (PUT /admin/change-username).

Clinic logins are auto-provisioned at onboarding as `<name-slug><6 hex>`
(app/routers/clinics.py) - e.g. "visakhamultispeciala3f9c1", which is correct
and unique but unusable by a human at a reception desk. This lets the account
rename itself.

A rename is a credential change, so it carries the same obligations as a
password change: prove possession of the current password, refuse to collide
with an existing or reserved name, and revoke every live session. The session
part is easy to get wrong - admin_sessions stores a SNAPSHOT of the username
and resolve_admin_session() rebuilds AdminUser from it, so a session left alive
across a rename keeps asserting the OLD identity in audit logs and would not be
found by a later revoke_sessions_for_user(new_name).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.admin import (
    AdminUser,
    ChangeUsernameRequest,
    change_username,
    hash_password,
)

UID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CLINIC = "11111111-1111-1111-1111-111111111111"
OLD = "visakhamultispeciala3f9c1"
PASSWORD = "correct-horse-battery"

USER = AdminUser(username=OLD, role="clinic_admin", clinic_id=CLINIC, user_id=UID)
ENV_USER = AdminUser(
    username="admin", role="super_admin", clinic_id=None, user_id="super_admin_env"
)


def _supabase(results, update_error=None):
    """Self-chaining Supabase mock returning `results` in call order."""
    queue = list(results)
    state = {"updating": False}
    chain = MagicMock()
    for m in ("select", "eq", "neq", "limit", "is_", "insert", "ilike"):
        getattr(chain, m).return_value = chain

    def _execute():
        if update_error is not None and state["updating"]:
            raise update_error
        return MagicMock(data=queue.pop(0) if queue else [])

    chain.execute.side_effect = _execute

    def _update(*a, **k):
        state["updating"] = True
        return chain

    chain.update.side_effect = _update

    sb_mock = MagicMock()
    sb_mock.table.return_value = chain
    return sb_mock


def _own_row(password=PASSWORD):
    return [{"id": UID, "username": OLD, "password_hash": hash_password(password)}]


async def _call(request_body, results, update_error=None):
    sb_mock = _supabase(results, update_error)
    revoke = AsyncMock()
    with patch("app.routers.admin.supabase", sb_mock), \
         patch("app.routers.admin.revoke_sessions_for_user", revoke), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock) as audit:
        result = await change_username(request_body, request=None, user=USER)
    return result, revoke, audit


# -- Input validation -------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "ab",           # too short
        "_leading",     # must start alphanumeric
        "trailing-",    # must end alphanumeric
        "has space",
        "has/slash",
        "has%percent",  # a LIKE wildcard must never reach a query
        "x" * 65,       # too long
        "",
    ],
)
def test_rejects_malformed_usernames(bad):
    with pytest.raises(ValidationError):
        ChangeUsernameRequest(current_password="x", new_username=bad)


@pytest.mark.parametrize("good", ["abc", "clinic_1", "st.marys", "a-b-c", "Visakha01"])
def test_accepts_reasonable_usernames(good):
    assert (
        ChangeUsernameRequest(current_password="x", new_username=good).new_username
        == good
    )


def test_surrounding_whitespace_is_trimmed():
    assert (
        ChangeUsernameRequest(
            current_password="x", new_username="  visakha  "
        ).new_username
        == "visakha"
    )


# -- Authorization ----------------------------------------------------------


@pytest.mark.asyncio
async def test_env_credential_account_cannot_rename_itself():
    """The env super_admin has no clinic_admins row to update."""
    body = ChangeUsernameRequest(current_password=PASSWORD, new_username="shortname")
    sb_mock = _supabase([])
    with patch("app.routers.admin.supabase", sb_mock):
        with pytest.raises(HTTPException) as exc:
            await change_username(body, request=None, user=ENV_USER)
    assert exc.value.status_code == 400
    sb_mock.table.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_current_password_is_refused():
    """Without this, a stolen session cookie could rename the account and lock
    the real owner out of their own panel."""
    body = ChangeUsernameRequest(current_password="wrong", new_username="shortname")
    with pytest.raises(HTTPException) as exc:
        await _call(body, [_own_row()])
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_password_does_not_revoke_sessions():
    body = ChangeUsernameRequest(current_password="wrong", new_username="shortname")
    sb_mock = _supabase([_own_row()])
    revoke = AsyncMock()
    with patch("app.routers.admin.supabase", sb_mock), \
         patch("app.routers.admin.revoke_sessions_for_user", revoke), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock):
        with pytest.raises(HTTPException):
            await change_username(body, request=None, user=USER)
    revoke.assert_not_awaited()


# -- Name collisions --------------------------------------------------------


@pytest.mark.asyncio
async def test_reserved_env_username_is_refused():
    """_authenticate_password checks the database BEFORE the env fallback, so a
    tenant row occupying the platform's username would sit in front of a
    platform credential on the login path."""
    from app.config import settings

    body = ChangeUsernameRequest(
        current_password=PASSWORD, new_username=settings.admin_username
    )
    with pytest.raises(HTTPException) as exc:
        await _call(body, [_own_row()])
    assert exc.value.status_code == 409
    assert "reserved" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_username_already_taken_is_refused():
    body = ChangeUsernameRequest(current_password=PASSWORD, new_username="taken")
    with pytest.raises(HTTPException) as exc:
        await _call(body, [_own_row(), [{"id": "someone-else"}]])
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_unique_violation_race_becomes_409_not_500():
    """The pre-check can lose a race with a concurrent rename; the UNIQUE index
    is the real arbiter and its error must surface as a usable message."""
    body = ChangeUsernameRequest(current_password=PASSWORD, new_username="racy")
    with pytest.raises(HTTPException) as exc:
        await _call(
            body,
            [_own_row(), []],
            update_error=Exception("duplicate key value violates unique constraint"),
        )
    assert exc.value.status_code == 409


# -- Happy path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_updates_row_and_revokes_the_old_name():
    body = ChangeUsernameRequest(current_password=PASSWORD, new_username="visakha")
    result, revoke, audit = await _call(
        body, [_own_row(), [], [{"id": UID, "username": "visakha"}]]
    )

    assert result["success"] is True
    assert result["username"] == "visakha"
    assert result["relogin_required"] is True

    # Sessions must be revoked for the OLD username: admin_sessions rows carry
    # the name as it was at login, so revoking the new one would match nothing.
    revoke.assert_awaited_once_with(OLD)

    audit.assert_awaited_once()
    assert audit.await_args.kwargs["details"] == {"from": OLD, "to": "visakha"}


@pytest.mark.asyncio
async def test_renaming_to_the_same_name_is_a_noop():
    """No row write, and crucially no session revocation - signing the user out
    for a change that did not happen would be its own bug."""
    body = ChangeUsernameRequest(current_password=PASSWORD, new_username=OLD)
    result, revoke, audit = await _call(body, [_own_row()])

    assert result["success"] is True
    assert result["relogin_required"] is False
    revoke.assert_not_awaited()
    audit.assert_not_awaited()
