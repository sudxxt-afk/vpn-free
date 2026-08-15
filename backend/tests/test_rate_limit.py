import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import rate_limit


class RateLimitFallbackTests(unittest.TestCase):
    def setUp(self):
        rate_limit._local_windows.clear()
        rate_limit.redis_client = None

    def tearDown(self):
        rate_limit._local_windows.clear()
        rate_limit.redis_client = None

    def test_redis_failure_uses_local_limit_instead_of_fail_open(self):
        request = SimpleNamespace(
            url=SimpleNamespace(path="/auth/login"),
            headers={},
            client=SimpleNamespace(host="127.0.0.1"),
        )
        broken = SimpleNamespace(incr=AsyncMock(side_effect=OSError("offline")))
        with patch("app.services.rate_limit.Redis.from_url", return_value=broken), patch.object(
            rate_limit.settings, "login_rate_limit_per_15_minutes", 2,
        ):
            first = asyncio.run(rate_limit.is_allowed(request))
            second = asyncio.run(rate_limit.is_allowed(request))
            third = asyncio.run(rate_limit.is_allowed(request))

        self.assertEqual((first[0], second[0], third[0]), (True, True, False))


if __name__ == "__main__":
    unittest.main()
