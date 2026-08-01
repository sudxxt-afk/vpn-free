from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt
from app.database import Base, SessionLocal, engine, get_db
from app.models import (AdminUser, AnalyticsEvent, AuditLog, BroadcastCampaign, BroadcastDelivery, Device, MetricSnapshot,
                        Node, NodeState, PoolPolicy, RequiredChannel, Role, Source, SourceRun, SupportMessage, SupportTicket, TelegramUser)
from app.schemas import (AdminCreate, AdminResponse, AdminUpdate, AdminUserLookup, BotUserRequest, BroadcastCreate, ChannelCreate, ChannelResponse, DashboardResponse,
                         DeviceCreate, DeviceResponse, LoginRequest, ManagedAdminResponse, ManagedUserResponse,
                         AnalyticsDayResponse, AnalyticsResponse, InternalEventPayload, LandingEventPayload, MetricSnapshotResponse,
                         NodeResponse, PoolPolicyPayload, PoolPolicyResponse, SourceCreate, SourceResponse, SupportReplyPayload, SupportTicketCreate)
from app.security import create_access_token, generate_device_token, hash_password, hash_token, require_admin, verify_password
from app.services.github import SourceError, normalize_github_url, refresh_source
from app.services.parser import classify_network_profile, display_region, parse_config, transport_key, with_display_name
from app.services.telegram import has_required_memberships, validate_bot_admin
from app.services.subgram import get_partner_access
from app.services.rate_limit import is_allowed
from app.services.telegram_html import sanitize_telegram_html

settings = get_settings()


def bootstrap() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if engine.dialect.name == "postgresql":
            # Telegram IDs exceed signed 32-bit integers. Keep existing local installs usable.
            db.execute(text("ALTER TABLE telegram_users ALTER COLUMN telegram_id TYPE BIGINT"))
            db.execute(text("ALTER TABLE required_channels ALTER COLUMN chat_id TYPE BIGINT"))
            db.execute(text("ALTER TABLE telegram_users ADD COLUMN IF NOT EXISTS bot_blocked_at TIMESTAMPTZ"))
            db.execute(text("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS telegram_id BIGINT"))
            db.execute(text("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS telegram_username VARCHAR(128)"))
            db.execute(text("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS support_enabled BOOLEAN NOT NULL DEFAULT FALSE"))
            db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_admin_users_telegram_id ON admin_users (telegram_id) WHERE telegram_id IS NOT NULL"))
            db.commit()
        exists = db.scalar(select(AdminUser).where(AdminUser.login == settings.initial_admin_login))
        if not exists:
            db.add(AdminUser(login=settings.initial_admin_login, password_hash=hash_password(settings.initial_admin_password), role=Role.OWNER))
            db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    bootstrap()
    yield


app = FastAPI(title="Zaza VPN", version="0.1.0", lifespan=lifespan)


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        result = await is_allowed(request)
        if result is not None:
            allowed, limit, window = result
            if not allowed:
                return Response(status_code=429, content='{"detail":"Слишком много запросов. Попробуйте позже"}', media_type="application/json", headers={"Retry-After": str(window), "X-RateLimit-Limit": str(limit)})
        response = await call_next(request)
        if result is not None:
            response.headers["X-RateLimit-Limit"] = str(result[1])
        return response


app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def audit(db: Session, action: str, admin_login: str | None = None, details: str | None = None) -> None:
    admin = db.scalar(select(AdminUser).where(AdminUser.login == admin_login)) if admin_login else None
    db.add(AuditLog(admin_id=admin.id if admin else None, action=action, details=details))


def audit_admin(db: Session, action: str, admin: AdminUser, details: str | None = None) -> None:
    db.add(AuditLog(admin_id=admin.id, action=action, details=details))


def managed_admin_response(item: AdminUser) -> ManagedAdminResponse:
    return ManagedAdminResponse(id=item.id, login=item.login, role=item.role, is_active=item.is_active,
                                telegram_id=item.telegram_id, telegram_username=item.telegram_username,
                                support_enabled=item.support_enabled)


def resolve_telegram_identity(db: Session, telegram_id: int | None, username: str | None) -> tuple[int | None, str | None]:
    normalized = username.strip().lstrip("@") if username else None
    if telegram_id is not None:
        known = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
        return telegram_id, known.username if known and known.username else normalized
    if normalized:
        known = db.scalar(select(TelegramUser).where(func.lower(TelegramUser.username) == normalized.lower()))
        if not known:
            raise HTTPException(status_code=422, detail="Пользователь с таким @username ещё не запускал бота")
        return known.telegram_id, known.username
    return None, None


def require_bot_admin(db: Session, telegram_id: int, *, support: bool = False) -> AdminUser:
    admin = db.scalar(select(AdminUser).where(AdminUser.telegram_id == telegram_id, AdminUser.is_active.is_(True),
                                               AdminUser.role.in_([Role.OWNER, Role.ADMIN])))
    if not admin or (support and not admin.support_enabled):
        raise HTTPException(status_code=403, detail="Нет доступа к Telegram-админке")
    return admin


def track_event(db: Session, event_type: str, user_id: UUID | None = None, device_id: UUID | None = None) -> None:
    db.add(AnalyticsEvent(event_type=event_type, telegram_user_id=user_id, device_id=device_id))


def source_response(source: Source) -> SourceResponse:
    return SourceResponse(
        id=source.id, name=source.name, github_url=source.github_url, is_enabled=source.is_enabled,
        last_success_at=source.last_success_at, last_error=source.last_error, content_hash=source.content_hash,
    )


