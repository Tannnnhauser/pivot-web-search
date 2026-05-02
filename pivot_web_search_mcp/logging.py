"""Logging utility for Pivot Web Search MCP plugin.

Always emits to stderr (required by MCP stdio transport).
When PIVOT_WEB_SEARCH_DEBUG=1, also appends to ~/.cache/pivot-web-search/server.log.
"""

import os
import pathlib
import sys
from datetime import datetime, timezone

_DEBUG = os.environ.get("PIVOT_WEB_SEARCH_DEBUG", "").strip() == "1"
_LOG_FILE = pathlib.Path.home() / ".cache" / "pivot-web-search" / "server.log"
_log_fh = None


def _get_log_fh():
    global _log_fh
    if _log_fh is None:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _log_fh = open(_LOG_FILE, "a", buffering=1)
    return _log_fh


def log(msg: str) -> None:
    """Write a [pivot-web-search] prefixed message to stderr. If DEBUG, also to log file."""
    line = f"[pivot-web-search] {msg}"
    print(line, file=sys.stderr)
    if _DEBUG:
        fh = _get_log_fh()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fh.write(f"{ts} {line}\n")


def debug(msg: str) -> None:
    """Write only when PIVOT_WEB_SEARCH_DEBUG=1."""
    if _DEBUG:
        log(msg)
