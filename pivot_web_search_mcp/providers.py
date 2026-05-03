#!/usr/bin/env python3
"""Search provider adapters and registry for hot-pluggable search sources.

Provides:
  - SearchResult: unified result dataclass
  - SearchProvider: base class for all adapters
  - Built-in adapters: Ddg, Tavily, Brave, Gemini, Searxng, JsonApi
  - ProviderRegistry: config-driven provider management with mtime reload
  - load_proxies(): config-driven proxy list with mtime reload
"""

import json
import os
import pathlib
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from .logging import log

# ---------------------------------------------------------------------------
# Config paths — resolved relative to the project root (parent of pivot_web_search_mcp/)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PROVIDERS_YAML = _PROJECT_ROOT / "config" / "providers.yaml"
_PROXIES_YAML = _PROJECT_ROOT / "config" / "proxies.yaml"

# ---------------------------------------------------------------------------
# YAML loader — PyYAML required
# ---------------------------------------------------------------------------

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


def _load_yaml(path):
    """Load a YAML file. Raises RuntimeError if PyYAML is missing."""
    if not _HAS_YAML:
        raise RuntimeError(f"PyYAML is required to load {path} — run: uv sync")
    with open(path, "r") as f:
        return yaml.safe_load(f)

# ---------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    """Unified result from any search provider."""
    results: list = field(default_factory=list)
    provider: str = ""
    answer: str | None = None

# ---------------------------------------------------------------------------
# SearchProvider base
# ---------------------------------------------------------------------------

class SearchProvider:
    """Base class for search provider adapters."""

    name: str = ""
    provider_type: str = ""
    priority: int = 100
    enabled: bool = True

    def __init__(self, name, priority=100, enabled=True, config=None):
        self.name = name
        self.priority = priority
        self.enabled = enabled
        self.config = config or {}

    async def search(self, query, max_results=5, **kwargs):
        """Returns SearchResult or None on failure."""
        raise NotImplementedError

    async def health_check(self):
        """Returns (available: bool, detail_or_error: str | None)."""
        return False, "not implemented"

    @property
    def tier(self) -> str:
        """Routing tier: 'free', 'daily', or 'paid'. From config or inferred."""
        explicit = self.config.get("tier")
        if explicit in ("free", "daily", "paid"):
            return explicit
        if self.provider_type in ("ddg", "searxng"):
            return "free"
        if self.provider_type == "gemini":
            return "daily"
        if self.config.get("api_key_env"):
            return "paid"
        return "free"

    def __repr__(self):
        state = "on" if self.enabled else "off"
        return f"<{self.__class__.__name__} {self.name!r} pri={self.priority} {state}>"

# ---------------------------------------------------------------------------
# Built-in adapters
# ---------------------------------------------------------------------------

class DdgProvider(SearchProvider):
    provider_type = "ddg"

    async def search(self, query, max_results=5, **kwargs):
        from . import search as s
        results = await s.search_ddg(
            query, max_results,
            region=kwargs.get("region", "wt-wt"),
            timelimit=kwargs.get("timelimit"),
            news=kwargs.get("news", False),
        )
        if results is None:
            return None
        return SearchResult(results=results, provider=self.name)

    async def health_check(self):
        try:
            from ddgs import DDGS  # noqa: F401

            from . import search as s
            client = await s._get_client()
            resp = await client.head("https://duckduckgo.com/", timeout=3.0)
            resp.raise_for_status()
            return True, None
        except ImportError:
            return False, "ddgs not installed"
        except Exception as e:
            return False, str(e)[:80]


class TavilyProvider(SearchProvider):
    provider_type = "tavily"

    def _get_key(self):
        env_var = self.config.get("api_key_env", "TAVILY_API_KEY")
        return os.environ.get(env_var, "").strip() or None

    async def search(self, query, max_results=5, **kwargs):
        from . import search as s
        tv = await s.search_tavily(
            query, max_results,
            include_answer=kwargs.get("include_answer", False),
            search_depth=kwargs.get("search_depth", "basic"),
            topic=kwargs.get("topic", "general"),
            days=kwargs.get("days"),
            include_domains=kwargs.get("include_domains"),
            exclude_domains=kwargs.get("exclude_domains"),
        )
        if tv is None:
            return None
        results, answer = tv
        return SearchResult(results=results, provider=self.name, answer=answer)

    async def health_check(self):
        key = self._get_key()
        return (True, None) if key else (False, "no API key")


