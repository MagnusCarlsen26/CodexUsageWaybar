#!/usr/bin/env python3
"""Waybar custom module for Cursor Agent usage from the dashboard API."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request

DEFAULT_LABEL = "◉"
AUTO_TOOLTIP_LABEL = "Auto + Composer"
API_TOOLTIP_LABEL = "API"
DEFAULT_CONFIG = {
    "label": DEFAULT_LABEL,
    "auto_label": "◈",
    "api_label": "</>",
    "api_base": "https://api2.cursor.sh",
    "auth_path": "~/.config/cursor/auth.json",
    "timeout_seconds": 15,
    "status_retries": 2,
    "retry_delay_seconds": 3,
    "cache_seconds": 300,
    "enabled": True,
}
CACHE_PATH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agent-usage-waybar" / "cache.json"
CONFIG_PATH = Path(
    os.environ.get("AGENT_USAGE_WAYBAR_CONFIG", Path.home() / ".config" / "agent-usage-waybar" / "config.json")
)
USAGE_PATH = "/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
ENTERPRISE_USAGE_PATH = "/auth/usage"


class ApiError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


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


def get_access_token(config: dict[str, Any]) -> str | None:
    env_token = os.environ.get("CURSOR_API_KEY")
    if env_token:
        return env_token

    auth_path = Path(str(config.get("auth_path", DEFAULT_CONFIG["auth_path"]))).expanduser()
    try:
        with auth_path.open("r", encoding="utf-8") as handle:
            auth_data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(auth_data, dict):
        return None
    token = auth_data.get("accessToken")
    return token if isinstance(token, str) and token else None


def api_base(config: dict[str, Any]) -> str:
    return str(config.get("api_base", DEFAULT_CONFIG["api_base"])).rstrip("/")


def request_json(
    method: str,
    url: str,
    token: str,
    timeout: float,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "agent-usage-waybar/1.0",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Connect-Protocol-Version"] = "1"

    req = request.Request(url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ApiError("auth", f"Cursor API returned HTTP {exc.code}. Run `agent login` to refresh auth.") from exc
        if exc.code == 429:
            raise ApiError("429", "Cursor API rate limit reached") from exc
        raise ApiError("api", f"Cursor API returned HTTP {exc.code}") from exc
    except TimeoutError as exc:
        raise ApiError("net", "Network timeout while calling Cursor API") from exc
    except error.URLError as exc:
        raise ApiError("net", f"Network error while calling Cursor API: {exc.reason}") from exc
    except OSError as exc:
        raise ApiError("net", f"Network error while calling Cursor API: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError("unknown", "Cursor API returned malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise ApiError("unknown", "Cursor API returned an unexpected response shape")
    return parsed


def fetch_current_period_usage(token: str, config: dict[str, Any]) -> dict[str, Any]:
    timeout = float(config.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"]))
    retries = max(1, int(config.get("status_retries", DEFAULT_CONFIG["status_retries"])))
    retry_delay = max(0.0, float(config.get("retry_delay_seconds", DEFAULT_CONFIG["retry_delay_seconds"])))
    url = f"{api_base(config)}{USAGE_PATH}"
    last_error: ApiError | None = None

    for attempt in range(1, retries + 1):
        try:
            return request_json("POST", url, token, timeout, body={})
        except ApiError as exc:
            last_error = exc
            if attempt < retries and retry_delay > 0:
                time.sleep(retry_delay)

    if last_error is None:
        raise ApiError("unknown", "Could not fetch Cursor usage")
    raise last_error


def fetch_enterprise_usage(token: str, config: dict[str, Any]) -> dict[str, Any]:
    timeout = float(config.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"]))
    url = f"{api_base(config)}{ENTERPRISE_USAGE_PATH}"
    return request_json("GET", url, token, timeout)


def parse_ms_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            ms = float(value)
        elif isinstance(value, (int, float)):
            ms = float(value)
        else:
            return None
    except (TypeError, ValueError):
        return None
    return ms / 1000.0


def format_datetime(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%a, %d %b %Y")


def format_cents(cents: Any) -> str | None:
    if not isinstance(cents, (int, float)) or not math.isfinite(float(cents)):
        return None
    return f"${float(cents) / 100:.2f}"


def percent_remaining(used: Any) -> int | None:
    if used is None:
        return None
    try:
        value = float(used)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return max(0, round(100 - value))


def plan_usage_has_percent_buckets(plan_usage: dict[str, Any]) -> bool:
    for key in ("autoPercentUsed", "apiPercentUsed", "totalPercentUsed"):
        if percent_remaining(plan_usage.get(key)) is not None:
            return True
    return False


def parse_plan_usage(response: dict[str, Any]) -> dict[str, Any]:
    plan_usage = response.get("planUsage")
    if not isinstance(plan_usage, dict):
        plan_usage = {}

    billing_start = parse_ms_timestamp(response.get("billingCycleStart"))
    billing_end = parse_ms_timestamp(response.get("billingCycleEnd"))

    included_spend = plan_usage.get("includedSpend")
    limit = plan_usage.get("limit")
    bonus_spend = plan_usage.get("bonusSpend")

    return {
        "auto_left": percent_remaining(plan_usage.get("autoPercentUsed")),
        "api_left": percent_remaining(plan_usage.get("apiPercentUsed")),
        "total_left": percent_remaining(plan_usage.get("totalPercentUsed")),
        "billing_start": billing_start,
        "billing_end": billing_end,
        "included_spend": included_spend,
        "limit": limit,
        "bonus_spend": bonus_spend if isinstance(bonus_spend, (int, float)) and bonus_spend > 0 else None,
        "display_message": response.get("displayMessage") if isinstance(response.get("displayMessage"), str) else None,
        "has_percent_buckets": plan_usage_has_percent_buckets(plan_usage),
    }


def parse_enterprise_usage(response: dict[str, Any]) -> dict[str, Any] | None:
    best_name: str | None = None
    best_limit = -1
    best_remaining: int | None = None
    best_used: int | None = None

    for name, bucket in response.items():
        if name == "startOfMonth" or not isinstance(bucket, dict):
            continue
        max_requests = bucket.get("maxRequestUsage")
        if not isinstance(max_requests, (int, float)) or max_requests <= 0:
            continue
        limit = int(max_requests)
        used = int(bucket.get("numRequests") or 0)
        if limit > best_limit:
            best_limit = limit
            best_name = name
            best_remaining = max(limit - used, 0)
            best_used = used

    if best_name is None or best_remaining is None:
        return None

    cycle_start: float | None = None
    start_of_month = response.get("startOfMonth")
    if isinstance(start_of_month, str):
        try:
            cycle_start = dt.datetime.fromisoformat(start_of_month.replace("Z", "+00:00")).timestamp()
        except ValueError:
            cycle_start = None

    return {
        "model": best_name,
        "remaining_requests": best_remaining,
        "used_requests": best_used,
        "limit_requests": best_limit,
        "cycle_start": cycle_start,
    }


def status_class(auto_left: int | None, api_left: int | None, total_left: int | None = None) -> str:
    percentages = [percent for percent in (auto_left, api_left, total_left) if percent is not None]
    if not percentages:
        return "error"
    lowest = min(percentages)
    if lowest <= 10:
        return "critical"
    if lowest <= 25:
        return "warn"
    return "ok"


def enterprise_status_class(remaining: int, limit: int) -> str:
    if limit <= 0:
        return "error"
    percent_left = remaining / limit * 100
    return status_class(int(round(percent_left)), None)


def make_agent_status_output(parsed: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    label = str(config.get("label", DEFAULT_LABEL))
    auto_label = str(config.get("auto_label", DEFAULT_CONFIG["auto_label"]))
    api_label = str(config.get("api_label", DEFAULT_CONFIG["api_label"]))
    auto_left = parsed.get("auto_left")
    api_left = parsed.get("api_left")
    total_left = parsed.get("total_left")

    parts = [label]
    if auto_left is not None:
        parts.extend([auto_label, f"{auto_left}%"])
    if api_left is not None:
        parts.extend([api_label, f"{api_left}%"])
    if auto_left is None and api_left is None and total_left is not None:
        parts.extend(["Total", f"{total_left}%"])

    tooltip_lines = ["Cursor Agent usage", ""]
    if parsed.get("billing_start") is not None and parsed.get("billing_end") is not None:
        tooltip_lines.append(
            f"Billing period: {format_datetime(float(parsed['billing_start']))} – "
            f"{format_datetime(float(parsed['billing_end']))}"
        )
        tooltip_lines.append("")

    if auto_left is not None:
        tooltip_lines.append(f"{AUTO_TOOLTIP_LABEL}: {auto_left}% remaining")
    if api_left is not None:
        tooltip_lines.append(f"{API_TOOLTIP_LABEL}: {api_left}% remaining")
    if total_left is not None:
        tooltip_lines.append(f"Total: {total_left}% remaining")

    included = format_cents(parsed.get("included_spend"))
    limit = format_cents(parsed.get("limit"))
    if included and limit:
        tooltip_lines.append(f"Included spend: {included} / {limit}")

    bonus = format_cents(parsed.get("bonus_spend"))
    if bonus:
        tooltip_lines.append(f"Bonus credits used: {bonus}")

    display_message = parsed.get("display_message")
    if display_message:
        tooltip_lines.append("")
        tooltip_lines.append(display_message)

    tooltip_lines.append("")
    tooltip_lines.append("Source: Cursor dashboard API")

    return {
        "text": " ".join(parts),
        "tooltip": "\n".join(tooltip_lines),
        "class": status_class(
            int(auto_left) if auto_left is not None else None,
            int(api_left) if api_left is not None else None,
            int(total_left) if total_left is not None else None,
        ),
    }


def make_enterprise_status_output(parsed: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    label = str(config.get("label", DEFAULT_LABEL))
    remaining = int(parsed["remaining_requests"])
    limit = int(parsed["limit_requests"])
    model = str(parsed.get("model", "requests"))

    tooltip_lines = [
        "Cursor Agent usage (request-based)",
        "",
        f"Model bucket: {model}",
        f"Requests remaining: {remaining} / {limit}",
    ]
    if parsed.get("used_requests") is not None:
        tooltip_lines.append(f"Requests used: {parsed['used_requests']}")
    if parsed.get("cycle_start") is not None:
        tooltip_lines.append(f"Cycle start: {format_datetime(float(parsed['cycle_start']))}")
    tooltip_lines.append("")
    tooltip_lines.append("Source: Cursor /auth/usage")

    return {
        "text": f"{label} {remaining} req",
        "tooltip": "\n".join(tooltip_lines),
        "class": enterprise_status_class(remaining, limit),
    }


def error_output(kind: str, message: str, config: dict[str, Any]) -> dict[str, str]:
    label = str(config.get("label", DEFAULT_LABEL))
    suffix = {
        "missing_token": "status ?",
        "auth": "status ?",
        "429": "429",
        "net": "net",
    }.get(kind, "status ?")
    return {
        "text": f"{label} {suffix}",
        "tooltip": message,
        "class": "error",
    }


def write_cache(output: dict[str, str]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_PATH.open("w", encoding="utf-8") as handle:
            json.dump({"saved_at": time.time(), "output": output}, handle)
    except OSError:
        pass


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


def live_output(token: str, config: dict[str, Any]) -> dict[str, str]:
    response = fetch_current_period_usage(token, config)
    parsed = parse_plan_usage(response)
    if parsed.get("has_percent_buckets"):
        return make_agent_status_output(parsed, config)

    enterprise = parse_enterprise_usage(fetch_enterprise_usage(token, config))
    if enterprise is not None:
        return make_enterprise_status_output(enterprise, config)

    raise ApiError("unknown", "Cursor usage response did not include plan or request quotas")


def disabled_output() -> dict[str, str]:
    return {"text": "", "tooltip": "", "class": "disabled"}


def main() -> int:
    config = load_config()
    if not config.get("enabled", True):
        print(json.dumps(disabled_output(), ensure_ascii=True))
        return 0

    token = get_access_token(config)
    if not token:
        output = error_output(
            "missing_token",
            "Not authenticated. Run `agent login` or set CURSOR_API_KEY.",
            config,
        )
        print(json.dumps(output, ensure_ascii=True))
        return 0

    try:
        output = live_output(token, config)
        write_cache(output)
    except ApiError as exc:
        retries = max(1, int(config.get("status_retries", DEFAULT_CONFIG["status_retries"])))
        if exc.kind == "auth":
            output = error_output("auth", exc.message, config)
        else:
            output = error_output(
                exc.kind,
                f"Could not fetch Cursor usage after {retries} attempt(s): {exc.message}",
                config,
            )
    except OSError as exc:
        retries = max(1, int(config.get("status_retries", DEFAULT_CONFIG["status_retries"])))
        output = {
            "text": f"{config.get('label', DEFAULT_LABEL)} status ?",
            "tooltip": f"Could not fetch Cursor usage after {retries} attempt(s): {exc}",
            "class": "error",
        }

    print(json.dumps(output, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
