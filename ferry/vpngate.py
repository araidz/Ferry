"""VPN Gate free relay list: fetch the iPhone CSV, parse the OpenVPN servers.

Wire format (pinned against a live sample — tests/fixtures/vpngate_sample.csv):

    *vpn_servers
    #HostName,IP,Score,Ping,Speed,CountryLong,CountryShort,NumVpnSessions,
     Uptime,TotalUsers,TotalTraffic,LogType,Operator,Message,OpenVPN_ConfigData_Base64
    <row>...
    *

Only rows whose last column (a base64 .ovpn, inline ca/cert/key, no user/pass)
is non-empty are usable; the rest are L2TP/SSTP-only.
"""

from __future__ import annotations

import base64
import csv
import json
import urllib.request
from dataclasses import asdict, dataclass

from . import STATE_DIR

API = "https://www.vpngate.net/api/iphone/"
UA = "Mozilla/5.0"  # vpngate serves an empty body to a blank User-Agent
CACHE = STATE_DIR / "servers.json"


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

    def config(self) -> str:
        return base64.b64decode(self.config_b64).decode("utf-8", "replace")


def _int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return 0


def parse(text: str) -> list[Server]:
    rows = [ln for ln in text.splitlines() if ln and not ln.startswith(("*", "#"))]
    out: list[Server] = []
    for f in csv.reader(rows):
        if len(f) < 15 or not f[14].strip():
            continue
        out.append(Server(f[0], f[1], _int(f[2]), _int(f[3]), _int(f[4]),
                           f[5], f[6], _int(f[7]), f[14]))
    return out


def fetch(timeout: float = 20.0) -> list[Server]:
    req = urllib.request.Request(API, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse(r.read().decode("utf-8", "replace"))


def load_cache() -> list[Server]:
    try:
        return [Server(**d) for d in json.loads(CACHE.read_text())]
    except (OSError, ValueError, TypeError):
        return []


def save_cache(servers: list[Server]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps([asdict(s) for s in servers]))
    except OSError:
        pass
