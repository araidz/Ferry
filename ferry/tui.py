"""Raw-ANSI terminal UI: theme, state, render, input. No third-party deps.

Full-redraw renderer (truecolor) in Trawl's look — gradient logo header, a
country rail beside a servers panel, a connection status view, footer hints.
The run loop lives in __main__.py; the ANSI/width/terminal primitives are
lifted verbatim from Trawl (proven, and shared house style).
"""

from __future__ import annotations

import json
import contextlib
import os
import re
import select
import shutil
import sys
import termios
import time
import tty
import unicodedata

from . import STATE_DIR, __version__, vpn
from .vpngate import Server
from . import security

# -- theme (Trawl's violet palette; wake motif instead of the net) -----------

ACCENT = "#a78bfa"
TEXT = "#e9e4f5"
ALT = "#b9a7e6"
GOOD = "#86d6a2"
WARN = "#f0c560"
BAD = "#ee7d92"
BRIGHT = "#d8b4fe"
RULE = "#6b6577"
DEEP = "#7c5cd6"
SHADE = "#4c3a8a"
WHITE = "#ffffff"

PTR = "❯"
STAR = "★"
DOT = "·"
ON = "●"
OFF = "○"
BUSY = "◌"

# ferry wordmark + a bow wake (gradient on the word, aqua on the wake)
LOGO_LINES = [
    "█▀▀ █▀▀ █▀▄ █▀▄ █ █   ~~~≈>",
    "█▀  █▄▄ █▀▄ █▀▄ ▀▄▀   <≈~~~",
]
WAKE_GLYPHS = set("~≈<>")
WAKE_COLOR = "#5fd0c5"  # aqua — reads as water against the violet

STATE_FILE = STATE_DIR / "state.json"
SORTS = {"score": lambda s: -s.score, "ping": lambda s: (s.ping or 99999),
         "speed": lambda s: -s.speed}
SORT_ORDER = ["score", "ping", "speed"]

MARGIN = 2

# -- ANSI + width primitives (verbatim from Trawl) ---------------------------

RESET = "\x1b[0m"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _fg(hexc: str) -> str:
    n = int(hexc[1:], 16)
    return f"\x1b[38;2;{(n >> 16) & 255};{(n >> 8) & 255};{n & 255}m"


