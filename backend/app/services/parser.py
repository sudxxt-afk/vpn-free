import base64
import hashlib
import ipaddress
import json
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

SUPPORTED_PROTOCOLS = {"vless", "ss", "trojan", "vmess", "hysteria2", "hy2", "tuic"}


@dataclass(frozen=True)
class ParsedConfig:
    raw: str
    protocol: str
    host: str
    port: int
    fingerprint: str
    network_profile: str
    profile_priority: int


def _is_public_host(host: str) -> bool:
    if not host or host.lower() in {"localhost", "0.0.0.0", "::1"}:
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return ip.is_global
    except ValueError:
        return "." in host and len(host) <= 253


def address_diversity_key(host: str) -> str | None:
    """Group literal IPv4 endpoints by /24 and IPv6 endpoints by /64."""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None
    prefix = 24 if address.version == 4 else 64
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def _parse_vmess(value: str) -> tuple[str, int] | None:
    payload = value.split("://", 1)[1].split("#", 1)[0]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.b64decode(payload).decode("utf-8"))
        host = str(data.get("add", ""))
        return host, int(data.get("port", 0))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _parse_standard(value: str) -> tuple[str, int] | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        # Public lists frequently contain redacted or otherwise malformed
        # rows. One bad authority must not abort the whole source refresh.
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.hostname and port:
        return unquote(parsed.hostname), port
    if value.lower().startswith("ss://"):
        encoded = value.split("://", 1)[1].split("#", 1)[0].split("?", 1)[0]
        encoded += "=" * (-len(encoded) % 4)
        try:
            decoded = base64.urlsafe_b64decode(encoded).decode("utf-8")
            host_part = decoded.rsplit("@", 1)[1]
            decoded_url = urlsplit(f"//{host_part}")
            if decoded_url.hostname and decoded_url.port:
                return unquote(decoded_url.hostname), decoded_url.port
        except (ValueError, UnicodeDecodeError, IndexError):
            return None
    return None


def classify_network_profile(value: str, protocol: str) -> tuple[str, int]:
    """A conservative transport-only hint, not a promise that a network is reachable."""
    if protocol in {"hysteria2", "tuic"}:
        return "wifi", 95
    if protocol == "trojan":
        return "mobile", 80
    if protocol == "vless":
        query = parse_qs(urlsplit(value).query)
        security = query.get("security", [""])[0].lower()
        if security == "reality":
            return "mobile", 100
    return "wifi", 70


def transport_key(value: str, protocol: str) -> str:
    """Stable admin-facing bucket used to cap the public subscription."""
    if protocol == "vless":
        query = parse_qs(urlsplit(value).query)
        if query.get("security", [""])[0].lower() == "reality":
            return "vless_reality"
        if query.get("type", [""])[0].lower() == "ws":
            return "vless_ws"
        return "vless_other"
    return {"hysteria2": "hysteria2", "tuic": "tuic", "trojan": "trojan", "ss": "shadowsocks", "vmess": "vmess"}.get(protocol, "vless_other")


def display_region(value: str, host: str) -> tuple[str, str]:
    """Use public-config labels when available; never invent geography from an IP."""
    label = unquote(urlsplit(value).fragment).strip()
    if value.lower().startswith("vmess://"):
        payload = value.split("://", 1)[1].split("#", 1)[0]
        payload += "=" * (-len(payload) % 4)
        try:
            label = str(json.loads(base64.b64decode(payload).decode("utf-8")).get("ps", "")).strip()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    flags = "".join(char for char in label if "\U0001F1E6" <= char <= "\U0001F1FF")
    cleaned = " ".join(label.replace(flags, " ").replace("[", " ").replace("]", " ").split())
    if cleaned:
        return flags or "🌐", cleaned[:48]
    return "🌐", "Неизвестный регион"


