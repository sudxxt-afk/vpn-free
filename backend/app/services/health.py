import json
import logging
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from sqlalchemy import and_, case, delete, func, not_, or_, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt
from app.models import Node, NodeProbeAttempt, NodeProbeState, NodeState, Source, SourceQuality
from app.services.scoring import calculate_score, refresh_state
from app.services.xray_probe import ProbeResult, probe_config

settings = get_settings()
_probe_cycle_lock = threading.Lock()

REJECTION_LABELS = {
    "static": "Некорректная конфигурация",
    "xray": "Xray не запустился",
    "http": "Нет доступа к тестовым сайтам",
    "throughput": "Не прошла проверка скорости",
    "retrying": "Ожидает повторной проверки",
    "internal": "Внутренняя ошибка проверки",
}


def verified_pool_conditions(now: datetime | None = None) -> tuple:
    """Allow one short-lived network failure without publishing stale configs forever."""
    now = now or datetime.now(timezone.utc)
    fresh_after = now - timedelta(minutes=settings.health_probe_fresh_minutes)
    grace_after = now - timedelta(minutes=settings.health_failure_grace_minutes)
    return (
        Source.is_enabled.is_(True),
        Node.state == NodeState.ACTIVE,
        NodeProbeState.static_valid.is_(True),
        NodeProbeState.xray_started.is_(True),
        or_(
            and_(NodeProbeState.stage == "passed", NodeProbeState.last_success_at >= fresh_after),
            and_(NodeProbeState.stage == "retrying", NodeProbeState.last_success_at >= grace_after),
        ),
    )


def normalize_node_states(db: Session) -> tuple[int, int]:
    """Demote legacy and expired-grace nodes before selecting the next batch."""
    legacy_ids = (
        select(Node.id)
        .outerjoin(NodeProbeState, NodeProbeState.node_id == Node.id)
        .where(Node.state == NodeState.ACTIVE, NodeProbeState.node_id.is_(None))
    )
    legacy = db.execute(
        update(Node).where(Node.id.in_(legacy_ids)).values(
            state=NodeState.CANDIDATE,
            score=0,
            success_checks=0,
            failed_checks=0,
            consecutive_failures=0,
            avg_latency_ms=None,
            last_checked_at=None,
        )
    ).rowcount or 0
    stale_ids = (
        select(Node.id)
        .join(Source, Source.id == Node.source_id)
        .join(NodeProbeState, NodeProbeState.node_id == Node.id)
        .where(Node.state == NodeState.ACTIVE, not_(and_(*verified_pool_conditions())))
    )
    stale = db.execute(update(Node).where(Node.id.in_(stale_ids)).values(state=NodeState.DEGRADED)).rowcount or 0
    if legacy or stale:
        db.commit()
        logging.info("normalized node states legacy=%s stale=%s", legacy, stale)
    return legacy, stale


def refresh_source_qualities(db: Session, source_ids: set[UUID]) -> None:
    for source_id in source_ids:
        rows = db.execute(
            select(Node, NodeProbeState)
            .join(NodeProbeState, NodeProbeState.node_id == Node.id)
            .where(Node.source_id == source_id, Node.state != NodeState.REMOVED)
        ).all()
        checked = len(rows)
        passed = sum(1 for _node, probe in rows if probe.stage in {"passed", "retrying"})
        pass_rate = passed / checked if checked else 0.0
        reasons = Counter(
            REJECTION_LABELS.get(probe.stage, probe.stage)
            for _node, probe in rows if probe.stage not in {"passed", "retrying"}
        )
        quality = db.get(SourceQuality, source_id)
        if quality is None:
            quality = SourceQuality(source_id=source_id)
            db.add(quality)
        quality.checked_nodes = checked
        quality.passed_nodes = passed
        quality.rejected_nodes = checked - passed
        quality.pass_rate = round(pass_rate, 4)
        quality.rejection_reasons_json = json.dumps(dict(reasons.most_common(5)), ensure_ascii=False)
        for node, probe in rows:
            node.score = calculate_score(node, probe.throughput_kbps, pass_rate)
    db.commit()


