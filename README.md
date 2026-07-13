# Ferry

```
█▀▀ █▀▀ █▀▄ █▀▄ █ █   ~~~≈>
█▀  █▄▄ █▀▄ █▀▄ ▀▄▀   <≈~~~
```

![macOS](https://img.shields.io/badge/macOS-000?logo=apple&logoColor=white)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-success)
![license](https://img.shields.io/badge/license-MIT-blue)

A terminal VPN hopper. Ferry lists free [**VPN Gate**](https://www.vpngate.net/)
servers by country, connects one via the system **`openvpn`**, and shows live
status — press a key to disconnect. No account, no signup. Just type `ferry`.

Ferry is a from-scratch Python TUI in the look and feel of its sibling
[Trawl](https://github.com/araidz/Trawl) — **zero third-party packages, stdlib only.**

## Preview

```
  █▀▀ █▀▀ █▀▄ █▀▄ █ █   ~~~≈>
  █▀  █▄▄ █▀▄ █▀▄ ▀▄▀   <≈~~~
  ● connected  Japan · 219.100.37.109 (JP) · 12m           auto-reconnect on
  ──────────────────────────────────────────────────────────────────────────
    ★ Favorites (2)      ╭─ Japan · score ─────────────────────────── (48) ─╮
  ▌ Japan (48)           │ ❯ public-vpn-153       10 ms   248 Mbps   JP  ★  │
    Korea Republic of…   │   vpn441877979         22 ms   180 Mbps   JP     │
    United States (2)    │   ...                                            │
    Australia (1)        ╰──────────────────────────────────────────────────╯
  ↑↓ server · ↵ connect · f favorite · ← back · S sort · ? keys · q quit
```

## Requirements

- **macOS**
- **[OpenVPN](https://openvpn.net/)** (`brew install openvpn`)
- **Python 3.10+**

## Install

### Homebrew

```sh
brew tap araidz/ferry https://github.com/araidz/Ferry
brew install ferry
```

`brew` pulls in `openvpn` and Python automatically.

### From source

```sh
git clone https://github.com/araidz/Ferry.git && cd Ferry
sh build.sh                                       # -> dist/ferry (one self-contained file)
ln -sf "$PWD/dist/ferry" /opt/homebrew/bin/ferry  # or anywhere on your PATH
```

Needs `openvpn` (`brew install openvpn`). Or run without building: `python3 -m ferry`.

## Usage

`ferry` opens to a country rail beside a servers panel. Pick a country, pick a
server, press Enter to connect. **openvpn runs as root, so connecting asks for
your `sudo` password** (the TUI steps aside for the prompt, then returns).

| Key | Action |
| --- | --- |
| `↑ ↓` | move within the focused pane |
| `→` / `Enter` | countries → servers · connect to a server |
| `←` / `b` | back |
| `Enter` | connect to the selected server |
| `d` | disconnect |
| `f` | favorite / unfavorite a server (pinned under ★ Favorites) |
| `S` | cycle sort — score / ping / speed |
| `a` | toggle auto-reconnect (relaunch if the tunnel drops) |
| `s` | show the connection status view |
| `r` | refresh the server list |
| `?` keys · `q` | quit (disconnects any active tunnel) |

## How it works

Ferry fetches the VPN Gate iPhone CSV (`/api/iphone/`), keeps the rows that ship
an OpenVPN config, and groups them by country. Connecting decodes that server's
self-contained `.ovpn`, writes it under Application Support, and launches
`sudo openvpn --config … --daemon --writepid … --log …`. The status view tails
that log for the completed handshake and does one exit-IP lookup as proof the
traffic really routes through the tunnel. Disconnecting is `sudo kill` of the
daemon (openvpn tears its routes down on `SIGTERM`).

State lives in `~/Library/Application Support/Ferry/`:
`servers.json` (cached list), `state.json` (favorites / sort / auto-reconnect),
`run/` (the active config, pidfile, logfile).

## Scope

Ferry changes your **routes** — your public IP and apparent country. It does
**not** force DNS into the tunnel (macOS needs an up/down helper for that), so
DNS queries may still use your local resolver. Fine for trivial uses; if you
need leak-proof DNS or a kill-switch, use a full client. There is no per-app
routing and no Windows/Linux support.

## Privacy

VPN Gate relays are volunteer-run and public — good for casual use, not for
anything sensitive. Ferry talks only to VPN Gate (server list), the relay you
pick, and one IP-echo service (exit-IP check).

## Credits

- [VPN Gate](https://www.vpngate.net/) (University of Tsukuba) — the free relay list
- [OpenVPN](https://openvpn.net/) — the tunnel
- [Trawl](https://github.com/araidz/Trawl) — the look-and-feel this grew from

No third-party code is used; Ferry is an independent stdlib-only implementation.

## License

[MIT](LICENSE)
