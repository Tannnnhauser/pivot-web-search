# Load Balancer Design: Tuple-Sort Routing + Circuit Breaker

## Context

The plugin has multiple search providers with different cost models and quota reset cycles. The current routing (`_quota_sorted_providers` in `server.py`) is a simple failover: DDG always first, then paid providers sorted by `usage_pct`. This has several problems:

1. DDG demotion is ad-hoc (3-failure session toggle, never auto-recovers)
2. No distinction between "free daily quota" (Gemini 500 RPD) and "expensive monthly quota" (Tavily/Brave)
3. Sorting by raw `usage_pct` doesn't account for time-in-window (30% at month start vs month end mean very different things)
4. No generic health tracking — only DDG has failure detection

## Design Principles

- **One tuple, one sort, natural degradation** — no if-else routing layers
- **Tier separation via lexicographic comparison** — tiers can never collide
- **Session-scoped state** — breaker resets on MCP server restart, no persistent file
- **Works for any provider subset** — from 1 provider to N, no special cases

---

## Algorithm

Each provider is scored as a 3-tuple: `(tier_rank, metric, priority)`

Python tuple comparison is lexicographic — first element decides unless tied, then second, then third.

### Tier Classification

| Tier | tier_rank | Meaning | Examples | Metric |
|------|-----------|---------|----------|--------|
| free | 0 | No quota, no cost | DDG, SearXNG | 0.0 (fixed) |
| daily | 1 | Daily reset, high headroom | Gemini (500 RPD) | Today's usage % |
| paid | 2 | Long-cycle, cross-day accumulation | Tavily, Brave, Serper | Pacing pressure |

Tier is determined from **config** (`providers.yaml` `tier` field), NOT inferred from quota.json state. Default inference for unconfigured providers: `ddg`/`searxng` type → free, `gemini` type → daily, anything with `api_key_env` → paid.

### Pacing Pressure (paid tier metric)

```
pressure = actual_usage_pct / elapsed_time_pct
```

- `> 1.0` = consuming faster than budget allows (prefer other providers)
- `< 1.0` = under budget (safe to use)
- `= 1.0` = exactly on pace

Elapsed time calculation per period type:
- **Monthly** (Tavily): `day_of_month / days_in_month` (UTC, matches quota reset)
- **Rolling** (Brave): `1 - (remaining_seconds / total_window_seconds)` using `reset_at` from headers
- **Missing data**: If no `reset_at` or no limit, return `0.0` (neutral — falls back to priority tiebreak)

Guard: `max(elapsed_time_pct, 0.01)` to prevent division by zero on period boundaries.

### High-Water Demotion (daily tier)

When a daily provider exceeds 85% usage AND more than 4 hours remain until PT midnight:
- Bump `tier_rank` from 1 → 2 (same as paid)
- Preserves last ~75 requests for late-day burst needs
- Near midnight (< 4 hours left): don't demote (use-it-or-lose-it)

### News Query Adjustment

When query context indicates news/time-sensitive AND other healthy providers exist:
- DDG `tier_rank` bumped to 3 (below paid)
- Only applies if DDG is NOT the only available provider (safety check)

---

## Circuit Breaker

### States

```
CLOSED (healthy) → OPEN (skip) → HALF_OPEN (probe) → CLOSED or OPEN
```

### Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Window size | 5 | CLI sessions are short, 10 would never fill |
| Min samples | 3 | Don't trigger on first 2 calls |
| Failure threshold | 3 consecutive OR > 60% in window | Matches current DDG behavior (3 consecutive) plus rate-based |
| Cooldown | 120s | Long enough to skip transient issues, short enough for session utility |
| Half-open probes | 1 attempt; success → CLOSED, failure → OPEN (restart cooldown) |

### Failure Classification

| Event | Counts as failure? |
|-------|--------------------|
| Exception / timeout | ✅ Yes |
| HTTP 429 / 503 | ✅ Yes |
| Returns None (no results at all) | ✅ Yes |
| Results < min_acceptable but non-empty | ❌ No (quality issue, not health) |
| Success ≥ min_acceptable | ❌ No |

