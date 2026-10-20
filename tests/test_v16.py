"""1.6: turning on automatically, remembered speeds, speed test, update checks, more apps, uninstall clean-up."""
import http.server
import json
import threading
import time

import pytest

from duallink import updates
from duallink.controller import Controller, cleanup
from duallink.speedtest import measure
from helpers import FakeQbt
from test_adapters import Adapters, wait_for


def make(tmp_path, **adapters):
    ad = Adapters(**(adapters or {"Ethernet": "127.0.0.1", "Ethernet 2": "127.0.0.2"}))
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad, ipv6_lister=lambda: {})
    ctl.watch_every = 0.2
    ctl.update_settings({"eth_name": "Ethernet", "hot_name": "Ethernet 2", "port": 0, "qbt_auto": False})
    notes = []
    ctl.notify = lambda title, msg: notes.append(title)
    return ctl, ad, notes


# ---- 1. turning on automatically ------------------------------------------------ #
def test_turns_on_when_hotspot_connects(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        del ad.named["Ethernet 2"]                       # phone not connected yet
        time.sleep(0.5)
        assert not ctl.running
        ad.named["Ethernet 2"] = "127.0.0.2"              # phone plugged in
        assert wait_for(lambda: ctl.running)
        assert "DualLink turned on" in notes
    finally:
        ctl.shutdown()


def test_does_not_turn_back_on_after_user_turns_it_off(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        time.sleep(0.5)                                  # hotspot already there at start: no switch-on
        assert not ctl.running
        assert ctl.power_on()["ok"]
        ctl.power_off()                                   # user turns it off while the phone stays connected
        time.sleep(0.8)
        assert not ctl.running
    finally:
        ctl.shutdown()


def test_auto_on_can_be_switched_off(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        ctl.update_settings({"auto_on_hotspot": False})
        del ad.named["Ethernet 2"]
        time.sleep(0.5)
        ad.named["Ethernet 2"] = "127.0.0.2"
        time.sleep(0.8)
        assert not ctl.running
    finally:
        ctl.shutdown()


def test_turn_on_when_dualink_starts(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        assert ctl.startup()["ok"] and not ctl.running   # off by default
        ctl.update_settings({"auto_on_start": True})
        assert ctl.startup()["ok"] and ctl.running
    finally:
        ctl.shutdown()


# ---- 3. remembered speeds -------------------------------------------------------- #
def test_speeds_are_remembered_and_used_next_time(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        assert ctl.power_on()["ok"]
        eth, hot = ctl.links
        now = time.monotonic()
        eth.per_conn, eth.measured_at = 200_000.0, now      # as if measured during this session
        hot.per_conn, hot.measured_at = 800_000.0, now
        ctl.power_off()
        saved = json.loads(ctl.cfg_path.read_text())["learned"]
        assert saved["Ethernet"]["per_conn"] == 200_000.0 and saved["Ethernet 2"]["per_conn"] == 800_000.0
    finally:
        ctl.shutdown()
    ctl2, _, _ = make(tmp_path)                           # next session reads the same config
    try:
        assert ctl2.power_on()["ok"]
        shares = [l.weight for l in ctl2.links]
        assert shares[1] / sum(shares) == pytest.approx(0.8, abs=0.01)   # hotspot favoured from the first second
        assert ctl2.status()["links"][1]["share"] == 80
    finally:
        ctl2.shutdown()


def test_no_memory_means_even_start(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        assert ctl.power_on()["ok"]
        assert [l.weight for l in ctl.links] == [1.0, 1.0]
    finally:
        ctl.shutdown()


# ---- 2. speed test ----------------------------------------------------------------- #
class ThrottledServer:
    """Serves endless bytes; speed depends on which local address the client came from."""

    def __init__(self, rates):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                rate = rates.get(self.client_address[0], 100_000)
                self.send_response(200)
                self.send_header("Content-Length", str(10**9))
                self.end_headers()
                try:
                    while True:
                        self.wfile.write(b"x" * (rate // 20))
                        time.sleep(0.05)
                except OSError:
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/__down?bytes=1000000000"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_measure_per_link():
    srv = ThrottledServer({"127.0.0.1": 400_000, "127.0.0.2": 1_600_000})
    try:
        slow = measure("127.0.0.1", seconds=2.0, streams=2, url=srv.url, warmup=0.5)
        fast = measure("127.0.0.2", seconds=2.0, streams=2, url=srv.url, warmup=0.5)
        assert slow["error"] is None and fast["error"] is None
        assert 2.5 < fast["bps"] / slow["bps"] < 6           # ~4x faster
        assert 600_000 < slow["bps"] < 1_000_000              # 2 streams x 400 KB/s
    finally:
        srv.close()


def test_measure_reports_errors():
    r = measure("127.0.0.1", seconds=1.0, streams=1, url="http://127.0.0.1:1/x", warmup=0.2)
    assert r["bps"] == 0 and r["error"]


def test_speed_test_saves_result_and_seeds_balancing(tmp_path):
    srv = ThrottledServer({"127.0.0.1": 300_000, "127.0.0.2": 900_000})
    ctl, ad, notes = make(tmp_path)
    try:
        ctl.speedtest_url, ctl.speedtest_seconds = srv.url, 1.5
        assert ctl.start_speed_test()["ok"]
        assert wait_for(lambda: not ctl.speedtest_state["running"], timeout=10)
        res = ctl.status()["speedtest"]["result"]
        assert res["hot"]["bps"] > 2 * res["eth"]["bps"] > 0
        assert ctl.power_on()["ok"]                           # no remembered speeds: uses the speed test
        w = [l.weight for l in ctl.links]
        assert w[1] / sum(w) > 0.65
    finally:
        ctl.shutdown(); srv.close()


# ---- 5. updates ------------------------------------------------------------------------ #
def test_version_comparison():
    assert updates.is_newer("v1.6.1", "1.6.0") and updates.is_newer("v2.0", "1.9.9")
    assert not updates.is_newer("v1.6.0", "1.6.0") and not updates.is_newer("v1.5.9", "1.6.0")


def test_update_check(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        release = {"tag_name": "v99.0.0", "html_url": "https://github.com/me/DualLink/releases/tag/v99.0.0",
                   "assets": [{"name": "DualLink-Setup-99.0.0.exe",
                               "browser_download_url": "https://github.com/me/DualLink/releases/download/v99.0.0/DualLink-Setup-99.0.0.exe"}]}
        r = ctl.check_updates_now(fetch=lambda repo: release)
        assert r["ok"] and r["update"]["version"] == "99.0.0" and r["update"]["installer"].endswith(".exe")
        assert ctl.status()["update"]["version"] == "99.0.0"
        assert "DualLink 99.0.0 is available" in notes
        r = ctl.check_updates_now(fetch=lambda repo: dict(release, tag_name="v0.1.0"))
        assert r["ok"] and r["update"] is None
    finally:
        ctl.shutdown()


def test_update_check_from_source_explains(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "REPO", "")
    ctl, ad, notes = make(tmp_path)
    try:
        r = ctl.check_updates_now()
        assert not r["ok"] and "GitHub" in r["message"]
    finally:
        ctl.shutdown()


# ---- 6. more apps ------------------------------------------------------------------------ #
@pytest.mark.parametrize("app", ["jd", "idm", "firefox", "other"])
def test_any_manual_app_keeps_passthrough_available(tmp_path, app):
    ctl, ad, notes = make(tmp_path)
    try:
        assert not ctl.proxy_up()
        ctl.update_settings({app: True})
        assert ctl.proxy_up() and ctl.proxy.passthrough
        assert ctl.status()["apps"][app] is True
        assert ctl.power_on()["ok"]
        ctl.power_off()
        assert ctl.proxy_up() and ctl.proxy.passthrough       # still there for the app while off
        ctl.update_settings({app: False})
        assert not ctl.proxy_up()
    finally:
        ctl.shutdown()


def test_internal_values_not_overwritten_by_the_interface(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        ctl.cfg["learned"] = {"Ethernet": {"per_conn": 5.0, "ts": time.time()}}
        pub = ctl.public_settings()
        assert "learned" not in pub and "speedtest" not in pub and "qbt_saved" not in pub
        ctl.update_settings({"learned": {}, "speedtest": {"x": 1}})
        assert ctl.cfg["learned"]["Ethernet"]["per_conn"] == 5.0 and ctl.cfg["speedtest"] is None
    finally:
        ctl.shutdown()


# ---- 4. uninstall clean-up ------------------------------------------------------------------ #
def test_cleanup_restores_qbittorrent(tmp_path):
    q = FakeQbt()
    try:
        original = dict(q.prefs)
        from duallink.qbt import QbtWebUI
        saved = QbtWebUI(q.url).apply_dual_link(1080)          # DualLink was on when it got uninstalled
        (tmp_path / "c.json").write_text(json.dumps({"qbt_url": q.url, "qbt_saved": saved}))
        cleanup(tmp_path / "c.json")
        assert q.prefs == original
        assert json.loads((tmp_path / "c.json").read_text())["qbt_saved"] is None
    finally:
        q.close()
