import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt
from app.database import Base, SessionLocal, engine, get_db
from app.models import AdminUser, AnalyticsEvent, AuditLog, Device, MetricSnapshot, Node, NodeState, PoolPolicy, RequiredChannel, Role, Source, SourceRun, TelegramUser
from app.schemas import (AdminCreate, AdminResponse, BotUserRequest, ChannelCreate, ChannelResponse, DashboardResponse,
                         DeviceCreate, DeviceResponse, LoginRequest, ManagedAdminResponse, ManagedUserResponse,
                         AnalyticsDayResponse, AnalyticsResponse, InternalEventPayload, LandingEventPayload, MetricSnapshotResponse,
                         NodeResponse, PoolPolicyPayload, PoolPolicyResponse, SourceCreate, SourceResponse)
from app.security import create_access_token, generate_device_token, hash_password, hash_token, require_admin, verify_password
from app.services.github import SourceError, normalize_github_url, refresh_source
from app.services.parser import classify_network_profile, display_region, parse_config, transport_key, with_display_name
from app.services.telegram import has_required_memberships, validate_bot_admin

settings = get_settings()


def bootstrap() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if engine.dialect.name == "postgresql":
            # Telegram IDs exceed signed 32-bit integers. Keep existing local installs usable.
            db.execute(text("ALTER TABLE telegram_users ALTER COLUMN telegram_id TYPE BIGINT"))
            db.execute(text("ALTER TABLE required_channels ALTER COLUMN chat_id TYPE BIGINT"))
            db.commit()
        exists = db.scalar(select(AdminUser).where(AdminUser.login == settings.initial_admin_login))
        if not exists:
            db.add(AdminUser(login=settings.initial_admin_login, password_hash=hash_password(settings.initial_admin_password), role=Role.OWNER))
            db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    bootstrap()
    yield


app = FastAPI(title="VPN Control Plane", version="0.1.0", lifespan=lifespan)
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
    start = datetime.now(timezone.utc) - timedelta(days=days - 1)
    events = db.scalars(select(AnalyticsEvent).where(AnalyticsEvent.created_at >= start).order_by(AnalyticsEvent.created_at)).all()
    points = {str((start + timedelta(days=offset)).date()): {"bot_starts": 0, "site_visits": 0, "happ_launches": 0, "vpn_issued": 0, "subscription_opens": 0} for offset in range(days)}
    event_fields = {"bot_start": "bot_starts", "site_visit": "site_visits", "happ_launch": "happ_launches", "vpn_issued": "vpn_issued", "subscription_open": "subscription_opens"}
    for event in events:
        key = str(event.created_at.date())
        field = event_fields.get(event.event_type)
        if key in points and field:
            points[key][field] += 1
    count = lambda name: sum(1 for event in events if event.event_type == name)
    return AnalyticsResponse(
        total_bot_users=db.scalar(select(func.count()).select_from(TelegramUser)) or 0,
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
    return [ManagedAdminResponse(id=item.id, login=item.login, role=item.role, is_active=item.is_active)
            for item in db.scalars(select(AdminUser).order_by(AdminUser.created_at)).all()]


@app.post("/admin/administrators", response_model=ManagedAdminResponse, status_code=status.HTTP_201_CREATED)
def create_administrator(payload: AdminCreate, request: Request, db: Session = Depends(get_db)) -> ManagedAdminResponse:
    current = require_admin(request, {Role.OWNER})
    if db.scalar(select(AdminUser).where(AdminUser.login == payload.login)):
        raise HTTPException(status_code=409, detail="Такой логин уже существует")
    item = AdminUser(login=payload.login, password_hash=hash_password(payload.password), role=payload.role)
    db.add(item)
    audit(db, "administrator.create", current["sub"], f"{item.login}:{item.role.value}")
    db.commit()
    db.refresh(item)
    return ManagedAdminResponse(id=item.id, login=item.login, role=item.role, is_active=item.is_active)


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
    elif payload.username != user.username:
        user.username = payload.username
        db.commit()
    return {"id": str(user.id), "telegram_id": user.telegram_id, "blocked": user.is_blocked}


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
async def bot_access(telegram_id: int, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user or user.is_blocked:
        return {"allowed": False, "reason": "Пользователь заблокирован"}
    allowed = await has_required_memberships(db, user)
    return {"allowed": allowed, "reason": None if allowed else "Подпишитесь на обязательные каналы"}


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
def bot_create_device(telegram_id: int, payload: DeviceCreate, db: Session = Depends(get_db)) -> DeviceResponse:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    existing = db.scalars(select(Device).where(Device.user_id == user.id, Device.is_revoked.is_(False))).all()
    if len(existing) >= 2:
        raise HTTPException(status_code=409, detail="Доступны только две ячейки устройств")
    occupied = {item.slot for item in existing}
    slot = next(slot for slot in (1, 2) if slot not in occupied)
    token, token_hash, hint = generate_device_token()
    device = Device(user_id=user.id, slot=slot, label=payload.label, token_hash=token_hash, token_hint=hint)
    db.add(device)
    db.flush()
    track_event(db, "vpn_issued", user_id=user.id, device_id=device.id)
    db.commit()
    db.refresh(device)
    return device_response(device, include_url=True, plain_token=token)


@app.get("/s/{token}")
async def subscription(token: str, db: Session = Depends(get_db)) -> Response:
    device = db.scalar(select(Device).where(Device.token_hash == hash_token(token), Device.is_revoked.is_(False)))
    if not device:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    user = db.get(TelegramUser, device.user_id)
    if not user or user.is_blocked or not await has_required_memberships(db, user):
        raise HTTPException(status_code=403, detail="Выполните условия доступа в Telegram-боте")
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

    # HAPP sees these as the first two ordinary servers. Their endpoint changes on the
    # next subscription refresh when the measured best candidate changes.
    payload_lines: list[str] = []
    auto_ids: set[UUID] = set()
    for profile, label in (("wifi", "📶 Автоподключение Wi‑Fi"), ("mobile", "📡 Автоподключение LTE")):
        candidate = best_by_profile.get(profile)
        if candidate:
            node, _source = candidate
            auto_ids.add(node.id)
            payload_lines.append(with_display_name(decrypt(node.config_ciphertext), label))
    for node, source, profile in selected:
        if node.id in auto_ids:
            continue
        emoji = "📡 LTE" if profile == "mobile" else "📶 Wi‑Fi"
        _flag, region = display_region(decrypt(node.config_ciphertext), node.host)
        payload_lines.append(with_display_name(decrypt(node.config_ciphertext), f"{emoji} · {region}"))
    payload = "\n".join(payload_lines)
    device.last_used_at = datetime.now(timezone.utc)
    db.commit()
    encoded = base64.b64encode(payload.encode()).decode()
    return Response(content=encoded, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": "inline; filename=subscription.txt"})
