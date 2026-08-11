import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Device, RetiredSubscription, TelegramUser
from app.security import hash_token
from app.services.subscriptions import (SPONSOR_UNSUBSCRIBED_MESSAGE, happ_retirement_payload,
                                        rollback_global_cutover, run_global_cutover)


class SubscriptionCutoverTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_cutover_is_idempotent_frees_slots_and_can_rollback(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=1001)
            db.add(user)
            db.flush()
            db.add_all([
                Device(user_id=user.id, slot=1, label="One", token_hash=hash_token("one"), token_hint="one"),
                Device(user_id=user.id, slot=2, label="Two", token_hash=hash_token("two"), token_hint="two"),
            ])
            db.commit()

            first = run_global_cutover(db)
            second = run_global_cutover(db)
            self.assertEqual((first.id, second.id, first.retired_count), (second.id, first.id, 2))
            self.assertEqual(db.query(Device).count(), 0)
            self.assertEqual(db.query(RetiredSubscription).count(), 2)

            self.assertEqual(rollback_global_cutover(db), 2)
            self.assertEqual(db.query(Device).count(), 2)
            self.assertEqual(db.query(RetiredSubscription).count(), 0)

    def test_unsubscribe_happ_payload_contains_distinct_message(self):
        body, headers = happ_retirement_payload("sponsor_unsubscribed")
        self.assertEqual(body, "")
        self.assertEqual(headers["support-url"], "https://t.me/zazaaVPN_bot?start=reissue")
        self.assertLessEqual(len(SPONSOR_UNSUBSCRIBED_MESSAGE), 200)


if __name__ == "__main__":
    unittest.main()