def device_response(device: Device, include_url: bool = False, plain_token: str | None = None) -> DeviceResponse:
    url = f"{settings.public_base_url}/s/{plain_token}" if include_url and plain_token else None
    return DeviceResponse(id=device.id, slot=device.slot, label=device.label, token_hint=device.token_hint, is_revoked=device.is_revoked, subscription_url=url)


def node_response(node: Node, source: Source | None = None) -> NodeResponse:
    raw_config = decrypt(node.config_ciphertext)
    parsed = parse_config(raw_config)
    if source:
        profile, profile_priority = node_profile(node, source)
    elif parsed:
        profile, profile_priority = parsed.network_profile, parsed.profile_priority
    else:
        profile, profile_priority = classify_network_profile(raw_config, node.protocol)
    region_emoji, region = display_region(raw_config, node.host)
    mobile = profile == "mobile"
    return NodeResponse(
        id=node.id, protocol=node.protocol, host=node.host, port=node.port, state=node.state, score=node.score,
        avg_latency_ms=node.avg_latency_ms, success_checks=node.success_checks, failed_checks=node.failed_checks,
        source_id=node.source_id, region=region, region_emoji=region_emoji, network_profile=profile,
        network_label="Мобильный интернет" if mobile else "Wi‑Fi",
        network_emoji="📡" if mobile else "📶", profile_priority=profile_priority,
    )


def source_network_hint(source: Source) -> str | None:
    """Respect explicit source lists such as BLACK_VLESS_RUS_mobile.txt."""
    reference = f"{source.github_url} {source.raw_url}".lower()
    return "mobile" if "mobile" in reference else None


def node_profile(node: Node, source: Source) -> tuple[str, int]:
    hint = source_network_hint(source)
    if hint:
        return "mobile", 110
    parsed = parse_config(decrypt(node.config_ciphertext))
    return (parsed.network_profile, parsed.profile_priority) if parsed else classify_network_profile("", node.protocol)


def pool_policy(db: Session) -> PoolPolicy:
    item = db.get(PoolPolicy, 1)
    if not item:
        item = PoolPolicy(id=1)
        db.add(item)
        db.commit()
        db.refresh(item)
    return item


def pool_policy_response(item: PoolPolicy) -> PoolPolicyResponse:
    return PoolPolicyResponse(**{name: getattr(item, name) for name in PoolPolicyPayload.model_fields}, updated_at=item.updated_at)


def require_internal(x_internal_key: str = Header(default="")) -> None:
    if x_internal_key != settings.internal_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Internal key required")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/login", response_model=AdminResponse)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> AdminResponse:
    admin = db.scalar(select(AdminUser).where(AdminUser.login == payload.login, AdminUser.is_active.is_(True)))
    if not admin or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")
    response.set_cookie("vpn_admin_token", create_access_token(admin.login, admin.role), httponly=True, samesite="lax",
                        secure=settings.public_base_url.startswith("https://"), max_age=43200)
    audit(db, "admin.login", admin.login)
    db.commit()
    return AdminResponse(login=admin.login, role=admin.role)


@app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie("vpn_admin_token")


@app.get("/auth/me", response_model=AdminResponse)
def auth_me(request: Request, db: Session = Depends(get_db)) -> AdminResponse:
    current = require_admin(request)
    admin = db.scalar(select(AdminUser).where(AdminUser.login == current["sub"]))
    if not admin:
        raise HTTPException(status_code=401, detail="Администратор не найден")
    return AdminResponse(login=admin.login, role=admin.role)


