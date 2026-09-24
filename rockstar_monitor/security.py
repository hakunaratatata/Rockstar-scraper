from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit


def normalize_public_url(url: str, allowed_hosts: tuple[str, ...]) -> str | None:
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (ValueError, AttributeError):
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    if port not in (None, 443) or not host_allowed(host, allowed_hosts) or _is_ip(host) or host == "localhost" or host.endswith(".local"):
        return None
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(normalized == item or normalized.endswith("." + item) for item in allowed_hosts)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
