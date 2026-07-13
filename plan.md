# Ferry — Project Plan

```
█▀▀ █▀▀ █▀▄ █▀▄ █ █    ~~~~~>
█▀▀ █▀▀ █▀▄ █▀▄ ▀▄▀    <~~~~~
```

A terminal VPN hopper: pick a country, pick a server, connect. Ferry carries
you across to a foreign port. One command (`ferry`) opens a TUI that lists free
**VPN Gate** servers by country, connects via the system **`openvpn`**, and
shows live status (your public IP + country, server, uptime). Press one key to
disconnect. Looks and feels like [Trawl](../Trawl) — its sibling.

- **Command:** `ferry`
- **Language:** Python 3.10+, **stdlib only — zero third-party packages** (hard rule, inherited from Trawl).
- **Engine:** the system `openvpn` (Homebrew), spawned as a daemon and driven by pid/log files.
- **Source:** [VPN Gate](https://www.vpngate.net/) public relay list — free, no account, many countries.
- **Target:** macOS (uses `sudo`, `open`, `pbcopy`, `scutil`/`ifconfig`).
- **Lineage:** Trawl (the finder/TUI to emulate; spawn-an-engine architecture).

---

## Why VPN Gate

Free, no signup, machine-readable. The iPhone CSV endpoint
`https://www.vpngate.net/api/iphone/` returns one row per volunteer server with
a base64-encoded, fully self-contained `.ovpn` (inline `ca`/`cert`/`key`, no
username/password). That means connect = decode the config → `openvpn --config`.
Exactly the "free VPN for trivial uses, different countries" ask.

**Expected CSV shape** (to be pinned against a live sample — see Build phase 0):

```
*vpn_servers
#HostName,IP,Score,Ping,Speed,CountryLong,CountryShort,NumVpnSessions,Uptime,TotalUsers,TotalTraffic,LogType,Operator,Message,OpenVPN_ConfigData_Base64
<host>,<ip>,<score>,<ping_ms>,<speed_bps>,Japan,JP,<n>,...,...,...,...,...,...,<base64 .ovpn>
...
*
```

- Keep only rows whose `OpenVPN_ConfigData_Base64` is non-empty (some are L2TP/SSTP-only).
- `CountryLong`/`CountryShort` drive the country list. `Score` (higher better),
  `Ping` (ms, lower better), `Speed` (bps, higher better) drive the sort.
- Cache the parsed list under Application Support; refetch on demand (`r`).

## The root requirement (decided: sudo prompt per connect)

`openvpn` must run as **root** to create the `tun` device and edit routes. No way
around it. Model:

1. Before entering raw-mode TUI, run `sudo -v` on the clean terminal so the
   password prompt is normal. (Re-prime if the sudo timestamp expired at connect.)
2. Connect: write the decoded `.ovpn` to a temp file, then
   `sudo openvpn --config <file> --daemon --writepid <pidfile> --log <logfile>`.
3. Status: tail `<logfile>` for `Initialization Sequence Completed`; read `<pidfile>`.
4. Disconnect: `sudo kill $(cat <pidfile>)`, then confirm the tun is gone.

No system files are touched; everything is reversible by disconnecting.

## OpenVPN 2.6/2.7 compatibility (build-time robustness, not a user choice)

VPN Gate configs are old. Before launching, post-process the decoded config:

- Strip `comp-lzo` / `compress` lines (removed in modern openvpn) — or emit
  `--allow-compression yes`.
- Ensure cipher negotiation: read the config's `cipher X`, pass
  `--data-ciphers-fallback X` (and a sane `--data-ciphers` list) so 2.7 doesn't refuse.
- `--connect-timeout` / `--connect-retry-max` so a dead volunteer server fails fast
  instead of hanging.

**Known ceiling (v1):** route-based IP change only. macOS DNS is not pushed into
the tunnel without an up/down helper (Tunnelblick-style), so DNS queries may still
use the local resolver (a DNS leak). Acceptable for "trivial uses"; flagged in the
UI. Upgrade path: bundle an up/down script that swaps `scutil` DNS. Marked with a
`ferry:` comment at the spot.

## Architecture (mirrors Trawl's spawn-an-engine split)

```
Ferry/
  ferry/
    __init__.py     __version__
    __main__.py     entry + run loop (poll stdin, poll status ~1s, redraw)
    vpngate.py      fetch + parse CSV -> [Server]; cache; country grouping   (Trawl sources.py analog)
    openvpn.py      spawn/kill daemon, pidfile+logfile, log tail, tun check,  (Trawl aria2.py analog)
                    public-IP/country probe, config post-processing
    tui.py          raw-ANSI TUI: countries -> servers -> status             (Trawl tui.py analog)
    theme.py        palette + glyphs (violet, maritime)                      (copied+retuned from Trawl)
  Formula/ferry.rb  Homebrew formula (depends_on "openvpn", "python@3.14")
  build.sh          zipapp -> dist/ferry
  README.md  LICENSE  .gitignore  plan.md
```

State in `~/Library/Application Support/Ferry/`:
- `servers.json` — cached VPN Gate list (+ fetch timestamp)
- `favorites.json` — pinned servers / last-used
- `config.json` — settings (sort mode, auto-reconnect on/off)
- `run/` — `ferry.pid`, `ferry.log`, active `*.ovpn` temp

## TUI flow

```
launch
  └─ status header: public IP + country (probed), openvpn presence check
  └─ Countries pane  ── ↑↓ move · → into servers · f favorite · r refresh
       └─ Servers pane (sorted: score | ping | speed, S cycles)
            └─ ↵ connect  → sudo prompt → daemon spawns → Status view
  Status view: connected server, country, uptime, throughput, "disconnect (d)"
```

Keys (draft): `↑↓` move · `←→`/`↵` navigate · `S` sort · `f` favorite ·
`r` refresh list · `d` disconnect · `g` settings · `?` help · `q` quit.

## Feature scope (v1, per decisions)

In: country pick, server pick, connect, disconnect, live status (IP/country/
uptime/throughput), **sort by latency/score/speed**, **favorites + last-used**,
**auto-reconnect on drop**.

Out (v1): kill-switch enforcement, DNS-leak prevention (flagged, not enforced),
per-app routing, Linux/Windows.

## Verification

- **Live-sample fixture** (phase 0): capture one real CSV + one decoded `.ovpn`
  into `tests/` as a fixture; parser test runs offline against it.
- **Parser self-check**: assert country grouping, base64 decode, sort order.
- **Connect smoke test**: on a real network, connect to one server, assert public
  IP/country changed vs. baseline, then disconnect and assert it reverted.
- No third-party test deps; a small `tests/` with stdlib `assert`/`unittest`.

## Build phases

0. **Capture live sample** (needs the user's temp VPN once): fetch CSV, decode a
   config, save fixtures, pin exact column order + config quirks. THEN finalize
   parser/launcher details.
1. `theme.py` + `vpngate.py` (fetch/parse/cache) + parser self-check.
2. `openvpn.py` (spawn/kill/status/IP probe/config post-process).
3. `tui.py` + `__main__.py` run loop.
4. Favorites, sort, auto-reconnect, settings.
5. `build.sh`, `Formula/ferry.rb`, `README.md`; connect smoke test.

## Open questions (resolve during build)

- Exact CSV column order & whether any OpenVPN rows carry `auth-user-pass` — pin in phase 0.
- macOS: `openvpn` from Homebrew keg vs. any preinstalled — detect via `which`/keg path.
- Throughput source: openvpn management interface vs. `netstat -ib` on the tun — decide in phase 2.

---

## Revised under ponytail-ultra (as built)

Phase 0 pinned the live format and let me delete speculative work. What
shipped is leaner than the plan above:

- **theme.py folded into `tui.py`** — ~40 lines, one consumer. 5 files, not 6:
  `__init__.py` (version + `STATE_DIR`), `vpngate.py`, `vpn.py`, `tui.py`, `__main__.py`.
- **Config post-processing deleted.** All 98 live configs carry `data-ciphers`,
  no `comp-lzo`, no `auth-user-pass` → `sudo openvpn --config` works as-is on 2.7.
  Kept `--connect-timeout 10 --connect-retry-max 2` (dead volunteer relays hang).
  `vpn.py` has a `ponytail:`-style note: strip compression only if a server refuses.
- **Throughput + openvpn management interface deleted.** Status = server, country,
  uptime, and a one-shot exit-IP check (the honest "did it work" signal).
- **Settings overlay deleted.** Sort (`S`) and auto-reconnect (`a`) are direct
  keys persisted to one `state.json`. No settings view.
- **Fetch threads deleted.** One blocking fetch at launch (plain stdout line),
  `servers.json` cache as offline fallback. Ferry loads one list once.
- **sheen animation + mouse deleted.** Kept the violet palette + gradient logo
  (that is the look); the shimmer is not identity.
- **Log/pid readability:** the run loop pre-creates `ferry.log`/`ferry.pid`
  user-owned so openvpn (root) truncates-in-place and they stay readable back —
  avoids parsing a root-owned log.
- **Open questions resolved:** column order confirmed; no `auth-user-pass` in any
  row; throughput dropped (no netstat/mgmt socket).

Verified: `tests/test_ferry.py` (parse/sort/group/render/vpn-state) passes;
zipapp builds; `--help`/`--version` work; all three views render at exact size.
Remaining: the live connect smoke test (needs sudo + a real relay).

### Post-v0.1 (user-requested during first run)

- **sudo once at launch, not per connect.** `vpn.sudo_prime()` (`sudo -v`) runs
  on the clean terminal before the TUI; the poll loop refreshes with
  `sudo -n -v` every 60s so the ticket never expires mid-session. Connect/
  disconnect check `sudo -n true` first — warm → run in place (no screen
  flicker), cold → drop out of the TUI so the prompt is visible.
- **Transport column + firewall-friendly highlight.** Volunteer relays bind
  random high ports that locked-down networks drop (the first connect failed on
  `tcp:1609`, "Network is unreachable"). `proto`/`port` are now decoded from each
  config at parse time, shown as `tcp:443` etc., and ports 443/992/995 render
  green — the ones that pass a strict firewall. Makes "connect on a blocked
  network" achievable by picking a green relay.
