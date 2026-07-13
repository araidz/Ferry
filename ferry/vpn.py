"""Drive the system openvpn: connect (sudo, daemonized), disconnect, status.

openvpn must run as root — it creates the tun device and rewrites routes. So
every sudo call must run with the TUI suspended (cooked terminal, main screen)
or its password prompt is invisible; the caller handles that. Connect writes the
decoded .ovpn to a run dir, daemonizes openvpn with a pidfile + logfile we
pre-create as ourselves (so they stay user-readable even though openvpn runs as
root), and disconnect is `sudo kill`.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from shutil import which

from . import STATE_DIR
from .vpngate import Server

RUN = STATE_DIR / "run"
PIDFILE = RUN / "ferry.pid"
LOGFILE = RUN / "ferry.log"
OVPN = RUN / "ferry.ovpn"

DONE = "Initialization Sequence Completed"  # openvpn's handshake-complete marker


class VPNError(Exception):
    pass


def installed() -> str | None:
    return which("openvpn")


def _pid() -> int | None:
    try:
        return int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None


def alive() -> bool:
    p = _pid()
    if p is None:
        return False
    try:
        os.kill(p, 0)
    except PermissionError:  # exists but owned by root — still alive
        return True
    except OSError:  # ESRCH — gone
        return False
    return True


def _log() -> str:
    try:
        return LOGFILE.read_text(errors="replace")
    except OSError:
        return ""


def connected() -> bool:
    return alive() and DONE in _log()


def log_error() -> str:
    """Best-effort last meaningful log line, for a failed connect."""
    lines = [ln for ln in _log().splitlines() if ln.strip()]
    for ln in reversed(lines):
        low = ln.lower()
        if any(w in low for w in ("error", "fatal", "cannot", "failed", "exiting")):
            return ln.split("] ")[-1][:120]
    return lines[-1][:120] if lines else "no output"


def connect(s: Server) -> None:
    """Launch openvpn as a root daemon. Raises on launch failure; connection
    completion is observed afterwards via connected(). Must run with the TUI
    suspended so sudo can prompt."""
    if not installed():
        raise VPNError("openvpn not found — brew install openvpn")
    RUN.mkdir(parents=True, exist_ok=True)
    OVPN.write_text(s.config())
    LOGFILE.write_text("")  # pre-create user-owned: always readable back
    PIDFILE.write_text("")
    r = subprocess.run(
        ["sudo", "openvpn", "--config", str(OVPN), "--daemon",
         "--writepid", str(PIDFILE), "--log", str(LOGFILE),
         # volunteer relays go dark often — fail fast instead of hanging:
         "--connect-timeout", "10", "--connect-retry-max", "2"],
    )
    if r.returncode != 0:
        raise VPNError(f"openvpn failed to launch (exit {r.returncode})")


def disconnect() -> None:
    """SIGTERM the daemon (openvpn tears down its routes on TERM). Must run with
    the TUI suspended in case the sudo timestamp has expired."""
    p = _pid()
    if p is not None:
        subprocess.run(["sudo", "kill", str(p)])
    try:
        PIDFILE.unlink()
    except OSError:
        pass


def exit_ip(timeout: float = 4.0) -> tuple[str, str] | None:
    """(ip, country-code) of the actual exit point — proof traffic really routes
    through the tunnel. None if the lookup is unreachable."""
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d.get("ip", "?"), d.get("country", "?")
    except Exception:  # noqa: BLE001 — any network/parse failure means "unknown"
        return None
