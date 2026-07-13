"""Offline self-check: parse the pinned VPN Gate fixture, sort/group, render.

    python3 tests/test_ferry.py

No network, no terminal, no framework — just asserts that fail loudly if the
CSV parse, sorting, country grouping, or the renderer break.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferry import vpn, vpngate  # noqa: E402
from ferry.tui import App, SORTS, render, strip_ansi  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "vpngate_sample.csv"


def test_parse():
    servers = vpngate.parse(FIX.read_text())
    assert len(servers) == 7, len(servers)  # 6 countries + 1 extra JP (udp)
    ccs = {s.cc for s in servers}
    assert {"JP", "KR", "US", "AU", "IN", "RO"} <= ccs, ccs
    s0 = servers[0]
    assert s0.score > 0 and s0.speed > 0
    cfg = s0.config()  # base64 decodes to a real .ovpn
    assert "remote " in cfg and ("<ca>" in cfg or "ca " in cfg), cfg[:120]
    active = [ln.strip() for ln in cfg.splitlines() if ln.strip() and not ln.startswith(("#", ";"))]
    assert not any(ln.startswith("auth-user-pass") for ln in active)  # self-contained
    assert all(s.port > 0 and s.proto in ("tcp", "udp") for s in servers)
    assert any(s.friendly for s in servers)  # public-vpn-153 is on 443
    assert servers[0].transport.startswith(servers[0].proto)
    return servers


def test_sort(servers):
    by_score = sorted(servers, key=SORTS["score"])
    assert all(by_score[i].score >= by_score[i + 1].score for i in range(len(by_score) - 1))
    by_ping = sorted([s for s in servers if s.ping], key=SORTS["ping"])
    assert all((by_ping[i].ping or 0) <= (by_ping[i + 1].ping or 0) for i in range(len(by_ping) - 1))


def test_grouping_and_favorites(servers):
    app = App(servers)
    # rail = Favorites row + one row per distinct country
    assert app._rail_rows()[0][0] == "Favorites"
    assert len(app._rail_rows()) == 1 + len({s.cc for s in servers})
    # Favorites row (csel 0) is empty until we add one
    assert app.current_servers() == []
    app.favorites.add(servers[0].host)
    assert app.current_servers()[0].host == servers[0].host
    # a country row lists only that country's servers, sorted by score
    app.csel = 1
    pool = app.current_servers()
    assert pool and all(s.cc == pool[0].cc for s in pool)
    assert all(pool[i].score >= pool[i + 1].score for i in range(len(pool) - 1))


def test_render_smoke(servers):
    app = App(servers)
    out = render(app, 100, 30)
    assert len(out) == 30, len(out)  # exactly fills the screen
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "not connected" in plain and "Favorites" in plain
    assert "connect" in plain  # footer hint present
    # status view renders without an active connection too
    app.view, app.conn = "status", "failed"
    app.status = "boom"
    assert len(render(app, 100, 30)) == 30


def test_vpn_no_process():
    # nothing running -> not alive / not connected, and log_error is safe
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")  # ensure absent
    assert vpn.alive() is False
    assert vpn.connected() is False
    assert isinstance(vpn.log_error(), str)


def test_failover_order(servers):
    app = App(servers)
    ordered = app._order(servers, first=None)
    assert len(ordered) <= 6
    fr = [i for i, s in enumerate(ordered) if s.friendly]
    nf = [i for i, s in enumerate(ordered) if not s.friendly]
    if fr and nf:
        assert max(fr) < min(nf), "friendly relays must be tried first"
    pick = servers[-1]
    assert app._order(servers, first=pick)[0].host == pick.host  # chosen server first



def test_on_key_connect(servers):
    # drive key dispatch through nav -> auto-connect with sudo/openvpn stubbed out;
    # catches wiring bugs (e.g. a missing term arg) that pure-render tests miss.
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm, tui.vpn.connect
    tui.vpn.sudo_warm = lambda: True          # warm -> _sudo_ctx never touches term
    tui.vpn.connect = lambda s: None          # no real openvpn
    try:
        app = App(servers)
        for k in ("down", "up", "right", "left"):
            app.on_key(k, None)               # navigation must not raise
        app.focus, app.csel = "countries", 1
        app.on_key("enter", None)             # auto-connect a country
        assert app.conn == "connecting" and app.active is not None
        app2 = App(servers)
        app2.csel, app2.focus = 1, "servers"
        app2.on_key("enter", None)            # connect a specific server
        assert app2.conn == "connecting"
    finally:
        tui.vpn.sudo_warm, tui.vpn.connect = saved

def test_cache_backfill(servers):
    # an OLD cache (pre-transport) must still yield ports, recovered from configs
    import json
    import tempfile
    from dataclasses import asdict
    old = []
    for s in servers:
        d = asdict(s)
        d.pop("proto"), d.pop("port")
        old.append(d)
    p = Path(tempfile.mkdtemp()) / "servers.json"
    p.write_text(json.dumps(old))
    vpngate.CACHE = p
    loaded = vpngate.load_cache()
    assert len(loaded) == len(servers)
    assert all(s.port > 0 for s in loaded), "ports not recovered from config"
    assert any(s.friendly for s in loaded)


if __name__ == "__main__":
    servers = test_parse()
    test_sort(servers)
    test_grouping_and_favorites(servers)
    test_render_smoke(servers)
    test_failover_order(servers)
    test_on_key_connect(servers)
    test_cache_backfill(servers)
    test_vpn_no_process()
    print("ok — all ferry self-checks passed")
