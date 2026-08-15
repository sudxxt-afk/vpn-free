import enum
import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UUID, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def uuid_column() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Role(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    VIEWER = "viewer"


class NodeState(str, enum.Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DEGRADED = "degraded"
    QUARANTINED = "quarantined"
    REMOVED = "removed"


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[uuid.UUID] = uuid_column()
    login: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.OWNER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    support_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TelegramUser(Base):
    __tablename__ = "telegram_users"
    id: Mapped[uuid.UUID] = uuid_column()
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    bot_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_membership_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("user_id", "slot", name="uq_device_slot"),)
    id: Mapped[uuid.UUID] = uuid_column()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), index=True)
    slot: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(80), default="Устройство")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_hint: Mapped[str] = mapped_column(String(12))
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SubscriptionCutover(Base):
    __tablename__ = "subscription_cutovers"
    id: Mapped[uuid.UUID] = uuid_column()
    cutover_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(40))
    retired_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RetiredSubscription(Base):
    """Recognises an old HAPP URL after its Device row has been removed."""
    __tablename__ = "retired_subscriptions"
    id: Mapped[uuid.UUID] = uuid_column()
    original_device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), index=True)
    slot: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_hint: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str] = mapped_column(String(40), index=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    original_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SubscriptionRestoration(Base):
    """One-time restoration marker for a forced subscription cutover."""
    __tablename__ = "subscription_restorations"
    __table_args__ = (UniqueConstraint("user_id", "campaign_key", name="uq_subscription_restoration"),)
    id: Mapped[uuid.UUID] = uuid_column()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), nullable=True)
    campaign_key: Mapped[str] = mapped_column(String(80), index=True)
    restored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[uuid.UUID] = uuid_column()
    name: Mapped[str] = mapped_column(String(120))
    github_url: Mapped[str] = mapped_column(Text, unique=True)
    raw_url: Mapped[str] = mapped_column(Text, unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_anomaly_count: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceQuality(Base):
    __tablename__ = "source_qualities"
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    checked_nodes: Mapped[int] = mapped_column(Integer, default=0)
    passed_nodes: Mapped[int] = mapped_column(Integer, default=0)
    rejected_nodes: Mapped[int] = mapped_column(Integer, default=0)
    pass_rate: Mapped[float] = mapped_column(Float, default=0.0)
    new_nodes_last_run: Mapped[int] = mapped_column(Integer, default=0)
    rejection_reasons_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SourceRun(Base):
    __tablename__ = "source_runs"
    id: Mapped[uuid.UUID] = uuid_column()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    found_count: Mapped[int] = mapped_column(Integer, default=0)
    published_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_node_fingerprint"),)
    id: Mapped[uuid.UUID] = uuid_column()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    protocol: Mapped[str] = mapped_column(String(20), index=True)
    host: Mapped[str] = mapped_column(String(255), index=True)
    port: Mapped[int] = mapped_column(Integer)
    config_ciphertext: Mapped[str] = mapped_column(Text)
    state: Mapped[NodeState] = mapped_column(Enum(NodeState), default=NodeState.CANDIDATE, index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    avg_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    success_checks: Mapped[int] = mapped_column(Integer, default=0)
    failed_checks: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NodeProbeState(Base):
    """Latest end-to-end Xray result. A node is never published without it."""
    __tablename__ = "node_probe_states"
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    static_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    xray_started: Mapped[bool] = mapped_column(Boolean, default=False)
    http_successes: Mapped[int] = mapped_column(Integer, default=0)
    http_attempts: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    throughput_kbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class NodeProbeAttempt(Base):
    """Immutable probe observations used for noisy-network decisions."""
    __tablename__ = "node_probe_attempts"
    id: Mapped[uuid.UUID] = uuid_column()
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    failure_class: Mapped[str] = mapped_column(String(32), default="passed", index=True)
    http_successes: Mapped[int] = mapped_column(Integer, default=0)
    http_attempts: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    throughput_kbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class RequiredChannel(Base):
    __tablename__ = "required_channels"
    id: Mapped[uuid.UUID] = uuid_column()
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PoolPolicy(Base):
    """The published-pool limits, configured once by an administrator."""
    __tablename__ = "pool_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    vless_reality_limit: Mapped[int] = mapped_column(Integer, default=15)
    vless_ws_limit: Mapped[int] = mapped_column(Integer, default=10)
    vless_other_limit: Mapped[int] = mapped_column(Integer, default=10)
    hysteria2_limit: Mapped[int] = mapped_column(Integer, default=15)
    tuic_limit: Mapped[int] = mapped_column(Integer, default=10)
    trojan_limit: Mapped[int] = mapped_column(Integer, default=20)
    shadowsocks_limit: Mapped[int] = mapped_column(Integer, default=10)
    vmess_limit: Mapped[int] = mapped_column(Integer, default=10)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"
    id: Mapped[uuid.UUID] = uuid_column()
    active_nodes: Mapped[int] = mapped_column(Integer, default=0)
    quarantined_nodes: Mapped[int] = mapped_column(Integer, default=0)
    average_ping_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    check_success_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AnalyticsEvent(Base):
    """Privacy-preserving product events; subscription tokens are never stored."""
    __tablename__ = "analytics_events"
    id: Mapped[uuid.UUID] = uuid_column()
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    telegram_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("telegram_users.id", ondelete="SET NULL"), nullable=True, index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SubgramWebhookEvent(Base):
    """Immutable, deduplicated Subgram delivery without storing sponsor URLs."""
    __tablename__ = "subgram_webhook_events"
    webhook_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("telegram_users.id", ondelete="SET NULL"), nullable=True, index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ads_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    resource_key: Mapped[str] = mapped_column(String(80), index=True)
    link_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    subscribe_date: Mapped[date] = mapped_column(Date)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SubgramSponsorState(Base):
    """Latest event for one user and sponsor; used for immediate HAPP gating."""
    __tablename__ = "subgram_sponsor_states"
    __table_args__ = (UniqueConstraint("telegram_id", "resource_key", name="uq_subgram_sponsor_state"),)
    id: Mapped[uuid.UUID] = uuid_column()
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    resource_key: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), index=True)
    latest_webhook_id: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SubgramAccessState(Base):
    """One-time sponsor onboarding result plus independent revocation state."""
    __tablename__ = "subgram_access_states"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), primary_key=True)
    assigned_ads_json: Mapped[str] = mapped_column(Text, default="[]")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = uuid_column()
    admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(120))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id: Mapped[uuid.UUID] = uuid_column()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="new", index=True)
    claimed_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupportMessage(Base):
    __tablename__ = "support_messages"
    id: Mapped[uuid.UUID] = uuid_column()
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True)
    sender_type: Mapped[str] = mapped_column(String(16))
    admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BroadcastCampaign(Base):
    __tablename__ = "broadcast_campaigns"
    id: Mapped[uuid.UUID] = uuid_column()
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True, nullable=True)
    author_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True)
    segment: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    text_html: Mapped[str] = mapped_column(Text, default="")
    photo_file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    buttons_json: Mapped[str] = mapped_column(Text, default="[]")
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class BroadcastDelivery(Base):
    __tablename__ = "broadcast_deliveries"
    __table_args__ = (UniqueConstraint("campaign_id", "user_id", name="uq_broadcast_recipient"),)
    id: Mapped[uuid.UUID] = uuid_column()
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Donation(Base):
    """A voluntary contribution. It never unlocks VPN features."""

    __tablename__ = "donations"
    id: Mapped[uuid.UUID] = uuid_column()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), index=True)
    method: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    amount_stars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_nano: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    invoice_payload: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    public_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    sender_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
