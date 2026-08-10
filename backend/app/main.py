from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import secrets
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt
from app.database import Base, SessionLocal, engine, get_db
from app.models import (AdminUser, AnalyticsEvent, AuditLog, BroadcastCampaign, BroadcastDelivery, Device, Donation, MetricSnapshot,
                        Node, NodeProbeAttempt, NodeProbeState, NodeState, PiarFlowAccessState, PiarFlowBotSnapshot,
                        PiarFlowDailyStat, PiarFlowEvent, PoolPolicy, RequiredChannel, RetiredSubscription, Role, Source,
                        SourceQuality, SourceRun, SupportMessage, SupportTicket, TelegramUser)
from app.schemas import (AdminCreate, AdminResponse, AdminUpdate, AdminUserLookup, BotUserRequest, BroadcastCreate, ChannelCreate, ChannelResponse, DashboardResponse,
                         DeviceCreate, DeviceResponse, LoginRequest, ManagedAdminResponse, ManagedUserResponse,
                         AnalyticsCohortResponse, AnalyticsDayResponse, AnalyticsResponse, InternalEventPayload, LandingEventPayload, MetricSnapshotResponse,
                         NodeProbeAttemptResponse, NodeResponse, PoolPolicyPayload, PoolPolicyResponse, SourceCreate, SourceResponse, StarDonationComplete,
                         StarDonationIntent, StarDonationPreCheckout, SupportReplyPayload, SupportTicketCreate, TonDonationPrepare,
                         PiarFlowAnalyticsResponse, PiarFlowDailyResponse, PiarFlowWebhookPayload)
from app.security import create_access_token, generate_device_token, hash_password, hash_token, require_admin, verify_password
from app.services.github import SourceError, normalize_github_url, refresh_source
from app.services.parser import address_diversity_key, classify_network_profile, display_region, parse_config, transport_key, with_display_name
from app.services.health import verified_pool_conditions
from app.services.telegram import has_required_memberships, validate_bot_admin
from app.services.piarflow import current_partner_access, get_partner_access, handle_unsubscribe
from app.services.rate_limit import is_allowed
from app.services.telegram_html import sanitize_telegram_html
from app.services.analytics import daily_retention_cohorts, sequential_funnel
from app.services.donations import (DonationError, complete_star_donation, create_star_donation, create_ton_session,
                                    donation_analytics, donation_by_public_token, donation_summary, prepare_ton_donation,
                                    public_ton_payload, validate_star_checkout, verify_pending_ton_donations)
from app.services.subscriptions import happ_retirement_payload

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
            db.execute(text("ALTER TABLE broadcast_campaigns ADD COLUMN IF NOT EXISTS buttons_json TEXT NOT NULL DEFAULT '[]'"))
            db.execute(text("ALTER TABLE broadcast_campaigns ADD COLUMN IF NOT EXISTS client_request_id UUID"))
            db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_broadcast_campaigns_client_request_id ON broadcast_campaigns (client_request_id) WHERE client_request_id IS NOT NULL"))
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


def bot_user(db: Session, telegram_id: int) -> TelegramUser:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


def source_response(source: Source, quality: SourceQuality | None = None) -> SourceResponse:
    try:
        rejection_reasons = json.loads(quality.rejection_reasons_json) if quality else {}
    except json.JSONDecodeError:
        rejection_reasons = {}
    return SourceResponse(
        id=source.id, name=source.name, github_url=source.github_url, is_enabled=source.is_enabled,
        last_success_at=source.last_success_at, last_error=source.last_error, content_hash=source.content_hash,
        quality_rating=round((quality.pass_rate if quality else 0) * 100, 1),
        checked_nodes=quality.checked_nodes if quality else 0,
        passed_nodes=quality.passed_nodes if quality else 0,
        rejected_nodes=quality.rejected_nodes if quality else 0,
        new_nodes_last_run=quality.new_nodes_last_run if quality else 0,
        rejection_reasons=rejection_reasons,
    )


