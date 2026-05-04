# Routing Edge-Case Handling Specification

This document defines edge-case handling for the routing design under test: **pure priority + quota gate + circuit breaker**, with ordering by `(effective_priority, call_counter)`, soft conservation via `conserve=true`, and failover on fewer than 2 results.

The fixes below are normative. Where a fix changes the design under test, the change is called out explicitly as a **design delta**.

## 1. Pace Ratio on Day 1 of a Monthly Quota

### Scenario
A monthly-quota provider starts a new period with `used=0`. At the exact start of the period, `elapsed=0`, so the naive formula

```python
pace_ratio = (used / limit) / (elapsed / period)
```

becomes `0 / 0`.

### Root Cause
The formula assumes a continuous elapsed fraction greater than zero. Quotas are discrete, but time starts at zero. The first request in a new period can also produce an artificially huge ratio if elapsed time is only a few seconds.

### Fix
**Design delta:** define `pace_ratio` with both a zero-usage fast path and a denominator floor.

Rules:

- If the provider has no declared quota, `pace_ratio = 0.0`.
- If `used == 0`, `pace_ratio = 0.0`.
- Compute `elapsed_frac` as real elapsed fraction in the quota window.
- Clamp the denominator to `max(elapsed_frac, 1 / limit, pace_min_elapsed_ratio)`.
- Use the clamped denominator for all `conserve` decisions.

This preserves the intent of pacing without division by zero and avoids a false conserve trigger on the first few requests of a new period.

```python
def compute_pace_ratio(entry, now):
    limit = entry.limit
    used = entry.used
    if not limit or limit <= 0:
        return 0.0
    if used <= 0:
        return 0.0

    period_start, period_end = quota_window(entry, now)
    elapsed_frac = (now - period_start).total_seconds() / (
        period_end - period_start
    ).total_seconds()

    quantum_floor = 1.0 / limit
    denom_floor = max(quantum_floor, CONFIG.routing.pace_min_elapsed_ratio)
    safe_elapsed = max(elapsed_frac, denom_floor)

    usage_frac = used / limit
    return usage_frac / safe_elapsed
```

### Config Surface
- `routing.pace_min_elapsed_ratio` (float, default `0.01`)
- No new per-provider config is required; existing `quota.limit` and `quota.period` are sufficient.

### Interactions with Other Design Elements
- `conserve=true` uses the corrected `pace_ratio`; it no longer misfires at period start.
- Exhaustion logic is unchanged: `used >= limit` still skips the provider entirely.
- Round-robin is unaffected because deferral is based on `effective_priority`, not on `call_counter`.

## 2. Perplexity with No Quota Declaration but Repeated 429s

### Scenario
A provider such as Perplexity Sonar Pro has no declared quota in config. After about 50 requests, it starts returning HTTP `429`. The circuit breaker opens, but there is no quota window to tell the system when to retry.

### Root Cause
The design ties pacing to declared quotas, but `429` is a runtime signal independent of configured quota metadata. Without an explicit retry policy, retry timing is undefined.

### Fix
**Design delta:** make `429` handling quota-independent and breaker-driven.

Rules:

- `429` opens the breaker immediately.
- Backoff for `429` is exponential per provider instance.
- If a `Retry-After` header is present, use it as a floor.
- When the cooldown expires, allow exactly one half-open probe.
- A successful probe closes the breaker and resets rate-limit strikes to zero.
- A probe that returns `429` reopens the breaker and doubles the backoff again, capped at `max_429_backoff_s`.

Recommended defaults:

- `base_429_backoff_s = 60`
- `max_429_backoff_s = 3600`

```python
def on_http_429(provider, headers, now):
    state = breaker_state(provider.name)
    retry_after_s = parse_retry_after(headers) or 0
    state.rate_limit_strikes += 1

    exp_backoff = CONFIG.routing.breaker.base_429_backoff_s * (
        2 ** (state.rate_limit_strikes - 1)
    )
    cooldown_s = min(
        CONFIG.routing.breaker.max_429_backoff_s,
        max(exp_backoff, retry_after_s),
    )

    state.state = "OPEN"
    state.open_reason = "429"
    state.open_until = now + timedelta(seconds=cooldown_s)


def maybe_half_open(provider, now):
    state = breaker_state(provider.name)
    if state.state == "OPEN" and now >= state.open_until:
        state.state = "HALF_OPEN"
        state.probe_in_flight = False


def on_probe_success(provider):
    state = breaker_state(provider.name)
    state.state = "CLOSED"
    state.rate_limit_strikes = 0
    state.server_error_strikes = 0
    state.open_until = None


def on_probe_429(provider, headers, now):
    on_http_429(provider, headers, now)
```

