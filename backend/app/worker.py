import asyncio
import logging
import socket
import ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, select

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.models import MetricSnapshot, Node, NodeState, Source
from app.models import TelegramUser
from app.services.alerts import notify_admins, should_alert
from app.services.github import refresh_source
from app.services.health import check_active_nodes
from app.services.telegram import has_required_memberships
from app.services.broadcasts import process_broadcasts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("apscheduler").setLevel(logging.WARNING)
settings = get_settings()


def refresh_all_sources() -> None:
    with SessionLocal() as db:
        sources = db.scalars(select(Source).where(Source.is_enabled.is_(True))).all()
        for source in sources:
            run = refresh_source(db, source)
            logging.info("source=%s status=%s found=%s", source.id, run.status, run.found_count)
            if run.status in {"error", "guarded"}:
                if asyncio.run(should_alert(f"source:{source.id}:{run.status}")):
                    asyncio.run(notify_admins(f"Источник {source.name}: {run.message or run.status}"))


def health_check() -> None:
    with SessionLocal() as db:
        ok, total = check_active_nodes(db)
        active = db.scalar(select(func.count()).select_from(Node).where(Node.state == NodeState.ACTIVE)) or 0
        quarantined = db.scalar(select(func.count()).select_from(Node).where(Node.state == NodeState.QUARANTINED)) or 0
        average_ping = db.scalar(select(func.avg(Node.avg_latency_ms)).where(Node.state == NodeState.ACTIVE))
        previous = db.scalars(select(MetricSnapshot).order_by(MetricSnapshot.created_at.desc()).limit(1)).first()
        db.add(MetricSnapshot(active_nodes=active, quarantined_nodes=quarantined,
                              average_ping_ms=round(average_ping, 1) if average_ping else None,
                              check_success_rate=round(ok / total, 3) if total else None))
        db.commit()
        logging.info("health checks successful=%s total=%s", ok, total)
        if total and ok / total < 0.35 and asyncio.run(should_alert("pool-quality")):
            asyncio.run(notify_admins(f"Качество пула упало: успешно {ok} из {total} проверок."))
        if previous and previous.active_nodes >= 10 and active < previous.active_nodes * settings.node_drop_alert_ratio and asyncio.run(should_alert("node-drop")):
            asyncio.run(notify_admins(f"Активные ноды резко упали: {previous.active_nodes} → {active}."))


def _tls_days_left(hostname: str, port: int) -> int:
    context = ssl.create_default_context()
    with socket.create_connection((hostname, port), timeout=8) as sock:
        with context.wrap_socket(sock, server_hostname=hostname) as secured:
            expires = datetime.strptime(secured.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (expires - datetime.now(timezone.utc)).days


def infrastructure_check() -> None:
    """Monitor the locally reachable API, public Nginx route, and TLS certificate."""
    with SessionLocal() as db:
        stale_before = datetime.now(timezone.utc) - timedelta(minutes=settings.source_refresh_minutes * 3)
        for source in db.scalars(select(Source).where(Source.is_enabled.is_(True))).all():
            if source.last_success_at is None or source.last_success_at < stale_before:
                if asyncio.run(should_alert(f"source-stale:{source.id}")):
                    asyncio.run(notify_admins(f"Источник {source.name} давно не обновлялся."))
    try:
        with httpx.Client(timeout=10) as client:
            api_response = client.get(f"{settings.backend_internal_url.rstrip('/')}/health")
            public_response = client.get(f"{settings.public_base_url.rstrip('/')}/health")
        if api_response.status_code != 200 and asyncio.run(should_alert("api-unavailable")):
            asyncio.run(notify_admins("API недоступен из воркера."))
        if public_response.status_code != 200 and asyncio.run(should_alert("nginx-unavailable")):
            asyncio.run(notify_admins("Nginx или публичный маршрут API недоступен."))
    except httpx.HTTPError as exc:
        if asyncio.run(should_alert("public-api-unavailable")):
            asyncio.run(notify_admins(f"Проверка API/Nginx не прошла: {exc}"))
    parsed = urlparse(settings.public_base_url)
    if parsed.scheme == "https" and parsed.hostname:
        try:
            left = _tls_days_left(parsed.hostname, parsed.port or 443)
            if left < settings.tls_alert_days and asyncio.run(should_alert("tls-expiring")):
                asyncio.run(notify_admins(f"TLS-сертификат {parsed.hostname} истекает через {left} дн."))
        except OSError as exc:
            if asyncio.run(should_alert("tls-check-failed")):
                asyncio.run(notify_admins(f"Не удалось проверить TLS-сертификат: {exc}"))


def revalidate_memberships() -> None:
    with SessionLocal() as db:
        users = db.scalars(select(TelegramUser).where(TelegramUser.is_blocked.is_(False))).all()
        failures = 0
        for user in users:
            if not asyncio.run(has_required_memberships(db, user)):
                failures += 1
        logging.info("membership validation users=%s failed=%s", len(users), failures)


if __name__ == "__main__":
    # The API usually creates the schema first; this keeps a fresh Compose
    # deployment race-free when the scheduler starts before the API listener.
    Base.metadata.create_all(bind=engine)
    refresh_all_sources()
    health_check()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(refresh_all_sources, "interval", minutes=settings.source_refresh_minutes, id="sources", max_instances=1, coalesce=True)
    scheduler.add_job(health_check, "interval", minutes=settings.health_check_minutes, id="health", max_instances=1, coalesce=True)
    scheduler.add_job(revalidate_memberships, "interval", hours=settings.membership_check_hours, id="memberships", max_instances=1, coalesce=True)
    scheduler.add_job(infrastructure_check, "interval", minutes=settings.alert_check_minutes, id="infrastructure", max_instances=1, coalesce=True)
    scheduler.add_job(process_broadcasts, "interval", seconds=5, id="broadcasts", max_instances=1, coalesce=True)
    scheduler.start()
