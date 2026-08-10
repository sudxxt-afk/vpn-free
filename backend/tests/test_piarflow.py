import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aiogram.types import CallbackQuery

from app import bot as bot_module
from app.bot import PiarFlowGateMiddleware, sponsor_gate_screen
from app.database import Base
from app.models import Device, PiarFlowAccessState, PiarFlowBotSnapshot, PiarFlowDailyStat, RetiredSubscription, TelegramUser
from app.security import hash_token
from app.services.piarflow import PiarFlowError, current_partner_access, get_partner_access, handle_unsubscribe, sync_piarflow_stats


class PiarFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.settings = SimpleNamespace(
            piarflow_enabled=True,
            piarflow_api_key="piar-key",
            piarflow_base_url="https://piarflow.com/v1",
            piarflow_stats_sync_hours=6,
            piarflow_stats_backfill_days=3,
            piarflow_stats_backfill_batch=3,
        )

    def tearDown(self):
        self.engine.dispose()

    def user(self, db, telegram_id=1001):
        user = TelegramUser(telegram_id=telegram_id, username="tester")
        db.add(user)
        db.commit()
        return user

    def test_sponsor_screen_numbers_remaining_tasks_and_shows_progress(self):
        text, markup = sponsor_gate_screen({
            "sponsor_total": 3,
            "sponsors": [
                {"link": "https://piarflow.com/track?a"},
                {"link": "https://piarflow.com/track?b"},
            ],
        })

        self.assertIn("1 из 3", text)
        self.assertIn("админам тоже надо кушать", text)
        self.assertEqual([row[0].text for row in markup.inline_keyboard], [
            "2. Открыть задание",
            "3. Открыть задание",
            "Проверить выполнение (1/3)",
        ])
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "piarflow:check")

    def test_first_start_issues_tasks_and_second_start_completes(self):
        issued = {"status": "ok", "sponsors": [
            {"link": f"https://t.me/sponsor{index}", "status": "unsubscribed"} for index in range(3)
        ]}
        checked = {"status": "ok", "sponsors": [
            {"link": f"https://t.me/sponsor{index}", "status": "subscribed"} for index in range(3)
        ]}
        request = AsyncMock(side_effect=[issued, checked])
        with self.Session() as db, patch("app.services.piarflow.get_settings", return_value=self.settings), patch(
            "app.services.piarflow._request", request,
        ):
            user = self.user(db)
            first = asyncio.run(get_partner_access(db, user))
            self.assertFalse(first.allowed)
            self.assertEqual((first.status, first.sponsor_total, len(first.sponsors)), ("pending", 3, 3))
            self.assertFalse(current_partner_access(db, user).allowed)

            second = asyncio.run(get_partner_access(db, user))
            self.assertTrue(second.allowed)
            self.assertEqual(db.get(PiarFlowAccessState, user.id).status, "completed")
            self.assertEqual(request.await_args_list[0].kwargs["payload"]["max_sponsors"], 3)
            self.assertEqual(len(request.await_args_list[1].kwargs["payload"]["links"]), 3)

    def test_partial_check_keeps_only_unfinished_buttons(self):
        links = [f"https://t.me/sponsor{index}" for index in range(3)]
        with self.Session() as db, patch("app.services.piarflow.get_settings", return_value=self.settings), patch(
            "app.services.piarflow._request",
            AsyncMock(side_effect=[
                {"status": "ok", "sponsors": [{"link": link, "status": "unsubscribed"} for link in links]},
                {"status": "ok", "sponsors": [
                    {"link": links[0], "status": "subscribed"},
                    {"link": links[1], "status": "not_counted"},
                    {"link": links[2], "status": "unsubscribed"},
                ]},
            ]),
        ):
            user = self.user(db)
            asyncio.run(get_partner_access(db, user))
            result = asyncio.run(get_partner_access(db, user))
            self.assertFalse(result.allowed)
            self.assertEqual([item.link for item in result.sponsors], [links[2]])
            self.assertEqual(result.sponsor_total, 3)

    def test_no_inventory_is_temporary_and_new_tasks_revoke_temporary_devices(self):
        with self.Session() as db, patch("app.services.piarflow.get_settings", return_value=self.settings), patch(
            "app.services.piarflow._request",
            AsyncMock(side_effect=[
                PiarFlowError("none", 404),
                {"status": "ok", "sponsors": [{"link": "https://t.me/new", "status": "unsubscribed"}]},
            ]),
        ):
            user = self.user(db)
            first = asyncio.run(get_partner_access(db, user))
            self.assertTrue(first.allowed)
            db.add(Device(user_id=user.id, slot=1, label="Phone", token_hash=hash_token("temporary"), token_hint="temporary"))
            db.commit()

            second = asyncio.run(get_partner_access(db, user))
            self.assertFalse(second.allowed)
            self.assertEqual(db.query(Device).count(), 0)
            retired = db.query(RetiredSubscription).one()
            self.assertEqual(retired.reason, "sponsor_required")

    def test_api_failure_fails_closed_for_new_user(self):
        with self.Session() as db, patch("app.services.piarflow.get_settings", return_value=self.settings), patch(
            "app.services.piarflow._request", AsyncMock(side_effect=PiarFlowError("timeout")),
        ):
            result = asyncio.run(get_partner_access(db, self.user(db)))
            self.assertFalse(result.allowed)
            self.assertIn("временно недоступен", result.reason)

    def test_unsubscribe_retires_all_devices_and_is_idempotent(self):
        with self.Session() as db, patch("app.services.piarflow.get_settings", return_value=self.settings):
            user = self.user(db)
            db.add(PiarFlowAccessState(user_id=user.id, status="completed", links_json='["https://t.me/sponsor"]'))
            db.add_all([
                Device(user_id=user.id, slot=1, label="One", token_hash=hash_token("one"), token_hint="one"),
                Device(user_id=user.id, slot=2, label="Two", token_hash=hash_token("two"), token_hint="two"),
            ])
            db.commit()

            self.assertEqual(handle_unsubscribe(db, user.telegram_id, "https://t.me/sponsor", 10), 2)
            self.assertEqual(handle_unsubscribe(db, user.telegram_id, "https://t.me/sponsor", 10), 0)
            self.assertEqual(db.query(Device).count(), 0)
            self.assertEqual(db.query(RetiredSubscription).count(), 2)
            self.assertEqual(db.get(PiarFlowAccessState, user.id).status, "unsubscribed")

    def test_provider_profile_and_daily_stats_are_persisted(self):
        async def provider(_method, path, **kwargs):
            if path == "/traffic_bot":
                return {"status": "ok", "bot": {"bot_id": 10, "username": "zazaaVPN_bot", "is_active": True,
                        "max_sponsors": 3, "reset_time": 60, "sold_subs": 12, "not_counted": 2, "earned": "24.50"}}
            return {"status": "ok", "stats": {"sold_subs": 4, "earned": "4.08"}, "date": kwargs["params"]["date"]}

        with self.Session() as db, patch("app.services.piarflow.get_settings", return_value=self.settings), patch(
            "app.services.piarflow._request", side_effect=provider,
        ):
            synced = asyncio.run(sync_piarflow_stats(db))
            snapshot = db.get(PiarFlowBotSnapshot, 1)
            self.assertEqual(synced, 3)
            self.assertEqual((snapshot.sold_subs, snapshot.not_counted, snapshot.earned), (12, 2, 24.5))
            self.assertEqual(db.query(PiarFlowDailyStat).count(), 3)


