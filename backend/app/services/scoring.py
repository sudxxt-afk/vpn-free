from app.models import Node, NodeState


def calculate_score(node: Node) -> float:
    total = node.success_checks + node.failed_checks
    availability = node.success_checks / total if total else 0.0
    availability_points = availability * 30
    stability_points = max(0.0, 20 - node.consecutive_failures * 6)
    if node.avg_latency_ms is None:
        latency_points = 0.0
    else:
        latency_points = max(0.0, 20 * (1 - min(node.avg_latency_ms, 1200) / 1200))
    throughput_points = 10.0 if node.success_checks >= 2 else 0.0
    source_points = 10.0
    freshness_points = 5.0
    return round(availability_points + stability_points + latency_points + throughput_points + source_points + freshness_points, 2)


def refresh_state(node: Node) -> None:
    node.score = calculate_score(node)
    if node.consecutive_failures >= 3 or (node.success_checks >= 3 and node.score < 55):
        node.state = NodeState.QUARANTINED
    elif node.success_checks >= 2 and node.score >= 70:
        node.state = NodeState.ACTIVE
    elif node.success_checks:
        node.state = NodeState.DEGRADED
    else:
        node.state = NodeState.CANDIDATE

