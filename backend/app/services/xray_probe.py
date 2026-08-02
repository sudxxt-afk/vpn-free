import base64
import json
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

import httpx


SUPPORTED_NETWORKS = {"tcp", "raw", "ws", "grpc", "http", "h2", "xhttp", "splithttp", "httpupgrade", "kcp"}


class ProbeConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    success: bool
    stage: str
    static_valid: bool
    xray_started: bool
    http_successes: int = 0
    http_attempts: int = 0
    latency_ms: float | None = None
    throughput_kbps: float | None = None
    error: str | None = None


def _decode_b64(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except ValueError as exc:
        raise ProbeConfigError("invalid base64 payload") from exc


def _query_value(query: dict[str, list[str]], *names: str, default: str = "") -> str:
    for name in names:
        values = query.get(name)
        if values and values[0] != "":
            return values[0]
    return default


def _stream_settings(network: str, security: str, address: str, query: dict[str, list[str]]) -> dict:
    network = (network or "tcp").lower()
    if network not in SUPPORTED_NETWORKS:
        raise ProbeConfigError(f"unsupported transport: {network}")
    if network == "raw":
        network = "tcp"
    if network == "splithttp":
        network = "xhttp"
    stream: dict = {"network": network, "security": security or "none"}
    host = _query_value(query, "host")
    path = unquote(_query_value(query, "path", default="/"))
    header_type = _query_value(query, "headerType", "type", default="none")
    if network == "tcp":
        stream["tcpSettings"] = {"header": {"type": header_type}}
    elif network == "ws":
        settings: dict = {"path": path, "headers": {}}
        if host:
            settings["headers"]["Host"] = host
        if _query_value(query, "ed").isdigit():
            settings["maxEarlyData"] = int(_query_value(query, "ed"))
            settings["earlyDataHeaderName"] = _query_value(query, "eh", default="Sec-WebSocket-Protocol")
        stream["wsSettings"] = settings
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": unquote(_query_value(query, "serviceName", default=path.lstrip("/"))),
            "multiMode": _query_value(query, "mode").lower() in {"multi", "gun"},
        }
    elif network in {"http", "h2"}:
        stream["network"] = "http"
        stream["httpSettings"] = {"path": path, "host": [host] if host else []}
    elif network == "xhttp":
        settings = {"path": path}
        if host:
            settings["host"] = host
        mode = _query_value(query, "mode")
        if mode:
            settings["mode"] = mode
        stream["xhttpSettings"] = settings
    elif network == "httpupgrade":
        stream["httpupgradeSettings"] = {"path": path, "host": host}
    elif network == "kcp":
        stream["kcpSettings"] = {"header": {"type": header_type}}

    server_name = _query_value(query, "sni", "serverName", default=address)
    fingerprint = _query_value(query, "fp", default="chrome")
    alpn = [part.strip() for part in _query_value(query, "alpn").split(",") if part.strip()]
    if security == "tls":
        tls = {"serverName": server_name, "fingerprint": fingerprint, "allowInsecure": False}
        if alpn:
            tls["alpn"] = alpn
        stream["tlsSettings"] = tls
    elif security == "reality":
        public_key = _query_value(query, "pbk", "publicKey")
        if not public_key or not server_name or not fingerprint:
            raise ProbeConfigError("reality requires pbk/publicKey, sni and fp")
        stream["realitySettings"] = {
            "serverName": server_name,
            "fingerprint": fingerprint,
            "publicKey": public_key,
            "shortId": _query_value(query, "sid", "shortId"),
            "spiderX": unquote(_query_value(query, "spx", "spiderX", default="/")),
        }
    elif security not in {"", "none"}:
        raise ProbeConfigError(f"unsupported security: {security}")
    return stream


def _standard_outbound(raw: str, protocol: str) -> dict:
    parsed = urlsplit(raw)
    try:
        address, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ProbeConfigError("invalid endpoint") from exc
    if not address or not port:
        raise ProbeConfigError("missing endpoint")
    query = parse_qs(parsed.query)
    secret = unquote(parsed.username or "")
    if not secret:
        raise ProbeConfigError(f"{protocol} credential is empty")
    network = _query_value(query, "type", default="tcp")
    security = _query_value(query, "security", default="none").lower()
    stream = _stream_settings(network, security, address, query)
    if protocol == "vless":
        try:
            uuid.UUID(secret)
        except ValueError as exc:
            raise ProbeConfigError("vless id is not a UUID") from exc
        user = {"id": secret, "encryption": _query_value(query, "encryption", default="none")}
        flow = _query_value(query, "flow")
        if flow:
            user["flow"] = flow
        settings = {"vnext": [{"address": address, "port": port, "users": [user]}]}
    else:
        settings = {"servers": [{"address": address, "port": port, "password": secret}]}
    return {"protocol": protocol, "settings": settings, "streamSettings": stream, "tag": "proxy"}


def _vmess_outbound(raw: str) -> dict:
    payload = raw.split("://", 1)[1].split("#", 1)[0]
    try:
        data = json.loads(_decode_b64(payload).decode("utf-8"))
        address, port, user_id = str(data["add"]), int(data["port"]), str(data["id"])
        uuid.UUID(user_id)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeConfigError("invalid vmess payload") from exc
    query = {key: [str(value)] for key, value in data.items() if value is not None}
    network = str(data.get("net", "tcp"))
    security = "tls" if str(data.get("tls", "")).lower() in {"tls", "1", "true"} else "none"
    stream = _stream_settings(network, security, address, query)
    user = {"id": user_id, "alterId": int(data.get("aid", 0) or 0), "security": str(data.get("scy", "auto"))}
    return {
        "protocol": "vmess",
        "settings": {"vnext": [{"address": address, "port": port, "users": [user]}]},
        "streamSettings": stream,
        "tag": "proxy",
    }