if __name__ == "__main__":
    unittest.main()


class PiarFlowMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_shows_gate_before_menu(self):
        message = AsyncMock()
        message.from_user = SimpleNamespace(id=999)
        blocked = {"allowed": False, "status": "pending", "sponsors": [{"link": "https://t.me/sponsor"}], "sponsor_total": 3}
        with patch.object(bot_module, "ensure_user", AsyncMock(return_value=999)), patch.object(
            bot_module, "api", AsyncMock(return_value={}),
        ), patch.object(bot_module, "allowed", AsyncMock(return_value=blocked)), patch.object(
            bot_module, "show_access_gate", AsyncMock(),
        ) as gate:
            await bot_module.start(message)
        gate.assert_awaited_once_with(message, blocked)
        message.answer.assert_not_awaited()

    async def test_old_admin_callback_is_blocked_without_access(self):
        callback = CallbackQuery.model_validate({
            "id": "callback-id",
            "from": {"id": 999, "is_bot": False, "first_name": "Admin"},
            "chat_instance": "instance",
            "data": "adm:home",
        })
        handler = AsyncMock()
        access = {"allowed": False, "status": "pending", "sponsors": [{"link": "https://t.me/sponsor"}], "sponsor_total": 3}
        enabled = SimpleNamespace(piarflow_enabled=True, piarflow_api_key="key")
        with patch.object(bot_module, "settings", enabled), patch.object(bot_module, "ensure_user", AsyncMock(return_value=999)), patch.object(
            bot_module, "access_status", AsyncMock(return_value=access),
        ), patch.object(bot_module, "show_access_gate", AsyncMock()) as gate:
            await PiarFlowGateMiddleware()(handler, callback, {})
        handler.assert_not_awaited()
        gate.assert_awaited_once()