class BraveProvider(SearchProvider):
    provider_type = "brave"

    def _get_key(self):
        env_var = self.config.get("api_key_env", "BRAVE_API_KEY")
        return os.environ.get(env_var, "").strip() or None

    async def search(self, query, max_results=5, **kwargs):
        from . import search as s
        rv = await s.search_brave(query, max_results)
        if rv is None:
            return None
        results, resp_headers = rv
        try:
            from . import quota
            quota.update_from_brave_headers(resp_headers)
        except Exception:
            pass
        return SearchResult(results=results, provider=self.name)

    async def health_check(self):
        key = self._get_key()
        return (True, None) if key else (False, "no API key")


class LlmSearchProvider(SearchProvider):
    """Adapter for LLM-based search via OpenAI-compatible APIs.

    Supports api_format: chat_completions, responses, gemini.
    Uses strategy pattern — format-specific logic is in llm_search_formats.py.
    """
    provider_type = "llm_search"

    def __init__(self, name, priority=100, enabled=True, config=None):
        super().__init__(name, priority, enabled, config)
        from .llm_search_formats import FORMAT_REGISTRY
        fmt_name = self.config.get("api_format", "chat_completions")
        fmt_cls = FORMAT_REGISTRY.get(fmt_name)
        if not fmt_cls:
            log(f"{name}: unknown api_format '{fmt_name}', falling back to chat_completions")
            from .llm_search_formats import ChatCompletionsFormat
            fmt_cls = ChatCompletionsFormat
        self._format = fmt_cls()

    def _get_key(self):
        env_var = self.config.get("api_key_env")
        if not env_var:
            return None
        return os.environ.get(env_var, "").strip() or None

    async def search(self, query, max_results=5, **kwargs):
        api_key = self._get_key()
        endpoint, headers, body = self._format.build_request(query, max_results, self.config)

        if not endpoint:
            log(f"{self.name}: no endpoint configured")
            return None

        if api_key and "Authorization" not in headers and "x-goog-api-key" not in headers:
            headers["Authorization"] = f"Bearer {api_key}"

        timeout = self.config.get("timeout", 30)

        try:
            from . import search as s
            resp = await s._open_with_fallback(
                "POST", endpoint, headers=headers, data=body, timeout=timeout)

            if resp.status_code >= 400:
                try:
                    err_obj = resp.json()
                except Exception:
                    err_obj = {}
                msg = self._format.parse_error(resp.status_code, err_obj)
                log(f"{self.name} HTTP {resp.status_code}: {msg}")
                return None

            obj = resp.json()
        except Exception as e:
            log(f"{self.name} failed: {e}")
            return None

        results, answer = self._format.parse_response(obj, max_results, self.name)
        if not results and not answer:
            return None

        return SearchResult(results=results, provider=self.name, answer=answer)

    async def health_check(self):
        endpoint = self.config.get("endpoint")
        if not endpoint:
            return False, "no endpoint"
        key = self._get_key()
        if self.config.get("api_key_env") and not key:
            return False, "no API key"
        return True, None


class GeminiProvider(LlmSearchProvider):
    """Gemini with Search grounding — thin wrapper over LlmSearchProvider.

    Backward-compatible: keeps type='gemini' and dual-key fallback logic.
    """
    provider_type = "gemini"

    def __init__(self, name, priority=100, enabled=True, config=None):
        config = dict(config or {})
        config.setdefault("api_format", "gemini")
        super().__init__(name, priority, enabled, config)

    def _get_key(self):
        env_var = self.config.get("api_key_env", "GEMINI_SEARCH_API_KEY")
        key = os.environ.get(env_var, "").strip()
        if not key:
            key = os.environ.get("GOOGLE_STUDIO_API_KEY", "").strip()
        return key or None

    async def search(self, query, max_results=5, **kwargs):
        api_key = self._get_key()
        if not api_key:
            log(f"No API key for {self.name}, skipping")
            return None

        endpoint, headers, body = self._format.build_request(query, max_results, self.config)
        headers["x-goog-api-key"] = api_key

        timeout = self.config.get("timeout", 30)

        try:
            from . import search as s
            resp = await s._open_with_fallback(
                "POST", endpoint, headers=headers, data=body, timeout=timeout)
            obj = resp.json()
        except Exception as e:
            log(f"{self.name} failed: {e}")
            return None

        results, answer = self._format.parse_response(obj, max_results, self.name)
        if not results:
            return None

        return SearchResult(results=results, provider=self.name, answer=answer)

    async def health_check(self):
        key = self._get_key()
        return (True, None) if key else (False, "no API key")


