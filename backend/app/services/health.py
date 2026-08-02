import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt
from app.models import Node, NodeProbeState, NodeState, Source
from app.services.scoring import refresh_state
from app.services.xray_probe import ProbeResult, probe_config

settings = get_settings()


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


def _selected_nodes(db: Session) -> list[tuple[object, str]]:
    """Reserve half a cycle for the published pool and half for new candidates."""
    limit = max(2, settings.health_probe_batch_size)
    active_limit = limit // 2
    now = datetime.now(timezone.utc)
    recheck_before = now - timedelta(minutes=max(1, settings.health_probe_fresh_minutes // 2))
    active_priority = case(
        (NodeProbeState.last_checked_at < recheck_before, 0),
        (NodeProbeState.node_id.is_(None), 1),
        else_=2,
    )
    active = db.execute(
        select(Node.id, Node.config_ciphertext)
        .join(Source, Source.id == Node.source_id)
        .outerjoin(NodeProbeState, NodeProbeState.node_id == Node.id)
        .where(Source.is_enabled.is_(True), Node.state == NodeState.ACTIVE)
        .order_by(active_priority, NodeProbeState.last_checked_at.asc(), Node.score.desc())
        .limit(active_limit)
    ).all()
    other = db.execute(
        select(Node.id, Node.config_ciphertext)
        .join(Source, Source.id == Node.source_id)
        .outerjoin(NodeProbeState, NodeProbeState.node_id == Node.id)
        .where(Source.is_enabled.is_(True), Node.state.in_([
            NodeState.CANDIDATE, NodeState.DEGRADED, NodeState.QUARANTINED,
        ]))
        .order_by(
            case((NodeProbeState.node_id.is_(None), 0), else_=1),
            NodeProbeState.last_checked_at.asc(),
            Node.score.desc(),
        )
        .limit(limit - len(active))
    ).all()
    return [(node_id, ciphertext) for node_id, ciphertext in [*active, *other]]


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
    if result.success:
        successful_steps = max(result.http_successes, settings.health_probe_required_successes)
        node.success_checks += successful_steps
        node.consecutive_failures = 0
        node.avg_latency_ms = (
            result.latency_ms if node.avg_latency_ms is None
            else round(node.avg_latency_ms * 0.7 + (result.latency_ms or node.avg_latency_ms) * 0.3, 2)
        )
        state.last_success_at = now
        refresh_state(node)
    else:
        node.failed_checks += max(1, result.http_attempts - result.http_successes)
        node.consecutive_failures += 1
        refresh_state(node)
        # Every hard gate is mandatory: partial connectivity is not publishable.
        node.state = NodeState.QUARANTINED


def check_active_nodes(db: Session) -> tuple[int, int]:
    selected = _selected_nodes(db)
    if not selected:
        return 0, 0
    ok = 0
    stages: Counter[str] = Counter()
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
            ok += int(result.success)
            stages[result.stage] += 1
    logging.info("xray probe batch passed=%s total=%s stages=%s", ok, len(selected), dict(stages))
    return ok, len(selected)
