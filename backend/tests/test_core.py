import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Node, NodeProbeState, NodeState, Source, SourceQuality
from app.services.health import _selected_nodes, apply_probe_result, normalize_node_states, refresh_source_qualities
from app.services.github import SourceError, normalize_github_url
from app.services.parser import address_diversity_key, parse_config, parse_payload, transport_key, with_display_name
from app.services.xray_probe import ProbeConfigError, ProbeResult, build_xray_config
from app.services.scoring import calculate_score


class ParserTests(unittest.TestCase):
    def test_parses_supported_protocols(self):
        vless = parse_config("vless://d5e9a2ee-1111-4444-9999-aaaaaaaaaaaa@8.8.8.8:443?security=tls#demo")
        trojan = parse_config("trojan://secret@1.1.1.1:443#demo")
        ss_payload = base64.urlsafe_b64encode(b"aes-128-gcm:secret@9.9.9.9:8443").decode().rstrip("=")
        shadowsocks = parse_config(f"ss://{ss_payload}#demo")
        vmess_payload = base64.b64encode(json.dumps({"add": "8.8.4.4", "port": 443}).encode()).decode()
        vmess = parse_config(f"vmess://{vmess_payload}")
        self.assertEqual((vless.protocol, vless.host, vless.port), ("vless", "8.8.8.8", 443))
        self.assertEqual(trojan.protocol, "trojan")
        self.assertEqual((shadowsocks.protocol, shadowsocks.port), ("ss", 8443))
        self.assertEqual(vmess.host, "8.8.4.4")

    def test_rejects_private_and_unknown_configs(self):
        self.assertIsNone(parse_config("vless://id@127.0.0.1:443"))
        self.assertIsNone(parse_config("wireguard://key@1.1.1.1:443"))
        self.assertEqual(parse_payload("vless://id@127.0.0.1:443\ninvalid"), [])

    def test_groups_literal_addresses_for_pool_diversity(self):
        self.assertEqual(address_diversity_key("8.8.8.8"), "8.8.8.0/24")
        self.assertEqual(address_diversity_key("8.8.8.200"), "8.8.8.0/24")
        self.assertIsNone(address_diversity_key("vpn.example.com"))

    def test_keeps_source_config_and_rewrites_only_client_label(self):
        config = "vless://d5e9a2ee-1111-4444-9999-aaaaaaaaaaaa@8.8.8.8:443?security=reality&type=tcp#old"
        parsed = parse_config(config)
        self.assertEqual(transport_key(config, parsed.protocol), "vless_reality")
        named = with_display_name(config, "📡 Автоподключение LTE")
        self.assertIn("security=reality&type=tcp", named)
        self.assertIn("%F0%9F%93%A1", named)


class GitHubUrlTests(unittest.TestCase):
    def test_normalizes_blob_url(self):
        self.assertEqual(
            normalize_github_url("https://github.com/example/repo/blob/main/list.txt"),
            "https://raw.githubusercontent.com/example/repo/main/list.txt",
        )

    def test_normalizes_encoded_github_filename(self):
        self.assertEqual(
            normalize_github_url("https://github.com/igareck/vpn-configs-for-russia/blob/main/BLACK_SS%2BAll_RUS.txt"),
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_SS%2BAll_RUS.txt",
        )

    def test_rejects_non_github_url(self):
        with self.assertRaises(SourceError):
            normalize_github_url("https://example.org/list.txt")