class SearxngProvider(SearchProvider):
    """Adapter for SearXNG instances exposing a JSON API."""
    provider_type = "searxng"

    async def search(self, query, max_results=5, **kwargs):
        endpoint = self.config.get("endpoint")
        if not endpoint:
            log(f"{self.name}: no endpoint configured")
            return None

        params = urllib.parse.urlencode({
            "q": query, "format": "json", "categories": "general",
        })
        url = f"{endpoint}?{params}"
        try:
            from . import search as s
            resp = await s._open_with_fallback(
                "GET", url,
                headers={"Accept": "application/json", "User-Agent": "pivot-web-search/1.0"},
                timeout=10)
            obj = resp.json()
            raw = obj.get("results", [])[:max_results]
            results = [{"title": r.get("title", ""), "url": r.get("url", ""),
                        "snippet": r.get("content", "")} for r in raw]
            return SearchResult(results=results, provider=self.name) if results else None
        except Exception as e:
            log(f"{self.name} failed: {e}")
            return None

    async def health_check(self):
        endpoint = self.config.get("endpoint")
        if not endpoint:
            return False, "no endpoint"
        try:
            from . import search as s
            client = await s._get_client()
            resp = await client.head(endpoint, timeout=3.0)
            resp.raise_for_status()
            return True, None
        except Exception as e:
            return False, str(e)[:80]


class JsonApiProvider(SearchProvider):
    """Generic adapter for JSON search APIs with configurable request/response mapping."""
    provider_type = "json_api"

    def _get_key(self):
        env_var = self.config.get("api_key_env")
        if not env_var:
            return None
        return os.environ.get(env_var, "").strip() or None

    def _resolve_dotpath(self, obj, path):
        """Resolve a dotpath like 'data.results' into nested dict access."""
        for part in path.split("."):
            if isinstance(obj, dict):
                obj = obj.get(part)
            elif isinstance(obj, (list, tuple)) and part.isdigit():
                obj = obj[int(part)]
            else:
                return None
        return obj

    def _render_template(self, val, context):
        """Replace {{key}} placeholders with context values."""
        if isinstance(val, str):
            def replacer(m):
                k = m.group(1)
                v = context.get(k, "")
                return str(v) if v is not None else ""
            return re.sub(r"\{\{(\w+)\}\}", replacer, val)
        if isinstance(val, dict):
            return {k: self._render_template(v, context) for k, v in val.items()}
        if isinstance(val, list):
            return [self._render_template(v, context) for v in val]
        return val

    async def search(self, query, max_results=5, **kwargs):
        endpoint = self.config.get("endpoint")
        if not endpoint:
            log(f"{self.name}: no endpoint configured")
            return None

        api_key = self._get_key()
        method = self.config.get("method", "GET").upper()
        headers = dict(self.config.get("headers", {}))

        ctx = {"query": query, "max_results": max_results, "api_key": api_key or ""}

        if api_key and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {api_key}"

        if method == "POST":
            body_template = self.config.get("request_body", {"q": "{{query}}"})
            body = self._render_template(body_template, ctx)
            data = json.dumps(body).encode("utf-8")
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"
        else:
            data = None
            params_template = self.config.get("request_params", {"q": "{{query}}", "num": "{{max_results}}"})
            params = self._render_template(params_template, ctx)
            qs = urllib.parse.urlencode(params)
            endpoint = f"{endpoint}?{qs}"

        try:
            from . import search as s
            resp = await s._open_with_fallback(
                method, endpoint, headers=headers, data=data, timeout=15)
            obj = resp.json()

            mapping = self.config.get("response_mapping", {})
            results_path = mapping.get("results_path", "results")
            title_key = mapping.get("title", "title")
            url_key = mapping.get("url", "url")
            snippet_key = mapping.get("snippet", "snippet")

            raw_results = self._resolve_dotpath(obj, results_path)
            if not isinstance(raw_results, list):
                return None

            results = []
            for r in raw_results[:max_results]:
                if not isinstance(r, dict):
                    continue
                results.append({
                    "title": str(r.get(title_key, "")),
                    "url": str(r.get(url_key, "")),
                    "snippet": str(r.get(snippet_key, "")),
                })
            return SearchResult(results=results, provider=self.name) if results else None

        except Exception as e:
            log(f"{self.name} failed: {e}")
            return None

    async def health_check(self):
        endpoint = self.config.get("endpoint")
        if not endpoint:
            return False, "no endpoint"
        key = self._get_key()
        if self.config.get("api_key_env") and not key:
            return False, "no API key"
        return True, None


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

