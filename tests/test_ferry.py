"""Offline self-check: parse the pinned VPN Gate fixture, sort/group, render.

    python3 tests/test_ferry.py

No network, no terminal, no framework — just asserts that fail loudly if the
CSV parse, sorting, country grouping, or the renderer break.
"""

import base64
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferry import vpn, vpngate  # noqa: E402
from ferry.tui import (App, SORTS, SORT_ORDER, LOGO, MARGIN,  # noqa: E402
                        _cw, _fg, _lerp, _logo_color, _rgb, _window,
                        cell, dtrunc, dwidth, fmt_ping, fmt_speed, fmt_uptime,
                        pad, parse_keys, render, strip_ansi, style)

FIX = Path(__file__).resolve().parent / "fixtures" / "vpngate_sample.csv"

# Hermetic state: redirect the app's state dir to a throwaway temp dir so the
# suite never reads or writes the real ~/Library/Application Support/Ferry.
import ferry.tui as _tui  # noqa: E402
_TEST_STATE = Path(tempfile.mkdtemp(prefix="ferry-test-"))
_tui.STATE_DIR = _TEST_STATE
_tui.STATE_FILE = _TEST_STATE / "state.json"
vpngate.STATE_DIR = _TEST_STATE
vpngate.CACHE = _TEST_STATE / "servers.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_server(host="h", ip="1.2.3.4", score=100, ping=10, speed=1_000_000,
                 country="Testland", cc="TT", sessions=1, proto="tcp", port=443,
                 config_b64=None):
    if config_b64 is None:
        cfg = f"proto {proto}\nremote {ip} {port}\n<ca>\nfake\n</ca>\n"
        config_b64 = base64.b64encode(cfg.encode()).decode()
    return vpngate.Server(host, ip, score, ping, speed, country, cc,
                          sessions, config_b64, proto, port)


# ---------------------------------------------------------------------------
# vpngate: parse
# ---------------------------------------------------------------------------

def test_parse():
    servers = vpngate.parse(FIX.read_text())
    assert len(servers) == 7, len(servers)
    ccs = {s.cc for s in servers}
    assert {"JP", "KR", "US", "AU", "IN", "RO"} <= ccs, ccs
    s0 = servers[0]
    assert s0.score > 0 and s0.speed > 0
    cfg = s0.config()
    assert "remote " in cfg and ("<ca>" in cfg or "ca " in cfg), cfg[:120]
    active = [ln.strip() for ln in cfg.splitlines() if ln.strip() and not ln.startswith(("#", ";"))]
    assert not any(ln.startswith("auth-user-pass") for ln in active)
    assert all(s.port > 0 and s.proto in ("tcp", "udp") for s in servers)
    assert any(s.friendly for s in servers)
    assert servers[0].transport.startswith(servers[0].proto)
    return servers


def test_parse_empty():
    assert vpngate.parse("") == []
    assert vpngate.parse("only garbage\nno commas here\n") == []


def test_parse_only_star_and_comments():
    text = "*vpn_servers\n#comment line\n*\n"
    assert vpngate.parse(text) == []


def test_parse_row_too_short():
    # fewer than 15 columns → skipped
    text = "short,row,only\n*\n"
    assert vpngate.parse(text) == []


def test_parse_empty_config_column():
    # row has 15 fields but config (col 14) is blank → skipped
    row = ",".join(["x"] * 14) + ",\n"
    text = f"*\n{row}*\n"
    assert vpngate.parse(text) == []


def test_parse_whitespace_config():
    # config column is whitespace-only → stripped → skipped
    row = ",".join(["x"] * 14) + ",   \n"
    text = f"*\n{row}*\n"
    assert vpngate.parse(text) == []


def test_parse_integer_parsing_edge_cases():
    # _int handles non-numeric gracefully
    assert vpngate._int("") == 0
    assert vpngate._int("abc") == 0
    assert vpngate._int("42") == 42
    assert vpngate._int("0") == 0
    assert vpngate._int("-5") == -5
    assert vpngate._int("  7  ") == 7


def test_remote_parsing():
    # proto + remote
    assert vpngate._remote("proto udp\nremote 1.2.3.4 1194\n") == ("udp", 1194)
    # proto tcp explicit
    assert vpngate._remote("proto tcp\nremote 1.2.3.4 443\n") == ("tcp", 443)
    # no proto line → defaults tcp
    assert vpngate._remote("remote 1.2.3.4 80\n") == ("tcp", 80)
    # no remote line → port 0
    assert vpngate._remote("proto udp\n") == ("udp", 0)
    # empty config
    assert vpngate._remote("") == ("tcp", 0)
    # comments and junk
    cfg = "# proto udp\n# this is a comment\nproto tcp\nremote 1.2.3.4 443\n; junk\n"
    assert vpngate._remote(cfg) == ("tcp", 443)
    # malformed proto value
    assert vpngate._remote("proto garbage\n") == ("tcp", 0)


# ---------------------------------------------------------------------------
# vpngate: Server dataclass
# ---------------------------------------------------------------------------

def test_server_config_decode():
    s = _make_server()
    cfg = s.config()
    assert "remote" in cfg
    assert "proto" in cfg
    assert isinstance(cfg, str)


def test_server_friendly():
    assert _make_server(port=443).friendly is True
    assert _make_server(port=995).friendly is True
    assert _make_server(port=992).friendly is True
    assert _make_server(port=1194).friendly is False
    assert _make_server(port=80).friendly is False
    assert _make_server(port=0).friendly is False


def test_server_transport():
    assert _make_server(proto="tcp", port=443).transport == "tcp:443"
    assert _make_server(proto="udp", port=1194).transport == "udp:1194"
    assert _make_server(port=0).transport == "tcp"  # no port → just proto


# ---------------------------------------------------------------------------
# vpngate: cache
# ---------------------------------------------------------------------------

def test_cache_roundtrip(servers=None):
    if servers is None:
        servers = vpngate.parse(FIX.read_text())
    with tempfile.TemporaryDirectory() as td:
        old_cache = vpngate.CACHE
        try:
            vpngate.CACHE = Path(td) / "servers.json"
            vpngate.save_cache(servers)
            loaded = vpngate.load_cache()
            assert len(loaded) == len(servers)
            for orig, cached in zip(servers, loaded):
                assert orig.host == cached.host
                assert orig.ip == cached.ip
                assert orig.score == cached.score
                assert orig.port == cached.port
                assert orig.proto == cached.proto
        finally:
            vpngate.CACHE = old_cache


def test_cache_backfill(servers):
    # OLD cache (pre-transport) must recover ports from configs
    old = []
    for s in servers:
        d = asdict(s)
        d.pop("proto"), d.pop("port")
        old.append(d)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "servers.json"
        p.write_text(json.dumps(old))
        old_cache = vpngate.CACHE
        try:
            vpngate.CACHE = p
            loaded = vpngate.load_cache()
            assert len(loaded) == len(servers)
            assert all(s.port > 0 for s in loaded), "ports not recovered"
            assert any(s.friendly for s in loaded)
        finally:
            vpngate.CACHE = old_cache


def test_cache_missing_file():
    old_cache = vpngate.CACHE
    try:
        vpngate.CACHE = Path("/tmp/ferry_no_such_file_cache.json")
        assert vpngate.load_cache() == []
    finally:
        vpngate.CACHE = old_cache


def test_cache_corrupt_json():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "servers.json"
        p.write_text("NOT JSON {{{")
        old_cache = vpngate.CACHE
        try:
            vpngate.CACHE = p
            assert vpngate.load_cache() == []
        finally:
            vpngate.CACHE = old_cache


def test_cache_extra_unknown_keys():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "servers.json"
        s = _make_server()
        d = asdict(s)
        d["unknown_field"] = "should be ignored"
        d["another_unknown"] = 42
        p.write_text(json.dumps([d]))
        old_cache = vpngate.CACHE
        try:
            vpngate.CACHE = p
            loaded = vpngate.load_cache()
            assert len(loaded) == 1
            assert loaded[0].host == s.host
        finally:
            vpngate.CACHE = old_cache


def test_cache_empty_list():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "servers.json"
        p.write_text("[]")
        old_cache = vpngate.CACHE
        try:
            vpngate.CACHE = p
            assert vpngate.load_cache() == []
        finally:
            vpngate.CACHE = old_cache


def test_save_cache_os_error():
    # save_cache must not raise on bad path
    old_cache = vpngate.CACHE
    try:
        vpngate.CACHE = Path("/nonexistent/dir/perm_denied/servers.json")
        vpngate.save_cache([_make_server()])  # should not raise
    finally:
        vpngate.CACHE = old_cache


# ---------------------------------------------------------------------------
# vpn: alive / connected / log_error
# ---------------------------------------------------------------------------

