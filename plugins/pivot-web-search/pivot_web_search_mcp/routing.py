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
from .defaults import DEFAULT_TIMEOUT
from .logging import log
from .quality_gate import Verdict, quality_gate
from .results import dedup_and_rank

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
# see `effective_budget`.
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


class CallCounter:
    def __init__(self):
        self._counts: dict[str, int] = {}

    def value(self, name: str) -> int:
        return self._counts.get(name, 0)

    def increment(self, name: str) -> None:
        self._counts[name] = self._counts.get(name, 0) + 1

    def reset(self) -> None:
        self._counts.clear()


call_counter = CallCounter()


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
            call_counter=call_counter.value(p.name),
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


def effective_budget(groups: list[list[ScoredProvider]]) -> float:
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
class AttemptResult:
    provider_name: str
    result: Any = None  # SearchResult or None
    error: str | None = None


@dataclass
class FailureInfo:
    failures: list = field(default_factory=list)


@dataclass
class SearchOutcome:
    """Internal discriminated outcome of a routing attempt.

    Exactly one field is populated:
      - result: SearchResult on ACCEPT
      - failure: FailureInfo on hard abort (TCP, all-unavailable, no partial)
      - partial: best non-ACCEPT SearchResult when nothing passed the gate
    """
    result: Any = None
    failure: FailureInfo | None = None
    partial: Any = None

    @classmethod
    def ok(cls, result):
        return cls(result=result)

    @classmethod
    def failed(cls, failure: FailureInfo):
        return cls(failure=failure)

    @classmethod
    def best_partial(cls, partial):
        return cls(partial=partial)

    def to_legacy(self):
        """Collapse to the historical SearchResult | FailureInfo | None union."""
        if self.result is not None:
            return self.result
        if self.partial is not None:
            return self.partial
        return self.failure


def partial_score(result) -> tuple:
    """Rank partial results: prefer any AI answer, then more URLs."""
    if result is None:
        return (False, 0)
    answer = getattr(result, "answer", None)
    has_answer = bool(answer and str(answer).strip())
    results_list = getattr(result, "results", []) or []
    return (has_answer, len(results_list))


def _classify_unavailable(providers, breaker: CircuitBreaker, affinity: str) -> list[dict]:
    """Per-provider state for the all-unavailable failure surface."""
    rows: list[dict] = []
    for p in providers:
        if not p.enabled:
            rows.append({"provider": p.name, "state": "disabled"})
            continue
        if getattr(p, "affinity", "general") == "deep" and affinity != "deep":
            rows.append({"provider": p.name, "state": "affinity_mismatch"})
            continue
        if affinity == "deep" and getattr(p, "affinity", "general") not in ("general", "deep"):
            rows.append({"provider": p.name, "state": "affinity_mismatch"})
            continue
        entry: dict[str, Any] = {"provider": p.name}
        if _quota.is_exhausted(p.name):
            entry["state"] = "quota_exhausted"
            retry_s = _quota.retry_after_seconds(p.name)
            if retry_s is not None:
                entry["retry_after_seconds"] = retry_s
        elif not breaker.is_available(p.name):
            entry["state"] = "circuit_open"
            entry["cooldown_remaining_seconds"] = round(breaker.time_until_recovery(p.name), 1)
        else:
            entry["state"] = "available"
        rows.append(entry)
    return rows


def _select_or_recover(
    providers: list,
    breaker: CircuitBreaker,
    affinity: str,
) -> tuple[list[ScoredProvider], FailureInfo | None]:
    """Return (candidates, None) on success, or ([], FailureInfo) when nothing is eligible."""
    candidates = select_providers(providers, breaker, affinity=affinity)
    if candidates:
        return candidates, None

    recovery = pick_recovery_candidate(providers, breaker, affinity=affinity)
    if recovery:
        return [ScoredProvider(
            provider=recovery,
            effective_priority=getattr(recovery, "effective_priority", recovery.priority),
            call_counter=call_counter.value(recovery.name),
            rr_seed=getattr(recovery, "_rr_seed", 0),
        )], None

    return [], FailureInfo(failures=_classify_unavailable(providers, breaker, affinity))