def device_response(device: Device, include_url: bool = False, plain_token: str | None = None) -> DeviceResponse:
    url = f"{settings.public_base_url}/s/{plain_token}" if include_url and plain_token else None
    return DeviceResponse(id=device.id, slot=device.slot, label=device.label, token_hint=device.token_hint, is_revoked=device.is_revoked, subscription_url=url)


def node_response(node: Node, source: Source | None = None, probe: NodeProbeState | None = None) -> NodeResponse:
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
    grace_until = None
    if probe and probe.stage == "retrying" and probe.last_success_at:
        grace_until = probe.last_success_at + timedelta(minutes=settings.health_failure_grace_minutes)
    return NodeResponse(
        id=node.id, protocol=node.protocol, host=node.host, port=node.port, state=node.state, score=node.score,
        avg_latency_ms=node.avg_latency_ms, success_checks=node.success_checks, failed_checks=node.failed_checks,
        source_id=node.source_id, region=region, region_emoji=region_emoji, network_profile=profile,
        network_label="Мобильный интернет" if mobile else "Wi‑Fi",
        network_emoji="📡" if mobile else "📶", profile_priority=profile_priority,
        probe_stage=probe.stage if probe else None,
        probe_throughput_kbps=probe.throughput_kbps if probe else None,
        probe_error=probe.last_error if probe else None,
        probe_checked_at=probe.last_checked_at if probe else None,
        probe_grace_until=grace_until,
    )


