"""Free Download Manager support: the proxy stays up in pass-through mode while DualLink is off."""
import time

import pytest

from duallink.controller import Controller
from duallink.engine import BalancingProxy, Link
from helpers import PeerServer, socks_connect
from test_adapters import Adapters, wait_for


def test_passthrough_does_not_bind_to_either_link():
    a, b = Link("Ethernet"), Link("Hotspot")
    a.ip, b.ip = "127.0.0.2", "127.0.0.3"
    p = BalancingProxy([a, b])
    p.passthrough = True
    srv = PeerServer("burst")
    p.start(0)
    try:
        for _ in range(4):
            c, rep = socks_connect(p.port, "127.0.0.1", srv.port)
            assert rep == 0
            c.close()
        assert wait_for(lambda: len(srv.sources) == 4)
        assert set(srv.sources) == {"127.0.0.1"}          # normal route, not 127.0.0.2 / .3
        assert a.down == b.down == 0
    finally:
        p.stop(); srv.close()


@pytest.fixture
def ctl(tmp_path):
    ad = Adapters(**{"Ethernet": "127.0.0.2", "Ethernet 2": "127.0.0.3"})
    c = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad)
    c.update_settings({"eth_name": "Ethernet", "hot_name": "Ethernet 2", "port": 0, "qbt_auto": False})
    yield c
    c.shutdown()


def test_fdm_on_keeps_proxy_available_while_off(ctl):
    srv = PeerServer("burst")
    try:
        assert not ctl.proxy_up()
        ctl.update_settings({"fdm": True})
        assert ctl.proxy_up() and ctl.proxy.passthrough
        assert ctl.status()["standby"] is True
        socks_connect(ctl.proxy.port, "127.0.0.1", srv.port)[0].close()
        assert wait_for(lambda: srv.sources == ["127.0.0.1"])        # works while "off"

        port = ctl.proxy.port
        ctl.cfg["port"] = port                                        # same port when turning on
        assert ctl.power_on()["ok"]
        assert ctl.proxy.port == port and not ctl.proxy.passthrough   # no restart: FDM keeps its setting
        pieces = [socks_connect(port, "127.0.0.1", srv.port)[0] for _ in range(4)]   # FDM's parallel pieces
        assert wait_for(lambda: len(srv.sources) == 5)
        assert sorted(srv.sources[1:]) == ["127.0.0.2"] * 2 + ["127.0.0.3"] * 2      # spread over both links
        for c in pieces:
            c.close()

        ctl.power_off()
        assert ctl.proxy_up() and ctl.proxy.passthrough               # still there for FDM
        socks_connect(port, "127.0.0.1", srv.port)[0].close()
        assert wait_for(lambda: srv.sources[-1] == "127.0.0.1")
    finally:
        srv.close()


def test_fdm_off_stops_proxy_when_dualink_is_off(ctl):
    ctl.update_settings({"fdm": True})
    assert ctl.proxy_up()
    ctl.update_settings({"fdm": False})
    assert not ctl.proxy_up()
    assert ctl.power_on()["ok"] and ctl.proxy_up()
    ctl.power_off()
    assert not ctl.proxy_up()                                         # qBittorrent-only: fully off


def test_quitting_stops_passthrough(ctl):
    ctl.update_settings({"fdm": True})
    ctl.shutdown()
    assert not ctl.proxy_up()


def test_busy_port_is_reported(ctl):
    import socket
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0)); blocker.listen(1)
    try:
        ctl.update_settings({"port": blocker.getsockname()[1], "fdm": True})
        assert ctl.status()["standby_error"]
    finally:
        blocker.close()
