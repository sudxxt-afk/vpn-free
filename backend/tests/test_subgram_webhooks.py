import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import SubgramSponsorState, SubgramWebhookEvent, TelegramUser
from app.schemas import SubgramWebhookEventPayload
from app.services.subgram_webhooks import (clear_webhook_blocks_after_live_check, expected_bot_id, has_webhook_block,
                                           process_webhooks, webhook_key_is_valid)


def event(webhook_id: int, status: str, *, user_id: int = 1001) -> SubgramWebhookEventPayload:
    return SubgramWebhookEventPayload(
        webhook_id=webhook_id,
        ads_id=55,
        link="https://t.me/sponsor-secret-link",
        user_id=user_id,
        bot_id=777000,
        status=status,
        subscribe_date=date(2026, 8, 14),
    )


class SubgramWebhookTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_key_and_bot_identity_are_checked(self):
        settings = SimpleNamespace(subgram_api_key="bot-api-key", telegram_bot_token="777000:telegram-secret")
        with patch("app.services.subgram_webhooks.get_settings", return_value=settings):
            self.assertTrue(webhook_key_is_valid("bot-api-key"))
            self.assertFalse(webhook_key_is_valid("wrong"))
            self.assertEqual(expected_bot_id(), 777000)

    def test_events_are_ordered_deduplicated_and_links_are_not_stored(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=1001, username="tester")
            db.add(user); db.commit()
            result = process_webhooks(db, [event(11, "subscribed"), event(10, "unsubscribed"), event(11, "subscribed")])
            db.commit()

            self.assertEqual((result.received, result.processed, result.duplicates), (3, 2, 1))
            state = db.scalar(select(SubgramSponsorState))
            self.assertEqual((state.status, state.latest_webhook_id), ("subscribed", 11))
            stored = db.get(SubgramWebhookEvent, 10)
            self.assertEqual(stored.telegram_user_id, user.id)
            self.assertEqual(len(stored.link_hash), 64)
            self.assertFalse(hasattr(stored, "link"))

    def test_unsubscribe_blocks_until_new_event_or_successful_live_check(self):
        with self.Session() as db:
            process_webhooks(db, [event(12, "unsubscribed")]); db.commit()
            self.assertTrue(has_webhook_block(db, 1001))
            self.assertEqual(clear_webhook_blocks_after_live_check(db, 1001), 1)
            db.commit()
            self.assertFalse(has_webhook_block(db, 1001))

            stale = process_webhooks(db, [event(9, "unsubscribed")]); db.commit()
            self.assertEqual(stale.stale, 1)
            state = db.scalar(select(SubgramSponsorState))
            self.assertEqual((state.status, state.latest_webhook_id), ("verified", 12))


if __name__ == "__main__":
    unittest.main()
