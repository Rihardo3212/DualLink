"""1.10: a small background engine (proxy + tray) and a window that only exists while it's open."""
import glob
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest

from duallink import instance
from duallink.api import Api, ControlServer, EngineClient, EngineError
from duallink.controller import Controller
from duallink.service import Engine
from test_adapters import Adapters, wait_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_EXE = os.path.join("C:\\", "Programs", "DualLink", "DualLinkWindow", "DualLinkWindow.exe")
needs_windows = pytest.mark.skipif(os.name != "nt", reason="Windows only")


def make_ctl(tmp_path):
    ad = Adapters(**{"Ethernet": "127.0.0.1", "Ethernet 2": "127.0.0.2"})
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad, ipv6_lister=lambda: {})
    ctl.update_settings({"eth_name": "Ethernet", "hot_name": "Ethernet 2", "port": 0, "qbt_auto": False,
                         "auto_on_hotspot": False})
    return ctl


def post(port, path="/api/echo", token=None, headers=None, body=b'{"args": [1, "two"]}', method="POST"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Content-Type": "application/json"}
    if token is not None:
        h["X-DualLink-Token"] = token
    h.update(headers or {})
    c.request(method, path, body if method == "POST" else None, h)
    r = c.getresponse()
    data = json.loads(r.read() or b"{}")
    c.close()
    return r.status, data


# ---- control API ------------------------------------------------------------------------ #
def test_control_api_only_answers_dualink():
    srv = ControlServer({"echo": lambda *a: list(a), "boom": lambda: 1 / 0}).start()
    try:
        tok, port = srv.token, srv.port
        assert post(port, token=tok) == (200, {"ok": True, "result": [1, "two"]})
        assert post(port)[0] == 403                                     # no token
        assert post(port, token=tok[:-1] + "x")[0] == 403               # wrong token
        assert post(port, token="é" * 4)[0] == 403                      # odd characters don't crash it
        assert post(port, token=tok, headers={"Origin": "https://example.com"})[0] == 403   # a web page
        assert post(port, token=tok, headers={"Host": f"evil.example:{port}"})[0] == 403   # DNS rebinding
        assert post(port, token=tok, headers={"Host": f"localhost:{port}"})[0] == 200
        assert post(port, "/api/nope", token=tok)[0] == 404
        assert post(port, "/api/__init__", token=tok)[0] == 404
        assert post(port, token=tok, method="GET")[0] == 405
        assert post(port, token=tok, body=b"{not json")[0] == 400
        assert post(port, token=tok, body=b'{"args": 5}')[0] == 400
        code, body = post(port, "/api/boom", token=tok, body=b'{"args": []}')
        assert code == 500 and "ZeroDivisionError" in body["error"]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.putrequest("POST", "/api/echo")
        c.putheader("Content-Length", str(5 << 20))
        c.putheader("X-DualLink-Token", tok)
        c.endheaders()
        assert c.getresponse().status == 413                            # not read into memory
        c.close()
    finally:
        srv.stop()


def test_control_api_listens_on_this_pc_only():
    srv = ControlServer({}).start()
    try:
        assert srv.httpd.server_address[0] == "127.0.0.1"
        assert len(srv.token) >= 40
        assert ControlServer({}).token != srv.token
    finally:
        srv.stop()


def test_engine_answers_the_window(tmp_path):
    ctl = make_ctl(tmp_path)
    api = Api(ctl)
    handlers = {n: getattr(api, n) for n in Api.PAGE}
    handlers["ping"] = lambda: {"pid": os.getpid()}
    srv = ControlServer(handlers).start()
    info = srv.publish(tmp_path / "engine.json")
    try:
        assert json.loads((tmp_path / "engine.json").read_text())["token"] == info["token"]
        client = EngineClient.connect(tmp_path / "engine.json")
        assert client is not None and client.pid == os.getpid()
        assert client.call("status")["running"] is False
        assert client.call("get_init")["settings"]["eth_name"] == "Ethernet"
        res = client.call("save_settings", {"quota_mb": 500})
        assert res["error"] is None and ctl.cfg["quota_mb"] == 500
        assert client.call("save_settings", {"extra": ["Ethernet"]})["error"]      # validation errors come back
        assert client.call("power", True)["ok"] and ctl.running
        assert client.call("status")["running"] is True
        assert client.call("power", False)["ok"] is not None and not ctl.running
        assert client.call("open_url", "https://evil.example/") is False

        results, errors = [], []

        def poll():
            try:
                for _ in range(10):
                    results.append(client.call("status")["version"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=poll) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors and len(results) == 80                  # many calls at once, kept-alive connections

        with pytest.raises(EngineError, match="Unknown request"):
            client.call("nope")
    finally:
        srv.unpublish()
        srv.stop()
        ctl.shutdown()
    assert not (tmp_path / "engine.json").exists()
    t0 = time.monotonic()
    with pytest.raises(EngineError, match="isn't responding"):
        client.call("status")                                    # engine gone: a clear error, quickly
    assert time.monotonic() - t0 < 5


def test_no_engine_found(tmp_path):
    assert EngineClient.connect(tmp_path / "engine.json") is None
    srv = ControlServer({"ping": lambda: {}}).start()
    srv.publish(tmp_path / "engine.json")
    srv.stop()                                                   # crashed engine: the file is stale
    t0 = time.monotonic()
    assert EngineClient.connect(tmp_path / "engine.json", wait=0.6) is None
    assert time.monotonic() - t0 < 3


def test_window_asks_only_for_what_the_engine_answers():
    """Every request in the C# window's code exists in the engine (and in the window's demo data)."""
    code = ""
    for path in glob.glob(os.path.join(ROOT, "native", "DualLinkWindow", "*.cs")):
        with open(path, encoding="utf-8") as f:
            code += f.read()
    asked = set(re.findall(r'CallAsync\("(\w+)"', code)) | {"get_init", "status"}
    answered = set(Api.PAGE) | {"ping", "window_closed", "open_window", "quit"}
    assert asked <= answered, asked - answered
    demo = set(re.findall(r'case "(\w+)":', code))
    assert asked <= demo, asked - demo


# ---- engine ------------------------------------------------------------------------------- #
class FakeProc:
    def __init__(self, cmd):
        self.cmd, self.code, self.terminated = cmd, None, False
        self._done = threading.Event()

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return self.code

    def terminate(self):
        self.terminated = True
        self.exit(1)

    def exit(self, code=0):
        self.code = code
        self._done.set()


class FakeTray:
    def __init__(self, **callbacks):
        self.cb, self.notes, self.tips, self.state, self.stopped = callbacks, [], [], None, False

    def notify(self, title, msg):
        self.notes.append(title)

    def set_state(self, on, tip):
        self.state = (on, tip)

    def set_tip(self, tip):
        self.tips.append(tip)

    def stop(self):
        self.stopped = True


def make_engine(tmp_path, minimized=False, tray=True):
    procs, trays = [], []

    def spawner(cmd):
        procs.append(FakeProc(cmd))
        return procs[-1]

    def tray_factory(**cb):
        if not tray:
            return None
        trays.append(FakeTray(**cb))
        return trays[-1]
    eng = Engine(ctl=make_ctl(tmp_path), minimized=minimized, spawner=spawner, tray_factory=tray_factory,
                 info_path=tmp_path / "engine.json", tip_every=0.1, window=lambda: WINDOW_EXE)
    return eng, procs, trays


def test_engine_opens_one_window_and_reopens_after_close(tmp_path):
    eng, procs, trays = make_engine(tmp_path)
    try:
        eng.start()
        assert len(procs) == 1 and procs[0].cmd == WINDOW_EXE
        assert eng.ping()["window"] is True
        eng.open_window()                                        # already open: brought to the front instead
        trays[0].cb["on_open"]()
        assert len(procs) == 1
        procs[0].exit()                                          # user closed the window
        assert wait_for(lambda: not eng.window_open())
        assert eng.open_window()["opened"] and len(procs) == 2
    finally:
        eng.quit()


def test_start_with_windows_has_no_window(tmp_path):
    eng, procs, trays = make_engine(tmp_path, minimized=True)
    try:
        eng.start()
        assert procs == [] and eng.ping()["window"] is False and trays[0].state == (False, "DualLink - off")
    finally:
        eng.quit()


def test_closing_the_window_keeps_dualink_in_the_tray(tmp_path):
    eng, procs, trays = make_engine(tmp_path)
    try:
        eng.start()
        assert eng.window_closed() == {"quitting": False}
        assert eng.window_closed() == {"quitting": False}
        assert trays[0].notes.count("DualLink is still running") == 1        # the hint only once
        assert json.loads(eng.ctl.cfg_path.read_text())["tray_hint_shown"] is True
        assert not eng.stopped.is_set()
    finally:
        eng.quit()


@pytest.mark.parametrize("why", ["setting", "no tray"])
def test_closing_the_window_quits_when_there_is_no_tray(tmp_path, why):
    eng, procs, trays = make_engine(tmp_path, tray=(why == "setting"))
    eng.start()
    if why == "setting":
        eng.ctl.update_settings({"tray": False})
    assert eng.window_closed() == {"quitting": True}
    assert eng.stopped.wait(5)
    assert not (tmp_path / "engine.json").exists()


def test_quit_through_the_api(tmp_path):
    eng, procs, trays = make_engine(tmp_path)
    eng.start()
    client = EngineClient.connect(tmp_path / "engine.json", wait=2)
    assert client.call("ping")["pid"] == os.getpid()
    assert client.call("power", True)["ok"] and eng.ctl.running
    assert client.call("quit") == {"quitting": True}
    assert eng.stopped.wait(5)
    assert not eng.ctl.running and not eng.ctl.proxy_up()             # switched off, proxy closed
    assert procs[0].terminated and trays[0].stopped                    # window and tray icon gone
    assert not (tmp_path / "engine.json").exists()
    with pytest.raises(EngineError):
        client.call("ping", timeout=2)


def test_tray_menu_and_tooltip(tmp_path):
    eng, procs, trays = make_engine(tmp_path, minimized=True)
    try:
        eng.start()
        tray = trays[0]
        tray.cb["on_toggle"]()                                    # "Turn on"
        assert eng.ctl.running and tray.state == (True, "DualLink - on") and tray.cb["is_on"]()
        assert wait_for(lambda: any("Ethernet" in t and "Hotspot" in t for t in tray.tips))
        assert all(len(t) <= 127 for t in tray.tips)
        tray.cb["on_toggle"]()                                    # "Turn off"
        assert not eng.ctl.running and tray.state == (False, "DualLink - off")
        eng.ctl.power_on = lambda: {"ok": False, "message": "No hotspot"}
        tray.cb["on_toggle"]()                                    # can't start: say so and open the window
        assert "DualLink couldn't start" in tray.notes and len(procs) == 1
    finally:
        eng.quit()


def test_updated_engine_says_so(tmp_path):
    eng, procs, trays = make_engine(tmp_path)
    eng.updated = True
    try:
        eng.start()
        assert "DualLink updated" in trays[0].notes and len(procs) == 1
    finally:
        eng.quit()


def test_engine_stays_light():
    """The engine never loads the window's libraries, which is where most of the memory went."""
    code = ("import sys; import duallink.service, duallink.tray, duallink.api; "
            "print(','.join(m for m in ('webview', 'pystray', 'PIL', 'clr') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_on_exit_notices_the_engine_ending():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    threading.Thread(target=p.wait, daemon=True).start()
    ended = threading.Event()
    instance.on_exit(p.pid, ended.set, poll=0.1)
    assert ended.wait(10)


# ---- Windows only (GitHub's Windows machine) ------------------------------------------------- #
@needs_windows
def test_native_tray_icon():
    from duallink.tray import NativeTray
    calls = []
    t = NativeTray(lambda: calls.append("open"), lambda: calls.append("toggle"), lambda: calls.append("quit"),
                   is_on=lambda: False).start()
    try:
        assert t.hwnd
        t.set_state(True, "DualLink - on")
        t.set_tip("DualLink - 1.0 MiB/s  (Ethernet 0.5 MiB/s, Hotspot 0.5 MiB/s)")
        t.notify("DualLink test", "Hello from the test suite.")
        from duallink.tray import WM_TRAY, WM_LBUTTONUP
        t._u.PostMessageW(t.hwnd, WM_TRAY, 1, WM_LBUTTONUP)        # a left-click opens the window
        assert wait_for(lambda: calls == ["open"])
    finally:
        t.stop()
    assert t.hwnd is None


@needs_windows
def test_named_mutex_and_events():
    name = f"Local\\DualLinkTest{os.getpid()}"
    assert not instance.held(name)
    assert instance.acquire(name=name) and instance.held(name)
    instance.release(name)
    assert not instance.held(name)
    hits = []
    instance.listen_for_show(lambda: hits.append(1), name=name + "Event")
    assert instance.signal_show(name + "Event")
    assert wait_for(lambda: hits == [1])