def with_display_name(value: str, label: str) -> str:
    """Rename a URI for client display without modifying endpoint parameters."""
    if value.lower().startswith("vmess://"):
        payload = value.split("://", 1)[1].split("#", 1)[0]
        payload += "=" * (-len(payload) % 4)
        try:
            config = json.loads(base64.b64decode(payload).decode("utf-8"))
            config["ps"] = label
            encoded = base64.b64encode(json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode()).decode()
            return f"vmess://{encoded}"
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return value
    return f"{value.split('#', 1)[0]}#{quote(label, safe='') }"


def parse_config(value: str) -> ParsedConfig | None:
    value = value.strip()
    if not value or "://" not in value:
        return None
    protocol = value.split("://", 1)[0].lower()
    if protocol not in SUPPORTED_PROTOCOLS:
        return None
    protocol = "hysteria2" if protocol == "hy2" else protocol
    endpoint = _parse_vmess(value) if protocol == "vmess" else _parse_standard(value)
    if not endpoint:
        return None
    host, port = endpoint
    if not _is_public_host(host) or not 1 <= port <= 65535:
        return None
    normalized = value.split("#", 1)[0].strip()
    network_profile, profile_priority = classify_network_profile(value, protocol)
    return ParsedConfig(
        raw=value,
        protocol=protocol,
        host=host.lower(),
        port=port,
        fingerprint=hashlib.sha256(normalized.encode()).hexdigest(),
        network_profile=network_profile,
        profile_priority=profile_priority,
    )


def parse_payload(payload: str) -> list[ParsedConfig]:
    lines = [line.strip() for line in payload.splitlines() if line.strip()]
    if len(lines) == 1 and "://" not in lines[0]:
        try:
            decoded = base64.urlsafe_b64decode(lines[0] + "=" * (-len(lines[0]) % 4)).decode("utf-8")
            lines = [line.strip() for line in decoded.splitlines() if line.strip()]
        except (ValueError, UnicodeDecodeError):
            pass
    parsed: dict[str, ParsedConfig] = {}
    for line in lines:
        config = parse_config(line)
        if config:
            parsed[config.fingerprint] = config
    return list(parsed.values())


_XRAY_PROXY_PROTOCOLS = {"vless", "vmess", "trojan", "shadowsocks"}


def _xray_json_stream_params(stream: object) -> list[tuple[str, str]]:
    if not isinstance(stream, dict):
        return [("type", "tcp"), ("security", "none")]
    network = str(stream.get("network") or "tcp").lower()
    security = str(stream.get("security") or "none").lower()
    if security in {"false", "0"}:
        security = "none"
    params = [("type", network), ("security", security)]
    settings = stream.get("realitySettings") or stream.get("tlsSettings")
    if isinstance(settings, dict):
        mapping = (("serverName", "sni"), ("fingerprint", "fp"), ("publicKey", "pbk"), ("shortId", "sid"), ("spiderX", "spx"))
        for source_key, param in mapping:
            value = settings.get(source_key)
            if value not in (None, ""):
                params.append((param, str(value)))
        alpn = settings.get("alpn")
        if isinstance(alpn, list) and alpn:
            params.append(("alpn", ",".join(str(item) for item in alpn)))
    ws = stream.get("wsSettings")
    if isinstance(ws, dict):
        path = ws.get("path")
        if path:
            params.append(("path", str(path)))
        headers = ws.get("headers")
        host = headers.get("Host") if isinstance(headers, dict) else None
        if host:
            params.append(("host", str(host)))
    grpc = stream.get("grpcSettings")
    if isinstance(grpc, dict) and grpc.get("serviceName"):
        params.append(("serviceName", str(grpc["serviceName"])))
    tcp = stream.get("tcpSettings")
    header = tcp.get("header") if isinstance(tcp, dict) else None
    if isinstance(header, dict) and header.get("type") not in (None, "none"):
        params.append(("headerType", str(header["type"])))
    return params