### All-OPEN Fallback

If every provider's circuit breaker is OPEN:
- Select the provider closest to cooldown expiry
- Force it into HALF_OPEN immediately
- Allow one probe request through

### Keying and Lifecycle

- Breaker state keyed by provider **name** (survives config hot-reload)
- Session-scoped (in-memory dict, no disk persistence)
- MCP server restart = clean slate

---

## Integration Points

### Callsites to update in `server.py`

| Location | Current | New |
|----------|---------|-----|
| `_quota_sorted_providers()` (~L127) | DDG-first + usage_pct sort | Replace with `_route_providers(providers, query_ctx)` |
| `_track_ddg_result()` (~L112) | DDG-only failure toggle | Replace with `_breaker.record(provider, outcome)` |
| `_search_with_registry()` (~L166) | Calls old sorter + DDG tracker | Calls new router + generic breaker |
| `_search_super_with_registry()` (~L197) | Same old sorter + tracker | **Ignores breaker for selection** (maximum coverage), but still records outcomes |

### Explicit provider selection (bypass)

When user specifies `provider="tavily"` explicitly:
- Skip routing entirely (current behavior, keep it)
- Still record outcome to breaker (so auto mode benefits from the signal)

### Domain-filter forced Tavily

When domain filtering forces Tavily selection (`server.py` ~L334):
- Same as explicit selection — bypass routing, record outcome

### `include_content` Brave LLM Context path

- Currently bypasses registry routing
- Record outcome to Brave's breaker for consistency

### Super mode semantics

- **Selection**: Include ALL non-exhausted providers (ignore OPEN state)
- **Recording**: Record outcomes to breaker (feeds future auto-mode decisions)
- Rationale: super mode's contract is "maximum coverage" — breaker is for auto mode optimization only

---

## Config Changes

### `config/providers.yaml` — add `tier` field

```yaml
providers:
  - name: ddg
    type: ddg
    tier: free          # NEW — routing tier
    enabled: true
    priority: 10

  - name: tavily
    type: tavily
    tier: paid          # NEW
    enabled: true
    priority: 20
    api_key_env: TAVILY_API_KEY

  - name: brave
    type: brave
    tier: paid          # NEW
    enabled: true
    priority: 30
    api_key_env: BRAVE_API_KEY

  - name: gemini
    type: gemini
    tier: daily         # NEW
    enabled: true
    priority: 40
    api_key_env: GEMINI_SEARCH_API_KEY
    model: gemini-2.5-flash
```

For user-added providers without `tier`, infer from type:
- `ddg`, `searxng` → free
- `gemini` → daily
- Anything else with `api_key_env` → paid
- Anything else without `api_key_env` → free

---

## File Changes

| File | Change |
|------|--------|
| `config/providers.yaml` | Add `tier` field to all providers |
| `pivot_web_search_mcp/providers.py` | `SearchProvider` parses `tier` from config, adds `tier` property with default inference |
| `pivot_web_search_mcp/server.py` | Delete `_ddg_consecutive_failures`, `_ddg_demoted`, `_DDG_FAILURE_THRESHOLD`, `_track_ddg_result()`, `_quota_sorted_providers()`. Add `CircuitBreaker` class, `_route_providers()`, `compute_pacing_pressure()`, helper `_hours_until_pt_midnight()`. Update `_search_with_registry()` and `_search_super_with_registry()` callsites. |
| `pivot_web_search_mcp/quota.py` | Add `compute_pacing_pressure(provider_name)` (or keep in server.py — TBD based on import cleanliness) |
| `tests/conftest.py` | Delete `_reset_ddg_demotion` fixture, add `_reset_circuit_breaker` fixture |
| `tests/test_routing.py` (new) | Full test coverage for tuple sort, breaker state machine, pacing pressure, tier demotion |
| `tests/test_failover.py` | Rewrite `TestDdgDemotion` → `TestCircuitBreaker` |
| `README.md` | Update quota/routing documentation section |

