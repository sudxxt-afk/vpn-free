import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from app.database import Base
from app.main import bot_delete_device, bot_rename_device, bot_rotate_device, bot_vpn_status, subscription
from app.crypto import encrypt
from app.models import Device, Node, NodeProbeState, NodeState, RetiredSubscription, Source, TelegramUser
from app.schemas import DeviceUpdate
from app.security import hash_token
from app.services.subgram import AccessDecision, Sponsor


class SubscriptionAccessTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_existing_subscription_rechecks_subgram_and_temporarily_clears_nodes(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=1001, username="tester")
            db.add(user)
            db.flush()
            token = "existing-device-token"
            db.add(Device(
                user_id=user.id,
                slot=1,
                label="Phone",
                token_hash=hash_token(token),
                token_hint="token",
            ))
            db.commit()

            denied = AccessDecision(False, "warning", (Sponsor("https://t.me/sponsor"),), 1)
            with patch("app.main.get_subgram_access", AsyncMock(return_value=denied)) as subgram:
                response = asyncio.run(subscription(token, db))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.body, b"")
            self.assertIn("start=sponsors", response.headers["support-url"])
            subgram.assert_awaited_once_with(1001, username="tester")

    def test_subscription_marks_dedicated_auto_routes_for_happ_network_filter(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=1003, username="auto-routes")
            db.add(user)
            db.flush()
            token = "auto-routes-token"
            db.add(Device(user_id=user.id, slot=1, label="Phone", token_hash=hash_token(token), token_hint="auto"))
            source = Source(name="test", github_url="https://github.com/example/test", raw_url="https://raw.githubusercontent.com/example/test")
            db.add(source)
            db.flush()
            now = datetime.now(timezone.utc)
            configs = (
                ("wifi", "vless://wifi-id@8.8.8.8:443?security=tls#WiFi"),
                ("mobile", "vless://mobile-id@8.8.4.4:443?security=reality#Mobile"),
            )
            for profile, raw in configs:
                node = Node(
                    source_id=source.id, fingerprint=f"{profile}-fingerprint", protocol="vless",
                    host="8.8.8.8" if profile == "wifi" else "8.8.4.4", port=443,
                    config_ciphertext=encrypt(raw), state=NodeState.ACTIVE, score=100,
                    success_checks=2,
                )
                db.add(node)
                db.flush()
                db.add(NodeProbeState(
                    node_id=node.id, stage="passed", static_valid=True, xray_started=True,
                    http_successes=1, http_attempts=1, last_checked_at=now, last_success_at=now,
                ))
            db.commit()

            with patch("app.main.get_subgram_access", AsyncMock(return_value=AccessDecision(True, "ok"))):
                response = asyncio.run(subscription(token, db))

            body = response.body.decode()
            self.assertIn("only%20WiFi", body)
            self.assertIn("only%20Mobile", body)
            self.assertEqual(response.headers["profile-update-interval"], "1")

    def test_retired_subscription_returns_happ_reissue_notice(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=1002, username="retired")
            db.add(user)
            db.flush()
            token = "retired-device-token"
            db.add(RetiredSubscription(
                original_device_id=user.id,
                user_id=user.id,
                slot=1,
                label="Phone",
                token_hash=hash_token(token),
                token_hint="retired",
                reason="global_reissue",
            ))
            db.commit()

            response = asyncio.run(subscription(token, db))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["support-url"], "https://t.me/zazaaVPN_bot?start=reissue")
            self.assertTrue(response.headers["announce"].startswith("base64:"))
            self.assertEqual(response.body, b"")

    def test_unknown_subscription_stays_not_found(self):
        with self.Session() as db, self.assertRaises(HTTPException) as raised:
            asyncio.run(subscription("unknown-device-token", db))
        self.assertEqual(raised.exception.status_code, 404)

    def test_device_management_and_restore_status(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=1100, username="devices")
            db.add(user); db.flush()
            old = RetiredSubscription(
                original_device_id=user.id, user_id=user.id, slot=1, label="Old phone",
                token_hash=hash_token("old-device"), token_hint="old", reason="global_reissue",
            )
            db.add(old); db.commit()

            empty = bot_vpn_status(1100, db)
            self.assertTrue(empty["can_restore"])
            device = Device(user_id=user.id, slot=1, label="Phone", token_hash=hash_token("live-device"), token_hint="live")
            db.add(device); db.commit(); db.refresh(device)
            renamed = bot_rename_device(1100, device.id, DeviceUpdate(label="Laptop"), db)
            self.assertEqual(renamed.label, "Laptop")

            with patch("app.main.get_subgram_access", AsyncMock(return_value=AccessDecision(True, "ok"))):
                rotated = asyncio.run(bot_rotate_device(1100, device.id, db))
            self.assertIsNotNone(rotated.subscription_url)
            self.assertEqual(db.query(RetiredSubscription).count(), 2)
            self.assertFalse(bot_vpn_status(1100, db)["can_restore"])

            result = bot_delete_device(1100, device.id, db)
            self.assertTrue(result["deleted"])
            self.assertTrue(bot_vpn_status(1100, db)["can_restore"])
            self.assertEqual(db.query(Device).count(), 0)
