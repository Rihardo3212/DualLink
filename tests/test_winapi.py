"""Windows API vs. the command-line tools it replaces. Runs on GitHub's Windows machine (skipped elsewhere)."""
import os
import socket

import pytest

needs_windows = pytest.mark.skipif(os.name != "nt", reason="Windows only")


@needs_windows
def test_adapters_match_ipconfig():
    from duallink import diag, winapi
    api = {ip for a in winapi.adapters() for ip in a["v4"] if not ip.startswith(("127.", "169.254."))}
    cli = {ip for a in diag.parse_ipconfig(diag._run(["ipconfig"])).values() for ip in a["v4"]
           if not ip.startswith(("127.", "169.254."))}
    assert api == cli and api                     # same IPv4 addresses, and at least one


@needs_windows
def test_adapter_names_match_ipconfig():
    from duallink import diag, winapi
    api = {a["name"] for a in winapi.adapters() if a["v4"]}
    cli = {n for n, a in diag.parse_ipconfig(diag._run(["ipconfig"])).items() if a["v4"]}
    assert api == cli


@needs_windows
def test_tcp_table_sees_our_own_connection():
    from duallink import winapi
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.create_connection(srv.getsockname())
    s, _ = srv.accept()
    try:
        mine = (("127.0.0.1", str(c.getsockname()[1]), "127.0.0.1", str(srv.getsockname()[1]), os.getpid()))
        assert mine in winapi.tcp_connections()
        assert not any(r[3] == "0" for r in winapi.tcp_connections())   # listening sockets left out
    finally:
        c.close(); s.close(); srv.close()


@needs_windows
def test_process_name_of_ourselves():
    from duallink import winapi
    assert winapi.process_names([os.getpid()])[os.getpid()].lower().startswith("python")


@needs_windows
def test_live_app_status_uses_the_api():
    from duallink import diag
    from duallink.engine import BalancingProxy, Link
    a = Link("Ethernet")
    a.ip = "127.0.0.1"
    p = BalancingProxy([a])
    p.start(0)
    c = socket.create_connection(("127.0.0.1", p.port))
    try:
        counts = diag.proxy_clients(p.port, own_pid=-1)
        assert any(name.lower().startswith("python") for name in counts)
    finally:
        c.close(); p.stop()
