import hashlib

import pytest

from utils.security import (
    PASSWORD_HASH_ITERATIONS,
    email_is_allowed,
    password_is_valid,
    token_is_current,
    validate_allowed_emails,
    validate_password_users,
)


def test_allowlist_is_case_insensitive():
    assert email_is_allowed(" Persona@Empresa.com ", ["persona@empresa.com"])
    assert not email_is_allowed("otro@empresa.com", ["persona@empresa.com"])


def test_token_expiration_is_checked():
    assert token_is_current({"exp": 101}, now=100)
    assert not token_is_current({"exp": 100}, now=100)
    assert not token_is_current({}, now=100)


def test_allowed_users_are_normalized_and_deduplicated():
    assert validate_allowed_emails([" Persona@Empresa.com ", "persona@empresa.com"]) == ("persona@empresa.com",)


def test_allowed_users_are_limited_to_five():
    assert len(validate_allowed_emails([f"persona{number}@empresa.com" for number in range(5)])) == 5
    with pytest.raises(ValueError, match="máximo 5"):
        validate_allowed_emails([f"persona{number}@empresa.com" for number in range(6)])


def password_user(username="operador", password="secreto", salt_hex="11" * 16):
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        PASSWORD_HASH_ITERATIONS,
    ).hex()
    return {"username": username, "salt": salt_hex, "password_hash": password_hash}


def test_password_login_is_case_insensitive_only_for_username():
    users = [password_user(username="GABCERV", password="Clave-Exacta")]

    assert password_is_valid(" gabcerv ", "Clave-Exacta", users)
    assert not password_is_valid("GABCERV", "clave-exacta", users)
    assert not password_is_valid("OTRO", "Clave-Exacta", users)


def test_password_users_require_secure_hashes_and_are_limited_to_five():
    assert len(validate_password_users([password_user(username=f"usuario{number}") for number in range(5)])) == 5
    with pytest.raises(ValueError, match="máximo 5"):
        validate_password_users([password_user(username=f"usuario{number}") for number in range(6)])
    with pytest.raises(ValueError, match="inválido"):
        validate_password_users([{"username": "operador", "salt": "00", "password_hash": "11"}])