def style(text: str, color: str | None = None, bold: bool = False, dim: bool = False) -> str:
    pre = ("\x1b[1m" if bold else "") + ("\x1b[2m" if dim else "") + (_fg(color) if color else "")
    return f"{pre}{text}{RESET}" if pre else text


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def _cw(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def dwidth(s: str) -> int:
    return sum(_cw(c) for c in s)


def dtrunc(s: str, maxw: int) -> str:
    if maxw <= 0:
        return ""
    if dwidth(s) <= maxw:
        return s
    out, w = "", 0
    for ch in s:
        cw = _cw(ch)
        if w + cw > maxw - 1:
            break
        out += ch
        w += cw
    return out + "…"


def pad(s: str, w: int, align: str = "left") -> str:
    gap = w - dwidth(s)
    if gap <= 0:
        return s
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def cell(text: str, w: int, align: str = "left", color: str | None = None,
         bold: bool = False, dim: bool = False) -> str:
    if w <= 0:
        return ""
    return style(pad(dtrunc(text, w), w, align), color, bold, dim)


# -- color math + logo (from Trawl's theme) ----------------------------------


def _rgb(h: str) -> tuple[int, int, int]:
    n = int(h[1:], 16)
    return (n >> 16) & 255, (n >> 8) & 255, n & 255


def _lerp(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    c = lambda x, y: round(x + (y - x) * t)  # noqa: E731
    return f"#{c(ar, br):02x}{c(ag, bg):02x}{c(ab, bb):02x}"


def _logo_color(t: float) -> str:
    if t < 0.15:
        return _lerp(WHITE, BRIGHT, t / 0.15)
    if t < 0.4:
        return _lerp(BRIGHT, ACCENT, (t - 0.15) / 0.25)
    if t < 0.7:
        return _lerp(ACCENT, DEEP, (t - 0.4) / 0.3)
    return _lerp(DEEP, SHADE, (t - 0.7) / 0.3)


def _logo_lines() -> list[str]:
    out = []
    rows = len(LOGO_LINES)
    for row, line in enumerate(LOGO_LINES):
        chars = list(line)
        last = max(1, len(chars) - 1)
        ty = row / max(1, rows - 1)
        seg = ""
        for i, ch in enumerate(chars):
            if ch == " ":
                seg += " "
            elif ch in WAKE_GLYPHS:
                seg += style(ch, WAKE_COLOR, bold=True)
            else:
                seg += style(ch, _logo_color(((i / last) + ty) / 2), bold=True)
        out.append(seg)
    return out


LOGO = _logo_lines()  # cached once — the logo never changes


def _window(sel: int, total: int, h: int) -> int:
    if total <= h:
        return 0
    return max(0, min(sel - h // 2, total - h))


# -- formatters --------------------------------------------------------------


def fmt_ping(ms: int) -> str:
    return f"{ms} ms" if ms else "-"


def fmt_speed(bps: int) -> str:
    if bps <= 0:
        return "-"
    mbps = bps / 1e6
    return f"{mbps:.1f} Mbps" if mbps < 10 else f"{mbps:.0f} Mbps"


def fmt_uptime(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# -- key parsing + terminal (verbatim from Trawl) ----------------------------

_ARROWS = {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}


def parse_keys(data: bytes) -> list[str]:
    keys: list[str] = []
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x1b:
            if i + 2 < n and data[i + 1] in (ord("["), ord("O")) and bytes([data[i + 2]]) in _ARROWS:
                keys.append(_ARROWS[bytes([data[i + 2]])])
                i += 3
            else:
                keys.append("esc")
                i += 1
        elif b in (0x0d, 0x0a):
            keys.append("enter")
            i += 1
        elif b in (0x7f, 0x08):
            keys.append("backspace")
            i += 1
        elif b == 0x09:
            keys.append("tab")
            i += 1
        elif b == 0x03:
            keys.append("ctrl-c")
            i += 1
        elif b < 0x20:
            i += 1
        else:
            j = i
            while j < n and data[j] >= 0x20 and data[j] != 0x1b:
                j += 1
            keys.extend(data[i:j].decode("utf-8", "ignore"))
            i = j
    return keys


class Terminal:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = None

    def enter(self) -> None:
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[3J\x1b[2J\x1b[H\x1b[?25l")
        sys.stdout.flush()

    def leave(self) -> None:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if self.saved:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def suspend(self):
        """Context manager: drop to the normal terminal for a sudo prompt, then
        restore the TUI."""
        return _Suspend(self)

    def size(self) -> tuple[int, int]:
        s = shutil.get_terminal_size((100, 30))
        return s.columns, s.lines

    def read_keys(self, timeout: float) -> list[str]:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            data = os.read(self.fd, 4096)
        except OSError:
            return []
        return parse_keys(data)

    def write(self, lines: list[str]) -> None:
        buf = ["\x1b[H"]
        for i, ln in enumerate(lines):
            buf.append(ln + "\x1b[K")
            if i < len(lines) - 1:
                buf.append("\r\n")
        buf.append("\x1b[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()


class _Suspend:
    def __init__(self, term: Terminal):
        self.term = term

    def __enter__(self):
        self.term.leave()
        return self.term

    def __exit__(self, *exc):
        self.term.enter()
        return False


# -- app state ---------------------------------------------------------------


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


class App:
    def __init__(self, servers: list[Server], load_error: str | None = None):
        self.servers = servers
        self.running = True
        self.help = False
        self.status = load_error or ""
        st = _load_state()
        self.favorites: set[str] = set(st.get("favorites", []))
        self.sort = st.get("sort") if st.get("sort") in SORTS else "score"
        self.autoreconnect = bool(st.get("autoreconnect", True))
        self.provider_i = st.get("provider_i", 0)
        self.engine = vpn.Engine(st.get("engine", "openvpn"))
        self.killswitch = bool(st.get("killswitch", False))
        self.dns_protect = bool(st.get("dns_protect", False))
        self._dns_service: str | None = None
        # connection
        self.conn = "idle"  # idle | connecting | connected | failed
        self.active: Server | None = None
        self.connect_start = 0.0
        self.exit_info: tuple[str, str] | None = None
        self.user_disconnected = False
        self.candidates: list[Server] = []  # failover queue for the current connect
        self.cand_i = 0
        # browse navigation
        self.focus = "countries"
        self.csel = 0
        self.ssel = 0
        self._last_refresh = 0.0
        self._cached_pool: list[Server] = []
        self._cached_pool_key: tuple[int, str, int] = (-1, "", -1)
        self._fav_gen = 0
        self._rebuild_countries()

    # -- derived data --------------------------------------------------------

    def _rebuild_countries(self) -> None:
        counts: dict[tuple[str, str], int] = {}
        for s in self.servers:
            counts[(s.cc, s.country)] = counts.get((s.cc, s.country), 0) + 1
        self.countries = sorted(counts.items(), key=lambda kv: kv[0][1])  # by long name
        self.csel = min(self.csel, len(self.countries))  # index 0 = Favorites row

    def _rail_rows(self) -> list[tuple[str, int]]:
        rows = [("Favorites", len(self.favorites))]
        rows += [(name, n) for (cc, name), n in self.countries]
        return rows

    def current_servers(self) -> list[Server]:
        fgen = self._fav_gen
        key = (self.csel, self.sort, fgen)
        if self._cached_pool_key == key:
            return self._cached_pool
        if self.csel == 0:  # Favorites row
            pool = [s for s in self.servers if s.host in self.favorites]
        else:
            (cc, name), _ = self.countries[self.csel - 1]
            pool = [s for s in self.servers if s.cc == cc]
        self._cached_pool = sorted(pool, key=SORTS[self.sort])
        self._cached_pool_key = key
        return self._cached_pool

    def current_server(self) -> Server | None:
        pool = self.current_servers()
        return pool[self.ssel] if pool and 0 <= self.ssel < len(pool) else None

    # -- persistence ---------------------------------------------------------

    def save(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({
                "favorites": sorted(self.favorites), "sort": self.sort,
                "autoreconnect": self.autoreconnect,
                "provider_i": self.provider_i,
                "engine": self.engine.value,
                "killswitch": self.killswitch,
                "dns_protect": self.dns_protect,
            }))
        except OSError:
            pass

    # -- polling (called every tick by the run loop) -------------------------

    def tick(self, term: Terminal) -> None:
        now = time.monotonic()
        if now - self._last_refresh > 60:
            vpn.sudo_refresh()  # keep the sudo ticket warm so connect never re-prompts
            self._last_refresh = now
        if self.conn == "connecting":
            el = time.monotonic() - self.connect_start
            if vpn.connected(self.engine):
                self.conn, self.candidates = "connected", []
                self.exit_info = vpn.exit_ip()
                # post-connect security wiring
                if self.killswitch and self.active:
                    security.killswitch_enable(self.active.ip, self.active.port,
                                               self.active.proto)
                if self.dns_protect:
                    self._dns_service = security.dns_backup()
                    security.dns_set(["1.1.1.1", "1.0.0.1"])
                ok, ip, country = security.tunnel_verified(timeout=10)
                if not ok and self.conn == "connected":
                    self.status = "tunnel verification failed — traffic may leak"
            elif el > 3 and not vpn.alive(self.engine):
                self._next_candidate(term)
            elif el > 15:
                self._next_candidate(term)
        elif self.conn == "connected" and not vpn.alive(self.engine):
            self._security_teardown()
            if self.autoreconnect and not self.user_disconnected and self.active:
                # retry the same server first, then fail over across the whole pool
                self._start_connect(term, self.servers, first=self.active, count=12)
            else:
                self.conn, self.active, self.exit_info = "idle", None, None

    # -- connection actions --------------------------------------------------

    def _order(self, pool: list[Server], first: Server | None = None,
               count: int = 6) -> list[Server]:
        from .vpngate import composite_score
        # firewall-friendly ports first (443/995 survive strict firewalls), then score
        ordered = sorted(pool, key=lambda s: (not s.friendly, -composite_score(s)))
        if first is not None:
            ordered = [first] + [s for s in ordered if s.host != first.host]
        return ordered[:count]

    def _start_connect(self, term: Terminal, pool: list[Server],
                       first: Server | None = None, count: int = 6) -> None:
        cands = self._order(pool, first, count)
        if not cands:
            self.status = "no servers to try"
            return
        if self.conn in ("connected", "connecting"):
            self._teardown_active(term)  # switching servers: drop the old tunnel first
        self.status = f"probing {len(cands)} servers…"
        healthy = self._healthy(cands)
        cands = healthy or cands[:1]  # ponytail: try at least one even if the probe fails
        self.candidates, self.cand_i, self.user_disconnected = cands, 0, False
        self._launch_current(term)

    def _healthy(self, cands: list[Server]) -> list[Server]:
        """Parallel TCP reachability probe; reachable servers, fastest first."""
        from concurrent.futures import ThreadPoolExecutor
        def probe(s: Server) -> tuple[Server, int | None]:
            return s, security.check_server_latency(s.ip, s.port)
        with ThreadPoolExecutor(max_workers=min(len(cands), 16)) as ex:
            results = list(ex.map(probe, cands))
        healthy = [(s, ms) for s, ms in results if ms is not None]
        healthy.sort(key=lambda t: (not t[0].friendly, t[1]))  # friendly first, then fastest
        return [s for s, _ in healthy]

    def _launch_current(self, term: Terminal) -> None:
        s = self.candidates[self.cand_i]
        with self._sudo_ctx(term):
            try:
                vpn.connect(s, self.engine)
                self.conn, self.active = "connecting", s
                self.connect_start = time.monotonic()
                self.exit_info = None
            except vpn.VPNError as e:
                self.conn, self.status = "failed", str(e)

    def _next_candidate(self, term: Terminal) -> None:
        with self._sudo_ctx(term):
            vpn.disconnect()  # tear down the failed attempt before the next
        if self.user_disconnected:
            self.conn, self.candidates = "idle", []
            return
        self.cand_i += 1
        if self.cand_i < len(self.candidates):
            self._launch_current(term)
        else:
            self.conn = "failed"
            self.status = f"no working server (tried {len(self.candidates)})"

    def _do_disconnect(self, term: Terminal) -> None:
        with self._sudo_ctx(term):
            vpn.disconnect(self.engine)

    def _sudo_ctx(self, term: Terminal):
        # warm ticket -> run in place (no screen flicker); cold -> drop out for the prompt
        return contextlib.nullcontext() if vpn.sudo_warm() else term.suspend()

    def _security_teardown(self) -> None:
        # unwind kill switch / DNS overrides before a tunnel goes away
        if self.killswitch:
            security.killswitch_disable()
        if self.dns_protect and self._dns_service:
            security.dns_restore(self._dns_service)
            self._dns_service = None

    def _teardown_active(self, term: Terminal) -> None:
        """Drop the live/attempting tunnel and its security wiring; ferry keeps running."""
        self.user_disconnected = True
        self._security_teardown()
        self._do_disconnect(term)
        self.conn, self.active, self.exit_info, self.candidates = "idle", None, None, []

    def auto_connect(self, term: Terminal) -> None:
        """One key: pick and connect the best working server anywhere."""
        if not self.servers:
            self.status = "no servers — press r to refresh"
            return
        self._start_connect(term, self.servers, count=12)

    # -- input ---------------------------------------------------------------

    def on_key(self, k: str, term: Terminal) -> None:
        if self.help:
            self.help = False
            return
        if k in ("ctrl-c", "q"):
            self.running = False
        elif k == "?":
            self.help = True
        elif k == "d":  # disconnect but keep ferry running
            if self.conn in ("connected", "connecting"):
                self._teardown_active(term)
                self.status = "disconnected"
        elif k == "c":  # auto-connect the best working server anywhere
            self.auto_connect(term)
        elif k == "r":
            self._refetch()
        elif k == "a":
            self.autoreconnect = not self.autoreconnect
            self.status = f"auto-reconnect {'on' if self.autoreconnect else 'off'}"
            self.save()
        elif k == "S":
            self.sort = SORT_ORDER[(SORT_ORDER.index(self.sort) + 1) % len(SORT_ORDER)]
            self.ssel = 0
            self.save()
        elif k == "P":
            from . import get_providers
            self.provider_i = (self.provider_i + 1) % len(get_providers())
            self._refetch()
        elif k == "E" and vpn.wg_installed():
            self.engine = (vpn.Engine.WIREGUARD if self.engine == vpn.Engine.OPENVPN
                           else vpn.Engine.OPENVPN)
            self.status = f"engine: {self.engine.value}"
            self.save()
        elif k == "k":
            self.killswitch = not self.killswitch
            self.status = f"kill switch: {'on' if self.killswitch else 'off'}"
            self.save()
        elif k == "n":
            self.dns_protect = not self.dns_protect
            self.status = f"dns protection: {'on' if self.dns_protect else 'off'}"
            self.save()
        elif self.focus == "countries":
            self._nav_countries(k, term)
        else:
            self._nav_servers(k, term)

    def _nav_countries(self, k: str, term: Terminal) -> None:
        n = len(self.countries) + 1  # +1 for the Favorites row
        if k == "up":
            self.csel = (self.csel - 1) % n
            self.ssel = 0
        elif k == "down":
            self.csel = (self.csel + 1) % n
            self.ssel = 0
        elif k in ("right", "enter"):  # open the country's server list
            if self.current_servers():
                self.focus = "servers"
                self.ssel = 0

    def _nav_servers(self, k: str, term: Terminal) -> None:
        pool = self.current_servers()
        if k in ("left", "b", "esc"):
            self.focus = "countries"
        elif k == "up":
            self.ssel = (self.ssel - 1) % len(pool) if pool else 0
        elif k == "down":
            self.ssel = (self.ssel + 1) % len(pool) if pool else 0
        elif k == "f":
            s = self.current_server()
            if s:
                self.favorites.symmetric_difference_update({s.host})
                self._fav_gen += 1
                self.save()
        elif k == "enter":  # connect this server (switches if already connected)
            s = self.current_server()
            if s:
                self._start_connect(term, pool, first=s)

    def _refetch(self) -> None:
        from . import get_providers, vpngate
        try:
            providers = get_providers()
            provider = providers[self.provider_i % len(providers)]
            self.servers = provider.fetch()
            vpngate.save_cache(self.servers)
            self._rebuild_countries()
            self.status = f"refreshed — {len(self.servers)} servers ({provider.name})"
        except Exception as e:  # noqa: BLE001
            self.status = f"refresh failed: {e}"


# -- render ------------------------------------------------------------------


def _status_line(app: App) -> str:
    if app.conn == "connected" and app.active:
        where = app.active.country
        ip = app.exit_info[0] if app.exit_info else app.active.ip
        cc = f" ({app.exit_info[1]})" if app.exit_info else ""
        up = fmt_uptime(time.monotonic() - app.connect_start)
        return (style(ON + " connected", GOOD, bold=True)
                + style(f"   {where} · {ip}{cc} · {up}", TEXT))
    if app.conn == "connecting":
        who = f" {app.active.host}" if app.active else ""
        prog = f" ({app.cand_i + 1}/{len(app.candidates)})" if len(app.candidates) > 1 else ""
        return style(f"{BUSY} connecting{who}{prog}…", WARN, bold=True)
    if app.conn == "failed":
        return style(OFF + " connect failed", BAD, bold=True)
    return style(OFF + " not connected", dim=True)


def _header(app: App, width: int) -> list[str]:
    out = [" " * MARGIN + ln for ln in LOGO]
    out.append("")
    left = " " * MARGIN + _status_line(app)
    if not vpn.installed():
        right = style("openvpn missing — brew install openvpn", BAD)
    else:
        tags = [t for t, on in (("auto-reconnect", app.autoreconnect),
                                ("kill-switch", app.killswitch),
                                ("dns-guard", app.dns_protect)) if on]
        right = style(" · ".join(tags), ALT, dim=True) if tags else ""
    gap = max(1, width - dwidth(strip_ansi(left)) - dwidth(strip_ansi(right)) - MARGIN)
    out.append(left + " " * gap + right)
    out.append(" " * MARGIN + style("─" * (width - 2 * MARGIN), RULE))
    return out


def _country_rows(app: App, width: int, h: int) -> list[str]:
    rows = app._rail_rows()  # [(name, count)], Favorites first
    top = _window(app.csel, len(rows), h)
    out = []
    for i in range(top, min(top + h, len(rows))):
        name, n = rows[i]
        sel = i == app.csel
        ptr = style(PTR + " ", ACCENT, bold=True) if sel else "  "
        star = style(STAR + " ", WARN) if i == 0 else ""
        color = ACCENT if sel else (WARN if i == 0 else TEXT)
        namew = max(8, width - 2 * MARGIN - 2 - dwidth(str(n)) - 1)
        line = ptr + cell(star + name, namew, color=color, bold=sel) + " " + style(str(n), dim=True)
        out.append(" " * MARGIN + line)
    return out


def _server_rows(app: App, width: int, h: int) -> list[str]:
    pool = app.current_servers()
    country = "Favorites" if app.csel == 0 else app.countries[app.csel - 1][0][1]
    head = (" " * MARGIN + style(country, ACCENT, bold=True)
            + style(f"   {len(pool)} server{'' if len(pool) == 1 else 's'} · sort: {app.sort}", dim=True))
    if not pool:
        empty = ("  no favorites yet — press f on a server to pin it"
                 if app.csel == 0 else "  no servers here")
        return [head, "", " " * MARGIN + style(empty, dim=True)]
    top = _window(app.ssel, len(pool), h - 1)
    inner = width - 2 * MARGIN
    namew = max(8, inner - 2 - 9 - 7 - 10 - 2)  # ptr(2) transport(9) ping(7) speed(10) star(2)
    out = [head]
    for i in range(top, min(top + (h - 1), len(pool))):
        s = pool[i]
        sel = i == app.ssel
        active = (app.active is not None and s.host == app.active.host
                  and app.conn in ("connected", "connecting"))
        if sel:
            ptr = style(PTR + " ", ACCENT, bold=True)
        elif active:
            ptr = style(ON + " ", GOOD, bold=True)
        else:
            ptr = "  "
        star = style(STAR, WARN) if s.host in app.favorites else " "
        host_color = ACCENT if sel else (GOOD if active else TEXT)
        line = (ptr
                + cell(s.host, namew, color=host_color, bold=sel or active)
                + cell(s.transport, 9, align="right", color=GOOD if s.friendly else None,
                       bold=s.friendly)
                + cell(fmt_ping(s.ping), 7, align="right", color=GOOD if 0 < s.ping < 80 else None)
                + cell(fmt_speed(s.speed), 10, align="right")
                + " " + star)
        out.append(" " * MARGIN + line)
    return out


def _help(app: App, width: int) -> list[str]:
    keys = [
        ("↑ ↓", "move"),
        ("↵", "open a country · connect a server"),
        ("←", "back to the country list"),
        ("c", "auto-connect the best server anywhere"),
        ("d", "disconnect (ferry keeps running)"),
        ("f", "favorite / unfavorite a server"),
        ("r", "refresh the server list"),
        ("S", "cycle sort — score / ping / speed"),
        ("a", "auto-reconnect if the tunnel drops"),
        ("k", "kill switch"),
        ("n", "DNS-leak protection"),
        ("P", "cycle provider"),
        ("?", "close this help"),
        ("q", "quit (disconnects any tunnel)"),
    ]
    out = [" " * MARGIN + style("Keys", ACCENT, bold=True), ""]
    for k, v in keys:
        out.append(" " * MARGIN + "  " + cell(k, 8, color=ACCENT, bold=True) + style(v, dim=True))
    out.append("")
    out.append(" " * MARGIN + style("green transport (443 / 995) slips through strict firewalls.",
                                     GOOD, dim=True))
    return out


def _footer(app: App, width: int) -> str:
    if app.help:
        hints = [("any key", "close")]
    else:
        if app.focus == "countries":
            hints = [("↑↓", "move"), ("↵", "open"), ("c", "auto-connect")]
        else:
            hints = [("↑↓", "move"), ("↵", "connect"), ("←", "back"), ("f", "fav")]
        if app.conn in ("connected", "connecting"):
            hints.append(("d", "disconnect"))
        hints += [("?", "keys"), ("q", "quit")]
    out, used = "", 0
    if app.status and not app.help:
        st = dtrunc(app.status, max(10, width // 2))
        out = style(st, ALT) + "   "
        used = dwidth(st) + 3
    sep = "  " + DOT + "  "
    sep_w = dwidth(sep)
    first = True
    for k, v in hints:
        add = dwidth(k) + 1 + dwidth(v) + (0 if first else sep_w)
        if used + add > width - MARGIN:
            break
        if not first:
            out += style(sep, dim=True)
        out += style(k, ACCENT) + style(" " + v, dim=True)
        used += add
        first = False
    return " " * MARGIN + out


def render(app: App, cols: int, rows: int) -> list[str]:
    width = max(40, cols)
    header = _header(app, width)
    body_h = max(3, rows - len(header) - 2)  # one blank above, footer below
    if app.help:
        body = _help(app, width)
    elif app.focus == "countries":
        body = _country_rows(app, width, body_h)
    else:
        body = _server_rows(app, width, body_h)
    footer = _footer(app, width)
    out = (header + [""] + body)[:rows - 1]
    out += [""] * max(0, rows - 1 - len(out))
    out.append(footer)
    return out
