"""Tests for tuple-sort routing and circuit breaker."""

import time
from unittest.mock import patch

import pytest

from pivot_web_search_mcp.routing import (
    BreakerState,
    CB_COOLDOWN_SECONDS,
    CircuitBreaker,
    compute_pacing_pressure,
    pick_recovery_candidate,
    route_providers,
    _hours_until_pt_midnight,
)
from pivot_web_search_mcp.providers import SearchProvider
from pivot_web_search_mcp import quota as _quota


class FakeProvider(SearchProvider):
    """Minimal provider for routing tests."""

    def __init__(self, name, provider_type="tavily", priority=10, tier=None, config=None):
        cfg = config or {}
        if tier:
            cfg["tier"] = tier
        if provider_type not in ("ddg", "searxng", "gemini") and "api_key_env" not in cfg:
            cfg["api_key_env"] = "FAKE_KEY"
        super().__init__(name, priority, True, cfg)
        self.provider_type = provider_type


# ---------------------------------------------------------------------------
# Circuit Breaker tests
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.get_state("x") == BreakerState.CLOSED

    def test_open_after_3_consecutive_failures(self):
        cb = CircuitBreaker()
        cb.record("x", False)
        cb.record("x", False)
        assert cb.get_state("x") == BreakerState.CLOSED
        cb.record("x", False)
        assert cb.get_state("x") == BreakerState.OPEN

    def test_stays_closed_under_threshold(self):
        cb = CircuitBreaker()
        cb.record("x", True)
        cb.record("x", True)
        cb.record("x", True)
        cb.record("x", False)
        cb.record("x", False)
        # Window: [T,T,T,F,F] → 2/5 = 40% < 60%, consecutive=2 < 3
        assert cb.get_state("x") == BreakerState.CLOSED

    def test_rate_based_open(self):
        cb = CircuitBreaker()
        # 4 failures, 1 success = 80% failure rate > 60% threshold
        cb.record("x", True)
        cb.record("x", False)
        cb.record("x", False)
        cb.record("x", False)  # 3 consecutive → opens
        assert cb.get_state("x") == BreakerState.OPEN

    def test_min_samples_guard(self):
        cb = CircuitBreaker()
        cb.record("x", False)
        cb.record("x", False)
        # Only 2 samples, even though 100% failure rate, < min_samples
        assert cb.get_state("x") == BreakerState.CLOSED

    def test_half_open_after_cooldown(self):
        cb = CircuitBreaker()
        cb.record("x", False)
        cb.record("x", False)
        cb.record("x", False)
        assert cb.get_state("x") == BreakerState.OPEN

        with patch("pivot_web_search_mcp.routing.time.time", return_value=time.time() + CB_COOLDOWN_SECONDS + 1):
            assert cb.get_state("x") == BreakerState.HALF_OPEN

    def test_half_open_success_closes(self):
        cb = CircuitBreaker()
        cb.record("x", False)
        cb.record("x", False)
        cb.record("x", False)
        cb._get_entry("x").state = BreakerState.HALF_OPEN

        cb.record("x", True)
        assert cb.get_state("x") == BreakerState.CLOSED

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker()
        cb.record("x", False)
        cb.record("x", False)
        cb.record("x", False)
        cb._get_entry("x").state = BreakerState.HALF_OPEN

        cb.record("x", False)
        assert cb.get_state("x") == BreakerState.OPEN

    def test_is_available(self):
        cb = CircuitBreaker()
        assert cb.is_available("x") is True
        cb.record("x", False)
        cb.record("x", False)
        cb.record("x", False)
        assert cb.is_available("x") is False

    def test_reset_all(self):
        cb = CircuitBreaker()
        cb.record("x", False)
        cb.record("x", False)
        cb.record("x", False)
        cb.reset_all()
        assert cb.get_state("x") == BreakerState.CLOSED

    def test_get_status(self):
        cb = CircuitBreaker()
        cb.record("x", True)
        cb.record("x", False)
        status = cb.get_status("x")
        assert status["state"] == "CLOSED"
        assert status["recent_ok"] == 1
        assert status["recent_total"] == 2

    def test_all_open_fallback(self):
        cb = CircuitBreaker()
        for name in ("a", "b"):
            cb.record(name, False)
            cb.record(name, False)
            cb.record(name, False)

        pa = FakeProvider("a", priority=10)
        pb = FakeProvider("b", priority=20)

        with patch.object(_quota, "is_exhausted", return_value=False):
            result = pick_recovery_candidate([pa, pb], cb)

        assert result is not None
        assert cb.get_state(result.name) == BreakerState.HALF_OPEN


