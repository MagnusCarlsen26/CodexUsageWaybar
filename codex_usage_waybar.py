#!/usr/bin/env python3
"""Waybar custom module for local Codex usage estimates."""

from __future__ import annotations

import datetime as dt
import fcntl
import fnmatch
import json
import os
import pty
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from typing import Any
from urllib import error, parse, request


API_BASE = "https://api.openai.com/v1/organization"
DEFAULT_LABEL = "◎"
DEFAULT_CONFIG = {
    "currency": "USD",
    "label": DEFAULT_LABEL,
    "codex_model_patterns": ["codex", "gpt-5.*codex"],
    "codex_log_path": str(Path.home() / ".codex" / "log" / "codex-tui.log"),
    "five_hour_token_limit": 0,
    "weekly_token_limit": 0,
    "daily_warn_usd": 5.0,
    "daily_critical_usd": 10.0,
    "timeout_seconds": 20,
    "cache_seconds": 300,
    "status_retries": 2,
    "retry_delay_seconds": 3,
    "enabled": True,
}
CACHE_PATH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "codex-usage-waybar" / "cache.json"
CONFIG_PATH = Path(os.environ.get("CODEX_USAGE_WAYBAR_CONFIG", Path.home() / ".config" / "codex-usage-waybar" / "config.json"))
ENV_PATH = Path(os.environ.get("CODEX_USAGE_WAYBAR_ENV", Path.home() / ".config" / "codex-usage-waybar" / "env"))


class ApiError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


TOKEN_USAGE_RE = re.compile(r"codex\.turn\.token_usage\.total_tokens=(\d+)")
TURN_MODEL_RE = re.compile(r"\bmodel=([^\s}:]+)")
ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
FIVE_HOUR_STATUS_RE = re.compile(r"5h limit:\s*(?:\[[^\]]+\]\s*)?(?P<percent>\d+)% left\s*(?:\(resets (?P<reset>[^)]+)\))?", re.IGNORECASE)
WEEKLY_STATUS_RE = re.compile(r"Weekly limit:\s*(?:\[[^\]]+\]\s*)?(?P<percent>\d+)% left(?:\s*\(resets (?P<reset>[^)]+)\))?", re.IGNORECASE)


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


def get_api_key() -> str | None:
    return os.environ.get("OPENAI_ADMIN_KEY") or os.environ.get("OPENAI_API_KEY") or read_key_from_env_file()


def read_key_from_env_file() -> str | None:
    try:
        with ENV_PATH.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in ("OPENAI_ADMIN_KEY", "OPENAI_API_KEY"):
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            return value
    return None


def today_range(now: float | None = None) -> tuple[int, int]:
    current = dt.datetime.fromtimestamp(now or time.time()).astimezone()
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(current.timestamp())


def build_url(path: str, params: list[tuple[str, str | int]]) -> str:
    return f"{API_BASE}{path}?{parse.urlencode(params)}"


