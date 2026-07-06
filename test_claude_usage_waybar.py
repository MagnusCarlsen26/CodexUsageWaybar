import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_usage_waybar as module


SAMPLE_USAGE = """
 Settings  Status  Config  Usage  Stats

 Current session
██                                                4%used
Resets 9:30pm (Asia/Kolkata)
Current week (all models)
                                                  0%used
Resets Jul 6, 7:30pm (Asia/Kolkata)
Current week (Opus)
███                                               12%used
Resets Jul 6, 7:30pm (Asia/Kolkata)
What's contributing to your limits usage?
Scanning local sessions…
"""


class ParseTests(unittest.TestCase):
    def test_parses_all_sections(self):
        parsed = module.parse_claude_usage(SAMPLE_USAGE)
        self.assertEqual(parsed["session"]["percent_used"], 4)
        self.assertEqual(parsed["session"]["reset"], "9:30pm (Asia/Kolkata)")
        self.assertEqual(parsed["week_all"]["percent_used"], 0)
        self.assertEqual(parsed["week_all"]["reset"], "Jul 6, 7:30pm (Asia/Kolkata)")
        self.assertEqual(parsed["week_opus"]["percent_used"], 12)

    def test_parses_without_opus(self):
        text = """
 Current session
██                                                30%used
Resets 5:00pm (UTC)
Current week (all models)
█████                                             45%used
Resets Jul 9, 5:00pm (UTC)
"""
        parsed = module.parse_claude_usage(text)
        self.assertEqual(parsed["session"]["percent_used"], 30)
        self.assertEqual(parsed["week_all"]["percent_used"], 45)
        self.assertNotIn("week_opus", parsed)

    def test_raises_when_no_limits(self):
        with self.assertRaises(ValueError):
            module.parse_claude_usage("nothing useful here")


class OutputTests(unittest.TestCase):
    def test_percent_left_conversion(self):
        self.assertEqual(module.percent_left({"percent_used": 4}), 96)
        self.assertEqual(module.percent_left({"percent_used": 100}), 0)
        self.assertEqual(module.percent_left({"percent_used": 150}), 0)
        self.assertIsNone(module.percent_left({"percent_used": None}))
        self.assertIsNone(module.percent_left(None))

    def test_status_class_thresholds(self):
        self.assertEqual(module.status_class(96, 100), "ok")
        self.assertEqual(module.status_class(96, 20), "warn")
        self.assertEqual(module.status_class(5, 80), "critical")
        self.assertEqual(module.status_class(None, None), "error")

    def test_make_status_output(self):
        parsed = module.parse_claude_usage(SAMPLE_USAGE)
        output = module.make_status_output(parsed, {"label": "✳"})
        self.assertEqual(output["text"], "✳ S 96% W 100%")
        self.assertEqual(output["class"], "ok")
        self.assertIn("Session: 96% left (resets 9:30pm (Asia/Kolkata))", output["tooltip"])
        self.assertIn("Weekly (Opus): 88% left", output["tooltip"])
        self.assertIn("Source: claude /usage", output["tooltip"])

    def test_missing_bucket_shows_placeholder(self):
        output = module.make_status_output({"session": {"percent_used": 50, "reset": None}}, {"label": "✳"})
        self.assertEqual(output["text"], "✳ S 50% W ?%")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cache_path = Path(self.tmpdir.name) / "cache.json"
        patcher = mock.patch.object(module, "CACHE_PATH", self.cache_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roundtrip(self):
        output = {"text": "✳ S 96% W 100%", "tooltip": "hi", "class": "ok"}
        module.write_cache(output)
        cached = module.read_cache(300)
        self.assertEqual(cached["text"], output["text"])
        self.assertEqual(cached["class"], "ok")

    def test_stale_output_annotates_tooltip(self):
        module.write_cache({"text": "✳ S 96% W 100%", "tooltip": "orig", "class": "ok"})
        cached = module.read_cache(300)
        stale = module.make_stale_output(cached, "boom")
        self.assertEqual(stale["class"], "warn")
        self.assertIn("Showing last known good status.", stale["tooltip"])
        self.assertIn("boom", stale["tooltip"])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cache_path = Path(self.tmpdir.name) / "cache.json"
        patcher = mock.patch.object(module, "CACHE_PATH", self.cache_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disabled_config_emits_blank(self):
        with mock.patch.object(module, "load_config", return_value={"enabled": False}):
            with mock.patch("builtins.print") as fake_print:
                module.main()
        payload = json.loads(fake_print.call_args[0][0])
        self.assertEqual(payload, {"text": "", "tooltip": "", "class": "disabled"})

    def test_failure_falls_back_to_cache(self):
        module.write_cache({"text": "✳ S 96% W 100%", "tooltip": "orig", "class": "ok"})
        with mock.patch.object(module, "load_config", return_value=dict(module.DEFAULT_CONFIG)):
            with mock.patch.object(module, "read_claude_usage_with_retries", side_effect=ValueError("nope")):
                with mock.patch("builtins.print") as fake_print:
                    module.main()
        payload = json.loads(fake_print.call_args[0][0])
        self.assertEqual(payload["text"], "✳ S 96% W 100%")
        self.assertEqual(payload["class"], "warn")

    def test_failure_without_cache_emits_error(self):
        with mock.patch.object(module, "load_config", return_value=dict(module.DEFAULT_CONFIG)):
            with mock.patch.object(module, "read_claude_usage_with_retries", side_effect=ValueError("nope")):
                with mock.patch("builtins.print") as fake_print:
                    module.main()
        payload = json.loads(fake_print.call_args[0][0])
        self.assertEqual(payload["class"], "error")
        self.assertIn("status ?", payload["text"])


if __name__ == "__main__":
    unittest.main()
