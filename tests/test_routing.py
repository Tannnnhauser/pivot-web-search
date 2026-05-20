"""Tests for the priority-group routing engine."""

import asyncio
import time
from unittest.mock import patch

import httpx

from pivot_web_search_mcp import quota
from pivot_web_search_mcp.defaults import DEFAULT_TIMEOUT, SMART_DEFAULT_PRIORITY
from pivot_web_search_mcp.providers import SearchResult
from pivot_web_search_mcp.routing import (
    CB_CONSECUTIVE_THRESHOLD,
    CB_COOLDOWN_SECONDS,
    HEDGE_DELAY_MS,
    LLM_BUDGET_EXTENSION_S,
    TOTAL_BUDGET_S,
    AttemptResult,
    BreakerState,
    CallCounter,
    CircuitBreaker,
    FailureInfo,
    ScoredProvider,
    build_priority_groups,
    effective_budget,
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
        assert SMART_DEFAULT_PRIORITY["llm_search"] == 10
        assert SMART_DEFAULT_PRIORITY["tavily"] == 20
        assert SMART_DEFAULT_PRIORITY["brave"] == 20
        assert SMART_DEFAULT_PRIORITY["searxng"] == 30
        assert SMART_DEFAULT_PRIORITY["json_api"] == 30
        assert SMART_DEFAULT_PRIORITY["gemini"] == 20
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
        assert not isinstance(result, FailureInfo)
        assert result.provider == "tavily"

    async def test_falls_through_on_empty_result(self):
        providers = [
            FakeProvider("bad", priority=10, search_result=None),
            FakeProvider("good", priority=20, search_result=make_result(3, "good")),
        ]
        b = CircuitBreaker()
        result = await execute_search("test query", 5, providers, b)
        assert not isinstance(result, FailureInfo)
        assert result.provider == "good"

    async def test_returns_failure_info_when_all_fail(self):
        providers = [
            FakeProvider("a", priority=10, search_result=None),
            FakeProvider("b", priority=20, search_result=None),
        ]
        b = CircuitBreaker()
        result = await execute_search("test", 5, providers, b)
        assert isinstance(result, FailureInfo)

    async def test_timeout_triggers_failover(self):
        providers = [
            FakeProvider("slow", priority=10, search_delay=10, timeout=0.1,
                         search_result=make_result(3, "slow")),
            FakeProvider("fast", priority=20, search_result=make_result(3, "fast")),
        ]
        b = CircuitBreaker()
        result = await execute_search("test", 5, providers, b)
        assert not isinstance(result, FailureInfo)
        assert result.provider == "fast"

    async def test_exception_triggers_failover(self):
        providers = [
            FakeProvider("err", priority=10, search_error=RuntimeError("boom")),
            FakeProvider("ok", priority=20, search_result=make_result(3, "ok")),
        ]
        b = CircuitBreaker()
        result = await execute_search("test", 5, providers, b)
        assert not isinstance(result, FailureInfo)
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
        assert not isinstance(result, FailureInfo)
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

    async def test_hedged_cancels_losing_tasks(self):
        """Once the winning hedged provider returns ACCEPT, the losers must be cancelled."""

        class TrackingProvider(FakeProvider):
            def __init__(self, name, **kwargs):
                super().__init__(name, **kwargs)
                self.was_cancelled = False
                self.completed = False

            async def search(self, query, max_results=5, **kwargs):
                try:
                    if self._search_delay:
                        await asyncio.sleep(self._search_delay)
                    self.completed = True
                    if self._search_error:
                        raise self._search_error
                    return self._search_result
                except asyncio.CancelledError:
                    self.was_cancelled = True
                    raise

        fast = TrackingProvider("fast", priority=10, search_delay=0,
                                search_result=make_result(3, "fast"))
        slow = TrackingProvider("slow", priority=10, search_delay=5,
                                search_result=make_result(3, "slow"))
        b = CircuitBreaker()
        result = await execute_search("python tutorial", 5, [fast, slow], b)
        assert result.provider == "fast"
        assert fast.completed
        assert not slow.completed
        assert slow.was_cancelled

    async def test_recovery_candidate_on_all_open(self):
        providers = [FakeProvider("solo", priority=10, search_result=make_result(3, "solo"))]
        b = CircuitBreaker()
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("solo")
        result = await execute_search("test", 5, providers, b)
        assert isinstance(result, FailureInfo)
        assert len(result.failures) == 1
        f = result.failures[0]
        assert f["provider"] == "solo"
        assert f["state"] == "circuit_open"
        assert f["cooldown_remaining_seconds"] > 0

    async def test_tcp_failure_aborts_after_two_groups(self):
        """Two consecutive TCP-level failures should short-circuit (no network)."""
        import httpx
        providers = [
            FakeProvider("a", provider_type="tavily", priority=10,
                         search_error=httpx.ConnectError("DNS down")),
            FakeProvider("b", provider_type="brave", priority=20,
                         search_error=httpx.ConnectError("DNS down")),
            FakeProvider("c", provider_type="ddg", priority=30,
                         search_result=make_result(3, "c")),
        ]
        b = CircuitBreaker()
        result = await execute_search("test", 5, providers, b)
        assert isinstance(result, FailureInfo)
        # Provider c is never tried — short-circuited after 2 tcp_failures
        assert all(f["provider"] != "c" for f in result.failures)
        assert sum(1 for f in result.failures if f["error"] == "tcp_failure") == 2

    async def test_tcp_failure_resets_on_success(self):
        """TCP failure followed by success should reset the streak counter."""
        import httpx
        providers = [
            FakeProvider("a", provider_type="tavily", priority=10,
                         search_error=httpx.ConnectError("DNS down")),
            FakeProvider("b", provider_type="brave", priority=20,
                         search_result=make_result(3, "b")),
        ]
        b = CircuitBreaker()
        result = await execute_search("test", 5, providers, b)
        assert not isinstance(result, FailureInfo)
        assert result.provider == "b"

    async def test_connect_timeout_classified_as_tcp_failure(self):
        """httpx.ConnectTimeout (NOT a subclass of ConnectError) must also count as tcp_failure."""
        import httpx
        providers = [
            FakeProvider("a", provider_type="tavily", priority=10,
                         search_error=httpx.ConnectTimeout("connect timed out")),
            FakeProvider("b", provider_type="brave", priority=20,
                         search_error=httpx.ConnectTimeout("connect timed out")),
            FakeProvider("c", provider_type="ddg", priority=30,
                         search_result=make_result(3, "c")),
        ]
        b = CircuitBreaker()
        result = await execute_search("test", 5, providers, b)
        assert isinstance(result, FailureInfo)
        assert sum(1 for f in result.failures if f["error"] == "tcp_failure") == 2
        assert all(f["provider"] != "c" for f in result.failures)

    async def testcall_counter_only_on_success(self):
        """DC2: failed attempts should NOT increment call_counter."""
        from pivot_web_search_mcp.routing import call_counter
        call_counter.reset()
        providers = [
            FakeProvider("flaky", priority=10,
                         search_error=RuntimeError("boom")),
            FakeProvider("ok", priority=20,
                         search_result=make_result(3, "ok")),
        ]
        b = CircuitBreaker()
        await execute_search("test", 5, providers, b)
        assert call_counter.value("flaky") == 0
        assert call_counter.value("ok") == 1

    async def test_stops_starting_new_groups_once_total_budget_is_spent(self):
        partial = SearchResult(
            results=[
                {"url": "https://partial-1.com", "title": "x", "snippet": "alpha"},
                {"url": "https://partial-2.com", "title": "y", "snippet": "beta"},
            ],
            provider="g1",
        )
        providers = [
            FakeProvider("g1", priority=10),
            FakeProvider("g2", priority=20),
            FakeProvider("g3", priority=30),
        ]
        b = CircuitBreaker()

        group_results = iter([
            AttemptResult(provider_name="g1", result=partial),
            AttemptResult(provider_name="g2", error="timeout"),
            AttemptResult(provider_name="g3", result=make_result(3, "g3")),
        ])

        with patch("pivot_web_search_mcp.routing.effective_budget", return_value=1.0), \
                patch("pivot_web_search_mcp.routing._execute_priority_group",
                      side_effect=lambda *args, **kwargs: next(group_results)) as mock_exec, \
                patch("pivot_web_search_mcp.routing.time.time", return_value=0.0), \
                patch("pivot_web_search_mcp.routing.time.monotonic",
                      side_effect=[0.0, 0.0, 0.6, 1.2]):
            result = await execute_search("zzz", 5, providers, b)

        assert not isinstance(result, FailureInfo)
        assert result.provider == "g1"
        assert mock_exec.call_count == 2

    async def test_retry_after_marks_provider_quota_exhausted(self):
        response = httpx.Response(
            429,
            headers={"Retry-After": "120"},
            request=httpx.Request("GET", "https://example.com/search"),
        )
        providers = [
            FakeProvider(
                "brave",
                priority=10,
                search_error=httpx.HTTPStatusError("rate limited", request=response.request, response=response),
            )
        ]
        b = CircuitBreaker()

        result = await execute_search("test", 5, providers, b)

        assert isinstance(result, FailureInfo)
        assert result.failures == [{"provider": "brave", "error": "rate_limited"}]
        assert quota.is_exhausted("brave") is True
        exhausted_until = quota.load_quota()["brave"]["exhausted_until"]
        assert exhausted_until is not None

    async def test_hedge_skips_second_leg_that_would_exhaust_quota(self):
        quota.set_provider_limit("hedge2", 1)
        providers = [
            FakeProvider("hedge1", priority=10, search_error=RuntimeError("boom")),
            FakeProvider("hedge2", priority=10, search_result=make_result(3, "hedge2", answer="A" * 40)),
            FakeProvider("fallback", priority=20, search_result=make_result(3, "fallback", answer="B" * 40)),
        ]
        b = CircuitBreaker()

        result = await execute_search("python tutorial", 5, providers, b)

        assert not isinstance(result, FailureInfo)
        assert result.provider == "fallback"


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
        b._get_entry("p1").opened_at = time.time() - (CB_COOLDOWN_SECONDS + 1)

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

    def test_recovery_respects_affinity(self):
        """Deep-only provider must not be picked for a general query."""
        b = CircuitBreaker()
        deep_only = FakeProvider("deep_only", affinity="deep")
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("deep_only")
        result = pick_recovery_candidate([deep_only], b, affinity="general")
        assert result is None

    def test_recovery_returns_closed_provider_when_available(self):
        b = CircuitBreaker()
        closed = FakeProvider("closed")
        open_p = FakeProvider("open_p")
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("open_p")
        result = pick_recovery_candidate([closed, open_p], b)
        assert result is not None
        assert result.name == "closed"

    def test_returns_none_when_all_open_are_still_cooling_down(self):
        b = CircuitBreaker()
        p1 = FakeProvider("p1")
        p2 = FakeProvider("p2")
        for provider in (p1, p2):
            for _ in range(CB_CONSECUTIVE_THRESHOLD):
                b.record_failure(provider.name)

        result = pick_recovery_candidate([p1, p2], b)
        assert result is None


# ---------------------------------------------------------------------------
# Call Counter Tests
# ---------------------------------------------------------------------------


class TestCallCounter:
    def test_initial_value_is_zero(self):
        c = CallCounter()
        assert c.value("new") == 0

    def test_increment(self):
        c = CallCounter()
        c.increment("p1")
        c.increment("p1")
        assert c.value("p1") == 2

    def test_reset(self):
        c = CallCounter()
        c.increment("p1")
        c.reset()
        assert c.value("p1") == 0


# ---------------------------------------------------------------------------
# Effective Budget Tests
# ---------------------------------------------------------------------------


def _grouped(*priorities_and_providers):
    """Build groups list from (priority, provider) tuples."""
    candidates = [ScoredProvider(p, prio, 0, 0) for prio, p in priorities_and_providers]
    return build_priority_groups(candidates)


class TestEffectiveBudget:
    def test_no_llm_uses_base_budget(self):
        groups = _grouped(
            (10, FakeProvider("tavily", provider_type="tavily")),
            (20, FakeProvider("ddg", provider_type="ddg")),
        )
        assert effective_budget(groups) == TOTAL_BUDGET_S

    def test_llm_in_first_group_extends_budget(self):
        llm = FakeProvider("perplexity", provider_type="llm_search", timeout=15)
        groups = _grouped((10, llm))
        assert effective_budget(groups) == TOTAL_BUDGET_S + 15 + LLM_BUDGET_EXTENSION_S

    def test_llm_in_second_group_extends_budget(self):
        """LLM in any group extends — must scan all groups, not just first."""
        groups = _grouped(
            (10, FakeProvider("tavily", provider_type="tavily")),
            (20, FakeProvider("perplexity", provider_type="llm_search", timeout=15)),
        )
        assert effective_budget(groups) == TOTAL_BUDGET_S + 15 + LLM_BUDGET_EXTENSION_S

    def test_max_llm_timeout_wins_across_groups(self):
        """Multiple LLMs: budget uses the longest timeout."""
        groups = _grouped(
            (10, FakeProvider("fast_llm", provider_type="llm_search", timeout=8)),
            (20, FakeProvider("slow_llm", provider_type="llm_search", timeout=20)),
        )
        assert effective_budget(groups) == TOTAL_BUDGET_S + 20 + LLM_BUDGET_EXTENSION_S

    def test_hedged_llm_group_extends_once(self):
        """Two LLMs at same priority: max timeout wins, extension applied once."""
        candidates = [
            ScoredProvider(FakeProvider("a", provider_type="llm_search", timeout=12), 10, 0, 0),
            ScoredProvider(FakeProvider("b", provider_type="llm_search", timeout=18), 10, 0, 1),
        ]
        groups = build_priority_groups(candidates)
        assert len(groups) == 1
        assert effective_budget(groups) == TOTAL_BUDGET_S + 18 + LLM_BUDGET_EXTENSION_S

    def test_llm_with_default_timeout(self):
        """LLM without explicit timeout falls back to DEFAULT_TIMEOUT['llm_search']."""
        llm = FakeProvider("p", provider_type="llm_search", timeout=None)
        # FakeProvider's timeout_seconds property returns DEFAULT_TIMEOUT lookup when timeout=None
        groups = _grouped((10, llm))
        expected = TOTAL_BUDGET_S + DEFAULT_TIMEOUT["llm_search"] + LLM_BUDGET_EXTENSION_S
        assert effective_budget(groups) == expected

    def test_empty_groups(self):
        assert effective_budget([]) == TOTAL_BUDGET_S


# ---------------------------------------------------------------------------
# Concurrency Tests
# ---------------------------------------------------------------------------


class TestConcurrentHalfOpen:
    async def test_concurrent_half_open_only_one_probe(self):
        """N concurrent is_available checks past cooldown should yield exactly one HALF_OPEN log."""
        from pivot_web_search_mcp import routing as routing_mod

        b = CircuitBreaker()
        for _ in range(CB_CONSECUTIVE_THRESHOLD):
            b.record_failure("p1")
        # Advance past cooldown
        b._get_entry("p1").opened_at = time.time() - (CB_COOLDOWN_SECONDS + 1)

        log_calls = []
        original_log = routing_mod.log

        def capture(msg):
            log_calls.append(msg)
            return original_log(msg)

        async def probe():
            return b.is_available("p1")

        with patch.object(routing_mod, "log", side_effect=capture):
            results = await asyncio.gather(*[probe() for _ in range(20)])

        assert all(results)
        half_open_logs = [m for m in log_calls if "HALF_OPEN (cooldown expired)" in m]
        assert len(half_open_logs) == 1


# ---------------------------------------------------------------------------
# Best-Partial Selection Tests
# ---------------------------------------------------------------------------


class TestBestPartialSelection:
    async def test_best_partial_prefers_answer_over_url_count(self):
        """A partial with an AI answer should beat a partial with more URLs but no answer."""
        with_answer = SearchResult(
            results=[{"url": "https://a.com", "title": "x", "snippet": "y"}],
            provider="answerer",
            answer="short answer",  # < 40 chars so verdict is not ACCEPT via Gate 0
        )
        more_urls = SearchResult(
            results=[
                {"url": f"https://b{i}.com", "title": "x", "snippet": "y"}
                for i in range(5)
            ],
            provider="urls",
        )
        providers = [
            FakeProvider("answerer", priority=10, search_result=with_answer),
            FakeProvider("urls", priority=20, search_result=more_urls),
        ]
        b = CircuitBreaker()
        result = await execute_search("zzz_unmatchable_term", 5, providers, b)
        assert not isinstance(result, FailureInfo)
        assert result.provider == "answerer"
