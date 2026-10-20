"""1.10: a small background engine (proxy + tray) and a window that only exists while it's open."""
import http.client
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest

from duallink import instance
from duallink.api import Api, ControlServer, EngineClient, EngineError, RemoteApi
from duallink.controller import Controller
from duallink.service import Engine, window_command
from test_adapters import Adapters, wait_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def test_window_talks_to_engine_through_remote_api(tmp_path):
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
        remote = RemoteApi(client)
        for name in Api.PAGE:                                   # same methods and parameters as Api
            assert inspect.signature(getattr(remote, name)) == inspect.signature(getattr(api, name)), name
            assert inspect.ismethod(getattr(remote, name))      # what pywebview looks for
            assert inspect.getfullargspec(getattr(remote, name)).args == inspect.getfullargspec(getattr(api, name)).args
        assert [n for n in dir(remote) if not n.startswith("_")] == sorted(Api.PAGE)   # nothing else exposed
        assert remote.status()["running"] is False
        assert remote.get_init()["settings"]["eth_name"] == "Ethernet"
        res = remote.save_settings({"quota_mb": 500})
        assert res["error"] is None and ctl.cfg["quota_mb"] == 500
        assert remote.save_settings({"extra": ["Ethernet"]})["error"]      # validation errors come back
        assert remote.power(True)["ok"] and ctl.running
        assert remote.status()["running"] is True
        assert remote.power(False)["ok"] is not None and not ctl.running
        assert remote.open_url("https://evil.example/") is False
        assert RemoteApi(client, visible=lambda: False).status() == {"hidden": True}

        results, errors = [], []

        def poll():
            try:
                for _ in range(10):
                    results.append(remote.status()["version"])
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
        remote.status()                                          # engine gone: a clear error, quickly
    assert time.monotonic() - t0 < 5


def test_no_engine_found(tmp_path):
    assert EngineClient.connect(tmp_path / "engine.json") is None
    srv = ControlServer({"ping": lambda: {}}).start()
    srv.publish(tmp_path / "engine.json")
    srv.stop()                                                   # crashed engine: the file is stale
    t0 = time.monotonic()
    assert EngineClient.connect(tmp_path / "engine.json", wait=0.6) is None
    assert time.monotonic() - t0 < 3


def test_page_uses_only_api_methods():
    with open(os.path.join(ROOT, "duallink", "ui", "index.html"), encoding="utf-8") as f:
        used = set(re.findall(r"\bapi\.(\w+)\(", f.read()))
    assert used and used <= set(Api.PAGE), used - set(Api.PAGE)


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
                 info_path=tmp_path / "engine.json", tip_every=0.1)
    return eng, procs, trays


def test_engine_opens_one_window_and_reopens_after_close(tmp_path):
    eng, procs, trays = make_engine(tmp_path)
    try:
        eng.start()
        assert len(procs) == 1 and procs[0].cmd[-1] == "--window"
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
            "print(','.join(m for m in ('webview', 'pystray', 'PIL', 'clr', 'duallink.window') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_window_process_skips_engine_code():
    code = ("import sys; import duallink.window; "
            "print(','.join(m for m in ('duallink.controller', 'duallink.engine', 'duallink.qbt') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_window_command():
    cmd = window_command()
    assert cmd[0] == sys.executable and cmd[-1] == "--window" and cmd[1].endswith("main.py")
    sys.frozen = True                                            # as in DualLink.exe
    try:
        assert window_command() == [sys.executable, "--window"]
    finally:
        del sys.frozen


def test_on_exit_notices_the_engine_ending():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    threading.Thread(target=p.wait, daemon=True).start()
    ended = threading.Event()
    instance.on_exit(p.pid, ended.set, poll=0.1)
    assert ended.wait(10)


# ---- the window process ---------------------------------------------------------------------- #
class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for h in self.handlers:
            h()


def fake_webview(script):
    """A stand-in for pywebview: start() runs `script(window)` instead of showing a real window."""
    import types
    mod = types.ModuleType("webview")

    class Window:
        def __init__(self, url, js_api, kw):
            self.url, self.js_api, self.kw, self.destroyed = url, js_api, kw, False
            self.events = types.SimpleNamespace(**{n: _Event() for n in
                                                   ("closed", "minimized", "restored", "shown", "maximized")})

        def show(self):
            pass

        def restore(self):
            pass

        def destroy(self):
            self.destroyed = True

    def create_window(title, url, js_api=None, **kw):
        mod.window = Window(url, js_api, kw)
        return mod.window

    mod.create_window = create_window
    mod.start = lambda: script(mod.window)
    return mod


def test_window_process_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from duallink import window
    eng, procs, trays = make_engine(tmp_path, minimized=True)
    eng._info_path = None                                       # engine.json where the window looks for it
    seen = {}

    def script(w):
        seen["version"] = w.js_api.status()["version"]          # the page's first poll reaches the engine
        w.events.minimized.fire()
        seen["minimised"] = w.js_api.status()
        w.events.restored.fire()
        seen["init"] = sorted(w.js_api.get_init())
        w.events.closed.fire()                                  # user closes the window
    fake = fake_webview(script)
    old = sys.modules.get("webview")
    sys.modules["webview"] = fake
    try:
        eng.start()
        assert (tmp_path / "DualLink" / "engine.json").exists()
        assert window.run() == 0
        assert seen == {"version": eng.ping()["version"], "minimised": {"hidden": True},
                        "init": ["adapters", "settings"]}
        assert os.path.exists(fake.window.url) and fake.window.url.endswith("index.html")
        assert fake.window.kw["min_size"] == (720, 560)
        assert "DualLink is still running" in trays[0].notes      # the engine heard about the close
        assert not eng.stopped.is_set()
    finally:
        instance.release(instance.WINDOW_MUTEX)
        if old is None:
            sys.modules.pop("webview", None)
        else:
            sys.modules["webview"] = old
        eng.quit()


def test_window_without_engine_starts_the_engine(tmp_path, monkeypatch):
    import types
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from duallink import window
    started = []
    monkeypatch.setattr(window, "subprocess", types.SimpleNamespace(
        Popen=lambda cmd, **kw: started.append((cmd, kw))))
    t0 = time.monotonic()
    assert window.run() == 0                                   # nothing to show yet: the engine opens the window
    assert time.monotonic() - t0 < 10
    assert len(started) == 1
    cmd, kw = started[0]
    assert cmd == window.engine_command() and "--window" not in cmd
    assert kw["env"].get("PYINSTALLER_RESET_ENVIRONMENT") == "1"   # it must outlive this window process
    assert not instance.held(instance.WINDOW_MUTEX)


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
