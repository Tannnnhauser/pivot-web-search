"""Tuple-sort routing and circuit breaker for provider selection.

Replaces the ad-hoc DDG demotion + usage_pct sorting with a generic system:
- Providers scored as (tier_rank, metric, priority) tuples
- Circuit breaker tracks per-provider health with sliding window
- Pacing pressure accounts for time-in-window for paid providers
"""

import time
from calendar import monthrange
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from . import quota as _quota
from .logging import log

_PACIFIC = ZoneInfo("America/Los_Angeles")

# Tier ranks for tuple-sort (lexicographic: lower = preferred)
TIER_RANK = {"free": 0, "daily": 1, "paid": 2}

# Circuit breaker parameters
CB_WINDOW_SIZE = 5
CB_MIN_SAMPLES = 3
CB_CONSECUTIVE_THRESHOLD = 3
CB_RATE_THRESHOLD = 0.6
CB_COOLDOWN_SECONDS = 120

# High-water demotion
HIGH_WATER_PCT = 85.0
HIGH_WATER_MIN_HOURS = 4

# News demotion tier rank for DDG
NEWS_DDG_TIER_RANK = 3


class BreakerState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class _BreakerEntry:
    __slots__ = ("state", "outcomes", "consecutive_failures", "opened_at")

    def __init__(self):
        self.state = BreakerState.CLOSED
        self.outcomes: deque = deque(maxlen=CB_WINDOW_SIZE)
        self.consecutive_failures = 0
        self.opened_at: float | None = None


class CircuitBreaker:
    """Per-provider circuit breaker with sliding-window failure tracking."""

    def __init__(self):
        self._breakers: dict[str, _BreakerEntry] = {}

    def _get_entry(self, name: str) -> _BreakerEntry:
        if name not in self._breakers:
            self._breakers[name] = _BreakerEntry()
        return self._breakers[name]

    def get_state(self, name: str) -> BreakerState:
        entry = self._get_entry(name)
        if entry.state == BreakerState.OPEN and entry.opened_at is not None:
            if time.time() - entry.opened_at >= CB_COOLDOWN_SECONDS:
                entry.state = BreakerState.HALF_OPEN
                log(f"{name} breaker HALF_OPEN (cooldown expired)")
        return entry.state

    def is_available(self, name: str) -> bool:
        state = self.get_state(name)
        return state in (BreakerState.CLOSED, BreakerState.HALF_OPEN)

    def record(self, name: str, success: bool) -> None:
        entry = self._get_entry(name)
        state = self.get_state(name)

        if state == BreakerState.HALF_OPEN:
            if success:
                entry.state = BreakerState.CLOSED
                entry.outcomes.clear()
                entry.consecutive_failures = 0
                entry.opened_at = None
                log(f"{name} breaker CLOSED (probe succeeded)")
            else:
                entry.state = BreakerState.OPEN
                entry.opened_at = time.time()
                log(f"{name} breaker OPEN (probe failed, cooldown restarted)")
            return

        if state == BreakerState.OPEN:
            return

        # CLOSED state
        entry.outcomes.append(success)
        if success:
            entry.consecutive_failures = 0
        else:
            entry.consecutive_failures += 1

        should_open = False
        if entry.consecutive_failures >= CB_CONSECUTIVE_THRESHOLD:
            should_open = True
        elif len(entry.outcomes) >= CB_MIN_SAMPLES:
            failure_count = sum(1 for o in entry.outcomes if not o)
            if failure_count / len(entry.outcomes) > CB_RATE_THRESHOLD:
                should_open = True

        if should_open:
            entry.state = BreakerState.OPEN
            entry.opened_at = time.time()
            log(f"{name} breaker OPEN after {entry.consecutive_failures} consecutive failures")

    def time_until_recovery(self, name: str) -> float:
        entry = self._get_entry(name)
        if entry.state != BreakerState.OPEN or entry.opened_at is None:
            return 0.0
        elapsed = time.time() - entry.opened_at
        return max(0.0, CB_COOLDOWN_SECONDS - elapsed)

    def force_half_open(self, name: str) -> None:
        entry = self._get_entry(name)
        entry.state = BreakerState.HALF_OPEN
        log(f"{name} breaker forced HALF_OPEN (all-open fallback)")

    def reset_all(self) -> None:
        self._breakers.clear()

    def get_status(self, name: str) -> dict:
        entry = self._get_entry(name)
        state = self.get_state(name)
        recent_ok = sum(1 for o in entry.outcomes if o)
        total = len(entry.outcomes)
        status = {"state": state.value, "recent_ok": recent_ok, "recent_total": total}
        if state == BreakerState.OPEN:
            status["cooldown_remaining"] = round(self.time_until_recovery(name), 1)
        return status


