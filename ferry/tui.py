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

RAIL_W = 22
MARGIN = 2
GAP = 2

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


# -- panels (from Trawl) -----------------------------------------------------


def _panel_top(title: str, width: int, count: str | None, bw: str) -> str:
    label = f" {title} "
    cnt = f" {count} " if count else ""
    fill = max(0, width - 4 - dwidth(label) - dwidth(cnt))
    return (style("╭─", bw) + style(label, ALT, bold=True) + style("─" * fill, bw)
            + style(cnt, dim=True) + style("─╮", bw))


def _panel_bottom(width: int, bw: str) -> str:
    return style("╰" + "─" * (width - 2) + "╯", bw)


def _side(line: str, bw: str) -> str:
    return style("│", bw) + " " + line + " " + style("│", bw)


def _wrap_panel(title: str, inner: list[str], width: int, height: int,
                focused: bool, count: str | None = None) -> list[str]:
    bw = ACCENT if focused else RULE
    inner_w = width - 4
    body_h = height - 2
    rows = (inner + [cell("", inner_w)] * body_h)[:body_h]
    return [_panel_top(title, width, count, bw)] + [_side(r, bw) for r in rows] + [_panel_bottom(width, bw)]


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
        self.autoreconnect = bool(st.get("autoreconnect", False))
        # connection
        self.conn = "idle"  # idle | connecting | connected | failed
        self.active: Server | None = None
        self.connect_start = 0.0
        self.exit_info: tuple[str, str] | None = None
        self.user_disconnected = False
        self.view = "browse"  # browse | status
        # browse navigation
        self.focus = "countries"
        self.csel = 0
        self.ssel = 0
        self._last_refresh = 0.0
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
        if self.csel == 0:  # Favorites row
            pool = [s for s in self.servers if s.host in self.favorites]
        else:
            (cc, name), _ = self.countries[self.csel - 1]
            pool = [s for s in self.servers if s.cc == cc]
        return sorted(pool, key=SORTS[self.sort])

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
            if vpn.connected():
                self.conn = "connected"
                self.exit_info = vpn.exit_ip()  # one-shot; brief UI stall is fine
            elif not vpn.alive() and time.monotonic() - self.connect_start > 2:
                self.conn, self.status = "failed", vpn.log_error()
            elif time.monotonic() - self.connect_start > 30:
                self._do_disconnect(term)
                self.conn, self.status = "failed", "timed out"
        elif self.conn == "connected" and not vpn.alive():
            if self.autoreconnect and not self.user_disconnected and self.active:
                self._launch(term, self.active)
            else:
                self.conn, self.active, self.exit_info = "idle", None, None
                self.view = "browse"

    # -- connection actions --------------------------------------------------

    def _launch(self, term: Terminal, s: Server) -> None:
        with self._sudo_ctx(term):
            try:
                vpn.connect(s)
                self.conn, self.active = "connecting", s
                self.connect_start = time.monotonic()
                self.exit_info, self.user_disconnected = None, False
            except vpn.VPNError as e:
                self.conn, self.status = "failed", str(e)
        self.view = "status"

    def _do_disconnect(self, term: Terminal) -> None:
        with self._sudo_ctx(term):
            vpn.disconnect()

    def _sudo_ctx(self, term: Terminal):
        # warm ticket -> run in place (no screen flicker); cold -> drop out for the prompt
        return contextlib.nullcontext() if vpn.sudo_warm() else term.suspend()

    # -- input ---------------------------------------------------------------

    def on_key(self, k: str, term: Terminal) -> None:
        if self.help:
            self.help = False
            return
        if k == "ctrl-c":
            self.running = False
            return
        if k in ("?",):
            self.help = True
            return
        if k == "a":
            self.autoreconnect = not self.autoreconnect
            self.status = f"auto-reconnect {'on' if self.autoreconnect else 'off'}"
            self.save()
            return
        if self.view == "status":
            self._on_key_status(k, term)
        else:
            self._on_key_browse(k, term)

    def _on_key_browse(self, k: str, term: Terminal) -> None:
        if k == "q":
            self.running = False
        elif k == "r":
            self._refetch()
        elif k == "S":
            self.sort = SORT_ORDER[(SORT_ORDER.index(self.sort) + 1) % len(SORT_ORDER)]
            self.ssel = 0
            self.save()
        elif k == "s" and self.conn in ("connected", "connecting"):
            self.view = "status"
        elif self.focus == "countries":
            self._nav_countries(k)
        else:
            self._nav_servers(k, term)

    def _nav_countries(self, k: str) -> None:
        n = len(self.countries) + 1  # +1 for Favorites row
        if k == "up":
            self.csel = (self.csel - 1) % n
            self.ssel = 0
        elif k == "down":
            self.csel = (self.csel + 1) % n
            self.ssel = 0
        elif k in ("right", "enter"):
            if self.current_servers():
                self.focus = "servers"

    def _nav_servers(self, k: str, term: Terminal) -> None:
        pool = self.current_servers()
        if k == "left":
            self.focus = "countries"
        elif k == "up":
            self.ssel = (self.ssel - 1) % len(pool) if pool else 0
        elif k == "down":
            self.ssel = (self.ssel + 1) % len(pool) if pool else 0
        elif k == "f":
            s = self.current_server()
            if s:
                self.favorites.symmetric_difference_update({s.host})
                self.save()
        elif k == "enter":
            s = self.current_server()
            if s:
                self._launch(term, s)

    def _on_key_status(self, k: str, term: Terminal) -> None:
        if k in ("d",) and self.conn in ("connected", "connecting"):
            self.user_disconnected = True
            self._do_disconnect(term)
            self.conn, self.active, self.exit_info = "idle", None, None
            self.view = "browse"
        elif k in ("b", "left", "esc"):
            self.view = "browse"
        elif k == "q":
            self.running = False

    def _refetch(self) -> None:
        from . import vpngate
        try:
            self.servers = vpngate.fetch()
            vpngate.save_cache(self.servers)
            self._rebuild_countries()
            self.status = f"refreshed — {len(self.servers)} servers"
        except Exception as e:  # noqa: BLE001
            self.status = f"refresh failed: {e}"


