"""SOCKS5 UDP ASSOCIATE: how qBittorrent sends DHT and UDP-tracker traffic through the proxy."""
import socket
import struct
import threading
import time

import pytest

from duallink.engine import BalancingProxy, Link

ETH, HOT = "127.0.0.1", "127.0.0.2"


class UdpEcho:
    """Replies 'echo:<data>' and records which local IP each packet came from."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sources = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                return
            self.sources.append(addr[0])
            self.sock.sendto(b"echo:" + data, addr)

    def close(self):
        self.sock.close()


def associate(proxy_port):
    """Opens a UDP association like libtorrent does. Returns (control_tcp, udp_socket, relay_addr)."""
    c = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    c.sendall(b"\x05\x01\x00")
    assert c.recv(2) == b"\x05\x00"
    c.sendall(b"\x05\x03\x00\x01" + socket.inet_aton("0.0.0.0") + struct.pack(">H", 0))
    rep = b""
    while len(rep) < 10:
        rep += c.recv(10 - len(rep))
    assert rep[1] == 0, rep
    relay = (socket.inet_ntoa(rep[4:8]), struct.unpack(">H", rep[8:10])[0])
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.bind(("127.0.0.1", 0))
    u.settimeout(2)
    return c, u, relay


def udp_packet(host, port, payload, by_name=False):
    if by_name:
        addr = b"\x03" + bytes([len(host)]) + host.encode()
    else:
        addr = b"\x01" + socket.inet_aton(host)
    return b"\x00\x00\x00" + addr + struct.pack(">H", port) + payload


@pytest.fixture
def setup():
    a, b = Link("Ethernet"), Link("Hotspot")
    a.ip, b.ip = ETH, HOT
    p = BalancingProxy([a, b])
    echo = UdpEcho()
    p.start(0)
    yield p, a, b, echo
    p.stop()
    echo.close()


def test_udp_round_trip_goes_over_ethernet(setup):
    p, eth, hot, echo = setup
    c, u, relay = associate(p.port)
    u.sendto(udp_packet("127.0.0.1", echo.port, b"ping"), relay)
    data, src = u.recvfrom(65535)
    assert src == relay
    assert data[:4] == b"\x00\x00\x00\x01"                       # SOCKS5 UDP header
    assert socket.inet_ntoa(data[4:8]) == "127.0.0.1"
    assert struct.unpack(">H", data[8:10])[0] == echo.port       # tells qBittorrent who replied
    assert data[10:] == b"echo:ping"
    assert echo.sources == [ETH]                                  # DHT stays off the hotspot
    assert eth.udp_bytes > 0 and hot.udp_bytes == 0
    assert eth.down == 0                                          # not mixed into speed measurements
    assert p.udp_assocs == 1
    c.close()


def test_udp_hostname_destination(setup):
    p, eth, hot, echo = setup
    c, u, relay = associate(p.port)
    u.sendto(udp_packet("localhost", echo.port, b"tracker", by_name=True), relay)
    data, _ = u.recvfrom(65535)
    assert data[10:] == b"echo:tracker"
    c.close()


def test_udp_uses_hotspot_when_ethernet_unavailable(setup):
    p, eth, hot, echo = setup
    eth.enabled = False
    c, u, relay = associate(p.port)
    u.sendto(udp_packet("127.0.0.1", echo.port, b"x"), relay)
    u.recvfrom(65535)
    assert echo.sources == [HOT]
    c.close()


def test_udp_association_ends_with_tcp_connection(setup):
    p, eth, hot, echo = setup
    c, u, relay = associate(p.port)
    u.sendto(udp_packet("127.0.0.1", echo.port, b"1"), relay)
    u.recvfrom(65535)
    c.close()
    time.sleep(0.3)
    assert p.udp_assocs == 0
    u.sendto(udp_packet("127.0.0.1", echo.port, b"2"), relay)
    with pytest.raises((socket.timeout, ConnectionResetError)):
        u.recvfrom(65535)


def test_fragments_and_strangers_are_ignored(setup):
    p, eth, hot, echo = setup
    c, u, relay = associate(p.port)
    u.sendto(udp_packet("127.0.0.1", echo.port, b"ok"), relay)
    u.recvfrom(65535)
    frag = b"\x00\x00\x01" + udp_packet("127.0.0.1", echo.port, b"frag")[3:]
    u.sendto(frag, relay)
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    other.sendto(udp_packet("127.0.0.1", echo.port, b"stranger"), relay)   # not the associated client
    time.sleep(0.3)
    assert echo.sources == [ETH]
    other.close()
    c.close()


def test_udp_counts_toward_hotspot_data_limit():
    hot = Link("Hotspot")
    hot.quota_bytes = 1000
    hot.udp_bytes = 1000
    assert hot.over_quota()
