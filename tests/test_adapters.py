"""Following the connections by adapter name: new IPs, unplugging, re-plugging, starting without the phone."""
import json
import time

import pytest

from duallink.controller import Controller
from helpers import PeerServer, socks_connect


class Adapters:
    """A changeable fake of the PC's network adapters (what ipconfig would show)."""

    def __init__(self, **named):
        self.named = dict(named)

    def __call__(self):
        return [(f"Ethernet adapter {n}", ip) for n, ip in self.named.items()]


@pytest.fixture
def env(tmp_path):
    ad = Adapters(**{"Ethernet": "127.0.0.1", "Ethernet 2": "127.0.0.2"})
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad)
    ctl.watch_every = 0.2
    ctl.update_settings({"eth_name": "Ethernet", "hot_name": "Ethernet 2", "port": 0,
                         "qbt_auto": False, "auto": False})
    notes = []
    ctl.notify = lambda title, msg: notes.append(title)
    srv = PeerServer("idle")
    yield ctl, ad, notes, srv
    ctl.power_off()
    srv.close()


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def open_conns(ctl, srv, n):
    return [socks_connect(ctl.proxy.port, "127.0.0.1", srv.port)[0] for _ in range(n)]


def test_hotspot_gets_new_ip(env):
    ctl, ad, notes, srv = env
    assert ctl.power_on()["ok"]
    ad.named["Ethernet 2"] = "127.0.0.3"                     # phone reconnected with a new address
    assert wait_for(lambda: ctl.links[1].ip == "127.0.0.3")
    assert "Hotspot got a new address" in notes
    conns = open_conns(ctl, srv, 6)
    assert wait_for(lambda: "127.0.0.3" in srv.sources)       # new connections use the new IP
    assert "127.0.0.2" not in srv.sources
    assert ctl.cfg["hot_ip"] == "127.0.0.3"
    assert json.loads(ctl.cfg_path.read_text())["hot_name"] == "Ethernet 2"
    for c in conns:
        c.close()


def test_unplug_and_replug_phone(env):
    ctl, ad, notes, srv = env
    assert ctl.power_on()["ok"]
    conns = open_conns(ctl, srv, 6)
    assert wait_for(lambda: ctl.links[1].active == 3)
    del ad.named["Ethernet 2"]                               # phone unplugged
    assert wait_for(lambda: ctl.links[1].ip == "")
    assert wait_for(lambda: ctl.links[1].active == 0)         # its connections were closed right away
    assert "Hotspot disconnected" in notes
    st = ctl.status()["links"][1]
    assert st["present"] is False and st["adapter"] == "Ethernet 2"
    n_before = len(srv.sources)
    more = open_conns(ctl, srv, 3)                            # qBittorrent reconnects -> Ethernet only
    assert wait_for(lambda: len(srv.sources) == n_before + 3)
    assert set(srv.sources[n_before:]) == {"127.0.0.1"}
    ad.named["Ethernet 2"] = "127.0.0.2"                     # plugged back in
    assert wait_for(lambda: ctl.links[1].ip == "127.0.0.2")
    assert "Hotspot connected" in notes
    assert ctl.cfg["hot_name"] == "Ethernet 2"               # the saved choice was never replaced
    for c in conns + more:
        c.close()


def test_start_without_phone_then_it_joins(env):
    ctl, ad, notes, srv = env
    del ad.named["Ethernet 2"]
    r = ctl.power_on()
    assert r["ok"] and "will add the hotspot automatically" in r["message"]
    assert ctl.links[1].ip == "" and ctl.links[0].ip == "127.0.0.1"
    ad.named["Ethernet 2"] = "127.0.0.2"
    assert wait_for(lambda: ctl.links[1].ip == "127.0.0.2")


def test_phone_returns_under_a_different_adapter_name(env):
    ctl, ad, notes, srv = env
    assert ctl.power_on()["ok"]
    del ad.named["Ethernet 2"]
    assert wait_for(lambda: ctl.links[1].ip == "")
    ad.named["Ethernet 3"] = "172.20.10.4"                   # iPhone on another USB port
    assert wait_for(lambda: ctl.cfg["hot_name"] == "Ethernet 3")
    assert ctl.links[1].ip == "172.20.10.4"


def test_switching_connection_in_settings_applies_immediately(env):
    ctl, ad, notes, srv = env
    ad.named["Wi-Fi"] = "127.0.0.4"
    assert ctl.power_on()["ok"]
    ctl.update_settings({"hot_name": "Wi-Fi"})
    assert ctl.links[1].ip == "127.0.0.4"


def test_settings_from_older_version_are_migrated(tmp_path):
    (tmp_path / "c.json").write_text(json.dumps({"eth_ip": "192.168.8.5", "hot_ip": "172.20.10.3"}))
    ad = Adapters(**{"Ethernet": "192.168.8.5", "Ethernet 2": "172.20.10.3"})
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad)
    ctl.adapters()
    assert ctl.cfg["eth_name"] == "Ethernet" and ctl.cfg["hot_name"] == "Ethernet 2"


def test_unplugged_choice_stays_in_the_list(tmp_path):
    ad = Adapters(**{"Ethernet": "192.168.8.5"})
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad)
    ctl.cfg.update(eth_name="Ethernet", hot_name="Ethernet 2")
    items = ctl.adapters()
    assert ctl.cfg["hot_name"] == "Ethernet 2"                # not silently replaced
    assert {"name": "Ethernet 2", "ip": "", "ip6": "", "hotspot": False, "present": False} in items
