import pytest

from duallink.controller import Controller
from duallink.qbt import QbtError, QbtWebUI
from helpers import FakeQbt


@pytest.fixture(params=[True, False], ids=["qbt5", "qbt4"])
def qbt(request):
    q = FakeQbt(v5=request.param)
    yield q
    q.close()


def test_apply_and_restore(qbt):
    original = dict(qbt.prefs)
    c = QbtWebUI(qbt.url)
    saved = c.apply_dual_link(1080)
    p = qbt.prefs
    assert p["proxy_type"] == ("SOCKS5" if qbt.v5 else 2)
    assert (p["proxy_ip"], p["proxy_port"]) == ("127.0.0.1", 1080)
    assert p["proxy_peer_connections"] and p["proxy_bittorrent"]
    assert p["bittorrent_protocol"] == 1
    assert p["max_connec"] == 500 and p["max_connec_per_torrent"] == 200
    c.restore(saved)
    assert qbt.prefs == original


def test_reconnect_uses_right_api_version(qbt):
    c = QbtWebUI(qbt.url)
    c.reconnect(c.downloading_hashes())
    posts = [p for m, p in qbt.calls if m == "POST"]
    if qbt.v5:
        assert posts[-2:] == ["/api/v2/torrents/stop", "/api/v2/torrents/start"]
    else:
        assert posts[-2:] == ["/api/v2/torrents/pause", "/api/v2/torrents/resume"]


def test_login_and_auth_errors():
    q = FakeQbt(username="admin", password="secret")
    try:
        assert QbtWebUI(q.url, "admin", "secret").version() == "v5.2.4"
        with pytest.raises(QbtError) as e:
            QbtWebUI(q.url, "admin", "wrong").version()
        assert e.value.kind == "auth"
        with pytest.raises(QbtError) as e:
            QbtWebUI(q.url).version()          # no credentials, no localhost bypass
        assert e.value.kind == "auth"
    finally:
        q.close()


def test_unreachable():
    with pytest.raises(QbtError) as e:
        QbtWebUI("http://127.0.0.1:1", timeout=1).version()
    assert e.value.kind == "unreachable"


def make_ctl(tmp_path, qbt_url):
    adapters = lambda: [("Ethernet adapter Ethernet", "127.0.0.1"), ("Ethernet adapter Ethernet 2", "127.0.0.2")]
    ctl = Controller(cfg_path=tmp_path / "cfg.json", adapter_lister=adapters)
    ctl.update_settings({"eth_ip": "127.0.0.1", "hot_ip": "127.0.0.2", "port": 0, "qbt_url": qbt_url})
    return ctl


def test_controller_power_cycle_configures_and_restores_qbt(tmp_path):
    q = FakeQbt()
    original = dict(q.prefs)
    try:
        ctl = make_ctl(tmp_path, q.url)
        assert ctl.power_on() == {"ok": True}
        assert ctl.status()["running"] and ctl.qbt_state["state"] == "connected"
        assert q.prefs["proxy_port"] == ctl.proxy.port
        assert ctl.cfg["qbt_saved"]                      # originals kept for crash recovery
        assert ctl.power_off() == {"ok": True}
        assert q.prefs == original and ctl.cfg["qbt_saved"] is None
    finally:
        q.close()


def test_controller_runs_without_qbt(tmp_path):
    ctl = make_ctl(tmp_path, "http://127.0.0.1:1")
    assert ctl.power_on()["ok"]
    assert ctl.qbt_state["state"] == "unreachable"
    ctl.power_off()


def test_controller_rejects_bad_setup(tmp_path):
    ctl = make_ctl(tmp_path, "http://127.0.0.1:1")
    ctl.update_settings({"hot_ip": "127.0.0.1"})
    assert not ctl.power_on()["ok"]
    ctl.update_settings({"hot_ip": "10.9.9.9"})        # adapter that doesn't exist
    ctl.cfg["hot_ip"] = "10.9.9.9"
    ctl._list_adapters = lambda: [("Ethernet adapter Ethernet", "127.0.0.1")]
    r = ctl.power_on()
    assert not r["ok"]


def test_adapter_names_and_hotspot_guess(tmp_path):
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=lambda: [
        ("Ethernet adapter Ethernet", "192.168.8.5"), ("Ethernet adapter Ethernet 2", "172.20.10.3")])
    items = ctl.adapters()
    assert items[0]["name"] == "Ethernet" and items[1]["hotspot"]
    assert ctl.cfg["eth_ip"] == "192.168.8.5" and ctl.cfg["hot_ip"] == "172.20.10.3"
