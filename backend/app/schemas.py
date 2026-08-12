from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from urllib.parse import urlparse

from app.models import NodeState, Role


class LoginRequest(BaseModel):
    login: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)


class AdminResponse(BaseModel):
    login: str
    role: Role


class AdminCreate(BaseModel):
    login: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=200)
    role: Role = Role.VIEWER
    telegram_id: int | None = None
    telegram_username: str | None = Field(default=None, max_length=128)
    support_enabled: bool = False


class AdminUpdate(BaseModel):
    role: Role
    telegram_id: int | None = None
    telegram_username: str | None = Field(default=None, max_length=128)
    support_enabled: bool = False
    is_active: bool = True


class ManagedAdminResponse(BaseModel):
    id: UUID
    login: str
    role: Role
    is_active: bool
    telegram_id: int | None = None
    telegram_username: str | None = None
    support_enabled: bool = False


class ManagedUserResponse(BaseModel):
    id: UUID
    telegram_id: int
    username: str | None
    is_blocked: bool
    device_count: int
    last_membership_check: datetime | None


class SourceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    github_url: str = Field(min_length=15, max_length=2048)


class SourceResponse(BaseModel):
    id: UUID
    name: str
    github_url: str
    is_enabled: bool
    last_success_at: datetime | None
    last_error: str | None
    content_hash: str | None
    quality_rating: float = 0
    checked_nodes: int = 0
    passed_nodes: int = 0
    rejected_nodes: int = 0
    new_nodes_last_run: int = 0
    rejection_reasons: dict[str, int] = Field(default_factory=dict)


class NodeResponse(BaseModel):
    id: UUID
    protocol: str
    host: str
    port: int
    state: NodeState
    score: float
    avg_latency_ms: float | None
    success_checks: int
    failed_checks: int
    source_id: UUID
    region: str
    region_emoji: str
    network_profile: str
    network_label: str
    network_emoji: str
    profile_priority: int
    probe_stage: str | None = None
    probe_throughput_kbps: float | None = None
    probe_error: str | None = None
    probe_checked_at: datetime | None = None
    probe_grace_until: datetime | None = None


class NodeProbeAttemptResponse(BaseModel):
    stage: str
    failure_class: str
    http_successes: int
    http_attempts: int
    latency_ms: float | None = None
    throughput_kbps: float | None = None
    error: str | None = None
    checked_at: datetime


class PoolPolicyPayload(BaseModel):
    vless_reality_limit: int = Field(default=15, ge=0, le=100)
    vless_ws_limit: int = Field(default=10, ge=0, le=100)
    vless_other_limit: int = Field(default=10, ge=0, le=100)
    hysteria2_limit: int = Field(default=15, ge=0, le=100)
    tuic_limit: int = Field(default=10, ge=0, le=100)
    trojan_limit: int = Field(default=20, ge=0, le=100)
    shadowsocks_limit: int = Field(default=10, ge=0, le=100)
    vmess_limit: int = Field(default=10, ge=0, le=100)


class PoolPolicyResponse(PoolPolicyPayload):
    updated_at: datetime | None = None


class ChannelCreate(BaseModel):
    chat_id: int
    title: str = Field(min_length=1, max_length=255)
    username: str | None = Field(default=None, max_length=255)


class ChannelResponse(BaseModel):
    id: UUID
    chat_id: int
    title: str
    username: str | None
    is_active: bool


class DashboardResponse(BaseModel):
    active_nodes: int
    quarantined_nodes: int
    average_ping: float | None
    active_users: int
    sources_with_errors: int
    required_channels: int


class MetricSnapshotResponse(BaseModel):
    active_nodes: int
    quarantined_nodes: int
    average_ping_ms: float | None
    check_success_rate: float | None
    created_at: datetime


class LandingEventPayload(BaseModel):
    token: str = Field(min_length=16, max_length=256)
    event_type: str = Field(pattern="^(site_visit|link_copy|happ_launch|setup_confirmed)$")


class InternalEventPayload(BaseModel):
    event_type: str = Field(pattern="^(bot_start|vpn_issued|donation_open)$")


class PiarFlowWebhookPayload(BaseModel):
    tg_user_id: int | None = None
    offer_link: str | None = Field(default=None, max_length=2048)
    status: str | None = Field(default=None, max_length=32)
    chat_id: int | None = None
    bot_id: int | None = None
    test: bool = False


class SupportTicketCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    category: str = Field(default="other", pattern="^(connection|happ|speed|payment|other)$")
    telegram_file_id: str | None = Field(default=None, max_length=512)


class SupportReplyPayload(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class AdminUserLookup(BaseModel):
    query: str = Field(min_length=1, max_length=128)


class BroadcastButtonPayload(BaseModel):
    text: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=5, max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https", "tg"} or (parsed.scheme != "tg" and not parsed.netloc):
            raise ValueError("Разрешены только ссылки http, https и tg")
        return value.strip()


class BroadcastCreate(BaseModel):
    client_request_id: UUID
    segment: str = Field(pattern="^(active|all|with_devices|without_devices)$")
    text_html: str = Field(default="", max_length=4096)
    photo_file_id: str | None = Field(default=None, max_length=512)
    buttons: list[BroadcastButtonPayload] = Field(default_factory=list, max_length=6)


class AnalyticsDayResponse(BaseModel):
    date: str
    bot_starts: int = 0
    site_visits: int = 0
    happ_launches: int = 0
    link_copies: int = 0
    setup_confirmed: int = 0
    vpn_issued: int = 0
    subscription_opens: int = 0


class AnalyticsCohortResponse(BaseModel):
    date: str
    users: int
    d0: float | None = None
    d1: float | None = None
    d3: float | None = None
    d7: float | None = None


class PiarFlowDailyResponse(BaseModel):
    date: date
    sold_subs: int
    earned: float


class PiarFlowAnalyticsResponse(BaseModel):
    enabled: bool
    provider_active: bool | None = None
    username: str | None = None
    max_sponsors: int = 0
    reset_time: int = 0
    sold_subs: int = 0
    not_counted: int = 0
    earned: float = 0
    last_synced_at: datetime | None = None
    last_error: str | None = None
    barrier_users: int = 0
    task_users: int = 0
    check_users: int = 0
    completed_users: int = 0
    pending_users: int = 0
    deferred_users: int = 0
    unsubscribed_users: int = 0
    check_attempts: int = 0
    average_checks_to_complete: float = 0
    api_errors: int = 0
    no_inventory: int = 0
    unsubscribe_events: int = 0
    revoked_devices: int = 0
    check_conversion: float = 0
    completion_conversion: float = 0
    days: list[PiarFlowDailyResponse] = Field(default_factory=list)


class AnalyticsResponse(BaseModel):
    total_bot_users: int
    new_bot_users: int
    known_bot_blocks: int
    active_users_1d: int
    active_users_7d: int
    active_users_30d: int
    active_devices: int
    funnel_bot_users: int
    funnel_vpn_users: int
    funnel_site_users: int
    funnel_happ_users: int
    funnel_subscription_users: int
    bot_starts: int
    unique_site_visitors: int
    happ_launches: int
    link_copies: int
    setup_confirmed: int
    vpn_issued: int
    subscription_opens: int
    donation_opens: int
    donation_supporters: int
    donation_stars_count: int
    donation_stars_total: int
    donation_ton_count: int
    donation_ton_total: float
    days: list[AnalyticsDayResponse]
    cohorts: list[AnalyticsCohortResponse]
    piarflow: PiarFlowAnalyticsResponse


class StarDonationIntent(BaseModel):
    amount: int = Field(ge=1, le=10000)


class StarDonationPreCheckout(BaseModel):
    invoice_payload: str = Field(min_length=1, max_length=128)
    currency: str = Field(pattern="^XTR$")
    total_amount: int = Field(ge=1, le=10000)


class StarDonationComplete(StarDonationPreCheckout):
    telegram_payment_charge_id: str = Field(min_length=1, max_length=255)
    provider_payment_charge_id: str | None = Field(default=None, max_length=255)


class TonDonationPrepare(BaseModel):
    amount: Decimal = Field(ge=Decimal("0.1"), le=Decimal("100"), decimal_places=3)


class BotUserRequest(BaseModel):
    telegram_id: int
    username: str | None = None


class DeviceCreate(BaseModel):
    label: str = Field(default="Устройство", min_length=1, max_length=80)


class DeviceUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=80)


class DeviceResponse(BaseModel):
    id: UUID
    slot: int
    label: str
    token_hint: str
    is_revoked: bool
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    subscription_url: str | None = None