def _probe(raw: str, *, urls: tuple[str, ...], speed_url: str | None) -> ProbeResult:
    try:
        return probe_config(
            decrypt(raw),
            xray_binary=settings.xray_binary,
            urls=urls,
            required_successes=settings.health_probe_required_successes,
            speed_url=speed_url,
            min_speed_kbps=settings.health_probe_min_speed_kbps,
            timeout_seconds=settings.health_probe_timeout_seconds,
        )
    except Exception as exc:  # One corrupt row must not abort the entire batch.
        return ProbeResult(False, "internal", False, False, error=f"{type(exc).__name__}: {exc}"[:500], failure_class="probe_infrastructure")


def _probe_targets() -> tuple[tuple[str, ...], str | None]:
    """Exclude an unhealthy control URL instead of blaming every external config."""
    healthy: list[str] = []
    try:
        with httpx.Client(timeout=settings.health_probe_timeout_seconds, follow_redirects=True) as client:
            for url in settings.probe_urls:
                try:
                    response = client.get(url, headers={"User-Agent": "ZazaVPN-Health-Control/1.0"})
                    response.raise_for_status()
                    healthy.append(url)
                except httpx.HTTPError:
                    logging.warning("probe control unavailable url=%s", urlsplit_host(url))
            speed_url: str | None = settings.health_probe_speed_url
            if speed_url:
                try:
                    with client.stream("GET", speed_url, headers={"User-Agent": "ZazaVPN-Health-Control/1.0"}) as response:
                        response.raise_for_status()
                except httpx.HTTPError:
                    logging.warning("speed control unavailable; skipping speed gate")
                    speed_url = None
    except httpx.HTTPError:
        healthy = []
        speed_url = None
    if len(healthy) < settings.health_probe_required_successes:
        return (), None
    return tuple(healthy), speed_url