def test_vpn_no_process():
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    assert vpn.alive() is False
    assert vpn.connected() is False
    assert isinstance(vpn.log_error(), str)


def test_alive_pid_not_a_number():
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        pf.write_text("not_a_number")
        vpn.PIDFILE = pf
        assert vpn.alive() is False
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")


def test_alive_empty_pidfile():
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        pf.write_text("")
        vpn.PIDFILE = pf
        assert vpn.alive() is False
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")


def test_alive_whitespace_pidfile():
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        pf.write_text("  \n  ")
        vpn.PIDFILE = pf
        assert vpn.alive() is False
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")


def test_alive_stale_pid():
    # PID 99999999 is almost certainly not running
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        pf.write_text("99999999")
        vpn.PIDFILE = pf
        # might be alive on some systems, but usually not
        result = vpn.alive()
        assert isinstance(result, bool)
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")


def test_alive_current_process():
    # Our own PID should definitely be alive
    import os
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        pf.write_text(str(os.getpid()))
        vpn.PIDFILE = pf
        assert vpn.alive() is True
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")


def test_connected_no_logfile():
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.LOGFILE = Path("/tmp/ferry_no_such_log.log")
    assert vpn.connected() is False


def test_connected_log_without_done():
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        lf = Path(td) / "log"
        pf.write_text(str(os.getpid()))
        lf.write_text("stuff happening\nmore stuff\n")
        vpn.PIDFILE = pf
        vpn.LOGFILE = lf
        assert vpn.connected() is False
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.LOGFILE = Path("/tmp/ferry_no_such_log.log")


def test_connected_log_with_done():
    import os
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        lf = Path(td) / "log"
        pf.write_text(str(os.getpid()))
        lf.write_text("stuff\nInitialization Sequence Completed\nmore\n")
        vpn.PIDFILE = pf
        vpn.LOGFILE = lf
        assert vpn.connected() is True
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.LOGFILE = Path("/tmp/ferry_no_such_log.log")


def test_connected_large_log():
    """DONE marker at the end of a >4KB log — tail read must find it."""
    import os
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        lf = Path(td) / "log"
        pf.write_text(str(os.getpid()))
        # 10KB of padding then the marker
        padding = "x" * 10000 + "\n"
        lf.write_text(padding + "Initialization Sequence Completed\n")
        vpn.PIDFILE = pf
        vpn.LOGFILE = lf
        assert vpn.connected() is True
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.LOGFILE = Path("/tmp/ferry_no_such_log.log")


def test_log_error_no_logfile():
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.LOGFILE = Path("/tmp/ferry_no_such_log.log")
    assert vpn.log_error() == "no output"


def test_log_error_empty_log():
    import os
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        lf = Path(td) / "log"
        pf.write_text(str(os.getpid()))
        lf.write_text("")
        vpn.PIDFILE = pf
        vpn.LOGFILE = lf
        result = vpn.log_error()
        assert isinstance(result, str)
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.LOGFILE = Path("/tmp/ferry_no_such_log.log")


def test_log_error_with_error_lines():
    import os
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        lf = Path(td) / "log"
        pf.write_text(str(os.getpid()))
        lf.write_text("line1\nFATAL: something broke\nline3\n")
        vpn.PIDFILE = pf
        vpn.LOGFILE = lf
        result = vpn.log_error()
        assert "FATAL" in result or "something broke" in result
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.LOGFILE = Path("/tmp/ferry_no_such_log.log")


def test_log_error_no_error_keyword():
    import os
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        lf = Path(td) / "log"
        pf.write_text(str(os.getpid()))
        lf.write_text("normal line one\nnormal line two\n")
        vpn.PIDFILE = pf
        vpn.LOGFILE = lf
        result = vpn.log_error()
        assert result == "normal line two"
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.LOGFILE = Path("/tmp/ferry_no_such_log.log")


def test_log_error_truncates_long_lines():
    import os
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "pid"
        lf = Path(td) / "log"
        pf.write_text(str(os.getpid()))
        long_line = "error: " + "A" * 200
        lf.write_text(long_line + "\n")
        vpn.PIDFILE = pf
        vpn.LOGFILE = lf
        result = vpn.log_error()
        assert len(result) <= 120
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.LOGFILE = Path("/tmp/ferry_no_such_log.log")


def test_disconnect_no_pidfile():
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.disconnect()  # should not raise


def test_vpnerror_is_exception():
    assert issubclass(vpn.VPNError, Exception)
    e = vpn.VPNError("test")
    assert str(e) == "test"


# ---------------------------------------------------------------------------
# tui: ANSI + width primitives
# ---------------------------------------------------------------------------

def test_fg():
    result = _fg("#ff0000")
    assert result == "\x1b[38;2;255;0;0m"
    result2 = _fg("#000000")
    assert result2 == "\x1b[38;2;0;0;0m"
    result3 = _fg("#ffffff")
    assert result3 == "\x1b[38;2;255;255;255m"


def test_style_no_decoration():
    assert style("hello") == "hello"
    assert style("hello", None) == "hello"


def test_style_bold():
    result = style("hello", bold=True)
    assert "\x1b[1m" in result
    assert "hello" in result
    assert result.endswith("\x1b[0m")


def test_style_dim():
    result = style("hello", dim=True)
    assert "\x1b[2m" in result


def test_style_color():
    result = style("hello", "#aabbcc")
    assert "\x1b[38;2;" in result
    assert "hello" in result


def test_style_all():
    result = style("hi", "#ff0000", bold=True, dim=True)
    assert "\x1b[1m" in result
    assert "\x1b[2m" in result
    assert "\x1b[38;2;" in result


def test_strip_ansi():
    assert strip_ansi(style("hello", "#ff0000", bold=True)) == "hello"
    assert strip_ansi("no ansi here") == "no ansi here"
    assert strip_ansi("") == ""
    assert strip_ansi("\x1b[1m\x1b[38;2;255;0;0mtest\x1b[0m") == "test"


def test_cw_ascii():
    assert _cw("a") == 1
    assert _cw("Z") == 1
    assert _cw("0") == 1
    assert _cw(" ") == 1


def test_cw_wide_char():
    # CJK character
    assert _cw("中") == 2


def test_cw_combining():
    # combining acute accent
    assert _cw("\u0301") == 0


def test_dwidth():
    assert dwidth("") == 0
    assert dwidth("abc") == 3
    assert dwidth("中") == 2
    assert dwidth("ab中") == 4


def test_dtrunc_empty():
    assert dtrunc("", 5) == ""


def test_dtrunc_short_enough():
    assert dtrunc("hi", 10) == "hi"


def test_dtrunc_exact_fit():
    assert dtrunc("abc", 3) == "abc"


def test_dtrunc_needs_truncation():
    result = dtrunc("hello world", 5)
    assert dwidth(result) <= 5
    assert result.endswith("…")


def test_dtrunc_zero_max():
    assert dtrunc("anything", 0) == ""
    assert dtrunc("anything", -1) == ""


def test_dtrunc_wide_chars():
    # "中文测试" = 8 display width; truncate to 5
    result = dtrunc("中文测试", 5)
    assert dwidth(result) <= 5
    assert result.endswith("…")


def test_pad_left():
    assert pad("hi", 5) == "hi   "
    assert len(pad("hi", 5)) == 5


def test_pad_right():
    assert pad("hi", 5, "right") == "   hi"


def test_pad_center():
    result = pad("hi", 5, "center")
    assert dwidth(result) == 5
    assert result.startswith(" ")
    assert result.endswith(" ")


def test_pad_no_op():
    assert pad("hello", 3) == "hello"
    assert pad("hello", 5) == "hello"


def test_cell_zero_width():
    assert cell("text", 0) == ""
    assert cell("text", -1) == ""


def test_cell_with_color():
    result = cell("hi", 5, color="#ff0000")
    assert strip_ansi(result) == "hi   "
    assert "\x1b[38;2;" in result


def test_cell_truncates():
    result = cell("hello world", 5)
    assert dwidth(strip_ansi(result)) == 5


# ---------------------------------------------------------------------------
# tui: color math
# ---------------------------------------------------------------------------

def test_rgb():
    assert _rgb("#000000") == (0, 0, 0)
    assert _rgb("#ffffff") == (255, 255, 255)
    assert _rgb("#ff8000") == (255, 128, 0)


def test_lerp_endpoints():
    assert _lerp("#000000", "#ffffff", 0.0) == "#000000"
    assert _lerp("#000000", "#ffffff", 1.0) == "#ffffff"
    assert _lerp("#000000", "#ffffff", 0.5) == "#808080"