def verified_pool_summary(db: Session) -> tuple[int, float | None]:
    active = db.scalar(
        select(func.count()).select_from(Node)
        .join(Source, Source.id == Node.source_id)
        .join(NodeProbeState, NodeProbeState.node_id == Node.id)
        .where(*verified_pool_conditions())
    ) or 0
    ping = db.scalar(
        select(func.avg(Node.avg_latency_ms)).select_from(Node)
        .join(Source, Source.id == Node.source_id)
        .join(NodeProbeState, NodeProbeState.node_id == Node.id)
        .where(*verified_pool_conditions())
    )
    return active, ping


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
    active, ping = verified_pool_summary(db)
    quarantined = db.scalar(select(func.count()).select_from(Node).where(Node.state == NodeState.QUARANTINED)) or 0
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
    events_from = min(start, now - timedelta(days=30))
    events = db.scalars(select(AnalyticsEvent).where(AnalyticsEvent.created_at >= events_from).order_by(AnalyticsEvent.created_at)).all()
    points = {str((start + timedelta(days=offset)).date()): {"bot_starts": 0, "site_visits": 0, "happ_launches": 0, "vpn_issued": 0, "subscription_opens": 0} for offset in range(days)}
    event_fields = {"bot_start": "bot_starts", "site_visit": "site_visits", "happ_launch": "happ_launches", "vpn_issued": "vpn_issued", "subscription_open": "subscription_opens"}
    for event in events:
        key = str(event.created_at.date())
        field = event_fields.get(event.event_type)
        if key in points and field:
            points[key][field] += 1
    count = lambda name: sum(1 for event in events if event.created_at >= start and event.event_type == name)
    def unique_users(event_types: set[str], since: datetime) -> int:
        return len({event.telegram_user_id for event in events if event.created_at >= since and event.event_type in event_types and event.telegram_user_id is not None})
    active_types = {"bot_start", "site_visit", "happ_launch", "vpn_issued", "subscription_open"}
    funnel = sequential_funnel(events, start)
    first_starts = db.execute(
        select(AnalyticsEvent.telegram_user_id, func.min(AnalyticsEvent.created_at))
        .where(AnalyticsEvent.event_type == "bot_start", AnalyticsEvent.telegram_user_id.is_not(None))
        .group_by(AnalyticsEvent.telegram_user_id)
    ).all()
    cohorts = daily_retention_cohorts(first_starts, events, now.date(), cohort_days=min(days, 14))
    donation_totals = donation_analytics(db, start)
    partner_events = db.scalars(
        select(PiarFlowEvent).where(PiarFlowEvent.created_at >= start).order_by(PiarFlowEvent.created_at)
    ).all()
    partner_users = lambda name: {
        event.user_id for event in partner_events if event.event_type == name and event.user_id is not None
    }
    barrier_users = partner_users("barrier_shown")
    task_users = partner_users("tasks_issued")
    check_users = partner_users("check_attempt")
    completed_users = partner_users("completed")
    checks_by_user: dict[UUID, int] = {}
    for event in partner_events:
        if event.event_type == "check_attempt" and event.user_id is not None:
            checks_by_user[event.user_id] = checks_by_user.get(event.user_id, 0) + 1
    completed_checks = [checks_by_user.get(user_id, 0) for user_id in completed_users]
    snapshot = db.get(PiarFlowBotSnapshot, 1)
    partner_days = db.scalars(
        select(PiarFlowDailyStat).where(PiarFlowDailyStat.date >= start.date()).order_by(PiarFlowDailyStat.date)
    ).all()
    status_count = lambda value: db.scalar(
        select(func.count()).select_from(PiarFlowAccessState).where(PiarFlowAccessState.status == value)
    ) or 0
    partner_analytics = PiarFlowAnalyticsResponse(
        enabled=settings.piarflow_enabled,
        provider_active=snapshot.is_active if snapshot else None,
        username=snapshot.username if snapshot else None,
        max_sponsors=snapshot.max_sponsors if snapshot else 0,
        reset_time=snapshot.reset_time if snapshot else 0,
        sold_subs=snapshot.sold_subs if snapshot else 0,
        not_counted=snapshot.not_counted if snapshot else 0,
        earned=round(snapshot.earned, 2) if snapshot else 0,
        last_synced_at=snapshot.last_synced_at if snapshot else None,
        last_error=snapshot.last_error if snapshot else None,
        barrier_users=len(barrier_users),
        task_users=len(task_users),
        check_users=len(check_users),
        completed_users=len(completed_users),
        pending_users=status_count("pending"),
        deferred_users=status_count("deferred_no_inventory"),
        unsubscribed_users=status_count("unsubscribed"),
        check_attempts=sum(checks_by_user.values()),
        average_checks_to_complete=round(sum(completed_checks) / len(completed_checks), 2) if completed_checks else 0,
        api_errors=sum(1 for event in partner_events if event.event_type == "api_error"),
        no_inventory=sum(1 for event in partner_events if event.event_type == "no_inventory"),
        unsubscribe_events=sum(1 for event in partner_events if event.event_type == "unsubscribed"),
        revoked_devices=sum(event.revoked_devices for event in partner_events),
        check_conversion=round(len(check_users) * 100 / len(barrier_users), 1) if barrier_users else 0,
        completion_conversion=round(len(completed_users) * 100 / len(barrier_users), 1) if barrier_users else 0,
        days=[PiarFlowDailyResponse(date=item.date, sold_subs=item.sold_subs, earned=item.earned) for item in partner_days],
    )
    return AnalyticsResponse(
        total_bot_users=db.scalar(select(func.count()).select_from(TelegramUser)) or 0,
        new_bot_users=db.scalar(select(func.count()).select_from(TelegramUser).where(TelegramUser.created_at >= start)) or 0,
        known_bot_blocks=db.scalar(select(func.count()).select_from(TelegramUser).where(TelegramUser.bot_blocked_at.is_not(None))) or 0,
        active_users_1d=unique_users(active_types, now - timedelta(days=1)),
        active_users_7d=unique_users(active_types, now - timedelta(days=7)),
        active_users_30d=unique_users(active_types, now - timedelta(days=30)),
        active_devices=db.scalar(select(func.count()).select_from(Device).where(Device.is_revoked.is_(False))) or 0,
        funnel_bot_users=funnel["bot_start"],
        funnel_vpn_users=funnel["vpn_issued"],
        funnel_site_users=funnel["site_visit"],
        funnel_happ_users=funnel["happ_launch"],
        funnel_subscription_users=funnel["subscription_open"],
        bot_starts=count("bot_start"),
        unique_site_visitors=len({event.device_id for event in events if event.event_type == "site_visit" and event.device_id is not None}),
        happ_launches=count("happ_launch"),
        vpn_issued=count("vpn_issued"),
        subscription_opens=count("subscription_open"),
        donation_opens=count("donation_open"),
        **donation_totals,
        days=[AnalyticsDayResponse(date=date, **values) for date, values in points.items()],
        cohorts=[AnalyticsCohortResponse(**item) for item in cohorts],
        piarflow=partner_analytics,
    )


