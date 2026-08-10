import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import PiarFlowTask, SponsorGate, TelegramUser
from app.services.piarflow import get_partner_access


class PiarFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_issue_then_check_the_same_sponsor_links(self):
        requests: list[tuple[str, dict]] = []

        class Response:
            def __init__(self, body):
                self.body = body
                self.status_code = 200

            def json(self):
                return self.body

            def raise_for_status(self):
                return None

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, *, headers, json):
                requests.append((url, json))
                if headers["Authorization"] != "Bearer piar-key":
                    raise AssertionError("PiarFlow bearer token was not sent")
                if url.endswith("/check"):
                    return Response({"status": "ok", "sponsors": [{"link": "https://t.me/sponsor", "status": "subscribed"}]})
                return Response({"status": "ok", "sponsors": [{"link": "https://t.me/sponsor", "status": "unsubscribed"}]})

        settings = SimpleNamespace(piarflow_enabled=True, piarflow_api_key="piar-key", piarflow_base_url="https://piarflow.com/v1")
        with self.Session() as db, patch("app.services.piarflow.get_settings", return_value=settings), patch("app.services.piarflow.httpx.AsyncClient", Client):
            user = TelegramUser(telegram_id=1001, username="tester")
            db.add(user)
            db.commit()

            first = asyncio.run(get_partner_access(db, user, 1))
            self.assertFalse(first.allowed)
            self.assertEqual(first.sponsors[0].link, "https://t.me/sponsor")
            self.assertIsNotNone(db.get(PiarFlowTask, user.id))

            second = asyncio.run(get_partner_access(db, user, 1))
            self.assertTrue(second.allowed)
            self.assertIsNone(db.get(PiarFlowTask, user.id))
            self.assertEqual(db.get(SponsorGate, user.id).completed_tier, 1)
            self.assertTrue(requests[0][0].endswith("/sponsors"))
            self.assertTrue(requests[1][0].endswith("/sponsors/check"))
            self.assertEqual(requests[1][1]["links"], ["https://t.me/sponsor"])
