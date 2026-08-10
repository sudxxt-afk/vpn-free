import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.main import subscription
from app.models import Device, TelegramUser
from app.security import hash_token
from app.services.piarflow import PartnerDecision


class SubscriptionAccessTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_existing_subscription_does_not_recheck_sponsor_tasks(self):
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

            with patch("app.main.get_partner_access", AsyncMock(return_value=PartnerDecision(False))) as partner:
                response = asyncio.run(subscription(token, db))

            self.assertEqual(response.status_code, 200)
            partner.assert_not_awaited()
