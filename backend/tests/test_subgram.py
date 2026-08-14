import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.bot import sponsor_gate_screen, start
from app.services.subgram import get_subgram_access


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

    def test_ok_and_provider_errors_fail_open_as_documented(self):
        with patch("app.services.subgram.get_settings", return_value=self.settings), patch(
            "app.services.subgram._request", AsyncMock(return_value=(200, {"status": "ok", "message": "ok"})),
        ):
            self.assertTrue(asyncio.run(get_subgram_access(123)).allowed)

        request = httpx.Request("POST", "https://api.subgram.org/get-sponsors")
        with patch("app.services.subgram.get_settings", return_value=self.settings), patch(
            "app.services.subgram._request", AsyncMock(side_effect=httpx.ConnectError("offline", request=request)),
        ):
            result = asyncio.run(get_subgram_access(123))
        self.assertTrue(result.allowed)
        self.assertEqual(result.status, "error")

    def test_warning_without_safe_links_fails_open(self):
        with patch("app.services.subgram.get_settings", return_value=self.settings), patch(
            "app.services.subgram._request", AsyncMock(return_value=(200, {"status": "warning", "additional": {"sponsors": []}})),
        ):
            result = asyncio.run(get_subgram_access(123))
        self.assertTrue(result.allowed)

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
        api.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