@app.get("/admin/dashboard", response_model=DashboardResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> DashboardResponse:
    require_admin(request)
    active = db.scalar(select(func.count()).select_from(Node).where(Node.state == NodeState.ACTIVE)) or 0
    quarantined = db.scalar(select(func.count()).select_from(Node).where(Node.state == NodeState.QUARANTINED)) or 0
    ping = db.scalar(select(func.avg(Node.avg_latency_ms)).where(Node.state == NodeState.ACTIVE))
    users = db.scalar(select(func.count()).select_from(TelegramUser).where(TelegramUser.is_blocked.is_(False))) or 0
    errors = db.scalar(select(func.count()).select_from(Source).where(Source.last_error.is_not(None))) or 0
    channels = db.scalar(select(func.count()).select_from(RequiredChannel).where(RequiredChannel.is_active.is_(True))) or 0
    return DashboardResponse(active_nodes=active, quarantined_nodes=quarantined, average_ping=round(ping, 1) if ping else None,
                             active_users=users, sources_with_errors=errors, required_channels=channels)


@app.get("/admin/metrics", response_model=list[MetricSnapshotResponse])
def metrics(request: Request, db: Session = Depends(get_db), limit: int = 36) -> list[MetricSnapshotResponse]:
    require_admin(request)
    items = db.scalars(select(MetricSnapshot).order_by(MetricSnapshot.created_at.desc()).limit(min(limit, 144))).all()
    return [MetricSnapshotResponse(active_nodes=item.active_nodes, quarantined_nodes=item.quarantined_nodes,
                                   average_ping_ms=item.average_ping_ms, check_success_rate=item.check_success_rate,
                                   created_at=item.created_at) for item in reversed(items)]


@app.get("/admin/analytics", response_model=AnalyticsResponse)
def analytics(request: Request, db: Session = Depends(get_db), days: int = 14) -> AnalyticsResponse:
    require_admin(request)
    days = max(7, min(days, 90))
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days - 1)
    events = db.scalars(select(AnalyticsEvent).where(AnalyticsEvent.created_at >= now - timedelta(days=30)).order_by(AnalyticsEvent.created_at)).all()
    points = {str((start + timedelta(days=offset)).date()): {"bot_starts": 0, "site_visits": 0, "happ_launches": 0, "vpn_issued": 0, "subscription_opens": 0} for offset in range(days)}
    event_fields = {"bot_start": "bot_starts", "site_visit": "site_visits", "happ_launch": "happ_launches", "vpn_issued": "vpn_issued", "subscription_open": "subscription_opens"}
    for event in events:
        key = str(event.created_at.date())
        field = event_fields.get(event.event_type)
        if key in points and field:
            points[key][field] += 1
    count = lambda name: sum(1 for event in events if event.event_type == name)
    def unique_users(event_types: set[str], since: datetime) -> int:
        return len({event.telegram_user_id for event in events if event.created_at >= since and event.event_type in event_types and event.telegram_user_id is not None})
    active_types = {"bot_start", "site_visit", "happ_launch", "vpn_issued", "subscription_open"}
    return AnalyticsResponse(
        total_bot_users=db.scalar(select(func.count()).select_from(TelegramUser)) or 0,
        new_bot_users=db.scalar(select(func.count()).select_from(TelegramUser).where(TelegramUser.created_at >= start)) or 0,
        known_bot_blocks=db.scalar(select(func.count()).select_from(TelegramUser).where(TelegramUser.bot_blocked_at.is_not(None))) or 0,
        active_users_1d=unique_users(active_types, now - timedelta(days=1)),
        active_users_7d=unique_users(active_types, now - timedelta(days=7)),
        active_users_30d=unique_users(active_types, now - timedelta(days=30)),
        active_devices=db.scalar(select(func.count()).select_from(Device).where(Device.is_revoked.is_(False))) or 0,
        funnel_bot_users=unique_users({"bot_start"}, start),
        funnel_site_users=unique_users({"site_visit"}, start),
        funnel_happ_users=unique_users({"happ_launch"}, start),
        funnel_subscription_users=unique_users({"subscription_open"}, start),
        bot_starts=count("bot_start"),
        unique_site_visitors=len({event.device_id for event in events if event.event_type == "site_visit" and event.device_id is not None}),
        happ_launches=count("happ_launch"),
        vpn_issued=count("vpn_issued"),
        subscription_opens=count("subscription_open"),
        days=[AnalyticsDayResponse(date=date, **values) for date, values in points.items()],
    )


@app.get("/admin/administrators", response_model=list[ManagedAdminResponse])
def list_administrators(request: Request, db: Session = Depends(get_db)) -> list[ManagedAdminResponse]:
    require_admin(request, {Role.OWNER})
    return [managed_admin_response(item) for item in db.scalars(select(AdminUser).order_by(AdminUser.created_at)).all()]


@app.post("/admin/administrators", response_model=ManagedAdminResponse, status_code=status.HTTP_201_CREATED)
def create_administrator(payload: AdminCreate, request: Request, db: Session = Depends(get_db)) -> ManagedAdminResponse:
    current = require_admin(request, {Role.OWNER})
    if db.scalar(select(AdminUser).where(AdminUser.login == payload.login)):
        raise HTTPException(status_code=409, detail="Такой логин уже существует")
    if payload.support_enabled and payload.role == Role.VIEWER:
        raise HTTPException(status_code=422, detail="Viewer не может работать с поддержкой")
    telegram_id, username = resolve_telegram_identity(db, payload.telegram_id, payload.telegram_username)
    if telegram_id and db.scalar(select(AdminUser).where(AdminUser.telegram_id == telegram_id)):
        raise HTTPException(status_code=409, detail="Этот Telegram уже привязан к другому администратору")
    item = AdminUser(login=payload.login, password_hash=hash_password(payload.password), role=payload.role,
                     telegram_id=telegram_id, telegram_username=username, support_enabled=payload.support_enabled)
    db.add(item)
    audit(db, "administrator.create", current["sub"], f"{item.login}:{item.role.value}")
    db.commit()
    db.refresh(item)
    return managed_admin_response(item)


@app.patch("/admin/administrators/{admin_id}", response_model=ManagedAdminResponse)
def update_administrator(admin_id: UUID, payload: AdminUpdate, request: Request, db: Session = Depends(get_db)) -> ManagedAdminResponse:
    current = require_admin(request, {Role.OWNER})
    item = db.get(AdminUser, admin_id)
    if not item:
        raise HTTPException(status_code=404, detail="Администратор не найден")
    if payload.support_enabled and payload.role == Role.VIEWER:
        raise HTTPException(status_code=422, detail="Viewer не может работать с поддержкой")
    if item.login == current["sub"] and (not payload.is_active or payload.role != Role.OWNER):
        raise HTTPException(status_code=409, detail="Нельзя отключить или понизить текущего владельца")
    telegram_id, username = resolve_telegram_identity(db, payload.telegram_id, payload.telegram_username)
    if telegram_id and db.scalar(select(AdminUser).where(AdminUser.telegram_id == telegram_id, AdminUser.id != item.id)):
        raise HTTPException(status_code=409, detail="Этот Telegram уже привязан к другому администратору")
    item.role = payload.role
    item.telegram_id = telegram_id
    item.telegram_username = username
    item.support_enabled = payload.support_enabled
    item.is_active = payload.is_active
    audit(db, "administrator.update", current["sub"], f"{item.login}:{item.role.value}:{telegram_id}")
    db.commit()
    db.refresh(item)
    return managed_admin_response(item)