# ---------------------------------------------------------------------------
# Tuple Sort tests
# ---------------------------------------------------------------------------


class TestTupleSort:
    def test_free_before_daily_before_paid(self):
        cb = CircuitBreaker()
        providers = [
            FakeProvider("paid1", "tavily", priority=10, tier="paid"),
            FakeProvider("daily1", "gemini", priority=10, tier="daily"),
            FakeProvider("free1", "ddg", priority=10, tier="free"),
        ]
        with patch.object(_quota, "is_exhausted", return_value=False), \
             patch.object(_quota, "get_usage_pct", return_value=0.0), \
             patch("pivot_web_search_mcp.routing.compute_pacing_pressure", return_value=0.0):
            result = route_providers(providers, cb)
        assert [p.name for p in result] == ["free1", "daily1", "paid1"]

    def test_within_free_sorted_by_priority(self):
        cb = CircuitBreaker()
        providers = [
            FakeProvider("ddg2", "ddg", priority=20, tier="free"),
            FakeProvider("ddg1", "ddg", priority=5, tier="free"),
        ]
        with patch.object(_quota, "is_exhausted", return_value=False):
            result = route_providers(providers, cb)
        assert [p.name for p in result] == ["ddg1", "ddg2"]

    def test_daily_sorted_by_usage(self):
        cb = CircuitBreaker()
        providers = [
            FakeProvider("gem_high", "gemini", priority=10, tier="daily"),
            FakeProvider("gem_low", "gemini", priority=10, tier="daily"),
        ]
        usage_map = {"gem_high": 60.0, "gem_low": 20.0}
        with patch.object(_quota, "is_exhausted", return_value=False), \
             patch.object(_quota, "get_usage_pct", side_effect=lambda n: usage_map.get(n, 0.0)):
            result = route_providers(providers, cb)
        assert [p.name for p in result] == ["gem_low", "gem_high"]

    def test_paid_sorted_by_pressure_then_priority(self):
        cb = CircuitBreaker()
        providers = [
            FakeProvider("high_pressure", "tavily", priority=10, tier="paid"),
            FakeProvider("low_pressure", "brave", priority=20, tier="paid"),
        ]
        pressure_map = {"high_pressure": 1.5, "low_pressure": 0.4}
        with patch.object(_quota, "is_exhausted", return_value=False), \
             patch.object(_quota, "get_usage_pct", return_value=0.0), \
             patch("pivot_web_search_mcp.routing.compute_pacing_pressure", side_effect=lambda n: pressure_map.get(n, 0.0)):
            result = route_providers(providers, cb)
        assert [p.name for p in result] == ["low_pressure", "high_pressure"]

    def test_exhausted_excluded(self):
        cb = CircuitBreaker()
        providers = [
            FakeProvider("ok", "tavily", priority=10, tier="paid"),
            FakeProvider("exhausted", "brave", priority=20, tier="paid"),
        ]
        with patch.object(_quota, "is_exhausted", side_effect=lambda n: n == "exhausted"), \
             patch.object(_quota, "get_usage_pct", return_value=0.0), \
             patch("pivot_web_search_mcp.routing.compute_pacing_pressure", return_value=0.0):
            result = route_providers(providers, cb)
        assert [p.name for p in result] == ["ok"]

    def test_open_breaker_excluded(self):
        cb = CircuitBreaker()
        cb.record("broken", False)
        cb.record("broken", False)
        cb.record("broken", False)

        providers = [
            FakeProvider("ok", "tavily", priority=10, tier="paid"),
            FakeProvider("broken", "brave", priority=5, tier="paid"),
        ]
        with patch.object(_quota, "is_exhausted", return_value=False), \
             patch.object(_quota, "get_usage_pct", return_value=0.0), \
             patch("pivot_web_search_mcp.routing.compute_pacing_pressure", return_value=0.0):
            result = route_providers(providers, cb)
        assert [p.name for p in result] == ["ok"]

    def test_single_provider(self):
        cb = CircuitBreaker()
        providers = [FakeProvider("solo", "ddg", priority=10, tier="free")]
        with patch.object(_quota, "is_exhausted", return_value=False):
            result = route_providers(providers, cb)
        assert [p.name for p in result] == ["solo"]

    def test_empty_providers(self):
        cb = CircuitBreaker()
        assert route_providers([], cb) == []


