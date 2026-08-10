"""Recheck legacy TCP/RAW Xray failures after a probe-adapter upgrade."""
import argparse
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select

from app.crypto import decrypt
from app.database import Base, SessionLocal, engine
from app.models import Node, NodeProbeState, NodeState
from app.services.health import check_active_nodes


def _is_legacy_transport(ciphertext: str) -> bool:
    raw = decrypt(ciphertext)
    return parse_qs(urlsplit(raw).query).get("type", [""])[0].lower() in {"tcp", "raw"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=600)
    args = parser.parse_args()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        rows = db.execute(
            select(Node.id, Node.config_ciphertext)
            .join(NodeProbeState, NodeProbeState.node_id == Node.id)
            .where(Node.state != NodeState.REMOVED, NodeProbeState.stage == "xray")
            .limit(max(1, args.limit))
        ).all()
        node_ids = [node_id for node_id, ciphertext in rows if _is_legacy_transport(ciphertext)]
        total_ok = total_checked = 0
        batch_size = 60
        for start in range(0, len(node_ids), batch_size):
            ok, checked = check_active_nodes(db, priority_node_ids=node_ids[start:start + batch_size])
            total_ok += ok
            total_checked += checked
        print(f"revalidated={total_checked} passed={total_ok}")


if __name__ == "__main__":
    main()