def _shadowsocks_outbound(raw: str) -> dict:
    body = raw.split("://", 1)[1].split("#", 1)[0]
    parsed = urlsplit(raw)
    try:
        if parsed.hostname and parsed.port and parsed.username:
            credential = unquote(parsed.username)
            if ":" not in credential:
                credential = _decode_b64(credential).decode("utf-8")
            address, port = parsed.hostname, parsed.port
        else:
            decoded = _decode_b64(body.split("?", 1)[0]).decode("utf-8")
            credential, endpoint = decoded.rsplit("@", 1)
            endpoint_url = urlsplit(f"//{endpoint}")
            address, port = endpoint_url.hostname, endpoint_url.port
        method, password = credential.split(":", 1)
    except (AttributeError, IndexError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ProbeConfigError("invalid shadowsocks payload") from exc
    if not address or not port or not method or not password:
        raise ProbeConfigError("incomplete shadowsocks payload")
    return {
        "protocol": "shadowsocks",
        "settings": {"servers": [{"address": address, "port": port, "method": method, "password": password}]},
        "tag": "proxy",
    }


def build_xray_config(raw: str, socks_port: int) -> dict:
    protocol = raw.split("://", 1)[0].lower() if "://" in raw else ""
    if protocol in {"vless", "trojan"}:
        outbound = _standard_outbound(raw, protocol)
    elif protocol == "vmess":
        outbound = _vmess_outbound(raw)
    elif protocol == "ss":
        outbound = _shadowsocks_outbound(raw)
    elif protocol in {"hysteria2", "hy2", "tuic"}:
        raise ProbeConfigError(f"{protocol} is not supported by the pinned Xray probe")
    else:
        raise ProbeConfigError(f"unsupported protocol: {protocol or 'missing'}")
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "listen": "127.0.0.1",
            "port": socks_port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
            "tag": "probe-in",
        }],
        "outbounds": [outbound, {"protocol": "blackhole", "tag": "blocked"}],
        "routing": {"domainStrategy": "AsIs", "rules": [{"type": "field", "inboundTag": ["probe-in"], "outboundTag": "proxy"}]},
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_socks(port: int, process: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + min(timeout, 3.0)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def probe_config(
    raw: str,
    *,
    xray_binary: str,
    urls: tuple[str, ...],
    required_successes: int,
    speed_url: str,
    min_speed_kbps: float,
    timeout_seconds: float,
) -> ProbeResult:
    port = _free_port()
    try:
        config = build_xray_config(raw, port)
    except ProbeConfigError as exc:
        return ProbeResult(False, "static", False, False, error=str(exc))

    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(
            [xray_binary, "run", "-c", "stdin:"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps(config, separators=(",", ":")))
        process.stdin.close()
        if not _wait_for_socks(port, process, timeout_seconds):
            error = "xray did not start"
            if process.poll() is not None and process.stderr:
                error = process.stderr.read().strip()[-500:] or error
            return ProbeResult(False, "xray", True, False, error=error)

        successes = 0
        latencies: list[float] = []
        errors: list[str] = []
        proxy = f"socks5://127.0.0.1:{port}"
        with httpx.Client(proxy=proxy, timeout=timeout_seconds, follow_redirects=True) as client:
            for url in urls:
                started = time.perf_counter()
                try:
                    response = client.get(url, headers={"User-Agent": "ZazaVPN-Health/1.0"})
                    response.raise_for_status()
                    successes += 1
                    latencies.append((time.perf_counter() - started) * 1000)
                except httpx.HTTPError as exc:
                    errors.append(f"{urlsplit(url).hostname}: {type(exc).__name__}")

            if successes < required_successes:
                return ProbeResult(
                    False, "http", True, True, successes, len(urls),
                    round(sum(latencies) / len(latencies), 2) if latencies else None,
                    error="; ".join(errors)[:500] or "not enough successful HTTP probes",
                )

            started = time.perf_counter()
            try:
                speed_response = client.get(speed_url, headers={"User-Agent": "ZazaVPN-Health/1.0"})
                speed_response.raise_for_status()
                elapsed = max(time.perf_counter() - started, 0.001)
                throughput = len(speed_response.content) * 8 / elapsed / 1000
            except httpx.HTTPError as exc:
                return ProbeResult(
                    False, "throughput", True, True, successes, len(urls),
                    round(sum(latencies) / len(latencies), 2), error=f"speed probe: {type(exc).__name__}",
                )
        if throughput < min_speed_kbps:
            return ProbeResult(
                False, "throughput", True, True, successes, len(urls),
                round(sum(latencies) / len(latencies), 2), round(throughput, 2),
                f"speed {throughput:.0f} Kbit/s below {min_speed_kbps:.0f}",
            )
        return ProbeResult(
            True, "passed", True, True, successes, len(urls),
            round(sum(latencies) / len(latencies), 2), round(throughput, 2), None,
        )
    except (OSError, ValueError) as exc:
        return ProbeResult(False, "xray", True, False, error=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