def _xray_json_outbound_uri(outbound: dict, name: str) -> str | None:
    protocol = str(outbound.get("protocol") or "").lower()
    if protocol not in _XRAY_PROXY_PROTOCOLS:
        return None
    settings = outbound.get("settings")
    if not isinstance(settings, dict):
        return None
    endpoint: dict = {}
    if isinstance(settings.get("vnext"), list) and settings["vnext"]:
        endpoint = settings["vnext"][0]
    elif isinstance(settings.get("servers"), list) and settings["servers"]:
        endpoint = settings["servers"][0]
    address = str(endpoint.get("address") or "").strip()
    port = int(endpoint.get("port") or 0)
    if not address or not 1 <= port <= 65535:
        return None
    stream = outbound.get("streamSettings")
    query = urlencode(_xray_json_stream_params(stream))
    label = quote(name or f"{address}:{port}", safe="")
    users = endpoint.get("users") if isinstance(endpoint.get("users"), list) else []
    user = users[0] if users else {}
    if protocol == "vless":
        user_id = str(user.get("id") or "").strip()
        if not user_id:
            return None
        extra = ""
        flow = user.get("flow")
        if flow:
            extra += f"&flow={quote(str(flow), safe='')}"
        encryption = user.get("encryption") or "none"
        return f"vless://{user_id}@{address}:{port}?{query}{extra}&encryption={encryption}#{label}"
    password = str(endpoint.get("password") or "").strip()
    method = str(endpoint.get("method") or "").strip()
    if protocol == "trojan" and password:
        return f"trojan://{quote(password, safe='')}@{address}:{port}?{query}#{label}"
    if protocol == "shadowsocks" and method and password:
        userinfo = base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
        plugin = endpoint.get("plugin")
        plugin_part = f"?plugin={quote(str(plugin), safe='')}" if plugin else ""
        return f"ss://{userinfo}@{address}:{port}{plugin_part}#{label}"
    if protocol == "vmess" and user.get("id"):
        payload = {
            "v": "2", "ps": name, "add": address, "port": str(port), "id": str(user.get("id")),
            "aid": str(user.get("alterId") or 0), "scy": user.get("security") or "auto",
        }
        if isinstance(stream, dict):
            payload["net"] = str(stream.get("network") or "tcp")
            payload["tls"] = "tls" if str(stream.get("security")) == "tls" else ""
            reality_or_tls = stream.get("tlsSettings") or stream.get("realitySettings")
            if isinstance(reality_or_tls, dict):
                payload["sni"] = str(reality_or_tls.get("serverName") or "")
            ws = stream.get("wsSettings")
            if isinstance(ws, dict):
                payload["path"] = str(ws.get("path") or "")
                headers = ws.get("headers")
                if isinstance(headers, dict) and headers.get("Host"):
                    payload["host"] = str(headers["Host"])
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
        return f"vmess://{encoded}"
    return None


def xray_json_share_links(raw: str) -> list[str]:
    """Convert Xray client JSON exports into standard share links."""
    data = json.loads(raw)
    elements = data if isinstance(data, list) else [data]
    links: list[str] = []
    seen: set[str] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        remark = str(element.get("remarks") or element.get("title") or "").strip()
        proxies = [item for item in element.get("outbounds") or [] if isinstance(item, dict)]
        usable = 0
        for outbound in proxies:
            suffix = f" · {usable + 1}" if len([p for p in proxies if str(p.get('protocol') or '').lower() in _XRAY_PROXY_PROTOCOLS]) > 1 else ""
            uri = _xray_json_outbound_uri(outbound, f"{remark}{suffix}")
            if not uri:
                continue
            key = uri.split("#", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            links.append(uri)
            usable += 1
    return links


def decode_subscription_body(raw: str) -> str:
    """Return newline-separated share links from a plain, base64 or JSON body."""
    text = raw.strip()
    if not text:
        return text
    first_line = text.splitlines()[0].lstrip()
    if first_line.startswith(("[", "{")):
        return "\n".join(xray_json_share_links(text))
    if "://" in first_line:
        return text
    compacted = "".join(text.split())
    padded = compacted.replace("-", "+").replace("_", "/") + "=" * (-len(compacted) % 4)
    try:
        decoded = base64.b64decode(padded.encode(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return text
    return decoded if "://" in decoded else text
