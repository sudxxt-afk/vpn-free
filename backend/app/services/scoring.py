from app.models import Node, NodeState


def _latency_points(latency_ms: float | None) -> float:
    if latency_ms is None:
        return 0.0
    if latency_ms <= 150:
        return 25.0
    if latency_ms <= 300:
        return 25.0 - (latency_ms - 150) * 3 / 150
    if latency_ms <= 600:
        return 22.0 - (latency_ms - 300) * 8 / 300
    if latency_ms <= 1000:
        return 14.0 - (latency_ms - 600) * 10 / 400
    return max(0.0, 4.0 - (latency_ms - 1000) * 4 / 500)


def _throughput_points(throughput_kbps: float | None) -> float:
    if throughput_kbps is None or throughput_kbps < 128:
        return 0.0
    if throughput_kbps < 512:
        return 3.0 + (throughput_kbps - 128) * 3 / 384
    if throughput_kbps < 1000:
        return 6.0 + (throughput_kbps - 512) * 3 / 488
    if throughput_kbps < 3000:
        return 9.0 + (throughput_kbps - 1000) * 4 / 2000
    if throughput_kbps < 6000:
        return 13.0 + (throughput_kbps - 3000) * 2 / 3000
    return 15.0


def calculate_score(node: Node, throughput_kbps: float | None = None, source_pass_rate: float = 0.5) -> float:
    total = node.success_checks + node.failed_checks
    availability = node.success_checks / total if total else 0.0
    availability_points = availability * 30
    stability_points = max(0.0, 15 - node.consecutive_failures * 6)
    latency_points = _latency_points(node.avg_latency_ms)
    throughput_points = _throughput_points(throughput_kbps)
    source_points = max(0.0, min(source_pass_rate, 1.0)) * 10
    freshness_points = 5.0
    return round(availability_points + stability_points + latency_points + throughput_points + source_points + freshness_points, 2)


def refresh_state(node: Node, throughput_kbps: float | None = None, source_pass_rate: float = 0.5) -> None:
    node.score = calculate_score(node, throughput_kbps, source_pass_rate)
    if node.consecutive_failures >= 3 or (node.success_checks >= 3 and node.score < 55):
        node.state = NodeState.QUARANTINED
    elif node.success_checks >= 2 and node.score >= 70:
        node.state = NodeState.ACTIVE
    elif node.success_checks:
        node.state = NodeState.DEGRADED
    else:
        node.state = NodeState.CANDIDATE
