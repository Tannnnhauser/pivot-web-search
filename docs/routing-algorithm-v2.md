# Routing Algorithm v2: Priority + Quota Gates

Final design document for the pivot-web-search provider routing system.

**Status**: Design complete, ready for implementation.

**Replaces**: The existing tier-based tuple-sort routing in `routing.py`.

---

## 1. Design Principles

1. **User-declared priority** is the primary sort key. Lower number = tried first.
2. **Quota gates** prevent over-consumption. Exhausted providers are excluded; conserved providers are deferred.
3. **Circuit breaker** protects against failing providers with exponential backoff.
4. **Same priority = round-robin** via monotonic call counter per provider.
5. **Failover on quality**: fewer than 2 results triggers next-provider attempt.
6. **Single provider never errors**: always returns best-effort results.
7. **Observability first**: every routing decision is logged with reason.

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
- `effective_priority` = declared `priority` if provider is **active**, or `priority + 10000` if provider is **conserved** (deferred)
- `call_counter` = monotonically incrementing per-provider counter, used to round-robin among same-priority providers

### 3.2 Provider States

```
ACTIVE      — available for routing (priority unchanged)
CONSERVED   — available as fallback only (priority += 10000)
EXHAUSTED   — excluded from routing entirely
CIRCUIT_OPEN — excluded until cooldown expires
```

### 3.3 Pseudocode

```python
def select_providers(providers: list[Provider], query_context: QueryContext) -> list[Provider]:
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
        
        # Gate 3: Conservation check (with hysteresis)
        conserved = False
        if p.conserve and not is_exhausted(p):
            pace_ratio = compute_pace_ratio(p)
            threshold = CONSERVE_EXIT if p._conserved else CONSERVE_ENTER
            if pace_ratio > threshold:
                conserved = True
                log(f"DEFER {p.name}: pace_ratio={pace_ratio:.2f} > {threshold}, conserving")
        
        effective_priority = p.priority + (10000 if conserved else 0)
        
        candidates.append(ScoredProvider(
            provider=p,
            sort_key=(effective_priority, p.call_counter),
            state="CONSERVED" if conserved else "ACTIVE",
            breaker_state=breaker_state,
        ))
    
    # Sort by (effective_priority, call_counter)
    candidates.sort(key=lambda c: c.sort_key)
    
    return candidates


def execute_search(query: str, max_results: int, **kwargs) -> SearchResult | FailureInfo:
    """Execute search with failover."""
    
    candidates = select_providers(registry.get_enabled(), query_context)
    
    if not candidates:
        # All-open fallback: find provider closest to cooldown expiry
        recovery = pick_recovery_candidate(registry.get_enabled())
        if recovery:
            candidates = [recovery]
        else:
            return FailureInfo(failures=[], reason="all providers unavailable")
    
    min_acceptable = min(2, max_results)
    best_so_far = None
    failures = []
    
    for scored in candidates:
        p = scored.provider
        
        try:
            result = await p.search(query, max_results, **kwargs)
        except Exception as e:
            failures.append({"provider": p.name, "error": str(e)})
            circuit_breaker.record_failure(p.name, error=e)
            continue
        
        if result is None or not result.results:
            failures.append({"provider": p.name, "error": "no results"})
            circuit_breaker.record_failure(p.name, error=None)
            continue
        
        # Quality gate: enough results?
        if len(result.results) >= min_acceptable:
            circuit_breaker.record_success(p.name)
            record_usage(p.name)
            p.call_counter += 1  # advance round-robin
            log(f"SUCCESS {p.name}: {len(result.results)} results")
            return result
        
        # Partial results: record but continue trying
        circuit_breaker.record_success(p.name)  # provider worked, just low results
        record_usage(p.name)
        p.call_counter += 1
        
        if best_so_far is None or len(result.results) > len(best_so_far.results):
            best_so_far = result
            log(f"PARTIAL {p.name}: {len(result.results)}/{min_acceptable} results, continuing")
    
    # Exhausted all candidates
    if best_so_far is not None:
        return best_so_far  # best-effort, even if < min_acceptable
    
    return FailureInfo(failures=failures)
```

---

## 4. Quota Pacing Logic

### 4.1 Core Formula

