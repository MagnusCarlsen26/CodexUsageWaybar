import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

import agent_usage_waybar as module


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")


SAMPLE_USAGE_RESPONSE = {
    "billingCycleStart": "1780721542000",
    "billingCycleEnd": "1783313542000",
    "planUsage": {
        "totalSpend": 4742,
        "includedSpend": 2000,
        "bonusSpend": 2742,
        "limit": 2000,
        "remainingBonus": False,
        "bonusTooltip": "Bonus credits from model providers.",
        "autoPercentUsed": 31.613333333333333,
        "apiPercentUsed": 0,
        "totalPercentUsed": 24.317948717948717,
    },
    "enabled": True,
    "displayMessage": "You've hit your usage limit",
}


class AgentUsageWaybarTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cache_path = Path(self.tmpdir.name) / "cache.json"
        self.auth_path = Path(self.tmpdir.name) / "auth.json"
        self.config_path = Path(self.tmpdir.name) / "config.json"
        self.cache_patch = mock.patch.object(module, "CACHE_PATH", self.cache_path)
        self.config_path_patch = mock.patch.object(module, "CONFIG_PATH", self.config_path)
        self.cache_patch.start()
        self.config_path_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(self.config_path_patch.stop)
        self.config = dict(module.DEFAULT_CONFIG)
        self.config["auth_path"] = str(self.auth_path)

    def test_parse_plan_usage_percent_remaining(self):
        parsed = module.parse_plan_usage(SAMPLE_USAGE_RESPONSE)

        self.assertEqual(parsed["auto_left"], 68)
        self.assertEqual(parsed["api_left"], 100)
        self.assertEqual(parsed["total_left"], 76)
        self.assertTrue(parsed["has_percent_buckets"])
        self.assertIsNotNone(parsed["billing_start"])
        self.assertIsNotNone(parsed["billing_end"])

    def test_make_agent_status_output(self):
        parsed = module.parse_plan_usage(SAMPLE_USAGE_RESPONSE)
        output = module.make_agent_status_output(parsed, self.config)

        self.assertEqual(output["text"], "◉ ◈ 68% </> 100%")
        self.assertEqual(output["class"], "ok")
        self.assertIn("Auto + Composer: 68% remaining", output["tooltip"])
        self.assertIn("API: 100% remaining", output["tooltip"])
        self.assertIn("Included spend: $20.00 / $20.00", output["tooltip"])
        self.assertIn("Bonus credits used: $27.42", output["tooltip"])
        self.assertIn("Billing period:", output["tooltip"])

    def test_status_class_thresholds(self):
        self.assertEqual(module.status_class(30, 50), "ok")
        self.assertEqual(module.status_class(20, 50), "warn")
        self.assertEqual(module.status_class(5, 50), "critical")
        self.assertEqual(module.status_class(None, None), "error")

    def test_parse_enterprise_usage_picks_highest_limit_bucket(self):
        response = {
            "gpt-3.5-turbo": {
                "numRequests": 10,
                "maxRequestUsage": 100,
            },
            "gpt-4": {
                "numRequests": 50,
                "maxRequestUsage": 500,
            },
            "startOfMonth": "2026-06-06T04:52:22.000Z",
        }

        parsed = module.parse_enterprise_usage(response)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["model"], "gpt-4")
        self.assertEqual(parsed["remaining_requests"], 450)
        self.assertEqual(parsed["limit_requests"], 500)

    def test_make_enterprise_status_output(self):
        parsed = {
            "model": "gpt-4",
            "remaining_requests": 350,
            "used_requests": 150,
            "limit_requests": 500,
            "cycle_start": 1_800_000_000.0,
        }
        output = module.make_enterprise_status_output(parsed, self.config)

        self.assertEqual(output["text"], "◉ 350 req")
        self.assertEqual(output["class"], "ok")
        self.assertIn("Model bucket: gpt-4", output["tooltip"])
        self.assertIn("Requests remaining: 350 / 500", output["tooltip"])

    def test_missing_auth_outputs_error(self):
        missing_auth = Path(self.tmpdir.name) / "missing-auth.json"
        config = dict(self.config)
        config["auth_path"] = str(missing_auth)
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(module, "load_config", return_value=config),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            module.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["text"], "◉ status ?")
        self.assertEqual(payload["class"], "error")
        self.assertIn("agent login", payload["tooltip"])

    def test_main_success_writes_cache(self):
        self.auth_path.write_text(json.dumps({"accessToken": "secret"}), encoding="utf-8")

        def fake_urlopen(req, timeout=0):
            url = req.full_url
            if url.endswith(module.USAGE_PATH):
                return FakeResponse(SAMPLE_USAGE_RESPONSE)
            raise AssertionError(f"unexpected url: {url}")

        with (
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            module.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["text"], "◉ ◈ 68% </> 100%")
        cached = module.read_cache(300)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached["text"], payload["text"])

    def test_main_enterprise_fallback_when_plan_usage_missing(self):
        self.auth_path.write_text(json.dumps({"accessToken": "secret"}), encoding="utf-8")
        enterprise_response = {
            "gpt-4": {
                "numRequests": 150,
                "maxRequestUsage": 500,
            },
            "startOfMonth": "2026-06-06T04:52:22.000Z",
        }

        def fake_urlopen(req, timeout=0):
            url = req.full_url
            if url.endswith(module.USAGE_PATH):
                return FakeResponse({"billingCycleStart": "1780721542000", "billingCycleEnd": "1783313542000"})
            if url.endswith(module.ENTERPRISE_USAGE_PATH):
                return FakeResponse(enterprise_response)
            raise AssertionError(f"unexpected url: {url}")

        with (
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            module.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["text"], "◉ 350 req")
        self.assertEqual(payload["class"], "ok")

    def test_http_failure_after_retries_outputs_error(self):
        self.auth_path.write_text(json.dumps({"accessToken": "secret"}), encoding="utf-8")
        self.config.update({"status_retries": 2, "retry_delay_seconds": 0})
        exc = module.ApiError("net", "network down")

        with (
            mock.patch.object(module, "load_config", return_value=self.config),
            mock.patch.object(module, "fetch_current_period_usage", side_effect=exc),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            module.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["text"], "◉ net")
        self.assertEqual(payload["class"], "error")
        self.assertIn("after 2 attempt(s)", payload["tooltip"])

    def test_http_401_maps_to_auth(self):
        exc = Message()
        http_error = __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            "https://example.test",
            401,
            "Unauthorized",
            exc,
            io.BytesIO(),
        )
        with mock.patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(module.ApiError) as raised:
                module.request_json("POST", "https://example.test", "secret", 1, body={})

        self.assertEqual(raised.exception.kind, "auth")

    def test_failed_refresh_does_not_overwrite_last_successful_cache(self):
        self.auth_path.write_text(json.dumps({"accessToken": "secret"}), encoding="utf-8")

        def fake_urlopen(req, timeout=0):
            if req.full_url.endswith(module.USAGE_PATH):
                return FakeResponse(SAMPLE_USAGE_RESPONSE)
            raise AssertionError("unexpected request")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            module.main()

        before = module.read_cache(300)
        self.assertIsNotNone(before)

        self.config.update({"status_retries": 1, "retry_delay_seconds": 0})
        with (
            mock.patch.object(module, "load_config", return_value=self.config),
            mock.patch.object(
                module,
                "fetch_current_period_usage",
                side_effect=module.ApiError("net", "boom"),
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            module.main()

        after = module.read_cache(300)
        self.assertIsNotNone(after)
        assert after is not None
        assert before is not None
        self.assertEqual(after["text"], before["text"])


if __name__ == "__main__":
    unittest.main()
