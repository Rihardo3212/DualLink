"""Traffic check: parsing Windows tool output and drawing conclusions (simulates the FDM/MediaFire case)."""
from duallink import diag
from duallink.controller import Controller
from test_adapters import Adapters

IPCONFIG = """
Windows IP Configuration


Ethernet adapter Ethernet:

   Connection-specific DNS Suffix  . : home
   IPv6 Address. . . . . . . . . . . : 2a00:f503:280:c402:2875:e137:35b7:cd45
   Temporary IPv6 Address. . . . . . : 2a00:f503:280:c402:1111:2222:3333:4444
   Link-local IPv6 Address . . . . . : fe80::1234:5678%12
   IPv4 Address. . . . . . . . . . . : 192.168.8.5
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : fe80::1%12
                                       192.168.8.1

Ethernet adapter Ethernet 2:

   Connection-specific DNS Suffix  . :
   IPv6 Address. . . . . . . . . . . : 2a00:808:114:dc57:5fba:7ea7:669a:4f3c
   IPv4 Address. . . . . . . . . . . : 172.20.10.3
"""

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1234
  TCP    127.0.0.1:1080         0.0.0.0:0              LISTENING       5000
  TCP    127.0.0.1:52001        127.0.0.1:1080         ESTABLISHED     7000
  TCP    127.0.0.1:52002        127.0.0.1:1080         ESTABLISHED     7000
  TCP    127.0.0.1:1080         127.0.0.1:52001        ESTABLISHED     5000
  TCP    192.168.8.5:52100      104.16.1.1:443         ESTABLISHED     5000
  TCP    172.20.10.3:52200      199.91.152.10:443      ESTABLISHED     7000
  TCP    172.20.10.3:52201      199.91.152.10:443      ESTABLISHED     7000
  TCP    192.168.8.5:52300      142.250.1.1:443        ESTABLISHED     8000
  TCP    [::]:135               [::]:0                 LISTENING       1234
  TCP    [2a00:808:114:dc57:5fba:7ea7:669a:4f3c]:52400  [2606:4700::1]:443  ESTABLISHED  7000
"""

TASKLIST = '''"System Idle Process","0","Services","0","8 K"
"fdm.exe","7000","Console","1","120,000 K"
"DualLink.exe","5000","Console","1","50,000 K"
"chrome.exe","8000","Console","1","300,000 K"
'''

SUBIF = """
   MTU  MediaSenseState   Bytes In  Bytes Out  Interface
------  ---------------  ---------  ---------  -------------
4294967295                1          0       5000  Loopback Pseudo-Interface 1
  1500                1  1000000000   20000000  Ethernet
  1500                1  500000000   10000000  Ethernet 2
"""


def test_parse_ipconfig():
    a = diag.parse_ipconfig(IPCONFIG)
    assert a["Ethernet"]["v4"] == ["192.168.8.5"]
    assert "2a00:f503:280:c402:2875:e137:35b7:cd45" in a["Ethernet"]["v6"]
    assert all(not ip.startswith("fe80") for ip in a["Ethernet"]["v6"])
    assert a["Ethernet 2"] == {"v4": ["172.20.10.3"], "v6": ["2a00:808:114:dc57:5fba:7ea7:669a:4f3c"]}


def test_parse_netstat_tasklist_subinterfaces():
    rows = diag.parse_netstat(NETSTAT)
    assert ("127.0.0.1", "52001", "127.0.0.1", "1080", 7000) in rows
    assert not any(r[3] == "0" for r in rows)                              # listening sockets skipped
    assert ("2a00:808:114:dc57:5fba:7ea7:669a:4f3c", "52400", "2606:4700::1", "443", 7000) in rows
    assert diag.parse_tasklist(TASKLIST)[7000] == "fdm.exe"
    assert diag.parse_subinterfaces(SUBIF) == {"Loopback Pseudo-Interface 1": 0,
                                               "Ethernet": 1000000000, "Ethernet 2": 500000000}


def test_summarize_finds_programs_going_around_dualink():
    s = diag.summarize(diag.parse_netstat(NETSTAT), diag.parse_tasklist(TASKLIST),
                       diag.parse_ipconfig(IPCONFIG), 1080, own_pid=5000)
    assert s["through"] == {"fdm.exe": 2}
    assert s["direct"]["Ethernet 2"] == {"fdm.exe": 3}                    # 2 over IPv4 + 1 over IPv6
    assert s["direct"]["Ethernet"] == {"chrome.exe": 1}                   # DualLink's own sockets excluded


def make_ctl(tmp_path, hot_windows_rate, hot_dualink_rate):
    ad = Adapters(**{"Ethernet": "127.0.0.2", "Ethernet 2": "127.0.0.3"})
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad)
    ctl.update_settings({"eth_name": "Ethernet", "hot_name": "Ethernet 2", "port": 0, "qbt_auto": False})
    state = {"t": 0}

    def rx():
        state["t"] += 1
        if state["t"] == 2:   # second snapshot, after the wait: simulate the transfer during the window
            ctl.links[0].down += int(2.0 * 2**20 * 0.2)
            ctl.links[1].down += int(hot_dualink_rate * 0.2)
        k = state["t"] - 1
        return {"Ethernet": 10**9 + k * int(2.1 * 2**20 * 0.2), "Ethernet 2": 5 * 10**8 + k * int(hot_windows_rate * 0.2)}
    ctl.diag_rx = rx
    return ctl


def test_check_explains_the_fdm_mismatch(tmp_path):
    ctl = make_ctl(tmp_path, hot_windows_rate=7 * 2**20, hot_dualink_rate=0.17 * 2**20)
    ctl.diag_conns = lambda port, pid: {"through": {"fdm.exe": 8}, "direct": {"Ethernet 2": {"fdm.exe": 6}}}
    assert ctl.power_on()["ok"]
    r = ctl.traffic_check(seconds=0.2)
    ctl.shutdown()
    hot = r["links"][1]
    assert hot["other"] > 6 * 2**20                                         # ~6.8 MiB/s around DualLink
    texts = " ".join(n["text"] for n in r["notes"])
    assert "isn't going through DualLink" in texts and "fdm.exe" in texts
    assert "also connects directly 6 times" in texts
    assert not any(n["kind"] == "good" for n in r["notes"])


def test_check_all_good(tmp_path):
    ctl = make_ctl(tmp_path, hot_windows_rate=3 * 2**20, hot_dualink_rate=3 * 2**20)
    ctl.diag_conns = lambda port, pid: {"through": {"fdm.exe": 8}, "direct": {}}
    assert ctl.power_on()["ok"]
    r = ctl.traffic_check(seconds=0.2)
    ctl.shutdown()
    assert [n["kind"] for n in r["notes"]] == ["good"]


def test_check_reports_refused_ipv6_and_missing_proxy_users(tmp_path):
    ctl = make_ctl(tmp_path, hot_windows_rate=0, hot_dualink_rate=0)
    ctl.diag_conns = lambda port, pid: {"through": {}, "direct": {}}
    assert ctl.power_on()["ok"]
    ctl.proxy.refused_ipv6 = 4
    r = ctl.traffic_check(seconds=0.2)
    ctl.shutdown()
    texts = " ".join(n["text"] for n in r["notes"])
    assert "No program is connected through DualLink" in texts
    assert "couldn't make 4 IPv6 connections" in texts
