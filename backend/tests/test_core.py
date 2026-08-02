import base64
import json
import unittest
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Node, NodeProbeState, NodeState, Source
from app.services.health import _selected_nodes, apply_probe_result
from app.services.github import SourceError, normalize_github_url
from app.services.parser import parse_config, parse_payload, transport_key, with_display_name
from app.services.xray_probe import ProbeConfigError, ProbeResult, build_xray_config


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