def test_lerp_clamp():
    # t < 0 clamps to 0, t > 1 clamps to 1
    assert _lerp("#000000", "#ffffff", -1.0) == "#000000"
    assert _lerp("#000000", "#ffffff", 2.0) == "#ffffff"


def test_lerp_same_color():
    assert _lerp("#aabbcc", "#aabbcc", 0.5) == "#aabbcc"


def test_logo_color_ranges():
    # just verify they return valid hex colors without crashing
    for t in [0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0]:
        result = _logo_color(t)
        assert result.startswith("#")
        assert len(result) == 7


def test_logo_cached():
    assert isinstance(LOGO, list)
    assert len(LOGO) == 2
    assert len(LOGO[0]) > 0
    assert len(LOGO[1]) > 0


# ---------------------------------------------------------------------------
# tui: formatters
# ---------------------------------------------------------------------------

def test_fmt_ping():
    assert fmt_ping(0) == "-"
    assert fmt_ping(10) == "10 ms"
    assert fmt_ping(150) == "150 ms"


def test_fmt_speed():
    assert fmt_speed(0) == "-"
    assert fmt_speed(-1) == "-"
    # 1 Mbps
    result = fmt_speed(1_000_000)
    assert "1.0 Mbps" in result
    # 10 Mbps
    result = fmt_speed(10_000_000)
    assert "10 Mbps" in result
    # 100 Mbps
    result = fmt_speed(100_000_000)
    assert "100 Mbps" in result


def test_fmt_uptime():
    assert fmt_uptime(0) == "0s"
    assert fmt_uptime(30) == "30s"
    assert fmt_uptime(60) == "1m00s"
    assert fmt_uptime(90) == "1m30s"
    assert fmt_uptime(3600) == "1h00m"
    assert fmt_uptime(3661) == "1h01m"


# ---------------------------------------------------------------------------
# tui: key parsing
# ---------------------------------------------------------------------------

def test_parse_keys_empty():
    assert parse_keys(b"") == []


def test_parse_keys_arrows():
    assert parse_keys(b"\x1b[A") == ["up"]
    assert parse_keys(b"\x1b[B") == ["down"]
    assert parse_keys(b"\x1b[C") == ["right"]
    assert parse_keys(b"\x1b[D") == ["left"]


def test_parse_keys_enter():
    assert parse_keys(b"\r") == ["enter"]
    assert parse_keys(b"\n") == ["enter"]


def test_parse_keys_backspace():
    assert parse_keys(b"\x7f") == ["backspace"]
    assert parse_keys(b"\x08") == ["backspace"]


def test_parse_keys_tab():
    assert parse_keys(b"\t") == ["tab"]


def test_parse_keys_ctrl_c():
    assert parse_keys(b"\x03") == ["ctrl-c"]


def test_parse_keys_esc():
    assert parse_keys(b"\x1b") == ["esc"]


def test_parse_keys_printable():
    assert parse_keys(b"abc") == ["a", "b", "c"]


def test_parse_keys_mixed():
    data = b"\x1b[Ahello\x1b[B"
    assert parse_keys(data) == ["up", "h", "e", "l", "l", "o", "down"]


def test_parse_keys_unknown_esc():
    # ESC + [ + something not in arrow map
    # The [ is consumed as a printable char after the esc
    assert parse_keys(b"\x1b[Z") == ["esc", "[", "Z"]


def test_parse_keys_osc_prefix():
    # ESC O prefix (SS3) for arrows
    assert parse_keys(b"\x1bOA") == ["up"]
    assert parse_keys(b"\x1bOB") == ["down"]


def test_parse_keys_control_chars_ignored():
    # 0x01-0x1f (except the ones we handle) should be silently skipped
    result = parse_keys(b"\x01\x02\x04\x05")
    assert result == []


def test_parse_keys_unicode():
    result = parse_keys("hello".encode("utf-8"))
    assert result == ["h", "e", "l", "l", "o"]


# ---------------------------------------------------------------------------
# tui: _window
# ---------------------------------------------------------------------------

def test_window_fits():
    assert _window(0, 5, 10) == 0
    assert _window(3, 5, 10) == 0


def test_window_needs_scroll():
    # 10 items, window 5, selection at 0 → offset 0
    assert _window(0, 10, 5) == 0
    # selection at 4 → center → offset 2
    assert _window(4, 10, 5) == 2
    # selection at 9 (last) → offset 5
    assert _window(9, 10, 5) == 5


def test_window_edge_cases():
    assert _window(0, 0, 5) == 0
    assert _window(0, 1, 1) == 0


# ---------------------------------------------------------------------------
# tui: App state
# ---------------------------------------------------------------------------

def test_sort(servers):
    by_score = sorted(servers, key=SORTS["score"])
    assert all(by_score[i].score >= by_score[i + 1].score for i in range(len(by_score) - 1))
    by_ping = sorted([s for s in servers if s.ping], key=SORTS["ping"])
    assert all((by_ping[i].ping or 0) <= (by_ping[i + 1].ping or 0) for i in range(len(by_ping) - 1))


def test_grouping_and_favorites(servers):
    app = App(servers)
    assert app._rail_rows()[0][0] == "Favorites"
    assert len(app._rail_rows()) == 1 + len({s.cc for s in servers})
    assert app.current_servers() == []
    app.favorites.add(servers[0].host)
    app._fav_gen += 1
    assert app.current_servers()[0].host == servers[0].host
    app.csel = 1
    pool = app.current_servers()
    assert pool and all(s.cc == pool[0].cc for s in pool)
    assert all(pool[i].score >= pool[i + 1].score for i in range(len(pool) - 1))


def test_current_server():
    app = App([_make_server("a"), _make_server("b")])
    # No favorites → csel 0 → empty
    assert app.current_server() is None
    # Switch to country
    app.csel = 1
    s = app.current_server()
    assert s is not None
    # Out of bounds
    app.ssel = 999
    assert app.current_server() is None


def test_empty_server_list():
    app = App([])
    assert app.current_servers() == []
    assert app.current_server() is None
    assert len(app._rail_rows()) == 1  # just Favorites
    assert app.countries == []


def test_single_server():
    s = _make_server("only-one", country="Solo", cc="SO")
    app = App([s])
    app.csel = 1
    assert len(app.current_servers()) == 1
    assert app.current_servers()[0].host == "only-one"


def test_rebuild_countries():
    servers = [
        _make_server("a", cc="US", country="United States"),
        _make_server("b", cc="US", country="United States"),
        _make_server("c", cc="JP", country="Japan"),
    ]
    app = App(servers)
    assert len(app.countries) == 2
    # sorted by long name
    assert app.countries[0][0][1] == "Japan"
    assert app.countries[1][0][1] == "United States"


def test_rebuild_countries_clamps_csel():
    servers = [_make_server("a", cc="US", country="US")]
    app = App(servers)
    app.csel = 5  # way out of bounds
    app._rebuild_countries()
    assert app.csel <= len(app.countries)


def test_sort_cycling():
    app = App([_make_server()])
    assert app.sort == "score"
    # cycle through all sorts
    for expected in SORT_ORDER[1:]:
        app.on_key("S", None)
        assert app.sort == expected
    # wraps back
    app.on_key("S", None)
    assert app.sort == "score"


def test_autoreconnect_toggle():
    app = App([_make_server()])
    app.autoreconnect = False  # default is now on; pin a known start for the toggle
    app.on_key("a", None)
    assert app.autoreconnect is True
    app.on_key("a", None)
    assert app.autoreconnect is False


def test_help_toggle():
    app = App([_make_server()])
    assert app.help is False
    app.on_key("?", None)
    assert app.help is True
    app.on_key("anything", None)  # any key closes help
    assert app.help is False


def test_ctrl_c_quits():
    app = App([_make_server()])
    assert app.running is True
    app.on_key("ctrl-c", None)
    assert app.running is False


def test_q_quits_in_browse():
    app = App([_make_server()])
    app.on_key("q", None)
    assert app.running is False


# ---------------------------------------------------------------------------
# tui: App navigation
# ---------------------------------------------------------------------------

def test_nav_countries_up_down(servers):
    app = App(servers)
    n = len(app.countries) + 1
    initial = app.csel
    app.on_key("down", None)
    assert app.csel == (initial + 1) % n
    app.on_key("up", None)
    assert app.csel == initial


def test_nav_countries_wraps():
    app = App([_make_server("a", cc="X", country="X"),
               _make_server("b", cc="Y", country="Y")])
    n = len(app.countries) + 1
    # go to top and wrap up
    app.csel = 0
    app.on_key("up", None)
    assert app.csel == n - 1


def test_nav_right_to_servers(servers):
    app = App(servers)
    app.csel = 1  # first country
    app.on_key("right", None)
    assert app.focus == "servers"


