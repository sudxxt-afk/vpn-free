import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import encrypt
from app.models import Node, NodeState, Source, SourceRun
from app.services.parser import decode_subscription_body, parse_payload

MAX_SOURCE_BYTES = 5 * 1024 * 1024
SOURCE_USER_AGENT = "ZazaVPN/1.0"
HAPP_ADD_PREFIX = "happ://add/"


class SourceError(ValueError):
    pass


def normalize_source_url(value: str) -> str:
    """Accept GitHub files and generic client subscription URLs (happ://add/...)."""
    text = value.strip()
    lowered = text.lower()
    if lowered.startswith(HAPP_ADD_PREFIX):
        text = text[len(HAPP_ADD_PREFIX):].strip()
    elif lowered.startswith("happ://"):
        raise SourceError("Поддерживаются ссылки happ://add/<https-адрес подписки>")
    parsed = urlparse(text)
    if parsed.scheme != "https":
        raise SourceError("Разрешены только HTTPS-ссылки")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise SourceError("Укажите полный адрес файла или подписки")
    if parsed.hostname == "raw.githubusercontent.com":
        if len(parsed.path.strip("/").split("/")) < 4:
            raise SourceError("Укажите путь к конкретному файлу GitHub")
        return text
    if parsed.hostname == "github.com":
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 5 and parts[2] == "blob":
            owner, repo, _, ref = parts[:4]
            path = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    return text


def _run(db: Session, source: Source, status: str, message: str | None = None) -> SourceRun:
    item = SourceRun(source_id=source.id, status=status, message=message)
    db.add(item)
    db.flush()
    return item


def _finish(run: SourceRun, *, status: str, found: int = 0, published: int = 0, message: str | None = None) -> None:
    run.status = status
    run.found_count = found
    run.published_count = published
    run.message = message
    run.finished_at = datetime.now(timezone.utc)


def refresh_source(db: Session, source: Source) -> SourceRun:
    run = _run(db, source, "running")
    headers = {"Accept": "text/plain", "User-Agent": SOURCE_USER_AGENT}
    if source.last_body and source.etag:
        headers["If-None-Match"] = source.etag
    if source.last_body and source.last_modified:
        headers["If-Modified-Since"] = source.last_modified
    try:
        with httpx.Client(follow_redirects=True, timeout=18.0) as client:
            response = client.get(source.raw_url, headers=headers)
        if response.status_code == 304:
            _finish(run, status="unchanged", message="Источник подтвердил неизменность")
            source.last_success_at = datetime.now(timezone.utc)
            db.commit()
            return run
        response.raise_for_status()
        if len(response.content) > MAX_SOURCE_BYTES:
            raise SourceError("Тело источника превышает 5 MiB")
        raw_body = response.text
    except (httpx.HTTPError, SourceError) as exc:
        source.last_error = str(exc)
        _finish(run, status="error", message=str(exc))
        db.commit()
        return run

    content_hash = hashlib.sha256(raw_body.encode()).hexdigest()
    if content_hash == source.content_hash:
        source.etag = response.headers.get("etag", source.etag)
        source.last_modified = response.headers.get("last-modified", source.last_modified)
        source.last_success_at = datetime.now(timezone.utc)
        if source.last_body is None:
            try:
                source.last_body = decode_subscription_body(raw_body)
            except ValueError:
                pass
        _finish(run, status="unchanged", message="Хеш тела не изменился")
        db.commit()
        return run

    try:
        content = decode_subscription_body(raw_body)
    except ValueError as exc:
        content = ""
        source.last_error = str(exc)
        _finish(run, status="error", message=f"Не удалось разобрать тело источника: {exc}")
        db.commit()
        return run

    configs = parse_payload(content)
    if not configs and raw_body.lstrip().startswith(("[", "{")):
        source.last_error = "empty subscription payload"
        _finish(run, status="error", message="Подписка не содержит распознанных серверов")
        db.commit()
        return run
    previous = db.scalars(select(Node).where(Node.source_id == source.id, Node.state != NodeState.REMOVED)).all()
    previous_count = len(previous)
    anomalous = previous_count >= 5 and (len(configs) < previous_count * 0.2 or len(configs) > previous_count * 10)
    if anomalous:
        if source.pending_hash != content_hash:
            source.pending_hash = content_hash
            source.pending_anomaly_count = 1
        else:
            source.pending_anomaly_count += 1
        if source.pending_anomaly_count < 2:
            _finish(run, status="guarded", found=len(configs), message="Аномальная смена набора; ожидается повторное подтверждение")
            db.commit()
            return run

    seen = {config.fingerprint for config in configs}
    existing = {node.fingerprint: node for node in db.scalars(select(Node).where(Node.source_id == source.id)).all()}
    # A public list often repeats the same share links from another source.
    # Fingerprints are global, so skip such duplicates instead of failing the
    # entire import with a database uniqueness error.
    known_fingerprints = set(db.scalars(select(Node.fingerprint).where(Node.fingerprint.in_(seen))).all()) if seen else set()
    now = datetime.now(timezone.utc)
    for config in configs:
        node = existing.get(config.fingerprint)
        if node:
            node.last_seen_at = now
            if node.state == NodeState.REMOVED:
                node.state = NodeState.CANDIDATE
                node.removed_at = None
            continue
        if config.fingerprint in known_fingerprints:
            continue
        db.add(Node(
            source_id=source.id,
            fingerprint=config.fingerprint,
            protocol=config.protocol,
            host=config.host,
            port=config.port,
            config_ciphertext=encrypt(config.raw),
        ))
    for node in previous:
        if node.fingerprint not in seen:
            node.state = NodeState.REMOVED
            node.removed_at = now
    source.content_hash = content_hash
    source.etag = response.headers.get("etag")
    source.last_modified = response.headers.get("last-modified")
    source.pending_hash = None
    source.pending_anomaly_count = 0
    source.last_success_at = now
    source.last_error = None
    source.last_body = content
    _finish(run, status="processed", found=len(configs), published=len(configs))
    db.commit()
    return run