_ADAPTER_MAP = {
    "ddg": DdgProvider,
    "tavily": TavilyProvider,
    "brave": BraveProvider,
    "gemini": GeminiProvider,
    "searxng": SearxngProvider,
    "json_api": JsonApiProvider,
    "llm_search": LlmSearchProvider,
}

# ---------------------------------------------------------------------------
# Default providers (used when no config file exists)
# ---------------------------------------------------------------------------

_DEFAULT_PROVIDERS = [
    {"name": "ddg", "type": "ddg", "enabled": True, "priority": 10},
    {"name": "tavily", "type": "tavily", "enabled": True, "priority": 20, "api_key_env": "TAVILY_API_KEY"},
    {"name": "brave", "type": "brave", "enabled": True, "priority": 30, "api_key_env": "BRAVE_API_KEY"},
    {"name": "gemini", "type": "gemini", "enabled": True, "priority": 40, "api_key_env": "GEMINI_SEARCH_API_KEY"},
]

# Default proxies (used when no config file exists)
_DEFAULT_PROXIES = [
    {"name": "direct", "url": None, "enabled": True, "priority": 1},
    {"name": "myproxy1", "url": "http://myproxy1.example:8080", "enabled": False, "priority": 2},
    {"name": "myproxy2", "url": "http://myproxy2.example:8080", "enabled": False, "priority": 3},
]

# ---------------------------------------------------------------------------
# ProviderRegistry
# ---------------------------------------------------------------------------