# ---------------------------------------------------------------------------
# Pacing pressure
# ---------------------------------------------------------------------------


def compute_pacing_pressure(provider_name: str) -> float:
    """Pacing pressure: actual_usage_pct / elapsed_time_pct.

    > 1.0 means consuming faster than budget allows.
    Returns 0.0 if no quota data available.
    """
    data = _quota.load_quota()
    entry = data.get(provider_name, {})
    limit = entry.get("limit")
    if not limit or limit <= 0:
        return 0.0

    used = entry.get("used", 0)
    usage_frac = used / limit
    period = entry.get("period", "monthly")

    if period == "rolling":
        elapsed = _rolling_elapsed(entry)
    else:
        elapsed = _monthly_elapsed()

    elapsed = max(elapsed, 0.01)
    return usage_frac / elapsed


def _monthly_elapsed() -> float:
    """Fraction of current month elapsed (UTC)."""
    now = datetime.now(timezone.utc)
    _, days_in_month = monthrange(now.year, now.month)
    return now.day / days_in_month


def _rolling_elapsed(entry: dict) -> float:
    """Fraction of rolling window elapsed, using reset_at timestamp."""
    reset_at_str = entry.get("reset_at")
    if not reset_at_str:
        return 1.0  # no reset_at → assume fully elapsed (neutral pressure)
    try:
        reset_dt = datetime.fromisoformat(reset_at_str)
    except (ValueError, TypeError):
        return 1.0

    now = datetime.now(timezone.utc)
    remaining = (reset_dt - now).total_seconds()
    if remaining <= 0:
        return 1.0

    last_synced_str = entry.get("last_synced")
    if last_synced_str:
        try:
            last_synced = datetime.fromisoformat(last_synced_str)
            total_window = (reset_dt - last_synced).total_seconds()
            if total_window > 0:
                return 1.0 - (remaining / total_window)
        except (ValueError, TypeError):
            pass

    # Fallback: assume 30-day window
    total_window = 30 * 24 * 3600
    return 1.0 - (remaining / total_window)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _hours_until_pt_midnight() -> float:
    """Hours remaining until next PT midnight."""
    now = datetime.now(_PACIFIC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now >= midnight:
        from datetime import timedelta
        midnight += timedelta(days=1)
    return (midnight - now).total_seconds() / 3600


def route_providers(providers, breaker: CircuitBreaker, *, is_news: bool = False) -> list:
    """Sort providers by (tier_rank, metric, priority). Returns active providers only."""
    scored = []
    has_healthy_non_ddg = False

    # Pre-scan for news demotion safety check
    if is_news:
        for p in providers:
            if p.provider_type != "ddg" and not _quota.is_exhausted(p.name) and breaker.is_available(p.name):
                has_healthy_non_ddg = True
                break

    for p in providers:
        if _quota.is_exhausted(p.name):
            continue
        if not breaker.is_available(p.name):
            continue

        tier = p.tier
        rank = TIER_RANK.get(tier, 2)

        # High-water demotion for daily tier
        if tier == "daily" and rank == 1:
            usage = _quota.get_usage_pct(p.name)
            if usage > HIGH_WATER_PCT and _hours_until_pt_midnight() > HIGH_WATER_MIN_HOURS:
                rank = 2
                hours_left = _hours_until_pt_midnight()
                log(f"{p.name} demoted to paid tier (usage {usage:.0f}%, {hours_left:.1f}h until reset)")

        # News demotion for DDG
        if is_news and p.provider_type == "ddg" and has_healthy_non_ddg:
            rank = NEWS_DDG_TIER_RANK

        # Compute metric per tier
        if tier == "free":
            metric = 0.0
        elif tier == "daily":
            metric = _quota.get_usage_pct(p.name) / 100.0
        else:
            metric = compute_pacing_pressure(p.name)

        scored.append((rank, metric, p.priority, p))

    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in scored]


def pick_recovery_candidate(providers, breaker: CircuitBreaker):
    """All-OPEN fallback: find provider closest to cooldown expiry, force HALF_OPEN."""
    best = None
    best_time = float("inf")

    for p in providers:
        if _quota.is_exhausted(p.name):
            continue
        remaining = breaker.time_until_recovery(p.name)
        if remaining < best_time:
            best = p
            best_time = remaining

    if best is not None:
        breaker.force_half_open(best.name)
    return best