### Config Surface
- `routing.breaker.base_429_backoff_s` (int, default `60`)
- `routing.breaker.max_429_backoff_s` (int, default `3600`)
- `routing.breaker.half_open_max_probes` (int, default `1`)

### Interactions with Other Design Elements
- No quota declaration is needed; this works for undocumented rate limits.
- The provider stays in the config and remains eligible again after cooldown; it is not marked exhausted.
- Multiple instances of the same provider type maintain independent breaker state.

## 3. All Providers Circuit-Broken Simultaneously

### Scenario
Every non-exhausted provider is in breaker state `OPEN` at the same time.

### Root Cause
Normal routing removes open providers from the eligible set. If every provider is open, the scheduler has no candidate and the design needs an explicit global fallback rule.

### Fix
**Design delta:** define a global all-open policy that respects cooldowns instead of force-probing immediately.

Rules:

- Build the normal eligible set first.
- If the eligible set is empty:
  - Ignore exhausted providers.
  - Find the open provider with the smallest `open_until`.
  - If its cooldown has expired, move it to `HALF_OPEN` and try exactly one probe.
  - If no cooldown has expired yet, return a structured routing error instead of violating breaker policy.
- The error must include the soonest retry time and the provider chosen as the next recovery candidate.

```python
def choose_provider_or_error(providers, now):
    eligible = [p for p in providers if is_eligible(p, now)]
    if eligible:
        return sort_for_request(eligible)[0]

    cooling = [
        p for p in providers
        if not quota_is_exhausted(p.name) and breaker_state(p.name).state == "OPEN"
    ]
    if not cooling:
        raise RoutingError(code="no_active_providers")

    candidate = min(cooling, key=lambda p: breaker_state(p.name).open_until)
    candidate_state = breaker_state(candidate.name)

    if now >= candidate_state.open_until:
        candidate_state.state = "HALF_OPEN"
        candidate_state.probe_in_flight = False
        return candidate

    raise RoutingError(
        code="all_providers_cooling_down",
        retry_after_s=ceil((candidate_state.open_until - now).total_seconds()),
        recovery_candidate=candidate.name,
    )
```

### Config Surface
- `routing.breaker.return_all_open_error` (bool, default `true`)
- `routing.breaker.force_probe_on_global_exhaustion` (bool, default `false`)

### Interactions with Other Design Elements
- Exhausted providers remain skipped; they are not recovery candidates.
- Round-robin does not run until at least one provider becomes eligible again.
- This rule composes cleanly with the `429` exponential backoff policy from Section 2.

## 4. `conserve=true` Interaction with Failover

### Scenario
Provider `A` has `priority=1`, `conserve=true`, and is deferred because `pace_ratio >= 1.5`. Provider `B` with `priority=5` is tried first and returns fewer than 2 results. The question is whether routing should go back to deferred `A` immediately or continue to `C`.

### Root Cause
`conserve` is a soft demotion, not exhaustion and not breaker unavailability. The base sort alone does not say whether deferred providers are still part of the same failover pass.

### Fix
**Design delta:** split a request into two ordered passes: `primary` and `deferred`.

Rules:

- Providers skipped for exhaustion or open breaker are removed entirely.
- Providers deferred by `conserve` stay in a `deferred` list for the same request.
- Failover runs all `primary` candidates first.
- Only if no `primary` provider returns at least 2 results does the scheduler run the `deferred` list.
- The scheduler does **not** jump back to `A` immediately after `B` fails; it continues through the non-deferred pool first.

