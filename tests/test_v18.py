"""1.8: in-app updates (download, verify, switch off safely, install, restart) and a single running copy."""
import hashlib
import http.server
import threading
import time

import pytest

from duallink import instance, updates
from duallink.controller import Controller
from test_adapters import Adapters, wait_for

PAYLOAD = b"new DualLink build " * 50000          # ~950 KB


class FileServer:
    def __init__(self, body, advertised=None):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/DualLink-Setup-9.9.9.exe"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def asset(url, size=len(PAYLOAD), digest="sha256:" + hashlib.sha256(PAYLOAD).hexdigest()):
    return {"name": "DualLink-Setup-9.9.9.exe", "url": url, "size": size, "digest": digest}


# ---- download & verify ----------------------------------------------------------------- #
def test_download_verifies_and_reports_progress(tmp_path):
    srv = FileServer(PAYLOAD)
    try:
        seen = []
        path = updates.download(asset(srv.url), tmp_path, progress=lambda d, t: seen.append((d, t)), allow_any_host=True)
        assert path.read_bytes() == PAYLOAD and path.name == "DualLink-Setup-9.9.9.exe"
        assert seen[-1] == (len(PAYLOAD), len(PAYLOAD)) and len(seen) > 1
    finally:
        srv.close()


def test_download_rejects_wrong_checksum(tmp_path):
    srv = FileServer(PAYLOAD)
    try:
        with pytest.raises(updates.UpdateError, match="checksum"):
            updates.download(asset(srv.url, digest="sha256:" + "0" * 64), tmp_path, allow_any_host=True)
        assert not list((tmp_path / "DualLink-update").glob("*.exe"))       # nothing left to run
    finally:
        srv.close()


def test_download_rejects_incomplete_file(tmp_path):
    srv = FileServer(PAYLOAD)
    try:
        with pytest.raises(updates.UpdateError, match="incomplete"):
            updates.download(asset(srv.url, size=len(PAYLOAD) + 10, digest=""), tmp_path, allow_any_host=True)
    finally:
        srv.close()


def test_download_only_from_github(tmp_path):
    with pytest.raises(updates.UpdateError, match="GitHub"):
        updates.download(asset("https://evil.example.com/DualLink.exe"), tmp_path)
    with pytest.raises(updates.UpdateError, match="GitHub"):
        updates.download(asset("http://github.com/x/DualLink.exe"), tmp_path)    # not https


# ---- release details ---------------------------------------------------------------------- #
def test_check_reads_assets_and_notes():
    release = {"tag_name": "v9.9.9", "html_url": "https://github.com/me/DualLink/releases/tag/v9.9.9",
               "body": "## What's Changed\n* Faster", "assets": [
                   {"name": "DualLink-Setup-9.9.9.exe", "size": 123, "digest": "sha256:ab",
                    "browser_download_url": "https://github.com/me/DualLink/releases/download/v9.9.9/DualLink-Setup-9.9.9.exe"},
                   {"name": "DualLink.exe", "size": 456,
                    "browser_download_url": "https://github.com/me/DualLink/releases/download/v9.9.9/DualLink.exe"}]}
    u = updates.check(repo="me/DualLink", fetch=lambda r: release)
    assert u["version"] == "9.9.9" and u["notes"].startswith("## What's Changed")
    assert u["assets"]["installer"]["size"] == 123 and u["assets"]["installer"]["digest"] == "sha256:ab"
    assert u["assets"]["portable"]["url"].endswith("/DualLink.exe")


# ---- standalone exe swap ------------------------------------------------------------------- #
def test_portable_swap(tmp_path):
    cur = tmp_path / "DualLink.exe"
    cur.write_bytes(b"old")
    new = tmp_path / "download.exe"
    new.write_bytes(b"new")
    updates.swap_portable(new, cur, launch=False)
    assert cur.read_bytes() == b"new"
    assert (tmp_path / "DualLink.old.exe").read_bytes() == b"old"     # the running one, renamed
    updates.remove_leftovers(cur)
    assert not (tmp_path / "DualLink.old.exe").exists()


def test_running_from_source_is_detected():
    assert updates.install_kind() == "source"


# ---- the whole update flow in the controller ------------------------------------------------- #
def make(tmp_path, kind):
    ad = Adapters(**{"Ethernet": "127.0.0.1", "Ethernet 2": "127.0.0.2"})
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad, ipv6_lister=lambda: {})
    ctl.update_settings({"eth_name": "Ethernet", "hot_name": "Ethernet 2", "port": 0, "qbt_auto": False})
    calls = []
    ctl.install_kind_fn = lambda: kind

    def fake_download(a, progress):
        for i in range(1, 5):
            progress(i * 25, 100)
            time.sleep(0.05)
        calls.append(("download", a["name"]))
        p = tmp_path / a["name"]
        p.write_bytes(b"x")
        return p
    ctl.update_download = fake_download
    ctl.update_installer = lambda p: calls.append(("installer", p.name, ctl.running))
    ctl.update_portable = lambda p: calls.append(("portable", p.name, ctl.running))
    ctl.quit_app = lambda: calls.append(("quit",))
    ctl.update = {"version": "9.9.9", "url": "https://github.com/me/DualLink/releases/tag/v9.9.9", "assets": {
        "installer": {"name": "DualLink-Setup-9.9.9.exe", "url": "https://github.com/x", "size": 100},
        "portable": {"name": "DualLink.exe", "url": "https://github.com/y", "size": 100}}}
    return ctl, calls


def test_installed_copy_updates_with_installer(tmp_path):
    ctl, calls = make(tmp_path, "installed")
    try:
        assert ctl.power_on()["ok"]
        assert ctl.start_update()["ok"]
        assert wait_for(lambda: ("quit",) in calls)
        assert calls == [("download", "DualLink-Setup-9.9.9.exe"),
                         ("installer", "DualLink-Setup-9.9.9.exe", False),     # switched off before installing
                         ("quit",)]
        assert ctl.status()["update_state"]["phase"] == "installing"
    finally:
        ctl.shutdown()


def test_standalone_copy_swaps_exe(tmp_path):
    ctl, calls = make(tmp_path, "portable")
    try:
        assert ctl.start_update()["ok"]
        assert wait_for(lambda: ("quit",) in calls)
        assert calls[0] == ("download", "DualLink.exe") and calls[1][0] == "portable"
    finally:
        ctl.shutdown()


def test_update_errors_are_reported(tmp_path):
    ctl, calls = make(tmp_path, "installed")
    try:
        def broken(a, progress):
            raise updates.UpdateError("The download failed (no internet).")
        ctl.update_download = broken
        assert ctl.start_update()["ok"]
        assert wait_for(lambda: ctl.update_state["phase"] == "error")
        assert "no internet" in ctl.status()["update_state"]["message"]
        assert ("quit",) not in calls                                          # nothing installed, still running
        ctl.install_kind_fn = lambda: "source"
        r = ctl.start_update()
        assert not r["ok"] and "git pull" in r["message"]
        ctl.install_kind_fn = lambda: "installed"
        ctl.update["assets"]["installer"] = None
        r = ctl.start_update()
        assert not r["ok"] and "download page" in r["message"]
    finally:
        ctl.shutdown()


def test_single_instance_off_windows():
    assert instance.acquire() is True
