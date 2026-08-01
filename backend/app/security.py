import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import AdminUser, Role

settings = get_settings()
password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_context.verify(password, password_hash)


def create_access_token(login: str, role: Role) -> str:
    payload = {
        "sub": login,
        "role": role.value,
        "exp": datetime.now(timezone.utc) + timedelta(hours=12),
    }
    return jwt.encode(payload, settings.app_secret, algorithm=ALGORITHM)


def require_admin(request: Request, allowed: set[Role] | None = None) -> dict:
    token = request.cookies.get("vpn_admin_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход")
    try:
        payload = jwt.decode(token, settings.app_secret, algorithms=[ALGORITHM])
        Role(payload["role"])
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недействительная сессия") from exc
    with SessionLocal() as db:
        admin = db.scalar(select(AdminUser).where(AdminUser.login == payload["sub"], AdminUser.is_active.is_(True)))
    if not admin:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Учётная запись отключена")
    role = admin.role
    payload["role"] = role.value
    if allowed and role not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
    return payload


def generate_device_token() -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest(), token[-8:]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
