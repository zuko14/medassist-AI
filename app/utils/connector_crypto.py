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
