# Routing Edge-Case Handling Specification

This document defines edge-case handling for the routing design: **pure priority + quota gate + circuit breaker + latency budget**, with ordering by `(effective_priority, call_counter)`, hedged requests for same-priority providers, and quality-gate failover.

The fixes below are normative. Where a fix changes the design, the change is called out explicitly as a **design delta**.

## 1. Provider With No Quota Declaration but Repeated 429s

### Scenario
A provider such as Perplexity Sonar Pro has no declared quota in config. After about 50 requests, it starts returning HTTP `429`. The circuit breaker opens, but there is no quota window to tell the system when to retry.

### Root Cause
The design ties exhaustion to declared quotas, but `429` is a runtime signal independent of configured quota metadata.

### Fix
The circuit breaker handles this independently of quota:

Rules:

- `429` opens the breaker immediately (1 failure = open).
- If a `Retry-After` header is present, use it as cooldown (clamped to 10-600s).
- If no `Retry-After`, use the fixed default cooldown (60s).
- When the cooldown expires, allow exactly one half-open probe.
- A successful probe closes the breaker.
- A probe that returns `429` reopens the breaker with the same fixed cooldown.

No exponential backoff. A persistently rate-limited provider naturally retries at 1/60s — negligible load for a CLI tool.

```python
def on_http_429(provider_name: str, headers: dict):
    retry_after = parse_retry_after(headers)
    cooldown = clamp(retry_after, 10, 600) if retry_after else DEFAULT_COOLDOWN
    open_breaker(provider_name, cooldown=cooldown)
    log(f"BREAKER {provider_name}: CLOSED -> OPEN (429, cooldown={cooldown}s)")
```

### Interactions
- No quota declaration is needed; breaker operates independently.
- Provider stays in config and becomes eligible again after cooldown.
- Multiple instances of the same provider type maintain independent breaker state.
- Log a diagnostic: `"WARNING: {name} returned 429 but has no quota configured. Consider adding quota declaration."`

## 2. All Providers Circuit-Broken Simultaneously

### Scenario
Every non-exhausted provider is in breaker state `OPEN` at the same time.

### Root Cause
Normal routing removes open providers from the eligible set. If every provider is open, the scheduler has no candidate.

### Fix
Define a global all-open recovery policy:

Rules:

