"""ferry entry point + run loop.

Fetch the VPN Gate list once (plain stdout, before raw mode), then single
-threaded loop: poll stdin, poll the connection state, full-redraw. openvpn does
the tunnelling in its own root process; on quit we tear any tunnel down.
"""

from __future__ import annotations

import sys

from . import __version__, vpn, vpngate
from .tui import App, Terminal, render

HELP = ("ferry — connect to free VPN Gate servers from a terminal.\n"
        "  ferry            browse countries, pick a server, connect\n"
        "  ferry --version  print version\n\n"
        "openvpn must be installed (brew install openvpn) and runs as root,\n"
        "so connecting asks for your sudo password.")


def load_servers() -> tuple[list[vpngate.Server], str | None]:
    print("Fetching VPN Gate server list…", flush=True)
    try:
        servers = vpngate.fetch()
        vpngate.save_cache(servers)
        return servers, None
    except Exception as e:  # noqa: BLE001
        cached = vpngate.load_cache()
        note = None if cached else f"vpngate unreachable: {e}"
        if cached:
            note = f"offline — showing {len(cached)} cached servers"
        return cached, note


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if any(a in ("-h", "--help") for a in argv):
        print(HELP)
        return 0
    if any(a in ("-V", "--version") for a in argv):
        print(f"ferry {__version__}")
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("ferry needs an interactive terminal.")
        return 1
    if not vpn.installed():
        print("openvpn not found. Install it with:  brew install openvpn")
        return 1

    servers, note = load_servers()
    if not servers:
        print(note or "no servers available.")
        return 1

    app = App(servers, note)
    # One password up front on the clean terminal; the loop keeps the ticket warm
    # so connecting/disconnecting never prompt again this session.
    print("openvpn needs root — enter your password once to unlock connecting:")
    vpn.sudo_prime()

    term = Terminal()
    term.enter()
    try:
        while app.running:
            for k in term.read_keys(0.25):
                app.on_key(k, term)
                if not app.running:
                    break
            if not app.running:
                break
            app.tick(term)
            cols, rows = term.size()
            term.write(render(app, cols, rows))
    except KeyboardInterrupt:
        pass
    finally:
        term.leave()
        if app.conn in ("connected", "connecting"):
            print("Disconnecting…")
            vpn.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
