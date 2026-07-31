import asyncio
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, select

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.models import MetricSnapshot, Node, NodeState, Source
from app.models import TelegramUser
from app.services.alerts import notify_admins
from app.services.github import refresh_source
from app.services.health import check_active_nodes
from app.services.telegram import has_required_memberships

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
settings = get_settings()


def refresh_all_sources() -> None:
    with SessionLocal() as db:
        sources = db.scalars(select(Source).where(Source.is_enabled.is_(True))).all()
        for source in sources:
            run = refresh_source(db, source)
            logging.info("source=%s status=%s found=%s", source.id, run.status, run.found_count)
            if run.status in {"error", "guarded"}:
                asyncio.run(notify_admins(f"Источник {source.name}: {run.message or run.status}"))


def health_check() -> None:
    with SessionLocal() as db:
        ok, total = check_active_nodes(db)
        active = db.scalar(select(func.count()).select_from(Node).where(Node.state == NodeState.ACTIVE)) or 0
        quarantined = db.scalar(select(func.count()).select_from(Node).where(Node.state == NodeState.QUARANTINED)) or 0
        average_ping = db.scalar(select(func.avg(Node.avg_latency_ms)).where(Node.state == NodeState.ACTIVE))
        db.add(MetricSnapshot(active_nodes=active, quarantined_nodes=quarantined,
                              average_ping_ms=round(average_ping, 1) if average_ping else None,
                              check_success_rate=round(ok / total, 3) if total else None))
        db.commit()
        logging.info("health checks successful=%s total=%s", ok, total)
        if total and ok / total < 0.35:
            asyncio.run(notify_admins(f"Качество пула упало: успешно {ok} из {total} проверок."))


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
    scheduler.start()