```python
def build_request_plan(providers, now):
    primary = []
    deferred = []

    for p in sorted(providers, key=sort_key_for_request):
        if quota_is_exhausted(p.name):
            continue
        if breaker_state(p.name).state == "OPEN":
            continue

        if p.conserve and compute_pace_ratio(quota_entry(p.name), now) >= 1.5:
            deferred.append(p)
        else:
            primary.append(p)

    return primary, deferred


def failover_search(query, providers, now):
    primary, deferred = build_request_plan(providers, now)

    for pool in (primary, deferred):
        for p in pool:
            outcome = execute_search(p, query)
            if outcome.success and len(outcome.results) >= 2:
                return outcome

    raise RoutingError(code="all_providers_insufficient_or_failed")
```

### Config Surface
- Existing per-provider `conserve` flag
- `routing.conserve_threshold` (float, default `1.5`)
- `routing.deferred_second_pass` (bool, default `true`)

### Interactions with Other Design Elements
- `conserve` remains a soft quota-protection mechanism, not a hard skip.
- Round-robin applies independently inside each pass because only actually attempted providers increment `call_counter`.
- Circuit-broken or exhausted providers never enter either pass.

## 5. Monthly Quota Conversion for Daily Budgeting

### Scenario
A provider such as Tavily has `1000/month`. The design needs a daily budget for observability or budget reporting. The ambiguity is whether this is `remaining / remaining_days`, whether the current day counts, and whether weekends should be weighted differently.

### Root Cause
A monthly quota is a calendar-window limit, but the routing formula is continuous. A derived daily budget must be specified separately or different implementations will produce different numbers.

### Fix
**Design delta:** define `daily_budget` as a reporting and optional alerting value, not as a routing sort key.

Rules:

- Routing continues to use cumulative `pace_ratio`; it does not use `daily_budget` for provider ordering.
- For a monthly quota, compute:
  - `remaining = max(limit - used, 0)`
  - `remaining_days = max(1, ceil(seconds_until_period_end / 86400))`
  - `daily_budget = remaining / remaining_days`
- Count the current partial day in `remaining_days`.
- Do not apply weekend weighting by default.
- If weighted calendars are needed later, make them explicit config, not implicit behavior.

```python
def compute_daily_budget(entry, now):
    if entry.period != "monthly":
        return None

    period_end = first_instant_of_next_month(now, entry.timezone)
    remaining = max(entry.limit - entry.used, 0)
    remaining_days = max(
        1,
        ceil((period_end - now).total_seconds() / 86400),
    )
    return remaining / remaining_days
```

Optional weighted extension:

```python
def compute_weighted_daily_budget(entry, now, weights):
    remaining = max(entry.limit - entry.used, 0)
    weighted_days = max(1.0, sum(weights[d.weekday()] for d in days_left(now)))
    return remaining / weighted_days
```

### Config Surface
- No new config is required for uniform daily budgeting.
- Optional future config:
  - `quota.calendar_weights` (mapping `0..6 -> float`)
  - `quota.timezone` (default provider/account timezone)

### Interactions with Other Design Elements
- `conserve` should still key off `pace_ratio`, not `daily_budget`.
- Exhaustion is unchanged.
- Status output can show `daily_budget` without affecting routing decisions.

## 6. Burst Scenario: 100 Searches Mid-Month Causes Conserve to Overreact

### Scenario
On day 15 of a monthly quota window, a user performs 100 searches. The provider's `pace_ratio` rises enough that `conserve=true` defers the provider, even though the user may still need its quality.

### Root Cause
A single threshold with no hysteresis can cause request-to-request flapping, and a soft demotion without a guaranteed fallback pass can feel like a hard exclusion.

### Fix
**Design delta:** add hysteresis to `conserve` and define that deferred providers still participate in the same request as a second pass.

Rules:

- Enter conserve mode when `pace_ratio >= conserve_enter_ratio`.
- Exit conserve mode only when `pace_ratio <= conserve_exit_ratio`.
- Require `conserve_exit_ratio < conserve_enter_ratio`.
- Use the two-pass failover plan from Section 4 so a conserved provider is still reachable within the same request after cheaper providers fail.

Recommended defaults:

- `conserve_enter_ratio = 1.5`
- `conserve_exit_ratio = 1.2`

```python
def update_conserve_state(provider, now):
    ratio = compute_pace_ratio(quota_entry(provider.name), now)
    state = conserve_state(provider.name)

    if state.active:
        state.active = ratio >= CONFIG.routing.conserve_exit_ratio
    else:
        state.active = ratio >= CONFIG.routing.conserve_enter_ratio

    return state.active
```