class ProviderRegistry:
    """Manages search providers loaded from config, with mtime-based reload."""

    def __init__(self):
        self._providers = []
        self._config_path = None
        self._config_mtime = 0
        self._from_env = False

    def _load_from_env(self):
        """Build provider list from PIVOT_WEB_SEARCH_PROVIDERS env var.

        Returns (providers, source) or (None, None) if env var is not set.
        """
        raw = os.environ.get("PIVOT_WEB_SEARCH_PROVIDERS", "").strip()
        if not raw:
            return None, None

        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
        if not names:
            return None, None

        entries = []
        for i, name in enumerate(names):
            ptype = name
            entry = {"name": name, "type": ptype, "enabled": True, "priority": (i + 1) * 10}
            default = next((d for d in _DEFAULT_PROVIDERS if d["name"] == name), None)
            if default and "api_key_env" in default:
                entry["api_key_env"] = default["api_key_env"]
            entries.append(entry)

        providers = self._build_providers(entries)
        return providers, f"env PIVOT_WEB_SEARCH_PROVIDERS={raw}"

    def load(self, config_path=None):
        """Load providers from env var, YAML config, or defaults (in that priority)."""
        self._config_path = pathlib.Path(config_path) if config_path else _PROVIDERS_YAML

        env_providers, env_source = self._load_from_env()
        if env_providers is not None:
            self._providers = env_providers
            self._from_env = True
            source = env_source
        elif self._config_path.exists():
            try:
                data = _load_yaml(str(self._config_path))
                self._config_mtime = self._config_path.stat().st_mtime
                entries = data.get("providers", [])
                if not isinstance(entries, list):
                    raise ValueError("'providers' must be a list")
                self._providers = self._build_providers(entries)
                source = str(self._config_path)
            except Exception as e:
                log(f"Failed to load {self._config_path}: {e} — using defaults")
                self._providers = self._build_providers(_DEFAULT_PROVIDERS)
                source = "defaults (config load failed)"
        else:
            self._providers = self._build_providers(_DEFAULT_PROVIDERS)
            source = "defaults (no config file)"

        log(f"Loaded {len(self._providers)} providers from {source}")

    def _build_providers(self, entries):
        providers = []
        for entry in entries:
            ptype = entry.get("type", "")
            cls = _ADAPTER_MAP.get(ptype)
            if not cls:
                log(f"Unknown provider type '{ptype}' for '{entry.get('name', '?')}', skipping")
                continue
            p = cls(
                name=entry.get("name", ptype),
                priority=entry.get("priority", 100),
                enabled=entry.get("enabled", True),
                config=entry,
            )
            providers.append(p)
        return providers

    def _check_reload(self):
        """Reload config if file mtime changed (cheap stat check)."""
        if self._from_env or not self._config_path:
            return
        try:
            if self._config_path.exists():
                mtime = self._config_path.stat().st_mtime
                if mtime > self._config_mtime:
                    log("Config changed, reloading providers")
                    self.load(str(self._config_path))
        except Exception:
            pass

    def get_ordered(self):
        """Returns enabled providers sorted by priority (ascending). Auto-reloads on config change."""
        self._check_reload()
        return sorted([p for p in self._providers if p.enabled], key=lambda p: p.priority)

    def get_all(self):
        """Returns all providers (including disabled). Auto-reloads on config change."""
        self._check_reload()
        return list(self._providers)

    def get_by_name(self, name):
        """Get a specific provider by name."""
        self._check_reload()
        for p in self._providers:
            if p.name == name:
                return p
        return None

    def reload(self):
        """Force re-read config file."""
        if self._config_path:
            self.load(str(self._config_path))

    @property
    def config_source(self):
        if self._config_path and self._config_path.exists():
            return str(self._config_path)
        return "defaults"

    def get_config_sources(self):
        """Return source metadata for providers config."""
        if self._from_env:
            raw = os.environ.get("PIVOT_WEB_SEARCH_PROVIDERS", "")
            return {"source": "env", "env_var": "PIVOT_WEB_SEARCH_PROVIDERS", "value": raw}
        if self._config_path and self._config_path.exists():
            return {"source": "yaml", "path": str(self._config_path)}
        return {"source": "default"}


# ---------------------------------------------------------------------------
# Proxy config loader (kept sync — reads tiny YAML files from disk)
# ---------------------------------------------------------------------------

_proxies_mtime = 0
_proxies_list = None  # list of proxy URL strings (None = direct)
_proxies_lock = threading.Lock()


def _load_proxies_from_env():
    """Parse PIVOT_WEB_SEARCH_PROXIES env var into a proxy URL list.

    Returns list or None if env var is not set.
    Maps 'direct' → None (no proxy).
    """
    raw = os.environ.get("PIVOT_WEB_SEARCH_PROXIES", "").strip()
    if not raw:
        return None
    result = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.lower() == "direct":
            result.append(None)
        else:
            result.append(entry)
    return result if result else None


def load_proxies(config_path=None):
    """Load proxy list from env var, YAML config, or defaults (in that priority).

    Returns list of proxy URLs (None = direct connection).
    Uses mtime-based caching — cheap to call on every request.
    """
    global _proxies_mtime, _proxies_list

    path = pathlib.Path(config_path) if config_path else _PROXIES_YAML

    with _proxies_lock:
        # Fast path: return cached list if config hasn't changed
        if _proxies_list is not None:
            try:
                if path.exists():
                    mtime = path.stat().st_mtime
                    if mtime <= _proxies_mtime:
                        return _proxies_list
                else:
                    return _proxies_list
            except Exception:
                return _proxies_list

        env_proxies = _load_proxies_from_env()
        if env_proxies is not None:
            _proxies_list = env_proxies
            _proxies_mtime = time.time()
            log(f"Loaded {len(env_proxies)} proxies from env PIVOT_WEB_SEARCH_PROXIES")
            return _proxies_list

        if path.exists():
            try:
                data = _load_yaml(str(path))
                _proxies_mtime = path.stat().st_mtime
                entries = data.get("proxies", [])
                if not isinstance(entries, list):
                    raise ValueError("'proxies' must be a list")
                sorted_entries = sorted(
                    [e for e in entries if e.get("enabled", True)],
                    key=lambda e: e.get("priority", 100),
                )
                result = []
                for e in sorted_entries:
                    url = e.get("url")
                    if url and url.startswith("socks5://"):
                        try:
                            import socks  # noqa: F401
                            result.append(url)
                        except ImportError:
                            log(f"SOCKS5 proxy '{e.get('name', '?')}' skipped — install with: uv pip install pysocks")
                            continue
                    else:
                        result.append(url)
                _proxies_list = result
                log(f"Loaded {len(result)} proxies from {path}")
                return result
            except Exception as e:
                log(f"Failed to load {path}: {e} — using defaults")

        _proxies_list = [e["url"] for e in _DEFAULT_PROXIES if e.get("enabled", True)]
        _proxies_mtime = time.time()
        return _proxies_list