---

## Observability

### `WebSearchConfig status` output additions

```
Routing:
  ddg:      tier=free   breaker=CLOSED  (5/5 recent OK)
  tavily:   tier=paid   breaker=CLOSED  pressure=0.4 (under budget)
  brave:    tier=paid   breaker=OPEN    cooldown=83s remaining
  gemini:   tier=daily  breaker=CLOSED  usage=12% (60/500 today)
```

### Logging

- Circuit breaker state transitions: `log(f"{name} breaker OPEN after {n} failures")`
- Provider selection reasoning: `log(f"Selected {name}: tier={tier} pressure={pressure:.2f}")`
- High-water demotion: `log(f"{name} demoted to paid tier (usage {pct}%, {hours}h until reset)")`

---

## Test Plan

### Circuit Breaker tests

- CLOSED → OPEN after 3 consecutive failures
- CLOSED → stays CLOSED with < 3 failures in window
- OPEN → HALF_OPEN after cooldown expires
- HALF_OPEN → CLOSED on success
- HALF_OPEN → OPEN on failure (cooldown restarts)
- Min samples guard (< 3 calls never triggers OPEN)
- All-OPEN fallback picks closest-to-recovery

### Tuple Sort tests

- Free providers sort before daily before paid
- Within free tier, sorted by priority
- Within daily tier, sorted by usage_pct
- Within paid tier, sorted by pacing pressure then priority
- Exhausted providers get `(inf,)` — sorted last
- OPEN breaker providers get `(inf,)` — sorted last
- Single provider returns that provider
- Mixed config (some tiers empty) works correctly

### Pacing Pressure tests

- Monthly: day 1 with 0 usage → pressure 0
- Monthly: day 15 with 50% usage → pressure ~1.0
- Monthly: day 15 with 80% usage → pressure ~1.6
- Rolling: mid-window with expected usage → ~1.0
- Rolling: missing reset_at → returns 0.0
- Division by zero guard at period boundary

### High-Water Demotion tests

- 84% usage → stays tier 1 (daily)
- 86% usage with > 4h to midnight → tier 2 (demoted)
- 86% usage with < 4h to midnight → stays tier 1 (use-it-or-lose-it)

### News Demotion tests

- DDG demoted to tier 3 for news query when other providers available
- DDG NOT demoted when it's the only provider
- Non-DDG free providers (SearXNG) unaffected by news demotion

### Integration tests

- Super mode includes OPEN providers, records outcomes
- Explicit provider selection bypasses routing, records to breaker
- Failover still works: sorted list is iterated on quality failure

---

## Estimation

- ~250-300 lines new code (CircuitBreaker ~80, routing ~60, pacing ~40, tests ~120)
- ~40 lines deleted (DDG demotion globals + old sorter)
- Risk: low — routing is internal, no API surface change, existing failover loop unchanged

---

## Discussion History

This design was developed over 4 rounds of three-way discussion (Claude + Gemini + Codex):

1. **Round 1**: Established circuit breaker + fair share as base model. Gemini argued against time-based token buckets (bursty CLI usage). Codex proposed budget slack selector.
2. **Round 2**: Explored multi-strategy (user profiles). All three rejected it — converged on dynamic scarcity-based routing with zero config.
3. **Round 3**: Proposed unified scoring function. Gemini and Codex both identified arithmetic scoring as fragile — converged on tuple sort as the correct abstraction.
4. **Round 4 (final review)**: Three reviewers identified: tier inference from quota.json is unreliable (needs config), breaker params too large for CLI, super mode interaction missed, failure classification needed, news demotion safety check needed.

Key rejected alternatives:
- Multi-armed bandit (no reliable reward signal)
- Named user strategies/profiles (over-engineering for CLI)
- Arithmetic scoring with magic number gaps (cross-tier collision risk)
- Token bucket time-pacing (too rigid for bursty developer usage)
