"""Shared Fernet encrypt/decrypt for connector credentials at rest.

Used by both the admin API (encrypting a password on save) and the
connector runner (decrypting it before use). Single source of truth so the
two never drift.
"""


def encrypt_password(plaintext: str, key: str) -> str:
    from cryptography.fernet import Fernet

    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt_password(encrypted: str, key: str) -> str:
    from cryptography.fernet import Fernet

    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.decrypt(
        encrypted.encode() if isinstance(encrypted, str) else encrypted
    ).decode()

def fernet_key_problem(key: str) -> str | None:
    """Return why `key` is unusable as a Fernet key, or None if it is fine.

    Worth separating because the two failure modes need opposite remedies and
    are indistinguishable from the raw exception name alone:

      * ValueError    -> the KEY is malformed. Stored credentials are intact;
                         fix CONNECTOR_ENCRYPTION_KEY on the server.
      * InvalidToken  -> the key is a valid Fernet key but not the one this
                         ciphertext was encrypted with (or the ciphertext is
                         corrupt). Credentials must be re-entered.

    Note that Fernet tolerates surrounding quotes, spaces and newlines (they
    are not in the base64 alphabet and get discarded), so those are NOT causes.
    """
    from cryptography.fernet import Fernet

    if not key:
        return "CONNECTOR_ENCRYPTION_KEY is not set"
    try:
        Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        return (
            "CONNECTOR_ENCRYPTION_KEY is not a valid Fernet key "
            f"({type(e).__name__}: {e}). It must be exactly the output of "
            "Fernet.generate_key() — 32 bytes, url-safe base64, 44 characters "
            "ending in '='. A passphrase, a truncated value, or an encrypted "
            "token pasted in by mistake all produce this."
        )
    return None


def describe_decrypt_failure(key: str, exc: Exception) -> str:
    """Turn a decryption exception into something an operator can act on.

    The previous message was just the exception class name, which told the
    admin nothing about whether to fix the server config or re-enter the
    password — the two possible causes need opposite actions.
    """
    problem = fernet_key_problem(key)
    if problem:
        return (
            f"{problem} Stored credentials are intact and will work again once "
            "the correct key is restored — do NOT re-enter the password until "
            "then, and note that generating a NEW key makes the stored password "
            "permanently undecryptable."
        )
    return (
        f"Stored password could not be decrypted ({type(exc).__name__}). The "
        "server's encryption key is valid, so this password was encrypted with "
        "a different key — re-enter the password in Admin -> Report Connector "
        "to re-encrypt it with the current key."
    )
