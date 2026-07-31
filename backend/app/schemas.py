from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

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


class ManagedAdminResponse(BaseModel):
    id: UUID
    login: str
    role: Role
    is_active: bool


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
    event_type: str = Field(pattern="^(site_visit|happ_launch)$")


class InternalEventPayload(BaseModel):
    event_type: str = Field(pattern="^(bot_start|vpn_issued)$")


class AnalyticsDayResponse(BaseModel):
    date: str
    bot_starts: int = 0
    site_visits: int = 0
    happ_launches: int = 0
    vpn_issued: int = 0
    subscription_opens: int = 0


class AnalyticsResponse(BaseModel):
    total_bot_users: int
    new_bot_users: int
    known_bot_blocks: int
    bot_starts: int
    unique_site_visitors: int
    happ_launches: int
    vpn_issued: int
    subscription_opens: int
    days: list[AnalyticsDayResponse]


class BotUserRequest(BaseModel):
    telegram_id: int
    username: str | None = None


class DeviceCreate(BaseModel):
    label: str = Field(default="Устройство", min_length=1, max_length=80)


class DeviceResponse(BaseModel):
    id: UUID
    slot: int
    label: str
    token_hint: str
    is_revoked: bool
    subscription_url: str | None = None
