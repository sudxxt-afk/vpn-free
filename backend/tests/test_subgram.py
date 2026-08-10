import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import PartnerGate, TelegramUser
from app.services.subgram import get_partner_access


class SubgramTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_first_and_repeat_checks_use_the_same_subscribe_task(self):
        requests: list[dict] = []

        class Response:
            def json(self):
                return {
                    "status": "warning",
                    "additional": {"sponsors": [{
                        "status": "unsubscribed",
                        "available_now": True,
                        "link": "https://t.me/sponsor",
                        "resource_name": "Sponsor",
                        "button_text": "Subscribe",
                    }]},
                }

            def raise_for_status(self):
                return None

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, _url, *, headers, json):
                requests.append({"headers": headers, "json": json})
                return Response()

        settings = SimpleNamespace(
            subgram_enabled=True,
            subgram_api_key="bot-key",
            subgram_base_url="https://api.subgram.org",
        )
        with self.Session() as db, patch("app.services.subgram.get_settings", return_value=settings), patch(
            "app.services.subgram.httpx.AsyncClient", Client
        ):
            user = TelegramUser(telegram_id=1001, username="tester")
            db.add(user)
            db.commit()

            first = asyncio.run(get_partner_access(db, user, 1))
            second = asyncio.run(get_partner_access(db, user, 1))

            self.assertFalse(first.allowed)
            self.assertEqual(first.sponsors[0].link, "https://t.me/sponsor")
            self.assertFalse(second.allowed)
            self.assertEqual([item["json"]["action"] for item in requests], ["subscribe", "subscribe"])
            self.assertTrue(all(item["json"]["get_links"] == 1 for item in requests))
            self.assertEqual(db.get(PartnerGate, user.id).pending_tier, 1)

