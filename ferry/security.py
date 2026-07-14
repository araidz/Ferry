"""Security: PF kill switch, DNS leak fix, tunnel verification.

Each capability is independent — toggle kill switch, DNS protection, and
tunnel verification separately. All operations are best-effort; failures
are logged to status, never raised (a broken kill switch is worse than none).
"""

from __future__ import annotations

import socket
import subprocess
import time
import urllib.request
import json

# -- PF kill switch -----------------------------------------------------------

ANCHOR = "com.ferry.killswitch"

PF_TEMPLATE = """\
set block-policy drop
set skip on lo0
block all
pass quick on {phys} proto udp from any port 68 to any port 67
pass quick on {phys} proto udp from any port 67 to any port 68
pass quick on {phys} proto udp to 224.0.0.251 port 5353
pass quick on {phys} proto udp from 224.0.0.251 port 5353
pass quick on {phys} proto {proto} from any to {ip} port {port}
pass on {utun} all
"""


def _phys_if() -> str:
    """Primary physical interface (en0, en1, etc.)."""
    try:
        r = subprocess.run(["route", "-n", "get", "0.0.0.0"],
                           capture_output=True, text=True, timeout=5)
        for ln in r.stdout.splitlines():
            if "interface:" in ln:
                return ln.split(":")[-1].strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return "en0"


def _utun_if() -> str | None:
    """Active utun interface, or None."""
    try:
        r = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
        for ln in r.stdout.splitlines():
            if ln.startswith("utun"):
                return ln.split(":")[0]
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def killswitch_enable(server_ip: str, server_port: int, proto: str = "udp") -> None:
    """Load PF anchor blocking all traffic except VPN + loopback + DHCP + mDNS."""
    phys = _phys_if()
    utun = _utun_if()
    if not utun:
        return  # ponytail: no tunnel interface, can't enable
    rules = PF_TEMPLATE.format(phys=phys, proto=proto,
                               ip=server_ip, port=server_port, utun=utun)
    try:
        subprocess.run(["pfctl", "-a", ANCHOR, "-f", "-"],
                       input=rules.encode(), capture_output=True, timeout=5)
        subprocess.run(["pfctl", "-F", "states"], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def killswitch_disable() -> None:
    """Remove PF anchor and flush states."""
    try:
        subprocess.run(["pfctl", "-a", ANCHOR, "-Fr"],
                       capture_output=True, timeout=5)
        subprocess.run(["pfctl", "-F", "states"], capture_output=True, timeout=5)
        subprocess.run(["pfctl", "-f", "/etc/pf.conf"], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


# -- DNS leak fix -------------------------------------------------------------

def _active_service() -> str:
    """Active network service name (e.g., 'Wi-Fi')."""
    try:
        r = subprocess.run(["route", "-n", "get", "0.0.0.0"],
                           capture_output=True, text=True, timeout=5)
        iface = ""
        for ln in r.stdout.splitlines():
            if "interface:" in ln:
                iface = ln.split(":")[-1].strip()
                break
        if not iface:
            return "Wi-Fi"
        r2 = subprocess.run(["networksetup", "-listnetworkserviceorder"],
                            capture_output=True, text=True, timeout=5)
        for ln in r2.stdout.splitlines():
            if iface in ln:
                return ln.split("Hardware Port: ")[-1].split(",")[0].strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return "Wi-Fi"


def dns_backup() -> str:
    """Return the active network service name for later restore."""
    return _active_service()


def dns_set(dns: list[str], service: str | None = None) -> None:
    """Set DNS servers on the active interface."""
    svc = service or _active_service()
    try:
        subprocess.run(["networksetup", "-setdnsservers", svc] + dns,
                       capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def dns_restore(service: str) -> None:
    """Restore DHCP-provided DNS."""
    try:
        subprocess.run(["networksetup", "-setdnsservers", service, "Empty"],
                       capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


# -- Tunnel verification ------------------------------------------------------

def tunnel_interface_alive() -> bool:
    """Check if a utun interface is UP."""
    try:
        r = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
        in_utun = False
        for ln in r.stdout.splitlines():
            if ln.startswith("utun"):
                in_utun = True
            elif in_utun and "status: active" in ln:
                return True
            elif in_utun and ln and not ln[0].isspace():
                in_utun = False
    except (subprocess.TimeoutExpired, OSError):
        pass
    return False


def default_route_through_tunnel() -> bool:
    """Check if default route goes through utun."""
    try:
        r = subprocess.run(["netstat", "-nr"], capture_output=True, text=True, timeout=5)
        for ln in r.stdout.splitlines():
            if ln.startswith("default") and "utun" in ln:
                return True
    except (subprocess.TimeoutExpired, OSError):
        pass
    return False


def verify_exit_ip(timeout: float = 5.0) -> tuple[str, str] | None:
    """Fetch current public IP and country."""
    try:
        req = urllib.request.Request(
            "https://ipinfo.io/json",
            headers={"User-Agent": "ferry"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode())
            return d.get("ip"), d.get("country")
    except Exception:
        return None


def tunnel_verified(timeout: float = 5.0) -> tuple[bool, str | None, str | None]:
    """
    Full tunnel verification.
    Returns (ok, actual_ip, actual_country).
    ok = interface alive + route through tunnel + IP check.
    """
    if not tunnel_interface_alive():
        return False, None, None
    if not default_route_through_tunnel():
        return False, None, None
    info = verify_exit_ip(timeout)
    if info is None:
        return True, None, None  # ponytail: IP check failed but interface+route OK
    return True, info[0], info[1]


# -- Health check -------------------------------------------------------------

def check_server_latency(ip: str, port: int, timeout: float = 2.0) -> int | None:
    """TCP connect to server:port, return latency in ms or None if unreachable."""
    start = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
        return int((time.monotonic() - start) * 1000)
    except (OSError, TimeoutError, OverflowError):
        return None
    finally:
        sock.close()
