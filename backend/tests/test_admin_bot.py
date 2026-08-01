import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import bot as bot_module
from app.database import Base
from app.main import claim_ticket, create_broadcast, create_support_ticket, require_bot_admin, resolve_telegram_identity
from app.models import AdminUser, BroadcastCampaign, Device, Donation, Role, TelegramUser
from app.schemas import BroadcastCreate, SupportTicketCreate
from app.services.analytics import daily_retention_cohorts, sequential_funnel
from app.services.broadcasts import _recipient_query, _send_delivery, format_broadcast_report
from app.services import broadcasts
from app.services.broadcast_drafts import BroadcastDraftStore
from app.services.donations import (DonationError, complete_star_donation, create_star_donation,
                                    match_ton_transaction, validate_star_checkout)
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

    def test_stars_donation_is_validated_and_idempotent(self):
        with self.Session() as db:
            user = TelegramUser(telegram_id=404, username="donor")
            db.add(user); db.commit(); db.refresh(user)
            item = create_star_donation(db, user, 100)
            self.assertEqual(validate_star_checkout(db, user, item.invoice_payload, "XTR", 100).id, item.id)
            with self.assertRaises(DonationError):
                validate_star_checkout(db, user, item.invoice_payload, "XTR", 50)
            paid = complete_star_donation(
                db, user, invoice_payload=item.invoice_payload, currency="XTR", total_amount=100,
                telegram_payment_charge_id="charge-1", provider_payment_charge_id=None,
            )
            self.assertEqual((paid.status, paid.amount_stars), ("paid", 100))
            repeated = complete_star_donation(
                db, user, invoice_payload=item.invoice_payload, currency="XTR", total_amount=100,
                telegram_payment_charge_id="charge-1", provider_payment_charge_id=None,
            )
            self.assertEqual(repeated.id, item.id)

    def test_broadcast_confirmation_is_idempotent(self):
        with self.Session() as db:
            admin = AdminUser(login="sender", password_hash="x", role=Role.ADMIN, telegram_id=405, is_active=True)
            db.add(admin); db.commit()
            payload = BroadcastCreate(client_request_id=uuid4(), segment="all", text_html="Новость")
            first = create_broadcast(405, payload, db)
            repeated = create_broadcast(405, payload, db)
            self.assertEqual(first["id"], repeated["id"])
            self.assertEqual(len(db.query(BroadcastCampaign).all()), 1)

    def test_ton_match_requires_reference_amount_and_fresh_transaction(self):
        now = datetime.now(timezone.utc)
        item = Donation(user_id=UUID(int=7), method="ton", status="pending", amount_nano=1_000_000_000,
                        reference="zaza-reference", created_at=now)
        wrong = [{"utime": int(now.timestamp()), "transaction_id": {"hash": "wrong"},
                  "in_msg": {"value": "1000000000", "message": "another", "source": "sender"}}]
        self.assertIsNone(match_ton_transaction(item, wrong))
        underpaid = [{"utime": int(now.timestamp()), "transaction_id": {"hash": "underpaid"},
                      "in_msg": {"value": "999999999", "message": "zaza-reference", "source": "sender"}}]
        self.assertIsNone(match_ton_transaction(item, underpaid))
        valid = [{"utime": int(now.timestamp()), "transaction_id": {"hash": "tx-1"},
                  "in_msg": {"value": "1000000000", "message": "zaza-reference", "source": "sender"}}]
        self.assertEqual(match_ton_transaction(item, valid), {"tx_hash": "tx-1", "sender": "sender", "value": 1_000_000_000})


class BroadcastButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_buttons_are_attached_to_text_delivery(self):
        payload = BroadcastCreate(client_request_id=uuid4(), segment="active", text_html="<b>Новость</b>",
                                  buttons=[{"text": "Открыть", "url": "https://example.com"}])
        campaign = SimpleNamespace(photo_file_id=None, text_html=payload.text_html,
                                   buttons_json='[{"text":"Открыть","url":"https://example.com"}]')
        bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
        await _send_delivery(bot, campaign, 123)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url, "https://example.com")

    async def test_draft_survives_store_recreation(self):
        class FakeRedis:
            values: dict[str, str] = {}

            async def get(self, key):
                return self.values.get(key)

            async def set(self, key, value, ex=None):
                self.values[key] = value

            async def delete(self, key):
                self.values.pop(key, None)

            async def aclose(self):
                return None

        first = BroadcastDraftStore(FakeRedis())
        state = await first.begin(501)
        self.assertEqual((state["stage"], state["draft"]["kind"]), ("content", None))
        state["draft"]["text_html"] = "<b>Сохранено</b>"
        state["draft"]["kind"] = "photo_caption"
        state["stage"] = "segment"
        await first.save(501, state)
        restored = await BroadcastDraftStore(FakeRedis()).load(501)
        self.assertEqual(
            (restored["stage"], restored["draft"]["kind"], restored["draft"]["text_html"]),
            ("segment", "photo_caption", "<b>Сохранено</b>"),
        )

    async def test_photo_delivery_allows_empty_or_formatted_caption(self):
        campaign = SimpleNamespace(
            photo_file_id="telegram-file-id",
            text_html="<b>Подпись</b>",
            buttons_json="[]",
        )
        bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
        await _send_delivery(bot, campaign, 777)
        bot.send_photo.assert_awaited_once()
        self.assertEqual(bot.send_photo.await_args.args, (777, "telegram-file-id"))
        self.assertEqual(bot.send_photo.await_args.kwargs["caption"], "<b>Подпись</b>")

    async def test_image_document_delivery_preserves_caption(self):
        campaign = SimpleNamespace(
            photo_file_id="document:telegram-document-id",
            text_html="<i>Подпись файла</i>",
            buttons_json="[]",
        )
        bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(), send_document=AsyncMock())
        await _send_delivery(bot, campaign, 778)
        bot.send_document.assert_awaited_once()
        self.assertEqual(bot.send_document.await_args.args, (778, "telegram-document-id"))
        self.assertEqual(bot.send_document.await_args.kwargs["caption"], "<i>Подпись файла</i>")

    async def test_image_document_with_caption_advances_broadcast_wizard(self):
        class Drafts:
            def __init__(self):
                self.state = {
                    "stage": "content",
                    "client_request_id": str(uuid4()),
                    "draft": {"kind": None, "segment": None, "text_html": "", "photo_file_id": None, "buttons": []},
                }

            async def load(self, _telegram_id):
                return self.state

            async def save(self, _telegram_id, state):
                self.state = state

        drafts = Drafts()
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=779),
            text=None,
            caption="Подпись",
            html_caption="<b>Подпись</b>",
            photo=None,
            document=SimpleNamespace(file_id="document-id", mime_type="image/png", file_name="poster.png"),
            answer=AsyncMock(),
        )
        with patch.object(bot_module, "broadcast_drafts", drafts):
            await bot_module.state_message(message)
        self.assertEqual(drafts.state["stage"], "segment")
        self.assertEqual(drafts.state["draft"]["kind"], "document_caption")
        self.assertEqual(drafts.state["draft"]["photo_file_id"], "document:document-id")
        self.assertEqual(drafts.state["draft"]["text_html"], "<b>Подпись</b>")
        message.answer.assert_awaited_once()

    async def test_worker_completes_persisted_campaign(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as db:
            user = TelegramUser(telegram_id=601, username="recipient")
            author = AdminUser(login="author", password_hash="x", role=Role.ADMIN, telegram_id=602, is_active=True)
            db.add_all([user, author]); db.flush()
            campaign = BroadcastCampaign(client_request_id=uuid4(), author_admin_id=author.id, segment="all", text_html="Проверка")
            db.add(campaign); db.commit(); campaign_id = campaign.id
        fake_bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(),
                                   session=SimpleNamespace(close=AsyncMock()))
        with patch.object(broadcasts, "SessionLocal", Session), patch.object(broadcasts, "Bot", return_value=fake_bot):
            await broadcasts._process_campaign(campaign_id)
        with Session() as db:
            result = db.get(BroadcastCampaign, campaign_id)
            self.assertEqual((result.status, result.total_count, result.sent_count, result.failed_count),
                             ("completed", 1, 1, 0))
        self.assertEqual(fake_bot.send_message.await_count, 2)
        self.assertIn("Рассылка завершена", fake_bot.send_message.await_args_list[1].args[1])
        engine.dispose()

    async def test_broadcast_report_includes_delivery_outcomes(self):
        report = format_broadcast_report({
            "campaign_id": "deadbeef", "segment": "all", "total_count": 10,
            "sent_count": 8, "failed_count": 1, "skipped_count": 1,
            "duration": "2 сек", "errors": [("chat not found", 1)], "author_telegram_id": 1,
        })
        self.assertIn("Доставлено: <b>8</b>", report)
        self.assertIn("Бот недоступен: <b>1</b>", report)
        self.assertIn("chat not found — 1", report)


if __name__ == "__main__":
    unittest.main()
