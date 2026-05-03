"""URL validation, content-type detection, and API key loaders."""

import ipaddress
import os
import socket
import urllib.parse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FETCH_BYTES = 10 * 1024 * 1024  # 10 MB download cap
MAX_CONTENT_CHARS = 100_000  # 100K chars markdown truncation
BINARY_CONTENT_TYPES = {"image/", "audio/", "video/", "application/octet-stream",
                        "application/pdf", "application/zip", "application/gzip"}

# ---------------------------------------------------------------------------
# Key loaders
# ---------------------------------------------------------------------------


def _load_tavily_key():
    return os.environ.get("TAVILY_API_KEY", "").strip() or None


def _load_brave_key():
    return os.environ.get("BRAVE_API_KEY", "").strip() or None


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def validate_url(url):
    """Validate and normalize a URL for fetching. Returns normalized URL or raises ValueError."""
    if len(url) > 2000:
        raise ValueError(f"URL too long ({len(url)} chars, max 2000)")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme '{parsed.scheme}' not allowed (only http/https)")
    if parsed.username or parsed.password:
        raise ValueError("URLs with credentials (username/password) are not allowed")
    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("URL has no hostname")
    if "." not in hostname:
        raise ValueError(f"Invalid hostname '{hostname}' (must have at least two segments)")
    # SSRF protection: block private/reserved IP ranges
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
                raise ValueError(f"URL resolves to private/reserved IP ({ip})")
    except socket.gaierror:
        pass  # DNS resolution failure; let the request fail naturally later
    except ValueError as e:
        if "private" in str(e) or "reserved" in str(e):
            raise
    # Auto-upgrade http to https
    if parsed.scheme == "http":
        url = "https" + url[4:]
    return url


def _is_binary_content_type(ct):
    """Check if a content-type header indicates binary content."""
    if not ct:
        return False
    ct = ct.lower().split(";")[0].strip()
    return any(ct.startswith(b) for b in BINARY_CONTENT_TYPES)
