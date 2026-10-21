"""1.11: the native window (preview) - choosing it, switching, and falling back to the web window."""
import json
import os
import subprocess

from duallink import service
from duallink.api import EngineClient
from duallink.service import Engine, native_window_path, spawn
from test_adapters import wait_for
from test_v110 import FakeProc, FakeTray, make_ctl

NATIVE = os.path.join("C:\\", "Programs", "DualLink", "DualLinkWindow", "DualLinkWindow.exe")


def make(tmp_path, native=NATIVE, use_native=True, minimized=True):
    procs, trays = [], []

    def spawner(cmd):
        procs.append(FakeProc(cmd))
        return procs[-1]

    def tray_factory(**cb):
        trays.append(FakeTray(**cb))
        return trays[-1]
    ctl = make_ctl(tmp_path)
    ctl.update_settings({"native_window": use_native})
    eng = Engine(ctl=ctl, minimized=minimized, spawner=spawner, tray_factory=tray_factory,
                 info_path=tmp_path / "engine.json", tip_every=0.1, native=lambda: native)
    return eng, procs, trays


def test_native_window_opens_when_chosen_and_installed(tmp_path):
    eng, procs, trays = make(tmp_path)
    try:
        eng.start()
        assert eng.ctl.status()["native_available"] is True
        assert eng.open_window() == {"ok": True, "opened": True, "native": True}
        assert procs[0].cmd == [NATIVE]
    finally:
        eng.quit()


def test_web_window_when_native_not_installed(tmp_path):
    eng, procs, trays = make(tmp_path, native=None)
    try:
        eng.start()
        assert eng.ctl.status()["native_available"] is False
        assert eng.open_window()["native"] is False
        assert procs[0].cmd[-1] == "--window"
    finally:
        eng.quit()


def test_web_window_unless_chosen(tmp_path):
    eng, procs, trays = make(tmp_path, use_native=False)
    try:
        eng.start()
        eng.open_window()
        assert procs[0].cmd[-1] == "--window"
    finally:
        eng.quit()


def test_switching_the_setting_swaps_the_open_window(tmp_path):
    eng, procs, trays = make(tmp_path, use_native=False)
    try:
        eng.start()
        eng.open_window()
        client = EngineClient.connect(tmp_path / "engine.json", wait=2)
        res = client.call("save_settings", {"native_window": True})     # what either window sends
        assert res["settings"]["native_window"] is True
        assert wait_for(lambda: len(procs) == 2)
        assert procs[0].terminated and procs[1].cmd == [NATIVE]
        client.call("save_settings", {"native_window": False})            # and back again
        assert wait_for(lambda: len(procs) == 3)
        assert procs[1].terminated and procs[2].cmd[-1] == "--window"
        assert not eng.native_failed                                        # closed by us, not a crash
    finally:
        eng.quit()


def test_other_settings_dont_reopen_the_window(tmp_path):
    eng, procs, trays = make(tmp_path)
    try:
        eng.start()
        eng.open_window()
        eng.save_settings({"quota_mb": 100})
        import time
        time.sleep(0.7)
        assert len(procs) == 1 and not procs[0].terminated
    finally:
        eng.quit()


def test_native_window_crash_falls_back_to_web_window(tmp_path):
    eng, procs, trays = make(tmp_path)
    try:
        eng.start()
        eng.open_window()
        procs[0].exit(3)                                           # crashed while starting
        assert wait_for(lambda: len(procs) == 2)
        assert procs[1].cmd[-1] == "--window" and eng.native_failed
        assert "The new window couldn't start" in trays[0].notes
        procs[1].exit(0)                                           # closed by the user
        assert wait_for(lambda: not eng.window_open())
        eng.open_window()
        assert procs[2].cmd[-1] == "--window"                      # stays on the web window for now
        eng.save_settings({"native_window": False})
        eng.save_settings({"native_window": True})                  # choosing it again tries again
        assert not eng.native_failed
    finally:
        eng.quit()


def test_closing_the_native_window_normally_is_not_a_crash(tmp_path):
    eng, procs, trays = make(tmp_path)
    try:
        eng.start()
        eng.open_window()
        procs[0].exit(0)
        assert wait_for(lambda: not eng.window_open())
        import time
        time.sleep(0.3)
        assert len(procs) == 1 and not eng.native_failed
    finally:
        eng.quit()


def test_native_window_that_cannot_start_at_all(tmp_path):
    calls = []

    def spawner(cmd):
        calls.append(cmd)
        if cmd == [NATIVE]:
            raise OSError("not a valid application")
        return FakeProc(cmd)
    ctl = make_ctl(tmp_path)
    ctl.update_settings({"native_window": True})
    eng = Engine(ctl=ctl, minimized=True, spawner=spawner, tray_factory=lambda **cb: FakeTray(**cb),
                 info_path=tmp_path / "engine.json", native=lambda: NATIVE)
    try:
        eng.start()
        assert eng.open_window()["opened"]
        assert calls == [[NATIVE], calls[1]] and calls[1][-1] == "--window" and eng.native_failed
    finally:
        eng.quit()


def test_native_window_gets_a_clean_environment(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.append((cmd, kw)) or "proc")
    spawn([NATIVE])
    spawn(service.window_command())
    (native_cmd, native_kw), (web_cmd, web_kw) = seen
    assert native_kw["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"   # its own app, not sharing our files
    assert web_kw["env"] is None


def test_native_window_path(tmp_path, monkeypatch):
    exe = tmp_path / "DualLinkWindow.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("DUALLINK_NATIVE_WINDOW", str(exe))
    assert native_window_path() == str(exe)
    monkeypatch.setenv("DUALLINK_NATIVE_WINDOW", str(tmp_path / "missing.exe"))
    assert native_window_path() is None
    monkeypatch.setenv("DUALLINK_NATIVE_WINDOW", "")
    assert native_window_path() is None                       # running from source: no native window


def test_setting_is_public_and_saved(tmp_path):
    ctl = make_ctl(tmp_path)
    try:
        assert ctl.public_settings()["native_window"] is False
        ctl.update_settings({"native_window": True})
        assert json.loads(ctl.cfg_path.read_text())["native_window"] is True
    finally:
        ctl.shutdown()


def test_web_window_has_the_switch():
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "duallink", "ui", "index.html"),
              encoding="utf-8") as f:
        page = f.read()
    assert 'id="s_native"' in page and "native_available" in page and "native_window" in page