@app.post("/admin/donations/{donation_id}/refund")
async def refund_star_donation(donation_id: UUID, request: Request, db: Session = Depends(get_db)) -> dict:
    current = require_admin(request, {Role.OWNER})
    item = db.get(Donation, donation_id)
    if not item or item.method != "stars" or item.status != "paid" or not item.telegram_payment_charge_id:
        raise HTTPException(status_code=409, detail="Этот донат нельзя вернуть")
    user = db.get(TelegramUser, item.user_id)
    if not user or not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="Telegram-платежи недоступны")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/refundStarPayment",
            json={"user_id": user.telegram_id, "telegram_payment_charge_id": item.telegram_payment_charge_id},
        )
    body = response.json()
    if not response.is_success or not body.get("ok"):
        raise HTTPException(status_code=502, detail=str(body.get("description") or "Telegram не выполнил возврат"))
    item.status = "refunded"
    item.refunded_at = datetime.now(timezone.utc)
    audit(db, "donation.refund", current["sub"], str(item.id))
    db.commit()
    return {"status": "refunded"}


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
    rows = db.execute(
        select(Source, SourceQuality)
        .outerjoin(SourceQuality, SourceQuality.source_id == Source.id)
        .order_by(Source.created_at.desc())
    ).all()
    return [source_response(source, quality) for source, quality in rows]


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
    return source_response(source, db.get(SourceQuality, source.id))


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
    return source_response(source, db.get(SourceQuality, source.id))


@app.get("/admin/sources/{source_id}/runs")
def source_runs(source_id: UUID, request: Request, db: Session = Depends(get_db)) -> list[dict]:
    require_admin(request)
    runs = db.scalars(select(SourceRun).where(SourceRun.source_id == source_id).order_by(SourceRun.started_at.desc()).limit(30)).all()
    return [{"id": str(run.id), "status": run.status, "message": run.message, "found": run.found_count, "published": run.published_count, "started_at": run.started_at, "finished_at": run.finished_at} for run in runs]


@app.get("/admin/nodes", response_model=list[NodeResponse])
def list_nodes(request: Request, db: Session = Depends(get_db), limit: int = 100) -> list[NodeResponse]:
    require_admin(request)
    rows = db.execute(
        select(Node, Source, NodeProbeState)
        .join(Source, Source.id == Node.source_id)
        .outerjoin(NodeProbeState, NodeProbeState.node_id == Node.id)
        .order_by(Node.score.desc()).limit(min(limit, 500))
    ).all()
    response = [node_response(node, source, probe) for node, source, probe in rows]
    return sorted(response, key=lambda item: (item.profile_priority, item.score), reverse=True)


@app.get("/admin/nodes/{node_id}/probes", response_model=list[NodeProbeAttemptResponse])
def node_probe_history(node_id: UUID, request: Request, days: int = 14, db: Session = Depends(get_db)) -> list[NodeProbeAttemptResponse]:
    require_admin(request)
    if not db.get(Node, node_id):
        raise HTTPException(status_code=404, detail="Нода не найдена")
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, settings.health_probe_history_days)))
    attempts = db.scalars(
        select(NodeProbeAttempt)
        .where(NodeProbeAttempt.node_id == node_id, NodeProbeAttempt.checked_at >= since)
        .order_by(NodeProbeAttempt.checked_at.desc()).limit(100)
    ).all()
    return [NodeProbeAttemptResponse(
        stage=item.stage, failure_class=item.failure_class,
        http_successes=item.http_successes, http_attempts=item.http_attempts,
        latency_ms=item.latency_ms, throughput_kbps=item.throughput_kbps,
        error=item.error, checked_at=item.checked_at,
    ) for item in attempts]


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


