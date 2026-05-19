#!/usr/bin/env python3
"""Quota tracking for search providers.

Tracks API usage across sessions via a shared JSON file at
~/.cache/pivot-web-search/quota.json. Supports:
  - Tavily: proactive sync via GET /usage endpoint (monthly reset)
  - Brave: passive tracking via X-RateLimit-* response headers (rolling 30-day window)
  - Gemini: local counting with daily reset at PT midnight (500 RPD free tier)
  - DDG: not tracked (free, no quota)

Cross-platform file locking (filelock) ensures cross-process safety.
Providers auto-reset on first access after their period rolls over.
"""

import json
import os
import pathlib
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from filelock import FileLock

from .http_client import _open_with_fallback
from .logging import log

_PACIFIC = ZoneInfo("America/Los_Angeles")

_QUOTA_DIR = pathlib.Path.home() / ".cache" / "pivot-web-search"
_QUOTA_FILE = _QUOTA_DIR / "quota.json"
_QUOTA_LOCK = FileLock(str(_QUOTA_FILE) + ".lock")
_TAVILY_USAGE_URL = "https://api.tavily.com/usage"
_MIN_SYNC_INTERVAL = 60  # seconds between Tavily /usage calls

_quota_cache = None
_quota_cache_ts = 0
_QUOTA_CACHE_TTL = 5  # seconds


def _current_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _current_day_pt():
    """Current date in US/Pacific timezone (Gemini quota resets at PT midnight)."""
    return datetime.now(_PACIFIC).strftime("%Y-%m-%d")


