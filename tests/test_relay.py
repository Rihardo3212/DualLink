"""1.9: the faster relay - data sent early, flow control, data limit, clean shutdown."""
import socket
import struct
import threading
import time

from duallink.engine import BalancingProxy, Link
from test_adapters import wait_for


def proxy(**kw):
    a, b = Link("Ethernet"), Link("Hotspot")
    a.ip, b.ip = "127.0.0.1", "127.0.0.2"
    for k, v in kw.items():
        setattr(b, k, v)
    p = BalancingProxy([a, b])
    p.auto = False
    p.start(0)
    return p, a, b


def listener():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    return s


def request(port, target_port):
    return b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack(">H", target_port)


def test_data_sent_together_with_the_request_arrives():
    """Some apps send their first bytes (e.g. a TLS hello) without waiting for the proxy's reply."""
    p, a, b = proxy()
    srv = listener()
    got = []

    def echo():
        c, _ = srv.accept()
        data = c.recv(100)
        got.append(data)
        c.sendall(b"echo:" + data)
        time.sleep(0.5)
        c.close()
    threading.Thread(target=echo, daemon=True).start()
    try:
        c = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        c.sendall(b"\x05\x01\x00" + request(p.port, srv.getsockname()[1]) + b"HELLO")   # all at once
        reply = b""
        while len(reply) < 2 + 10 + 10:
            reply += c.recv(100)
        assert reply[:2] == b"\x05\x00" and reply[3] == 0
        assert reply[12:] == b"echo:HELLO" and got == [b"HELLO"]
        c.close()
    finally:
        p.stop(); srv.close()


def test_slow_app_does_not_make_dualink_buffer_everything():
    """Flow control: if the app reads slowly, DualLink stops reading from the internet instead of
    holding a whole fast download in memory."""
    p, a, b = proxy()
    srv = listener()
    sent = [0]

    def blast():
        c, _ = srv.accept()
        chunk = b"x" * 65536
        try:
            while sent[0] < 200 * 2**20:
                c.sendall(chunk)
                sent[0] += len(chunk)
        except OSError:
            pass
    threading.Thread(target=blast, daemon=True).start()
    try:
        c = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        c.sendall(b"\x05\x01\x00")
        c.recv(2)
        c.sendall(request(p.port, srv.getsockname()[1]))
        c.recv(10)
        time.sleep(1.5)                                   # the app reads nothing for 1.5 s
        in_flight = sent[0]
        assert in_flight < 32 * 2**20, f"{in_flight / 2**20:.0f} MiB piled up"   # bounded by socket buffers
        received = 0
        c.settimeout(5)
        while received < 20 * 2**20:                       # then reading resumes normally
            received += len(c.recv(1 << 20))
        assert received >= 20 * 2**20
        c.close()
    finally:
        p.stop(); srv.close()


def test_data_limit_cuts_the_connection_mid_transfer():
    p, a, b = proxy(quota_bytes=256 * 1024)
    a.enabled = False                                      # force the connection onto the hotspot
    srv = listener()

    def send():
        c, _ = srv.accept()
        try:
            c.sendall(b"x" * (8 * 2**20))
        except OSError:
            pass
    threading.Thread(target=send, daemon=True).start()
    try:
        c = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        c.sendall(b"\x05\x01\x00")
        c.recv(2)
        c.sendall(request(p.port, srv.getsockname()[1]))
        c.recv(10)
        got = 0
        while True:
            d = c.recv(65536)
            if not d:
                break
            got += len(d)
        assert got < 4 * 2**20 and b.over_quota()         # stopped well before 8 MiB
        c.close()
    finally:
        p.stop(); srv.close()


def test_counters_and_cleanup():
    p, a, b = proxy()
    srv = listener()

    def serve():
        c, _ = srv.accept()
        c.sendall(b"y" * 300_000)
        c.recv(100)
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    try:
        c = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        c.sendall(b"\x05\x01\x00")
        c.recv(2)
        c.sendall(request(p.port, srv.getsockname()[1]))
        c.recv(10)
        got = 0
        while got < 300_000:
            got += len(c.recv(65536))
        c.sendall(b"thanks")
        assert wait_for(lambda: a.active + b.active == 0)  # closed on both sides, nothing left over
        assert a.down + b.down == 300_000 and a.up + b.up == 6
        assert not p.conns
        c.close()
    finally:
        p.stop(); srv.close()


def test_hidden_window_gets_no_status_payload(tmp_path):
    from duallink.controller import Controller
    from test_adapters import Adapters
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=Adapters(**{"Ethernet": "127.0.0.1"}),
                     ipv6_lister=lambda: {})
    try:
        assert "links" in ctl.status(for_ui=True)
        ctl.ui_visible = False
        assert ctl.status(for_ui=True) == {"hidden": True}       # nothing to compute or send
        assert "links" in ctl.status()                            # tray tooltip still gets real numbers
    finally:
        ctl.shutdown()
