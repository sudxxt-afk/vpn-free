import base64
import json
import unittest

from app.services.github import SourceError, normalize_github_url
from app.services.parser import parse_config, parse_payload, transport_key, with_display_name


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


if __name__ == "__main__":
    unittest.main()
