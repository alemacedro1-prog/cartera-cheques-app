from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

MAX_ALLOWED_USERS = 5
PASSWORD_HASH_ITERATIONS = 310_000


def normalize_email(value: object) -> str:
    return str(value or "").strip().casefold()


def normalize_allowed_emails(allowed: object) -> tuple[str, ...]:
    if isinstance(allowed, str):
        values = allowed.split(",")
    elif isinstance(allowed, (list, tuple)):
        values = allowed
    else:
        values = []
    return tuple(dict.fromkeys(normalize_email(item) for item in values if normalize_email(item)))


def validate_allowed_emails(allowed: object, max_users: int = MAX_ALLOWED_USERS) -> tuple[str, ...]:
    emails = normalize_allowed_emails(allowed)
    if not emails:
        raise ValueError("Configurá al menos un correo autorizado.")
    if len(emails) > max_users:
        raise ValueError(f"La aplicación admite como máximo {max_users} usuarios autorizados.")
    return emails


def email_is_allowed(email: object, allowed: object) -> bool:
    normalized = normalize_email(email)
    return bool(normalized) and normalized in set(normalize_allowed_emails(allowed))


def token_is_current(user: Mapping[str, object], now: int | None = None) -> bool:
    try:
        expiration = int(user.get("exp", 0))
    except (TypeError, ValueError):
        return False
    return expiration > (now if now is not None else int(time.time()))


def normalize_username(value: object) -> str:
    return str(value or "").strip().casefold()


def validate_password_users(users: object, max_users: int = MAX_ALLOWED_USERS) -> tuple[dict[str, str], ...]:
    if not isinstance(users, (list, tuple)) or not users:
        raise ValueError("Configurá al menos un usuario y contraseña autorizados.")
    if len(users) > max_users:
        raise ValueError(f"La aplicación admite como máximo {max_users} usuarios autorizados.")

    validated: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in users:
        if not isinstance(item, Mapping):
            raise ValueError("La configuración de usuarios autorizados es inválida.")
        username = normalize_username(item.get("username"))
        salt = str(item.get("salt") or "").strip().lower()
        password_hash = str(item.get("password_hash") or "").strip().lower()
        try:
            salt_bytes = bytes.fromhex(salt)
            hash_bytes = bytes.fromhex(password_hash)
        except ValueError as error:
            raise ValueError("El hash de contraseña configurado es inválido.") from error
        if not username or len(salt_bytes) < 16 or len(hash_bytes) != 32:
            raise ValueError("El usuario, salt o hash de contraseña configurado es inválido.")
        if username in seen:
            raise ValueError("Los nombres de usuario autorizados no pueden repetirse.")
        seen.add(username)
        validated.append({"username": username, "salt": salt, "password_hash": password_hash})
    return tuple(validated)


def password_is_valid(username: object, password: object, users: object) -> bool:
    normalized_username = normalize_username(username)
    password_text = str(password or "")
    if not normalized_username or not password_text:
        return False
    for user in validate_password_users(users):
        if not hmac.compare_digest(normalized_username, user["username"]):
            continue
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password_text.encode("utf-8"),
            bytes.fromhex(user["salt"]),
            PASSWORD_HASH_ITERATIONS,
        ).hex()
        return hmac.compare_digest(candidate, user["password_hash"])
    return False
