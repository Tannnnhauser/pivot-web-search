"""Tests for the priority-group routing engine."""

import asyncio
import time
from unittest.mock import patch

from pivot_web_search_mcp.providers import SearchResult
from pivot_web_search_mcp.routing import (
    CB_CONSECUTIVE_THRESHOLD,
    CB_COOLDOWN_SECONDS,
    DEFAULT_TIMEOUT,
    HEDGE_DELAY_MS,
    SMART_DEFAULT_PRIORITY,
    BreakerState,
    CircuitBreaker,
    ScoredProvider,
    _CallCounter,
    _FailureInfo,
    build_priority_groups,
    execute_search,
    pick_recovery_candidate,
    select_providers,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeProvider:
    """Minimal provider for routing tests."""

    def __init__(self, name, provider_type="tavily", priority=10, enabled=True,
                 affinity="general", timeout=None, search_result=None, search_delay=0,
                 search_error=None):
        self.name = name
        self.provider_type = provider_type
        self.priority = priority
        self.enabled = enabled
        self.config = {"type": provider_type}
        self._effective_priority = priority
        self._rr_seed = 0
        self._affinity = affinity
        self._timeout = timeout
        self._search_result = search_result
        self._search_delay = search_delay
        self._search_error = search_error

    @property
    def affinity(self):
        return self._affinity

    @property
    def timeout_seconds(self):
        if self._timeout is not None:
            return self._timeout
        return DEFAULT_TIMEOUT.get(self.provider_type, 6)

    @property
    def effective_priority(self):
        return self._effective_priority

    async def search(self, query, max_results=5, **kwargs):
        if self._search_delay:
            await asyncio.sleep(self._search_delay)
        if self._search_error:
            raise self._search_error
        return self._search_result


def make_result(n=3, provider="test", answer=None):
    """Create a SearchResult with n fake results."""
    results = [
        {"url": f"https://site{i}.com", "title": f"Title {i}",
         "snippet": f"Snippet about query topic {i}"}
        for i in range(n)
    ]
    return SearchResult(results=results, provider=provider, answer=answer)


# ---------------------------------------------------------------------------
# Circuit Breaker Tests
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_initial_state_is_closed(self):
        b = CircuitBreaker()
        assert b.get_state("p1") == BreakerState.CLOSED
        assert b.is_available("p1")

    def test_opens_after_consecutive_failures(self):
        b = CircuitBreaker()
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("p1")
        assert b.get_state("p1") == BreakerState.OPEN
        assert not b.is_available("p1")

    def test_success_resets_consecutive_count(self):
        b = CircuitBreaker()
        b.record_failure("p1")
        b.record_failure("p1")
        b.record_success("p1")
        b.record_failure("p1")
        b.record_failure("p1")
        assert b.get_state("p1") == BreakerState.CLOSED

    def test_cooldown_transitions_to_half_open(self):
        b = CircuitBreaker()
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("p1")
        assert b.get_state("p1") == BreakerState.OPEN

        with patch("pivot_web_search_mcp.routing.time.time", return_value=time.time() + CB_COOLDOWN_SECONDS + 1):
            assert b.get_state("p1") == BreakerState.HALF_OPEN
            assert b.is_available("p1")

    def test_half_open_success_closes(self):
        b = CircuitBreaker()
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("p1")

        entry = b._get_entry("p1")
        entry.state = BreakerState.HALF_OPEN

        b.record_success("p1")
        assert b.get_state("p1") == BreakerState.CLOSED

    def test_half_open_failure_reopens(self):
        b = CircuitBreaker()
        entry = b._get_entry("p1")
        entry.state = BreakerState.HALF_OPEN

        b.record_failure("p1")
        assert b.get_state("p1") == BreakerState.OPEN

    def test_open_immediately(self):
        b = CircuitBreaker()
        b.open_immediately("p1")
        assert b.get_state("p1") == BreakerState.OPEN

    def test_open_immediately_custom_cooldown(self):
        b = CircuitBreaker()
        b.open_immediately("p1", cooldown_s=30)
        entry = b._get_entry("p1")
        assert entry.cooldown_override == 30

    def test_cooldown_is_60_seconds(self):
        assert CB_COOLDOWN_SECONDS == 60

    def test_time_until_recovery(self):
        b = CircuitBreaker()
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("p1")
        remaining = b.time_until_recovery("p1")
        assert 59 < remaining <= 60

    def test_force_half_open(self):
        b = CircuitBreaker()
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("p1")
        b.force_half_open("p1")
        assert b.get_state("p1") == BreakerState.HALF_OPEN

    def test_get_status(self):
        b = CircuitBreaker()
        status = b.get_status("p1")
        assert status["state"] == "CLOSED"
        assert status["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# Select Providers Tests
# ---------------------------------------------------------------------------


class TestSelectProviders:
    def test_filters_disabled(self):
        providers = [
            FakeProvider("a", enabled=True),
            FakeProvider("b", enabled=False),
        ]
        b = CircuitBreaker()
        result = select_providers(providers, b)
        assert len(result) == 1
        assert result[0].provider.name == "a"

    def test_filters_deep_affinity_in_general_mode(self):
        providers = [
            FakeProvider("general", affinity="general"),
            FakeProvider("deep", affinity="deep"),
        ]
        b = CircuitBreaker()
        result = select_providers(providers, b, affinity="general")
        assert len(result) == 1
        assert result[0].provider.name == "general"

    def test_includes_deep_in_deep_mode(self):
        providers = [
            FakeProvider("general", affinity="general"),
            FakeProvider("deep", affinity="deep"),
        ]
        b = CircuitBreaker()
        result = select_providers(providers, b, affinity="deep")
        assert len(result) == 2

    def test_filters_exhausted(self):
        providers = [FakeProvider("a"), FakeProvider("b")]
        b = CircuitBreaker()
        with patch("pivot_web_search_mcp.routing._quota.is_exhausted", side_effect=lambda n: n == "b"):
            result = select_providers(providers, b)
        assert len(result) == 1
        assert result[0].provider.name == "a"

    def test_filters_circuit_broken(self):
        providers = [FakeProvider("a"), FakeProvider("b")]
        b = CircuitBreaker()
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("b")
        result = select_providers(providers, b)
        assert len(result) == 1
        assert result[0].provider.name == "a"

    def test_sorted_by_priority(self):
        providers = [
            FakeProvider("low", priority=90),
            FakeProvider("high", priority=10),
            FakeProvider("mid", priority=40),
        ]
        b = CircuitBreaker()
        result = select_providers(providers, b)
        names = [c.provider.name for c in result]
        assert names == ["high", "mid", "low"]


# ---------------------------------------------------------------------------
# Priority Grouping Tests
# ---------------------------------------------------------------------------


class TestPriorityGrouping:
    def test_same_priority_grouped(self):
        candidates = [
            ScoredProvider(FakeProvider("a"), 10, 0, 0),
            ScoredProvider(FakeProvider("b"), 10, 0, 1),
            ScoredProvider(FakeProvider("c"), 20, 0, 0),
        ]
        groups = build_priority_groups(candidates)
        assert len(groups) == 2
        assert len(groups[0]) == 2
        assert len(groups[1]) == 1

    def test_empty_input(self):
        groups = build_priority_groups([])
        assert groups == []

    def test_single_provider(self):
        candidates = [ScoredProvider(FakeProvider("a"), 10, 0, 0)]
        groups = build_priority_groups(candidates)
        assert len(groups) == 1
        assert len(groups[0]) == 1


# ---------------------------------------------------------------------------
# Smart Defaults Tests
# ---------------------------------------------------------------------------


class TestSmartDefaults:
    def test_default_priorities(self):
        assert SMART_DEFAULT_PRIORITY["tavily"] == 10
        assert SMART_DEFAULT_PRIORITY["brave"] == 10
        assert SMART_DEFAULT_PRIORITY["searxng"] == 30
        assert SMART_DEFAULT_PRIORITY["json_api"] == 30
        assert SMART_DEFAULT_PRIORITY["gemini"] == 40
        assert SMART_DEFAULT_PRIORITY["llm_search"] == 60
        assert SMART_DEFAULT_PRIORITY["ddg"] == 90

    def test_default_timeouts(self):
        assert DEFAULT_TIMEOUT["brave"] == 4
        assert DEFAULT_TIMEOUT["tavily"] == 4
        assert DEFAULT_TIMEOUT["ddg"] == 6
        assert DEFAULT_TIMEOUT["gemini"] == 20
        assert DEFAULT_TIMEOUT["llm_search"] == 15

    def test_hedge_delay_is_200ms(self):
        assert HEDGE_DELAY_MS == 200


# ---------------------------------------------------------------------------
# Execute Search Tests
# ---------------------------------------------------------------------------


class TestExecuteSearch:
    async def test_returns_first_quality_result(self):
        providers = [
            FakeProvider("tavily", priority=10, search_result=make_result(3, "tavily")),
            FakeProvider("ddg", priority=90, search_result=make_result(2, "ddg")),
        ]
        b = CircuitBreaker()
        result = await execute_search("python tutorial", 5, providers, b)
        assert result is not None
        assert not isinstance(result, _FailureInfo)
        assert result.provider == "tavily"

    async def test_falls_through_on_empty_result(self):
        providers = [
            FakeProvider("bad", priority=10, search_result=None),
            FakeProvider("good", priority=20, search_result=make_result(3, "good")),
        ]
        b = CircuitBreaker()
        result = await execute_search("test query", 5, providers, b)
        assert not isinstance(result, _FailureInfo)
        assert result.provider == "good"

    async def test_returns_failure_info_when_all_fail(self):
        providers = [
            FakeProvider("a", priority=10, search_result=None),
            FakeProvider("b", priority=20, search_result=None),
        ]
        b = CircuitBreaker()
        result = await execute_search("test", 5, providers, b)
        assert isinstance(result, _FailureInfo)

    async def test_timeout_triggers_failover(self):
        providers = [
            FakeProvider("slow", priority=10, search_delay=10, timeout=0.1,
                         search_result=make_result(3, "slow")),
            FakeProvider("fast", priority=20, search_result=make_result(3, "fast")),
        ]
        b = CircuitBreaker()
        result = await execute_search("test", 5, providers, b)
        assert not isinstance(result, _FailureInfo)
        assert result.provider == "fast"

    async def test_exception_triggers_failover(self):
        providers = [
            FakeProvider("err", priority=10, search_error=RuntimeError("boom")),
            FakeProvider("ok", priority=20, search_result=make_result(3, "ok")),
        ]
        b = CircuitBreaker()
        result = await execute_search("test", 5, providers, b)
        assert not isinstance(result, _FailureInfo)
        assert result.provider == "ok"

    async def test_returns_best_partial_when_no_accept(self):
        partial_result = SearchResult(
            results=[{"url": "https://a.com", "title": "Irrelevant", "snippet": "nothing"}],
            provider="partial",
        )
        providers = [
            FakeProvider("partial", priority=10, search_result=partial_result),
            FakeProvider("empty", priority=20, search_result=None),
        ]
        b = CircuitBreaker()
        result = await execute_search("quantum physics", 5, providers, b)
        assert result is not None
        assert not isinstance(result, _FailureInfo)
        assert result.provider == "partial"

    async def test_hedged_same_priority_first_wins(self):
        providers = [
            FakeProvider("fast", priority=10, search_delay=0,
                         search_result=make_result(3, "fast")),
            FakeProvider("slow", priority=10, search_delay=2,
                         search_result=make_result(3, "slow")),
        ]
        b = CircuitBreaker()
        result = await execute_search("python tutorial", 5, providers, b)
        assert result.provider == "fast"

    async def test_affinity_deep_filters_general(self):
        providers = [
            FakeProvider("general", priority=10, affinity="general",
                         search_result=make_result(3, "general")),
            FakeProvider("deep", priority=10, affinity="deep",
                         search_result=make_result(3, "deep")),
        ]
        b = CircuitBreaker()
        result = await execute_search("test", 5, providers, b, affinity="general")
        assert result.provider == "general"

    async def test_recovery_candidate_on_all_open(self):
        providers = [FakeProvider("solo", priority=10, search_result=make_result(3, "solo"))]
        b = CircuitBreaker()
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("solo")
        result = await execute_search("test", 5, providers, b)
        assert not isinstance(result, _FailureInfo)
        assert result.provider == "solo"


# ---------------------------------------------------------------------------
# Pick Recovery Candidate Tests
# ---------------------------------------------------------------------------


class TestPickRecoveryCandidate:
    def test_picks_closest_to_expiry(self):
        b = CircuitBreaker()
        p1 = FakeProvider("p1")
        p2 = FakeProvider("p2")

        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("p1")
        b._get_entry("p1").opened_at = time.time() - 50

        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("p2")
        b._get_entry("p2").opened_at = time.time() - 10

        result = pick_recovery_candidate([p1, p2], b)
        assert result is not None
        assert result.name == "p1"
        assert b.get_state("p1") == BreakerState.HALF_OPEN

    def test_skips_exhausted(self):
        b = CircuitBreaker()
        p1 = FakeProvider("p1")
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("p1")

        with patch("pivot_web_search_mcp.routing._quota.is_exhausted", return_value=True):
            result = pick_recovery_candidate([p1], b)
        assert result is None

    def test_returns_none_when_no_providers(self):
        b = CircuitBreaker()
        result = pick_recovery_candidate([], b)
        assert result is None


# ---------------------------------------------------------------------------
# Call Counter Tests
# ---------------------------------------------------------------------------


class TestCallCounter:
    def test_initial_value_is_zero(self):
        c = _CallCounter()
        assert c.value("new") == 0

    def test_increment(self):
        c = _CallCounter()
        c.increment("p1")
        c.increment("p1")
        assert c.value("p1") == 2

    def test_reset(self):
        c = _CallCounter()
        c.increment("p1")
        c.reset()
        assert c.value("p1") == 0
