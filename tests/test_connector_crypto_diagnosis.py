"""Connector credential crypto — telling a bad KEY apart from a bad TOKEN.

Production incident 2026-08-29: the Report Connector dashboard showed
"Password decryption failed: ValueError" every 5 minutes with nothing else.
The exception class alone cannot tell an operator which of two mutually
exclusive remedies to apply:

  * bad key   -> stored credentials are INTACT; restore the env var.
                 Generating a new key destroys them permanently.
  * bad token -> the key is fine; the password must be re-entered.

Applying the wrong one loses the credential. These tests pin the distinction.
"""

import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils.connector_crypto import (
    decrypt_password,
    describe_decrypt_failure,
    encrypt_password,
    fernet_key_problem,
)

GOOD_KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()
TOKEN = encrypt_password("hunter2", GOOD_KEY)


# ── Which exception means what ───────────────────────────────────────────────

def test_round_trip():
    assert decrypt_password(TOKEN, GOOD_KEY) == "hunter2"


@pytest.mark.parametrize(
    "bad_key",
    [
        "my-secret-passphrase",              # not base64 at all
        "MTIzNDU2Nzg5MDEyMzQ1Ng==",          # valid base64, only 16 bytes
        TOKEN,                               # a Fernet *token* pasted as the key
    ],
)
def test_malformed_key_raises_value_error(bad_key):
    """ValueError is the signature of a bad KEY — this is what production saw."""
    with pytest.raises(ValueError):
        decrypt_password(TOKEN, bad_key)


@pytest.mark.parametrize("bad_token", ["", "hunter2", TOKEN[:-5]])
def test_bad_ciphertext_raises_invalid_token_not_value_error(bad_token):
    """A bad token never surfaces as ValueError, so the two are separable."""
    with pytest.raises(InvalidToken):
        decrypt_password(bad_token, GOOD_KEY)


def test_wrong_but_valid_key_is_invalid_token():
    with pytest.raises(InvalidToken):
        decrypt_password(TOKEN, OTHER_KEY)


@pytest.mark.parametrize(
    "wrapped",
    ['"%s"' % GOOD_KEY, GOOD_KEY + "\n", " %s " % GOOD_KEY],
)
def test_quotes_and_whitespace_are_tolerated(wrapped):
    """Rules these out as causes: they are outside the base64 alphabet and
    get discarded, so 'the env var has quotes' is never the explanation."""
    assert decrypt_password(TOKEN, wrapped) == "hunter2"


# ── The diagnosis helpers ────────────────────────────────────────────────────

def test_good_key_reports_no_problem():
    assert fernet_key_problem(GOOD_KEY) is None


def test_unset_key_is_named_as_such():
    problem = fernet_key_problem("")
    assert problem is not None
    assert "not set" in problem


def test_malformed_key_problem_names_the_env_var():
    problem = fernet_key_problem("my-secret-passphrase")
    assert problem is not None
    assert "CONNECTOR_ENCRYPTION_KEY" in problem
    assert "generate_key" in problem


def test_bad_key_message_says_credentials_are_intact_and_warns_against_rekey():
    """The dangerous mistake is generating a fresh key, which is unrecoverable."""
    msg = describe_decrypt_failure("my-secret-passphrase", ValueError("boom"))
    assert "intact" in msg
    assert "NEW key" in msg
    assert "do NOT" in msg


def test_bad_token_message_tells_the_admin_to_re_enter_the_password():
    msg = describe_decrypt_failure(GOOD_KEY, InvalidToken())
    assert "re-enter" in msg.lower()
    assert "Report Connector" in msg


def test_the_two_messages_are_different():
    """The whole point: one exception name produced one useless message."""
    bad_key_msg = describe_decrypt_failure("my-secret-passphrase", ValueError("boom"))
    bad_token_msg = describe_decrypt_failure(GOOD_KEY, InvalidToken())
    assert bad_key_msg != bad_token_msg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
