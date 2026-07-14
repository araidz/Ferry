"""VPN Gate free relay list: fetch the iPhone CSV, parse the OpenVPN servers.

Wire format (pinned against a live sample — tests/fixtures/vpngate_sample.csv):

    *vpn_servers
    #HostName,IP,Score,Ping,Speed,CountryLong,CountryShort,NumVpnSessions,
     Uptime,TotalUsers,TotalTraffic,LogType,Operator,Message,OpenVPN_ConfigData_Base64
    <row>...
    *

Only rows whose last column (a base64 .ovpn, inline ca/cert/key, no user/pass)
is non-empty are usable; the rest are L2TP/SSTP-only. The transport (proto/port)
lives inside that config, so we decode it once at parse time — restrictive
networks only pass a few ports, and the UI needs the port to steer the user.
"""

from __future__ import annotations

import base64
import csv
import json
from dataclasses import asdict, dataclass

from . import STATE_DIR, net

API = "https://www.vpngate.net/api/iphone/"
UA = "Mozilla/5.0"  # vpngate serves an empty body to a blank User-Agent
CACHE = STATE_DIR / "servers.json"
FRIENDLY_PORTS = {443, 992, 995}  # HTTPS/POP3S-lookalikes — pass most firewalls


@dataclass
class Server:
    host: str
    ip: str
    score: int  # higher is better
    ping: int  # ms, 0 = unknown
    speed: int  # bits/s
    country: str  # long name, e.g. "Korea Republic of"
    cc: str  # 2-letter, e.g. "KR"
    sessions: int
    config_b64: str
    proto: str = "tcp"  # from the config's `proto` line
    port: int = 0  # from the config's `remote <ip> <port>` line

    def config(self) -> str:
        return base64.b64decode(self.config_b64).decode("utf-8", "replace")

    @property
    def friendly(self) -> bool:
        """Port a locked-down network is likely to let through."""
        return self.port in FRIENDLY_PORTS

    @property
    def transport(self) -> str:
        return f"{self.proto}:{self.port}" if self.port else self.proto


def _int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return 0


def _remote(cfg: str) -> tuple[str, int]:
    proto, port = "tcp", 0
    for ln in cfg.splitlines():
        p = ln.split()
        if len(p) >= 2 and p[0] == "proto":
            proto = "udp" if "udp" in p[1] else "tcp"
        elif len(p) >= 3 and p[0] == "remote":
            port = _int(p[2])
    return proto, port


def parse(text: str) -> list[Server]:
    rows = [ln for ln in text.splitlines() if ln and not ln.startswith(("*", "#"))]
    out: list[Server] = []
    for f in csv.reader(rows):
        if len(f) < 15 or not f[14].strip():
            continue
        s = Server(f[0], f[1], _int(f[2]), _int(f[3]), _int(f[4]),
                   f[5], f[6], _int(f[7]), f[14])
        s.proto, s.port = _remote(s.config())
        out.append(s)
    return out


def fetch(timeout: float = 20.0) -> list[Server]:
    return parse(net.get(API, timeout, headers={"User-Agent": UA}).decode("utf-8", "replace"))


def load_cache() -> list[Server]:
    try:
        # tolerate older caches missing keys; drop unknown keys defensively
        rows = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return []
    fields = Server.__dataclass_fields__.keys()
    out: list[Server] = []
    for d in rows:
        try:
            s = Server(**{k: v for k, v in d.items() if k in fields})
        except TypeError:
            continue
        if not s.port:  # old cache had no transport — recover it from the config
            s.proto, s.port = _remote(s.config())
        out.append(s)
    return out


def save_cache(servers: list[Server]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps([asdict(s) for s in servers]))
    except OSError:
        pass


class VPNGate:
    """VPN Gate free relay provider — 3,200+ volunteer OpenVPN servers."""

    name = "VPN Gate"

    def fetch(self, timeout: float = 20.0) -> list[Server]:
        return fetch(timeout)

    def available(self) -> bool:
        return bool(net.resolve("www.vpngate.net"))


def composite_score(s: Server) -> float:
    """Higher = better. Combines score, speed, ping, port friendliness."""
    score_norm = s.score / 1e6
    speed_norm = min(s.speed / 1e9, 1.0)
    ping_norm = 1.0 - min((s.ping or 500) / 500, 1.0)
    port_bonus = 0.2 if s.friendly else 0
    return score_norm + speed_norm + ping_norm + port_bonus