```python
pace_ratio = usage_fraction / elapsed_fraction
```

Where:
- `usage_fraction = used / limit`  (0.0 to 1.0+)
- `elapsed_fraction` = fraction of quota period that has passed

### 4.2 Elapsed Fraction by Period Type

| Period | Formula |
|--------|---------|
| `daily` | `hour_of_day / 24` (PT timezone, min 1/24 at start of day) |
| `monthly` | `day_of_month / days_in_month` (UTC, min 1/days_in_month) |
| `rolling` | `(now - window_start) / (reset_at - window_start)` |

**Division guard**: `elapsed_fraction = max(elapsed_fraction, 0.01)`

### 4.3 State Transitions

```
                  pace_ratio <= 1.0
    ACTIVE  <─────────────────────────  CONSERVED
       │                                    ▲
       │    pace_ratio > CONSERVE_ENTER     │    pace_ratio < CONSERVE_EXIT
       └────────────────────────────────────┘
       │
       │    used >= limit
       ▼
   EXHAUSTED
```

- **ACTIVE**: `used < limit` AND (`conserve` is false OR `pace_ratio <= CONSERVE_EXIT` OR was never conserved)
- **CONSERVED**: `used < limit` AND `conserve` is true AND `pace_ratio > CONSERVE_ENTER` (or still > CONSERVE_EXIT if already conserved)
- **EXHAUSTED**: `used >= limit`

Hysteresis thresholds (prevent flapping on boundary):
- `CONSERVE_ENTER = 1.5` — enter conservation when pace_ratio exceeds this
- `CONSERVE_EXIT = 1.2` — exit conservation only when pace_ratio drops below this

### 4.4 When `conserve` Triggers Deferral

Conservation kicks in when the provider is being used significantly faster than its budget allows:

```python
def should_conserve(provider) -> bool:
    if not provider.conserve:
        return False
    if not provider.quota or not provider.quota.limit:
        return False  # no limit declared, nothing to conserve
    
    pace_ratio = compute_pace_ratio(provider)
    
    # Hysteresis: different thresholds for entering vs exiting conservation
    if provider._conserved:
        return pace_ratio > CONSERVE_EXIT   # 1.2 — stay conserved until well below
    return pace_ratio > CONSERVE_ENTER      # 1.5 — only enter when clearly over-pacing
```

A conserved provider is NOT excluded. It is demoted to priority + 10000, meaning it will still be tried if all non-conserved providers fail.

### 4.5 Edge Case: Monthly Provider on Day 1

On day 1 of a monthly period:
- `elapsed_fraction = 1 / days_in_month` (e.g., 1/30 = 0.033)
- Even 1 call out of a 1000-limit quota gives `usage_fraction = 0.001`
- `pace_ratio = 0.001 / 0.033 = 0.03` (well under 1.0, ACTIVE)

This naturally works. The first few calls never trigger conservation because the usage fraction is so small.

### 4.6 Edge Case: Provider With No Quota Declaration Gets Rate-Limited

If a provider has no `quota` block but returns 429:
1. The circuit breaker handles this (see Section 5)
2. After the breaker cooldown, the provider is re-tried
3. The provider is never marked EXHAUSTED (no limit to compare against)
4. Recommendation: log a warning suggesting the user add a quota declaration

---

## 5. Circuit Breaker Specifics

### 5.1 Parameters

```yaml
circuit_breaker:
  window_size: 5          # sliding window of recent outcomes
  consecutive_threshold: 3 # consecutive failures to open
  rate_threshold: 0.6     # failure rate in window to open (60%)
  min_samples: 3          # minimum outcomes before rate check applies
  base_cooldown: 60       # seconds, initial cooldown
  max_cooldown: 600       # seconds, maximum after backoff
  backoff_multiplier: 2   # exponential backoff factor
```

### 5.2 State Machine

```
          record_failure (threshold met)
CLOSED  ──────────────────────────────────► OPEN
   ▲                                          │
   │    record_success                        │ cooldown expires
   │    (probe succeeded)                     ▼
   └─────────────────────────────────────  HALF_OPEN
                                              │
                     record_failure            │
                     (probe failed)           │
                          ▼                   │
                        OPEN ◄────────────────┘
                    (cooldown *= backoff_multiplier)
```

