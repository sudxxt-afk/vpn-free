import asyncio
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.main import bot_access
from app.models import SubgramAccessState, TelegramUser
from app.schemas import SubgramWebhookEventPayload
from app.services.subgram import AccessDecision, SubscriptionReview
from app.services.subgram_access import has_cached_access, has_sponsor_block, recheck_due_access_states
from app.services.subgram_webhooks import process_webhooks


class PersistentSubgramAccessTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_first_ok_is_persisted_and_second_access_uses_cache(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=7001, username="cached")
            db.add(user); db.commit()
            live = AsyncMock(return_value=AccessDecision(True, "ok", ads_ids=(101, 102)))
            with patch("app.services.subgram_access.get_subgram_access", live):
                first = asyncio.run(bot_access(7001, db=db))
                second = asyncio.run(bot_access(7001, db=db))

            self.assertTrue(first["allowed"])
            self.assertEqual(second["status"], "cached")
            self.assertEqual(live.await_count, 1)
            state = db.get(SubgramAccessState, user.id)
            self.assertIsNotNone(state.verified_at)
            self.assertEqual(json.loads(state.assigned_ads_json), [101, 102])
            self.assertTrue(has_cached_access(db, user))

    def test_ok_without_ads_ids_is_still_persisted(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=7004, username="no_ads")
            db.add(user); db.commit()
            live = AsyncMock(return_value=AccessDecision(True, "ok"))
            with patch("app.services.subgram_access.get_subgram_access", live):
                first = asyncio.run(bot_access(7004, db=db))
                second = asyncio.run(bot_access(7004, db=db))

            self.assertTrue(first["allowed"])
            self.assertEqual(second["status"], "cached")
            self.assertEqual(live.await_count, 1)
            state = db.get(SubgramAccessState, user.id)
            self.assertEqual(json.loads(state.assigned_ads_json), [])
            self.assertTrue(has_cached_access(db, user))

    def test_unsubscribe_webhook_revokes_cached_access(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=7002, username="webhook")
            db.add(user); db.flush()
            db.add(SubgramAccessState(
                user_id=user.id,
                assigned_ads_json="[201]",
                verified_at=datetime.now(timezone.utc),
                last_checked_at=datetime.now(timezone.utc),
            ))
            db.commit()
            process_webhooks(db, [SubgramWebhookEventPayload(
                webhook_id=1, ads_id=201, link="https://t.me/sponsor", user_id=7002,
                bot_id=123, status="unsubscribed", subscribe_date=date.today(),
            )])
            db.commit()

            self.assertTrue(has_sponsor_block(db, user))
            self.assertFalse(has_cached_access(db, user))

    def test_background_recheck_revokes_without_requesting_new_tasks(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=7003, username="poll")
            db.add(user); db.flush()
            db.add(SubgramAccessState(
                user_id=user.id,
                assigned_ads_json="[301]",
                verified_at=datetime.now(timezone.utc) - timedelta(days=2),
                last_checked_at=datetime.now(timezone.utc) - timedelta(days=2),
            ))
            db.commit()
            review = AsyncMock(return_value=SubscriptionReview(True, ((301, "unsubscribed"),)))
            with patch("app.services.subgram_access.get_subgram_subscriptions", review):
                checked, blocked = asyncio.run(recheck_due_access_states(db))

            self.assertEqual((checked, blocked), (1, 1))
            review.assert_awaited_once_with(7003, (301,))
            self.assertTrue(has_sponsor_block(db, user))


if __name__ == "__main__":
    unittest.main()
