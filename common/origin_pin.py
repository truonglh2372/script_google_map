#!/usr/bin/env python3
"""Send the crawler's server calls straight to the origin IP, bypassing Cloudflare (AQD_ORIGIN_IP).

Why: the Viettel -> Cloudflare SG route drops packets, so API calls through Cloudflare are slow; the origin answers
fast. See truonglh2372/an_uong_quanh_day#1058.

How it works, like `curl --resolve anuongquanhday.com:443:<ip>`: only the DNS lookup of the AQD_BASE host is replaced
by the IP. The URL, the TLS SNI, the Host header and the certificate check all keep the host name, so the origin's
normal certificate must match it (it does: kamal-proxy serves a Let's Encrypt cert for anuongquanhday.com). TLS
verification is never turned off, and the Authorization header/token is unchanged.

- AQD_ORIGIN_IP empty or unset -> nothing changes (normal DNS through Cloudflare). This is the default.
- AQD_ORIGIN_IP must be one IPv4/IPv6 literal and AQD_BASE must be https, otherwise OriginPinError (a ValueError).
- Only that one host name is pinned; Google, Foody, photo CDNs etc. resolve normally.
- Rollback: delete the AQD_ORIGIN_IP line from creds.env (or the environment) and restart the crawler.
"""
import ipaddress, os, socket, threading
from urllib.parse import urlsplit

ENV = "AQD_ORIGIN_IP"
_REAL_GETADDRINFO = socket.getaddrinfo
_PINS = {}  # lower-case host name -> IP literal
_LOCK = threading.Lock()


class OriginPinError(ValueError):
    pass


def parse(base, ip):
    """(host, ip) to pin, or None when ip is empty. Raises OriginPinError on a bad IP or a non-https base."""
    ip = (ip or "").strip()
    if not ip:
        return None
    try:
        if "%" in ip:  # an IPv6 zone id (fe80::1%en0) is a local-link address, never the origin
            raise ValueError(ip)
        address = ipaddress.ip_address(ip)  # rejects host names and CIDR ranges
    except ValueError:
        raise OriginPinError(f"{ENV} must be a single IP address (for example 203.0.113.10), or empty") from None
    parts = urlsplit((base or "").strip())
    if parts.scheme != "https" or not parts.hostname:
        raise OriginPinError(f"{ENV} only works with an https AQD_BASE, so the certificate is still checked")
    return parts.hostname.lower().rstrip("."), str(address)


def _getaddrinfo(host, *args, **kwargs):
    if isinstance(host, (bytes, bytearray)):
        key = bytes(host).decode("ascii", "ignore")
    else:
        key = host if isinstance(host, str) else ""
    ip = _PINS.get(key.lower().rstrip("."))
    return _REAL_GETADDRINFO(ip if ip else host, *args, **kwargs)


def install(base, ip=None):
    """Pin the host of `base` to `ip` (default: $AQD_ORIGIN_IP) for this process. Returns (host, ip) or None."""
    pin = parse(base, os.environ.get(ENV) if ip is None else ip)
    if pin is None:
        return None
    with _LOCK:
        _PINS[pin[0]] = pin[1]
        if socket.getaddrinfo is not _getaddrinfo:
            socket.getaddrinfo = _getaddrinfo
    return pin


def pinned(host):
    """The IP a host name is pinned to, or None."""
    return _PINS.get((host or "").lower().rstrip("."))


def reset():
    """Drop every pin and restore the real resolver (tests)."""
    with _LOCK:
        _PINS.clear()
        socket.getaddrinfo = _REAL_GETADDRINFO