### 5.3 Error Classification

| Error Type | Action |
|-----------|--------|
| HTTP 429 (Rate Limited) | Open breaker immediately (1 failure = open). Extract `Retry-After` header if present and use as cooldown. |
| HTTP 5xx (Server Error) | Record failure, apply normal threshold logic. |
| HTTP 401/403 (Auth) | Record failure, but also log warning about API key. |
| Timeout | Record failure, apply normal threshold logic. |
| Connection Error | Record failure, apply normal threshold logic. |
| HTTP 4xx (other) | Record failure, apply normal threshold logic. |
| Result count < 2 | Do NOT record as breaker failure (quality issue, not availability). |

### 5.4 Exponential Backoff

```python
def get_cooldown(provider_name: str) -> float:
    """Cooldown duration increases with consecutive open cycles."""
    entry = breakers[provider_name]
    cooldown = base_cooldown * (backoff_multiplier ** entry.open_count)
    return min(cooldown, max_cooldown)
```

- First open: 60s
- Second open (probe failed): 120s
- Third open: 240s
- Fourth open: 480s
- Fifth+ open: 600s (capped)

`open_count` resets to 0 when the breaker transitions HALF_OPEN -> CLOSED (successful probe).

### 5.5 429 with Retry-After

```python
def handle_429(provider_name: str, headers: dict):
    retry_after = parse_retry_after(headers)  # seconds
    if retry_after and retry_after > 0:
        # Use server-specified cooldown, clamped to [base_cooldown, max_cooldown]
        cooldown = clamp(retry_after, base_cooldown, max_cooldown)
    else:
        cooldown = get_cooldown(provider_name)
    
    open_breaker(provider_name, cooldown=cooldown)
```

### 5.6 Reset Timing

- **OPEN -> HALF_OPEN**: After cooldown expires (checked on next routing call)
- **HALF_OPEN -> CLOSED**: On first successful call
- **HALF_OPEN -> OPEN**: On first failed call (with increased cooldown)
- **Session restart**: All breaker state resets (in-memory only, not persisted)

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
    
    # Quota configuration (optional)
    quota:
      limit: 1000                 # Max calls per period. Required if quota block present.
      period: monthly             # daily | monthly | rolling. Default: monthly.
      conserve: true              # Optional. Default: false. When true, defer if over-pacing (hysteresis: enter 1.5, exit 1.2).
    
    # Provider-specific fields
    api_key_env: TAVILY_API_KEY   # Env var name for API key (type-specific)
    endpoint: "https://..."       # For searxng, json_api, llm_search
    # ... (all existing provider-specific fields remain unchanged)

# Optional: override global circuit breaker defaults per provider
circuit_breaker:
  base_cooldown: 60               # Default: 60
  max_cooldown: 600               # Default: 600
  backoff_multiplier: 2           # Default: 2
  window_size: 5                  # Default: 5
  consecutive_threshold: 3        # Default: 3
  rate_threshold: 0.6             # Default: 0.6