# -- render ------------------------------------------------------------------


def _conn_line(app: App) -> str:
    if app.conn == "connected" and app.active:
        where = app.active.country
        ip = app.exit_info[0] if app.exit_info else app.active.ip
        cc = f" ({app.exit_info[1]})" if app.exit_info else ""
        up = fmt_uptime(time.monotonic() - app.connect_start)
        return (style(ON + " ", GOOD) + style("connected ", GOOD, bold=True)
                + style(f"{where} · {ip}{cc} · {up}", TEXT))
    if app.conn == "connecting":
        return style(BUSY + " connecting…", WARN, bold=True)
    if app.conn == "failed":
        return style(OFF + " connect failed", BAD, bold=True)
    return style(OFF + " not connected", dim=True)


def _header(app: App, width: int) -> list[str]:
    out = [" " * MARGIN + ln for ln in _logo_lines()]
    right = ""
    if not vpn.installed():
        right = style("openvpn missing — brew install openvpn", BAD)
    elif app.autoreconnect:
        right = style("auto-reconnect on", ALT, dim=True)
    left = " " * MARGIN + _conn_line(app)
    gap = max(1, width - dwidth(strip_ansi(left)) - dwidth(strip_ansi(right)) - MARGIN)
    out.append(left + " " * gap + right)
    out.append(" " * MARGIN + style("─" * (width - 2 * MARGIN), RULE))
    return out


def _rail(app: App, h: int) -> list[str]:
    rows = app._rail_rows()
    top = _window(app.csel, len(rows), h)
    out = []
    for i in range(top, min(top + h, len(rows))):
        name, n = rows[i]
        sel = i == app.csel
        star = STAR + " " if i == 0 else ""
        mark = style("▌", ACCENT, bold=True) if sel else " "
        label = f"{star}{name} ({n})"
        color = ACCENT if sel else (WARN if i == 0 else None)
        out.append(mark + " " + cell(label, RAIL_W - 2, color=color, bold=sel, dim=not sel and i != 0))
    return (out + [cell("", RAIL_W)] * h)[:h]


def _server_rows(app: App, inner_w: int, vis_h: int) -> list[str]:
    pool = app.current_servers()
    if not pool:
        return [cell("  no servers — press f elsewhere to add favorites"
                     if app.csel == 0 else "  no servers", inner_w, dim=True)]
    top = _window(app.ssel, len(pool), vis_h)
    name_w = max(8, inner_w - 10 - 8 - 10 - 5 - 3)  # port(10) ping(8) speed(10) cc(5) ptr(3)
    rows = []
    for i in range(top, min(top + vis_h, len(pool))):
        s = pool[i]
        sel = i == app.ssel and app.focus == "servers"
        ptr = style(PTR, ACCENT, bold=True) + " " if sel else "  "
        fav = style(STAR, WARN) if s.host in app.favorites else " "
        line = (ptr
                + cell(s.host, name_w, color=ACCENT if sel else TEXT, bold=sel)
                + cell(s.transport, 10, align="right", color=GOOD if s.friendly else None,
                       bold=s.friendly)
                + cell(fmt_ping(s.ping), 8, align="right", color=GOOD if 0 < s.ping < 80 else None)
                + cell(fmt_speed(s.speed), 10, align="right")
                + " " + cell(s.cc, 3) + fav)
        rows.append(line)
    return rows


