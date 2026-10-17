import threading
import time

import pytest

from duallink.engine import BalancingProxy, Link
from helpers import PeerServer, reconnecting_client, socks_connect

ETH, HOT = "127.0.0.1", "127.0.0.2"   # two loopback addresses stand in for the two links


@pytest.fixture
def links():
    a, b = Link("Ethernet"), Link("Hotspot")
    a.ip, b.ip = ETH, HOT
    return [a, b]


@pytest.fixture
def fast_balancer(monkeypatch):
    monkeypatch.setattr(BalancingProxy, "BAL_PERIOD", 0.5)
    monkeypatch.setattr(BalancingProxy, "IDLE_AFTER", 2)
    monkeypatch.setattr(BalancingProxy, "MIN_AGE", 2)
    monkeypatch.setattr(BalancingProxy, "MOVE_EVERY", 0.6)


def read_all(c, n):
    got = 0
    while got < n:
        b = c.recv(65536)
        if not b:
            break
        got += len(b)
    return got


def test_relays_data_and_splits_by_weight(links):
    links[0].weight, links[1].weight = 2, 1
    p = BalancingProxy(links)
    p.auto = False
    srv = PeerServer("burst")
    p.start(0)
    try:
        conns = [socks_connect(p.port, "127.0.0.1", srv.port) for _ in range(30)]
        assert all(rep == 0 for _, rep in conns)
        assert sum(read_all(c, 200_000) for c, _ in conns) == 30 * 200_000
        assert srv.sources.count(ETH) == 20 and srv.sources.count(HOT) == 10
    finally:
        p.stop(); srv.close()


def test_burst_of_connections_is_spread(links):
    """qBittorrent opens many peer connections at once; they must not all land on one link."""
    p = BalancingProxy(links)
    srv = PeerServer("idle")
    p.start(0)
    try:
        out = []
        ts = [threading.Thread(target=lambda: out.append(socks_connect(p.port, "127.0.0.1", srv.port)[0]))
              for _ in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        time.sleep(0.3)
        assert 8 <= srv.sources.count(ETH) <= 12 and 8 <= srv.sources.count(HOT) <= 12, srv.sources
        for c in out:
            c.close()
    finally:
        p.stop(); srv.close()


def test_falls_back_when_link_disabled(links):
    links[1].enabled = False
    p = BalancingProxy(links)
    srv = PeerServer("burst")
    p.start(0)
    try:
        for _ in range(4):
            c, rep = socks_connect(p.port, "127.0.0.1", srv.port)
            assert rep == 0
            c.close()
        time.sleep(0.2)
        assert set(srv.sources) == {ETH}
    finally:
        p.stop(); srv.close()


def test_restart_on_same_port(links):
    p = BalancingProxy(links)
    srv = PeerServer("idle")
    p.start(0)
    port = p.port
    for _ in range(3):
        c, rep = socks_connect(port, "127.0.0.1", srv.port)
        assert rep == 0
        p.stop()
        p.start(port)  # must not fail with "address already in use"
        c.close()
    p.stop(); srv.close()


def test_quota_stops_using_link(links):
    links[1].quota_bytes = 1        # hotspot limit already reached after the first byte
    links[1].down = 10
    p = BalancingProxy(links)
    srv = PeerServer("burst")
    p.start(0)
    try:
        for _ in range(4):
            socks_connect(p.port, "127.0.0.1", srv.port)[0].close()
        time.sleep(0.2)
        assert set(srv.sources) == {ETH}
    finally:
        p.stop(); srv.close()


def _run_scenario(links, setup):
    p = BalancingProxy(links)
    data = PeerServer("stream", rates={ETH: 60_000, HOT: 600_000})   # hotspot is 10x faster
    idle = PeerServer("idle")
    stop = threading.Event()
    p.start(0)
    try:
        setup(p, data, idle, stop)
        time.sleep(9)
        return p, [l.active for l in links], [l.weight for l in links]
    finally:
        stop.set(); p.stop(); data.close(); idle.close()


def _clients(p, port, n, stop, gap=0.15):
    for _ in range(n):
        threading.Thread(target=reconnecting_client, args=(p.port, port, stop), daemon=True).start()
        time.sleep(gap)


def test_auto_favours_faster_link_and_moves_idle_peers(links, fast_balancer):
    def setup(p, data, idle, stop):
        links[1].enabled = False                      # first peers all land on Ethernet
        _clients(p, data.port, 2, stop)
        _clients(p, idle.port, 8, stop, gap=0.05)
        time.sleep(0.5)
        links[1].enabled = True
        _clients(p, data.port, 2, stop)
    p, active, weights = _run_scenario(links, setup)
    assert weights[1] / sum(weights) > 0.75            # hotspot measured as much faster
    assert p.moved >= 5                                # idle peers were moved off Ethernet
    assert active[1] > active[0]


def test_unmeasured_link_is_not_penalised(links, fast_balancer):
    def setup(p, data, idle, stop):
        links[1].enabled = False                      # all data peers on Ethernet...
        _clients(p, data.port, 4, stop)
        time.sleep(0.5)
        links[1].enabled = True
        _clients(p, idle.port, 8, stop, gap=0.05)      # ...hotspot only ever sees idle peers
    p, active, weights = _run_scenario(links, setup)
    assert weights == [1.0, 1.0]                      # even split: no evidence the hotspot is slow
    assert p.moved == 0
