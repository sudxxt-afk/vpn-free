import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.bot import sponsor_gate_screen, start
from app.services.subgram import get_subgram_access, get_subgram_statistics


class SubgramTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            subgram_api_key="subgram-key",
            subgram_base_url="https://api.subgram.org",
            subgram_max_sponsors=3,
        )

    def test_warning_blocks_and_keeps_only_available_unsubscribed_sponsors(self):
        body = {
            "status": "warning",
            "message": "Нужна подписка",
            "total_fixed_link": 2,
            "additional": {"sponsors": [
                {"resource_id": "1", "link": "https://t.me/first", "resource_name": "Первый канал", "button_text": "Подписаться", "type": "channel", "status": "unsubscribed", "available_now": True},
                {"resource_id": "2", "link": "https://t.me/done", "resource_name": "Готово", "status": "subscribed", "available_now": True},
                {"resource_id": "3", "link": "https://t.me/stopped", "resource_name": "Стоп", "status": "unsubscribed", "available_now": False},
            ]},
        }
        request = AsyncMock(return_value=(200, body))
        with patch("app.services.subgram.get_settings", return_value=self.settings), patch("app.services.subgram._request", request):
            result = asyncio.run(get_subgram_access(123, username="@tester"))

        self.assertFalse(result.allowed)
        self.assertEqual([item.link for item in result.sponsors], ["https://t.me/first"])
        self.assertEqual(result.sponsor_total, 2)
        payload = request.await_args.args[0]
        self.assertEqual(payload, {
            "user_id": 123,
            "chat_id": 123,
            "action": "subscribe",
            "max_sponsors": 3,
            "get_links": 1,
            "username": "tester",
        })

    def test_only_explicit_ok_grants_access(self):
        with patch("app.services.subgram.get_settings", return_value=self.settings), patch(
            "app.services.subgram._request", AsyncMock(return_value=(200, {"status": "ok", "message": "ok"})),
        ):
            self.assertTrue(asyncio.run(get_subgram_access(123)).allowed)

        request = httpx.Request("POST", "https://api.subgram.org/get-sponsors")
        with patch("app.services.subgram.get_settings", return_value=self.settings), patch(
            "app.services.subgram._request", AsyncMock(side_effect=httpx.ConnectError("offline", request=request)),
        ):
            result = asyncio.run(get_subgram_access(123))
        self.assertFalse(result.allowed)
        self.assertEqual(result.status, "error")

    def test_warning_without_safe_links_fails_closed(self):
        with patch("app.services.subgram.get_settings", return_value=self.settings), patch(
            "app.services.subgram._request", AsyncMock(return_value=(200, {"status": "warning", "additional": {"sponsors": []}})),
        ):
            result = asyncio.run(get_subgram_access(123))
        self.assertFalse(result.allowed)

    def test_missing_key_fails_closed(self):
        settings = SimpleNamespace(subgram_api_key="")
        with patch("app.services.subgram.get_settings", return_value=settings):
            result = asyncio.run(get_subgram_access(123))
        self.assertFalse(result.allowed)
        self.assertEqual(result.status, "error")

    def test_statistics_are_normalized_from_documented_response(self):
        settings = SimpleNamespace(
            subgram_statistics_token="statistics-token",
            subgram_statistics_bot_id=123456,
            subgram_base_url="https://api.subgram.org",
        )
        body = {
            "status": "ok",
            "message": "Статистика получена",
            "data": {
                "labels": ["13.08", "14.08"],
                "subscribers_data": [2, "3"],
                "value_data": [4.5, "7.50"],
                "avg_price_data": [2.25, 2.5],
                "total_subscribers": 5,
                "total_value": 12,
                "requests_stats": {"total_requests": 20, "successful_requests": 18},
            },
        }
        request = AsyncMock(return_value=(200, body))
        with patch("app.services.subgram.get_settings", return_value=settings), patch(
            "app.services.subgram._statistics_request", request,
        ):
            result = asyncio.run(get_subgram_statistics(14))

        self.assertTrue(result.available)
        self.assertEqual((result.total_subscribers, result.total_revenue, result.average_price), (5, 12.0, 2.4))
        self.assertEqual((result.total_requests, result.successful_requests), (20, 18))
        self.assertEqual(result.days[1].subscribers, 3)
        params = request.await_args.args[0]
        self.assertEqual((params["action"], params["bot_id"], params["output_format"]), ("bots", 123456, "json"))

    def test_statistics_report_missing_or_rejected_token_without_fake_zeroes(self):
        missing = SimpleNamespace(subgram_statistics_token="", subgram_statistics_bot_id=None)
        with patch("app.services.subgram.get_settings", return_value=missing):
            result = asyncio.run(get_subgram_statistics())
        self.assertFalse(result.configured)
        self.assertFalse(result.available)

        configured = SimpleNamespace(
            subgram_statistics_token="wrong-token",
            subgram_statistics_bot_id=None,
            subgram_base_url="https://api.subgram.org",
        )
        with patch("app.services.subgram.get_settings", return_value=configured), patch(
            "app.services.subgram._statistics_request",
            AsyncMock(return_value=(401, {"status": "error", "message": "Невалидный API токен"})),
        ):
            rejected = asyncio.run(get_subgram_statistics())
        self.assertTrue(rejected.configured)
        self.assertFalse(rejected.available)
        self.assertEqual(rejected.message, "Невалидный API токен")

    def test_sponsor_screen_has_no_manual_check_button(self):
        text, markup = sponsor_gate_screen({
            "sponsors": [{"link": "https://t.me/sponsor", "title": "Новости", "button_text": "Подписаться"}],
            "sponsor_total": 1,
        })
        self.assertIn("полностью бесплатным", text)
        self.assertIn("покупать хлеб", text)
        self.assertIn("снова отправь /start", text)
        self.assertEqual(len(markup.inline_keyboard), 1)
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://t.me/sponsor")
        self.assertIsNone(markup.inline_keyboard[0][0].callback_data)


class SubgramStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_shows_sponsors_before_the_main_menu(self):
        message = SimpleNamespace(answer=AsyncMock())
        access = {
            "allowed": False,
            "status": "warning",
            "sponsors": [{"link": "https://t.me/sponsor", "title": "Новости", "button_text": "Подписаться"}],
            "sponsor_total": 1,
        }
        with patch("app.bot.ensure_user", AsyncMock(return_value=123)), patch(
            "app.bot.allowed", AsyncMock(return_value=access),
        ), patch("app.bot.api", AsyncMock(return_value={})) as api:
            await start(message)

        self.assertEqual(message.answer.await_count, 1)
        text = message.answer.await_args.args[0]
        self.assertIn("Каналы спонсоров", text)
        self.assertEqual(message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url, "https://t.me/sponsor")
        self.assertEqual(api.await_count, 1)

    async def test_returning_subscriber_sees_sponsors_before_restore(self):
        message = SimpleNamespace(answer=AsyncMock())
        access = {
            "allowed": False,
            "status": "warning",
            "sponsors": [{"link": "https://t.me/sponsor", "title": "Новости", "button_text": "Подписаться"}],
            "sponsor_total": 1,
        }
        with patch("app.bot.ensure_user", AsyncMock(return_value=123)), patch(
            "app.bot.allowed", AsyncMock(return_value=access),
        ), patch("app.bot.api", AsyncMock(return_value={"can_restore": True})) as api:
            await start(message)

        text = message.answer.await_args.args[0]
        self.assertIn("Каналы спонсоров", text)
        self.assertNotIn("Возобновить подписку", text)
        self.assertEqual(api.await_count, 1)


if __name__ == "__main__":
    unittest.main()