def _status_panel(app: App, width: int, height: int) -> list[str]:
    inner_w = width - 4
    inner: list[str] = [cell("", inner_w)]

    def field(label: str, value: str, color: str | None = None) -> str:
        return "  " + cell(label, 10, dim=True) + cell(value, inner_w - 12, color=color)

    if app.conn == "connecting":
        inner.append(cell("  connecting… (a few seconds; volunteer relays can be slow)", inner_w, color=WARN))
    elif app.conn == "failed":
        inner.append(cell("  connect failed", inner_w, color=BAD, bold=True))
        inner.append(cell("  " + app.status, inner_w, dim=True))
    s = app.active
    if s:
        inner.append(cell("", inner_w))
        inner.append(field("Server", s.host, ACCENT if app.conn == "connected" else None))
        inner.append(field("Country", f"{s.country} ({s.cc})"))
        if app.exit_info:
            inner.append(field("Exit IP", f"{app.exit_info[0]}  [{app.exit_info[1]}]", GOOD))
        else:
            inner.append(field("Exit IP", s.ip))
        if app.conn == "connected":
            inner.append(field("Uptime", fmt_uptime(time.monotonic() - app.connect_start)))
        inner.append(field("Ping", fmt_ping(s.ping)))
        inner.append(field("Speed", fmt_speed(s.speed)))
        inner.append(field("Transport", s.transport, GOOD if s.friendly else None))
    inner.append(cell("", inner_w))
    inner.append(field("Reconnect", "on" if app.autoreconnect else "off",
                       GOOD if app.autoreconnect else None))
    title = {"connected": "Connected", "connecting": "Connecting",
             "failed": "Failed"}.get(app.conn, "Status")
    return _wrap_panel(title, inner, width, height, True)


def _help_panel(width: int, height: int) -> list[str]:
    keys = [
        ("↑ ↓", "move"), ("→ / enter", "into servers · connect"),
        ("← / b", "back"), ("enter", "connect to selected server"),
        ("d", "disconnect"), ("f", "favorite server"),
        ("S", "cycle sort (score / ping / speed)"), ("a", "toggle auto-reconnect"),
        ("s", "show connection status"), ("r", "refresh server list"),
        ("?", "this help"), ("q", "quit"),
    ]
    inner_w = width - 4
    inner = [cell("", inner_w)]
    for k, v in keys:
        inner.append("  " + cell(k, 14, color=ACCENT, bold=True) + cell(v, inner_w - 16, dim=True))
    inner.append(cell("", inner_w))
    inner.append(cell("  green port (443 / 995) = best chance through a strict firewall.",
                      inner_w, color=GOOD, dim=True))
    inner.append(cell("  sudo is asked once at launch; ferry changes routes, not DNS.",
                      inner_w, dim=True))
    return _wrap_panel("Keys", inner, width, height, True)


def _footer(app: App, width: int) -> str:
    if app.help:
        hints = [("any key", "close")]
    elif app.view == "status":
        hints = [("d", "disconnect"), ("b", "back"), ("a", "reconnect"), ("?", "keys"), ("q", "quit")]
    elif app.focus == "countries":
        hints = [("↑↓", "country"), ("→", "servers"), ("S", "sort"), ("a", "reconnect"),
                 ("r", "refresh"), ("?", "keys"), ("q", "quit")]
    else:
        hints = [("↑↓", "server"), ("↵", "connect"), ("f", "favorite"), ("←", "back"),
                 ("S", "sort"), ("?", "keys"), ("q", "quit")]
    out, used = "", 0
    if app.status and not app.help:
        st = dtrunc(app.status, max(10, width // 2))
        out = style(st, ALT) + "   "
        used = dwidth(st) + 3
    sep = "  " + DOT + "  "
    sep_w = dwidth(sep)
    last = hints[-1]
    last_w = sep_w + dwidth(last[0]) + 1 + dwidth(last[1])
    first = True
    for k, v in hints[:-1]:
        add = dwidth(k) + 1 + dwidth(v) + (0 if first else sep_w)
        if used + add + last_w > width:
            break
        if not first:
            out += style(sep, dim=True)
        out += style(k, ACCENT) + style(" " + v, dim=True)
        used += add
        first = False
    if not first:
        out += style(sep, dim=True)
    out += style(last[0], ACCENT) + style(" " + last[1], dim=True)
    return " " * MARGIN + out


def render(app: App, cols: int, rows: int) -> list[str]:
    width = max(40, cols)
    header = _header(app, width)
    body_h = max(3, rows - len(header) - 3)
    body: list[str]

    if app.help:
        body = _help_panel(min(width - 2 * MARGIN, 72), body_h)
        body = [" " * MARGIN + ln for ln in body]
    elif app.view == "status":
        pw = min(width - 2 * MARGIN, 60)
        body = [" " * MARGIN + ln for ln in _status_panel(app, pw, body_h)]
    else:  # browse — rail beside servers panel
        rail = _rail(app, body_h)
        panel_w = width - 2 * MARGIN - RAIL_W - GAP
        inner_w = panel_w - 4
        name = "Favorites" if app.csel == 0 else app.countries[app.csel - 1][0][1]
        title = f"{name} · {app.sort}"
        srows = _server_rows(app, inner_w, body_h - 2)
        panel = _wrap_panel(title, srows, panel_w, body_h, app.focus == "servers",
                            count=f"({len(app.current_servers())})")
        body = []
        for i in range(body_h):
            r = rail[i] if i < len(rail) else cell("", RAIL_W)
            p = panel[i] if i < len(panel) else ""
            body.append(" " * MARGIN + r + " " * GAP + p)

    footer = _footer(app, width)
    out = header + [""] + body + [""] + [footer]
    return out[:rows] + [""] * max(0, rows - len(out))
