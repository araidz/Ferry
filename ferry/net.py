"""Censorship-resilient HTTP GET over stdlib only.

Some ISPs (e.g. UAE/Etisalat) block VPN provider sites two ways: they
DNS-blackhole the domain, and they RST-inject any TLS handshake whose SNI
names a blacklisted host. We defeat both without third-party deps:

* resolve names over DNS-over-HTTPS (bypasses the poisoned resolver);
* send the TLS ClientHello in small TCP segments so the DPI can't reassemble
  the SNI and never fires the reset.

Certificates are still fully verified against the real hostname, so the
bypass changes *how* bytes reach the server, not *who* we trust.

`get()` tries a plain urllib request first (fast path on open networks) and
only falls back to the fragmenting path when that fails.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.parse
import urllib.request

# ponytail: small enough that no single TCP segment carries the whole SNI;
# raise toward 64 (still bypasses here) only if throughput ever matters.
_FRAG = 8
_DOH = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")


def _doh(host: str, timeout: float) -> list[str]:
    for base in _DOH:
        try:
            url = f"{base}?name={urllib.parse.quote(host)}&type=A"
            req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
            if ips:
                return ips
        except Exception:  # noqa: BLE001 — try the next resolver
            continue
    return []


def resolve(host: str, timeout: float = 8.0) -> list[str]:
    """A-record IPs, DoH first (survives a poisoned resolver), else system DNS."""
    ips = _doh(host, timeout)
    if ips:
        return ips
    try:
        return list({ai[4][0] for ai in socket.getaddrinfo(host, 443, socket.AF_INET)})
    except OSError:
        return []


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    while body:
        line, _, body = body.partition(b"\r\n")
        try:
            n = int(line.split(b";")[0], 16)
        except ValueError:
            break
        if n == 0:
            break
        out += body[:n]
        body = body[n + 2:]  # skip the trailing CRLF
    return bytes(out)


def _frag_get(url: str, timeout: float, headers: dict[str, str], depth: int = 5) -> bytes:
    u = urllib.parse.urlparse(url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    if u.scheme != "https":
        raise ValueError("fragmented GET is HTTPS-only")

    ctx = ssl.create_default_context()  # full chain + hostname verification
    last: Exception | None = None
    for ip in resolve(host, timeout) or [host]:
        inb, outb = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = ctx.wrap_bio(inb, outb, server_hostname=host)
        try:
            sock = socket.create_connection((ip, port), timeout=timeout)
        except OSError as e:
            last = e
            continue
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        first = [True]

        def flush() -> None:
            data = outb.read()
            if not data:
                return
            if first[0]:  # fragment only the ClientHello flight
                first[0] = False
                for i in range(0, len(data), _FRAG):
                    sock.sendall(data[i:i + _FRAG])
            else:
                sock.sendall(data)

        try:
            while True:
                try:
                    tls.do_handshake()
                    flush()
                    break
                except ssl.SSLWantReadError:
                    flush()
                    chunk = sock.recv(16384)
                    if not chunk:
                        raise ConnectionError("connection closed during handshake")
                    inb.write(chunk)
            hdrs = {"Host": host, "User-Agent": "Mozilla/5.0",
                    "Accept-Encoding": "identity", "Connection": "close", **headers}
            req = f"GET {path} HTTP/1.1\r\n" + "".join(
                f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
            tls.write(req.encode())
            flush()
            buf = bytearray()
            while True:
                try:
                    d = tls.read(65536)
                    if not d:
                        break
                    buf += d
                except ssl.SSLWantReadError:
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    inb.write(chunk)
                except ssl.SSLZeroReturnError:
                    break
        except Exception as e:  # noqa: BLE001 — try next IP
            last = e
            continue
        finally:
            sock.close()

        head, _, body = bytes(buf).partition(b"\r\n\r\n")
        htext = head.decode("latin1")
        lines = htext.splitlines()
        status = lines[0] if lines else ""
        code = int(status.split()[1]) if len(status.split()) > 1 and status.split()[1].isdigit() else 0
        if code in (301, 302, 303, 307, 308) and depth > 0:
            loc = next((l.split(":", 1)[1].strip() for l in lines[1:]
                        if l.lower().startswith("location:")), "")
            if loc:
                return _frag_get(urllib.parse.urljoin(url, loc), timeout, headers, depth - 1)
        if code != 200:
            last = ConnectionError(f"HTTP status: {status or 'no response'}")
            continue
        if "transfer-encoding: chunked" in htext.lower():
            body = _dechunk(body)
        return body
    raise last or ConnectionError(f"could not reach {host}")


def get(url: str, timeout: float = 20.0, headers: dict[str, str] | None = None) -> bytes:
    """HTTP GET body. Plain urllib first; DPI-bypass fallback on failure."""
    headers = headers or {"User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:  # noqa: BLE001 — blocked/poisoned? try the bypass
        return _frag_get(url, timeout, headers)
