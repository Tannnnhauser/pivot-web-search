"""Config-driven provider registry with mtime-based reload."""

from __future__ import annotations

import pathlib

from ..config import USER_PROVIDERS_YAML, cache_still_valid
from ..config import load_yaml as _load_yaml
from ..defaults import SMART_DEFAULT_PRIORITY
from ..logging import log
from ..validation import _load_env_key
from .adapters import ADAPTER_MAP, DEFAULT_PROVIDERS


def _auto_detect_entries():
    """Build provider list from available API keys. DDG is always enabled."""
    entries = [{"name": "ddg", "type": "ddg", "enabled": True}]
    for default in DEFAULT_PROVIDERS:
        if default["name"] == "ddg":
            continue
        primary = default.get("api_key_env")
        fallback = default.get("api_key_env_fallback")
        has_key = (primary and _load_env_key(primary)) or (fallback and _load_env_key(fallback))
        if has_key:
            entries.append(dict(default))
    return entries


class ProviderRegistry:
    """Manages search providers loaded from user YAML or auto-detection, with mtime-based reload."""

    def __init__(self):
        self._providers = []
        self._config_path = None
        self._config_mtime = 0
        self._auto_detected = False

    def load(self, config_path=None):
        """Load providers from user YAML, falling back to auto-detection from API keys."""
        self._config_path = pathlib.Path(config_path) if config_path else USER_PROVIDERS_YAML
        self._auto_detected = False

        if self._config_path.exists():
            try:
                data = _load_yaml(str(self._config_path))
                self._config_mtime = self._config_path.stat().st_mtime
                entries = data.get("providers", [])
                if not isinstance(entries, list):
                    raise ValueError("'providers' must be a list")
                self._providers = self._build_providers(entries)
                source = str(self._config_path)
            except Exception as e:
                log(f"Failed to load {self._config_path}: {e} — falling back to auto-detect")
                self._providers = self._build_providers(_auto_detect_entries())
                self._auto_detected = True
                source = "auto-detect (config load failed)"
        else:
            self._providers = self._build_providers(_auto_detect_entries())
            self._auto_detected = True
            source = "auto-detect (no user config file)"

        log(f"Loaded {len(self._providers)} providers from {source}")

    def _build_providers(self, entries):
        providers = []
        for entry in entries:
            ptype = entry.get("type", "")
            cls = ADAPTER_MAP.get(ptype)
            if not cls:
                log(f"Unknown provider type '{ptype}' for '{entry.get('name', '?')}', skipping")
                continue
            # gemini is an alias for llm_search with a specific api_format
            if ptype == "gemini" and "api_format" not in entry:
                entry = {**entry, "api_format": "gemini"}
            p = cls(
                name=entry.get("name", ptype),
                priority=entry.get("priority", 100),
                enabled=entry.get("enabled", True),
                config=entry,
            )
            # Preserve type identity for SMART_DEFAULT_PRIORITY / DEFAULT_TIMEOUT
            if ptype != cls.provider_type:
                p.provider_type = ptype
            providers.append(p)
        self._apply_smart_defaults(providers)
        return providers

    def _apply_smart_defaults(self, providers):
        """Assign effective_priority based on provider type when not explicitly set."""
        enabled_types = {p.provider_type for p in providers if p.enabled}
        free_only = enabled_types <= {"ddg", "searxng"} and not any(
            p.config.get("api_key_env") for p in providers if p.enabled
        )

        for i, p in enumerate(providers):
            p._rr_seed = i
            explicit_priority = p.config.get("priority")
            if explicit_priority is not None:
                p._effective_priority = int(explicit_priority)
            elif free_only:
                p._effective_priority = 10
            else:
                p._effective_priority = SMART_DEFAULT_PRIORITY.get(p.provider_type, 50)

    def _check_reload(self):
        """Reload config if file mtime changed (cheap stat check)."""
        if self._auto_detected or not self._config_path:
            return
        if not cache_still_valid(self._config_path, self._config_mtime):
            log("Config changed, reloading providers")
            self.load(str(self._config_path))

    def get_ordered(self):
        """Returns enabled providers sorted by effective_priority (ascending). Auto-reloads on config change."""
        self._check_reload()
        return sorted([p for p in self._providers if p.enabled], key=lambda p: p.effective_priority)

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
        return "auto-detect"

    def get_config_sources(self):
        """Return source metadata for providers config."""
        if self._config_path and self._config_path.exists():
            return {"source": "yaml", "path": str(self._config_path)}
        return {"source": "auto-detect"}
