import logging
import threading
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import case, or_, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt
from app.models import Node, NodeProbeState, NodeState, Source, SourceQuality
from app.services.scoring import calculate_score, refresh_state
from app.services.xray_probe import ProbeResult, probe_config

settings = get_settings()
_probe_cycle_lock = threading.Lock()

REJECTION_LABELS = {
    "static": "Некорректная конфигурация",
    "xray": "Xray не запустился",
    "http": "Нет доступа к тестовым сайтам",
    "throughput": "Не прошла проверка скорости",
    "internal": "Внутренняя ошибка проверки",
}


def normalize_node_states(db: Session) -> tuple[int, int]:
    """Make ACTIVE mean a recent complete Xray pass, never a legacy TCP check."""
    fresh_after = datetime.now(timezone.utc) - timedelta(minutes=settings.health_probe_fresh_minutes)
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
        .where(
            Node.state == NodeState.ACTIVE,
            or_(
                Source.is_enabled.is_(False),
                NodeProbeState.stage != "passed",
                NodeProbeState.last_success_at.is_(None),
                NodeProbeState.last_success_at < fresh_after,
            ),
        )
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
        passed = sum(1 for _node, probe in rows if probe.stage == "passed")
        pass_rate = passed / checked if checked else 0.0
        reasons = Counter(
            REJECTION_LABELS.get(probe.stage, probe.stage)
            for _node, probe in rows if probe.stage != "passed"
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


def _probe(raw: str) -> ProbeResult:
    try:
        return probe_config(
            decrypt(raw),
            xray_binary=settings.xray_binary,
            urls=settings.probe_urls,
            required_successes=settings.health_probe_required_successes,
            speed_url=settings.health_probe_speed_url,
            min_speed_kbps=settings.health_probe_min_speed_kbps,
            timeout_seconds=settings.health_probe_timeout_seconds,
        )
    except Exception as exc:  # One corrupt row must not abort the entire batch.
        return ProbeResult(False, "internal", False, False, error=f"{type(exc).__name__}: {exc}"[:500])


def _selected_nodes(db: Session, priority_node_ids: list[UUID] | None = None) -> list[tuple[UUID, str]]:
    """Put freshly imported nodes first, then balance rechecks and backlog."""
    limit = max(2, settings.health_probe_batch_size)
    priority: list[tuple[UUID, str]] = []
    if priority_node_ids:
        priority = list(db.execute(
            select(Node.id, Node.config_ciphertext)
            .join(Source, Source.id == Node.source_id)
            .where(
                Source.is_enabled.is_(True),
                Node.id.in_(priority_node_ids),
                Node.state != NodeState.REMOVED,
            )
            .order_by(Node.first_seen_at.desc())
            .limit(limit)
        ).all())
    selected_ids = [node_id for node_id, _ in priority]
    remaining = limit - len(priority)
    if remaining <= 0:
        return priority
    active_limit = remaining // 2
    now = datetime.now(timezone.utc)
    recheck_before = now - timedelta(minutes=max(1, settings.health_probe_fresh_minutes // 2))
    active_priority = case(
        (NodeProbeState.last_checked_at < recheck_before, 0),
        (NodeProbeState.node_id.is_(None), 1),
        else_=2,
    )
    active_query = (
        select(Node.id, Node.config_ciphertext)
        .join(Source, Source.id == Node.source_id)
        .outerjoin(NodeProbeState, NodeProbeState.node_id == Node.id)
        .outerjoin(SourceQuality, SourceQuality.source_id == Node.source_id)
        .where(Source.is_enabled.is_(True), Node.state == NodeState.ACTIVE)
        .order_by(
            active_priority,
            NodeProbeState.last_checked_at.asc(),
            SourceQuality.pass_rate.desc().nullslast(),
            Node.score.desc(),
        )
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
        .where(Source.is_enabled.is_(True), Node.state.in_([
            NodeState.CANDIDATE, NodeState.DEGRADED, NodeState.QUARANTINED,
        ]))
        .order_by(
            case((NodeProbeState.node_id.is_(None), 0), else_=1),
            case((Node.state == NodeState.CANDIDATE, 0), else_=1),
            SourceQuality.pass_rate.desc().nullslast(),
            Node.first_seen_at.desc(),
            NodeProbeState.last_checked_at.asc(),
            Node.score.desc(),
        )
        .limit(remaining - len(active))
    )
    if selected_ids:
        other_query = other_query.where(Node.id.not_in(selected_ids))
    other = db.execute(other_query).all()
    return [(node_id, ciphertext) for node_id, ciphertext in [*priority, *active, *other]]


def apply_probe_result(db: Session, node: Node, result: ProbeResult) -> None:
    now = datetime.now(timezone.utc)
    state = db.get(NodeProbeState, node.id)
    if state is None:
        # Discard legacy TCP-only counters. They must not grant publication rights.
        state = NodeProbeState(node_id=node.id)
        db.add(state)
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
    state.throughput_kbps = result.throughput_kbps
    state.last_error = result.error
    state.last_checked_at = now
    node.last_checked_at = now
    quality = db.get(SourceQuality, node.source_id)
    source_pass_rate = quality.pass_rate if quality and quality.checked_nodes else 0.5
    if result.success:
        successful_steps = max(result.http_successes, settings.health_probe_required_successes)
        node.success_checks += successful_steps
        node.consecutive_failures = 0
        node.avg_latency_ms = (
            result.latency_ms if node.avg_latency_ms is None
            else round(node.avg_latency_ms * 0.7 + (result.latency_ms or node.avg_latency_ms) * 0.3, 2)
        )
        state.last_success_at = now
        refresh_state(node, result.throughput_kbps, source_pass_rate)
    else:
        node.failed_checks += max(1, result.http_attempts - result.http_successes)
        node.consecutive_failures += 1
        refresh_state(node, result.throughput_kbps, source_pass_rate)
        # Every hard gate is mandatory: partial connectivity is not publishable.
        node.state = NodeState.QUARANTINED


def check_active_nodes(db: Session, priority_node_ids: list[UUID] | None = None) -> tuple[int, int]:
    with _probe_cycle_lock:
        normalize_node_states(db)
        selected = _selected_nodes(db, priority_node_ids)
        if not selected:
            return 0, 0
        ok = 0
        stages: Counter[str] = Counter()
        affected_sources: set[UUID] = set()
        with ThreadPoolExecutor(max_workers=max(1, settings.health_probe_concurrency)) as executor:
            futures = {executor.submit(_probe, ciphertext): node_id for node_id, ciphertext in selected}
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
        logging.info(
            "xray probe batch passed=%s total=%s priority=%s stages=%s",
            ok, len(selected), len({node_id for node_id, _ in selected} & set(priority_node_ids or [])), dict(stages),
        )
        return ok, len(selected)
