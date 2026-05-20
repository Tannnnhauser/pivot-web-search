"""Concrete search-provider adapters and the type → class registry."""

from __future__ import annotations

import json
import re
import urllib.parse

import httpx

from .. import quota
from ..backends import search_brave, search_ddg, search_tavily
from ..http_client import _get_client, _open_with_fallback
from ..logging import log
from ..validation import _load_env_key
from .base import SearchProvider, SearchResult


class DdgProvider(SearchProvider):
    provider_type = "ddg"

    async def search(self, query, max_results=5, **kwargs):
        results = await search_ddg(
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

            client = await _get_client()
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
        return _load_env_key(env_var)

    async def search(self, query, max_results=5, **kwargs):
        topic = kwargs.get("topic")
        if topic is None:
            topic = "news" if kwargs.get("news") else "general"
        tv = await search_tavily(
            query, max_results,
            include_answer=kwargs.get("include_answer", False),
            search_depth=kwargs.get("search_depth", "basic"),
            topic=topic,
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
        return _load_env_key(env_var)

    async def search(self, query, max_results=5, **kwargs):
        rv = await search_brave(query, max_results)
        if rv is None:
            return None
        results, resp_headers = rv
        quota.update_from_brave_headers(resp_headers)
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
        from ..llm_search_formats import FORMAT_REGISTRY
        fmt_name = self.config.get("api_format", "chat_completions")
        fmt_cls = FORMAT_REGISTRY.get(fmt_name)
        if not fmt_cls:
            log(f"{name}: unknown api_format '{fmt_name}', falling back to chat_completions")
            from ..llm_search_formats import ChatCompletionsFormat
            fmt_cls = ChatCompletionsFormat
        self._format = fmt_cls()

    def _get_key(self):
        env_var = self.config.get("api_key_env")
        if not env_var:
            return None
        key = _load_env_key(env_var)
        if not key:
            fallback = self.config.get("api_key_env_fallback")
            if fallback:
                key = _load_env_key(fallback)
        return key

    async def search(self, query, max_results=5, **kwargs):
        api_key = self._get_key()

        if self.config.get("api_key_env") and not api_key:
            log(f"{self.name}: api_key_env set but key not found, skipping")
            return None

        endpoint, headers, body = self._format.build_request(query, max_results, self.config)

        if not endpoint:
            log(f"{self.name}: no endpoint configured")
            return None

        self._prepare_auth(api_key, headers)
        return await self._execute_request(endpoint, headers, body, max_results)

    def _prepare_auth(self, api_key, headers):
        """Inject auth into headers based on the format's auth_style."""
        if not api_key:
            return
        style = getattr(self._format, "auth_style", "bearer")
        if style == "x-goog-api-key":
            headers["x-goog-api-key"] = api_key
        elif "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {api_key}"

    async def _execute_request(self, endpoint, headers, body, max_results):
        """Shared HTTP dispatch + response parsing."""
        timeout = self.config.get("timeout", 30)

        try:
            resp = await _open_with_fallback(
                "POST", endpoint, headers=headers, data=body, timeout=timeout)
        except httpx.HTTPError as e:
            log(f"{self.name} request failed: {e}")
            return None

        if resp.status_code == 429 and resp.headers.get("Retry-After"):
            quota.mark_rate_limited(self.name, resp.headers.get("Retry-After"))
            log(f"{self.name} HTTP 429: rate limited")
            return None

        if resp.status_code >= 400:
            try:
                err_obj = resp.json()
            except ValueError:
                err_obj = {}
            msg = self._format.parse_error(resp.status_code, err_obj)
            log(f"{self.name} HTTP {resp.status_code}: {msg}")
            return None

        try:
            obj = resp.json()
        except ValueError as e:
            log(f"{self.name} response not JSON: {e}")
            return None

        results, answer = self._format.parse_response(obj, max_results, self.name)
        if not results:
            return None

        return SearchResult(results=results, provider=self.name, answer=answer)

    async def health_check(self):
        endpoint = self._format.resolve_endpoint(self.config)
        if not endpoint:
            return False, "no endpoint"
        key = self._get_key()
        if self.config.get("api_key_env") and not key:
            return False, "no API key"
        return True, None


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
            resp = await _open_with_fallback(
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
            client = await _get_client()
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
        return _load_env_key(env_var)

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
            qs = urllib.parse.urlencode(params) if isinstance(params, dict) else str(params)
            endpoint = f"{endpoint}?{qs}"

        try:
            resp = await _open_with_fallback(
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


ADAPTER_MAP = {
    "ddg": DdgProvider,
    "tavily": TavilyProvider,
    "brave": BraveProvider,
    "gemini": LlmSearchProvider,
    "searxng": SearxngProvider,
    "json_api": JsonApiProvider,
    "llm_search": LlmSearchProvider,
}

DEFAULT_PROVIDERS = [
    {"name": "ddg", "type": "ddg", "enabled": True},
    {"name": "tavily", "type": "tavily", "enabled": True, "api_key_env": "TAVILY_API_KEY"},
    {"name": "brave", "type": "brave", "enabled": True, "api_key_env": "BRAVE_API_KEY"},
    {
        "name": "gemini", "type": "gemini", "enabled": True,
        "api_format": "gemini",
        "api_key_env": "GEMINI_SEARCH_API_KEY",
        "api_key_env_fallback": "GOOGLE_STUDIO_API_KEY",
    },
]