@app.get("/internal/users/{telegram_id}/donations", dependencies=[Depends(require_internal)])
def bot_donation_summary(telegram_id: int, db: Session = Depends(get_db)) -> dict:
    return donation_summary(db, bot_user(db, telegram_id))


@app.post("/internal/users/{telegram_id}/donations/stars", dependencies=[Depends(require_internal)])
def bot_create_star_donation(telegram_id: int, payload: StarDonationIntent, db: Session = Depends(get_db)) -> dict:
    try:
        item = create_star_donation(db, bot_user(db, telegram_id), payload.amount)
    except DonationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": str(item.id), "invoice_payload": item.invoice_payload, "amount": item.amount_stars}


@app.post("/internal/users/{telegram_id}/donations/stars/precheckout", dependencies=[Depends(require_internal)])
def bot_validate_star_donation(telegram_id: int, payload: StarDonationPreCheckout, db: Session = Depends(get_db)) -> dict:
    try:
        validate_star_checkout(db, bot_user(db, telegram_id), payload.invoice_payload, payload.currency, payload.total_amount)
    except DonationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/internal/users/{telegram_id}/donations/stars/complete", dependencies=[Depends(require_internal)])
def bot_complete_star_donation(telegram_id: int, payload: StarDonationComplete, db: Session = Depends(get_db)) -> dict:
    try:
        item = complete_star_donation(
            db,
            bot_user(db, telegram_id),
            invoice_payload=payload.invoice_payload,
            currency=payload.currency,
            total_amount=payload.total_amount,
            telegram_payment_charge_id=payload.telegram_payment_charge_id,
            provider_payment_charge_id=payload.provider_payment_charge_id,
        )
    except DonationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": item.status, "amount": item.amount_stars}


@app.post("/internal/users/{telegram_id}/donations/ton", dependencies=[Depends(require_internal)])
def bot_create_ton_donation(telegram_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        item, token = create_ton_session(db, bot_user(db, telegram_id))
    except DonationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"id": str(item.id), "url": f"{settings.web_app_base_url.rstrip('/')}/donate?token={token}"}


@app.get("/donations/{token}")
def public_donation(token: str, db: Session = Depends(get_db)) -> dict:
    try:
        return public_ton_payload(donation_by_public_token(db, token))
    except DonationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/donations/{token}/prepare")
def public_prepare_donation(token: str, payload: TonDonationPrepare, db: Session = Depends(get_db)) -> dict:
    try:
        item = donation_by_public_token(db, token)
        if not settings.ton_donation_address.strip():
            raise DonationError("TON-донаты пока не настроены")
        return public_ton_payload(prepare_ton_donation(db, item, payload.amount))
    except DonationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/donations/{token}/verify")
async def public_verify_donation(token: str, db: Session = Depends(get_db)) -> dict:
    try:
        item = donation_by_public_token(db, token)
        if item.status == "pending" and item.reference:
            await verify_pending_ton_donations(db)
            db.refresh(item)
        return public_ton_payload(item)
    except DonationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Не удалось проверить TON-транзакцию. Попробуйте ещё раз") from exc


@app.post("/events/landing")
def landing_event(payload: LandingEventPayload, db: Session = Depends(get_db)) -> dict:
    device = db.scalar(select(Device).where(Device.token_hash == hash_token(payload.token), Device.is_revoked.is_(False)))
    if not device:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    track_event(db, payload.event_type, user_id=device.user_id, device_id=device.id)
    db.commit()
    return {"status": "recorded"}


