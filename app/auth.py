from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone


SESSION_COOKIE_NAME = "rp_session"
SESSION_TTL_DAYS = 30
PASSWORD_ITERATIONS = 260_000
PASSWORD_ALGORITHM = "pbkdf2_sha256"

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def generate_token(num_bytes: int = 32) -> str:
    return secrets.token_urlsafe(num_bytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return f"{PASSWORD_ALGORITHM}${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_raw, salt_hex, digest_hex = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != PASSWORD_ALGORITHM:
        return False
    try:
        iterations = int(iterations_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return secrets.compare_digest(candidate, expected)


def session_expires_at(days: int = SESSION_TTL_DAYS) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)