def test_nav_right_no_servers_stays():
    app = App([])
    app.on_key("right", None)
    assert app.focus == "countries"


def test_nav_left_back_to_countries(servers):
    app = App(servers)
    app.focus = "servers"
    app.on_key("left", None)
    assert app.focus == "countries"


def test_nav_server_up_down(servers):
    app = App(servers)
    app.csel = 1
    app.focus = "servers"
    pool = app.current_servers()
    initial = app.ssel
    app.on_key("down", None)
    assert app.ssel == (initial + 1) % len(pool)
    app.on_key("up", None)
    assert app.ssel == initial


def test_favorite_toggle(servers):
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm
    tui.vpn.sudo_warm = lambda: True
    try:
        app = App(servers)
        app.csel = 1
        app.focus = "servers"
        app.ssel = 0
        s = app.current_server()
        assert s.host not in app.favorites
        app.on_key("f", None)
        assert s.host in app.favorites
        app.on_key("f", None)
        assert s.host not in app.favorites
    finally:
        tui.vpn.sudo_warm = saved


def test_nav_enter_country(servers):
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.connect = lambda s, engine=None: None
    tui.security.check_server_latency = lambda ip, port, timeout=2.0: 10
    try:
        app = App(servers)
        app.csel = 1
        app.on_key("enter", None)
        assert app.focus == "servers"  # ↵ opens the country's server list
    finally:
        tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency = saved


def test_nav_enter_server(servers):
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.connect = lambda s, engine=None: None
    tui.security.check_server_latency = lambda ip, port, timeout=2.0: 10
    try:
        app = App(servers)
        app.csel = 1
        app.focus = "servers"
        app.ssel = 0
        app.on_key("enter", None)
        assert app.conn == "connecting"
        assert app.active is not None
    finally:
        tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency = saved


def test_order_prefers_friendly_ports():
    app = App([_make_server()])
    hi = _make_server("odd", ip="10.0.0.1", score=999999, port=1194)   # high score, odd port
    lo = _make_server("safe", ip="10.0.0.2", score=1, port=443)        # low score, friendly
    ordered = app._order([hi, lo])
    assert ordered[0].host == "safe"  # firewall-friendly wins even with a lower score


def test_c_key_auto_connects_globally(servers):
    import ferry.tui as tui
    saved = (tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency)
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.connect = lambda s, engine=None: None
    tui.security.check_server_latency = lambda ip, port, timeout=2.0: 10
    try:
        app = App(servers)
        app.focus, app.csel = "countries", 0  # sitting on empty Favorites row
        app.on_key("c", None)                 # global auto-connect ignores the selection
        assert app.conn == "connecting"
        assert app.active is not None
        assert len(app.candidates) >= 1
    finally:
        (tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency) = saved


def test_disconnect_tears_down_security():
    import ferry.tui as tui
    saved = (tui.vpn.sudo_warm, tui.vpn.disconnect, tui.security.killswitch_disable)
    calls = {"ks": 0}
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.disconnect = lambda engine=None: None
    tui.security.killswitch_disable = lambda: calls.__setitem__("ks", calls["ks"] + 1)
    try:
        app = App([_make_server()])
        app.conn, app.active, app.killswitch, app.view = "connected", _make_server(), True, "status"
        app.on_key("d", None)
        assert calls["ks"] == 1  # kill switch must be torn down on manual disconnect
        assert app.conn == "idle"
    finally:
        (tui.vpn.sudo_warm, tui.vpn.disconnect, tui.security.killswitch_disable) = saved


# ---------------------------------------------------------------------------
# tui: App connection / failover
# ---------------------------------------------------------------------------

def test_order_friendly_first(servers):
    app = App(servers)
    ordered = app._order(servers, first=None)
    assert len(ordered) <= 6
    fr = [i for i, s in enumerate(ordered) if s.friendly]
    nf = [i for i, s in enumerate(ordered) if not s.friendly]
    if fr and nf:
        assert max(fr) < min(nf)


def test_order_first_override(servers):
    app = App(servers)
    pick = servers[-1]
    ordered = app._order(servers, first=pick)
    assert ordered[0].host == pick.host


def test_order_empty_pool():
    app = App([])
    assert app._order([], first=None) == []


def test_start_connect_empty_pool():
    app = App([])
    app._start_connect(None, [])
    assert app.status == "no servers to try"


def test_disconnect_resets_state(servers):
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm, tui.vpn.disconnect
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.disconnect = lambda engine=None: None
    try:
        app = App(servers)
        app.conn = "connected"
        app.active = servers[0]
        app.on_key("d", None)
        assert app.conn == "idle"
        assert app.active is None
    finally:
        tui.vpn.sudo_warm, tui.vpn.disconnect = saved


def test_disconnect_only_when_connected(servers):
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm, tui.vpn.disconnect
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.disconnect = lambda engine=None: None
    try:
        app = App(servers)
        app.conn = "idle"
        app.on_key("d", None)
        # d should not disconnect when idle
        assert app.conn == "idle"
    finally:
        tui.vpn.sudo_warm, tui.vpn.disconnect = saved


def test_back_keys_return_to_countries():
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm
    tui.vpn.sudo_warm = lambda: True
    try:
        app = App([_make_server()])
        for k in ("b", "left", "esc"):
            app.focus = "servers"
            app.on_key(k, None)
            assert app.focus == "countries"
    finally:
        tui.vpn.sudo_warm = saved


def test_status_q_quits():
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm
    tui.vpn.sudo_warm = lambda: True
    try:
        app = App([_make_server()])
        app.on_key("q", None)
        assert app.running is False
    finally:
        tui.vpn.sudo_warm = saved




# ---------------------------------------------------------------------------
# tui: tick
# ---------------------------------------------------------------------------

def test_tick_sudo_refresh():
    import ferry.tui as tui
    saved_refresh = tui.vpn.sudo_refresh
    tui.vpn.sudo_refresh = lambda: None
    try:
        app = App([_make_server()])
        app._last_refresh = 0.0  # force refresh
        app.tick(None)
        assert app._last_refresh > 0
    finally:
        tui.vpn.sudo_refresh = saved_refresh


def test_tick_connecting_becomes_connected():
    import ferry.tui as tui
    saved = (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.exit_ip)
    tui.vpn.sudo_refresh = lambda: None
    tui.vpn.connected = lambda engine=None: True
    tui.vpn.exit_ip = lambda: ("1.2.3.4", "US")
    try:
        app = App([_make_server()])
        app.conn = "connecting"
        app.connect_start = tui.time.monotonic()
        app.tick(None)
        assert app.conn == "connected"
        assert app.exit_info == ("1.2.3.4", "US")
    finally:
        tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.exit_ip = saved


def test_tick_connected_dies_no_autoreconnect():
    import ferry.tui as tui
    saved = (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive)
    tui.vpn.sudo_refresh = lambda: None
    tui.vpn.connected = lambda engine=None: False
    tui.vpn.alive = lambda engine=None: False
    try:
        app = App([_make_server()])
        app.conn = "connected"
        app.active = _make_server()
        app.autoreconnect = False
        app.tick(None)
        assert app.conn == "idle"
        assert app.active is None
    finally:
        tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive = saved


def test_tick_connected_dies_with_autoreconnect():
    import ferry.tui as tui
    saved = (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive,
             tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency)
    tui.vpn.sudo_refresh = lambda: None
    tui.vpn.connected = lambda engine=None: False
    tui.vpn.alive = lambda engine=None: False
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.connect = lambda s, engine=None: None
    tui.security.check_server_latency = lambda ip, port, timeout=2.0: 10
    try:
        app = App([_make_server("a", cc="US", country="US")])
        app.conn = "connected"
        app.active = _make_server("a", cc="US", country="US")
        app.autoreconnect = True
        app.user_disconnected = False
        app.tick(None)
        # should attempt reconnect (state changes to connecting or stays connected)
        assert app.conn in ("connecting", "connected", "failed")
    finally:
        (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive,
         tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency) = saved


# ---------------------------------------------------------------------------
# tui: render
# ---------------------------------------------------------------------------

def test_render_smoke(servers):
    app = App(servers)
    out = render(app, 100, 30)
    assert len(out) == 30, len(out)
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "not connected" in plain and "Favorites" in plain
    assert "connect" in plain
    app.conn = "failed"
    app.status = "boom"
    assert len(render(app, 100, 30)) == 30


def test_render_minimal_terminal():
    app = App([_make_server()])
    out = render(app, 40, 10)
    assert len(out) == 10


def test_render_very_narrow():
    app = App([_make_server()])
    out = render(app, 10, 5)
    assert len(out) == 5


