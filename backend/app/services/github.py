import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import encrypt
from app.models import Node, NodeState, Source, SourceRun
from app.services.parser import parse_payload

MAX_SOURCE_BYTES = 5 * 1024 * 1024


class SourceError(ValueError):
    pass


def normalize_github_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https":
        raise SourceError("Разрешены только HTTPS-ссылки GitHub")
    if parsed.hostname == "raw.githubusercontent.com":
        if len(parsed.path.strip("/").split("/")) < 4:
            raise SourceError("Укажите путь к конкретному файлу GitHub")
        return value.strip()
    if parsed.hostname == "github.com":
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 5 and parts[2] == "blob":
            owner, repo, _, ref = parts[:4]
            path = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    raise SourceError("Поддерживаются GitHub blob и raw.githubusercontent.com ссылки на файл")


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
    headers = {"Accept": "text/plain"}
    if source.etag:
        headers["If-None-Match"] = source.etag
    if source.last_modified:
        headers["If-Modified-Since"] = source.last_modified
    try:
        with httpx.Client(follow_redirects=False, timeout=18.0) as client:
            response = client.get(source.raw_url, headers=headers)
        if response.status_code == 304:
            _finish(run, status="unchanged", message="GitHub подтвердил неизменность файла")
            source.last_success_at = datetime.now(timezone.utc)
            db.commit()
            return run
        response.raise_for_status()
        if len(response.content) > MAX_SOURCE_BYTES:
            raise SourceError("Файл источника превышает 5 MiB")
        content = response.text
    except (httpx.HTTPError, SourceError) as exc:
        source.last_error = str(exc)
        _finish(run, status="error", message=str(exc))
        db.commit()
        return run

    content_hash = hashlib.sha256(content.encode()).hexdigest()
    if content_hash == source.content_hash:
        source.etag = response.headers.get("etag", source.etag)
        source.last_modified = response.headers.get("last-modified", source.last_modified)
        source.last_success_at = datetime.now(timezone.utc)
        _finish(run, status="unchanged", message="Хеш файла не изменился")
        db.commit()
        return run

    configs = parse_payload(content)
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
    now = datetime.now(timezone.utc)
    for config in configs:
        node = existing.get(config.fingerprint)
        if node:
            node.last_seen_at = now
            if node.state == NodeState.REMOVED:
                node.state = NodeState.CANDIDATE
                node.removed_at = None
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
    _finish(run, status="processed", found=len(configs), published=len(configs))
    db.commit()
    return run

