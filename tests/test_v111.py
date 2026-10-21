"""1.11/1.12: the native window (DualLinkWindow.exe) - opening it, and saying so when it can't open."""
import json
import os
import subprocess
import sys
import time

from duallink import service
from duallink.service import Engine, spawn, window_path
from test_adapters import wait_for
from test_v110 import WINDOW_EXE, FakeProc, FakeTray, make_ctl


def make(tmp_path, exe=WINDOW_EXE, spawner=None):
    procs, trays = [], []

    def default_spawner(path):
        procs.append(FakeProc(path))
        return procs[-1]

    def tray_factory(**cb):
        trays.append(FakeTray(**cb))
        return trays[-1]
    eng = Engine(ctl=make_ctl(tmp_path), minimized=True, spawner=spawner or default_spawner,
                 tray_factory=tray_factory, info_path=tmp_path / "engine.json", tip_every=0.1, window=lambda: exe)
    return eng, procs, trays


def test_window_opens(tmp_path):
    eng, procs, trays = make(tmp_path)
    try:
        eng.start()
        assert eng.open_window() == {"ok": True, "opened": True}
        assert procs[0].cmd == WINDOW_EXE
    finally:
        eng.quit()


def test_missing_window_is_reported(tmp_path):
    eng, procs, trays = make(tmp_path, exe=None)
    try:
        eng.start()
        assert eng.open_window()["ok"] is False and procs == []
        assert "DualLink's window is missing" in trays[0].notes
    finally:
        eng.quit()


def test_window_that_cannot_start_at_all(tmp_path):
    def spawner(path):
        raise OSError("not a valid application")
    eng, procs, trays = make(tmp_path, spawner=spawner)
    try:
        eng.start()
        res = eng.open_window()
        assert res["ok"] is False and "not a valid application" in res["message"]
        assert "DualLink couldn't open its window" in trays[0].notes
        assert not eng.stopped.is_set()                           # downloads keep working
    finally:
        eng.quit()


def test_window_crashing_while_starting_is_reported(tmp_path):
    eng, procs, trays = make(tmp_path)
    try:
        eng.start()
        eng.open_window()
        procs[0].exit(3)
        assert wait_for(lambda: "DualLink's window couldn't start" in trays[0].notes)
        assert len(procs) == 1 and not eng.window_open()
        assert eng.open_window()["opened"] and len(procs) == 2   # opening it again is still possible
    finally:
        eng.quit()


def test_closing_the_window_normally_is_not_a_crash(tmp_path):
    eng, procs, trays = make(tmp_path)
    try:
        eng.start()
        eng.open_window()
        procs[0].exit(0)
        assert wait_for(lambda: not eng.window_open())
        time.sleep(0.3)
        assert not any("couldn't" in n for n in trays[0].notes)
    finally:
        eng.quit()


def test_window_closed_by_dualink_is_not_a_crash(tmp_path):
    eng, procs, trays = make(tmp_path)
    eng.start()
    eng.open_window()
    eng._close_window()                                          # e.g. quitting for an update
    time.sleep(0.3)
    assert procs[0].terminated and not any("couldn't" in n for n in trays[0].notes)
    eng.quit()


def test_window_gets_a_clean_environment(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.append((cmd, kw)) or "proc")
    assert spawn(WINDOW_EXE) == "proc"
    cmd, kw = seen[0]
    assert cmd == [WINDOW_EXE]
    assert kw["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"     # its own app, not sharing DualLink's files


def test_window_path(tmp_path, monkeypatch):
    exe = tmp_path / "DualLinkWindow.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("DUALLINK_NATIVE_WINDOW", str(exe))
    assert window_path() == str(exe)
    monkeypatch.setenv("DUALLINK_NATIVE_WINDOW", str(tmp_path / "missing.exe"))
    assert window_path() is None


def test_window_path_from_source_uses_the_newest_build(tmp_path, monkeypatch):
    monkeypatch.setenv("DUALLINK_NATIVE_WINDOW", "")
    monkeypatch.setattr(service, "REPO", str(tmp_path))
    assert window_path() is None                                  # nothing built yet
    debug = tmp_path / "native" / "DualLinkWindow" / "bin" / "x64" / "Debug" / "net10.0" / "win-x64"
    debug.mkdir(parents=True)
    (debug / "DualLinkWindow.exe").write_bytes(b"MZ")
    assert window_path() == str(debug / "DualLinkWindow.exe")
    published = tmp_path / "dist" / "native"
    published.mkdir(parents=True)
    (published / "DualLinkWindow.exe").write_bytes(b"MZ")
    os.utime(debug / "DualLinkWindow.exe", (1, 1))                # older
    assert window_path() == str(published / "DualLinkWindow.exe")


def test_window_path_when_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("DUALLINK_NATIVE_WINDOW", "")
    folder = tmp_path / "DualLinkWindow"
    folder.mkdir()
    (folder / "DualLinkWindow.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "DualLink.exe"))
    sys.frozen = True
    try:
        assert window_path() == str(folder / "DualLinkWindow.exe")
    finally:
        del sys.frozen


def test_setting_from_1_11_is_harmless(tmp_path):
    """1.11 had a "Use the new window" switch; its saved value doesn't matter any more."""
    (tmp_path / "c.json").write_text(json.dumps({"native_window": False, "eth_name": "Ethernet"}))
    ctl = make_ctl(tmp_path)
    try:
        ctl.update_settings({"native_window": True})
        assert ctl.public_settings()["eth_name"] == "Ethernet"
    finally:
        ctl.shutdown()