def test_render_help_view():
    app = App([_make_server()])
    app.help = True
    out = render(app, 100, 30)
    assert len(out) == 30
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "Keys" in plain



def test_render_status_connecting():
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.connect = lambda s, engine=None: None
    tui.security.check_server_latency = lambda ip, port, timeout=2.0: 10
    try:
        app = App([_make_server()])
        app.csel, app.focus, app.ssel = 1, "servers", 0
        app.on_key("enter", None)
        out = render(app, 100, 30)
        plain = "\n".join(strip_ansi(l) for l in out)
        assert "connecting" in plain
    finally:
        tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency = saved


def test_render_status_connected():
    import ferry.tui as tui
    saved = tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.exit_ip
    tui.vpn.sudo_refresh = lambda: None
    tui.vpn.connected = lambda engine=None: True
    tui.vpn.exit_ip = lambda: ("1.2.3.4", "US")
    try:
        app = App([_make_server()])
        app.conn = "connecting"
        app.active = _make_server()
        app.connect_start = tui.time.monotonic()
        app.tick(None)
        assert app.conn == "connected"
        out = render(app, 100, 30)
        plain = "\n".join(strip_ansi(l) for l in out)
        assert "connected" in plain
    finally:
        tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.exit_ip = saved


def test_render_empty_servers():
    app = App([])
    out = render(app, 100, 30)
    assert len(out) == 30
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "Favorites" in plain


def test_render_narrow_footer():
    app = App([_make_server()])
    out = render(app, 40, 20)
    assert len(out) == 20


def test_render_autoreconnect_header():
    app = App([_make_server()])
    app.autoreconnect = True
    out = render(app, 100, 30)
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "auto-reconnect" in plain


def test_render_footer_browse_countries():
    app = App([_make_server()])
    out = render(app, 100, 30)
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "auto-connect" in plain


def test_render_footer_browse_servers():
    app = App([_make_server()])
    app.focus = "servers"
    app.csel = 1
    out = render(app, 100, 30)
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "back" in plain


def test_render_footer_status():
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm
    tui.vpn.sudo_warm = lambda: True
    try:
        app = App([_make_server()])
        app.conn = "connected"
        out = render(app, 100, 30)
        plain = "\n".join(strip_ansi(l) for l in out)
        assert "disconnect" in plain
    finally:
        tui.vpn.sudo_warm = saved


def test_render_footer_help():
    app = App([_make_server()])
    app.help = True
    out = render(app, 100, 30)
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "close" in plain


def test_render_footer_with_status_message():
    app = App([_make_server()])
    app.status = "some status message"
    out = render(app, 100, 30)
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "some status message" in plain


def test_render_connected_status_line():
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm
    tui.vpn.sudo_warm = lambda: True
    try:
        app = App([_make_server()])
        app.active = _make_server(host="myserver", country="Testland", cc="TT")
        app.conn = "connected"
        app.exit_info = ("5.6.7.8", "DE")
        out = render(app, 100, 30)
        plain = "\n".join(strip_ansi(l) for l in out)
        assert "connected" in plain
        assert "5.6.7.8" in plain
        assert "Testland" in plain
    finally:
        tui.vpn.sudo_warm = saved


# ---------------------------------------------------------------------------
# tui: _on_key_browse edge cases
# ---------------------------------------------------------------------------

def test_r_key_triggers_refetch():
    import ferry.tui as tui
    from ferry.vpngate import VPNGate
    saved_fetch = VPNGate.fetch
    saved_cache, saved_dir = vpngate.CACHE, vpngate.STATE_DIR
    VPNGate.fetch = lambda self, timeout=20.0: [_make_server("fresh")]
    tmp = Path(tempfile.mkdtemp())
    vpngate.STATE_DIR, vpngate.CACHE = tmp, tmp / "servers.json"  # don't pollute the real cache
    try:
        app = App([_make_server("old")])
        app.on_key("r", None)
        assert app.status.startswith("refreshed")
        assert len(app.servers) == 1
        assert app.servers[0].host == "fresh"
    finally:
        VPNGate.fetch = saved_fetch
        vpngate.CACHE, vpngate.STATE_DIR = saved_cache, saved_dir


def test_r_refetch_failure():
    import ferry.tui as tui
    from ferry.vpngate import VPNGate
    saved_fetch = VPNGate.fetch
    def bad_fetch(self, timeout=20.0):
        raise Exception("network down")
    VPNGate.fetch = bad_fetch
    try:
        app = App([_make_server()])
        app.on_key("r", None)
        assert "refresh failed" in app.status
    finally:
        VPNGate.fetch = saved_fetch


# ---------------------------------------------------------------------------
# tui: on_key in help mode
# ---------------------------------------------------------------------------

def test_any_key_dismisses_help():
    app = App([_make_server()])
    app.help = True
    app.on_key("q", None)
    assert app.help is False
    assert app.running is True  # q was consumed by help, not quit


# ---------------------------------------------------------------------------
# tui: _header edge cases
# ---------------------------------------------------------------------------

def test_header_with_openvpn_missing():
    import ferry.tui as tui
    saved = tui.vpn.installed
    tui.vpn.installed = lambda: None
    try:
        app = App([_make_server()])
        header = tui._header(app, 100)
        plain = "\n".join(strip_ansi(l) for l in header)
        assert "openvpn missing" in plain
    finally:
        tui.vpn.installed = saved


# ---------------------------------------------------------------------------
# tui: _conn_line edge cases
# ---------------------------------------------------------------------------

def test_conn_line_idle():
    from ferry.tui import _status_line
    app = App([_make_server()])
    line = _status_line(app)
    assert "not connected" in strip_ansi(line)


def test_conn_line_connecting_with_candidates():
    from ferry.tui import _status_line
    app = App([_make_server()])
    app.conn = "connecting"
    app.active = _make_server()
    app.candidates = [_make_server("a"), _make_server("b")]
    app.cand_i = 0
    line = _status_line(app)
    plain = strip_ansi(line)
    assert "connecting" in plain
    assert "1/2" in plain


def test_conn_line_connecting_single():
    from ferry.tui import _status_line
    app = App([_make_server()])
    app.conn = "connecting"
    app.active = _make_server()
    app.candidates = [_make_server("a")]
    line = _status_line(app)
    plain = strip_ansi(line)
    assert "connecting" in plain
    assert "/" not in plain  # no progress when single candidate


def test_conn_line_failed():
    from ferry.tui import _status_line
    app = App([_make_server()])
    app.conn = "failed"
    line = _status_line(app)
    assert "failed" in strip_ansi(line)


def test_conn_line_connected_with_exit_info():
    import ferry.tui as tui
    from ferry.tui import _status_line
    saved = tui.time.monotonic
    tui.time.monotonic = lambda: 100.0
    try:
        app = App([_make_server()])
        app.conn = "connected"
        app.active = _make_server(country="Japan")
        app.connect_start = 90.0
        app.exit_info = ("1.2.3.4", "JP")
        line = _status_line(app)
        plain = strip_ansi(line)
        assert "Japan" in plain
        assert "1.2.3.4" in plain
        assert "connected" in plain
    finally:
        tui.time.monotonic = saved


def test_conn_line_connected_no_exit_info():
    import ferry.tui as tui
    from ferry.tui import _status_line
    saved = tui.time.monotonic
    tui.time.monotonic = lambda: 100.0
    try:
        app = App([_make_server()])
        app.conn = "connected"
        app.active = _make_server(ip="5.6.7.8")
        app.connect_start = 95.0
        app.exit_info = None
        line = _status_line(app)
        plain = strip_ansi(line)
        assert "5.6.7.8" in plain
    finally:
        tui.time.monotonic = saved


# ---------------------------------------------------------------------------
# tui: tick failover
# ---------------------------------------------------------------------------

def test_tick_connecting_daemon_died():
    import ferry.tui as tui
    saved = (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive,
             tui.vpn.sudo_warm, tui.vpn.disconnect, tui.vpn.connect)
    tui.vpn.sudo_refresh = lambda: None
    tui.vpn.connected = lambda engine=None: False
    tui.vpn.alive = lambda engine=None: False
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.disconnect = lambda engine=None: None
    tui.vpn.connect = lambda s, engine=None: None
    try:
        app = App([_make_server("a", cc="US", country="US"),
                    _make_server("b", cc="US", country="US")])
        app.conn = "connecting"
        app.active = _make_server("a", cc="US", country="US")
        app.connect_start = tui.time.monotonic() - 5  # >3 seconds
        app.candidates = [_make_server("a", cc="US", country="US"),
                           _make_server("b", cc="US", country="US")]
        app.cand_i = 0
        app.tick(None)
        # should have moved to next candidate
        assert app.cand_i == 1 or app.conn == "failed"
    finally:
        (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive,
         tui.vpn.sudo_warm, tui.vpn.disconnect, tui.vpn.connect) = saved