def fetch_json(url: str, api_key: str, timeout: float) -> dict[str, Any]:
    req = request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "codex-usage-waybar/1.0",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        if exc.code == 401 or exc.code == 403:
            raise ApiError("auth", f"OpenAI API returned HTTP {exc.code}") from exc
        if exc.code == 429:
            raise ApiError("429", "OpenAI API rate limit reached") from exc
        raise ApiError("api", f"OpenAI API returned HTTP {exc.code}") from exc
    except TimeoutError as exc:
        raise ApiError("net", "Network timeout while calling OpenAI API") from exc
    except error.URLError as exc:
        raise ApiError("net", f"Network error while calling OpenAI API: {exc.reason}") from exc
    except OSError as exc:
        raise ApiError("net", f"Network error while calling OpenAI API: {exc}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ApiError("unknown", "OpenAI API returned malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise ApiError("unknown", "OpenAI API returned an unexpected response shape")
    return parsed


def fetch_paginated(path: str, params: list[tuple[str, str | int]], api_key: str, timeout: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page: str | None = None

    while True:
        query = list(params)
        if page:
            query.append(("page", page))
        payload = fetch_json(build_url(path, query), api_key, timeout)
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise ApiError("unknown", f"OpenAI API {path} response did not include a data list")
        rows.extend(item for item in data if isinstance(item, dict))

        next_page = payload.get("next_page") or payload.get("next")
        if not next_page:
            break
        if not isinstance(next_page, str):
            raise ApiError("unknown", f"OpenAI API {path} response included an invalid page cursor")
        page = next_page

    return rows


def iter_results(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for bucket in rows:
        bucket_results = bucket.get("results")
        if isinstance(bucket_results, list):
            results.extend(item for item in bucket_results if isinstance(item, dict))
        else:
            results.append(bucket)
    return results


def extract_amount(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("value", "amount", "usd"):
            if isinstance(value.get(key), (int, float)):
                return float(value[key])
    return 0.0


def summarize_costs(rows: list[dict[str, Any]]) -> tuple[float, bool]:
    results = iter_results(rows)
    total = 0.0
    found = False
    for item in results:
        amount = item.get("amount") or item.get("cost") or item.get("total")
        value = extract_amount(amount)
        if value:
            found = True
        total += value
    return total, found or bool(results)


def model_matches(model: str, patterns: list[str]) -> bool:
    lowered = model.lower()
    for pattern in patterns:
        pattern_lower = str(pattern).lower()
        if re.search(pattern_lower, lowered):
            return True
        if fnmatch.fnmatch(lowered, pattern_lower):
            return True
    return False


def summarize_usage(rows: list[dict[str, Any]], patterns: list[str]) -> dict[str, Any]:
    input_tokens = 0
    output_tokens = 0
    codex_models: set[str] = set()
    all_models: set[str] = set()

    for item in iter_results(rows):
        model = item.get("model")
        if isinstance(model, str) and model:
            all_models.add(model)
            if model_matches(model, patterns):
                codex_models.add(model)

        input_tokens += int(item.get("input_tokens") or item.get("prompt_tokens") or 0)
        output_tokens += int(item.get("output_tokens") or item.get("completion_tokens") or 0)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "codex_models": sorted(codex_models),
        "all_models": sorted(all_models),
    }


def format_money(amount: float, currency: str) -> str:
    symbol = "$" if currency.upper() == "USD" else f"{currency.upper()} "
    return f"{symbol}{amount:.2f}"


def format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.0f}k"
    return str(tokens)


def parse_log_timestamp(line: str) -> float | None:
    if len(line) < 20:
        return None
    raw = line.split(" ", 1)[0]
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_codex_turns(log_path: str) -> list[dict[str, Any]]:
    path = Path(log_path).expanduser()
    turns: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "session_task.turn" not in line or "codex.turn.token_usage.total_tokens=" not in line:
                    continue
                ts = parse_log_timestamp(line)
                if ts is None:
                    continue
                token_match = TOKEN_USAGE_RE.search(line)
                if token_match is None:
                    continue
                model_match = TURN_MODEL_RE.search(line)
                turns.append(
                    {
                        "ts": ts,
                        "tokens": int(token_match.group(1)),
                        "model": model_match.group(1) if model_match else "unknown",
                    }
                )
    except OSError:
        return []
    return turns


def summarize_window(turns: list[dict[str, Any]], now: float, seconds: int, limit: int) -> dict[str, Any]:
    start = now - seconds
    in_window = [turn for turn in turns if float(turn["ts"]) >= start]
    used_tokens = sum(int(turn["tokens"]) for turn in in_window)
    used_turns = len(in_window)
    oldest_ts = min((float(turn["ts"]) for turn in in_window), default=None)
    reset_ts = oldest_ts + seconds if oldest_ts is not None else now + seconds
    remaining = max(limit - used_tokens, 0) if limit > 0 else None
    percent_left = (remaining / limit * 100) if remaining is not None and limit > 0 else None
    return {
        "used_tokens": used_tokens,
        "used_turns": used_turns,
        "limit": limit,
        "remaining": remaining,
        "percent_left": percent_left,
        "reset_ts": reset_ts,
    }


def local_usage_summary(config: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    current = now or time.time()
    turns = read_codex_turns(str(config.get("codex_log_path", DEFAULT_CONFIG["codex_log_path"])))
    return {
        "five_hour": summarize_window(turns, current, 5 * 60 * 60, int(config.get("five_hour_token_limit") or 0)),
        "weekly": summarize_window(turns, current, 7 * 24 * 60 * 60, int(config.get("weekly_token_limit") or 0)),
        "total_turns_seen": len(turns),
        "source": str(config.get("codex_log_path", DEFAULT_CONFIG["codex_log_path"])),
    }


def format_datetime(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%a, %d %b %Y %I:%M %p %Z")


def format_remaining(window: dict[str, Any]) -> str:
    remaining = window.get("remaining")
    if remaining is None:
        return "limit not set"
    return f"{format_tokens(int(remaining))} left ({window['percent_left']:.0f}%)"


def local_class(local: dict[str, Any]) -> str:
    percentages = [
        window["percent_left"]
        for window in (local["five_hour"], local["weekly"])
        if window.get("percent_left") is not None
    ]
    if not percentages:
        return "warn"
    lowest = min(percentages)
    if lowest <= 10:
        return "critical"
    if lowest <= 25:
        return "warn"
    return "ok"


def classify(amount: float, config: dict[str, Any], partial: bool = False) -> str:
    if amount >= float(config.get("daily_critical_usd", DEFAULT_CONFIG["daily_critical_usd"])):
        return "critical"
    if partial or amount >= float(config.get("daily_warn_usd", DEFAULT_CONFIG["daily_warn_usd"])):
        return "warn"
    return "ok"


def make_output(
    amount: float,
    cost_found: bool,
    usage: dict[str, Any],
    config: dict[str, Any],
    note: str | None = None,
    cached: bool = False,
) -> dict[str, str]:
    label = str(config.get("label", DEFAULT_LABEL))
    money = format_money(amount, str(config.get("currency", DEFAULT_CONFIG["currency"])))
    cls = classify(amount, config, partial=(not cost_found or cached))

    tooltip_lines = [
        f"Today: {money}",
        "Cost total is organization API spend for today.",
    ]
    if cached:
        tooltip_lines.append("Showing cached data because the live request failed.")
    if not cost_found:
        tooltip_lines.append("No cost records were returned for this period.")
    if usage.get("input_tokens") or usage.get("output_tokens"):
        tooltip_lines.append(f"Input: {format_tokens(int(usage.get('input_tokens', 0)))}")
        tooltip_lines.append(f"Output: {format_tokens(int(usage.get('output_tokens', 0)))}")
    codex_models = usage.get("codex_models") or []
    if codex_models:
        tooltip_lines.append(f"Codex-like models: {', '.join(codex_models)}")
    elif usage.get("all_models"):
        tooltip_lines.append("No Codex-like model usage found in grouped usage data.")
    tooltip_lines.append("Costs are not filtered to Codex models unless OpenAI returns model-level cost data.")
    if note:
        tooltip_lines.append(note)

    return {
        "text": f"{label} {money}",
        "tooltip": "\n".join(tooltip_lines),
        "class": cls,
    }


def make_combined_output(
    local: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, str]:
    label = str(config.get("label", DEFAULT_LABEL))
    five = local["five_hour"]
    week = local["weekly"]

    if five.get("percent_left") is None:
        five_text = "5h ?%"
    else:
        five_text = f"5h {five['percent_left']:.0f}%"
    if week.get("percent_left") is None:
        week_text = "W ?%"
    else:
        week_text = f"W {week['percent_left']:.0f}%"

    tooltip_lines = [
        "Codex local usage estimate",
        "",
        "5-hour window:",
        f"  Used: {format_tokens(int(five['used_tokens']))} tokens across {five['used_turns']} turns",
        f"  Remaining: {format_remaining(five)}",
        f"  Window ends: {format_datetime(float(five['reset_ts']))}",
        "",
        "Weekly window:",
        f"  Used: {format_tokens(int(week['used_tokens']))} tokens across {week['used_turns']} turns",
        f"  Remaining: {format_remaining(week)}",
        f"  Window ends: {format_datetime(float(week['reset_ts']))}",
        "",
        "This is estimated from local Codex logs, not an official subscription quota endpoint.",
    ]
    if five.get("limit", 0) <= 0 or week.get("limit", 0) <= 0:
        tooltip_lines.append("Set five_hour_token_limit and weekly_token_limit in ~/.config/codex-usage-waybar/config.json to show left values.")

    return {
        "text": f"{label} {five_text} {week_text}",
        "tooltip": "\n".join(tooltip_lines),
        "class": local_class(local),
    }


def error_output(kind: str, message: str, config: dict[str, Any]) -> dict[str, str]:
    label = str(config.get("label", DEFAULT_LABEL))
    suffix = {
        "missing_key": "no key",
        "auth": "auth",
        "429": "429",
        "net": "net",
    }.get(kind, "?")
    return {
        "text": f"{label} {suffix}",
        "tooltip": message,
        "class": "error",
    }


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
        "class": "warn",
        "saved_at": float(saved_at),
    }


def write_cache(output: dict[str, str]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_PATH.open("w", encoding="utf-8") as handle:
            json.dump({"saved_at": time.time(), "output": output}, handle)
    except OSError:
        pass


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value).replace("\r", "\n")


def run_codex_status(timeout: float) -> str:
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
    env = os.environ.copy()
    if env.get("TERM") in (None, "", "dumb"):
        env["TERM"] = "xterm-256color"
    proc = subprocess.Popen(
        ["codex", "--no-alt-screen"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave_fd)
    output = bytearray()
    status_attempts = 0
    handled_update_prompt = False
    send_at = time.monotonic() + 3
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
            if not handled_update_prompt and "Update available!" in text and "Skip" in text:
                os.write(master_fd, b"2\n\r")
                handled_update_prompt = True
                send_at = time.monotonic() + 1
                continue
            if status_attempts == 0 and time.monotonic() >= send_at and "›" in text:
                os.write(master_fd, b"/status\n\r")
                status_attempts += 1
            if status_attempts > 0 and "refresh requested; run /status again shortly" in text:
                os.write(master_fd, b"/status\n\r")
                status_attempts += 1
                output.clear()
                time.sleep(2)
                continue
            if status_attempts > 0 and "5h limit:" in text and "Weekly limit:" in text:
                break
        return strip_ansi(output.decode("utf-8", errors="replace"))
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


def parse_codex_status(text: str) -> dict[str, str | int | None]:
    normalized = " ".join(line.strip() for line in text.splitlines() if line.strip())
    five = FIVE_HOUR_STATUS_RE.search(normalized)
    weekly = WEEKLY_STATUS_RE.search(normalized)
    if not five and not weekly:
        raise ValueError("Codex status output did not include limits")
    return {
        "five_hour_percent": int(five.group("percent")) if five else None,
        "five_hour_reset": five.group("reset") if five else None,
        "weekly_percent": int(weekly.group("percent")) if weekly else None,
        "weekly_reset": weekly.group("reset") if weekly else None,
    }


def status_class(five_hour_percent: int | None, weekly_percent: int | None) -> str:
    percentages = [percent for percent in (five_hour_percent, weekly_percent) if percent is not None]
    if not percentages:
        return "error"
    lowest = min(percentages)
    if lowest <= 10:
        return "critical"
    if lowest <= 25:
        return "warn"
    return "ok"


def make_cli_status_output(status: dict[str, str | int | None], config: dict[str, Any]) -> dict[str, str]:
    label = str(config.get("label", DEFAULT_LABEL))
    five_percent = status["five_hour_percent"]
    weekly_percent = status["weekly_percent"]
    five_text = f"{five_percent}%" if five_percent is not None else "?%"
    weekly_text = f"{weekly_percent}%" if weekly_percent is not None else "?%"
    five_reset = status.get("five_hour_reset")
    weekly_reset = status.get("weekly_reset")
    tooltip = "\n".join(
        [
            "Codex CLI status",
            f"5-hour limit: {five_text} left" + (f" (resets {five_reset})" if five_reset else ""),
            f"Weekly limit: {weekly_text} left" + (f" (resets {weekly_reset})" if weekly_reset else ""),
            "Source: codex /status",
        ]
    )
    return {
        "text": f"{label} 5h {five_text} W {weekly_text}",
        "tooltip": tooltip,
        "class": status_class(
            int(five_percent) if five_percent is not None else None,
            int(weekly_percent) if weekly_percent is not None else None,
        ),
    }


def make_stale_cli_status_output(cached: dict[str, Any], message: str) -> dict[str, str]:
    tooltip = cached["tooltip"]
    updated_at = cached.get("saved_at")
    updated_line = None
    if isinstance(updated_at, (int, float)):
        updated_line = f"Last updated: {format_datetime(float(updated_at))}"
    if tooltip:
        suffix = ["Showing last known good status."]
        if updated_line:
            suffix.append(updated_line)
        suffix.append(f"Latest refresh failed: {message}")
        tooltip = f"{tooltip}\n\n" + "\n".join(suffix)
    else:
        suffix = ["Showing last known good status."]
        if updated_line:
            suffix.append(updated_line)
        suffix.append(f"Latest refresh failed: {message}")
        tooltip = "\n".join(suffix)
    return {
        "text": cached["text"],
        "tooltip": tooltip,
        "class": "warn",
    }


def read_codex_status_with_retries(config: dict[str, Any]) -> str:
    timeout = float(config.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"]))
    retries = max(1, int(config.get("status_retries", DEFAULT_CONFIG["status_retries"])))
    retry_delay = max(0.0, float(config.get("retry_delay_seconds", DEFAULT_CONFIG["retry_delay_seconds"])))
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            status_text = run_codex_status(timeout)
            parse_codex_status(status_text)
            return status_text
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            last_error = exc
            if attempt < retries and retry_delay > 0:
                time.sleep(retry_delay)

    if last_error is None:
        raise ValueError("Codex status output did not include limits")
    raise last_error


def live_output(config: dict[str, Any], api_key: str) -> dict[str, str]:
    start_time, end_time = today_range()
    timeout = float(config.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"]))

    common = [("start_time", start_time), ("end_time", end_time), ("bucket_width", "1d")]
    cost_rows = fetch_paginated("/costs", common, api_key, timeout)
    usage_note = None
    try:
        usage_rows = fetch_paginated("/usage/completions", common + [("group_by[]", "model")], api_key, timeout)
    except ApiError as exc:
        usage_rows = []
        usage_note = f"Token/model usage unavailable: {exc.message}"

    amount, cost_found = summarize_costs(cost_rows)
    usage = summarize_usage(usage_rows, list(config.get("codex_model_patterns", DEFAULT_CONFIG["codex_model_patterns"])))
    return make_output(amount, cost_found, usage, config, note=usage_note)


def disabled_output() -> dict[str, str]:
    return {"text": "", "tooltip": "", "class": "disabled"}


def main() -> int:
    config = load_config()
    if not config.get("enabled", True):
        print(json.dumps(disabled_output(), ensure_ascii=True))
        return 0

    label = str(config.get("label", DEFAULT_LABEL))
    try:
        status_text = read_codex_status_with_retries(config)
        output = make_cli_status_output(parse_codex_status(status_text), config)
        write_cache(output)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        retries = max(1, int(config.get("status_retries", DEFAULT_CONFIG["status_retries"])))
        output = {
            "text": f"{label} status ?",
            "tooltip": f"Could not read Codex CLI /status after {retries} attempt(s): {exc}",
            "class": "error",
        }
    print(json.dumps(output, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
