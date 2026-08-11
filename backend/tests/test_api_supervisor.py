import subprocess
import unittest
from unittest.mock import Mock, patch

from app.api_supervisor import healthcheck, stop_process


class ApiSupervisorTests(unittest.TestCase):
    @patch("app.api_supervisor.urlopen")
    def test_healthcheck_accepts_200(self, urlopen: Mock) -> None:
        response = Mock(status=200)
        urlopen.return_value.__enter__.return_value = response

        self.assertTrue(healthcheck())

    @patch("app.api_supervisor.urlopen", side_effect=TimeoutError)
    def test_healthcheck_rejects_timeout(self, _urlopen: Mock) -> None:
        self.assertFalse(healthcheck())

    def test_stop_process_escalates_after_timeout(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("uvicorn", 1), 0]

        stop_process(process, timeout=1)

        process.terminate.assert_called_once()
        process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