def _read_file():
    """Read quota.json with file lock. Returns dict."""
    if not _QUOTA_FILE.exists():
        return {}
    try:
        with _QUOTA_LOCK:
            data = json.loads(_QUOTA_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_file(data):
    """Write quota.json with file lock."""
    _QUOTA_DIR.mkdir(parents=True, exist_ok=True)
    with _QUOTA_LOCK:
        _QUOTA_FILE.write_text(json.dumps(data, indent=2))


def _ensure_month(data, provider):
    """Auto-reset provider entry if month has changed."""
    month = _current_month()
    entry = data.get(provider, {})
    if entry.get("month") != month:
        data[provider] = {
            "month": month,
            "used": 0,
            "limit": entry.get("limit"),
            "source": entry.get("source", "local"),
            "last_synced": None,
        }
    return data


def _ensure_period(data, provider):
    """Auto-reset provider entry based on its period (monthly, daily, or rolling)."""
    entry = data.get(provider, {})
    period = entry.get("period", "monthly")
    if period == "daily":
        today = _current_day_pt()
        if entry.get("day") != today:
            data[provider] = {
                "period": "daily",
                "day": today,
                "month": _current_month(),
                "used": 0,
                "limit": entry.get("limit"),
                "source": entry.get("source", "config"),
                "last_synced": None,
            }
    elif period == "rolling":
        reset_at = entry.get("reset_at")
        if reset_at:
            try:
                reset_dt = datetime.fromisoformat(reset_at)
                if datetime.now(timezone.utc) >= reset_dt:
                    data[provider] = {
                        "period": "rolling",
                        "used": 0,
                        "limit": entry.get("limit"),
                        "source": entry.get("source", "header"),
                        "last_synced": None,
                        "reset_at": None,
                    }
            except (ValueError, TypeError):
                pass
    else:
        _ensure_month(data, provider)
    return data


def load_quota():
    """Load full quota state, auto-resetting stale periods. Cached for 5s."""
    global _quota_cache, _quota_cache_ts
    now = time.time()
    if _quota_cache is not None and (now - _quota_cache_ts) < _QUOTA_CACHE_TTL:
        return _quota_cache
    data = _read_file()
    changed = False
    for provider in list(data.keys()):
        entry = data[provider]
        period = entry.get("period", "monthly")
        needs_reset = False
        if period == "daily":
            needs_reset = entry.get("day") != _current_day_pt()
        elif period == "rolling":
            reset_at = entry.get("reset_at")
            if reset_at:
                try:
                    needs_reset = datetime.now(timezone.utc) >= datetime.fromisoformat(reset_at)
                except (ValueError, TypeError):
                    pass
        else:
            needs_reset = entry.get("month") != _current_month()
        if needs_reset:
            _ensure_period(data, provider)
            changed = True
    if changed:
        _write_file(data)
    _quota_cache = data
    _quota_cache_ts = now
    return data


def record_usage(provider_name):
    """Increment usage count for a provider. Called after each successful search."""
    global _quota_cache
    if provider_name in ("ddg", "DDG"):
        return
    _QUOTA_DIR.mkdir(parents=True, exist_ok=True)
    with _QUOTA_LOCK:
        try:
            raw = _QUOTA_FILE.read_text() if _QUOTA_FILE.exists() else ""
            data = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, OSError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        _ensure_period(data, provider_name)
        prev_used = data[provider_name].get("used", 0)
        new_used = prev_used + 1
        data[provider_name]["used"] = new_used
        _QUOTA_FILE.write_text(json.dumps(data, indent=2))
        limit = data[provider_name].get("limit")
    _quota_cache = None

    # Warn when crossing 80% threshold (one-shot per period).
    if limit and limit > 0:
        prev_pct = (prev_used / limit) * 100.0
        new_pct = (new_used / limit) * 100.0
        if prev_pct < 80.0 <= new_pct:
            log(f"WARN {provider_name} quota at {new_pct:.0f}% ({new_used}/{limit})")


def update_from_brave_headers(headers):
    """Parse Brave X-RateLimit-* headers and update quota state.

    Brave returns comma-separated values: per-second, per-month.
    We only care about the monthly values (second item).
    Also parses X-RateLimit-Reset for the rolling 30-day window reset time.
    """
    global _quota_cache
    remaining_raw = headers.get("X-RateLimit-Remaining", "")
    limit_raw = headers.get("X-RateLimit-Limit", "")

    try:
        remaining_parts = [int(x.strip()) for x in remaining_raw.split(",") if x.strip()]
        limit_parts = [int(x.strip()) for x in limit_raw.split(",") if x.strip()]
    except (ValueError, AttributeError):
        return

    if len(remaining_parts) < 2 or len(limit_parts) < 2:
        return

    monthly_remaining = remaining_parts[1]
    monthly_limit = limit_parts[1]
    monthly_used = monthly_limit - monthly_remaining

    reset_at = None
    reset_raw = headers.get("X-RateLimit-Reset", "")
    try:
        reset_parts = [int(x.strip()) for x in reset_raw.split(",") if x.strip()]
        if len(reset_parts) >= 2:
            reset_seconds = reset_parts[1]
            reset_at = (datetime.now(timezone.utc) + timedelta(seconds=reset_seconds)).isoformat()
    except (ValueError, AttributeError):
        pass

    data = _read_file()
    if "brave" not in data:
        data["brave"] = {}
    data["brave"].update({
        "period": "rolling",
        "used": max(monthly_used, 0),
        "limit": monthly_limit,
        "remaining": monthly_remaining,
        "source": "header",
        "last_synced": datetime.now(timezone.utc).isoformat(),
    })
    if reset_at:
        data["brave"]["reset_at"] = reset_at
    _write_file(data)
    _quota_cache = None


async def sync_tavily_usage(api_key=None):
    """Call Tavily /usage endpoint to sync real credit data.

    Rate-limited to 1 call per minute. Returns True on success.
    """
    if not api_key:
        from .validation import _load_env_key
        api_key = _load_env_key("TAVILY_API_KEY")
    if not api_key:
        return False

    data = _read_file()
    _ensure_month(data, "tavily")

    last_synced = data["tavily"].get("last_synced")
    if last_synced:
        try:
            last_ts = datetime.fromisoformat(last_synced).timestamp()
            if time.time() - last_ts < _MIN_SYNC_INTERVAL:
                return False
        except (ValueError, TypeError):
            pass

    try:
        resp = await _open_with_fallback(
            "GET", _TAVILY_USAGE_URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=10)
        obj = resp.json()

        key_usage = obj.get("key", {}).get("usage", 0)
        key_limit = obj.get("key", {}).get("limit")
        if key_limit is None:
            key_limit = obj.get("account", {}).get("plan_limit")

        data["tavily"].update({
            "used": key_usage,
            "limit": key_limit,
            "source": "api",
            "last_synced": datetime.now(timezone.utc).isoformat(),
        })
        _write_file(data)
        log(f"Tavily quota synced: {key_usage}/{key_limit} used")
        return True
    except Exception as e:
        log(f"Tavily usage sync failed: {e}")
        return False


def set_provider_limit(provider_name, limit, period="monthly"):
    """Set a manual quota limit for a provider (e.g., from userConfig)."""
    data = _read_file()
    _ensure_period(data, provider_name)
    data[provider_name]["limit"] = limit
    data[provider_name]["source"] = "config"
    if period == "daily":
        data[provider_name]["period"] = "daily"
        data[provider_name]["day"] = _current_day_pt()
    _write_file(data)


def get_usage_pct(provider_name):
    """Return usage percentage (0.0–100.0+). Returns 0.0 if no limit configured."""
    data = load_quota()
    entry = data.get(provider_name, {})
    limit = entry.get("limit")
    if not limit or limit <= 0:
        return 0.0
    used = entry.get("used", 0)
    return (used / limit) * 100.0


def is_exhausted(provider_name):
    """Return True if provider has hit or exceeded its quota limit."""
    data = load_quota()
    entry = data.get(provider_name, {})
    limit = entry.get("limit")
    if not limit or limit <= 0:
        return False
    return entry.get("used", 0) >= limit


def get_quota_summary():
    """Return quota info for all tracked providers. Used by WebSearchConfig status."""
    data = load_quota()
    summary = {}
    for name, entry in data.items():
        limit = entry.get("limit")
        used = entry.get("used", 0)
        summary[name] = {
            "used": used,
            "limit": limit,
            "usage_pct": round((used / limit) * 100.0, 1) if limit and limit > 0 else None,
            "source": entry.get("source", "local"),
        }
    return summary