- Build the normal eligible set first.
- If the eligible set is empty:
  - Ignore exhausted providers (they can't help).
  - Find the open provider whose cooldown expires soonest.
  - If its cooldown has expired, move it to `HALF_OPEN` and try exactly one probe.
  - If no cooldown has expired yet, return a structured error with the soonest retry time.

```python
def pick_recovery_candidate(providers: list[Provider]) -> ScoredProvider | None:
    """Find the open provider closest to recovery."""
    cooling = [
        p for p in providers
        if p.enabled and not is_exhausted(p)
        and circuit_breaker.get_state(p.name) == OPEN
    ]
    if not cooling:
        return None

    candidate = min(cooling, key=lambda p: circuit_breaker.opens_at(p.name))
    
    if circuit_breaker.cooldown_expired(candidate.name):
        circuit_breaker.transition_half_open(candidate.name)
        log(f"PROBE {candie}: forced HALF_OPEN (all-open fallback)")
        return ScoredProvider(provider=candidate, sort_key=(candidate.priority, 0))
    
    return None  # All still cooling — caller returns FailureInfo with retry time
```

### Error Response
When no recovery is possible:
```json
{
  "error": "All providers unavailable",
  "reason": "all_circuit_open",
  "recovers_in": "45s",
  "recovery_candidate": "ddg",
  "suggestions": ["Wait for circuit breaker cooldown", "Add another provider"]
}
```

### Interactions
- Exhausted providers remain skipped — they are not recovery candidates.
- The total budget (10s) still applies even during recovery attempts.
- Network-down short-circuit (2 TCP failures) takes precedence over recovery logic.

## 3. Priority Conflicts: Same Priority, One Provider Exhausted

### Scenario
Two providers share the same priority and participate in round-robin via hedged requests. One has a quota and becomes exhausted; the other is unlimited.

### Root Cause
Round-robin fairness only makes sense over the eligible set. Exhausted providers should not occupy hedge slots.

### Fix
Exhausted providers are removed from the eligible pool before sorting and do not participate in hedging or round-robin.

Rules:

- Evaluate exhaustion before forming priority groups.
- If `used >= limit`, the provider is excluded from the current request.
- Its `call_counter` does not increment while excluded.
- On quota reset, the provider re-enters the pool. Since call_counter is in-memory and resets each session, no normalization is needed across sessions. Within a session, re-entry at the current counter value means it may get slightly more calls initially — acceptable for a CLI tool.

```python
def eligible_at_priority(providers: list, priority: int) -> list:
    return [
        p for p in providers
        if p.effective_priority == priority
        and not is_exhausted(p)
        and circuit_breaker.get_state(p.name) != OPEN
    ]
```

### Interactions
- Hedged requests only fire for eligible providers at the same priority.
- If a priority group has only one remaining eligible provider, it executes as a simple sequential call (no hedging).
- Circuit-broken providers are handled the same way.

## 4. Health Check Success but Search Returns `None`

### Scenario
A provider passes `health_check()` at startup, but at search time it returns `None`. The question is whether that counts toward the circuit breaker.

### Root Cause
`health_check()` proves only that the provider looked reachable at probe time. A runtime `None` is ambiguous.

### Fix
Classify search outcomes explicitly:

| Outcome | Breaker Action | Failover Action |
|---------|---------------|-----------------|
| Results >= min_acceptable | record_success | Return result |
| Results > 0 but < min_acceptable | record_success | Continue (quality gate) |
| Result is None or empty | record_failure | Continue failover |
| HTTP 429 | open immediately | Continue failover |
| HTTP 5xx | record_failure | Continue failover |
| Timeout (asyncio.TimeoutError) | record_failure | Continue failover |
| TCP ConnectionError | record_failure + network-down counter | Continue failover |

Key distinctions:
- **Insufficient results** (1 result for a max_results=5 query): NOT a breaker failure. The provider worked, just returned little. Records success, increments call_counter, but failover continues.
- **None/empty**: IS a breaker failure. Provider failed to produce anything.
- **Quality gate failure** (keyword overlap): NOT a breaker failure. Same as insufficient results.

```python
def classify_and_handle(provider, result, error=None):
    if error:
        if isinstance(error, asyncio.TimeoutError):
            circuit_breaker.record_failure(provider.name)
            return "timeout"
        if is_429(error):
            circuit_breaker.open_immediately(provider.name, error.headers)
            return "rate_limited"
        circuit_breaker.record_failure(provider.name)
        return "error"
    
    if result is None or not result.results:
        circuit_breaker.record_failure(provider.name)
        return "empty"
    
    # Has results — provider worked
    circuit_breaker.record_success(provider.name)
    record_usage(provider.name)
    provider.call_counter += 1
    return "success"
```

### Interactions
- A passing `health_check()` does not reset breaker state.
- Startup health check is informational (for status display), not authoritative for routing.
- 3 consecutive None responses → breaker opens (60s cooldown).

## 5. Config Validation: Two Unlimited Providers at the Same Priority

### Scenario
Two unlimited providers have the same priority. The question is whether config loading should reject that as ambiguous.

### Root Cause
Same-priority routing is valid under the design, but the initial tie-break must be deterministic before `call_counter` diverges.

### Fix
This is **valid config**. It must not raise a validation error.

Rules:

- Same-priority providers form a hedged-request group.
- The hedge ordering within a group uses `call_counter` as primary tiebreaker and config order (`rr_seed`) as secondary.
- The first-in-config provider fires immediately; the second fires after `hedge_delay` ms.
- After each attempt, `call_counter` increments, naturally rotating the "first to fire" slot.

```python
def sort_key(provider):
    return (provider.effective_priority, provider.call_counter, provider.rr_seed)
```

On the first request (both counters = 0), config order determines who fires first. On the second request, the one that ran first now has counter=1 and sorts second — natural round-robin within the hedged group.

### Interactions
- If one same-priority provider becomes exhausted or circuit-broken, it leaves the group and the remaining providers execute without hedging (or with reduced hedging).
- Diagnostic note in status: no warning. Same-priority unlimited providers is a valid load-balancing configuration.

## 6. Hedged Request: Both Providers Return Before Timeout

### Scenario
Two same-priority providers are hedged. The first responds in 100ms with 3 results. The second responds in 250ms with 5 results. Which result is used?

### Root Cause
Hedging's purpose is latency protection, not quality maximization. The design must choose between "first complete response wins" and "best response wins."

### Fix
**First response that passes the quality gate wins.** Cancel the remaining tasks.

Rules:

- When a hedged task completes, evaluate the quality gate immediately.
- If it passes: return the result, cancel all pending tasks.
- If it fails: wait for the next task to complete.
- If all hedged tasks complete and none pass the quality gate: return the best result (most results) and continue to the next priority group.

```python
async def _hedged_request(group, query, max_results, timeout, **kwargs):
    tasks = []
    for i, scored in enumerate(group):
        tasks.append(asyncio.create_task(
            _delayed_search(scored.provider, query, max_results, delay=i * HEDGE_DELAY, **kwargs)
        ))
    
    best = None
    while tasks:
        done, tasks_set = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        tasks = list(tasks_set)
        
        for task in done:
            result = task.result()
            if result and quality_gate_passes(result.results, query, result.answer):
                for t in tasks:
                    t.cancel()
                return result
            if result and (best is None or len(result.results) > len(best.results)):
                best = result
    
    return best  # None or partial — caller decides whether to continue failover
```

### Interactions
- Both providers are charged quota (if they responded) and both increment call_counter.
- The cancelled task may or may not have fired its HTTP request (depends on hedge_delay). If cancelled before the delay, no quota is consumed.
- Circuit breaker records the outcome for all tasks that actually received a response.

## 7. Network Down: Proxy-Induced vs. Internet-Down

### Scenario
The user has a proxy configured. Provider A fails with TCP error through the proxy. The question is: is the internet down, or just the proxy?

### Root Cause
The network-down short-circuit (2 consecutive TCP failures → abort) could fire prematurely if the issue is proxy-specific rather than internet-wide.

### Fix
Network-down detection counts consecutive TCP failures **across different network paths**, not just different providers.

Rules:

- If a provider fails via proxy, the proxy failover (direct → proxy1 → proxy2) should be attempted first.
- Only count toward network-down after the per-provider proxy chain is exhausted.
- If provider A exhausts all proxy paths with TCP failures AND provider B also fails with TCP error on its first path: that's 2 "confirmed unreachable" providers → network-down.

```python
# Network-down only fires after proxy failover is exhausted per provider
consecutive_provider_tcp_failures = 0

for provider in candidates:
    result = await provider.search_with_proxy_failover(query, ...)
    if result.error_type == "tcp_all_proxies_exhausted":
        consecutive_provider_tcp_failures += 1
        if consecutive_provider_tcp_failures >= 2:
            return FailureInfo(reason="network_unreachable", ...)
    else:
        consecutive_provider_tcp_failures = 0
```

### Interactions
- Per-host proxy cache (existing feature) means that on subsequent requests, the working proxy path is tried first — reducing false network-down triggers.
- If only one provider is configured, network-down cannot be detected (single failure could be provider-specific). In this case, the timeout handles it (5s timeout → error).

## 8. Super Mode: Provider Timeout Within Parallel Gather

### Scenario
In super mode, all providers are queried in parallel via `asyncio.gather`. One provider is very slow (e.g., Gemini taking 15s). The total budget is 10s.

### Root Cause
`asyncio.gather` by default waits for all tasks. Without a budget constraint, one slow provider blocks the entire super mode result.

### Fix
Super mode uses `asyncio.wait` with the total budget as timeout, not `asyncio.gather`:

```python
async def super_mode_search(query, max_results, providers, **kwargs):
    budget = TOTAL_BUDGET  # 10s
    
    tasks = {
        asyncio.create_task(p.search(query, max_results, **kwargs)): p
        for p in providers
        if not is_exhausted(p) and circuit_breaker.get_state(p.name) != OPEN
    }
    
    done, pending = await asyncio.wait(tasks.keys(), timeout=budget)
    
    # Cancel stragglers
    for task in pending:
        task.cancel()
        provider = tasks[task]
        log(f"SUPER_TIMEOUT {provider.name}: exceeded {budget}s budget")
    
    # Collect results from completed tasks
    results_by_provider = {}
    for task in done:
        provider = tasks[task]
        try:
            result = task.result()
            if result and result.results:
                results_by_provider[provider.name] = result.results
                circuit_breaker.record_success(provider.name)
        except Exception as e:
            circuit_breaker.record_failure(provider.name)
    
    # Dedup and rank across providers
    return dedup_and_rank(results_by_provider, max_results)
```

### Interactions
- Timed-out providers do NOT open the circuit breaker (timeout in super mode is budget-driven, not provider health).
- Timed-out providers DO count toward quota if their request was sent (best-effort — may not know if the request completed server-side).
- The total budget for super mode is the same as for failover mode (10s default).

## 9. Quality Gate: LLM Provider Returns Answer but Zero URLs

### Scenario
An `llm_search` provider (e.g., Perplexity) returns a high-quality answer string but zero traditional search result objects. The quality gate's `unique_urls < 2` check would fail it.

### Root Cause
LLM search providers are fundamentally different — their primary output is an answer, not a list of URLs. The quality gate must account for this.

### Fix
Already handled in the quality gate design (Gate 0):

```python
# Gate 0: LLM answer is a valid response regardless of result count
if answer and len(answer) > 20:
    return True  # passes quality gate
```

Additional rules:
- If the provider returns both an answer AND results, both are included in the response.
- If the provider returns only an answer with no results, the response is still valid.
- The answer field is extracted from the LLM response by the `llm_search_formats.py` strategy layer.
- Keyword overlap (Gate 2) is skipped when an answer passes Gate 0.

### Interactions
- Circuit breaker: A response with an answer (even if 0 URLs) counts as success.
- Quota: Charged normally — the provider was called and responded.
- Super mode: LLM answers are not merged with URL-based results. If an LLM provider participates in super mode, its answer is presented separately.

## 10. Timeout Interaction with Hedged Requests

### Scenario
Provider A (priority 20) is hedged with Provider B (priority 20). Provider A fires immediately. After 200ms (hedge delay), Provider B fires. Provider A times out at 5s. Provider B responded at 3s. What happens?

### Timeline
```
t=0ms:    Provider A starts
t=200ms:  Provider B starts (hedge)
t=3000ms: Provider B responds with 5 results → quality gate passes
t=3000ms: Provider A is cancelled (still pending)
```

### Fix
This is the expected happy path for hedging. Provider B's response passes the quality gate, so:
- Provider A's task is cancelled at t=3000ms.
- Provider A does NOT count as a timeout failure for circuit breaker purposes (it was cancelled, not timed out).
- Only Provider B's quota is charged (Provider A's request may or may not have reached the server — best-effort).

Rules for cancelled tasks:
- Cancelled before `hedge_delay`: No side effects. Provider was never called.
- Cancelled after firing but before response: No circuit breaker update. Quota may or may not be consumed server-side (not our problem — user already got their answer).
- Timeout (not cancelled — actually exceeded per_provider_timeout): Circuit breaker records failure.

### Interactions
- The per-provider timeout (5s) is the individual task timeout.
- The total budget (10s) is the outer deadline for the entire priority group traversal.
- A hedged group's effective timeout is `min(per_provider_timeout, total_budget_remaining)`.