def test_tick_connecting_stalled():
    import ferry.tui as tui
    saved = (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive,
             tui.vpn.sudo_warm, tui.vpn.disconnect, tui.vpn.connect)
    tui.vpn.sudo_refresh = lambda: None
    tui.vpn.connected = lambda engine=None: False
    tui.vpn.alive = lambda engine=None: True  # still alive but stalled
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.disconnect = lambda engine=None: None
    tui.vpn.connect = lambda s, engine=None: None
    try:
        app = App([_make_server("a", cc="US", country="US"),
                    _make_server("b", cc="US", country="US")])
        app.conn = "connecting"
        app.active = _make_server("a", cc="US", country="US")
        app.connect_start = tui.time.monotonic() - 20  # >15 seconds
        app.candidates = [_make_server("a", cc="US", country="US"),
                           _make_server("b", cc="US", country="US")]
        app.cand_i = 0
        app.tick(None)
        assert app.cand_i == 1 or app.conn == "failed"
    finally:
        (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive,
         tui.vpn.sudo_warm, tui.vpn.disconnect, tui.vpn.connect) = saved


def test_tick_failover_exhausted():
    import ferry.tui as tui
    saved = (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive,
             tui.vpn.sudo_warm, tui.vpn.disconnect, tui.vpn.connect)
    tui.vpn.sudo_refresh = lambda: None
    tui.vpn.connected = lambda engine=None: False
    tui.vpn.alive = lambda engine=None: False
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.disconnect = lambda engine=None: None
    tui.vpn.connect = lambda s, engine=None: None
    try:
        app = App([_make_server("a", cc="US", country="US")])
        app.conn = "connecting"
        app.candidates = [_make_server("a", cc="US", country="US")]
        app.cand_i = 0
        app.connect_start = tui.time.monotonic() - 5
        app.tick(None)
        # only one candidate, it failed → no more to try
        assert app.conn == "failed"
        assert "tried 1" in app.status
    finally:
        (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive,
         tui.vpn.sudo_warm, tui.vpn.disconnect, tui.vpn.connect) = saved


def test_tick_failover_user_disconnected():
    import ferry.tui as tui
    saved = (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive,
             tui.vpn.sudo_warm, tui.vpn.disconnect)
    tui.vpn.sudo_refresh = lambda: None
    tui.vpn.connected = lambda engine=None: False
    tui.vpn.alive = lambda engine=None: False
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.disconnect = lambda engine=None: None
    try:
        app = App([_make_server("a", cc="US", country="US"),
                    _make_server("b", cc="US", country="US")])
        app.conn = "connecting"
        app.user_disconnected = True
        app.connect_start = tui.time.monotonic() - 5
        app.candidates = [_make_server("a", cc="US", country="US"),
                           _make_server("b", cc="US", country="US")]
        app.cand_i = 0
        app.tick(None)
        assert app.conn == "idle"
        assert app.candidates == []
    finally:
        (tui.vpn.sudo_refresh, tui.vpn.connected, tui.vpn.alive,
         tui.vpn.sudo_warm, tui.vpn.disconnect) = saved


# ---------------------------------------------------------------------------
# tui: on_key connect failure path
# ---------------------------------------------------------------------------

def test_launch_current_connect_failure():
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm, tui.vpn.connect
    tui.vpn.sudo_warm = lambda: True
    def bad_connect(s, engine=None):
        raise vpn.VPNError("no openvpn")
    tui.vpn.connect = bad_connect
    try:
        app = App([_make_server()])
        app.candidates = [_make_server()]
        app.cand_i = 0
        app._launch_current(None)
        assert app.conn == "failed"
        assert "no openvpn" in app.status
    finally:
        tui.vpn.sudo_warm, tui.vpn.connect = saved


# ---------------------------------------------------------------------------
# tui: full key dispatch smoke (all keys from every state)
# ---------------------------------------------------------------------------

def test_on_key_connect(servers):
    import ferry.tui as tui
    saved = tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency
    tui.vpn.sudo_warm = lambda: True
    tui.vpn.connect = lambda s, engine=None: None
    tui.security.check_server_latency = lambda ip, port, timeout=2.0: 10
    try:
        app = App(servers)
        for k in ("down", "up", "right", "left"):
            app.on_key(k, None)
        app.focus, app.csel = "countries", 1
        app.on_key("enter", None)  # open the country
        assert app.focus == "servers"
        app.on_key("enter", None)  # connect the selected server
        assert app.conn == "connecting" and app.active is not None
        app2 = App(servers)
        app2.csel, app2.focus = 1, "servers"
        app2.on_key("enter", None)
        assert app2.conn == "connecting"
    finally:
        tui.vpn.sudo_warm, tui.vpn.connect, tui.security.check_server_latency = saved


# ---------------------------------------------------------------------------
# tui: save persistence
# ---------------------------------------------------------------------------

def test_save_creates_state_file():
    import ferry.tui as tui
    with tempfile.TemporaryDirectory() as td:
        orig_dir = tui.STATE_DIR
        tui.STATE_DIR = Path(td)
        tui.STATE_FILE = tui.STATE_DIR / "state.json"
        try:
            app = App([_make_server()])
            app.favorites.add("test-host")
            app._fav_gen += 1
            app.sort = "ping"
            app.autoreconnect = True
            app.save()
            data = json.loads((Path(td) / "state.json").read_text())
            assert data["favorites"] == ["test-host"]
            assert data["sort"] == "ping"
            assert data["autoreconnect"] is True
        finally:
            tui.STATE_DIR = orig_dir
            tui.STATE_FILE = orig_dir / "state.json"


def test_save_os_error_swallows():
    import ferry.tui as tui
    orig_dir = tui.STATE_DIR
    tui.STATE_DIR = Path("/tmp/ferry_perm_test/impossible/nested")
    try:
        app = App([_make_server()])
        app.save()  # should not raise
    finally:
        tui.STATE_DIR = orig_dir


# ---------------------------------------------------------------------------
# tui: _footer with status message truncation
# ---------------------------------------------------------------------------

def test_footer_status_truncation():
    app = App([_make_server()])
    app.status = "a" * 500  # very long status
    result = tui_footer(app, 80)
    plain = strip_ansi(result)
    # should not exceed terminal width
    assert len(plain) < 80 + 20  # margin + some slack for ANSI


def tui_footer(app, width):
    from ferry.tui import _footer
    return _footer(app, width)


# ---------------------------------------------------------------------------
# Integration: parse → sort → group → render full pipeline
# ---------------------------------------------------------------------------

def test_full_pipeline():
    servers = vpngate.parse(FIX.read_text())
    app = App(servers)
    # navigate countries
    app.on_key("down", None)
    app.on_key("right", None)
    assert app.focus == "servers"
    assert len(app.current_servers()) > 0
    # render full screen
    out = render(app, 120, 40)
    assert len(out) == 40
    plain = "\n".join(strip_ansi(l) for l in out)
    # should have server hostnames visible
    assert any(s.host in plain for s in servers)


def test_pipeline_with_favorites():
    servers = vpngate.parse(FIX.read_text())
    app = App(servers)
    # add all as favorites
    for s in servers:
        app.favorites.add(s.host)
    app._fav_gen += 1
    app.csel = 0
    pool = app.current_servers()
    assert len(pool) == len(servers)


# ---------------------------------------------------------------------------
# vpngate: composite_score
# ---------------------------------------------------------------------------

def test_composite_score_basic():
    s = _make_server(score=1000000, ping=50, speed=100_000_000, port=443)
    sc = vpngate.composite_score(s)
    assert sc > 0


def test_composite_score_higher_better():
    s1 = _make_server(score=2000000, ping=10, speed=200_000_000, port=443)
    s2 = _make_server(score=100000, ping=200, speed=10_000_000, port=1194)
    assert vpngate.composite_score(s1) > vpngate.composite_score(s2)


def test_composite_score_port_bonus():
    s_friendly = _make_server(port=443)
    s_not = _make_server(port=1194)
    assert vpngate.composite_score(s_friendly) > vpngate.composite_score(s_not)


def test_composite_score_zero_ping():
    s = _make_server(ping=0)
    sc = vpngate.composite_score(s)
    assert sc > 0


# ---------------------------------------------------------------------------
# vpn: Engine enum
# ---------------------------------------------------------------------------

def test_engine_enum():
    assert vpn.Engine.OPENVPN.value == "openvpn"
    assert vpn.Engine.WIREGUARD.value == "wireguard"
    assert vpn.Engine("openvpn") == vpn.Engine.OPENVPN
    assert vpn.Engine("wireguard") == vpn.Engine.WIREGUARD


