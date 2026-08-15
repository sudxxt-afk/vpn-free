import asyncio
import socket
import unittest

from app.bot import telegram_ipv4_session


class BotNetworkTests(unittest.TestCase):
    def test_telegram_session_forces_ipv4(self):
        session = telegram_ipv4_session()
        try:
            self.assertEqual(session._connector_init["family"], socket.AF_INET)
        finally:
            asyncio.run(session.close())


if __name__ == "__main__":
    unittest.main()
