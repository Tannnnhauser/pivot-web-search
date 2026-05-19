"""Configuration loaders for proxies, fetch, and the project YAML root.

Reads tiny YAML files from disk with mtime-based caching so callers can
hit these on every request cheaply. Env vars take precedence over YAML;
YAML over defaults.
"""

import json
import os
import pathlib
import threading
import time

from .logging import log

# ---------------------------------------------------------------------------
# Paths and YAML loader
# ---------------------------------------------------------------------------

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVIDERS_YAML = PROJECT_ROOT / "config" / "providers.yaml"
PROXIES_YAML = PROJECT_ROOT / "config" / "proxies.yaml"
FETCH_YAML = PROJECT_ROOT / "config" / "fetch.yaml"

try:
    import yaml  # noqa: F401
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


def load_yaml(path):
    """Load a YAML file. Raises RuntimeError if PyYAML is missing."""
    if not _HAS_YAML:
        raise RuntimeError(f"PyYAML is required to load {path} — run: uv sync")
    import yaml as _yaml
    with open(path, "r") as f:
        return _yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Proxies
# ---------------------------------------------------------------------------

_DEFAULT_PROXIES = [
    {"name": "direct", "url": None, "enabled": True, "priority": 1},
    {"name": "myproxy1", "url": "http://myproxy1.example:8080", "enabled": False, "priority": 2},
    {"name": "myproxy2", "url": "http://myproxy2.example:8080", "enabled": False, "priority": 3},
]

_proxies_mtime = 0
_proxies_list = None
_proxies_lock = threading.Lock()


def _load_proxies_from_env():
    """Parse PIVOT_WEB_SEARCH_PROXIES into a proxy URL list (None = direct)."""
    raw = os.environ.get("PIVOT_WEB_SEARCH_PROXIES", "").strip()
    if not raw:
        return None
    result = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        result.append(None if entry.lower() == "direct" else entry)
    return result if result else None


def load_proxies(config_path=None):
    """Load proxy list from env, YAML, or defaults (priority order)."""
    global _proxies_mtime, _proxies_list

    path = pathlib.Path(config_path) if config_path else PROXIES_YAML

    with _proxies_lock:
        if _proxies_list is not None:
            try:
                if path.exists():
                    if path.stat().st_mtime <= _proxies_mtime:
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
                data = load_yaml(str(path))
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
                            import socks  # type: ignore[import-not-found]  # noqa: F401
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
    raw = os.environ.get("PIVOT_WEB_SEARCH_PROXIES", "").strip()
    if raw:
        return {"source": "env", "env_var": "PIVOT_WEB_SEARCH_PROXIES", "value": raw}
    if PROXIES_YAML.exists():
        return {"source": "yaml", "path": str(PROXIES_YAML)}
    return {"source": "default"}


# ---------------------------------------------------------------------------
# Fetch config
# ---------------------------------------------------------------------------

_DEFAULT_FETCH_CONFIG = {
    "js_renderer": "none",
    "max_chars": 100_000,
    "empty_threshold": 200,
    "playwright": {"timeout": 30000, "wait_until": "networkidle"},
    "tavily": {"extract_depth": "advanced", "format": "markdown", "timeout": 30},
}

_fetch_config = None
_fetch_config_mtime = 0
_fetch_config_lock = threading.Lock()


def load_fetch_config(config_path=None):
    """Load fetch config from env, YAML, or defaults (priority order)."""
    global _fetch_config_mtime, _fetch_config

    path = pathlib.Path(config_path) if config_path else FETCH_YAML

    if _fetch_config is not None:
        try:
            if path.exists():
                if path.stat().st_mtime <= _fetch_config_mtime:
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
                data = load_yaml(str(path))
                _fetch_config_mtime = path.stat().st_mtime
                loaded = data.get("fetch", {}) if isinstance(data, dict) and "fetch" in data else (data or {})
                _fetch_config = {**_DEFAULT_FETCH_CONFIG, **loaded}
                return _fetch_config
            except Exception as e:
                log(f"Failed to load {path}: {e} — using defaults")

        _fetch_config = dict(_DEFAULT_FETCH_CONFIG)
        _fetch_config_mtime = time.time()
        return _fetch_config


def reload_fetch_config():
    """Force re-read fetch config."""
    global _fetch_config, _fetch_config_mtime
    _fetch_config = None
    _fetch_config_mtime = 0
    return load_fetch_config()


def get_fetch_config_source():
    raw = os.environ.get("PIVOT_WEB_SEARCH_FETCH_CONFIG", "").strip()
    if raw:
        return {"source": "env", "env_var": "PIVOT_WEB_SEARCH_FETCH_CONFIG"}
    if FETCH_YAML.exists():
        return {"source": "yaml", "path": str(FETCH_YAML)}
    return {"source": "default"}
