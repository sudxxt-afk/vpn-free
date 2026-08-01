import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import AsyncMock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.main import claim_ticket, create_support_ticket, require_bot_admin, resolve_telegram_identity
from app.models import AdminUser, Device, Role, TelegramUser
from app.schemas import BroadcastCreate, SupportTicketCreate
from app.services.analytics import daily_retention_cohorts, sequential_funnel
from app.services.broadcasts import _recipient_query, _send_delivery
from app.services.telegram_html import sanitize_telegram_html


class AdminBotTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_bot_admin_roles_and_support_flag(self):
        with self.Session() as db:
            owner = AdminUser(login="owner", password_hash="x", role=Role.OWNER, telegram_id=101, support_enabled=True)
            viewer = AdminUser(login="viewer", password_hash="x", role=Role.VIEWER, telegram_id=102, support_enabled=True)
            admin = AdminUser(login="admin", password_hash="x", role=Role.ADMIN, telegram_id=103, support_enabled=False)
            db.add_all([owner, viewer, admin]); db.commit()
            self.assertEqual(require_bot_admin(db, 101, support=True).login, "owner")
            with self.assertRaises(HTTPException):
                require_bot_admin(db, 102)
            with self.assertRaises(HTTPException):
                require_bot_admin(db, 103, support=True)

    def test_username_resolution_and_atomic_ticket_claim(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=2001, username="KnownUser")
            first = AdminUser(login="first", password_hash="x", role=Role.ADMIN, telegram_id=201, support_enabled=True)
            second = AdminUser(login="second", password_hash="x", role=Role.ADMIN, telegram_id=202, support_enabled=True)
            db.add_all([user, first, second]); db.commit()
            telegram_id, username = resolve_telegram_identity(db, None, "@knownuser")
            self.assertEqual((telegram_id, username), (2001, "KnownUser"))
            ticket = create_support_ticket(2001, SupportTicketCreate(text="Нужна помощь"), db)
            ticket_id = UUID(ticket["id"])
            claim_ticket(201, ticket_id, db)
            with self.assertRaises(HTTPException):
                claim_ticket(202, ticket_id, db)

    def test_broadcast_segments_and_html_sanitizer(self):
        with self.Session() as db:
            with_device = TelegramUser(telegram_id=301, username="device")
            without_device = TelegramUser(telegram_id=302, username="empty")
            blocked_bot = TelegramUser(telegram_id=303, username="blocked", bot_blocked_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
            db.add_all([with_device, without_device, blocked_bot]); db.flush()
            db.add(Device(user_id=with_device.id, slot=1, label="one", token_hash="h", token_hint="hint")); db.commit()
            self.assertEqual([item.telegram_id for item in db.scalars(_recipient_query("with_devices")).all()], [301])
            self.assertEqual([item.telegram_id for item in db.scalars(_recipient_query("without_devices")).all()], [302])
        clean = sanitize_telegram_html('<b>Жирный</b> <a href="javascript:bad">опасно</a> <a href="https://example.com">ссылка</a>')
        self.assertIn("<b>Жирный</b>", clean)
        self.assertNotIn("javascript", clean)
        self.assertIn('href="https://example.com"', clean)

    def test_sequential_funnel_and_daily_retention(self):
        now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        first_user, second_user = UUID(int=1), UUID(int=2)
        events = [
            SimpleNamespace(telegram_user_id=first_user, event_type=name, created_at=now + timedelta(minutes=index))
            for index, name in enumerate(("bot_start", "vpn_issued", "site_visit", "happ_launch", "subscription_open"))
        ]
        events += [
            SimpleNamespace(telegram_user_id=second_user, event_type="bot_start", created_at=now),
            SimpleNamespace(telegram_user_id=second_user, event_type="site_visit", created_at=now + timedelta(minutes=1)),
        ]
        funnel = sequential_funnel(events, now - timedelta(minutes=1))
        self.assertEqual(funnel, {"bot_start": 2, "vpn_issued": 1, "site_visit": 1, "happ_launch": 1, "subscription_open": 1})
        activity = events + [SimpleNamespace(telegram_user_id=first_user, event_type="subscription_open", created_at=now + timedelta(days=1))]
        cohorts = daily_retention_cohorts([(first_user, now), (second_user, now)], activity, (now + timedelta(days=1)).date(), 2)
        today_cohort = next(item for item in cohorts if item["date"] == str(now.date()))
        self.assertEqual((today_cohort["users"], today_cohort["d0"], today_cohort["d1"]), (2, 100.0, 50.0))


class BroadcastButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_buttons_are_attached_to_text_delivery(self):
        payload = BroadcastCreate(segment="active", text_html="<b>Новость</b>", buttons=[{"text": "Открыть", "url": "https://example.com"}])
        campaign = SimpleNamespace(photo_file_id=None, text_html=payload.text_html,
                                   buttons_json='[{"text":"Открыть","url":"https://example.com"}]')
        bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
        await _send_delivery(bot, campaign, 123)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url, "https://example.com")


if __name__ == "__main__":
    unittest.main()