@app.post("/webhooks/piarflow/{webhook_secret}")
def piarflow_webhook(webhook_secret: str, payload: PiarFlowWebhookPayload, db: Session = Depends(get_db)) -> dict:
    configured = settings.piarflow_webhook_secret
    if not configured or not secrets.compare_digest(webhook_secret, configured):
        raise HTTPException(status_code=404, detail="Webhook не найден")
    if payload.test:
        return {"ok": True, "test": True}
    if payload.status != "unsubscribed" or payload.tg_user_id is None or not payload.offer_link:
        raise HTTPException(status_code=422, detail="Некорректное событие PiarFlow")
    snapshot = db.get(PiarFlowBotSnapshot, 1)
    if snapshot and snapshot.bot_id and payload.bot_id and snapshot.bot_id != payload.bot_id:
        raise HTTPException(status_code=403, detail="Событие предназначено другому боту")
    revoked = handle_unsubscribe(db, payload.tg_user_id, payload.offer_link, payload.bot_id)
    return {"ok": True, "revoked_devices": revoked}


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
        "status": decision.status,
        "reason": decision.reason,
        "sponsors": [{"link": item.link, "title": item.title, "button_text": item.button_text} for item in decision.sponsors],
        "sponsor_total": decision.sponsor_total,
    }


@app.get("/internal/users/{telegram_id}/access", dependencies=[Depends(require_internal)])
def bot_access_status(telegram_id: int, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user or user.is_blocked:
        return {"allowed": False, "status": "blocked", "reason": "Пользователь заблокирован", "sponsors": [], "sponsor_total": 0}
    decision = current_partner_access(db, user)
    return {
        "allowed": decision.allowed,
        "status": decision.status,
        "reason": decision.reason,
        "sponsors": [{"link": item.link, "title": item.title, "button_text": item.button_text} for item in decision.sponsors],
        "sponsor_total": decision.sponsor_total,
    }


@app.get("/internal/users/{telegram_id}/devices", dependencies=[Depends(require_internal)], response_model=list[DeviceResponse])
def bot_devices(telegram_id: int, db: Session = Depends(get_db)) -> list[DeviceResponse]:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if not user:
        return []
    return [device_response(device) for device in db.scalars(
        select(Device).where(Device.user_id == user.id, Device.is_revoked.is_(False)).order_by(Device.slot)
    ).all()]


@app.post("/internal/users/{telegram_id}/devices/{device_id}/rotate", dependencies=[Depends(require_internal)], response_model=DeviceResponse)
def bot_rotate_device(telegram_id: int, device_id: UUID, db: Session = Depends(get_db)) -> DeviceResponse:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    device = db.get(Device, device_id)
    if not user or not device or device.user_id != user.id:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    if not current_partner_access(db, user).allowed:
        raise HTTPException(status_code=403, detail="Сначала выполните задания PiarFlow")
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
    active, ping = verified_pool_summary(db)
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
    partner = current_partner_access(db, user)
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
            "has_photo": bool(item.photo_file_id), "button_count": len(json.loads(item.buttons_json or "[]")),
            "total_count": item.total_count, "sent_count": item.sent_count,
            "failed_count": item.failed_count, "skipped_count": item.skipped_count,
            "created_at": item.created_at.isoformat() if item.created_at else None}