def urlsplit_host(url: str) -> str:
    return url.split("/", 3)[2] if "://" in url else url


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def _selected_nodes(db: Session, priority_node_ids: list[UUID] | None = None) -> list[tuple[UUID, str]]:
    """Put imports and due retries first, then balance healthy and backlog probes."""
    limit = max(2, settings.health_probe_batch_size)
    priority: list[tuple[UUID, str]] = []
    if priority_node_ids:
        priority = list(db.execute(
            select(Node.id, Node.config_ciphertext)
            .join(Source, Source.id == Node.source_id)
            .where(Source.is_enabled.is_(True), Node.id.in_(priority_node_ids), Node.state != NodeState.REMOVED)
            .order_by(Node.first_seen_at.desc()).limit(limit)
        ).all())
    selected_ids = [node_id for node_id, _ in priority]
    remaining = limit - len(priority)
    if remaining <= 0:
        return priority

    retry_before = datetime.now(timezone.utc) - timedelta(seconds=settings.health_retry_seconds)
    confirmation_limit = min(remaining, max(1, settings.health_probe_batch_size * 2 // 3))
    retry_query = (
        select(Node.id, Node.config_ciphertext)
        .join(Source, Source.id == Node.source_id)
        .join(NodeProbeState, NodeProbeState.node_id == Node.id)
        .where(
            Source.is_enabled.is_(True),
            Node.state != NodeState.REMOVED,
            NodeProbeState.last_checked_at < retry_before,
            or_(
                NodeProbeState.stage == "retrying",
                and_(NodeProbeState.stage == "passed", Node.state.in_([NodeState.CANDIDATE, NodeState.DEGRADED])),
            ),
        )
        .order_by(NodeProbeState.last_checked_at.asc()).limit(confirmation_limit)
    )
    if selected_ids:
        retry_query = retry_query.where(Node.id.not_in(selected_ids))
    retries = db.execute(retry_query).all()
    selected_ids.extend(node_id for node_id, _ in retries)
    remaining -= len(retries)
    if remaining <= 0:
        return [*priority, *retries]

    active_limit = remaining // 2
    recheck_before = datetime.now(timezone.utc) - timedelta(minutes=max(1, settings.health_probe_fresh_minutes // 2))
    active_query = (
        select(Node.id, Node.config_ciphertext)
        .join(Source, Source.id == Node.source_id)
        .outerjoin(NodeProbeState, NodeProbeState.node_id == Node.id)
        .outerjoin(SourceQuality, SourceQuality.source_id == Node.source_id)
        .where(Source.is_enabled.is_(True), Node.state == NodeState.ACTIVE)
        .order_by(case((NodeProbeState.last_checked_at < recheck_before, 0), else_=1), NodeProbeState.last_checked_at.asc(), SourceQuality.pass_rate.desc().nullslast(), Node.score.desc())
        .limit(active_limit)
    )
    if selected_ids:
        active_query = active_query.where(Node.id.not_in(selected_ids))
    active = db.execute(active_query).all()
    selected_ids.extend(node_id for node_id, _ in active)

    other_query = (
        select(Node.id, Node.config_ciphertext)
        .join(Source, Source.id == Node.source_id)
        .outerjoin(NodeProbeState, NodeProbeState.node_id == Node.id)
        .outerjoin(SourceQuality, SourceQuality.source_id == Node.source_id)
        .where(Source.is_enabled.is_(True), Node.state.in_([NodeState.CANDIDATE, NodeState.DEGRADED, NodeState.QUARANTINED]))
        .order_by(case((NodeProbeState.node_id.is_(None), 0), else_=1), case((Node.state == NodeState.CANDIDATE, 0), else_=1), SourceQuality.pass_rate.desc().nullslast(), Node.first_seen_at.desc(), NodeProbeState.last_checked_at.asc(), Node.score.desc())
        .limit(remaining - len(active))
    )
    if selected_ids:
        other_query = other_query.where(Node.id.not_in(selected_ids))
    return [*priority, *retries, *active, *db.execute(other_query).all()]


def retry_node_ids(db: Session, limit: int | None = None) -> list[UUID]:
    retry_before = datetime.now(timezone.utc) - timedelta(seconds=settings.health_retry_seconds)
    rows = db.scalars(
        select(Node.id)
        .join(Source, Source.id == Node.source_id)
        .join(NodeProbeState, NodeProbeState.node_id == Node.id)
        .where(
            Source.is_enabled.is_(True), Node.state != NodeState.REMOVED,
            NodeProbeState.last_checked_at < retry_before,
            or_(
                NodeProbeState.stage == "retrying",
                and_(NodeProbeState.stage == "passed", Node.state.in_([NodeState.CANDIDATE, NodeState.DEGRADED])),
            ),
        )
        .order_by(NodeProbeState.last_checked_at.asc()).limit(limit or max(2, settings.health_probe_batch_size // 3))
    ).all()
    return list(rows)


def _speed_probe_due(db: Session, node_id: UUID) -> bool:
    fresh_after = datetime.now(timezone.utc) - timedelta(hours=settings.health_probe_speed_fresh_hours)
    last_speed = db.scalar(
        select(func.max(NodeProbeAttempt.checked_at)).where(
            NodeProbeAttempt.node_id == node_id,
            NodeProbeAttempt.throughput_kbps.is_not(None),
        )
    )
    return last_speed is None or last_speed < fresh_after


def _refresh_attempt_counters(db: Session, node: Node) -> None:
    attempts = list(db.scalars(
        select(NodeProbeAttempt)
        .where(NodeProbeAttempt.node_id == node.id, NodeProbeAttempt.failure_class != "probe_infrastructure")
        .order_by(NodeProbeAttempt.checked_at.desc()).limit(12)
    ))
    node.success_checks = sum(attempt.stage == "passed" for attempt in attempts)
    node.failed_checks = len(attempts) - node.success_checks
    node.consecutive_failures = 0
    for attempt in attempts:
        if attempt.stage == "passed":
            break
        node.consecutive_failures += 1


def _has_temporal_passes(db: Session, node_id: UUID, now: datetime) -> bool:
    passes = list(db.scalars(
        select(NodeProbeAttempt.checked_at)
        .where(NodeProbeAttempt.node_id == node_id, NodeProbeAttempt.stage == "passed")
        .order_by(NodeProbeAttempt.checked_at.desc()).limit(2)
    ))
    return len(passes) == 2 and passes[0] - passes[1] >= timedelta(seconds=settings.health_min_pass_interval_seconds)


def apply_probe_result(db: Session, node: Node, result: ProbeResult) -> None:
    now = datetime.now(timezone.utc)
    state = db.get(NodeProbeState, node.id)
    if state is None:
        state = NodeProbeState(node_id=node.id)
        db.add(state)
    was_active = node.state == NodeState.ACTIVE
    previous_success = _utc(state.last_success_at)
    had_attempts = db.scalar(select(func.count()).select_from(NodeProbeAttempt).where(NodeProbeAttempt.node_id == node.id)) > 0
    db.add(NodeProbeAttempt(
        node_id=node.id, stage=result.stage, failure_class=result.failure_class,
        http_successes=result.http_successes, http_attempts=result.http_attempts,
        latency_ms=result.latency_ms, throughput_kbps=result.throughput_kbps,
        error=result.error, checked_at=now,
    ))
    if result.failure_class == "probe_infrastructure":
        return
    if not had_attempts:
        node.success_checks = 0
        node.failed_checks = 0
        node.consecutive_failures = 0
        node.avg_latency_ms = None
        node.state = NodeState.CANDIDATE

    state.stage = result.stage
    state.static_valid = result.static_valid
    state.xray_started = result.xray_started
    state.http_successes = result.http_successes
    state.http_attempts = result.http_attempts
    state.latency_ms = result.latency_ms
    if result.throughput_kbps is not None:
        state.throughput_kbps = result.throughput_kbps
    state.last_error = result.error
    state.last_checked_at = now
    node.last_checked_at = now

    _refresh_attempt_counters(db, node)
    quality = db.get(SourceQuality, node.source_id)
    source_pass_rate = quality.pass_rate if quality and quality.checked_nodes else 0.5
    if result.success:
        state.last_success_at = now
        node.avg_latency_ms = result.latency_ms if node.avg_latency_ms is None else round(node.avg_latency_ms * 0.7 + (result.latency_ms or node.avg_latency_ms) * 0.3, 2)
        refresh_state(node, state.throughput_kbps, source_pass_rate)
        previous_pass_is_independent = previous_success and previous_success <= now - timedelta(seconds=settings.health_min_pass_interval_seconds)
        if not _has_temporal_passes(db, node.id, now) and not (was_active and previous_pass_is_independent):
            node.state = NodeState.DEGRADED
        return

    if result.failure_class in {"config", "unsupported"}:
        node.state = NodeState.QUARANTINED
        return
    if result.failure_class == "network":
        grace_after = now - timedelta(minutes=settings.health_failure_grace_minutes)
        if was_active and previous_success and previous_success >= grace_after and node.consecutive_failures == 1:
            state.stage = "retrying"
            node.state = NodeState.ACTIVE
        elif node.consecutive_failures >= 2:
            node.state = NodeState.QUARANTINED
        else:
            node.state = NodeState.QUARANTINED if node.success_checks == 0 else NodeState.DEGRADED
        return
    node.state = NodeState.QUARANTINED


def purge_probe_history(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.health_probe_history_days)
    deleted = db.execute(delete(NodeProbeAttempt).where(NodeProbeAttempt.checked_at < cutoff)).rowcount or 0
    db.commit()
    return deleted


def check_active_nodes(db: Session, priority_node_ids: list[UUID] | None = None) -> tuple[int, int]:
    with _probe_cycle_lock:
        normalize_node_states(db)
        urls, speed_url = _probe_targets()
        if not urls:
            logging.warning("health probe skipped: fewer than %s healthy control URLs", settings.health_probe_required_successes)
            return 0, 0
        selected = _selected_nodes(db, priority_node_ids)
        if not selected:
            return 0, 0
        work = [(node_id, ciphertext, speed_url if _speed_probe_due(db, node_id) else None) for node_id, ciphertext in selected]
        ok = 0
        stages: Counter[str] = Counter()
        affected_sources: set[UUID] = set()
        with ThreadPoolExecutor(max_workers=max(1, settings.health_probe_concurrency)) as executor:
            futures = {executor.submit(_probe, ciphertext, urls=urls, speed_url=node_speed_url): node_id for node_id, ciphertext, node_speed_url in work}
            for future in as_completed(futures):
                node_id = futures[future]
                result = future.result()
                node = db.get(Node, node_id)
                if node is None or node.state == NodeState.REMOVED:
                    continue
                apply_probe_result(db, node, result)
                db.commit()
                affected_sources.add(node.source_id)
                ok += int(result.success)
                stages[result.stage] += 1
        refresh_source_qualities(db, affected_sources)
        logging.info("xray probe batch passed=%s total=%s priority=%s stages=%s", ok, len(selected), len({node_id for node_id, _ in selected} & set(priority_node_ids or [])), dict(stages))
        return ok, len(selected)