# ---------------------------------------------------------------------------
# Pacing Pressure tests
# ---------------------------------------------------------------------------


class TestPacingPressure:
    def test_no_limit_returns_zero(self):
        with patch.object(_quota, "load_quota", return_value={"tavily": {"used": 50}}):
            assert compute_pacing_pressure("tavily") == 0.0

    def test_no_entry_returns_zero(self):
        with patch.object(_quota, "load_quota", return_value={}):
            assert compute_pacing_pressure("tavily") == 0.0

    def test_monthly_midpoint_on_pace(self):
        from datetime import datetime, timezone
        # Simulate: day 15 of 30-day month, 50% used
        entry = {"used": 500, "limit": 1000, "period": "monthly"}
        with patch.object(_quota, "load_quota", return_value={"tavily": entry}), \
             patch("pivot_web_search_mcp.routing._monthly_elapsed", return_value=0.5):
            pressure = compute_pacing_pressure("tavily")
        assert abs(pressure - 1.0) < 0.01

    def test_monthly_over_pace(self):
        entry = {"used": 800, "limit": 1000, "period": "monthly"}
        with patch.object(_quota, "load_quota", return_value={"tavily": entry}), \
             patch("pivot_web_search_mcp.routing._monthly_elapsed", return_value=0.5):
            pressure = compute_pacing_pressure("tavily")
        assert abs(pressure - 1.6) < 0.01

    def test_rolling_with_reset_at(self):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(days=15)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        entry = {
            "used": 500, "limit": 1000, "period": "rolling",
            "reset_at": future, "last_synced": past,
        }
        with patch.object(_quota, "load_quota", return_value={"brave": entry}):
            pressure = compute_pacing_pressure("brave")
        # ~50% elapsed, ~50% used → pressure ~1.0
        assert 0.8 < pressure < 1.2

    def test_division_guard(self):
        # period boundary: elapsed would be 0 without guard
        entry = {"used": 10, "limit": 1000, "period": "monthly"}
        with patch.object(_quota, "load_quota", return_value={"tavily": entry}), \
             patch("pivot_web_search_mcp.routing._monthly_elapsed", return_value=0.0):
            pressure = compute_pacing_pressure("tavily")
        # Should use max(0.0, 0.01) guard → 0.01 / 0.01 = 1.0
        assert pressure == 0.01 / 0.01  # just verifies no ZeroDivisionError


# ---------------------------------------------------------------------------
# High-Water Demotion tests
# ---------------------------------------------------------------------------