```

### 6.2 Validation Rules

| Field | Type | Constraints |
|-------|------|------------|
| `name` | string | Required. Unique across all providers. `[a-z0-9_-]+`, max 64 chars. |
| `type` | string | Required. Must be a registered adapter type. |
| `enabled` | bool | Optional. Default: true. |
| `priority` | int | Optional. Range: 1-9999. Default: per type_defaults. |
| `quota.limit` | int | Required if `quota` present. Must be > 0. |
| `quota.period` | string | Optional. One of: `daily`, `monthly`, `rolling`. Default: `monthly`. |
| `quota.conserve` | bool | Optional. Default: false. |
| `api_key_env` | string | Optional. Must be a valid env var name `[A-Z0-9_]+`. |
| `circuit_breaker.base_cooldown` | int | Optional. Range: 5-3600. Default: 60. |
| `circuit_breaker.max_cooldown` | int | Optional. Range: 60-7200. Default: 600. |
| `circuit_breaker.backoff_multiplier` | float | Optional. Range: 1.0-10.0. Default: 2.0. |

### 6.3 Backward Compatibility

The existing `tier` field is deprecated but still recognized. If present without a `quota` block:
- `tier: free` → no quota, priority default 10
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

| Type | Default Quota | Default Conserve | Source Override |
|------|---------------|------------------|----------------|
| `ddg` | none (unlimited) | false | — |
| `searxng` | none (unlimited) | false | — |
| `tavily` | `{limit: 1000, period: monthly}` | true | `PIVOT_WEB_SEARCH_TAVILY_QUOTA` |
| `brave` | `{limit: 2000, period: monthly}` | true | `PIVOT_WEB_SEARCH_BRAVE_QUOTA` |
| `gemini` | `{limit: 500, period: daily}` | true | `PIVOT_WEB_SEARCH_GEMINI_QUOTA` |
| `json_api` | none | false | User must configure |
| `llm_search` | none | false | User must configure |

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
| Standard paid | `tavily + brave + ddg` | tavily=20, brave=20, ddg=90 | Tavily/Brave (round-robin) → DDG |
| Self-hoster | `searxng + ddg` | searxng=30, ddg=90 | SearXNG → DDG |
| Premium | `llm_search + tavily + ddg` | llm=10, tavily=20, ddg=90 | Premium → Tavily → DDG |
| Enterprise | `json_api(premium) + gemini + ddg` | json=10, gemini=20, ddg=90 | Custom → Gemini → DDG |
| Budget maximizer | `tavily(pri=20) + brave(pri=20) + ddg(pri=20)` | all=20 (explicit) | Round-robin all three |

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

### 8.4 Call Counter Persistence

The `call_counter` is stored alongside quota data in `~/.cache/pivot-web-search/quota.json`:

```json
{
  "serper": {
    "used": 45,
    "limit": 100,
    "period": "daily",
    "call_counter": 127
  },
  "google-cse": {
    "used": 43,
    "limit": 100,
    "period": "daily",
    "call_counter": 125
  }
}
```

Counter persists across sessions to maintain fair distribution.

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
| Provider selected | `ROUTE {name} pri={pri} state={state}` | `ROUTE tavily pri=20 state=ACTIVE` |
| Provider skipped (exhausted) | `SKIP {name}: quota exhausted ({used}/{limit})` | `SKIP tavily: quota exhausted (1000/1000)` |
| Provider skipped (breaker) | `SKIP {name}: circuit OPEN, {secs}s remaining` | `SKIP brave: circuit OPEN, 45s remaining` |
| Provider deferred (conserve) | `DEFER {name}: pace_ratio={ratio:.2f}, conserving` | `DEFER tavily: pace_ratio=1.34, conserving` |
| Search success | `SUCCESS {name}: {count} results` | `SUCCESS ddg: 5 results` |
| Partial results | `PARTIAL {name}: {count}/{min} results, continuing` | `PARTIAL ddg: 1/2 results, continuing` |
| Search failure | `FAIL {name}: {error}` | `FAIL brave: HTTP 429` |
| Breaker state change | `BREAKER {name}: {old} -> {new} (reason)` | `BREAKER brave: CLOSED -> OPEN (3 consecutive failures)` |
| Recovery probe | `PROBE {name}: forced HALF_OPEN (all-open fallback)` | `PROBE ddg: forced HALF_OPEN (all-open fallback)` |
| Quota warning | `QUOTA {name}: {pct}% used, pace_ratio={ratio}` | `QUOTA tavily: 85% used, pace_ratio=1.7` |
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
    "algorithm": "priority_with_quota_gates",
    "total_providers": 4,
    "active": 3,
    "conserved": 0,
    "exhausted": 1,
    "circuit_open": 0
  },
  "providers": [
    {
      "name": "ddg",
      "type": "ddg",
      "priority": 10,
      "effective_priority": 10,
      "state": "ACTIVE",
      "enabled": true,
      "available": true,
      "quota": null,
      "breaker": {
        "state": "CLOSED",
        "recent_ok": 4,
        "recent_total": 5,
        "open_count": 0
      },
      "call_counter": 42
    },
    {
      "name": "tavily",
      "type": "tavily",
      "priority": 20,
      "effective_priority": 20,
      "state": "ACTIVE",
      "enabled": true,
      "available": true,
      "quota": {
        "used": 450,
        "limit": 1000,
        "period": "monthly",
        "usage_pct": 45.0,
        "pace_ratio": 0.9,
        "conserve": true,
        "source": "api"
      },
      "breaker": {
        "state": "CLOSED",
        "recent_ok": 3,
        "recent_total": 3,
        "open_count": 0
      },
      "call_counter": 38
    },
    {
      "name": "brave",
      "type": "brave",
      "priority": 30,
      "effective_priority": 30,
      "state": "EXHAUSTED",
      "enabled": true,
      "available": false,
      "quota": {
        "used": 2000,
        "limit": 2000,
        "period": "rolling",
        "usage_pct": 100.0,
        "pace_ratio": null,
        "conserve": true,
        "resets_at": "2024-03-01T00:00:00Z",
        "source": "header"
      },
      "breaker": {
        "state": "CLOSED",
        "recent_ok": 5,
        "recent_total": 5,
        "open_count": 0
      },
      "call_counter": 35
    },
    {
      "name": "gemini",
      "type": "gemini",
      "priority": 40,
      "effective_priority": 40,
      "state": "ACTIVE",
      "enabled": true,
      "available": true,
      "quota": {
        "used": 120,
        "limit": 500,
        "period": "daily",
        "usage_pct": 24.0,
        "pace_ratio": 0.48,
        "conserve": true,
        "resets_at": "PT midnight",
        "source": "config"
      },
      "breaker": {
        "state": "CLOSED",
        "recent_ok": 2,
        "recent_total": 2,
        "open_count": 0
      },
      "call_counter": 15
    }
  ],
  "circuit_breaker_config": {
    "base_cooldown": 60,
    "max_cooldown": 600,
    "backoff_multiplier": 2,
    "window_size": 5,
    "consecutive_threshold": 3,
    "rate_threshold": 0.6
  },
  "routing_order": ["ddg", "tavily", "gemini"],
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
| `routing.active` | Number of providers that will be tried before exhausted/open ones |
| `routing.conserved` | Number deferred due to pacing (still available as fallback) |
| `effective_priority` | Actual sort priority (includes +10000 for conserved) |
| `state` | Human-readable: ACTIVE, CONSERVED, EXHAUSTED, CIRCUIT_OPEN, DISABLED |
| `quota.pace_ratio` | Current consumption rate vs budget rate (>1.0 = over-pacing) |
| `routing_order` | The actual order providers would be tried right now (excluding exhausted/open) |
| `breaker.open_count` | How many times this breaker has opened (affects backoff) |
| `call_counter` | Total calls dispatched to this provider (for round-robin) |

---

## 12. Migration Path

### 12.1 What Changes

| Current (v1) | New (v2) |
|-------------|----------|
| `tier` field determines sort rank | `priority` field determines sort order directly |
| Three-level tier rank (0, 1, 2) | Flat numeric priority (1-9999) |
| Usage-pct as secondary metric | `call_counter` as tiebreaker |
| Pacing pressure for paid tier only | Pacing for any provider with `conserve: true` |
| High-water demotion (special case) | Replaced by generic conservation logic |
| News demotion for DDG (special case) | Removed (user controls priority) |
| Fixed 120s cooldown | Exponential backoff 60s-600s |
| Single failure threshold | Per-error-type handling (429 = immediate open) |

### 12.2 Backward Compatibility

Existing `config/providers.yaml` files continue to work:
- `priority` field already exists and is used
- `tier` field is mapped to `quota` defaults if no `quota` block present
- No configuration changes required for basic operation
- Users can incrementally adopt `quota` blocks

### 12.3 Implementation Order

1. Add `quota` schema to provider config parsing (backward-compat with `tier`)
2. Add `call_counter` to quota.json persistence
3. Implement new `select_providers()` with conservation logic
4. Replace `route_providers()` internals (same function signature)
5. Upgrade circuit breaker with exponential backoff + 429 handling
6. Update `WebSearchConfig` status output
7. Update logging to match new event format
8. Remove `TIER_RANK`, `HIGH_WATER_*`, `NEWS_DDG_*` constants
9. Update tests

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

### 13.2 Power User (Explicit Quotas, Conservation, Multiple Same-Priority)

```yaml
providers:
  - name: sonar-pro
    type: llm_search
    # No explicit priority → default 10 (tier: premium)
    endpoint: "https://api.perplexity.ai/chat/completions"
    api_key_env: PERPLEXITY_API_KEY
    api_format: chat_completions
    model: sonar-pro
    quota:
      limit: 50
      period: daily
      conserve: true

  - name: tavily
    type: tavily
    # No explicit priority → default 20 (tier: paid)
    api_key_env: TAVILY_API_KEY
    quota:
      limit: 1000
      period: monthly
      conserve: true

  - name: serper
    type: json_api
    priority: 20          # Explicit — same as Tavily: round-robin between them
    api_key_env: SERPER_API_KEY
    endpoint: "https://google.serper.dev/search"
    quota:
      limit: 2500
      period: monthly
      conserve: true

  - name: searxng-local
    type: searxng
    # No explicit priority → default 30 (tier: self-hosted)
    endpoint: "http://localhost:8888/search"

  - name: ddg
    type: ddg
    # No explicit priority → default 90 (tier: fallback)

