"""Drive system OpenVPN or WireGuard: connect, disconnect, status.

openvpn runs as root (creates tun device, rewrites routes). WireGuard uses
wg-quick which also needs root (sudo). Each engine has its own connect/disconnect/
alive/connected functions. The TUI picks the engine at startup or via key toggle.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from enum import Enum
from shutil import which

from . import STATE_DIR
from .vpngate import Server

RUN = STATE_DIR / "run"
PIDFILE = RUN / "ferry.pid"
LOGFILE = RUN / "ferry.log"
OVPN = RUN / "ferry.ovpn"
WG_CFG = RUN / "ferry-wg.conf"

DONE = "Initialization Sequence Completed"  # openvpn's handshake-complete marker


class Engine(Enum):
    OPENVPN = "openvpn"
    WIREGUARD = "wireguard"


class VPNError(Exception):
    pass


# -- installed checks --------------------------------------------------------

def installed() -> str | None:
    return which("openvpn")


def wg_installed() -> bool:
    return which("wg-quick") is not None


# -- sudo helpers ------------------------------------------------------------

def sudo_prime() -> bool:
    """Ask for the sudo password once, up front, on a clean terminal."""
    return subprocess.run(["sudo", "-v"]).returncode == 0


def sudo_warm() -> bool:
    """True if the sudo ticket is still valid."""
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


def sudo_refresh() -> None:
    """Extend the ticket without prompting; a no-op if it already expired."""
    subprocess.run(["sudo", "-n", "-v"], capture_output=True)


# -- OpenVPN engine ----------------------------------------------------------

def _pid() -> int | None:
    try:
        return int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None


def alive_ovpn() -> bool:
    p = _pid()
    if p is None:
        return False
    try:
        os.kill(p, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def connected_ovpn() -> bool:
    if not alive_ovpn():
        return False
    try:
        with open(LOGFILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            return DONE.encode() in f.read()
    except OSError:
        return False


def log_error() -> str:
    """Best-effort last meaningful log line, for a failed connect."""
    try:
        with open(LOGFILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return "no output"
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    for ln in reversed(lines):
        low = ln.lower()
        if any(w in low for w in ("error", "fatal", "cannot", "failed", "exiting")):
            return ln.split("] ")[-1][:120]
    return lines[-1][:120] if lines else "no output"


def connect_ovpn(s: Server) -> None:
    """Launch openvpn as a root daemon."""
    if not installed():
        raise VPNError("openvpn not found — brew install openvpn")
    RUN.mkdir(parents=True, exist_ok=True)
    OVPN.write_text(s.config())
    LOGFILE.write_text("")
    PIDFILE.write_text("")
    r = subprocess.run(
        ["sudo", "openvpn", "--config", str(OVPN), "--daemon",
         "--writepid", str(PIDFILE), "--log", str(LOGFILE),
         "--connect-timeout", "8", "--connect-retry-max", "1"],
    )
    if r.returncode != 0:
        raise VPNError(f"openvpn failed to launch (exit {r.returncode})")


def disconnect_ovpn() -> None:
    p = _pid()
    if p is not None:
        subprocess.run(["sudo", "kill", str(p)])
    try:
        PIDFILE.unlink()
    except OSError:
        pass


# -- WireGuard engine --------------------------------------------------------

def connect_wg(s: Server) -> None:
    """Write config, run wg-quick up."""
    if not wg_installed():
        raise VPNError("wireguard-tools not found — brew install wireguard-tools")
    RUN.mkdir(parents=True, exist_ok=True)
    WG_CFG.write_text(s.config())
    LOGFILE.write_text("")
    r = subprocess.run(
        ["sudo", "wg-quick", "up", str(WG_CFG)],
        capture_output=True)
    if r.returncode != 0:
        raise VPNError(f"wg-quick failed (exit {r.returncode})")


def disconnect_wg() -> None:
    if WG_CFG.exists():
        subprocess.run(["sudo", "wg-quick", "down", str(WG_CFG)],
                       capture_output=True)
        WG_CFG.unlink(missing_ok=True)


def alive_wg() -> bool:
    """Check if WireGuard has a recent handshake."""
    try:
        r = subprocess.run(["sudo", "wg", "show"], capture_output=True, text=True,
                           timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return False
    for ln in r.stdout.splitlines():
        if "latest handshake" in ln.lower():
            low = ln.lower()
            if "none" in low:
                return False
            if "hour" in low or "day" in low:
                return False
            return True
    return False


def connected_wg() -> bool:
    return alive_wg()


# -- Unified API (engine-dispatched) -----------------------------------------

def alive(engine: Engine = Engine.OPENVPN) -> bool:
    if engine == Engine.WIREGUARD:
        return alive_wg()
    return alive_ovpn()


def connected(engine: Engine = Engine.OPENVPN) -> bool:
    if engine == Engine.WIREGUARD:
        return connected_wg()
    return connected_ovpn()


def connect(s: Server, engine: Engine = Engine.OPENVPN) -> None:
    if engine == Engine.WIREGUARD:
        connect_wg(s)
    else:
        connect_ovpn(s)


def disconnect(engine: Engine = Engine.OPENVPN) -> None:
    if engine == Engine.WIREGUARD:
        disconnect_wg()
    else:
        disconnect_ovpn()


# -- Misc --------------------------------------------------------------------

def exit_ip(timeout: float = 4.0) -> tuple[str, str] | None:
    """(ip, country-code) of the actual exit point."""
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d.get("ip", "?"), d.get("country", "?")
    except Exception:
        return None
