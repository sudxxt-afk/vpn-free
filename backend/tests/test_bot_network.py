import asyncio
import socket
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.bot import telegram_ipv4_session, warm_telegram_connections


class BotNetworkTests(unittest.TestCase):
    def test_telegram_session_forces_ipv4(self):
        session = telegram_ipv4_session()
        try:
            self.assertEqual(session._connector_init["family"], socket.AF_INET)
        finally:
            asyncio.run(session.close())

    def test_warmup_retries_short_failures_until_pool_is_ready(self):
        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=[TimeoutError(), object(), object(), object()])

        successful = asyncio.run(warm_telegram_connections(bot, required=2, rounds=2))

        self.assertGreaterEqual(successful, 2)
        self.assertEqual(bot.get_me.await_count, 4)


if __name__ == "__main__":
    unittest.main()