circuit_breaker:
  base_cooldown: 30       # Faster recovery for local dev
  max_cooldown: 300
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
      conserve: false     # No point conserving with no fallback
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
      conserve: true

  - name: brave
    type: brave
    priority: 20
    api_key_env: BRAVE_API_KEY
    quota:
      limit: 2000
      period: monthly
      conserve: true

  - name: gemini
    type: gemini
    priority: 20
    api_key_env: GEMINI_SEARCH_API_KEY
    quota:
      limit: 500
      period: daily
      conserve: true

  - name: ddg
    type: ddg
    priority: 20          # Same as others — participates in round-robin
```

Routing: All four providers at priority 20. `call_counter` distributes evenly. When any provider's `conserve` triggers (pace_ratio > 1.5), it's deferred to priority 10020 — others continue round-robin. DDG never exhausts, so it picks up slack when paid providers conserve.

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
    priority: int = 50
    api_key_env: str | None = None
    quota: QuotaConfig | None = None
    # ... other provider-specific fields in self.config dict

@dataclass
class QuotaConfig:
    limit: int
    period: Literal["daily", "monthly", "rolling"] = "monthly"
    conserve: bool = False
```

### 14.2 Scored Provider (Internal to Router)

```python
@dataclass
class ScoredProvider:
    provider: SearchProvider
    sort_key: tuple[int, int]      # (effective_priority, call_counter)
    state: Literal["ACTIVE", "CONSERVED"]
    breaker_state: BreakerState
```