class XrayProbeTests(unittest.TestCase):
    def test_fast_nodes_and_reliable_sources_score_higher(self):
        fast = Node(success_checks=6, failed_checks=0, consecutive_failures=0, avg_latency_ms=220)
        slow = Node(success_checks=6, failed_checks=0, consecutive_failures=0, avg_latency_ms=1100)
        self.assertGreater(calculate_score(fast, 3000, 0.7), calculate_score(slow, 500, 0.1))
        self.assertGreater(calculate_score(fast, 3000, 0.7), calculate_score(fast, 3000, 0.1))

    def test_legacy_and_stale_active_nodes_are_demoted(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as db:
            source = Source(name="source", github_url="https://github.com/a/states", raw_url="https://raw.githubusercontent.com/a/states/main/list")
            db.add(source); db.flush()
            legacy = Node(source_id=source.id, fingerprint="5" * 64, protocol="vless", host="8.8.8.8", port=443,
                          config_ciphertext="legacy", state=NodeState.ACTIVE, success_checks=100)
            stale = Node(source_id=source.id, fingerprint="6" * 64, protocol="vless", host="8.8.4.4", port=443,
                         config_ciphertext="stale", state=NodeState.ACTIVE)
            fresh = Node(source_id=source.id, fingerprint="7" * 64, protocol="vless", host="1.1.1.1", port=443,
                         config_ciphertext="fresh", state=NodeState.ACTIVE)
            db.add_all([legacy, stale, fresh]); db.flush()
            db.add_all([
                NodeProbeState(node_id=stale.id, stage="passed", static_valid=True, xray_started=True,
                               last_success_at=datetime.now(timezone.utc) - timedelta(hours=2)),
                NodeProbeState(node_id=fresh.id, stage="passed", static_valid=True, xray_started=True,
                               last_success_at=datetime.now(timezone.utc)),
            ])
            db.commit()
            normalize_node_states(db)
            self.assertEqual((legacy.state, legacy.success_checks), (NodeState.CANDIDATE, 0))
            self.assertEqual(stale.state, NodeState.DEGRADED)
            self.assertEqual(fresh.state, NodeState.ACTIVE)
        engine.dispose()

    def test_source_quality_records_rating_and_rejection_reasons(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as db:
            source = Source(name="quality", github_url="https://github.com/a/quality", raw_url="https://raw.githubusercontent.com/a/quality/main/list")
            db.add(source); db.flush()
            passed = Node(source_id=source.id, fingerprint="8" * 64, protocol="vless", host="8.8.8.8", port=443,
                          config_ciphertext="passed", state=NodeState.ACTIVE, success_checks=2, avg_latency_ms=200)
            failed = Node(source_id=source.id, fingerprint="9" * 64, protocol="vless", host="8.8.4.4", port=443,
                          config_ciphertext="failed", state=NodeState.QUARANTINED, failed_checks=1)
            db.add_all([passed, failed]); db.flush()
            db.add_all([
                NodeProbeState(node_id=passed.id, stage="passed", static_valid=True, xray_started=True, throughput_kbps=2500),
                NodeProbeState(node_id=failed.id, stage="http", static_valid=True, xray_started=True, last_error="timeout"),
            ])
            db.commit()
            refresh_source_qualities(db, {source.id})
            quality = db.get(SourceQuality, source.id)
            self.assertEqual((quality.checked_nodes, quality.passed_nodes, quality.rejected_nodes), (2, 1, 1))
            self.assertEqual(quality.pass_rate, 0.5)
            self.assertIn("Нет доступа", quality.rejection_reasons_json)
        engine.dispose()

    def test_fresh_source_nodes_are_selected_before_the_regular_pool(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as db:
            enabled = Source(name="enabled", github_url="https://github.com/a/enabled", raw_url="https://raw.githubusercontent.com/a/enabled/main/list")
            disabled = Source(name="disabled", github_url="https://github.com/a/disabled", raw_url="https://raw.githubusercontent.com/a/disabled/main/list", is_enabled=False)
            db.add_all([enabled, disabled]); db.flush()
            active = Node(source_id=enabled.id, fingerprint="1" * 64, protocol="vless", host="8.8.8.8", port=443,
                          config_ciphertext="active", state=NodeState.ACTIVE, score=99)
            priority = Node(source_id=enabled.id, fingerprint="2" * 64, protocol="vless", host="8.8.4.4", port=443,
                            config_ciphertext="priority", state=NodeState.CANDIDATE)
            ignored = Node(source_id=disabled.id, fingerprint="3" * 64, protocol="vless", host="1.1.1.1", port=443,
                           config_ciphertext="ignored", state=NodeState.CANDIDATE)
            db.add_all([active, priority, ignored]); db.commit()
            selected = _selected_nodes(db, [priority.id, ignored.id])
            self.assertEqual(selected[0][0], priority.id)
            self.assertNotIn(ignored.id, [node_id for node_id, _ in selected])
        engine.dispose()

    def test_unchecked_backlog_prefers_the_more_reliable_source(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as db:
            reliable = Source(name="reliable", github_url="https://github.com/a/reliable", raw_url="https://raw.githubusercontent.com/a/reliable/main/list")
            noisy = Source(name="noisy", github_url="https://github.com/a/noisy", raw_url="https://raw.githubusercontent.com/a/noisy/main/list")
            db.add_all([reliable, noisy]); db.flush()
            db.add_all([
                SourceQuality(source_id=reliable.id, checked_nodes=100, passed_nodes=70, pass_rate=0.7),
                SourceQuality(source_id=noisy.id, checked_nodes=100, passed_nodes=10, pass_rate=0.1),
            ])
            reliable_node = Node(source_id=reliable.id, fingerprint="a" * 64, protocol="vless", host="8.8.8.8", port=443,
                                 config_ciphertext="reliable", state=NodeState.CANDIDATE)
            noisy_node = Node(source_id=noisy.id, fingerprint="b" * 64, protocol="vless", host="8.8.4.4", port=443,
                              config_ciphertext="noisy", state=NodeState.CANDIDATE)
            db.add_all([noisy_node, reliable_node]); db.commit()
            selected = _selected_nodes(db)
            self.assertEqual(selected[0][0], reliable_node.id)
        engine.dispose()

    def test_reality_requires_public_key_and_builds_strict_stream(self):
        invalid = "vless://d5e9a2ee-1111-4444-9999-aaaaaaaaaaaa@8.8.8.8:443?security=reality&type=tcp&sni=example.com&fp=chrome"
        with self.assertRaisesRegex(ProbeConfigError, "requires pbk"):
            build_xray_config(invalid, 1080)
        valid = invalid + "&pbk=public-key&sid=abcd"
        config = build_xray_config(valid, 1080)
        outbound = config["outbounds"][0]
        self.assertEqual(outbound["protocol"], "vless")
        self.assertEqual(outbound["streamSettings"]["realitySettings"]["publicKey"], "public-key")

    def test_builds_websocket_trojan_and_vmess_outbounds(self):
        trojan = build_xray_config(
            "trojan://secret@1.1.1.1:443?security=tls&type=ws&host=cdn.example.com&path=%2Fws&sni=example.com",
            1081,
        )["outbounds"][0]
        self.assertEqual(trojan["streamSettings"]["wsSettings"]["headers"]["Host"], "cdn.example.com")
        vmess_payload = base64.b64encode(json.dumps({
            "v": "2", "add": "8.8.4.4", "port": "443",
            "id": "d5e9a2ee-1111-4444-9999-aaaaaaaaaaaa", "aid": "0",
            "net": "grpc", "tls": "tls", "sni": "example.com", "path": "service",
        }).encode()).decode()
        vmess = build_xray_config(f"vmess://{vmess_payload}", 1082)["outbounds"][0]
        self.assertEqual(vmess["protocol"], "vmess")
        self.assertEqual(vmess["streamSettings"]["grpcSettings"]["serviceName"], "service")

    def test_only_full_probe_promotes_node_and_failure_quarantines_it(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as db:
            source = Source(name="test", github_url="https://github.com/a/b", raw_url="https://raw.githubusercontent.com/a/b/main/list")
            db.add(source); db.flush()
            node = Node(
                id=uuid4(), source_id=source.id, fingerprint="a" * 64,
                protocol="vless", host="8.8.8.8", port=443, config_ciphertext="unused",
                state=NodeState.ACTIVE, success_checks=99,
            )
            db.add(node); db.flush()
            apply_probe_result(db, node, ProbeResult(
                True, "passed", True, True, http_successes=2, http_attempts=3,
                latency_ms=120.0, throughput_kbps=500.0,
            ))
            db.flush()
            probe = db.get(NodeProbeState, node.id)
            self.assertEqual((node.state, node.success_checks, probe.stage), (NodeState.ACTIVE, 2, "passed"))
            apply_probe_result(db, node, ProbeResult(
                False, "http", True, True, http_successes=1, http_attempts=3,
                latency_ms=250.0, error="only one endpoint",
            ))
            self.assertEqual(node.state, NodeState.QUARANTINED)
            self.assertEqual(probe.last_error, "only one endpoint")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