async def _walk_priority_groups(
    groups: list[list[ScoredProvider]],
    query: str,
    max_results: int,
    breaker: CircuitBreaker,
    **kwargs,
) -> SearchOutcome:
    """Iterate priority groups under a soft total budget, returning a SearchOutcome."""
    best_so_far = None
    failures: list[dict] = []
    consecutive_tcp_failures = 0

    budget_s = effective_budget(groups)
    started_at = time.monotonic()

    for group in groups:
        elapsed_s = time.monotonic() - started_at
        if elapsed_s >= budget_s:
            log(f"BUDGET exceeded after {elapsed_s:.2f}s and {len(failures)} failures, stopping")
            break

        group_result = await _execute_priority_group(group, query, max_results, breaker, **kwargs)

        if group_result.error:
            failures.append({"provider": group_result.provider_name, "error": group_result.error})
            if group_result.error == "tcp_failure":
                consecutive_tcp_failures += 1
                if consecutive_tcp_failures >= 2:
                    return SearchOutcome.failed(FailureInfo(failures=failures))
            continue

        if group_result.result is None:
            continue

        consecutive_tcp_failures = 0
        result = group_result.result

        results_list = result.results if hasattr(result, "results") else []
        answer = result.answer if hasattr(result, "answer") else None
        verdict = quality_gate(query, results_list, answer)

        if verdict == Verdict.ACCEPT:
            log(f"SUCCESS {group_result.provider_name}: {len(results_list)} results, verdict=ACCEPT")
            return SearchOutcome.ok(result)

        if best_so_far is None or partial_score(result) > partial_score(best_so_far):
            best_so_far = result
            log(f"PARTIAL {group_result.provider_name}: verdict={verdict.value}, continuing")

    if best_so_far is not None:
        return SearchOutcome.best_partial(best_so_far)
    return SearchOutcome.failed(FailureInfo(failures=failures))


async def execute_search(
    query: str,
    max_results: int,
    providers: list,
    breaker: CircuitBreaker,
    affinity: str = "general",
    **kwargs,
):
    """Main routing entry: priority-group failover with hedging and quality gate.

    Returns SearchResult, FailureInfo, or None (legacy union; see SearchOutcome).
    """
    candidates, failure = _select_or_recover(providers, breaker, affinity)
    if failure is not None:
        return failure

    outcome = await _walk_priority_groups(
        build_priority_groups(candidates), query, max_results, breaker, **kwargs,
    )
    return outcome.to_legacy()


async def _execute_priority_group(
    group: list[ScoredProvider],
    query: str,
    max_results: int,
    breaker: CircuitBreaker,
    **kwargs,
) -> AttemptResult:
    """Execute a same-priority group with hedged requests.

    For single-provider groups: direct execution with timeout.
    For multi-provider groups: staggered starts, first quality-gate pass wins.
    """
    if len(group) == 1:
        return await attempt_single(group[0], query, max_results, breaker, **kwargs)

    # Hedged execution
    return await _attempt_hedged(group, query, max_results, breaker, **kwargs)


async def attempt_single(
    scored: ScoredProvider,
    query: str,
    max_results: int,
    breaker: CircuitBreaker,
    **kwargs,
) -> AttemptResult:
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
        return AttemptResult(provider_name=p.name, error="timeout")
    except httpx.HTTPStatusError as e:
        retry_after = e.response.headers.get("Retry-After") if e.response is not None else None
        if e.response is not None and e.response.status_code == 429 and retry_after:
            _quota.mark_rate_limited(p.name, retry_after)
            log(f"{p.name} rate limited; quota exhausted until Retry-After")
            return AttemptResult(provider_name=p.name, error="rate_limited")
        breaker.record_failure(p.name)
        log(f"{p.name} http failure: {e}")
        return AttemptResult(provider_name=p.name, error=str(e))
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError, OSError) as e:
        breaker.record_failure(p.name)
        log(f"{p.name} tcp failure: {e}")
        return AttemptResult(provider_name=p.name, error="tcp_failure")
    except Exception as e:
        breaker.record_failure(p.name)
        log(f"{p.name} error: {e}")
        return AttemptResult(provider_name=p.name, error=str(e))

    if result is not None:
        call_counter.increment(p.name)
        breaker.record_success(p.name)
        _quota.record_usage(p.name)
        return AttemptResult(provider_name=p.name, result=result)
    else:
        breaker.record_failure(p.name)
        return AttemptResult(provider_name=p.name, error="returned no results")


