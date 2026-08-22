"""Runtime configuration inspection shared by MCP and CLI."""

from __future__ import annotations

import urllib.parse

from . import quota as _quota
from .config import (
    get_fetch_config_source,
    get_proxy_config_source,
    load_proxies,
    reload_fetch_config,
    reload_proxies,
)
from .providers import ProviderRegistry
from .routing import CircuitBreaker


def _redact_proxy_url(url: str | None) -> str | None:
    if url is None:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        return urllib.parse.urlunparse(parsed._replace(netloc=netloc))
    return url


class ConfigServiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ConfigService:
    """Inspect or reload provider, proxy, fetch, quota, and breaker state."""

    def __init__(self, registry: ProviderRegistry, breaker: CircuitBreaker):
        self.registry = registry
        self.breaker = breaker

    async def execute(self, action: str = "status") -> dict:
        if action == "reload":
            return self.reload()
        if action == "status":
            return await self.status()
        raise ConfigServiceError("INVALID_REQUEST", f"Unsupported config action: {action}")

    def reload(self) -> dict:
        self.registry.reload()
        proxies = reload_proxies()
        fetch_config = reload_fetch_config()
        self.breaker.reset_all()
        return {
            "action": "reload",
            "providers_loaded": len(self.registry.get_all()),
            "proxies_loaded": len(proxies),
            "fetch_config_loaded": bool(fetch_config),
            "providers_config": self.registry.config_source,
            "breaker": "reset",
        }

    async def status(self) -> dict:
        quota_summary = _quota.get_quota_summary()
        provider_info = []
        for provider in sorted(self.registry.get_all(), key=lambda item: item.effective_priority):
            available, detail = await provider.health_check()
            info = {
                "name": provider.name,
                "type": provider.provider_type,
                "priority": provider.effective_priority,
                "affinity": provider.affinity,
                "timeout": provider.timeout_seconds,
                "enabled": provider.enabled,
                "available": available,
                "detail": detail,
                "breaker": self.breaker.get_status(provider.name),
            }
            if provider.name in quota_summary:
                info["quota"] = quota_summary[provider.name]
            provider_info.append(info)

        proxies = load_proxies()
        proxy_info = []
        for proxy in proxies:
            redacted = _redact_proxy_url(proxy)
            proxy_info.append({"url": redacted, "label": redacted or "direct"})

        return {
            "action": "status",
            "providers_config": self.registry.config_source,
            "config_sources": {
                "providers": self.registry.get_config_sources(),
                "proxies": get_proxy_config_source(),
                "fetch": get_fetch_config_source(),
            },
            "providers": provider_info,
            "proxies": proxy_info,
        }