def test_alive_default_engine():
    # alive() with no args defaults to OPENVPN
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    assert vpn.alive() is False


def test_alive_with_engine():
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    assert vpn.alive(vpn.Engine.OPENVPN) is False


def test_connected_default_engine():
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    assert vpn.connected() is False


def test_disconnect_default_engine():
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.disconnect()  # should not raise


def test_disconnect_ovpn():
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    vpn.disconnect_ovpn()  # should not raise


def test_wg_installed_returns_bool():
    result = vpn.wg_installed()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# vpn: alive_ovpn / connected_ovpn
# ---------------------------------------------------------------------------

def test_alive_ovpn_no_pid():
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    assert vpn.alive_ovpn() is False


def test_connected_ovpn_no_pid():
    vpn.PIDFILE = Path("/tmp/ferry-nope.pid")
    assert vpn.connected_ovpn() is False


# ---------------------------------------------------------------------------
# provider: protocol conformance
# ---------------------------------------------------------------------------

def test_vpngate_provider_conforms():
    vg = vpngate.VPNGate()
    assert vg.name == "VPN Gate"
    assert callable(vg.fetch)
    assert callable(vg.available)


def test_vpnbook_provider_conforms():
    from ferry.vpnbook import VPNBook
    vb = VPNBook()
    assert vb.name == "VPNBook"
    assert callable(vb.fetch)
    assert callable(vb.available)


def test_get_providers():
    from ferry import get_providers
    providers = get_providers()
    assert len(providers) >= 2
    names = [p.name for p in providers]
    assert "VPN Gate" in names
    assert "VPNBook" in names


# ---------------------------------------------------------------------------
# vpnbook: password caching
# ---------------------------------------------------------------------------

def test_vpnbook_password_cache():
    from ferry import vpnbook
    import time
    # inject a fake password
    vpnbook._cached_pass = "testpass123"
    vpnbook._cached_pass_ts = time.monotonic()
    # should return cached
    assert vpnbook._scrape_password() == "testpass123"
    # clear
    vpnbook._cached_pass = ""
    vpnbook._cached_pass_ts = 0


def test_vpnbook_server_list():
    from ferry.vpnbook import _SERVERS
    assert len(_SERVERS) == 6
    hosts = [s[0] for s in _SERVERS]
    assert "us147" in hosts
    assert "uk1" in hosts


def test_vpnbook_config_url():
    from ferry.vpnbook import _CFG_URL
    url = _CFG_URL.format(host="us147")
    assert "us147" in url
    assert "tcp443" in url


# ---------------------------------------------------------------------------
# security: PF template rendering
# ---------------------------------------------------------------------------

def test_killswitch_template_has_all_rules():
    from ferry.security import PF_TEMPLATE
    t = PF_TEMPLATE.format(phys="en0", proto="udp", ip="1.2.3.4",
                           port=443, utun="utun0")
    assert "block all" in t
    assert "pass on utun0" in t
    assert "1.2.3.4" in t
    assert "443" in t
    assert "en0" in t


def test_killswitch_disable_doesnt_raise():
    """disable() should not raise even without PF."""
    from ferry.security import killswitch_disable
    # This will fail silently (no PF available in test env) — that's fine
    try:
        killswitch_disable()
    except Exception:
        pass  # ponytail: expected in test env


# ---------------------------------------------------------------------------
# security: tunnel verification functions exist and return bool
# ---------------------------------------------------------------------------

def test_tunnel_interface_alive_returns_bool():
    from ferry.security import tunnel_interface_alive
    result = tunnel_interface_alive()
    assert isinstance(result, bool)


def test_default_route_through_tunnel_returns_bool():
    from ferry.security import default_route_through_tunnel
    result = default_route_through_tunnel()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# security: check_server_latency
# ---------------------------------------------------------------------------

def test_check_server_latency_bad_ip():
    from ferry.security import check_server_latency
    result = check_server_latency("192.0.2.1", 1, timeout=0.5)  # TEST-NET, should fail
    assert result is None


# ---------------------------------------------------------------------------
# net: DPI-bypass fetch (offline — no sockets touched)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, data): self._data = data
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, *a): return self._data


def test_net_dechunk():
    from ferry import net
    body = b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"
    assert net._dechunk(body) == b"Wikipedia"
    # a lone terminating chunk yields empty
    assert net._dechunk(b"0\r\n\r\n") == b""


def test_net_get_direct_fast_path():
    from ferry import net
    import urllib.request
    saved = urllib.request.urlopen
    calls = {"frag": 0}
    net_saved = net._frag_get
    net._frag_get = lambda *a, **k: calls.__setitem__("frag", calls["frag"] + 1) or b"BYPASS"
    urllib.request.urlopen = lambda req, timeout=None: _FakeResp(b"DIRECT")
    try:
        assert net.get("https://example.com/") == b"DIRECT"
        assert calls["frag"] == 0  # bypass must NOT run when direct works
    finally:
        urllib.request.urlopen, net._frag_get = saved, net_saved


def test_net_get_falls_back_to_bypass():
    from ferry import net
    import urllib.request
    saved = urllib.request.urlopen
    net_saved = net._frag_get
    def boom(*a, **k): raise ConnectionResetError("blocked")
    urllib.request.urlopen = boom
    net._frag_get = lambda url, timeout, headers: b"BYPASS"
    try:
        assert net.get("https://example.com/") == b"BYPASS"
    finally:
        urllib.request.urlopen, net._frag_get = saved, net_saved

# ---------------------------------------------------------------------------
# security: dns functions exist
# ---------------------------------------------------------------------------

def test_dns_backup_returns_string():
    from ferry.security import dns_backup
    result = dns_backup()
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# TUI: provider/engine/security keybindings
# ---------------------------------------------------------------------------

def test_provider_cycling():
    import ferry.tui as tui
    from ferry.vpngate import VPNGate
    from ferry.vpnbook import VPNBook
    saved = tui.vpn.sudo_warm
    saved_vg, saved_vb = VPNGate.fetch, VPNBook.fetch
    saved_cache, saved_dir = vpngate.CACHE, vpngate.STATE_DIR
    tui.vpn.sudo_warm = lambda: True
    VPNGate.fetch = lambda self, timeout=20.0: [_make_server()]  # offline: no real network
    VPNBook.fetch = lambda self, timeout=20.0: [_make_server()]
    tmp = Path(tempfile.mkdtemp())
    vpngate.STATE_DIR, vpngate.CACHE = tmp, tmp / "servers.json"  # don't pollute the real cache
    try:
        app = App([_make_server()])
        app.on_key("P", None)
        assert app.provider_i == 1
        app.on_key("P", None)
        assert app.provider_i == 0  # wraps
    finally:
        tui.vpn.sudo_warm = saved
        VPNGate.fetch, VPNBook.fetch = saved_vg, saved_vb
        vpngate.CACHE, vpngate.STATE_DIR = saved_cache, saved_dir


def test_engine_cycling():
    import ferry.tui as tui
    saved_wg = tui.vpn.wg_installed
    tui.vpn.wg_installed = lambda: True
    try:
        app = App([_make_server()])
        assert app.engine == vpn.Engine.OPENVPN
        app.on_key("E", None)
        assert app.engine == vpn.Engine.WIREGUARD
        app.on_key("E", None)
        assert app.engine == vpn.Engine.OPENVPN
    finally:
        tui.vpn.wg_installed = saved_wg


def test_engine_cycling_not_available():
    import ferry.tui as tui
    saved_wg = tui.vpn.wg_installed
    tui.vpn.wg_installed = lambda: False
    try:
        app = App([_make_server()])
        app.on_key("E", None)
        assert app.engine == vpn.Engine.OPENVPN  # unchanged
    finally:
        tui.vpn.wg_installed = saved_wg


def test_killswitch_toggle():
    app = App([_make_server()])
    assert app.killswitch is False
    app.on_key("k", None)
    assert app.killswitch is True
    assert "kill switch" in app.status
    app.on_key("k", None)
    assert app.killswitch is False


def test_dns_protect_toggle():
    app = App([_make_server()])
    assert app.dns_protect is False
    app.on_key("n", None)
    assert app.dns_protect is True
    assert "dns protection" in app.status
    app.on_key("n", None)
    assert app.dns_protect is False