async def _attempt_hedged(
    group: list[ScoredProvider],
    query: str,
    max_results: int,
    breaker: CircuitBreaker,
    **kwargs,
) -> AttemptResult:
    """Staggered concurrent execution. First quality-gate pass wins."""

    async def _delayed_attempt(scored: ScoredProvider, delay_ms: int):
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
            if _quota.would_exhaust_on_next_use(scored.provider.name):
                log(f"SKIP {scored.provider.name}: hedge would exhaust quota")
                return AttemptResult(provider_name=scored.provider.name, error="hedge_skipped_quota")
        return await attempt_single(scored, query, max_results, breaker, **kwargs)

    tasks: dict[asyncio.Task, ScoredProvider] = {}
    for i, scored in enumerate(group):
        delay = i * HEDGE_DELAY_MS
        task = asyncio.create_task(_delayed_attempt(scored, delay))
        tasks[task] = scored

    # +0.5s slack so asyncio.wait doesn't fire before the last task's per-attempt wait_for under scheduling latency
    outer_timeout = max(
        getattr(s.provider, "timeout_seconds", DEFAULT_TIMEOUT.get(s.provider.provider_type, 6))
        for s in group
    ) + (len(group) - 1) * HEDGE_DELAY_MS / 1000.0 + 0.5

    best_partial: AttemptResult | None = None
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

                    if best_partial is None or partial_score(attempt.result) > partial_score(
                        best_partial.result if best_partial else None
                    ):
                        best_partial = attempt
                elif attempt.error and best_partial is None:
                    best_partial = attempt

    finally:
        for t in remaining:
            t.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)

    if best_partial is not None:
        return best_partial
    return AttemptResult(provider_name=group[0].provider.name, error="all hedged attempts failed")


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def pick_recovery_candidate(providers, breaker: CircuitBreaker, affinity: str = "general"):
    """Return the first enabled, quota-OK provider whose breaker is CLOSED or HALF_OPEN.

    Calls ``breaker.get_state``, which promotes OPEN → HALF_OPEN as a side effect
    when cooldown has expired. So a breaker that just exited cooldown is eligible;
    one that is still inside cooldown is skipped. Returns None if every candidate
    is enabled-off, deep-affinity-mismatched, quota-exhausted, or still cooling.
    """

    for p in providers:
        if not p.enabled:
            continue
        if getattr(p, "affinity", "general") == "deep" and affinity != "deep":
            continue
        if affinity == "deep" and getattr(p, "affinity", "general") not in ("general", "deep"):
            continue
        if _quota.is_exhausted(p.name):
            continue
        state = breaker.get_state(p.name)
        if state in (BreakerState.CLOSED, BreakerState.HALF_OPEN):
            return p
    return None


# ---------------------------------------------------------------------------
# Super mode: query all eligible providers in parallel, merge results
# ---------------------------------------------------------------------------


async def execute_super_search(
    query: str,
    max_results: int,
    providers: list,
    breaker: CircuitBreaker,
    affinity: str = "general",
    **kwargs,
):
    """Query all eligible providers in parallel; merge results via dedup_and_rank.

    Super mode ignores priority ordering and the quality gate. Returns
    SearchResult or None when every candidate produced nothing.
    """
    from .providers import SearchResult

    candidates = select_providers(providers, breaker, affinity=affinity)
    if not candidates:
        return None

    async def _timed_search(provider):
        try:
            return await asyncio.wait_for(
                provider.search(query, max_results, **kwargs),
                timeout=provider.timeout_seconds,
            )
        except asyncio.TimeoutError:
            breaker.record_failure(provider.name)
            log(f"super: {provider.name} timed out after {provider.timeout_seconds}s")
            return None
        except Exception as e:
            breaker.record_failure(provider.name)
            log(f"super: {provider.name} failed: {e}")
            return None

    search_results = await asyncio.gather(*(_timed_search(c.provider) for c in candidates))

    results_by_provider: dict[str, list] = {}
    answer = None

    for c, sr in zip(candidates, search_results):
        p = c.provider
        if sr and sr.results:
            results_by_provider[p.name] = sr.results
            _quota.record_usage(p.name)
            breaker.record_success(p.name)
            call_counter.increment(p.name)
            log(f"super: {p.name} returned {len(sr.results)} results")
            if sr.answer and not answer:
                answer = sr.answer
        elif sr is None:
            log(f"super: {p.name} returned nothing")

    if not results_by_provider:
        return None

    merged, providers_used = dedup_and_rank(results_by_provider, max_results)
    return SearchResult(
        results=merged,
        provider=",".join(providers_used),
        answer=answer,
    )