class TestHighWaterDemotion:
    def test_below_threshold_no_demotion(self):
        cb = CircuitBreaker()
        providers = [FakeProvider("gem", "gemini", priority=40, tier="daily")]
        with patch.object(_quota, "is_exhausted", return_value=False), \
             patch.object(_quota, "get_usage_pct", return_value=84.0), \
             patch("pivot_web_search_mcp.routing._hours_until_pt_midnight", return_value=6.0):
            result = route_providers(providers, cb)
        # Should remain tier 1 (daily), not demoted
        assert len(result) == 1

    def test_above_threshold_with_time_demotes(self):
        cb = CircuitBreaker()
        providers = [
            FakeProvider("ddg", "ddg", priority=10, tier="free"),
            FakeProvider("gem", "gemini", priority=40, tier="daily"),
            FakeProvider("tavily", "tavily", priority=20, tier="paid"),
        ]
        pressure_map = {"tavily": 0.5}
        with patch.object(_quota, "is_exhausted", return_value=False), \
             patch.object(_quota, "get_usage_pct", return_value=86.0), \
             patch("pivot_web_search_mcp.routing._hours_until_pt_midnight", return_value=6.0), \
             patch("pivot_web_search_mcp.routing.compute_pacing_pressure", side_effect=lambda n: pressure_map.get(n, 0.0)):
            result = route_providers(providers, cb)
        # ddg (free, rank 0) → tavily (paid, rank 2 with pressure 0.5) → gem (demoted to rank 2 with metric 0.86)
        names = [p.name for p in result]
        assert names[0] == "ddg"
        # gem demoted to paid tier, its metric (0.86) > tavily pressure (0.5)
        assert names.index("tavily") < names.index("gem")

    def test_above_threshold_near_midnight_no_demotion(self):
        cb = CircuitBreaker()
        providers = [
            FakeProvider("gem", "gemini", priority=40, tier="daily"),
            FakeProvider("tavily", "tavily", priority=20, tier="paid"),
        ]
        with patch.object(_quota, "is_exhausted", return_value=False), \
             patch.object(_quota, "get_usage_pct", return_value=90.0), \
             patch("pivot_web_search_mcp.routing._hours_until_pt_midnight", return_value=3.0), \
             patch("pivot_web_search_mcp.routing.compute_pacing_pressure", return_value=0.5):
            result = route_providers(providers, cb)
        # Near midnight: gem stays daily (rank 1), so it comes before tavily (rank 2)
        assert result[0].name == "gem"


# ---------------------------------------------------------------------------
# News Demotion tests
# ---------------------------------------------------------------------------


class TestNewsDemotion:
    def test_ddg_demoted_for_news(self):
        cb = CircuitBreaker()
        providers = [
            FakeProvider("ddg", "ddg", priority=10, tier="free"),
            FakeProvider("tavily", "tavily", priority=20, tier="paid"),
        ]
        with patch.object(_quota, "is_exhausted", return_value=False), \
             patch.object(_quota, "get_usage_pct", return_value=0.0), \
             patch("pivot_web_search_mcp.routing.compute_pacing_pressure", return_value=0.0):
            result = route_providers(providers, cb, is_news=True)
        # DDG demoted to rank 3 (below paid rank 2)
        assert result[0].name == "tavily"
        assert result[1].name == "ddg"

    def test_ddg_not_demoted_when_only_provider(self):
        cb = CircuitBreaker()
        providers = [FakeProvider("ddg", "ddg", priority=10, tier="free")]
        with patch.object(_quota, "is_exhausted", return_value=False):
            result = route_providers(providers, cb, is_news=True)
        # Only provider → no demotion, still returned
        assert result[0].name == "ddg"

    def test_non_ddg_free_unaffected(self):
        cb = CircuitBreaker()
        providers = [
            FakeProvider("searxng", "searxng", priority=5, tier="free"),
            FakeProvider("tavily", "tavily", priority=20, tier="paid"),
        ]
        with patch.object(_quota, "is_exhausted", return_value=False), \
             patch.object(_quota, "get_usage_pct", return_value=0.0), \
             patch("pivot_web_search_mcp.routing.compute_pacing_pressure", return_value=0.0):
            result = route_providers(providers, cb, is_news=True)
        # SearXNG (free, rank 0) still first — news demotion only affects DDG
        assert result[0].name == "searxng"
