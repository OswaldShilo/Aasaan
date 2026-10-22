"""
Client IP extraction and rate limit keying with configurable proxy hop trust.

On Azure Container Apps (ACA) and reverse proxies (e.g. Caddy), the ingress proxy
appends the verified remote client IP to the right end of the X-Forwarded-For header.
Any values to the left may be client-supplied and cannot be trusted.

AASAAN_TRUSTED_PROXY_HOPS:
  - 0: Trust no proxy headers; use socket peer address directly.
  - N >= 1: Take the N-th entry from the RIGHT of X-Forwarded-For.
  Default: 1 (appropriate for ACA ingress or Caddy in Docker Compose).
"""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import Optional
from starlette.requests import Request

log_ = logging.getLogger("aasaan.client_ip")


def _parse_candidate(raw: str) -> Optional[str]:
    """Parse and validate an IP string which may include ports or brackets.

    Returns normalized canonical IP string if valid, else None.
    Never raises exceptions.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().strip("\"'")
    if not s or len(s) > 100:
        return None

    # Handle bracketed IPv6, e.g. [::1]:80, [::1], [2001:db8::1]:443
    if s.startswith("["):
        close_idx = s.find("]")
        if close_idx != -1:
            candidate = s[1:close_idx]
            remainder = s[close_idx + 1 :]
            if remainder:
                if not remainder.startswith(":"):
                    return None
                port_str = remainder[1:]
                if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
                    return None
            try:
                ip = ipaddress.ip_address(candidate)
                return str(ip)
            except ValueError:
                return None
        return None

    # Handle host:port (IPv4 with port, or bare IPv6)
    if ":" in s:
        # Exactly one colon means IPv4 with port (e.g. 1.2.3.4:8080)
        if s.count(":") == 1:
            host, port_str = s.split(":", 1)
            if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
                return None
            try:
                ip = ipaddress.ip_address(host)
                if ip.version == 4:
                    return str(ip)
            except ValueError:
                return None
        # Multiple colons: test if it's a bare IPv6 address (e.g. 2001:db8::1)
        try:
            ip = ipaddress.ip_address(s)
            return str(ip)
        except ValueError:
            return None

    # Plain IPv4
    try:
        ip = ipaddress.ip_address(s)
        return str(ip)
    except ValueError:
        return None


def get_trusted_proxy_hops() -> int:
    """Read AASAAN_TRUSTED_PROXY_HOPS from environment (default: 1)."""
    val = os.environ.get("AASAAN_TRUSTED_PROXY_HOPS", "1").strip()
    try:
        hops = int(val)
        return hops if hops >= 0 else 1
    except ValueError:
        return 1


def get_client_ip(request: Request) -> str:
    """Extract and validate client IP address according to AASAAN_TRUSTED_PROXY_HOPS.

    Returns validated canonical IP address or socket peer host.
    Never raises exceptions.
    """
    peer = ""
    if request.client and request.client.host:
        peer = request.client.host

    hops = get_trusted_proxy_hops()
    if hops == 0:
        return peer or "127.0.0.1"

    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return peer or "127.0.0.1"

    entries = [p.strip() for p in xff.split(",") if p.strip()]
    if len(entries) < hops:
        return peer or "127.0.0.1"

    # Take the N-th entry from the RIGHT
    candidate_raw = entries[-hops]
    validated = _parse_candidate(candidate_raw)
    if validated is not None:
        return validated

    return peer or "127.0.0.1"


def get_rate_limit_key(request: Request) -> str:
    """Derive rate-limit bucket key for request.

    IPv4 addresses are bucketed per IP.
    IPv6 addresses are bucketed per /64 prefix to prevent prefix-rotation bypass.
    """
    ip_str = get_client_ip(request)
    try:
        addr = ipaddress.ip_address(ip_str)
        if addr.version == 6:
            net = ipaddress.IPv6Network((addr, 64), strict=False)
            return f"{net.network_address}/64"
        return str(addr)
    except ValueError:
        # Non-IP peer fallback in tests (e.g. 'testclient')
        return ip_str