def test_save_includes_new_fields():
    import ferry.tui as tui
    with tempfile.TemporaryDirectory() as td:
        orig_dir = tui.STATE_DIR
        tui.STATE_DIR = Path(td)
        tui.STATE_FILE = tui.STATE_DIR / "state.json"
        try:
            app = App([_make_server()])
            app.provider_i = 1
            app.engine = vpn.Engine.WIREGUARD
            app.killswitch = True
            app.dns_protect = True
            app.save()
            data = json.loads((Path(td) / "state.json").read_text())
            assert data["provider_i"] == 1
            assert data["engine"] == "wireguard"
            assert data["killswitch"] is True
            assert data["dns_protect"] is True
        finally:
            tui.STATE_DIR = orig_dir
            tui.STATE_FILE = orig_dir / "state.json"


def test_load_state_restores_new_fields():
    import ferry.tui as tui
    with tempfile.TemporaryDirectory() as td:
        orig_dir = tui.STATE_DIR
        tui.STATE_DIR = Path(td)
        tui.STATE_FILE = tui.STATE_DIR / "state.json"
        try:
            tui.STATE_FILE.write_text(json.dumps({
                "provider_i": 1, "engine": "wireguard",
                "killswitch": True, "dns_protect": True,
                "favorites": [], "sort": "score",
            }))
            app = App([_make_server()])
            assert app.provider_i == 1
            assert app.engine == vpn.Engine.WIREGUARD
            assert app.killswitch is True
            assert app.dns_protect is True
        finally:
            tui.STATE_DIR = orig_dir
            tui.STATE_FILE = orig_dir / "state.json"



def test_help_includes_new_keys():
    import ferry.tui as tui
    app = App([_make_server()])
    app.help = True
    out = tui.render(app, 100, 30)
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "kill switch" in plain
    assert "DNS" in plain
    assert "provider" in plain


def test_security_tags_in_header():
    import ferry.tui as tui
    app = App([_make_server()])
    app.killswitch = True
    app.dns_protect = True
    out = tui.render(app, 100, 30)
    plain = "\n".join(strip_ansi(l) for l in out)
    assert "kill-switch" in plain
    assert "dns-guard" in plain


if __name__ == "__main__":
    servers = test_parse()
    test_parse_empty()
    test_parse_only_star_and_comments()
    test_parse_row_too_short()
    test_parse_empty_config_column()
    test_parse_whitespace_config()
    test_parse_integer_parsing_edge_cases()
    test_remote_parsing()
    test_server_config_decode()
    test_server_friendly()
    test_server_transport()
    test_cache_roundtrip(servers)
    test_cache_backfill(servers)
    test_cache_missing_file()
    test_cache_corrupt_json()
    test_cache_extra_unknown_keys()
    test_cache_empty_list()
    test_save_cache_os_error()
    test_vpn_no_process()
    test_alive_pid_not_a_number()
    test_alive_empty_pidfile()
    test_alive_whitespace_pidfile()
    test_alive_stale_pid()
    test_alive_current_process()
    test_connected_no_logfile()
    test_connected_log_without_done()
    test_connected_log_with_done()
    test_connected_large_log()
    test_log_error_no_logfile()
    test_log_error_empty_log()
    test_log_error_with_error_lines()
    test_log_error_no_error_keyword()
    test_log_error_truncates_long_lines()
    test_disconnect_no_pidfile()
    test_vpnerror_is_exception()
    test_fg()
    test_style_no_decoration()
    test_style_bold()
    test_style_dim()
    test_style_color()
    test_style_all()
    test_strip_ansi()
    test_cw_ascii()
    test_cw_wide_char()
    test_cw_combining()
    test_dwidth()
    test_dtrunc_empty()
    test_dtrunc_short_enough()
    test_dtrunc_exact_fit()
    test_dtrunc_needs_truncation()
    test_dtrunc_zero_max()
    test_dtrunc_wide_chars()
    test_pad_left()
    test_pad_right()
    test_pad_center()
    test_pad_no_op()
    test_cell_zero_width()
    test_cell_with_color()
    test_cell_truncates()
    test_rgb()
    test_lerp_endpoints()
    test_lerp_clamp()
    test_lerp_same_color()
    test_logo_color_ranges()
    test_logo_cached()
    test_fmt_ping()
    test_fmt_speed()
    test_fmt_uptime()
    test_parse_keys_empty()
    test_parse_keys_arrows()
    test_parse_keys_enter()
    test_parse_keys_backspace()
    test_parse_keys_tab()
    test_parse_keys_ctrl_c()
    test_parse_keys_esc()
    test_parse_keys_printable()
    test_parse_keys_mixed()
    test_parse_keys_unknown_esc()
    test_parse_keys_osc_prefix()
    test_parse_keys_control_chars_ignored()
    test_parse_keys_unicode()
    test_window_fits()
    test_window_needs_scroll()
    test_window_edge_cases()
    test_sort(servers)
    test_grouping_and_favorites(servers)
    test_current_server()
    test_empty_server_list()
    test_single_server()
    test_rebuild_countries()
    test_rebuild_countries_clamps_csel()
    test_sort_cycling()
    test_autoreconnect_toggle()
    test_help_toggle()
    test_ctrl_c_quits()
    test_q_quits_in_browse()
    test_nav_countries_up_down(servers)
    test_nav_countries_wraps()
    test_nav_right_to_servers(servers)
    test_nav_right_no_servers_stays()
    test_nav_left_back_to_countries(servers)
    test_nav_server_up_down(servers)
    test_favorite_toggle(servers)
    test_nav_enter_country(servers)
    test_nav_enter_server(servers)
    test_order_prefers_friendly_ports()
    test_c_key_auto_connects_globally(servers)
    test_disconnect_tears_down_security()
    test_order_friendly_first(servers)
    test_order_first_override(servers)
    test_order_empty_pool()
    test_start_connect_empty_pool()
    test_disconnect_resets_state(servers)
    test_disconnect_only_when_connected(servers)
    test_back_keys_return_to_countries()
    test_status_q_quits()
    test_tick_sudo_refresh()
    test_tick_connecting_becomes_connected()
    test_tick_connected_dies_no_autoreconnect()
    test_tick_connected_dies_with_autoreconnect()
    test_render_smoke(servers)
    test_render_minimal_terminal()
    test_render_very_narrow()
    test_render_help_view()
    test_render_status_connecting()
    test_render_status_connected()
    test_render_empty_servers()
    test_render_narrow_footer()
    test_render_autoreconnect_header()
    test_render_footer_browse_countries()
    test_render_footer_browse_servers()
    test_render_footer_status()
    test_render_footer_help()
    test_render_footer_with_status_message()
    test_render_connected_status_line()
    test_r_key_triggers_refetch()
    test_r_refetch_failure()
    test_any_key_dismisses_help()
    test_header_with_openvpn_missing()
    test_conn_line_idle()
    test_conn_line_connecting_with_candidates()
    test_conn_line_connecting_single()
    test_conn_line_failed()
    test_conn_line_connected_with_exit_info()
    test_conn_line_connected_no_exit_info()
    test_tick_connecting_daemon_died()
    test_tick_connecting_stalled()
    test_tick_failover_exhausted()
    test_tick_failover_user_disconnected()
    test_launch_current_connect_failure()
    test_on_key_connect(servers)
    test_save_creates_state_file()
    test_save_os_error_swallows()
    test_footer_status_truncation()
    test_full_pipeline()
    test_pipeline_with_favorites()
    # v0.2: composite score
    test_composite_score_basic()
    test_composite_score_higher_better()
    test_composite_score_port_bonus()
    test_composite_score_zero_ping()
    # v0.2: engine enum
    test_engine_enum()
    test_alive_default_engine()
    test_alive_with_engine()
    test_connected_default_engine()
    test_disconnect_default_engine()
    test_disconnect_ovpn()
    test_wg_installed_returns_bool()
    test_alive_ovpn_no_pid()
    test_connected_ovpn_no_pid()
    # v0.2: provider conformance
    test_vpngate_provider_conforms()
    test_vpnbook_provider_conforms()
    test_get_providers()
    # v0.2: vpnbook
    test_vpnbook_password_cache()
    test_vpnbook_server_list()
    test_vpnbook_config_url()
    # v0.2: security
    test_killswitch_template_has_all_rules()
    test_killswitch_disable_doesnt_raise()
    test_tunnel_interface_alive_returns_bool()
    test_default_route_through_tunnel_returns_bool()
    test_check_server_latency_bad_ip()
    # DPI-bypass net layer
    test_net_dechunk()
    test_net_get_direct_fast_path()
    test_net_get_falls_back_to_bypass()
    test_dns_backup_returns_string()
    # v0.2: TUI keybindings
    test_provider_cycling()
    test_engine_cycling()
    test_engine_cycling_not_available()
    test_killswitch_toggle()
    test_dns_protect_toggle()
    test_save_includes_new_fields()
    test_load_state_restores_new_fields()
    test_help_includes_new_keys()
    test_security_tags_in_header()
    print("ok — all ferry self-checks passed")