### Config Surface
- `routing.conserve_enter_ratio` (float, default `1.5`)
- `routing.conserve_exit_ratio` (float, default `1.2`)
- `routing.deferred_second_pass` (bool, default `true`)

### Interactions with Other Design Elements
- This reduces flapping without weakening exhaustion or breaker behavior.
- The denominator floor from Section 1 also dampens false spikes early in a quota window.
- Same-priority round-robin still works because deferred providers are sorted normally inside the deferred pass.

## 7. Priority Conflicts: Same Priority, One Provider Exhausted

### Scenario
Two providers share the same priority and participate in round-robin. One has a quota and becomes exhausted; the other is unlimited. The question is whether the exhausted provider remains in the round-robin pool.

### Root Cause
Round-robin fairness only makes sense over the eligible set. If exhausted providers remain in the pool, they will continue to win sort slots even though they cannot execute.

### Fix
Exhausted providers are removed from the eligible pool before sorting and do not participate in round-robin until their quota resets.

Rules:

- Evaluate exhaustion before sorting.
- If `used >= limit`, the provider is excluded from the current request.
- Its `call_counter` does not increment while excluded.
- On quota reset, reinsert it into the same-priority pool with `call_counter` normalized to the minimum active counter in that pool. This prevents a stale counter from unfairly dominating re-entry.

```python
def eligible_same_priority_pool(providers, priority):
    return [
        p for p in providers
        if p.priority == priority
        and not quota_is_exhausted(p.name)
        and breaker_state(p.name).state != "OPEN"
    ]


def on_quota_reset(provider, providers):
    active_peers = eligible_same_priority_pool(providers, provider.priority)
    if not active_peers:
        provider.call_counter = 0
    else:
        provider.call_counter = min(p.call_counter for p in active_peers)
```

### Config Surface
- No new required config
- Optional: `routing.rr_rejoin_policy` with values `min_counter` or `preserve`; default `min_counter`

### Interactions with Other Design Elements
- Exhaustion remains a hard skip, unlike `conserve`.
- A breaker-open provider is handled the same way for eligibility, but for a different reason.
- Multiple instances of the same backend follow the same rule independently.

## 8. Stale Results Detection for Time-Sensitive Queries Without Dates

### Scenario
A query is time-sensitive, but the provider does not return publication dates. The system needs to avoid returning obviously stale results without inventing freshness it cannot verify.

### Root Cause
Freshness cannot be reliably inferred from undated results. A hard stale/not-stale decision based only on snippets would create false confidence and unstable routing.

### Fix
**Design delta:** make freshness tri-state: `fresh`, `stale`, or `unknown`.

Rules:

- Only classify a result as `fresh` or `stale` when there is explicit evidence:
  - provider-supplied timestamp
  - parseable date in the URL
  - parseable date in the snippet/title
- Otherwise classify as `unknown`.
- For time-sensitive queries:
  - Prefer providers marked `freshness_capable=true`.
  - If a freshness-capable provider returns at least 2 `fresh` results, accept it.
  - If a freshness-capable provider returns fewer than 2 `fresh` results, continue failover.
  - Providers with only `unknown` freshness may still be used as fallback if no freshness-capable provider succeeds.

```python
def classify_freshness(result, now, window_hours):
    ts = (
        result.get("published_at")
        or parse_date_from_url(result.get("url"))
        or parse_date_from_text(result.get("title", ""))
        or parse_date_from_text(result.get("snippet", ""))
    )
    if ts is None:
        return "unknown"
    age_hours = (now - ts).total_seconds() / 3600
    return "fresh" if age_hours <= window_hours else "stale"


def is_time_sensitive_accept(provider, results, now, query_ctx):
    labels = [
        classify_freshness(r, now, query_ctx.freshness_window_hours)
        for r in results
    ]
    fresh_count = sum(1 for x in labels if x == "fresh")

    if fresh_count >= 2:
        return True
    if provider.freshness_capable:
        return False
    return query_ctx.allow_unknown_freshness_fallback
```