@app.get("/admin/users", response_model=list[ManagedUserResponse])
def list_users(request: Request, db: Session = Depends(get_db), limit: int = 100) -> list[ManagedUserResponse]:
    require_admin(request)
    result = db.execute(
        select(TelegramUser, func.count(Device.id).label("devices"))
        .outerjoin(Device, Device.user_id == TelegramUser.id)
        .group_by(TelegramUser.id)
        .order_by(TelegramUser.created_at.desc())
        .limit(min(limit, 500))
    ).all()
    return [ManagedUserResponse(id=user.id, telegram_id=user.telegram_id, username=user.username, is_blocked=user.is_blocked,
                                device_count=devices, last_membership_check=user.last_membership_check) for user, devices in result]


@app.patch("/admin/users/{user_id}/block", response_model=ManagedUserResponse)
def toggle_user_block(user_id: UUID, request: Request, db: Session = Depends(get_db)) -> ManagedUserResponse:
    current = require_admin(request, {Role.OWNER, Role.ADMIN})
    user = db.get(TelegramUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    user.is_blocked = not user.is_blocked
    audit(db, "user.block_toggle", current["sub"], f"{user.telegram_id}:{user.is_blocked}")
    db.commit()
    device_count = db.scalar(select(func.count()).select_from(Device).where(Device.user_id == user.id)) or 0
    return ManagedUserResponse(id=user.id, telegram_id=user.telegram_id, username=user.username, is_blocked=user.is_blocked,
                               device_count=device_count, last_membership_check=user.last_membership_check)


@app.get("/admin/sources", response_model=list[SourceResponse])
def list_sources(request: Request, db: Session = Depends(get_db)) -> list[SourceResponse]:
    require_admin(request)
    return [source_response(item) for item in db.scalars(select(Source).order_by(Source.created_at.desc())).all()]


@app.post("/admin/sources", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
def create_source(payload: SourceCreate, request: Request, db: Session = Depends(get_db)) -> SourceResponse:
    current = require_admin(request, {Role.OWNER, Role.ADMIN})
    try:
        raw_url = normalize_github_url(payload.github_url)
    except SourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if db.scalar(select(Source).where(Source.raw_url == raw_url)):
        raise HTTPException(status_code=409, detail="Такой источник уже добавлен")
    source = Source(name=payload.name, github_url=payload.github_url, raw_url=raw_url)
    db.add(source)
    audit(db, "source.create", current["sub"], payload.github_url)
    db.commit()
    db.refresh(source)
    return source_response(source)


@app.post("/admin/sources/{source_id}/refresh")
def refresh_one(source_id: UUID, request: Request, db: Session = Depends(get_db)) -> dict:
    current = require_admin(request, {Role.OWNER, Role.ADMIN})
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Источник не найден")
    run = refresh_source(db, source)
    audit(db, "source.refresh", current["sub"], str(source_id))
    db.commit()
    return {"status": run.status, "found": run.found_count, "message": run.message}


@app.patch("/admin/sources/{source_id}/toggle", response_model=SourceResponse)
def toggle_source(source_id: UUID, request: Request, db: Session = Depends(get_db)) -> SourceResponse:
    current = require_admin(request, {Role.OWNER, Role.ADMIN})
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Источник не найден")
    source.is_enabled = not source.is_enabled
    audit(db, "source.toggle", current["sub"], f"{source_id}:{source.is_enabled}")
    db.commit()
    return source_response(source)


@app.get("/admin/sources/{source_id}/runs")
def source_runs(source_id: UUID, request: Request, db: Session = Depends(get_db)) -> list[dict]:
    require_admin(request)
    runs = db.scalars(select(SourceRun).where(SourceRun.source_id == source_id).order_by(SourceRun.started_at.desc()).limit(30)).all()
    return [{"id": str(run.id), "status": run.status, "message": run.message, "found": run.found_count, "published": run.published_count, "started_at": run.started_at, "finished_at": run.finished_at} for run in runs]


@app.get("/admin/nodes", response_model=list[NodeResponse])
def list_nodes(request: Request, db: Session = Depends(get_db), limit: int = 100) -> list[NodeResponse]:
    require_admin(request)
    rows = db.execute(select(Node, Source).join(Source, Source.id == Node.source_id).order_by(Node.score.desc()).limit(min(limit, 500))).all()
    response = [node_response(node, source) for node, source in rows]
    return sorted(response, key=lambda item: (item.profile_priority, item.score), reverse=True)


@app.get("/admin/pool-policy", response_model=PoolPolicyResponse)
def get_pool_policy(request: Request, db: Session = Depends(get_db)) -> PoolPolicyResponse:
    require_admin(request)
    return pool_policy_response(pool_policy(db))


@app.put("/admin/pool-policy", response_model=PoolPolicyResponse)
def update_pool_policy(payload: PoolPolicyPayload, request: Request, db: Session = Depends(get_db)) -> PoolPolicyResponse:
    current = require_admin(request, {Role.OWNER, Role.ADMIN})
    item = pool_policy(db)
    for name, value in payload.model_dump().items():
        setattr(item, name, value)
    audit(db, "pool_policy.update", current["sub"], str(payload.model_dump()))
    db.commit()
    db.refresh(item)
    return pool_policy_response(item)


@app.get("/admin/channels", response_model=list[ChannelResponse])
def list_channels(request: Request, db: Session = Depends(get_db)) -> list[ChannelResponse]:
    require_admin(request)
    return [ChannelResponse(id=c.id, chat_id=c.chat_id, title=c.title, username=c.username, is_active=c.is_active) for c in db.scalars(select(RequiredChannel)).all()]


@app.post("/admin/channels", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(payload: ChannelCreate, request: Request, db: Session = Depends(get_db)) -> ChannelResponse:
    current = require_admin(request, {Role.OWNER, Role.ADMIN})
    if not await validate_bot_admin(payload.chat_id):
        raise HTTPException(status_code=422, detail="Бот не является администратором этого канала")
    if db.scalar(select(RequiredChannel).where(RequiredChannel.chat_id == payload.chat_id)):
        raise HTTPException(status_code=409, detail="Канал уже добавлен")
    channel = RequiredChannel(**payload.model_dump())
    db.add(channel)
    audit(db, "channel.create", current["sub"], str(payload.chat_id))
    db.commit()
    db.refresh(channel)
    return ChannelResponse(id=channel.id, chat_id=channel.chat_id, title=channel.title, username=channel.username, is_active=channel.is_active)


@app.patch("/admin/channels/{channel_id}/toggle", response_model=ChannelResponse)
def toggle_channel(channel_id: UUID, request: Request, db: Session = Depends(get_db)) -> ChannelResponse:
    current = require_admin(request, {Role.OWNER, Role.ADMIN})
    channel = db.get(RequiredChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Канал не найден")
    channel.is_active = not channel.is_active
    audit(db, "channel.toggle", current["sub"], str(channel_id))
    db.commit()
    return ChannelResponse(id=channel.id, chat_id=channel.chat_id, title=channel.title, username=channel.username, is_active=channel.is_active)


@app.post("/internal/users", dependencies=[Depends(require_internal)])
def ensure_bot_user(payload: BotUserRequest, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == payload.telegram_id))
    if not user:
        user = TelegramUser(telegram_id=payload.telegram_id, username=payload.username)
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.username = payload.username
        user.bot_blocked_at = None
        db.commit()
    return {"id": str(user.id), "telegram_id": user.telegram_id, "blocked": user.is_blocked}


@app.post("/internal/users/{telegram_id}/bot-blocked", dependencies=[Depends(require_internal)])
def mark_bot_blocked(telegram_id: int, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user:
        return {"status": "ignored"}
    user.bot_blocked_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "recorded"}


@app.post("/internal/users/{telegram_id}/events", dependencies=[Depends(require_internal)])
def bot_event(telegram_id: int, payload: InternalEventPayload, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    track_event(db, payload.event_type, user_id=user.id)
    db.commit()
    return {"status": "recorded"}


@app.post("/events/landing")
def landing_event(payload: LandingEventPayload, db: Session = Depends(get_db)) -> dict:
    device = db.scalar(select(Device).where(Device.token_hash == hash_token(payload.token), Device.is_revoked.is_(False)))
    if not device:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    track_event(db, payload.event_type, user_id=device.user_id, device_id=device.id)
    db.commit()
    return {"status": "recorded"}


@app.post("/internal/users/{telegram_id}/access", dependencies=[Depends(require_internal)])
async def bot_access(telegram_id: int, target_devices: int = 1, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user or user.is_blocked:
        return {"allowed": False, "reason": "Пользователь заблокирован"}
    allowed = await has_required_memberships(db, user)
    if not allowed:
        return {"allowed": False, "reason": "Подпишитесь на обязательные каналы", "sponsors": []}
    decision = await get_partner_access(db, user, target_devices)
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "tier": decision.tier,
        "sponsors": [{"link": item.link, "title": item.title, "button_text": item.button_text} for item in decision.sponsors],
    }


@app.get("/internal/users/{telegram_id}/devices", dependencies=[Depends(require_internal)], response_model=list[DeviceResponse])
def bot_devices(telegram_id: int, db: Session = Depends(get_db)) -> list[DeviceResponse]:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user:
        return []
    return [device_response(device) for device in db.scalars(select(Device).where(Device.user_id == user.id).order_by(Device.slot)).all()]


@app.post("/internal/users/{telegram_id}/devices/{device_id}/rotate", dependencies=[Depends(require_internal)], response_model=DeviceResponse)
def bot_rotate_device(telegram_id: int, device_id: UUID, db: Session = Depends(get_db)) -> DeviceResponse:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    device = db.get(Device, device_id)
    if not user or not device or device.user_id != user.id:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    token, token_hash, hint = generate_device_token()
    device.token_hash = token_hash
    device.token_hint = hint
    device.is_revoked = False
    track_event(db, "vpn_issued", user_id=user.id, device_id=device.id)
    db.commit()
    return device_response(device, include_url=True, plain_token=token)


@app.get("/internal/channels", dependencies=[Depends(require_internal)])
def bot_channels(db: Session = Depends(get_db)) -> list[dict]:
    channels = db.scalars(select(RequiredChannel).where(RequiredChannel.is_active.is_(True))).all()
    return [{"title": channel.title, "username": channel.username, "chat_id": channel.chat_id} for channel in channels]


@app.get("/internal/status", dependencies=[Depends(require_internal)])
def bot_status(db: Session = Depends(get_db)) -> dict:
    active = db.scalar(select(func.count()).select_from(Node).where(Node.state == NodeState.ACTIVE)) or 0
    ping = db.scalar(select(func.avg(Node.avg_latency_ms)).where(Node.state == NodeState.ACTIVE))
    return {"active_nodes": active, "average_ping": round(ping, 1) if ping else None}


@app.post("/internal/users/{telegram_id}/devices", dependencies=[Depends(require_internal)], response_model=DeviceResponse)
async def bot_create_device(telegram_id: int, payload: DeviceCreate, db: Session = Depends(get_db)) -> DeviceResponse:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    existing = db.scalars(select(Device).where(Device.user_id == user.id, Device.is_revoked.is_(False))).all()
    if len(existing) >= 8:
        raise HTTPException(status_code=409, detail="Доступно не более 8 устройств")
    if not await has_required_memberships(db, user):
        raise HTTPException(status_code=403, detail="Подпишитесь на обязательные каналы")
    partner = await get_partner_access(db, user, len(existing) + 1)
    if not partner.allowed:
        raise HTTPException(status_code=403, detail="Сначала выполните условия партнёрского доступа")
    occupied = {item.slot for item in existing}
    slot = next(slot for slot in range(1, 9) if slot not in occupied)
    token, token_hash, hint = generate_device_token()
    device = Device(user_id=user.id, slot=slot, label=payload.label, token_hash=token_hash, token_hint=hint)
    db.add(device)
    db.flush()
    track_event(db, "vpn_issued", user_id=user.id, device_id=device.id)
    db.commit()
    db.refresh(device)
    return device_response(device, include_url=True, plain_token=token)


def ticket_payload(db: Session, ticket: SupportTicket) -> dict:
    user = db.get(TelegramUser, ticket.user_id)
    messages = db.scalars(select(SupportMessage).where(SupportMessage.ticket_id == ticket.id).order_by(SupportMessage.created_at)).all()
    return {
        "id": str(ticket.id), "status": ticket.status, "telegram_id": user.telegram_id if user else None,
        "username": user.username if user else None, "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "claimed_by_admin_id": str(ticket.claimed_by_admin_id) if ticket.claimed_by_admin_id else None,
        "messages": [{"sender_type": item.sender_type, "text": item.text, "created_at": item.created_at.isoformat() if item.created_at else None}
                     for item in messages],
    }


def campaign_payload(item: BroadcastCampaign) -> dict:
    return {"id": str(item.id), "segment": item.segment, "status": item.status, "text_html": item.text_html,
            "has_photo": bool(item.photo_file_id), "total_count": item.total_count, "sent_count": item.sent_count,
            "failed_count": item.failed_count, "skipped_count": item.skipped_count,
            "created_at": item.created_at.isoformat() if item.created_at else None}


@app.get("/internal/admin/{telegram_id}/dashboard", dependencies=[Depends(require_internal)])
def bot_admin_dashboard(telegram_id: int, db: Session = Depends(get_db)) -> dict:
    admin = require_bot_admin(db, telegram_id)
    return {
        "login": admin.login, "role": admin.role.value, "support_enabled": admin.support_enabled,
        "active_nodes": db.scalar(select(func.count()).select_from(Node).where(Node.state == NodeState.ACTIVE)) or 0,
        "problem_nodes": db.scalar(select(func.count()).select_from(Node).where(Node.state.in_([NodeState.DEGRADED, NodeState.QUARANTINED]))) or 0,
        "average_ping": db.scalar(select(func.avg(Node.avg_latency_ms)).where(Node.state == NodeState.ACTIVE)),
        "source_errors": db.scalar(select(func.count()).select_from(Source).where(Source.last_error.is_not(None))) or 0,
        "new_tickets": db.scalar(select(func.count()).select_from(SupportTicket).where(SupportTicket.status == "new")) or 0,
        "active_broadcasts": db.scalar(select(func.count()).select_from(BroadcastCampaign).where(BroadcastCampaign.status.in_(["queued", "processing"]))) or 0,
    }


@app.get("/internal/support/admins", dependencies=[Depends(require_internal)])
def support_admins(db: Session = Depends(get_db)) -> list[int]:
    return list(db.scalars(select(AdminUser.telegram_id).where(AdminUser.is_active.is_(True), AdminUser.support_enabled.is_(True),
                                                              AdminUser.telegram_id.is_not(None),
                                                              AdminUser.role.in_([Role.OWNER, Role.ADMIN]))).all())


@app.post("/internal/users/{telegram_id}/tickets", dependencies=[Depends(require_internal)])
def create_support_ticket(telegram_id: int, payload: SupportTicketCreate, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    ticket = SupportTicket(user_id=user.id)
    db.add(ticket)
    db.flush()
    db.add(SupportMessage(ticket_id=ticket.id, sender_type="user", text=payload.text.strip()))
    db.commit()
    db.refresh(ticket)
    result = ticket_payload(db, ticket)
    result["admin_ids"] = support_admins(db)
    return result


@app.get("/internal/admin/{telegram_id}/tickets", dependencies=[Depends(require_internal)])
def admin_tickets(telegram_id: int, status_filter: str | None = None, limit: int = 20, db: Session = Depends(get_db)) -> list[dict]:
    require_bot_admin(db, telegram_id, support=True)
    query = select(SupportTicket).order_by(SupportTicket.created_at.desc()).limit(min(limit, 50))
    if status_filter in {"new", "answered", "closed"}:
        query = query.where(SupportTicket.status == status_filter)
    return [ticket_payload(db, item) for item in db.scalars(query).all()]


@app.post("/internal/admin/{telegram_id}/tickets/{ticket_id}/claim", dependencies=[Depends(require_internal)])
def claim_ticket(telegram_id: int, ticket_id: UUID, db: Session = Depends(get_db)) -> dict:
    admin = require_bot_admin(db, telegram_id, support=True)
    ticket = db.get(SupportTicket, ticket_id)
    if not ticket or ticket.status == "closed":
        raise HTTPException(status_code=404, detail="Обращение недоступно")
    if ticket.claimed_by_admin_id and ticket.claimed_by_admin_id != admin.id:
        raise HTTPException(status_code=409, detail="Обращение уже взял другой администратор")
    if not ticket.claimed_by_admin_id:
        result = db.execute(update(SupportTicket).where(SupportTicket.id == ticket_id, SupportTicket.claimed_by_admin_id.is_(None))
                            .values(claimed_by_admin_id=admin.id, claimed_at=datetime.now(timezone.utc)))
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="Обращение уже взял другой администратор")
        audit_admin(db, "support.ticket.claim", admin, str(ticket_id))
        db.commit()
    return ticket_payload(db, db.get(SupportTicket, ticket_id))


@app.post("/internal/admin/{telegram_id}/tickets/{ticket_id}/reply", dependencies=[Depends(require_internal)])
async def reply_ticket(telegram_id: int, ticket_id: UUID, payload: SupportReplyPayload, db: Session = Depends(get_db)) -> dict:
    admin = require_bot_admin(db, telegram_id, support=True)
    ticket = db.get(SupportTicket, ticket_id)
    if not ticket or ticket.claimed_by_admin_id != admin.id or ticket.status == "closed":
        raise HTTPException(status_code=409, detail="Сначала возьмите открытое обращение в работу")
    user = db.get(TelegramUser, ticket.user_id)
    if not user or not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="Бот недоступен для отправки ответа")
    endpoint = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(endpoint, json={"chat_id": user.telegram_id, "text": f"💬 Ответ поддержки Zaza VPN\n\n{payload.text.strip()}"})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Telegram не принял ответ; попробуйте ещё раз") from exc
    ticket.status = "answered"
    ticket.replied_at = datetime.now(timezone.utc)
    db.add(SupportMessage(ticket_id=ticket.id, sender_type="admin", admin_id=admin.id, text=payload.text.strip()))
    audit_admin(db, "support.ticket.reply", admin, str(ticket_id))
    db.commit()
    return {"ticket": ticket_payload(db, ticket), "user_telegram_id": user.telegram_id if user else None}


@app.post("/internal/admin/{telegram_id}/tickets/{ticket_id}/{action}", dependencies=[Depends(require_internal)])
def change_ticket_status(telegram_id: int, ticket_id: UUID, action: str, db: Session = Depends(get_db)) -> dict:
    admin = require_bot_admin(db, telegram_id, support=True)
    ticket = db.get(SupportTicket, ticket_id)
    if not ticket or action not in {"close", "reopen"}:
        raise HTTPException(status_code=404, detail="Обращение или действие не найдено")
    if action == "close":
        ticket.status = "closed"
        ticket.closed_at = datetime.now(timezone.utc)
    else:
        ticket.status = "new"
        ticket.closed_at = None
        ticket.claimed_by_admin_id = None
        ticket.claimed_at = None
    audit_admin(db, f"support.ticket.{action}", admin, str(ticket_id))
    db.commit()
    return ticket_payload(db, ticket)


@app.post("/internal/admin/{telegram_id}/users/search", dependencies=[Depends(require_internal)])
def admin_user_search(telegram_id: int, payload: AdminUserLookup, db: Session = Depends(get_db)) -> dict:
    require_bot_admin(db, telegram_id)
    value = payload.query.strip().lstrip("@")
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == int(value))) if value.isdigit() else db.scalar(
        select(TelegramUser).where(func.lower(TelegramUser.username) == value.lower()))
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    devices = db.scalar(select(func.count()).select_from(Device).where(Device.user_id == user.id, Device.is_revoked.is_(False))) or 0
    return {"id": str(user.id), "telegram_id": user.telegram_id, "username": user.username, "is_blocked": user.is_blocked,
            "device_count": devices, "last_membership_check": user.last_membership_check.isoformat() if user.last_membership_check else None}


@app.post("/internal/admin/{telegram_id}/users/{user_id}/block", dependencies=[Depends(require_internal)])
def admin_user_block(telegram_id: int, user_id: UUID, db: Session = Depends(get_db)) -> dict:
    admin = require_bot_admin(db, telegram_id)
    user = db.get(TelegramUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    user.is_blocked = not user.is_blocked
    audit_admin(db, "telegram.user.block" if user.is_blocked else "telegram.user.unblock", admin, str(user.telegram_id))
    db.commit()
    return {"is_blocked": user.is_blocked}


@app.get("/internal/admin/{telegram_id}/sources", dependencies=[Depends(require_internal)])
def admin_sources(telegram_id: int, db: Session = Depends(get_db)) -> list[dict]:
    require_bot_admin(db, telegram_id)
    return [{"id": str(item.id), "name": item.name, "is_enabled": item.is_enabled, "last_error": item.last_error,
             "last_success_at": item.last_success_at.isoformat() if item.last_success_at else None}
            for item in db.scalars(select(Source).order_by(Source.created_at)).all()]


@app.post("/internal/admin/{telegram_id}/sources/{source_id}/refresh", dependencies=[Depends(require_internal)])
def admin_refresh_source(telegram_id: int, source_id: UUID, db: Session = Depends(get_db)) -> dict:
    admin = require_bot_admin(db, telegram_id)
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Источник не найден")
    run = refresh_source(db, source)
    audit_admin(db, "telegram.source.refresh", admin, f"{source_id}:{run.status}")
    db.commit()
    return {"name": source.name, "status": run.status, "found_count": run.found_count,
            "published_count": run.published_count, "message": run.message}


@app.get("/internal/admin/{telegram_id}/broadcasts", dependencies=[Depends(require_internal)])
def admin_broadcasts(telegram_id: int, db: Session = Depends(get_db)) -> list[dict]:
    require_bot_admin(db, telegram_id)
    return [campaign_payload(item) for item in db.scalars(select(BroadcastCampaign).order_by(BroadcastCampaign.created_at.desc()).limit(10)).all()]


@app.post("/internal/admin/{telegram_id}/broadcasts", dependencies=[Depends(require_internal)])
def create_broadcast(telegram_id: int, payload: BroadcastCreate, db: Session = Depends(get_db)) -> dict:
    admin = require_bot_admin(db, telegram_id)
    clean = sanitize_telegram_html(payload.text_html)
    max_length = 1024 if payload.photo_file_id else 4096
    if len(clean) > max_length:
        raise HTTPException(status_code=422, detail=f"Текст превышает лимит Telegram: {max_length} символов")
    if not clean and not payload.photo_file_id:
        raise HTTPException(status_code=422, detail="Рассылка не может быть пустой")
    item = BroadcastCampaign(author_admin_id=admin.id, segment=payload.segment, text_html=clean,
                             photo_file_id=payload.photo_file_id)
    db.add(item)
    db.flush()
    audit_admin(db, "broadcast.create", admin, f"{item.id}:{payload.segment}")
    db.commit()
    db.refresh(item)
    return campaign_payload(item)


@app.post("/internal/admin/{telegram_id}/broadcasts/{campaign_id}/cancel", dependencies=[Depends(require_internal)])
def cancel_broadcast(telegram_id: int, campaign_id: UUID, db: Session = Depends(get_db)) -> dict:
    admin = require_bot_admin(db, telegram_id)
    item = db.get(BroadcastCampaign, campaign_id)
    if not item or item.status in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Рассылку уже нельзя отменить")
    item.cancel_requested = True
    audit_admin(db, "broadcast.cancel", admin, str(campaign_id))
    db.commit()
    return campaign_payload(item)


@app.get("/s/{token}")
async def subscription(token: str, db: Session = Depends(get_db)) -> Response:
    device = db.scalar(select(Device).where(Device.token_hash == hash_token(token), Device.is_revoked.is_(False)))
    if not device:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    user = db.get(TelegramUser, device.user_id)
    if not user or user.is_blocked or not await has_required_memberships(db, user):
        raise HTTPException(status_code=403, detail="Выполните условия доступа в Telegram-боте")
    partner = await get_partner_access(db, user, device.slot)
    if not partner.allowed:
        raise HTTPException(status_code=403, detail="Выполните условия партнёрского доступа в Telegram-боте")
    track_event(db, "subscription_open", user_id=user.id, device_id=device.id)
    policy = pool_policy(db)
    rows = db.execute(
        select(Node, Source)
        .join(Source, Source.id == Node.source_id)
        .where(Node.state == NodeState.ACTIVE)
        .order_by(Node.score.desc())
    ).all()
    selected: list[tuple[Node, Source, str]] = []
    counts: dict[str, int] = {}
    best_by_profile: dict[str, tuple[Node, Source]] = {}
    for node, source in rows:
        profile, _ = node_profile(node, source)
        best_by_profile.setdefault(profile, (node, source))
        key = transport_key(decrypt(node.config_ciphertext), node.protocol)
        if counts.get(key, 0) >= getattr(policy, f"{key}_limit"):
            continue
        counts[key] = counts.get(key, 0) + 1
        selected.append((node, source, profile))

    # Keep the two dedicated auto routes first, then favour LTE-labelled nodes
    # in HAPP so mobile users see the most relevant locations immediately.
    selected.sort(key=lambda item: (0 if item[2] == "mobile" else 1, -item[0].score))

    # HAPP sees these as the first two ordinary servers. Their endpoint changes on the
    # next subscription refresh when the measured best candidate changes.
    payload_lines: list[str] = []
    auto_ids: set[UUID] = set()
    for profile, label in (("wifi", "📶 Автоподключение Wi‑Fi"), ("mobile", "📡 Автоподключение LTE")):
        candidate = best_by_profile.get(profile)
        if candidate:
            node, _source = candidate
            auto_ids.add(node.id)
            payload_lines.append(with_display_name(decrypt(node.config_ciphertext), f"🇪🇺 {label}"))
    for node, source, profile in selected:
        if node.id in auto_ids:
            continue
        emoji = "📡 LTE" if profile == "mobile" else "📶 Wi‑Fi"
        flag, region = display_region(decrypt(node.config_ciphertext), node.host)
        payload_lines.append(with_display_name(decrypt(node.config_ciphertext), f"{flag} {emoji} · {region}"))
    payload = "\n".join(payload_lines)
    device.last_used_at = datetime.now(timezone.utc)
    db.commit()
    # HAPP's ordinary URL subscriptions expect newline-separated share links,
    # not a Base64 envelope. Returning raw links also makes diagnostics in the
    # client much clearer when one source contains a malformed configuration.
    return Response(content=payload, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": "inline; filename=subscription.txt"})
