"""VPNBook provider: static OpenVPN configs with scraped credentials.

Servers are 6 static OpenVPN endpoints (TCP/443, firewall-friendly).
Configs are downloaded from static URLs; password is scraped from the
VPNBook website and rotates every ~2 weeks.
"""

from __future__ import annotations

import base64
import re

from . import net
from .vpngate import Server

UA = "Mozilla/5.0"

# ponytail: hardcoded server list, update when VPNBook adds/removes servers
_SERVERS = [
    ("us147", "United States", "US"),
    ("ca149", "Canada", "CA"),
    ("de18", "Germany", "DE"),
    ("fr1", "France", "FR"),
    ("uk1", "United Kingdom", "GB"),
    ("uk2", "United Kingdom", "GB"),
]

_CFG_URL = "https://static.vpnbook.com/freevpn/openvpn/{host}-tcp443.ovpn"
_PASS_URL = "https://www.vpnbook.com/freevpn/"

# ponytail: password cache — re-scrape after 1 hour
_cached_pass: str = ""
_cached_pass_ts: float = 0


def _scrape_password(timeout: float = 10.0) -> str:
    """Fetch the current VPNBook password. Cached for 1 hour."""
    import time
    global _cached_pass, _cached_pass_ts
    if _cached_pass and (time.monotonic() - _cached_pass_ts) < 3600:
        return _cached_pass
    html = net.get(_PASS_URL, timeout, headers={"User-Agent": UA}).decode("utf-8", "replace")
    # ponytail: fragile regex, re-check if VPNBook changes HTML
    m = re.search(r"Password:\s*<b>(\w+)</b>", html)
    if not m:
        raise RuntimeError("VPNBook password not found — page structure may have changed")
    _cached_pass = m.group(1)
    _cached_pass_ts = time.monotonic()
    return _cached_pass


def _download_cfg(host: str, timeout: float = 15.0) -> str:
    """Download a .ovpn config from VPNBook's static CDN."""
    return net.get(_CFG_URL.format(host=host), timeout,
                   headers={"User-Agent": UA}).decode("utf-8", "replace")


def _resolve_ip(host: str, timeout: float = 5.0) -> str:
    """Resolve hostname to IP. Falls back to hostname on failure."""
    ips = net.resolve(host, timeout)
    return ips[0] if ips else host


class VPNBook:
    """VPNBook free OpenVPN provider — 6 static servers, TCP/443."""

    name = "VPNBook"

    def fetch(self, timeout: float = 20.0) -> list[Server]:
        password = _scrape_password(timeout)
        servers: list[Server] = []
        for short, country, cc in _SERVERS:
            try:
                cfg = _download_cfg(short, timeout)
            except Exception:  # noqa: BLE001 — skip unreachable/blocked servers
                continue
            cfg += f"\nauth-user-pass\n{password}\n"
            b64 = base64.b64encode(cfg.encode()).decode()
            ip = _resolve_ip(f"{short}.vpnbook.com")
            servers.append(Server(
                host=f"{short}.vpnbook.com", ip=ip,
                score=500000, ping=0, speed=50_000_000,
                country=country, cc=cc, sessions=0,
                config_b64=b64, proto="tcp", port=443))
        return servers

    def available(self) -> bool:
        return bool(net.resolve("www.vpnbook.com"))
