import asyncio
import logging
import socket
import ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.base import BaseScheduler
from sqlalchemy import func, select

from app.config import get_settings, validate_runtime_settings
from app.database import Base, SessionLocal, bootstrap_lock, engine
from app.models import MetricSnapshot, Node, NodeProbeState, NodeState, Source, SourceQuality
from app.models import TelegramUser
from app.services.alerts import notify_admins, should_alert
from app.services.github import refresh_source
from app.services.health import check_active_nodes, purge_probe_history, verified_pool_conditions
from app.services.subgram_access import recheck_due_access_states
from app.services.telegram import has_required_memberships
from app.services.broadcasts import process_broadcasts
from app.services.donations import verify_pending_ton_donations

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("apscheduler").setLevel(logging.WARNING)
settings = get_settings()


def refresh_all_sources() -> None:
    with SessionLocal() as db:
        sources = db.scalars(select(Source).where(Source.is_enabled.is_(True))).all()
        priority_node_ids = []
        for source in sources:
            before = dict(db.execute(select(Node.id, Node.state).where(Node.source_id == source.id)).all())
            run = refresh_source(db, source)
            logging.info("source=%s status=%s found=%s", source.id, run.status, run.found_count)
            if run.status == "processed":
                after = db.execute(select(Node.id, Node.state).where(Node.source_id == source.id)).all()
                new_ids = [
                    node_id for node_id, state in after
                    if node_id not in before or (before[node_id] == NodeState.REMOVED and state != NodeState.REMOVED)
                ]
                priority_node_ids.extend(new_ids)
            else:
                new_ids = []
            quality = db.get(SourceQuality, source.id)
            if quality is None:
                quality = SourceQuality(source_id=source.id)
                db.add(quality)
            quality.new_nodes_last_run = len(new_ids)
            if run.status in {"error", "guarded"}:
                if asyncio.run(should_alert(f"source:{source.id}:{run.status}")):
                    asyncio.run(notify_admins(f"Источник {source.name}: {run.message or run.status}"))
        db.commit()
        if priority_node_ids:
            logging.info("new source nodes queued for immediate probe=%s", len(priority_node_ids))
            ok, total = check_active_nodes(db, priority_node_ids=priority_node_ids)
            logging.info("immediate source probe successful=%s total=%s", ok, total)


def health_check() -> None:
    with SessionLocal() as db:
        ok, total = check_active_nodes(db)
        verified = verified_pool_conditions()
        active = db.scalar(
            select(func.count()).select_from(Node)
            .join(Source, Source.id == Node.source_id)
            .join(NodeProbeState, NodeProbeState.node_id == Node.id)
            .where(*verified)
        ) or 0
        quarantined = db.scalar(
            select(func.count()).select_from(Node)
            .join(Source, Source.id == Node.source_id)
            .where(Source.is_enabled.is_(True), Node.state == NodeState.QUARANTINED)
        ) or 0
        average_ping = db.scalar(
            select(func.avg(Node.avg_latency_ms)).select_from(Node)
            .join(Source, Source.id == Node.source_id)
            .join(NodeProbeState, NodeProbeState.node_id == Node.id)
            .where(*verified)
        )
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


def cleanup_probe_history() -> None:
    with SessionLocal() as db:
        deleted = purge_probe_history(db)
        logging.info("probe history cleanup deleted=%s", deleted)


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


def revalidate_subgram_access() -> None:
    with SessionLocal() as db:
        checked, blocked = asyncio.run(recheck_due_access_states(db))
        logging.info("Subgram access recheck checked=%s blocked=%s", checked, blocked)


async def _notify_ton_donors(settled: list[tuple[int, float]]) -> None:
    if not settings.telegram_bot_token:
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        for telegram_id, amount in settled:
            try:
                await client.post(url, json={
                    "chat_id": telegram_id,
                    "parse_mode": "HTML",
                    "text": f"❤️ <b>Спасибо за поддержку!</b>\n\nTON-транзакция подтверждена: <b>{amount:g} TON</b>.",
                })
            except httpx.HTTPError:
                logging.exception("Unable to notify TON donor %s", telegram_id)


def ton_donation_check() -> None:
    if not settings.ton_donation_address.strip():
        return
    try:
        with SessionLocal() as db:
            settled = asyncio.run(verify_pending_ton_donations(db))
        if settled:
            asyncio.run(_notify_ton_donors(settled))
            logging.info("confirmed TON donations=%s", len(settled))
    except (httpx.HTTPError, ValueError):
        logging.exception("TON donation verification failed")


def configure_scheduler(scheduler: BaseScheduler) -> None:
    """Register recurring work without blocking queue processing at boot."""
    run_now = datetime.now(timezone.utc)
    scheduler.add_job(
        refresh_all_sources,
        "interval",
        minutes=settings.source_refresh_minutes,
        id="sources",
        max_instances=1,
        coalesce=True,
        jitter=max(0, settings.source_scheduler_jitter_seconds),
        next_run_time=run_now,
    )
    scheduler.add_job(
        health_check,
        "interval",
        minutes=settings.health_check_minutes,
        id="health",
        max_instances=1,
        coalesce=True,
        jitter=max(0, settings.health_scheduler_jitter_seconds),
        next_run_time=run_now + timedelta(minutes=2),
    )
    # check_active_nodes already gives due retries most of every batch. A second
    # scheduler job only waits on the same probe lock and duplicates work.
    scheduler.add_job(cleanup_probe_history, "interval", days=1, id="probe-history-cleanup", max_instances=1, coalesce=True)
    scheduler.add_job(revalidate_memberships, "interval", hours=settings.membership_check_hours, id="memberships", max_instances=1, coalesce=True)
    scheduler.add_job(revalidate_subgram_access, "interval", hours=1, id="subgram-access", max_instances=1, coalesce=True)
    scheduler.add_job(infrastructure_check, "interval", minutes=settings.alert_check_minutes, id="infrastructure", max_instances=1, coalesce=True)
    scheduler.add_job(process_broadcasts, "interval", seconds=5, id="broadcasts", max_instances=1, coalesce=True, next_run_time=run_now)
    scheduler.add_job(ton_donation_check, "interval", seconds=15, id="ton-donations", max_instances=1, coalesce=True)


if __name__ == "__main__":
    # API and worker may start together; the shared advisory lock serializes DDL.
    validate_runtime_settings(settings)
    with bootstrap_lock():
        Base.metadata.create_all(bind=engine)
    scheduler = BlockingScheduler(timezone="UTC")
    configure_scheduler(scheduler)
    scheduler.start()
