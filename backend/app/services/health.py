import socket
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Node, NodeState
from app.services.scoring import refresh_state


def check_node(node: Node, timeout_seconds: float = 7.0) -> bool:
    started = time.perf_counter()
    try:
        with socket.create_connection((node.host, node.port), timeout=timeout_seconds):
            elapsed = (time.perf_counter() - started) * 1000
    except OSError:
        node.failed_checks += 1
        node.consecutive_failures += 1
        node.last_checked_at = datetime.now(timezone.utc)
        refresh_state(node)
        return False
    node.success_checks += 1
    node.consecutive_failures = 0
    node.avg_latency_ms = elapsed if node.avg_latency_ms is None else round(node.avg_latency_ms * 0.7 + elapsed * 0.3, 2)
    node.last_checked_at = datetime.now(timezone.utc)
    refresh_state(node)
    return True


def check_active_nodes(db: Session) -> tuple[int, int]:
    nodes = db.scalars(select(Node).where(Node.state != NodeState.REMOVED)).all()
    ok = sum(1 for node in nodes if check_node(node))
    db.commit()
    return ok, len(nodes)

