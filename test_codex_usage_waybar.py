import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib import error

import codex_usage_waybar as module


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


class CodexUsageWaybarTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cache_path = Path(self.tmpdir.name) / "cache.json"
        self.env_path = Path(self.tmpdir.name) / "missing-env"
        self.cache_patch = mock.patch.object(module, "CACHE_PATH", self.cache_path)
        self.env_patch = mock.patch.object(module, "ENV_PATH", self.env_path)
        self.cache_patch.start()
        self.env_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(self.env_patch.stop)
        self.config = dict(module.DEFAULT_CONFIG)

    def test_local_usage_summary_calculates_rolling_windows(self):
        now = 1_800_000_000
        self.env_path.write_text("", encoding="utf-8")
        log_path = Path(self.tmpdir.name) / "codex.log"
        log_path.write_text(
            "\n".join(
                [
                    "2027-01-15T07:30:00Z  INFO session_loop:turn{otel.name=\"session_task.turn\" model=gpt-5.5 codex.turn.token_usage.total_tokens=1000}: close",
                    "2027-01-15T08:00:00Z  INFO session_loop:turn{otel.name=\"session_task.turn\" model=gpt-5.5 codex.turn.token_usage.total_tokens=2000}: close",
                    "2027-01-07T08:00:00Z  INFO session_loop:turn{otel.name=\"session_task.turn\" model=gpt-5.5 codex.turn.token_usage.total_tokens=9000}: close",
                ]
            ),
            encoding="utf-8",
        )
        config = dict(self.config)
        config.update({"codex_log_path": str(log_path), "five_hour_token_limit": 10_000, "weekly_token_limit": 20_000})

        summary = module.local_usage_summary(config, now=now)

        self.assertEqual(summary["five_hour"]["used_tokens"], 3000)
        self.assertEqual(summary["five_hour"]["remaining"], 7000)
        self.assertEqual(summary["weekly"]["used_tokens"], 3000)
        self.assertEqual(summary["weekly"]["remaining"], 17000)

    def test_summarizes_successful_cost_and_usage_response(self):
        cost_rows = [{"results": [{"amount": {"value": 1.23}}]}]
        usage_rows = [
            {
                "results": [
                    {"model": "gpt-5.2-codex", "input_tokens": 123000, "output_tokens": 45000},
                    {"model": "gpt-4.1", "input_tokens": 1000, "output_tokens": 2000},
                ]
            }
        ]

        output = module.make_output(
            *module.summarize_costs(cost_rows),
            module.summarize_usage(usage_rows, self.config["codex_model_patterns"]),
            self.config,
        )

        self.assertEqual(output["text"], "◎ $1.23")
        self.assertEqual(output["class"], "ok")
        self.assertIn("Input: 124k", output["tooltip"])
        self.assertIn("Output: 47k", output["tooltip"])
        self.assertIn("gpt-5.2-codex", output["tooltip"])

    def test_missing_api_key_still_outputs_local_usage(self):
        status_text = """
        5h limit:             [███████████░░░░░░░░░] 55% left (resets 16:56)
        Weekly limit:         [██████████████████░░] 88% left
                              (resets 13:50 on 20 May)
        """
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(module, "run_codex_status", return_value=status_text),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            module.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["text"], "◎ 5h 55% W 88%")
        self.assertEqual(payload["class"], "ok")
        self.assertNotIn("API billing", payload["tooltip"])
        self.assertIn("Source: codex /status", payload["tooltip"])

    def test_main_retries_status_before_error(self):
        status_text = """
        5h limit:             [███████████░░░░░░░░░] 55% left (resets 16:56)
        Weekly limit:         [██████████████████░░] 88% left
                              (resets 13:50 on 20 May)
        """
        self.config.update({"status_retries": 2, "retry_delay_seconds": 0})
        with (
            mock.patch.object(module, "load_config", return_value=self.config),
            mock.patch.object(module, "run_codex_status", side_effect=[ValueError("boot not ready"), status_text]) as run_status,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            module.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(run_status.call_count, 2)
        self.assertEqual(payload["text"], "◎ 5h 55% W 88%")
        self.assertEqual(payload["class"], "ok")

    def test_main_does_not_show_cached_status_when_refresh_fails(self):
        cached_output = {
            "text": "◎ 5h 55% W 88%",
            "tooltip": "Codex CLI status\n5-hour limit: 55% left (resets 16:56)\nWeekly limit: 88% left (resets 13:50 on 20 May)\nSource: codex /status",
            "class": "ok",
        }
        with (
            mock.patch("time.time", return_value=1_700_000_000),
        ):
            module.write_cache(cached_output)

        self.config.update({"status_retries": 2, "retry_delay_seconds": 0})
        with (
            mock.patch("time.time", return_value=1_700_000_060),
            mock.patch.object(module, "load_config", return_value=self.config),
            mock.patch.object(module, "run_codex_status", side_effect=ValueError("boom")) as run_status,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            module.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(run_status.call_count, 2)
        self.assertEqual(payload["text"], "◎ status ?")
        self.assertEqual(payload["class"], "error")
        self.assertIn("after 2 attempt(s)", payload["tooltip"])
        self.assertNotIn(cached_output["text"], payload["text"])
        self.assertNotIn("Showing last known good status.", payload["tooltip"])

    def test_main_falls_back_to_error_when_refresh_fails_without_cache(self):
        self.config.update({"status_retries": 3, "retry_delay_seconds": 0})
        with (
            mock.patch.object(module, "load_config", return_value=self.config),
            mock.patch.object(module, "run_codex_status", side_effect=ValueError("boom")),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            module.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["text"], "◎ status ?")
        self.assertEqual(payload["class"], "error")
        self.assertEqual(payload["tooltip"], "Could not read Codex CLI /status after 3 attempt(s): boom")

    def test_failed_refresh_does_not_overwrite_last_successful_cache(self):
        status_text = """
        5h limit:             [███████████░░░░░░░░░] 55% left (resets 16:56)
        Weekly limit:         [██████████████████░░] 88% left
                              (resets 13:50 on 20 May)
        """
        with mock.patch.object(module, "run_codex_status", return_value=status_text):
            module.main()

        before = module.read_cache(300)
        self.assertIsNotNone(before)
        self.assertEqual(before["text"], "◎ 5h 55% W 88%")

        self.config.update({"status_retries": 1, "retry_delay_seconds": 0})
        with (
            mock.patch.object(module, "load_config", return_value=self.config),
            mock.patch.object(module, "run_codex_status", side_effect=ValueError("boom")),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            module.main()

        after = module.read_cache(300)
        self.assertIsNotNone(after)
        self.assertEqual(after["text"], "◎ 5h 55% W 88%")
        self.assertEqual(after["tooltip"], before["tooltip"])

    def test_expired_cached_status_is_not_reused(self):
        cached_output = {
            "text": "◎ 5h 55% W 88%",
            "tooltip": "Codex CLI status",
            "class": "ok",
        }
        module.write_cache(cached_output)
        self.config.update({"status_retries": 1, "retry_delay_seconds": 0})

        with (
            mock.patch("time.time", return_value=1_000),
            mock.patch.object(module, "load_config", return_value=self.config),
            mock.patch.object(module, "run_codex_status", side_effect=ValueError("boom")),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            module.write_cache(cached_output)

        with (
            mock.patch("time.time", return_value=1_301),
            mock.patch.object(module, "load_config", return_value=self.config),
            mock.patch.object(module, "run_codex_status", side_effect=ValueError("boom")),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            module.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["text"], "◎ status ?")
        self.assertEqual(payload["class"], "error")

    def test_parse_codex_status(self):
        parsed = module.parse_codex_status(
            "5h limit: [████░] 21% left (resets 16:56)\n"
            "Weekly limit: [████████░] 74% left (resets 13:50 on 20 May)"
        )

        self.assertEqual(parsed["five_hour_percent"], 21)
        self.assertEqual(parsed["five_hour_reset"], "16:56")
        self.assertEqual(parsed["weekly_percent"], 74)
        self.assertEqual(parsed["weekly_reset"], "13:50 on 20 May")

    def test_parse_codex_status_allows_weekly_only(self):
        parsed = module.parse_codex_status("Weekly limit: [████████░] 74% left (resets 13:50 on 20 May)")

        self.assertIsNone(parsed["five_hour_percent"])
        self.assertIsNone(parsed["five_hour_reset"])
        self.assertEqual(parsed["weekly_percent"], 74)
        self.assertEqual(parsed["weekly_reset"], "13:50 on 20 May")

    def test_http_401_maps_to_auth(self):
        exc = error.HTTPError("https://example.test", 401, "Unauthorized", Message(), io.BytesIO())
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            with self.assertRaises(module.ApiError) as raised:
                module.fetch_json("https://example.test", "secret", 1)

        self.assertEqual(raised.exception.kind, "auth")

    def test_http_429_maps_to_rate_limit(self):
        exc = error.HTTPError("https://example.test", 429, "Rate limited", Message(), io.BytesIO())
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            with self.assertRaises(module.ApiError) as raised:
                module.fetch_json("https://example.test", "secret", 1)

        self.assertEqual(raised.exception.kind, "429")

    def test_network_timeout_maps_to_net(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
            with self.assertRaises(module.ApiError) as raised:
                module.fetch_json("https://example.test", "secret", 1)

        self.assertEqual(raised.exception.kind, "net")

    def test_malformed_json_maps_to_unknown(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"{bad")):
            with self.assertRaises(module.ApiError) as raised:
                module.fetch_json("https://example.test", "secret", 1)

        self.assertEqual(raised.exception.kind, "unknown")

    def test_cache_fallback_changes_class_to_warn(self):
        output = {"text": "◎ $1.00", "tooltip": "cached", "class": "ok"}
        module.write_cache(output)

        cached = module.read_cache(300)

        self.assertEqual(cached["text"], "◎ $1.00")
        self.assertEqual(cached["class"], "warn")

    def test_threshold_classes(self):
        self.assertEqual(module.classify(1.0, self.config), "ok")
        self.assertEqual(module.classify(5.0, self.config), "warn")
        self.assertEqual(module.classify(10.0, self.config), "critical")

    def test_paginated_fetch_uses_next_page(self):
        responses = [
            {"data": [{"results": [{"amount": {"value": 1}}]}], "next_page": "cursor_1"},
            {"data": [{"results": [{"amount": {"value": 2}}]}]},
        ]
        with mock.patch("urllib.request.urlopen", side_effect=[FakeResponse(responses[0]), FakeResponse(responses[1])]):
            rows = module.fetch_paginated("/costs", [("start_time", 1)], "secret", 1)

        total, found = module.summarize_costs(rows)
        self.assertTrue(found)
        self.assertEqual(total, 3.0)

    def test_live_output_keeps_cost_when_usage_endpoint_fails(self):
        def fake_fetch(path, params, api_key, timeout):
            if path == "/usage/completions":
                raise module.ApiError("api", "OpenAI API returned HTTP 500")
            return [{"results": [{"amount": {"value": 2.5}}]}]

        with mock.patch.object(module, "fetch_paginated", side_effect=fake_fetch):
            output = module.live_output(self.config, "secret")

        self.assertEqual(output["text"], "◎ $2.50")
        self.assertEqual(output["class"], "ok")
        self.assertIn("Token/model usage unavailable", output["tooltip"])

    def test_main_disabled_skips_status_lookup(self):
        self.config["enabled"] = False
        with (
            mock.patch.object(module, "load_config", return_value=self.config),
            mock.patch.object(module, "run_codex_status") as run_status,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = module.main()

        run_status.assert_not_called()
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["class"], "disabled")
        self.assertEqual(payload["text"], "")


if __name__ == "__main__":
    unittest.main()
