import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UUID, UniqueConstraint, func
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TelegramUser(Base):
    __tablename__ = "telegram_users"
    id: Mapped[uuid.UUID] = uuid_column()
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
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


class PartnerGate(Base):
    """The highest partner-access tier a Telegram user has completed."""
    __tablename__ = "partner_gates"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("telegram_users.id", ondelete="CASCADE"), primary_key=True)
    completed_tier: Mapped[int] = mapped_column(Integer, default=0)
    pending_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


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


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = uuid_column()
    admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(120))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
