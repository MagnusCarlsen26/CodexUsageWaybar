#!/usr/bin/env python3
"""Waybar custom module for Claude Code CLI usage limits."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
from typing import Any


DEFAULT_LABEL = "✳"
DEFAULT_CONFIG = {
    "label": DEFAULT_LABEL,
    "timeout_seconds": 25,
    "cache_seconds": 300,
    "status_retries": 2,
    "retry_delay_seconds": 3,
    "enabled": True,
}
CACHE_PATH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-usage-waybar" / "cache.json"
CONFIG_PATH = Path(
    os.environ.get("CLAUDE_USAGE_WAYBAR_CONFIG", Path.home() / ".config" / "claude-usage-waybar" / "config.json")
)

ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PERCENT_USED_RE = re.compile(r"(\d+)\s*%\s*used", re.IGNORECASE)
RESETS_RE = re.compile(r"Resets\s+(.+?)\s*$", re.IGNORECASE)

SESSION_RE = re.compile(r"Current session", re.IGNORECASE)
WEEK_ALL_RE = re.compile(r"Current week\s*\(all models\)", re.IGNORECASE)
# The third bucket is a per-model weekly limit whose name changes between
# Claude versions (Opus, Fable, …); match any model rather than a fixed name.
WEEK_MODEL_RE = re.compile(r"Current week\s*\((?!all models\))(.+?)\)", re.IGNORECASE)


def match_header(line: str) -> tuple[str, str | None] | None:
    """Return (section_key, model_name) if the line is a section header."""
    if WEEK_ALL_RE.search(line):
        return "week_all", None
    model = WEEK_MODEL_RE.search(line)
    if model is not None:
        return "week_model", model.group(1).strip()
    if SESSION_RE.search(line):
        return "session", None
    return None


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as handle:
                user_config = json.load(handle)
            if isinstance(user_config, dict):
                config.update(user_config)
        except (OSError, json.JSONDecodeError):
            pass
    return config


TERM_ROWS = 45
TERM_COLS = 160


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value).replace("\r", "\n")


def render_screen(data: bytes, rows: int = TERM_ROWS, cols: int = TERM_COLS) -> str:
    """Reconstruct the final terminal screen from raw PTY bytes.

    The /usage panel is an alt-screen TUI that repaints in place, so naive
    ANSI stripping merges overwritten frames and truncates lines (e.g. the
    per-model weekly header). Feeding the bytes through a real VT emulator
    yields the panel exactly as displayed. Falls back to strip_ansi if pyte
    is unavailable.
    """
    try:
        import pyte
    except ImportError:
        return strip_ansi(data.decode("utf-8", errors="replace"))
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    try:
        stream.feed(data)
    except Exception:
        return strip_ansi(data.decode("utf-8", errors="replace"))
    return "\n".join(screen.display)


def run_claude_usage(timeout: float) -> str:
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", TERM_ROWS, TERM_COLS, 0, 0))
    env = os.environ.copy()
    if env.get("TERM") in (None, "", "dumb"):
        env["TERM"] = "xterm-256color"
    proc = subprocess.Popen(
        ["/home/khushal/.local/bin/claude"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave_fd)
    output = bytearray()
    sent = False
    send_at = time.monotonic() + 4
    deadline = time.monotonic() + timeout

    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master_fd, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            text = strip_ansi(output.decode("utf-8", errors="replace"))
            if not sent and time.monotonic() >= send_at and "❯" in text:
                os.write(master_fd, b"/usage\n\r")
                sent = True
            if sent and "Current session" in text and "Current week" in text and RESETS_RE.search(text):
                # Let the panel finish repainting so reset strings aren't truncated,
                # then take the last fully-rendered copy of each section.
                capture_deadline = time.monotonic() + 2
                while time.monotonic() < capture_deadline:
                    readable, _, _ = select.select([master_fd], [], [], 0.1)
                    if readable:
                        try:
                            chunk = os.read(master_fd, 8192)
                        except OSError:
                            break
                        if chunk:
                            output.extend(chunk)
                break
        return render_screen(bytes(output))
    finally:
        try:
            os.write(master_fd, b"\x03\x04")
        except OSError:
            pass
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(timeout=1)
        os.close(master_fd)


def parse_claude_usage(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result: dict[str, Any] = {}
    current: str | None = None
    # The /usage TUI repaints several times and later frames get truncated,
    # which drops section headers (e.g. "Current week (all models)" -> "wek
    # (all models)"). Without a matched header the following percentages would
    # bleed into whichever section was previously current. To prevent that we
    # only accept the FIRST percentage after a header; any later stray percent
    # in the same block (from a header-less truncated section) is ignored until
    # a real header re-opens a section.
    awaiting_percent = False
    for line in lines:
        header = match_header(line)
        if header is not None:
            name, model = header
            current = name
            bucket = result.setdefault(current, {"percent_used": None, "reset": None})
            if model is not None:
                bucket["model"] = model
            awaiting_percent = True
            # a header line may also carry the percentage on the same wrapped line
            percent = PERCENT_USED_RE.search(line)
            if percent is not None:
                bucket["percent_used"] = int(percent.group(1))
                awaiting_percent = False
            continue
        if current is None:
            continue
        bucket = result[current]
        percent = PERCENT_USED_RE.search(line)
        if percent is not None and awaiting_percent:
            bucket["percent_used"] = int(percent.group(1))
            awaiting_percent = False
        # Alt-screen repaints can truncate the reset string, so keep the longest
        # (most complete) copy we observe rather than letting a partial clobber it.
        resets = RESETS_RE.search(line)
        if resets is not None:
            candidate = resets.group(1).strip()
            existing = bucket.get("reset")
            if existing is None or len(candidate) > len(existing):
                bucket["reset"] = candidate

    if not any(bucket.get("percent_used") is not None for bucket in result.values()):
        raise ValueError("Claude usage output did not include any limits")
    return result


def percent_left(bucket: dict[str, Any] | None) -> int | None:
    if not bucket:
        return None
    used = bucket.get("percent_used")
    if used is None:
        return None
    return max(0, 100 - int(used))


def status_class(*percents: int | None) -> str:
    values = [percent for percent in percents if percent is not None]
    if not values:
        return "error"
    lowest = min(values)
    if lowest <= 10:
        return "critical"
    if lowest <= 25:
        return "warn"
    return "ok"


def make_status_output(parsed: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    label = str(config.get("label", DEFAULT_LABEL))
    session_left = percent_left(parsed.get("session"))
    week_left = percent_left(parsed.get("week_all"))
    model_left = percent_left(parsed.get("week_model"))

    session_text = f"{session_left}%" if session_left is not None else "?%"
    week_text = f"{week_left}%" if week_left is not None else "?%"

    tooltip_lines = ["Claude Code usage", ""]
    session = parsed.get("session") or {}
    week = parsed.get("week_all") or {}
    model = parsed.get("week_model") or {}
    tooltip_lines.append(
        f"Session: {session_text} left" + (f" (resets {session['reset']})" if session.get("reset") else "")
    )
    tooltip_lines.append(
        f"Weekly (all models): {week_text} left" + (f" (resets {week['reset']})" if week.get("reset") else "")
    )
    if model_left is not None:
        model_name = model.get("model", "model")
        tooltip_lines.append(
            f"Weekly ({model_name}): {model_left}% left" + (f" (resets {model['reset']})" if model.get("reset") else "")
        )
    tooltip_lines.append("")
    tooltip_lines.append("Source: claude /usage")

    return {
        "text": f"{label} S {session_text} W {week_text}",
        "tooltip": "\n".join(tooltip_lines),
        "class": status_class(session_left, week_left, model_left),
    }


def format_datetime(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%a, %d %b %Y %I:%M %p %Z")


def make_stale_output(cached: dict[str, Any], message: str) -> dict[str, str]:
    tooltip = cached.get("tooltip") or ""
    suffix = ["Showing last known good status."]
    updated_at = cached.get("saved_at")
    if isinstance(updated_at, (int, float)):
        suffix.append(f"Last updated: {format_datetime(float(updated_at))}")
    suffix.append(f"Latest refresh failed: {message}")
    tooltip = (f"{tooltip}\n\n" if tooltip else "") + "\n".join(suffix)
    return {"text": cached["text"], "tooltip": tooltip, "class": "warn"}


def read_cache(max_age: int) -> dict[str, Any] | None:
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    saved_at = cached.get("saved_at")
    output = cached.get("output")
    if not isinstance(saved_at, (int, float)) or not isinstance(output, dict):
        return None
    if time.time() - float(saved_at) > max_age:
        return None
    if not all(isinstance(output.get(key), str) for key in ("text", "tooltip", "class")):
        return None
    return {
        "text": output["text"],
        "tooltip": output["tooltip"],
        "class": output["class"],
        "saved_at": float(saved_at),
    }


def write_cache(output: dict[str, str]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_PATH.open("w", encoding="utf-8") as handle:
            json.dump({"saved_at": time.time(), "output": output}, handle)
    except OSError:
        pass


def read_claude_usage_with_retries(config: dict[str, Any]) -> str:
    timeout = float(config.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"]))
    retries = max(1, int(config.get("status_retries", DEFAULT_CONFIG["status_retries"])))
    retry_delay = max(0.0, float(config.get("retry_delay_seconds", DEFAULT_CONFIG["retry_delay_seconds"])))
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            usage_text = run_claude_usage(timeout)
            parse_claude_usage(usage_text)
            return usage_text
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            last_error = exc
            if attempt < retries and retry_delay > 0:
                time.sleep(retry_delay)

    if last_error is None:
        raise ValueError("Claude usage output did not include any limits")
    raise last_error


def disabled_output() -> dict[str, str]:
    return {"text": "", "tooltip": "", "class": "disabled"}


def main() -> int:
    config = load_config()
    if not config.get("enabled", True):
        print(json.dumps(disabled_output(), ensure_ascii=True))
        return 0

    label = str(config.get("label", DEFAULT_LABEL))
    cache_seconds = int(config.get("cache_seconds", DEFAULT_CONFIG["cache_seconds"]))
    try:
        usage_text = read_claude_usage_with_retries(config)
        output = make_status_output(parse_claude_usage(usage_text), config)
        write_cache(output)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        retries = max(1, int(config.get("status_retries", DEFAULT_CONFIG["status_retries"])))
        message = f"Could not read Claude /usage after {retries} attempt(s): {exc}"
        cached = read_cache(max(cache_seconds, 1))
        if cached is not None:
            output = make_stale_output(cached, message)
        else:
            output = {"text": f"{label} status ?", "tooltip": message, "class": "error"}

    print(json.dumps(output, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