### 14.3 Circuit Breaker Entry (Enhanced)

```python
@dataclass
class BreakerEntry:
    state: BreakerState = BreakerState.CLOSED
    outcomes: deque = field(default_factory=lambda: deque(maxlen=5))
    consecutive_failures: int = 0
    opened_at: float | None = None
    open_count: int = 0            # NEW: tracks consecutive open cycles for backoff
    current_cooldown: float = 60.0  # NEW: current cooldown duration
```

### 14.4 Quota File Format (Enhanced)

```json
{
  "tavily": {
    "used": 450,
    "limit": 1000,
    "period": "monthly",
    "month": "2024-02",
    "source": "api",
    "last_synced": "2024-02-15T10:30:00Z",
    "call_counter": 38,
    "conserve": true
  },
  "gemini": {
    "used": 120,
    "limit": 500,
    "period": "daily",
    "day": "2024-02-15",
    "source": "config",
    "call_counter": 15,
    "conserve": true
  },
  "brave": {
    "used": 2000,
    "limit": 2000,
    "period": "rolling",
    "reset_at": "2024-03-01T00:00:00Z",
    "last_synced": "2024-02-15T10:30:00Z",
    "source": "header",
    "call_counter": 35,
    "conserve": true
  }
}
```

---

## 15. Testing Strategy

### 15.1 Unit Tests (Offline)