def reload_proxies():
    """Force re-read proxy config."""
    global _proxies_list, _proxies_mtime
    _proxies_list = None
    _proxies_mtime = 0
    return load_proxies()


def get_proxy_config_source():
    """Return source metadata for proxy configuration."""
    raw = os.environ.get("PIVOT_WEB_SEARCH_PROXIES", "").strip()
    if raw:
        return {"source": "env", "env_var": "PIVOT_WEB_SEARCH_PROXIES", "value": raw}
    if _PROXIES_YAML.exists():
        return {"source": "yaml", "path": str(_PROXIES_YAML)}
    return {"source": "default"}


# ---------------------------------------------------------------------------
# Fetch config loader (kept sync — reads tiny YAML files from disk)
# ---------------------------------------------------------------------------

_FETCH_YAML = _PROJECT_ROOT / "config" / "fetch.yaml"
_fetch_config = None
_fetch_config_mtime = 0
_fetch_config_lock = threading.Lock()

_DEFAULT_FETCH_CONFIG = {
    "js_renderer": "none",
    "max_chars": 100_000,
    "empty_threshold": 200,
    "playwright": {"timeout": 30000, "wait_until": "networkidle"},
    "tavily": {"extract_depth": "advanced", "format": "markdown", "timeout": 30},
}


def load_fetch_config(config_path=None):
    """Load fetch config from env var, YAML config, or defaults (in that priority).

    Returns dict with fetch configuration.
    Uses mtime-based caching — cheap to call on every request.
    """
    global _fetch_config_mtime, _fetch_config

    path = pathlib.Path(config_path) if config_path else _FETCH_YAML

    if _fetch_config is not None:
        try:
            if path.exists():
                mtime = path.stat().st_mtime
                if mtime <= _fetch_config_mtime:
                    return _fetch_config
            else:
                return _fetch_config
        except Exception:
            return _fetch_config

    with _fetch_config_lock:
        if _fetch_config is not None:
            return _fetch_config

        env_raw = os.environ.get("PIVOT_WEB_SEARCH_FETCH_CONFIG", "").strip()
        if env_raw:
            try:
                parsed = json.loads(env_raw)
                _fetch_config = {**_DEFAULT_FETCH_CONFIG, **parsed}
                _fetch_config_mtime = time.time()
                return _fetch_config
            except (json.JSONDecodeError, TypeError):
                pass

        if path.exists():
            try:
                data = _load_yaml(str(path))
                _fetch_config_mtime = path.stat().st_mtime
                # Support both `fetch:` nested and top-level flat formats
                loaded = data.get("fetch", {}) if isinstance(data, dict) and "fetch" in data else (data or {})
                _fetch_config = {**_DEFAULT_FETCH_CONFIG, **loaded}
                return _fetch_config
            except Exception as e:
                log(f"Failed to load {path}: {e} — using defaults")

        _fetch_config = dict(_DEFAULT_FETCH_CONFIG)
        _fetch_config_mtime = time.time()
        return _fetch_config


def get_fetch_config_source():
    """Return source metadata for fetch configuration."""
    raw = os.environ.get("PIVOT_WEB_SEARCH_FETCH_CONFIG", "").strip()
    if raw:
        return {"source": "env", "env_var": "PIVOT_WEB_SEARCH_FETCH_CONFIG"}
    if _FETCH_YAML.exists():
        return {"source": "yaml", "path": str(_FETCH_YAML)}
    return {"source": "default"}


def reload_fetch_config():
    """Force re-read fetch config."""
    global _fetch_config, _fetch_config_mtime
    _fetch_config = None
    _fetch_config_mtime = 0
    return load_fetch_config()
