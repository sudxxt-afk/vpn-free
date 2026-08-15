import base64
import hashlib

from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet() -> Fernet:
    configured = get_settings().app_encryption_key
    if configured:
        return Fernet(configured.encode())
    # Runtime startup validation prevents this development fallback from being
    # used in production. Keeping it here makes isolated SQLite tests portable.
    key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().app_secret.encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()
