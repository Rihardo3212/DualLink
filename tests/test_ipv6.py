"""IPv6 destinations (apps that look up addresses themselves and hand DualLink an IPv6 address)."""
import socket
import struct
import threading

import pytest

from duallink.controller import Controller
from duallink.engine import BalancingProxy, Link
from test_adapters import Adapters


def ipv6_available():
    try:
        s = socket.socket(socket.AF_INET6)
        s.bind(("::1", 0))
        s.close()
        return True
    except OSError:
        return False


needs_ipv6 = pytest.mark.skipif(not ipv6_available(), reason="no IPv6 on this machine")


def connect_v6(proxy_port, ip6, port):
    c = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    c.sendall(b"\x05\x01\x00")
    assert c.recv(2) == b"\x05\x00"
    c.sendall(b"\x05\x01\x00\x04" + socket.inet_pton(socket.AF_INET6, ip6) + struct.pack(">H", port))
    rep = b""
    while len(rep) < 10:
        chunk = c.recv(10 - len(rep))
        if not chunk:
            break
        rep += chunk
    return c, rep[1] if len(rep) > 1 else None


def test_ipv6_refused_and_counted_when_no_link_has_ipv6():
    a, b = Link("Ethernet"), Link("Hotspot")
    a.ip, b.ip = "127.0.0.1", "127.0.0.2"
    p = BalancingProxy([a, b])
    p.start(0)
    try:
        c, rep = connect_v6(p.port, "2001:db8::1", 443)
        assert rep == 8                       # "address type not supported"
        assert p.refused_ipv6 == 1
        c.close()
    finally:
        p.stop()


@needs_ipv6
def test_ipv6_connect_through_a_link_with_ipv6():
    srv = socket.socket(socket.AF_INET6)
    srv.bind(("::1", 0)); srv.listen(5)
    port = srv.getsockname()[1]
    seen = []
    threading.Thread(target=lambda: seen.append(srv.accept()[1][0]), daemon=True).start()
    a, b = Link("Ethernet"), Link("Hotspot")
    a.ip, b.ip = "127.0.0.1", "127.0.0.2"
    a.ip6 = "::1"                                    # only Ethernet has IPv6 here
    p = BalancingProxy([a, b])
    p.start(0)
    try:
        c, rep = connect_v6(p.port, "::1", port)
        assert rep == 0 and p.refused_ipv6 == 0
        c.close()
    finally:
        p.stop(); srv.close()


def test_controller_picks_up_ipv6_addresses(tmp_path):
    ad = Adapters(**{"Ethernet": "127.0.0.1", "Ethernet 2": "127.0.0.2"})
    v6 = {"Ethernet": "2a00:f503::5", "Ethernet 2": "2a00:808::3"}
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad, ipv6_lister=lambda: dict(v6))
    ctl.watch_every = 0.2
    ctl.update_settings({"eth_name": "Ethernet", "hot_name": "Ethernet 2", "port": 0, "qbt_auto": False})
    try:
        assert ctl.power_on()["ok"]
        assert (ctl.links[0].ip6, ctl.links[1].ip6) == ("2a00:f503::5", "2a00:808::3")
        v6["Ethernet 2"] = "2a00:808::99"           # phone got a new IPv6 address
        ctl.check_adapters()
        assert ctl.links[1].ip6 == "2a00:808::99"
    finally:
        ctl.shutdown()
