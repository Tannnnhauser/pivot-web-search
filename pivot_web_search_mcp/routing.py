"""Priority-group routing with hedged execution and circuit breaker.

Providers are grouped by effective_priority. Same-priority groups are
executed concurrently with staggered starts (hedging). First response
passing the quality gate wins. Groups are tried sequentially from
highest to lowest priority.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from . import quota as _quota
from .defaults import DEFAULT_TIMEOUT, SMART_DEFAULT_PRIORITY  # noqa: F401 — re-exported for callers
from .logging import log
from .quality_gate import Verdict, quality_gate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CB_CONSECUTIVE_THRESHOLD = 3
CB_COOLDOWN_SECONDS = 60
CB_RETRY_AFTER_MIN_S = 10
CB_RETRY_AFTER_MAX_S = 600

HEDGE_DELAY_MS = 200

# Soft total budget: once exceeded, no new priority group is started. The
# in-flight group still runs to its own per-provider timeout. The first
# group can extend the budget when an LLM provider is in play (Sec 5B.4) —
# see `_effective_budget`.
TOTAL_BUDGET_S = 10.0
LLM_BUDGET_EXTENSION_S = 2.0


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class BreakerState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class _BreakerEntry:
    __slots__ = ("state", "consecutive_failures", "opened_at", "cooldown_override")

    def __init__(self):
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self.opened_at: float | None = None
        self.cooldown_override: float | None = None


class CircuitBreaker:
    """Per-provider circuit breaker. Opens after 3 consecutive failures or on 429."""

    def __init__(self):
        self._breakers: dict[str, _BreakerEntry] = {}

    def _get_entry(self, name: str) -> _BreakerEntry:
        if name not in self._breakers:
            self._breakers[name] = _BreakerEntry()
        return self._breakers[name]

    def _cooldown_for(self, entry: _BreakerEntry) -> float:
        return entry.cooldown_override or CB_COOLDOWN_SECONDS

    def get_state(self, name: str) -> BreakerState:
        entry = self._get_entry(name)
        if entry.state == BreakerState.OPEN and entry.opened_at is not None:
            if time.time() - entry.opened_at >= self._cooldown_for(entry):
                entry.state = BreakerState.HALF_OPEN
                entry.cooldown_override = None
                log(f"{name} breaker HALF_OPEN (cooldown expired)")
        return entry.state

    def is_available(self, name: str) -> bool:
        state = self.get_state(name)
        return state in (BreakerState.CLOSED, BreakerState.HALF_OPEN)

    def record_success(self, name: str) -> None:
        entry = self._get_entry(name)
        state = self.get_state(name)
        if state == BreakerState.HALF_OPEN:
            entry.state = BreakerState.CLOSED
            entry.consecutive_failures = 0
            entry.opened_at = None
            log(f"{name} breaker CLOSED (probe succeeded)")
        elif state == BreakerState.CLOSED:
            entry.consecutive_failures = 0

    def record_failure(self, name: str) -> None:
        entry = self._get_entry(name)
        state = self.get_state(name)

        if state == BreakerState.HALF_OPEN:
            entry.state = BreakerState.OPEN
            entry.opened_at = time.time()
            log(f"{name} breaker OPEN (probe failed, cooldown restarted)")
            return

        if state == BreakerState.OPEN:
            return

        entry.consecutive_failures += 1
        if entry.consecutive_failures >= CB_CONSECUTIVE_THRESHOLD:
            entry.state = BreakerState.OPEN
            entry.opened_at = time.time()
            log(f"{name} breaker OPEN after {entry.consecutive_failures} consecutive failures")

    def open_immediately(self, name: str, cooldown_s: float | None = None) -> None:
        """Open breaker immediately (e.g. on 429). Optional custom cooldown from Retry-After."""
        entry = self._get_entry(name)
        entry.state = BreakerState.OPEN
        entry.opened_at = time.time()
        if cooldown_s is not None:
            entry.cooldown_override = min(max(cooldown_s, CB_RETRY_AFTER_MIN_S), CB_RETRY_AFTER_MAX_S)
        log(f"{name} breaker OPEN immediately (cooldown={self._cooldown_for(entry):.0f}s)")

    def time_until_recovery(self, name: str) -> float:
        entry = self._get_entry(name)
        if entry.state != BreakerState.OPEN or entry.opened_at is None:
            return 0.0
        elapsed = time.time() - entry.opened_at
        return max(0.0, self._cooldown_for(entry) - elapsed)

    def force_half_open(self, name: str) -> None:
        entry = self._get_entry(name)
        entry.state = BreakerState.HALF_OPEN
        log(f"{name} breaker forced HALF_OPEN (all-open fallback)")

    def reset_all(self) -> None:
        self._breakers.clear()

    def get_status(self, name: str) -> dict:
        entry = self._get_entry(name)
        state = self.get_state(name)
        status: dict[str, Any] = {
            "state": state.value,
            "consecutive_failures": entry.consecutive_failures,
        }
        if state == BreakerState.OPEN:
            status["cooldown_remaining"] = round(self.time_until_recovery(name), 1)
        return status


# ---------------------------------------------------------------------------
# Call counter (round-robin within same-priority groups)
# ---------------------------------------------------------------------------


class _CallCounter:
    def __init__(self):
        self._counts: dict[str, int] = {}

    def value(self, name: str) -> int:
        return self._counts.get(name, 0)

    def increment(self, name: str) -> None:
        self._counts[name] = self._counts.get(name, 0) + 1

    def reset(self) -> None:
        self._counts.clear()


_call_counter = _CallCounter()


# ---------------------------------------------------------------------------
# Provider selection and grouping
# ---------------------------------------------------------------------------


@dataclass
class ScoredProvider:
    provider: Any  # SearchProvider
    effective_priority: int
    call_counter: int
    rr_seed: int


def select_providers(
    providers: list,
    breaker: CircuitBreaker,
    affinity: str = "general",
) -> list[ScoredProvider]:
    """Filter and score eligible providers for routing."""
    candidates = []

    for p in providers:
        if not p.enabled:
            continue

        # Affinity gate
        if getattr(p, "affinity", "general") == "deep" and affinity != "deep":
            continue
        if affinity == "deep" and getattr(p, "affinity", "general") not in ("general", "deep"):
            continue

        # Quota gate
        if _quota.is_exhausted(p.name):
            log(f"SKIP {p.name}: quota exhausted")
            continue

        # Circuit breaker gate
        if not breaker.is_available(p.name):
            log(f"SKIP {p.name}: circuit breaker OPEN ({breaker.time_until_recovery(p.name):.0f}s remaining)")
            continue

        candidates.append(ScoredProvider(
            provider=p,
            effective_priority=getattr(p, "effective_priority", p.priority),
            call_counter=_call_counter.value(p.name),
            rr_seed=getattr(p, "_rr_seed", 0),
        ))

    candidates.sort(key=lambda c: (c.effective_priority, c.call_counter, c.rr_seed))
    return candidates


def build_priority_groups(candidates: list[ScoredProvider]) -> list[list[ScoredProvider]]:
    """Group candidates by effective_priority for hedged execution."""
    if not candidates:
        return []

    groups: list[list[ScoredProvider]] = []
    current_priority = candidates[0].effective_priority
    current_group: list[ScoredProvider] = []

    for c in candidates:
        if c.effective_priority != current_priority:
            groups.append(current_group)
            current_group = []
            current_priority = c.effective_priority
        current_group.append(c)

    if current_group:
        groups.append(current_group)
    return groups


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------


def _effective_budget(groups: list[list[ScoredProvider]]) -> float:
    """Total budget for walking priority groups.

    Sec 5B.4: when any group contains an llm_search provider (slow by nature),
    extend the budget by that provider's timeout + LLM_BUDGET_EXTENSION_S so
    the LLM isn't aborted mid-flight by the global deadline. Uses the longest
    LLM timeout across all groups when several are present.
    """
    llm_timeout = 0.0
    for group in groups:
        for scored in group:
            p = scored.provider
            if getattr(p, "provider_type", "") == "llm_search":
                t = getattr(p, "timeout_seconds",
                            DEFAULT_TIMEOUT.get(p.provider_type, 6))
                if t > llm_timeout:
                    llm_timeout = t
    if llm_timeout > 0:
        return TOTAL_BUDGET_S + llm_timeout + LLM_BUDGET_EXTENSION_S
    return TOTAL_BUDGET_S


@dataclass
class _AttemptResult:
    provider_name: str
    result: Any = None  # SearchResult or None
    error: str | None = None


@dataclass
class FailureInfo:
    failures: list = field(default_factory=list)


async def execute_search(
    query: str,
    max_results: int,
    providers: list,
    breaker: CircuitBreaker,
    affinity: str = "general",
    **kwargs,
):
    """Main routing entry: priority-group failover with hedging and quality gate.

    Returns SearchResult, FailureInfo, or None.
    """
    candidates = select_providers(providers, breaker, affinity=affinity)

    if not candidates:
        recovery = pick_recovery_candidate(providers, breaker)
        if recovery:
            candidates = [ScoredProvider(
                provider=recovery,
                effective_priority=getattr(recovery, "effective_priority", recovery.priority),
                call_counter=_call_counter.value(recovery.name),
                rr_seed=getattr(recovery, "_rr_seed", 0),
            )]
        else:
            return FailureInfo(failures=[{"provider": "all", "error": "all providers unavailable"}])

    groups = build_priority_groups(candidates)
    best_so_far = None
    failures: list[dict] = []
    consecutive_tcp_failures = 0

    deadline = time.time() + _effective_budget(groups)

    for group in groups:
        if time.time() >= deadline:
            log(f"BUDGET exceeded after {len(failures)} failures, stopping")
            break

        group_result = await _execute_priority_group(group, query, max_results, breaker, **kwargs)

        if group_result.error:
            failures.append({"provider": group_result.provider_name, "error": group_result.error})
            if group_result.error == "tcp_failure":
                consecutive_tcp_failures += 1
                if consecutive_tcp_failures >= 2:
                    return FailureInfo(failures=failures)
            continue

        if group_result.result is None:
            continue

        consecutive_tcp_failures = 0
        result = group_result.result

        # Quality gate
        results_list = result.results if hasattr(result, "results") else []
        answer = result.answer if hasattr(result, "answer") else None
        verdict = quality_gate(query, results_list, answer)

        if verdict == Verdict.ACCEPT:
            log(f"SUCCESS {group_result.provider_name}: {len(results_list)} results, verdict=ACCEPT")
            return result

        # Partial — keep best so far
        if best_so_far is None or len(results_list) > len(getattr(best_so_far, "results", [])):
            best_so_far = result
            log(f"PARTIAL {group_result.provider_name}: verdict={verdict.value}, continuing")

    if best_so_far is not None:
        return best_so_far
    return FailureInfo(failures=failures)


async def _execute_priority_group(
    group: list[ScoredProvider],
    query: str,
    max_results: int,
    breaker: CircuitBreaker,
    **kwargs,
) -> _AttemptResult:
    """Execute a same-priority group with hedged requests.

    For single-provider groups: direct execution with timeout.
    For multi-provider groups: staggered starts, first quality-gate pass wins.
    """
    if len(group) == 1:
        return await _attempt_single(group[0], query, max_results, breaker, **kwargs)

    # Hedged execution
    return await _attempt_hedged(group, query, max_results, breaker, **kwargs)


async def _attempt_single(
    scored: ScoredProvider,
    query: str,
    max_results: int,
    breaker: CircuitBreaker,
    **kwargs,
) -> _AttemptResult:
    """Execute a single provider with its timeout."""
    p = scored.provider
    timeout = getattr(p, "timeout_seconds", DEFAULT_TIMEOUT.get(p.provider_type, 6))

    try:
        result = await asyncio.wait_for(
            p.search(query, max_results, **kwargs),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        breaker.record_failure(p.name)
        log(f"{p.name} timed out after {timeout}s")
        return _AttemptResult(provider_name=p.name, error="timeout")
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError, OSError) as e:
        breaker.record_failure(p.name)
        log(f"{p.name} tcp failure: {e}")
        return _AttemptResult(provider_name=p.name, error="tcp_failure")
    except Exception as e:
        breaker.record_failure(p.name)
        log(f"{p.name} error: {e}")
        return _AttemptResult(provider_name=p.name, error=str(e))

    if result is not None:
        _call_counter.increment(p.name)
        breaker.record_success(p.name)
        _quota.record_usage(p.name)
        return _AttemptResult(provider_name=p.name, result=result)
    else:
        breaker.record_failure(p.name)
        return _AttemptResult(provider_name=p.name, error="returned no results")


async def _attempt_hedged(
    group: list[ScoredProvider],
    query: str,
    max_results: int,
    breaker: CircuitBreaker,
    **kwargs,
) -> _AttemptResult:
    """Staggered concurrent execution. First quality-gate pass wins."""

    async def _delayed_attempt(scored: ScoredProvider, delay_ms: int):
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        return await _attempt_single(scored, query, max_results, breaker, **kwargs)

    tasks: dict[asyncio.Task, ScoredProvider] = {}
    for i, scored in enumerate(group):
        delay = i * HEDGE_DELAY_MS
        task = asyncio.create_task(_delayed_attempt(scored, delay))
        tasks[task] = scored

    outer_timeout = max(
        getattr(s.provider, "timeout_seconds", DEFAULT_TIMEOUT.get(s.provider.provider_type, 6))
        for s in group
    ) + (len(group) - 1) * HEDGE_DELAY_MS / 1000.0

    best_partial: _AttemptResult | None = None
    remaining = set(tasks.keys())

    try:
        deadline = time.time() + outer_timeout
        while remaining:
            wait_timeout = max(deadline - time.time(), 0.1)
            done, remaining = await asyncio.wait(
                remaining, timeout=wait_timeout, return_when=asyncio.FIRST_COMPLETED
            )

            if not done:
                break

            for task in done:
                attempt = task.result()
                if attempt.result is not None:
                    results_list = attempt.result.results if hasattr(attempt.result, "results") else []
                    answer = attempt.result.answer if hasattr(attempt.result, "answer") else None
                    verdict = quality_gate(query, results_list, answer)

                    if verdict == Verdict.ACCEPT:
                        # Winner — cancel remaining
                        for t in remaining:
                            t.cancel()
                        return attempt

                    if best_partial is None or len(results_list) > len(
                        getattr(best_partial.result, "results", []) if best_partial.result else []
                    ):
                        best_partial = attempt
                elif attempt.error and best_partial is None:
                    best_partial = attempt

    finally:
        for t in remaining:
            t.cancel()
        # Suppress cancellation errors
        for t in remaining:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    if best_partial is not None:
        return best_partial
    return _AttemptResult(provider_name=group[0].provider.name, error="all hedged attempts failed")


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def pick_recovery_candidate(providers, breaker: CircuitBreaker):
    """All-OPEN fallback: find provider closest to cooldown expiry, force HALF_OPEN."""
    best = None
    best_time = float("inf")

    for p in providers:
        if not p.enabled:
            continue
        if _quota.is_exhausted(p.name):
            continue
        remaining = breaker.time_until_recovery(p.name)
        if remaining < best_time:
            best = p
            best_time = remaining

    if best is not None:
        breaker.force_half_open(best.name)
    return best