| Test Area | Cases |
|-----------|-------|
| Sort order | Priority ordering, call_counter tiebreak, conservation demotion |
| Smart defaults | Tier assignment, free-only collapse, explicit priority override, `premium` flag |
| Quota states | ACTIVE/CONSERVED/EXHAUSTED transitions, pace_ratio edge cases |
| Circuit breaker | Open/close/half-open transitions, exponential backoff, 429 handling |
| Multi-instance | Round-robin fairness, call_counter persistence |
| Edge cases | Single provider, all exhausted, no quota declaration + 429 |
| Backward compat | `tier` field mapping to quota defaults, v1 explicit priorities unchanged |
| Config validation | Invalid priority range, missing required fields, duplicate names |
| Diagnostic notes | Suboptimal config detection, priority ordering warnings |

### 15.2 Integration Tests (Live)

| Test Area | Cases |
|-----------|-------|
| Full failover | DDG rate-limit -> Tavily success |
| Quality gate | Provider returns 1 result -> failover to next |
| Conservation | Pace ratio > 1.0 -> provider deferred but reachable |
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
| Provider approaching quota | `"'tavily' at 85% quota (pace_ratio=1.3). Conservation active — deferred to fallback."` |
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

**Our differentiator**: We are the only search routing system that combines quality-based failover (result count threshold), quota-aware conservation, and heterogeneous provider support. LLM gateways route between providers serving the same model; we route between fundamentally different search backends.

---

## 18. Summary of Decisions

1. **Kill tiers as routing rank**: No more `free/daily/paid` tier_rank system. `effective_priority` is the sole sort key.
2. **Smart defaults by quality tier**: Premium=10, Paid=20, Self-hosted=30, Free=90. Only when no explicit priority.
3. **Free-only collapse**: If DDG is the only provider, it gets priority 10 (not 90).
4. **Kill high-water demotion**: Replaced by generic `conserve` flag + pace_ratio.
5. **Kill news demotion**: DDG priority is user-configured. Special-casing removed.
6. **Add call_counter**: Enables true round-robin at same priority without separate tracking.
7. **Add exponential backoff**: Prevents hammering a consistently-failing provider (60s → 600s).
8. **429 = immediate open**: Rate limits are treated as breaker events, not regular failures.
9. **conserve is opt-in**: Providers without `conserve: true` are never deferred, only exhausted.
10. **Single provider = best effort**: Never error if we got *any* results.
11. **Status shows routing_order + priority_source**: User can verify computed behavior matches intent.
12. **`premium` flag for json_api**: Promotes custom endpoints to tier 1 when warranted.

---

## 19. Future Enhancements (Not Blocking v2)

### 19.1 Advanced Quality Gate (Beyond Result Count)

The current quality gate (`< 2 results → failover`) is simple and effective. Future improvements based on meta-search research (SearXNG scoring, search aggregation literature):

| Signal | Description | When to Trigger |
|--------|-------------|----------------|
| Domain diversity | > 80% results from single domain | Indicates navigational/broken response |
| Query-term overlap | < 50% of results contain any query term | Suggests irrelevant response |
| Snippet quality | Missing or < 20 char snippets on majority | Degraded metadata response |
| Cross-provider consensus | Only penalize if peers returned more | Prevents false positives on obscure queries |
| Rolling baseline | Per-provider historical average | Replace static "< 2" with adaptive minimum |

These are additive — the current `< 2 results` gate remains as the primary, simple check.

### 19.2 Implementation Phasing

| Phase | Scope | Risk |
|-------|-------|------|
| **Phase 1 (v2.0)** | Replace sort key, add call_counter, smart defaults, daily pacing | Low — behavioral improvement, backward compatible |
| **Phase 2** | Exponential backoff, 429 immediate-open, conservation hysteresis | Medium — resilience |
| **Phase 3** | Enhanced status output, diagnostic notes, tier deprecation warnings | Low — UX polish |

### 19.3 Implementation Notes

Key concerns identified during feasibility review:
- **call_counter persistence**: Best-effort in-memory, flush to quota.json periodically. Eventual consistency is acceptable for round-robin fairness.
- **Hysteresis state**: Store `_conserved` flag in module-level dict keyed by provider name (survives config reload).
- **Hot reload + routing**: Use copy-on-write pattern (swap atomic reference) to avoid mid-flight invalidation.
- **`is_news` parameter**: Keep in `route_providers()` signature but ignore (backward compat). Remove in v3.
