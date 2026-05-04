# Routing Algorithm v2: Priority + Quota Gates + Latency Budgets

Final design document for the pivot-web-search provider routing system.

**Status**: Design complete, ready for implementation.

**Replaces**: The existing tier-based tuple-sort routing in `routing.py`.

**Design philosophy**: Optimize for **latency first**, then quality, then cost. A CLI user cares about getting answers fast. Quota protection is a warning, not an invisible throttle.

---

## 1. Design Principles

1. **Latency first**: Per-provider timeouts and total search budget ensure the user never waits more than 10 seconds.
2. **User-declared priority** is the primary sort key. Lower number = tried first.
3. **Quota gates** prevent over-consumption. Exhausted providers are excluded. Usage warnings (not throttling) inform the user.
4. **Circuit breaker** protects against failing providers with fixed cooldown + Retry-After.
5. **Same priority = round-robin** via in-memory call counter per provider.
6. **Hedged requests**: Same-priority providers use parallel probing — start the second after a short delay, take the first good response.
7. **Failover on quality**: fewer than 2 unique results (or zero keyword overlap) triggers next-provider attempt.
8. **Single provider never errors**: always returns best-effort results.
9. **Network-down short-circuit**: 2 consecutive TCP failures → stop trying, return error immediately.
10. **Observability first**: every routing decision is logged with reason.

---

## 2. Industry Context

Configuration patterns drawn from:

| System | Key Pattern We Adopt | What We Skip |
|--------|---------------------|--------------|
| **LiteLLM** | `order` field for priority; `allowed_fails` + `cooldown_time` per deployment; fallbacks separate from routing strategy | Redis-backed TPM/RPM tracking (overkill for CLI plugin) |
| **Portkey** | `mode: "fallback"` with `on_status_codes: [429, 503]`; composable nesting (cluster-as-primary with fallback to another cluster) | Virtual keys, sticky sessions, enterprise rate limits |
| **OpenRouter** | Price-based load balancing as default; per-request provider preferences; unknown limits treated as unlimited | Quantization routing, ZDR enforcement, centralized capacity tracking |
| **Bifrost** | Zero-config champion — starts immediately, progressive disclosure of complexity | Web UI runtime config (we use YAML + status tool) |
| **Cloudflare AI Gateway** | Health-check based failover pools with automatic recovery | Primarily observability layer, no quality-based routing |

Our design is closest to LiteLLM's `order` + cooldown system, simplified for a single-process MCP server with file-based quota persistence. Our **unique differentiator** is quality-based failover (< 2 results triggers continuation) — no other routing system in the LLM/search space does this.

---

## 3. The Complete Algorithm

### 3.1 Sort Key

Each provider is scored as a tuple for sorting:

```
sort_key = (effective_priority, call_counter)
```

Where:
- `effective_priority` = declared `priority` (from config or smart defaults)
- `call_counter` = monotonically incrementing per-provider counter (in-memory), used to round-robin among same-priority providers

### 3.2 Provider States

```
ACTIVE       — available for routing (priority unchanged)
EXHAUSTED    — excluded from routing entirely (used >= limit)
CIRCUIT_OPEN — excluded until cooldown expires
```

Three states only. No CONSERVED state. No priority manipulation based on usage patterns.

### 3.3 Pseudocode

```python
def select_providers(providers: list[Provider]) -> list[ScoredProvider]:
    """Return ordered list of providers to attempt."""
    
    candidates = []
    
    for p in providers:
        if not p.enabled:
            continue
        if p.api_key_required and not p.has_api_key():
            continue
        
        # Gate 1: Exhausted check
        if is_exhausted(p):
            log(f"SKIP {p.name}: quota exhausted ({p.used}/{p.limit})")
            continue
        
        # Gate 2: Circuit breaker check
        breaker_state = circuit_breaker.get_state(p.name)
        if breaker_state == OPEN:
            log(f"SKIP {p.name}: circuit OPEN, {breaker.time_remaining(p.name):.0f}s remaining")
            continue
        
        candidates.append(ScoredProvider(
            provider=p,
            sort_key=(p.effective_priority, p.call_counter),
            breaker_state=breaker_state,
        ))
    
    # Sort by (effective_priority, call_counter)
    candidates.sort(key=lambda c: c.sort_key)
    
    return candidates


async def execute_search(query: str, max_results: int, **kwargs) -> SearchResult | FailureInfo:
    """Execute search with failover, latency budget, and hedged requests."""
    
    candidates = select_providers(registry.get_enabled())
    
    if not candidates:
        recovery = pick_recovery_candidate(registry.get_enabled())
        if recovery:
            candidates = [recovery]
        else:
            return FailureInfo(failures=[], reason="all providers unavailable")
    
    min_acceptable = min(2, max_results)
    best_so_far = None
    failures = []
    consecutive_tcp_failures = 0
    budget_deadline = time.monotonic() + TOTAL_BUDGET  # 10s
    
    # Group candidates by priority for hedging
    priority_groups = group_by(candidates, key=lambda c: c.sort_key[0])
    
    for priority, group in priority_groups.items():
        if time.monotonic() > budget_deadline:
            log(f"BUDGET_EXHAUSTED: {TOTAL_BUDGET}s elapsed, returning best_so_far")
            break
        
        remaining = budget_deadline - time.monotonic()
        
        if len(group) == 1:
            # Single provider at this priority — simple sequential
            result = await _try_provider(group[0], query, max_results, timeout=min(PER_PROVIDER_TIMEOUT, remaining), **kwargs)
        else:
            # Multiple same-priority — hedged request
            result = await _hedged_request(group, query, max_results, timeout=min(PER_PROVIDER_TIMEOUT, remaining), **kwargs)
        
        if result is not None:
            if isinstance(result, FailureInfo):
                failures.extend(result.failures)
                # Check network-down
                if result.reason == "tcp_failure":
                    consecutive_tcp_failures += 1
                    if consecutive_tcp_failures >= 2:
                        return FailureInfo(failures=failures, reason="network_unreachable")
                continue
            
            consecutive_tcp_failures = 0
            
            if quality_gate_passes(result.results, query, result.answer):
                log(f"SUCCESS {result.provider}: {len(result.results)} results")
                return result
            
            # Partial — keep as best_so_far but continue
            if best_so_far is None or len(result.results) > len(best_so_far.results):
                best_so_far = result
                log(f"PARTIAL {result.provider}: {len(result.results)} results, continuing")
    
    if best_so_far is not None:
        return best_so_far
    
    return FailureInfo(failures=failures)
```

---

## 4. Quota Management

### 4.1 Design: Simple Exhaustion + Percentage Warning

No pacing formulas, no conservation state machine, no hysteresis. The quota system has exactly two behaviors:

1. **Exhaustion gate**: `used >= limit` → provider excluded from routing entirely.
2. **Usage warning**: `used / limit >= 0.8` → log a warning (user-visible in status), but **do not change routing behavior**.

This is intentionally simple. The user declared a priority order. We respect it until the provider literally cannot serve more requests.

### 4.2 State Transitions

```
                used < limit
ACTIVE  ◄──────────────────── (period reset)
   │
   │    used >= limit
   ▼
EXHAUSTED
```

Two states only. No CONSERVED state. No deferral. No priority manipulation based on usage patterns.

### 4.3 Why No Pacing / Conservation

The v1 design had `pace_ratio`, hysteresis thresholds, and a CONSERVED state that demoted providers to `priority + 10000`. This created three problems:

1. **Invisible throttling**: Users couldn't understand why their preferred provider wasn't being used.
2. **Over-engineering for CLI**: A developer makes ~20-50 searches per day. Even aggressive usage rarely exhausts a 1000/month Tavily quota.
3. **Contradicts user intent**: If the user set Tavily at priority 10, they want Tavily first. Demoting it based on math they can't see violates trust.

**Instead**: If a user is concerned about quota, they can set `quota.limit` to a lower number (e.g., 30/day for a 500/month Gemini plan). Exhaustion is explicit and predictable.

### 4.4 Quota Tracking

Quota data persists to `~/.cache/pivot-web-search/quota.json`:

```json
{
  "tavily": {
    "used": 450,
    "limit": 1000,
    "period": "monthly",
    "month": "2024-02",
    "source": "api",
    "last_synced": "2024-02-15T10:30:00Z"
  },
  "gemini": {
    "used": 120,
    "limit": 500,
    "period": "daily",
    "day": "2024-02-15",
    "source": "config"
  }
}
```

### 4.5 Period Reset Logic

| Period | Reset Trigger |
|--------|--------------|
| `daily` | UTC date changes (or PT midnight for Gemini, configurable) |
| `monthly` | Calendar month changes |
| `rolling` | `now > reset_at` (from provider's response headers) |

On reset: `used = 0`. Provider becomes ACTIVE again immediately.

### 4.6 Warning Thresholds

| Usage % | Behavior |
|---------|----------|
| < 80% | Normal operation, no warnings |
| 80-99% | Log: `QUOTA_WARN {name}: {pct}% used ({used}/{limit})` |
| 100% | Exclude from routing: `SKIP {name}: quota exhausted ({used}/{limit})` |

Warnings appear in `WebSearchConfig status` output and debug logs. They do NOT change routing order.

### 4.7 Provider With No Quota Declaration

If a provider has no `quota` block:
- Treated as unlimited — never excluded for quota reasons
- If it returns 429: circuit breaker handles protection (see Section 5)
- Log: `WARNING: {name} returned 429 but has no quota configured. Consider adding quota declaration.`

---

## 5. Circuit Breaker

### 5.1 Parameters

```yaml
circuit_breaker:
  consecutive_threshold: 3   # consecutive failures to open
  cooldown: 60               # seconds, fixed cooldown duration
```

Two parameters. No backoff multiplier, no sliding window, no rate threshold. A provider is either working or it's not.

### 5.2 State Machine

```
          3 consecutive failures (or 1x 429)
CLOSED  ───────────────────────────────────► OPEN
   ▲                                           │
   │    probe success                          │ cooldown expires (60s, or Retry-After)
   │                                           ▼
   └────────────────────────────────────── HALF_OPEN
                                               │
                     probe failure              │
                          ▼                    │
                        OPEN ◄─────────────────┘
                    (same fixed cooldown)
```

Three states, no escalation. Every OPEN→HALF_OPEN transition uses the same fixed cooldown (60s default). No exponential backoff — if a provider is persistently broken, the circuit opens again immediately after the probe fails, which naturally limits retry rate to 1 attempt per 60s.

### 5.3 Error Classification

| Error Type | Action |
|-----------|--------|
| HTTP 429 (Rate Limited) | Open breaker immediately (1 failure = open). Use `Retry-After` header as cooldown if present. |
| HTTP 5xx (Server Error) | Record failure, open at 3 consecutive. |
| HTTP 401/403 (Auth) | Record failure + log warning about API key. |
| Timeout (per-provider) | Record failure, open at 3 consecutive. |
| Connection Error (TCP) | Record failure, open at 3 consecutive. Also feeds network-down detection (see Section 5.5). |
| HTTP 4xx (other) | Record failure, open at 3 consecutive. |
| Result count < 2 | Do NOT record as breaker failure (quality issue, not availability). |

### 5.4 429 with Retry-After

```python
def handle_429(provider_name: str, headers: dict):
    retry_after = parse_retry_after(headers)  # seconds
    if retry_after and retry_after > 0:
        cooldown = clamp(retry_after, 10, 600)  # respect server, but cap at 10min
    else:
        cooldown = DEFAULT_COOLDOWN  # 60s
    
    open_breaker(provider_name, cooldown=cooldown)
```

### 5.5 Network-Down Short-Circuit

If 2 consecutive providers fail with TCP connection errors (not HTTP errors — actual socket failures):

```python
consecutive_tcp_failures = 0

for provider in candidates:
    try:
        result = await provider.search(...)
    except ConnectionError:
        consecutive_tcp_failures += 1
        if consecutive_tcp_failures >= 2:
            log("NETWORK_DOWN: 2 consecutive TCP failures, aborting search")
            return FailureInfo(
                failures=failures,
                reason="network_unreachable",
                suggestions=["Check your internet connection", "Check proxy configuration"]
            )
        continue
    else:
        consecutive_tcp_failures = 0  # reset on any non-TCP outcome
```

This prevents the user from waiting 10+ seconds while the system sequentially tries providers that all fail to connect.

### 5.6 Reset

- **OPEN → HALF_OPEN**: After cooldown expires (checked lazily on next routing call)
- **HALF_OPEN → CLOSED**: On first successful search call
- **HALF_OPEN → OPEN**: On first failed probe (same 60s cooldown again)
- **Session restart**: All breaker state resets (in-memory only, not persisted)

### 5.7 Why No Exponential Backoff

Exponential backoff (60s → 120s → 240s → 600s) is designed for systems that make thousands of requests per minute. A CLI user makes ~5-20 searches per session. Fixed 60s cooldown means:
- After a transient failure: provider is back in 60s.
- After persistent failure: breaker opens → 60s → probe fails → opens again → 60s. Effective retry rate is 1/60s. With 4 providers, that's 1 probe per 60s — negligible load.
- Exponential backoff would lock out a recovered provider for 10 minutes for no benefit.

---

## 5B. Latency Strategy

### 5B.1 Design Goal

A CLI user expects an answer within ~2-3 seconds. The maximum acceptable wait is 10 seconds. The latency strategy ensures the system never blocks longer than this, regardless of how many providers are configured or how slow they are.

### 5B.2 Three Latency Controls

| Control | Default | Purpose |
|---------|---------|---------|
| **Per-provider timeout** | 5s | Any single provider.search() call is cancelled after this. |
| **Total search budget** | 10s | The entire failover chain (across all providers) must complete within this. |
| **Hedge delay** | 200ms | When multiple providers share the same priority, start the second request after this delay. |

### 5B.3 Per-Provider Timeout

Every `provider.search()` call is wrapped in `asyncio.wait_for(timeout=per_provider_timeout)`:

```python
try:
    result = await asyncio.wait_for(p.search(query, max_results, **kwargs), timeout=timeout)
except asyncio.TimeoutError:
    log(f"TIMEOUT {p.name}: exceeded {timeout:.1f}s")
    circuit_breaker.record_failure(p.name, error="timeout")
    failures.append({"provider": p.name, "error": f"timeout ({timeout:.1f}s)"})
    continue
```

The timeout decreases as the budget is consumed: `timeout = min(per_provider_timeout, budget_remaining)`.

Per-provider override is possible via the `timeout` field in provider config (e.g., `timeout: 15` for a slow LLM search provider).

### 5B.4 Total Budget

A monotonic deadline is set at the start of `execute_search()`:

```python
first_timeout = candidates[0].provider.timeout if candidates else PER_PROVIDER_TIMEOUT
effective_budget = max(TOTAL_BUDGET, first_timeout + 2)  # extend for slow primary
budget_deadline = time.monotonic() + effective_budget
```

Before each provider attempt, check `time.monotonic() > budget_deadline`. If exceeded, return `best_so_far` or `FailureInfo`.

**Budget extension rule**: If the first (highest-priority) candidate has a per-provider timeout greater than `TOTAL_BUDGET`, the budget expands to `timeout + 2s`. This accommodates the "unlimited LLM" profile where a user explicitly chose a slow provider as their primary. The +2s leaves room for one fast fallback attempt if the primary times out. The extension only applies to the first candidate's timeout — it doesn't cascade.

### 5B.5 Hedged Requests

When multiple providers share the same priority (e.g., Tavily and Serper both at priority 20), they are tried concurrently with staggered starts:

```python
async def _hedged_request(group: list[ScoredProvider], query, max_results, timeout, **kwargs):
    """Start providers with staggered delays, return first good result."""
    
    async def attempt(provider, delay):
        if delay > 0:
            await asyncio.sleep(delay / 1000)  # hedge_delay is in ms
        return await provider.search(query, max_results, **kwargs)
    
    # Start first immediately, second after hedge_delay, third after 2x hedge_delay, etc.
    tasks = []
    for i, scored in enumerate(group):
        tasks.append(asyncio.create_task(
            attempt(scored.provider, i * HEDGE_DELAY)
        ))
    
    # Return first result that passes quality gate, cancel the rest
    done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    
    for task in pending:
        task.cancel()
    
    # Evaluate completed results
    for task in done:
        result = task.result()
        if result is not None and quality_gate_passes(result.results, query, result.answer):
            return result
    
    # If first-completed didn't pass, wait for remaining
    # (simplified: in practice, evaluate all done tasks)
    return best_from(done) or None
```

**Why hedging instead of pure parallel**: Starting all requests simultaneously wastes quota. The first provider at a priority level usually responds within 200ms. The hedge delay means the second provider only fires if the first is slow — saving 1 API call in the common case while still protecting against tail latency.

### 5B.6 Interaction with Other Components

| Component | Latency Behavior |
|-----------|-----------------|
| Quality gate | Evaluated per-result, not part of timeout. Sub-microsecond. |
| Circuit breaker | Timeout counts as a failure (opens after 3 consecutive). |
| Quota | Charged only on actual response (not on timeout/cancel). |
| Super mode | Has its own parallel strategy (asyncio.gather). Latency budget still applies as outer deadline. |
| Network-down | TCP failures are fast (usually < 1s). The 2-failure short-circuit fires well within budget. |

### 5B.7 Typical Latency Scenarios

| Scenario | Expected Latency | Mechanism |
|----------|-----------------|-----------|
| First provider responds fast | ~1-2s | Direct return, no failover |
| First provider slow, second fast | ~1.2s | Hedge fires at 200ms, second returns at ~1s |
| First provider timeout (5s default) | ~5s | Timeout fires, failover to second |
| All providers slow | 10s (budget) | Budget exhausts, return best_so_far or error |
| Network down | ~2-3s | Two TCP failures, short-circuit |
| DDG-only config, DDG slow | ~5s | Single provider timeout, return whatever came back |
| Unlimited LLM (timeout=30) succeeds | ~15-25s | Budget extended to 32s, LLM returns |
| Unlimited LLM (timeout=30) fails | ~32s | LLM times out at 30s, DDG fallback at ~31s |

---

## 6. Configuration Schema

### 6.1 Full YAML Specification

```yaml
# config/providers.yaml

providers:
  - name: ddg                     # REQUIRED. Unique identifier. Used in logs, quota tracking, status.
    type: ddg                     # REQUIRED. Adapter type: ddg|tavily|brave|gemini|searxng|json_api|llm_search
    enabled: true                 # Optional. Default: true. Set false to exclude entirely.
    priority: 10                  # Optional. Default: see type_defaults. Lower = tried first.
    timeout: 5                    # Optional. Per-provider timeout in seconds. Default: 5.
    
    # Quota configuration (optional)
    quota:
      limit: 1000                 # Max calls per period. Required if quota block present.
      period: monthly             # daily | monthly | rolling. Default: monthly.
    
    # Provider-specific fields
    api_key_env: TAVILY_API_KEY   # Env var name for API key (type-specific)
    endpoint: "https://..."       # For searxng, json_api, llm_search
    # ... (all existing provider-specific fields remain unchanged)

# Optional: override global circuit breaker defaults
circuit_breaker:
  consecutive_threshold: 3        # Default: 3
  cooldown: 60                    # Default: 60 (seconds)

# Optional: override latency budget
latency:
  per_provider_timeout: 5         # Default: 5 (seconds per provider attempt)
  total_budget: 10                # Default: 10 (total seconds before giving up)
  hedge_delay: 200                # Default: 200 (ms before starting hedged request)
```

### 6.2 Validation Rules

| Field | Type | Constraints |
|-------|------|------------|
| `name` | string | Required. Unique across all providers. `[a-z0-9_-]+`, max 64 chars. |
| `type` | string | Required. Must be a registered adapter type. |
| `enabled` | bool | Optional. Default: true. |
| `priority` | int | Optional. Range: 1-9999. Default: per type_defaults. |
| `timeout` | int/float | Optional. Range: 1-30. Default: 5. Per-provider request timeout. |
| `quota.limit` | int | Required if `quota` present. Must be > 0. |
| `quota.period` | string | Optional. One of: `daily`, `monthly`, `rolling`. Default: `monthly`. |
| `api_key_env` | string | Optional. Must be a valid env var name `[A-Z0-9_]+`. |
| `circuit_breaker.consecutive_threshold` | int | Optional. Range: 1-10. Default: 3. |
| `circuit_breaker.cooldown` | int | Optional. Range: 10-600. Default: 60. |
| `latency.per_provider_timeout` | int/float | Optional. Range: 1-30. Default: 5. |
| `latency.total_budget` | int/float | Optional. Range: 3-60. Default: 10. |
| `latency.hedge_delay` | int | Optional. Range: 50-2000 (ms). Default: 200. |

### 6.3 Backward Compatibility

The existing `tier` field is deprecated but still recognized. If present without a `quota` block:
- `tier: free` → no quota, priority default 90 (or 10 if free-only)
- `tier: daily` → `quota: {period: daily, limit: <from env or 500>}`
- `tier: paid` → `quota: {period: monthly, limit: <from env or null>}`

The `tier` field is ignored if a `quota` block is explicitly provided.

---

## 7. Smart Default Priority Assignment

### 7.1 Design Principle

**Explicit priorities always win.** The smart defaults system only activates for providers that do NOT have a `priority` field in their config. If a user sets `priority: 25`, that value is used verbatim — no inference, no override.

The goal: a user who drops in API keys and restarts gets sensible routing without touching priority numbers.

### 7.2 Provider Quality Tiers (for Default Computation Only)

Every adapter type has an intrinsic quality tier — used solely for computing default priority when none is specified:

| Tier | Adapters | Default Priority | Rationale |
|------|----------|-----------------|-----------|
| **1 — Premium** | `llm_search`, `json_api` with `premium: true` | 10 | Highest quality, tried first |
| **2 — Paid** | `tavily`, `brave`, `gemini`, `json_api` (default) | 20 | Good quality, metered API |
| **3 — Self-hosted** | `searxng` | 30 | Free, requires infrastructure, quality varies |
| **4 — Free fallback** | `ddg` | 90 | Zero cost, lower quality, last resort |

**Exception**: If ALL enabled providers are tier 4 (free-only config), all collapse to priority 10 — no point demoting the only available option.

### 7.3 Default Priority Algorithm

```python
TIER_BASE_PRIORITY = {1: 10, 2: 20, 3: 30, 4: 90}
FREE_ONLY_PRIORITY = 10

def assign_default_priorities(providers: list[ProviderConfig]) -> None:
    """Compute effective_priority for providers without explicit priority."""
    needs_default = [p for p in providers if p.priority is None]
    if not needs_default:
        return

    # If only free-tier providers exist, don't demote them
    all_tiers = {get_tier(p) for p in providers if p.enabled}
    free_only = all_tiers == {4}

    for p in needs_default:
        tier = get_tier(p)
        p.effective_priority = FREE_ONLY_PRIORITY if free_only else TIER_BASE_PRIORITY[tier]

def get_tier(provider: ProviderConfig) -> int:
    if provider.type == "llm_search":
        return 1
    if provider.type == "json_api" and provider.config.get("premium"):
        return 1
    if provider.type in ("tavily", "brave", "gemini"):
        return 2
    if provider.type == "json_api":
        return 2
    if provider.type == "searxng":
        return 3
    if provider.type == "ddg":
        return 4
    return 2  # Unknown adapter — safe middle ground
```

### 7.4 Default Quota Assignments

When no `quota` block is specified, these defaults apply based on type:

| Type | Default Quota | Source Override |
|------|---------------|----------------|
| `ddg` | none (unlimited) | — |
| `searxng` | none (unlimited) | — |
| `tavily` | `{limit: 1000, period: monthly}` | `PIVOT_WEB_SEARCH_TAVILY_QUOTA` |
| `brave` | `{limit: 2000, period: monthly}` | `PIVOT_WEB_SEARCH_BRAVE_QUOTA` |
| `gemini` | `{limit: 500, period: daily}` | `PIVOT_WEB_SEARCH_GEMINI_QUOTA` |
| `json_api` | none | User must configure |
| `llm_search` | none | User must configure |

Providers with no quota declaration and no limit are treated as unlimited. If they return 429, the circuit breaker handles protection (see Section 5.3).

### 7.5 The `premium` Flag

For `json_api` adapters wrapping premium LLM-powered search (Perplexity, You.com), set `premium: true` to promote from tier 2 → tier 1:

```yaml
- name: perplexity-sonar
  type: json_api
  premium: true          # → tier 1, default priority 10
  endpoint: "https://api.perplexity.ai/search"
```

`llm_search` type is always tier 1 — no flag needed.

### 7.6 Profile Validation

| Profile | Config | Computed Priorities | Routing Order |
|---------|--------|--------------------:|---------------|
| Minimalist (DDG only) | `ddg` | ddg=10 | DDG (sole provider) |
| Standard paid | `tavily + brave + ddg` | tavily=20, brave=20, ddg=90 | Tavily/Brave (hedged) → DDG |
| Self-hoster | `searxng + ddg` | searxng=30, ddg=90 | SearXNG → DDG |
| Premium (limited) | `llm_search + tavily + ddg` | llm=10, tavily=20, ddg=90 | Premium → Tavily → DDG |
| Enterprise | `json_api(premium) + gemini + ddg` | json=10, gemini=20, ddg=90 | Custom → Gemini → DDG |
| Budget maximizer | `tavily(pri=20) + brave(pri=20) + ddg(pri=20)` | all=20 (explicit) | Round-robin all three |
| Unlimited LLM | `llm_search(no quota) + ddg` | llm=10, ddg=90 | LLM search (timeout=30) → DDG |

### 7.7 The "Unlimited LLM" Profile

A user with free/unlimited access to an LLM search provider (enterprise AI Core, free Perplexity tier, etc.). This profile is unique:

- **Highest quality provider + no quota concern** — every request should go to the LLM provider.
- **But slowest response time** — LLM search typically takes 10-30s.
- The user accepts the latency tradeoff for quality.

Config:
```yaml
providers:
  - name: ai-core-search
    type: llm_search
    endpoint: "https://ai-core.internal/v2/chat/completions"
    api_key_env: AI_CORE_TOKEN
    model: gpt-4o-search
    timeout: 30             # LLM search is slow — explicit override
    # No quota block → unlimited

  - name: ddg
    type: ddg
    # DDG as fallback only when LLM search fails
```

**Latency budget interaction**: When the highest-priority provider has `timeout > total_budget`, the budget is extended to accommodate it. Specifically:

```python
effective_budget = max(TOTAL_BUDGET, first_candidate.timeout + 2)
# If LLM search has timeout=30, budget becomes 32s for this request
```

This ensures the system doesn't prematurely kill a legitimately slow provider that the user explicitly chose as their primary. The +2s allows time for DDG fallback if the LLM provider times out.

**Why this is safe**: The user declared this provider as primary by giving it the highest priority. They are explicitly opting into the latency. The total budget is a protection against *unintended* waiting (sequential failover through many providers), not against a single provider the user chose.

### 7.7 Why This Fixes the v1 Tier Problem

The old tier-based system sorted by `(tier_rank, metric, priority)` where free=0, daily=1, paid=2. This meant DDG (free, tier_rank=0) **always** sorted before paid providers regardless of user intent.

The new system sorts by `(effective_priority, call_counter)` only. Default priorities naturally encode quality preference (premium=10 < paid=20 < self-hosted=30 < free=90), but users can override any assignment. No tier_rank gates prevent a premium provider from being tried first.

---

## 8. Multi-Instance Handling

### 8.1 Call Counter Semantics

Each provider instance has an independent `call_counter` (uint64, wraps at 2^64). The counter increments after each successful dispatch (result returned, regardless of result count).

### 8.2 Round-Robin Among Same Priority

```python
providers_at_priority_20 = [
    Provider("serper",     priority=20, call_counter=5),
    Provider("google-cse", priority=20, call_counter=3),
]

# Sorted by (priority, call_counter):
# → google-cse (20, 3), serper (20, 5)
# google-cse gets the next call, its counter becomes 4
```

This naturally distributes calls evenly among same-priority providers without explicit tracking of "whose turn is it."

### 8.3 Failover Within Same Priority

When a same-priority provider fails quality check (< 2 results), the algorithm continues to the next entry in the sorted list. If `google-cse` returns 1 result, `serper` is tried next.

### 8.4 Call Counter: In-Memory Only

The `call_counter` is **not** persisted to disk. It starts at 0 on each session start.

Why: Round-robin fairness across sessions is not valuable for a CLI tool. A user's session typically lasts 30-60 minutes. Persisting the counter adds file I/O on every search call for negligible benefit. If a provider was used more last session, that doesn't mean it should be deprioritized this session.

On session start, all providers at the same priority begin with `call_counter = 0`. The first request is dispatched to whichever sorts first (alphabetical, or insertion order). After that, round-robin naturally distributes.

---

## 9. Edge Cases

### 9.1 Single Provider (No Failover Possible)

When only one provider is available (either configured or all others excluded):
- Execute the search
- Return whatever results come back, even if < 2
- NEVER return an error if the provider returned any results at all
- Only return `FailureInfo` if the provider itself failed (exception, None response)

```python
if len(candidates) == 1:
    min_acceptable = 0  # accept any results from sole provider
```

### 9.2 All Providers Exhausted

When every provider is either exhausted, circuit-open, or disabled:

1. Attempt the **all-open fallback**: find the provider whose circuit breaker cooldown is closest to expiring, force it to HALF_OPEN
2. If that provider is also quota-exhausted, check if any exhausted provider has a reset time within 60 seconds
3. If no recovery possible, return:

```json
{
  "error": "All providers unavailable",
  "reason": "all_exhausted_or_circuit_open",
  "provider_states": {
    "ddg": {"state": "CIRCUIT_OPEN", "recovers_in": "45s"},
    "tavily": {"state": "EXHAUSTED", "used": 1000, "limit": 1000, "resets": "2024-02-15T00:00:00Z"},
    "brave": {"state": "EXHAUSTED", "used": 2000, "limit": 2000}
  },
  "suggestions": ["Wait for circuit breaker cooldown", "Add another provider", "Increase quota limits"]
}
```

### 9.3 Provider Returns Stale Results (Time-Sensitive Query)

This is a **quality** issue, not a routing issue. The routing layer does not inspect result freshness. Instead:

- The `_apply_smart_defaults()` function already sets `timelimit` for time-sensitive queries
- Each provider adapter is responsible for passing time filters to its backend
- If a provider does not support time filtering (e.g., DDG without timelimit support for certain regions), that is an adapter limitation, not a routing concern
- The quality gate (< 2 results) handles the case where time filtering makes a provider return nothing

**No special routing logic needed.** Providers that cannot honor recency filters will naturally return fewer relevant results, triggering failover via the existing quality gate.

### 9.4 Same Priority, Different Quality Providers

When users set the same priority for providers of different quality (e.g., DDG and Tavily both at priority 20):

- Round-robin distributes calls evenly via `call_counter`
- Both providers get equal opportunity
- The quality gate ensures that if one returns poor results, the other is tried
- This is a valid user choice: they are expressing "I consider these equivalent"

**The system respects user intent.** We do NOT second-guess priority assignments. If the user wants DDG tried as often as Tavily, that is their choice.

### 9.5 Provider With No Quota Declaration Gets Rate-Limited

```python
# Provider has no quota block, but returns 429
def handle_unexpected_rate_limit(provider, error):
    # 1. Circuit breaker handles immediate protection
    circuit_breaker.record_failure(provider.name, error=error)
    
    # 2. Log a diagnostic recommendation
    log(f"WARNING: {provider.name} returned 429 but has no quota configured. "
        f"Consider adding quota declaration to prevent over-use.")
    
    # 3. If Retry-After header present, use it for cooldown
    # 4. Provider remains in rotation after cooldown (no permanent exclusion)
```

### 9.6 All Providers Return < 2 Results

If every provider returns at least something but fewer than `min_acceptable`:
- `best_so_far` captures the result with the most items
- After exhausting all candidates, `best_so_far` is returned (not an error)
- The user gets *something* rather than nothing

---

## 10. Logging / Observability

### 10.1 Log Events

Every routing decision produces a structured log line via `log()`:

| Event | Format | Example |
|-------|--------|---------|
| Provider selected | `ROUTE {name} pri={pri}` | `ROUTE tavily pri=20` |
| Provider skipped (exhausted) | `SKIP {name}: quota exhausted ({used}/{limit})` | `SKIP tavily: quota exhausted (1000/1000)` |
| Provider skipped (breaker) | `SKIP {name}: circuit OPEN, {secs}s remaining` | `SKIP brave: circuit OPEN, 45s remaining` |
| Search success | `SUCCESS {name}: {count} results` | `SUCCESS ddg: 5 results` |
| Partial results | `PARTIAL {name}: {count}/{min} results, continuing` | `PARTIAL ddg: 1/2 results, continuing` |
| Search failure | `FAIL {name}: {error}` | `FAIL brave: HTTP 429` |
| Timeout | `TIMEOUT {name}: exceeded {secs}s` | `TIMEOUT gemini: exceeded 5.0s` |
| Budget exhausted | `BUDGET_EXHAUSTED: {secs}s elapsed, returning best_so_far` | `BUDGET_EXHAUSTED: 10.0s elapsed, returning best_so_far` |
| Hedge started | `HEDGE {name}: starting after {ms}ms delay` | `HEDGE serper: starting after 200ms delay` |
| Network down | `NETWORK_DOWN: {n} consecutive TCP failures, aborting` | `NETWORK_DOWN: 2 consecutive TCP failures, aborting` |
| Breaker state change | `BREAKER {name}: {old} -> {new} (reason)` | `BREAKER brave: CLOSED -> OPEN (3 consecutive failures)` |
| Recovery probe | `PROBE {name}: forced HALF_OPEN (all-open fallback)` | `PROBE ddg: forced HALF_OPEN (all-open fallback)` |
| Quota warning | `QUOTA_WARN {name}: {pct}% used ({used}/{limit})` | `QUOTA_WARN tavily: 85% used (850/1000)` |
| Unexpected 429 | `WARNING: {name} returned 429 but has no quota configured` | (see 9.5) |

### 10.2 Per-Request Summary

At the end of each search dispatch, log a single summary line:

```
ROUTING query="{query}" tried=[ddg,tavily] selected=tavily results=5 elapsed=1.2s
```

### 10.3 What Is NOT Logged

- API keys or auth tokens (never)
- Full query text beyond first 50 chars (privacy)
- Response body content
- Per-result URLs (too verbose for routing layer)

---

## 11. WebSearchConfig Status Output

### 11.1 Status Response Schema

When the user calls `WebSearchConfig(action="status")`:

```json
{
  "action": "status",
  "routing": {
    "algorithm": "priority_with_latency_budget",
    "total_providers": 4,
    "active": 3,
    "exhausted": 1,
    "circuit_open": 0
  },
  "latency": {
    "per_provider_timeout": 5,
    "total_budget": 10,
    "hedge_delay_ms": 200
  },
  "providers": [
    {
      "name": "ddg",
      "type": "ddg",
      "priority": 90,
      "state": "ACTIVE",
      "enabled": true,
      "available": true,
      "quota": null,
      "breaker": "CLOSED",
      "call_counter": 42
    },
    {
      "name": "tavily",
      "type": "tavily",
      "priority": 20,
      "state": "ACTIVE",
      "enabled": true,
      "available": true,
      "quota": {
        "used": 450,
        "limit": 1000,
        "period": "monthly",
        "usage_pct": 45.0,
        "source": "api"
      },
      "breaker": "CLOSED",
      "call_counter": 38
    },
    {
      "name": "brave",
      "type": "brave",
      "priority": 20,
      "state": "EXHAUSTED",
      "enabled": true,
      "available": false,
      "quota": {
        "used": 2000,
        "limit": 2000,
        "period": "rolling",
        "usage_pct": 100.0,
        "resets_at": "2024-03-01T00:00:00Z",
        "source": "header"
      },
      "breaker": "CLOSED",
      "call_counter": 35
    },
    {
      "name": "gemini",
      "type": "gemini",
      "priority": 40,
      "state": "ACTIVE",
      "enabled": true,
      "available": true,
      "quota": {
        "used": 120,
        "limit": 500,
        "period": "daily",
        "usage_pct": 24.0,
        "resets_at": "PT midnight",
        "source": "config"
      },
      "breaker": "CLOSED",
      "call_counter": 15
    }
  ],
  "routing_order": ["tavily", "gemini", "ddg"],
  "config_sources": {
    "providers": {"source": "yaml", "path": "/path/to/config/providers.yaml"},
    "proxies": {"source": "yaml", "path": "/path/to/config/proxies.yaml"},
    "fetch": {"source": "yaml", "path": "/path/to/config/fetch.yaml"}
  }
}
```

### 11.2 Key Fields Explained

| Field | Purpose |
|-------|---------|
| `routing.active` | Number of providers available for routing (not exhausted, not circuit-open) |
| `state` | Human-readable: ACTIVE, EXHAUSTED, CIRCUIT_OPEN, DISABLED |
| `quota.usage_pct` | Percentage of quota consumed (for warning display) |
| `routing_order` | The actual order providers would be tried right now (excluding exhausted/open) |
| `call_counter` | Total calls dispatched to this provider this session (for round-robin) |

---

## 12. Migration Path

### 12.1 What Changes

| Current (v1) | New (v2) |
|-------------|----------|
| `tier` field determines sort rank | `priority` field determines sort order directly |
| Three-level tier rank (0, 1, 2) | Flat numeric priority (1-9999) |
| Usage-pct as secondary metric | `call_counter` as tiebreaker (in-memory) |
| Pacing pressure for paid tier only | Simple exhaustion gate (used >= limit → excluded) |
| High-water demotion (special case) | Removed — no invisible throttling |
| News demotion for DDG (special case) | Removed (user controls priority) |
| Fixed 120s cooldown | Fixed 60s cooldown + Retry-After respect |
| Single failure threshold | Per-error-type handling (429 = immediate open) |
| No timeouts | Per-provider 5s timeout + 10s total budget |
| Sequential-only failover | Hedged requests for same-priority providers |

### 12.2 Backward Compatibility

Existing `config/providers.yaml` files continue to work:
- `priority` field already exists and is used
- `tier` field is mapped to `quota` defaults if no `quota` block present
- No configuration changes required for basic operation
- Users can incrementally adopt `quota` blocks
- The `conserve` field is deprecated and ignored (no-op)

### 12.3 Implementation Order

1. Add `quota` schema to provider config parsing (backward-compat with `tier`)
2. Replace `route_providers()` with `select_providers()` + priority sort
3. Add `call_counter` (in-memory only, no persistence)
4. Add per-provider timeout wrapping
5. Add total budget deadline
6. Implement hedged requests for same-priority groups
7. Simplify circuit breaker (remove backoff, add network-down detection)
8. Add quality gate with keyword overlap
9. Update `WebSearchConfig` status output
10. Update logging to new event format
11. Remove `TIER_RANK`, `HIGH_WATER_*`, `NEWS_DDG_*`, `CONSERVE_*` constants
12. Update tests

---

## 13. Configuration Examples

### 13.1 Minimal (Zero-Config — Just API Keys)

```yaml
# User just sets env vars: TAVILY_API_KEY, BRAVE_API_KEY
# Smart defaults compute: tavily=20, brave=20, ddg=90
providers:
  - name: tavily
    type: tavily
    api_key_env: TAVILY_API_KEY

  - name: brave
    type: brave
    api_key_env: BRAVE_API_KEY

  - name: ddg
    type: ddg
```

Routing: Tavily/Brave round-robin (both priority 20), DDG as fallback (priority 90). Type defaults provide quota limits.

### 13.2 Power User (Explicit Quotas, Multiple Same-Priority)

```yaml
providers:
  - name: sonar-pro
    type: llm_search
    # No explicit priority → default 10 (tier: premium)
    endpoint: "https://api.perplexity.ai/chat/completions"
    api_key_env: PERPLEXITY_API_KEY
    api_format: chat_completions
    model: sonar-pro
    timeout: 15           # LLM search is slower
    quota:
      limit: 50
      period: daily

  - name: tavily
    type: tavily
    # No explicit priority → default 20 (tier: paid)
    api_key_env: TAVILY_API_KEY
    quota:
      limit: 1000
      period: monthly

  - name: serper
    type: json_api
    priority: 20          # Explicit — same as Tavily: round-robin between them
    api_key_env: SERPER_API_KEY
    endpoint: "https://google.serper.dev/search"
    quota:
      limit: 2500
      period: monthly

  - name: searxng-local
    type: searxng
    # No explicit priority → default 30 (tier: self-hosted)
    endpoint: "http://localhost:8888/search"

  - name: ddg
    type: ddg
    # No explicit priority → default 90 (tier: fallback)

circuit_breaker:
  cooldown: 30              # Faster recovery for local dev
```

Routing: sonar-pro(10) → tavily/serper(20, round-robin) → searxng(30) → ddg(90).

### 13.3 Single Provider (No Failover)

```yaml
providers:
  - name: tavily
    type: tavily
    priority: 10
    api_key_env: TAVILY_API_KEY
    quota:
      limit: 1000
      period: monthly
```

Routing behavior: always uses Tavily. Returns whatever Tavily returns (even 0 results as empty, or error as FailureInfo). Never errors with "all providers failed" if Tavily returned partial results.

### 13.4 Budget Maximizer (Even Distribution)

```yaml
# User wants all providers to share load equally — set same priority for all
providers:
  - name: tavily
    type: tavily
    priority: 20
    api_key_env: TAVILY_API_KEY
    quota:
      limit: 1000
      period: monthly

  - name: brave
    type: brave
    priority: 20
    api_key_env: BRAVE_API_KEY
    quota:
      limit: 2000
      period: monthly

  - name: gemini
    type: gemini
    priority: 20
    api_key_env: GEMINI_SEARCH_API_KEY
    quota:
      limit: 500
      period: daily

  - name: ddg
    type: ddg
    priority: 20          # Same as others — participates in round-robin
```

Routing: All four providers at priority 20. `call_counter` distributes evenly via round-robin. Hedged requests mean that if the first provider in the group is slow (>200ms), a second fires concurrently. When any provider hits its quota limit, it's excluded — others continue round-robin. DDG never exhausts, so it naturally absorbs load when paid providers are excluded.

### 13.5 v1 Migration (Explicit Priorities Unchanged)

```yaml
# Existing config from v1 users — works identically in v2
providers:
  - name: ddg
    type: ddg
    priority: 10           # Explicit: honored as-is

  - name: tavily
    type: tavily
    priority: 20           # Explicit: honored as-is
    api_key_env: TAVILY_API_KEY

  - name: brave
    type: brave
    priority: 30           # Explicit: honored as-is
    api_key_env: BRAVE_API_KEY

  - name: gemini
    type: gemini
    priority: 40           # Explicit: honored as-is
    api_key_env: GEMINI_SEARCH_API_KEY
```

Routing: DDG(10) → Tavily(20) → Brave(30) → Gemini(40). Smart defaults never activate because all priorities are explicit. This user's existing behavior is preserved exactly.

---

## 14. Data Structures (Implementation Reference)

### 14.1 Provider Config (Parsed)

```python
@dataclass
class ProviderConfig:
    name: str
    type: str
    enabled: bool = True
    priority: int | None = None   # None = use smart defaults
    timeout: float = 5.0          # per-provider timeout in seconds
    api_key_env: str | None = None
    quota: QuotaConfig | None = None
    # ... other provider-specific fields in self.config dict

@dataclass
class QuotaConfig:
    limit: int
    period: Literal["daily", "monthly", "rolling"] = "monthly"
```

### 14.2 Scored Provider (Internal to Router)

```python
@dataclass
class ScoredProvider:
    provider: SearchProvider
    sort_key: tuple[int, int]      # (effective_priority, call_counter)
    breaker_state: BreakerState
```

### 14.3 Circuit Breaker Entry

```python
@dataclass
class BreakerEntry:
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    cooldown: float = 60.0         # current cooldown (60s default, or Retry-After)
```

### 14.4 Quota File Format

```json
{
  "tavily": {
    "used": 450,
    "limit": 1000,
    "period": "monthly",
    "month": "2024-02",
    "source": "api",
    "last_synced": "2024-02-15T10:30:00Z"
  },
  "gemini": {
    "used": 120,
    "limit": 500,
    "period": "daily",
    "day": "2024-02-15",
    "source": "config"
  },
  "brave": {
    "used": 2000,
    "limit": 2000,
    "period": "rolling",
    "reset_at": "2024-03-01T00:00:00Z",
    "last_synced": "2024-02-15T10:30:00Z",
    "source": "header"
  }
}
```

---

## 15. Testing Strategy

### 15.1 Unit Tests (Offline)

| Test Area | Cases |
|-----------|-------|
| Sort order | Priority ordering, call_counter tiebreak |
| Smart defaults | Tier assignment, free-only collapse, explicit priority override, `premium` flag |
| Quota states | ACTIVE/EXHAUSTED transitions, period reset, warning threshold |
| Circuit breaker | Open/close/half-open transitions, fixed cooldown, 429 immediate-open, Retry-After |
| Latency | Per-provider timeout cancellation, total budget deadline, hedged request mechanics |
| Network-down | TCP failure counting, short-circuit after 2 consecutive |
| Multi-instance | Round-robin fairness (in-memory counter) |
| Quality gate | Keyword overlap, answer-field pass, count threshold |
| Edge cases | Single provider, all exhausted, no quota declaration + 429 |
| Backward compat | `tier` field mapping to quota defaults, v1 explicit priorities unchanged |
| Config validation | Invalid priority range, missing required fields, duplicate names |
| Diagnostic notes | Suboptimal config detection, priority ordering warnings |

### 15.2 Integration Tests (Live)

| Test Area | Cases |
|-----------|-------|
| Full failover | DDG rate-limit → Tavily success |
| Quality gate | Provider returns 1 result → failover to next |
| Timeout handling | Slow provider times out → next provider wins |
| Hedging | Same-priority providers, first slow → hedge fires |
| Recovery | All-open fallback finds closest-to-recovery provider |

---

## 16. Configuration UX and First-Run Experience

### 16.1 Zero-Config Progressive Disclosure

The system follows the Bifrost/OpenRouter pattern: works immediately with zero config, reveals complexity only when needed.

| User Action | System Behavior |
|-------------|----------------|
| Install plugin, no API keys | DDG enabled at priority 10, immediate search |
| Add `TAVILY_API_KEY` env var | Tavily auto-detected at priority 20, DDG becomes fallback (90) |
| Add multiple API keys | All detected providers get tier-based defaults, round-robin within same tier |
| Edit `config/providers.yaml` | Explicit priorities override all defaults |

### 16.2 First-Run Startup Log

On every startup (and config reload), the router logs its computed routing order:

```
[pivot-web-search] Provider routing order:
  1. tavily      priority=20  (source: default, tier: paid)     quota: 12/1000 monthly
  2. brave       priority=20  (source: default, tier: paid)     quota: 8/2000 monthly
  3. ddg         priority=90  (source: default, tier: fallback) quota: unlimited
```

This confirms the routing order without requiring the user to run `WebSearchConfig status`.

### 16.3 WebSearchConfig Diagnostic Notes

The `status` response includes a `notes` array with actionable suggestions:

| Condition | Note |
|-----------|------|
| All providers are free-tier only | `"Only free providers configured. Add a paid provider (Tavily/Brave) for better result quality."` |
| Paid provider priority > DDG priority | `"Provider 'tavily' (priority=95) will be tried AFTER 'ddg' (priority=10). Likely unintentional."` |
| Explicit priority higher than same-tier default | `"Explicit 'tavily' (50) will be tried after auto-assigned 'brave' (20). Set brave's priority explicitly if this is unintended."` |
| Provider circuit-broken | `"'brave' is circuit-broken (reopens in 45s). Routing to next available."` |
| Provider approaching quota | `"'tavily' at 85% quota (850/1000 monthly). Consider adding another provider."` |
| No quota declared but getting 429s | `"'perplexity' returned 429 but has no quota configured. Consider adding quota declaration."` |
| Free-only collapse changed on reload | `"Routing order changed: DDG demoted from primary (10) to fallback (90) — new paid provider detected."` |

### 16.4 Priority Source Transparency

Every provider in the status output shows where its priority came from:

```json
{
  "name": "tavily",
  "effective_priority": 20,
  "priority_source": "default (tier: paid)",
  "state": "ACTIVE"
}
```

Values for `priority_source`:
- `"explicit"` — from `priority:` field in config
- `"default (tier: premium)"` — computed, tier 1
- `"default (tier: paid)"` — computed, tier 2
- `"default (tier: self-hosted)"` — computed, tier 3
- `"default (tier: fallback)"` — computed, tier 4

### 16.5 Migration Path (v1 → v2)

**Zero breaking changes.** The transition is transparent:

| v1 Config | v2 Behavior |
|-----------|-------------|
| Has explicit `priority: 10/20/30/40` | Used verbatim, smart defaults never activate |
| Has `tier: free/daily/paid` but no `priority:` | `tier` mapped to quota defaults; priority computed from type |
| Has both `tier:` and `priority:` | `priority` wins for routing, `tier` provides quota hint |
| Has `quota:` block | Used as-is, overrides tier-based inference |

The `tier` field is deprecated but recognized. It provides backward-compatible quota inference when no `quota:` block is present.

---

## 17. Industry Patterns Adopted

Based on analysis of LiteLLM, Portkey, OpenRouter, Cloudflare AI Gateway, and Bifrost (2025-2026):

| Pattern | Source | How We Apply It |
|---------|--------|----------------|
| Fallbacks separate from load-balance | LiteLLM | Priority ordering is the fallback chain; `call_counter` provides load-balance within same priority |
| Unknown limit = unlimited, react to 429 | Portkey, LiteLLM | Providers without `quota` block treated as unlimited; 429 opens circuit breaker |
| Cheapest-first as default sort | OpenRouter | Smart defaults place free providers last only when paid alternatives exist |
| Cooldown on failure | LiteLLM (60s) | Exponential backoff 60s-600s with 429 as immediate trigger |
| Per-request provider override | OpenRouter | Existing `provider=` parameter already does this |
| Zero-config + progressive disclosure | Bifrost, OpenRouter | Works with DDG alone; adding keys activates providers automatically |
| Quality threshold failover | **Unique to us** | No other search router does quality-based continuation (< 2 results) |

**Our differentiator**: We are the only search routing system that combines quality-based failover (keyword overlap + result count), latency-budgeted hedging, and heterogeneous provider support. LLM gateways route between providers serving the same model; we route between fundamentally different search backends.

---

## 18. Summary of Decisions

1. **Kill tiers as routing rank**: No more `free/daily/paid` tier_rank system. `effective_priority` is the sole sort key.
2. **Smart defaults by quality tier**: Premium=10, Paid=20, Self-hosted=30, Free=90. Only when no explicit priority.
3. **Free-only collapse**: If DDG is the only provider, it gets priority 10 (not 90).
4. **Kill invisible throttling**: No pace_ratio, no conservation, no hysteresis. Exhaustion at 100% is the only quota gate.
5. **Kill news demotion**: DDG priority is user-configured. Special-casing removed.
6. **Add call_counter (in-memory)**: Enables true round-robin at same priority. Not persisted.
7. **Fixed 60s circuit breaker cooldown**: No exponential backoff. Retry-After from 429s is respected.
8. **429 = immediate open**: Rate limits are treated as breaker events, not regular failures.
9. **Per-provider timeout (5s)**: No single provider can block the user for more than 5 seconds.
10. **Total search budget (10s)**: The entire failover chain is capped regardless of provider count.
11. **Hedged requests**: Same-priority providers fire concurrently with staggered starts (200ms delay).
12. **Network-down short-circuit**: 2 consecutive TCP failures = stop immediately, don't waste time.
13. **Single provider = best effort**: Never error if we got *any* results.
14. **Quality gate with keyword overlap**: Catches irrelevant results, not just low count.
15. **Status shows routing_order + priority_source**: User can verify computed behavior matches intent.
16. **`premium` flag for json_api**: Promotes custom endpoints to tier 1 when warranted.

---

## 19. Quality Gate Design

### 19.1 Current Gate (v1)

```python
min_acceptable = min(2, max_results)
if len(results) < min_acceptable:
    continue_to_next_provider()
```

Simple, zero false positives, but misses cases where a provider returns 5 irrelevant results.

### 19.2 Enhanced Quality Gate (v2)

Based on analysis of 7 real failure scenarios and SearXNG/meta-search research:

**Signals evaluated and their verdict:**

| Signal | Include? | Rationale |
|--------|----------|-----------|
| Result count (post-dedup) | **Yes** (existing) | Core gate, proven reliable |
| Answer field presence | **Yes** (new) | LLM providers return answers without traditional results — valid response |
| Keyword overlap | **Yes** (new) | Catches irrelevant results (DDG returning "Debussy" for "Claude Code hooks") |
| Domain diversity | **No** | Too many false positives (StackOverflow for code queries, NIH for medical) |
| Snippet fill rate | **No** | Format difference, not quality — json_api/SearXNG may legitimately omit snippets |
| Cross-provider consensus | **No** | Requires extra network calls; incompatible with failover-by-design |
| Freshness | **No** | Provider capability issue, not quality gate — handled by routing (recency filters) |

### 19.3 The Minimum Viable Quality Gate

```python
def quality_gate_passes(results: list[dict], query: str, answer: str | None) -> bool:
    """Decide whether results are acceptable or failover should continue.
    
    Returns True = use these results. False = try next provider.
    """
    # Gate 0: LLM answer is a valid response regardless of result count
    if answer and len(answer) > 20:
        return True
    
    # Gate 1: Post-dedup count (existing behavior)
    unique_urls = {r.get("url") for r in results if r.get("url")}
    if len(unique_urls) < 2:
        return False
    
    # Gate 2: Minimal keyword overlap (new)
    # At least 1 query term must appear in at least 1 result title/snippet
    query_terms = _extract_significant_terms(query)
    if query_terms:
        for r in results:
            text = (r.get("title", "") + " " + r.get("snippet", "")).lower()
            if any(term in text for term in query_terms):
                return True  # found at least one relevant result
        return False  # zero results match any query term
    
    # No significant query terms extracted (e.g., single stopword) — pass
    return True


def _extract_significant_terms(query: str) -> list[str]:
    """Extract non-stopword terms from query, longest first.
    
    Returns lowercase terms. Skips common stopwords and single-char terms.
    """
    STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "in", "on",
                 "at", "to", "for", "of", "with", "and", "or", "not", "how",
                 "what", "when", "where", "why", "who", "which", "do", "does"}
    terms = [w.lower() for w in query.split() if len(w) > 1 and w.lower() not in STOPWORDS]
    return sorted(terms, key=len, reverse=True)[:5]  # top 5 longest terms
```

### 19.4 Scenario Validation

| Scenario | Gate Result | Correct? |
|----------|:-----------:|:--------:|
| DDG returns 5 StackOverflow results for "python asyncio timeout" | PASS (keyword "asyncio"/"timeout" in titles) | Yes |
| json_api returns 5 results with empty snippets | PASS (count >= 2, keyword check on titles) | Yes |
| All providers return 0-2 for obscure query | FAIL → failover exhausts naturally → best_so_far returned | Yes |
| DDG returns "Debussy" results for "Claude Code hooks" | FAIL (no result contains "Claude", "Code", "hooks") | Yes |
| Time-sensitive query with stale results | PASS (routing layer handles recency, not quality gate) | Yes |
| 10 results but 8 duplicate URLs → 2 unique | FAIL (post-dedup count < 2) | Yes |
| Perplexity returns answer + 0 results | PASS (answer field present) | Yes |

### 19.5 Interaction with Routing System

| Component | Quality Gate Behavior |
|-----------|---------------------|
| Circuit breaker | Quality failures do NOT open the breaker (not a transport error) |
| call_counter | YES increment — provider was called, quota consumed |
| Timeout | Quality gate runs after response, not subject to timeout |
| Logging | `QUALITY_FAIL {provider}: reason={reason}` |
| best_so_far | If gate fails but results > 0, compare with best_so_far by count |

### 19.6 Performance

- O(n) over results (typically 5-10 items)
- `_extract_significant_terms`: O(|query|), cached per-request
- Keyword check: substring search, sub-microsecond for typical result sets
- Zero network calls, zero allocations beyond a few string comparisons

---

### 19.7 SearXNG Scoring Reference (for Super Mode)

For informing our existing `dedup_and_rank()` in super mode, SearXNG's formula is relevant:

```python
# SearXNG: score = sum((occurrences * weight) / position) for each engine position
# Our equivalent in dedup_and_rank: rank by provider_count (number of providers that returned the URL)
```

Our super mode already uses cross-provider agreement as a ranking signal. The SearXNG pattern validates this approach. We adopt **Reciprocal Rank Fusion** (`1/(k + rank)` with k=60) for better rank dampening — the 2024-2025 standard in hybrid search (Elasticsearch, Azure AI Search, LangChain).

---

## 20. Implementation Phasing

This is a single-phase implementation. All features described in this document ship together. No phased rollout — the design is complete and self-consistent.

### 20.1 Implementation Notes

Key concerns identified during feasibility review:
- **call_counter**: In-memory only. Starts at 0 on session start. No persistence needed.
- **Hot reload + routing**: Use copy-on-write pattern (swap atomic reference) to avoid mid-flight invalidation.
- **Hedged requests**: Use `asyncio.create_task` + `asyncio.wait(return_when=FIRST_COMPLETED)`. Cancel pending on success.
- **Per-provider timeout**: `asyncio.wait_for` wrapper. Provider-specific override via `timeout` field.
- **`is_news` parameter**: Remove from router. Smart defaults handle news detection at a higher layer.
- **Backward compat**: `tier` and `conserve` fields are parsed but ignored. No errors on unknown fields.
