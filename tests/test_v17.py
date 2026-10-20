"""1.7: more than two connections, and live app status."""
import json
import threading
import time

import pytest

from duallink.controller import Controller, SettingsError, classify_clients
from helpers import PeerServer, reconnecting_client, socks_connect
from test_adapters import Adapters, wait_for
from test_v16 import ThrottledServer


def make(tmp_path, extra=("Wi-Fi",)):
    ad = Adapters(**{"Ethernet": "127.0.0.1", "Ethernet 2": "127.0.0.2", "Wi-Fi": "127.0.0.3", "Ethernet 3": "127.0.0.4"})
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad, ipv6_lister=lambda: {})
    ctl.watch_every = 0.2
    ctl.update_settings({"eth_name": "Ethernet", "hot_name": "Ethernet 2", "extra": list(extra),
                         "port": 0, "qbt_auto": False, "auto": False})
    notes = []
    ctl.notify = lambda title, msg: notes.append(title)
    return ctl, ad, notes


# ---- more than two connections ------------------------------------------------------- #
def test_three_links_share_connections(tmp_path):
    ctl, ad, notes = make(tmp_path)
    srv = PeerServer("idle")
    try:
        assert len(ctl.links) == 3 and ctl.links[2].name == "Wi-Fi"
        assert ctl.power_on()["ok"]
        conns = [socks_connect(ctl.proxy.port, "127.0.0.1", srv.port)[0] for _ in range(9)]
        assert wait_for(lambda: len(srv.sources) == 9)
        assert sorted(srv.sources) == ["127.0.0.1"] * 3 + ["127.0.0.2"] * 3 + ["127.0.0.3"] * 3
        st = ctl.status()["links"]
        assert [l["name"] for l in st] == ["Ethernet", "Hotspot", "Wi-Fi"]
        assert [l["kind"] for l in st] == ["eth", "hot", "extra"]
        assert [l["connections"] for l in st] == [3, 3, 3]
        for c in conns:
            c.close()
    finally:
        ctl.shutdown(); srv.close()


def test_add_and_remove_a_link_while_on(tmp_path):
    ctl, ad, notes = make(tmp_path, extra=())
    srv = PeerServer("idle")
    try:
        assert ctl.power_on()["ok"] and len(ctl.links) == 2
        ctl.update_settings({"extra": ["Wi-Fi", "Ethernet 3"]})      # added while running: used right away
        assert len(ctl.links) == 4 and ctl.links[3].ip == "127.0.0.4"
        conns = [socks_connect(ctl.proxy.port, "127.0.0.1", srv.port)[0] for _ in range(8)]
        assert wait_for(lambda: len(set(srv.sources)) == 4)
        ctl.update_settings({"extra": ["Ethernet 3"]})               # Wi-Fi removed
        assert [l.name for l in ctl.links][2:] == ["Ethernet 3"] and ctl.links[2].ip == "127.0.0.4"
        assert [l["name"] for l in ctl.status()["links"]] == ["Ethernet", "Hotspot", "Ethernet 3"]
        for c in conns:
            c.close()
    finally:
        ctl.shutdown(); srv.close()


def test_each_adapter_only_once(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        with pytest.raises(SettingsError):
            ctl.update_settings({"extra": ["Ethernet"]})              # already the Ethernet link
        with pytest.raises(SettingsError):
            ctl.update_settings({"extra": ["Wi-Fi", "Wi-Fi"]})
        assert ctl.extra_names() == ["Wi-Fi"]
        ctl.update_settings({"extra": ["Wi-Fi", "Ethernet 3", "Something"]})
        assert ctl.extra_names() == ["Wi-Fi", "Ethernet 3"]           # at most two extra links
    finally:
        ctl.shutdown()


def test_extra_link_unplugged_and_back(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        assert ctl.power_on()["ok"]
        del ad.named["Wi-Fi"]
        assert wait_for(lambda: ctl.links[2].ip == "")
        assert "Wi-Fi connection disconnected" in notes
        assert ctl.status()["links"][2]["present"] is False
        ad.named["Wi-Fi"] = "127.0.0.3"
        assert wait_for(lambda: ctl.links[2].ip == "127.0.0.3")
    finally:
        ctl.shutdown()


def test_speed_test_and_memory_cover_all_links(tmp_path):
    srv = ThrottledServer({"127.0.0.1": 200_000, "127.0.0.2": 600_000, "127.0.0.3": 400_000})
    ctl, ad, notes = make(tmp_path)
    try:
        ctl.speedtest_url, ctl.speedtest_seconds = srv.url, 1.5
        assert ctl.start_speed_test()["ok"]
        assert wait_for(lambda: not ctl.speedtest_state["running"], timeout=15)
        res = ctl.status()["speedtest"]["result"]
        assert [r["label"] for r in res["links"]] == ["Ethernet", "Hotspot", "Wi-Fi"]
        bps = [r["bps"] for r in res["links"]]
        assert bps[1] > bps[2] > bps[0] > 0
        ctl.update_settings({"auto": True})
        assert ctl.power_on()["ok"]
        w = [l.weight for l in ctl.links]
        assert w[1] > w[2] > w[0]                                      # balancing starts from the test
        now = time.monotonic()
        for l, v in zip(ctl.links, (1.0, 2.0, 3.0)):
            l.per_conn, l.measured_at = v, now
        ctl.power_off()
        learned = json.loads(ctl.cfg_path.read_text())["learned"]
        assert {k: v["per_conn"] for k, v in learned.items()} == {"Ethernet": 1.0, "Ethernet 2": 2.0, "Wi-Fi": 3.0}
    finally:
        ctl.shutdown(); srv.close()


# ---- live app status ------------------------------------------------------------------- #
def test_classify_clients():
    live = classify_clients({"fdm.exe": 9, "qBittorrent.exe": 40, "IDMan.exe": 2, "chrome.exe": 3, "javaw.exe": 4})
    assert live["fdm"] == {"connections": 9, "programs": ["fdm.exe"]}
    assert live["qbt"]["connections"] == 40 and live["idm"]["connections"] == 2 and live["jd"]["connections"] == 4
    assert live["other"] == {"connections": 3, "programs": ["chrome.exe"]}
    assert live["firefox"]["connections"] == 0


def test_live_status_and_seen_apps(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        counts = {"fdm.exe": 9}
        ctl.app_probe = lambda port, pid: dict(counts)
        ctl.check_apps()
        assert ctl.status()["app_live"] == {}                          # proxy not running: nothing to see
        ctl.update_settings({"fdm": True})                             # pass-through proxy now up
        ctl.check_apps()
        st = ctl.status()
        assert st["app_live"]["fdm"]["connections"] == 9 and "fdm" in st["seen_apps"]
        assert "fdm" in json.loads(ctl.cfg_path.read_text())["seen_apps"]   # remembered across restarts
        counts.clear()                                                 # download finished: idle
        ctl.check_apps()
        st = ctl.status()
        assert st["app_live"]["fdm"]["connections"] == 0 and "fdm" in st["seen_apps"]
        assert "seen_apps" not in ctl.public_settings()
    finally:
        ctl.shutdown()


def test_live_status_updates_by_itself(tmp_path):
    ctl, ad, notes = make(tmp_path)
    try:
        ctl.app_every = 0.2
        ctl.app_probe = lambda port, pid: {"IDMan.exe": 3}
        ctl.update_settings({"idm": True})
        assert wait_for(lambda: (ctl.status()["app_live"].get("idm") or {}).get("connections") == 3)
    finally:
        ctl.shutdown()