### Config Surface
- `provider.freshness_capable` (bool, default `false`)
- `routing.freshness_window_hours` (int, default query-class dependent)
- `routing.allow_unknown_freshness_fallback` (bool, default `true`)

### Interactions with Other Design Elements
- This does not change generic `<2 results` failover; it adds a stricter acceptance rule only for time-sensitive queries.
- Providers without dates are not incorrectly marked stale; they are treated as lower-confidence fallback.
- `conserve` and breaker logic are orthogonal to freshness classification.

## 9. Health Check Success but Search Returns `None`

### Scenario
A provider passes `health_check()` at startup, but at search time it returns `None`. The question is whether that counts toward the circuit breaker and how many such `None` results are needed to open it.

### Root Cause
`health_check()` proves only that the provider looked reachable at probe time. A runtime `None` is ambiguous unless the scheduler distinguishes transport failure from a low-quality but valid search response.

### Fix
**Design delta:** replace ambiguous `None` handling with explicit attempt classification.

Rules:

- A search-time `None` is treated as an execution failure, not as an insufficient-result success.
- Search outcomes are classified into:
  - `success`
  - `insufficient_results`
  - `http_429`
  - `http_5xx`
  - `transport_error`
  - `parse_error`
- Breaker updates:
  - `success` closes or stabilizes the breaker.
  - `insufficient_results` triggers failover but does **not** count as breaker failure.
  - `http_429`, `http_5xx`, `transport_error`, and `parse_error` count as breaker failures.
- For generic `None`/transport failures, open the breaker after 3 consecutive failures or failure rate greater than 60% over the last 5 attempts.

```python
def classify_attempt(result, exc=None, status_code=None):
    if status_code == 429:
        return "http_429"
    if status_code is not None and status_code >= 500:
        return "http_5xx"
    if exc is not None or result is None:
        return "transport_error"
    if len(result.results) < 2:
        return "insufficient_results"
    return "success"


def update_breaker(provider, outcome):
    if outcome == "success":
        breaker.record_success(provider.name)
    elif outcome == "http_429":
        breaker.record_429(provider.name)
    elif outcome in ("http_5xx", "transport_error", "parse_error"):
        breaker.record_standard_failure(provider.name)
    elif outcome == "insufficient_results":
        pass
```

### Config Surface
- `routing.breaker.window_size` (int, default `5`)
- `routing.breaker.consecutive_failure_threshold` (int, default `3`)
- `routing.breaker.failure_rate_threshold` (float, default `0.6`)

### Interactions with Other Design Elements
- A passing `health_check()` does not reset breaker state and does not exempt runtime failures.
- Failover on `<2 results` remains separate from breaker accounting.
- Provider instances keep independent breaker histories even if they share the same backend type.

## 10. Config Validation: Two Unlimited Providers at the Same Priority

### Scenario
Two unlimited providers have the same priority. The question is whether config loading should reject that as ambiguous or allow it and pick a deterministic first provider.

### Root Cause
Same-priority routing is valid under the design, but only if the initial tie-break is deterministic before `call_counter` diverges.

### Fix
This is **valid config**. It must not raise a validation error.

Rules:

- Same-priority providers form a round-robin group.
- Define a stable per-provider `rr_seed` from config order.
- Sort by `(effective_priority, call_counter, rr_seed)`.
- Increment `call_counter` only when a provider is actually attempted.
- If both providers start with `call_counter = 0`, the one appearing first in config order goes first on the first request, and the other goes first on the next request.

```python
def prepare_provider(entry, config_index):
    provider = build_provider(entry)
    provider.rr_seed = config_index
    provider.call_counter = 0
    return provider


def sort_key(provider, now):
    return (
        effective_priority(provider, now),
        provider.call_counter,
        provider.rr_seed,
    )


def on_attempt(provider):
    provider.call_counter += 1
```

### Config Surface
- No validation error for same-priority unlimited providers
- No new user-facing config is required
- `rr_seed` is internal state derived from config order

### Interactions with Other Design Elements
- If one same-priority provider becomes exhausted, breaker-open, or conserve-deferred, it simply leaves the active pool and round-robin continues over the remaining eligible members.
- Multiple instances of any backend type use the same deterministic rule.
- This keeps config permissive while preserving predictable request ordering.