@app.get("/internal/admin/{telegram_id}/dashboard", dependencies=[Depends(require_internal)])
def bot_admin_dashboard(telegram_id: int, db: Session = Depends(get_db)) -> dict:
    admin = require_bot_admin(db, telegram_id)
    active, ping = verified_pool_summary(db)
    return {
        "login": admin.login, "role": admin.role.value, "support_enabled": admin.support_enabled,
        "active_nodes": active,
        "problem_nodes": db.scalar(select(func.count()).select_from(Node).where(Node.state.in_([NodeState.DEGRADED, NodeState.QUARANTINED]))) or 0,
        "average_ping": ping,
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


@app.get("/internal/admin/{telegram_id}/broadcasts/test-recipients", dependencies=[Depends(require_internal)])
def broadcast_test_recipients(telegram_id: int, db: Session = Depends(get_db)) -> list[int]:
    require_bot_admin(db, telegram_id)
    return list(db.scalars(select(AdminUser.telegram_id).where(
        AdminUser.is_active.is_(True), AdminUser.telegram_id.is_not(None),
        AdminUser.role.in_([Role.OWNER, Role.ADMIN]))).all())


@app.post("/internal/admin/{telegram_id}/broadcasts", dependencies=[Depends(require_internal)])
def create_broadcast(telegram_id: int, payload: BroadcastCreate, db: Session = Depends(get_db)) -> dict:
    admin = require_bot_admin(db, telegram_id)
    existing = db.scalar(select(BroadcastCampaign).where(
        BroadcastCampaign.author_admin_id == admin.id,
        BroadcastCampaign.client_request_id == payload.client_request_id,
    ))
    if existing:
        return campaign_payload(existing)
    clean = sanitize_telegram_html(payload.text_html)
    max_length = 1024 if payload.photo_file_id else 4096
    if len(clean) > max_length:
        raise HTTPException(status_code=422, detail=f"Текст превышает лимит Telegram: {max_length} символов")
    if not clean and not payload.photo_file_id:
        raise HTTPException(status_code=422, detail="Рассылка не может быть пустой")
    buttons_json = json.dumps([button.model_dump() for button in payload.buttons], ensure_ascii=False)
    item = BroadcastCampaign(author_admin_id=admin.id, client_request_id=payload.client_request_id,
                             segment=payload.segment, text_html=clean,
                             photo_file_id=payload.photo_file_id, buttons_json=buttons_json)
    db.add(item)
    try:
        db.flush()
        audit_admin(db, "broadcast.create", admin, f"{item.id}:{payload.segment}")
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(BroadcastCampaign).where(
            BroadcastCampaign.client_request_id == payload.client_request_id,
        ))
        if existing:
            return campaign_payload(existing)
        raise
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
    token_hash = hash_token(token)
    device = db.scalar(select(Device).where(Device.token_hash == token_hash, Device.is_revoked.is_(False)))
    if not device:
        retired = db.scalar(select(RetiredSubscription).where(RetiredSubscription.token_hash == token_hash))
        if retired:
            body, headers = happ_retirement_payload(retired.reason)
            return Response(content=body, media_type="text/plain; charset=utf-8", headers=headers)
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    user = db.get(TelegramUser, device.user_id)
    if not user or user.is_blocked or not current_partner_access(db, user).allowed or not await has_required_memberships(db, user):
        raise HTTPException(status_code=403, detail="Выполните условия доступа в Telegram-боте")
    track_event(db, "subscription_open", user_id=user.id, device_id=device.id)
    policy = pool_policy(db)
    rows = db.execute(
        select(Node, Source)
        .join(Source, Source.id == Node.source_id)
        .join(NodeProbeState, NodeProbeState.node_id == Node.id)
        .where(*verified_pool_conditions())
        .order_by(Node.score.desc())
    ).all()
    selected: list[tuple[Node, Source, str]] = []
    counts: dict[str, int] = {}
    source_counts: dict[UUID, int] = {}
    host_counts: dict[str, int] = {}
    address_groups: set[str] = set()
    best_by_profile: dict[str, tuple[Node, Source]] = {}
    for node, source in rows:
        profile, _ = node_profile(node, source)
        key = transport_key(decrypt(node.config_ciphertext), node.protocol)
        if counts.get(key, 0) >= getattr(policy, f"{key}_limit"):
            continue
        if source_counts.get(source.id, 0) >= settings.subscription_max_per_source:
            continue
        if host_counts.get(node.host, 0) >= settings.subscription_max_per_host:
            continue
        address_group = address_diversity_key(node.host)
        if address_group and address_group in address_groups:
            continue
        counts[key] = counts.get(key, 0) + 1
        source_counts[source.id] = source_counts.get(source.id, 0) + 1
        host_counts[node.host] = host_counts.get(node.host, 0) + 1
        if address_group:
            address_groups.add(address_group)
        best_by_profile.setdefault(profile, (node, source))
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
