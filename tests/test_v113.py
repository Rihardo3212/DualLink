"""1.13: a new install starts with no apps; apps can be added and removed."""
import json
import time

from duallink.api import Api
from duallink.controller import Controller
from helpers import FakeQbt
from test_adapters import Adapters, wait_for


def make(tmp_path, **settings):
    ad = Adapters(**{"Ethernet": "127.0.0.1", "Ethernet 2": "127.0.0.2"})
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad, ipv6_lister=lambda: {})
    ctl.update_settings(dict({"eth_name": "Ethernet", "hot_name": "Ethernet 2", "port": 0}, **settings))
    return ctl


def reopen(tmp_path):
    return Controller(cfg_path=tmp_path / "c.json", adapter_lister=lambda: [], ipv6_lister=lambda: {})


def test_new_install_has_no_apps(tmp_path):
    q = FakeQbt()
    ctl = make(tmp_path, qbt_url=q.url)                         # no settings file before this
    try:
        s = ctl.public_settings()
        assert s["added_apps"] == []
        assert not s["qbt_auto"] and not any(s[k] for k in ("fdm", "jd", "idm", "firefox", "other"))
        assert ctl.power_on()["ok"]
        assert ctl.qbt_state["state"] == "disabled" and q.calls == []    # qBittorrent left alone
    finally:
        ctl.power_off()
        ctl.shutdown()
        q.close()


def test_settings_from_before_keep_their_list(tmp_path):
    """Up to 1.12 qBittorrent and FDM were always listed, plus any app that was switched on."""
    (tmp_path / "c.json").write_text(json.dumps({"qbt_auto": False, "fdm": False, "jd": True, "eth_name": "Ethernet"}))
    ctl = reopen(tmp_path)
    try:
        assert ctl.public_settings()["added_apps"] == ["qbt", "fdm", "jd"]
        assert ctl.cfg["qbt_auto"] is False                     # switched off, still listed
    finally:
        ctl.shutdown()
    (tmp_path / "c.json").write_text(json.dumps({"eth_name": "Ethernet"}))   # very old: qBittorrent was on by default
    ctl = reopen(tmp_path)
    try:
        assert ctl.cfg["qbt_auto"] is True and ctl.cfg["added_apps"] == ["qbt", "fdm"]
    finally:
        ctl.shutdown()


def test_adding_and_removing_apps(tmp_path):
    ctl = make(tmp_path)
    try:
        ctl.update_settings({"fdm": True})                      # switching an app on adds it
        assert ctl.cfg["added_apps"] == ["fdm"]
        ctl.update_settings({"added_apps": ["fdm", "qbt"], "qbt_auto": True})
        ctl.update_settings({"fdm": False})                     # switched off: stays in the list
        assert ctl.cfg["added_apps"] == ["fdm", "qbt"]
        ctl.update_settings({"added_apps": ["qbt"], "fdm": False})       # removed
        assert ctl.cfg["added_apps"] == ["qbt"]
    finally:
        ctl.shutdown()
    again = reopen(tmp_path)
    try:
        assert again.cfg["added_apps"] == ["qbt"] and not again.cfg["fdm"]    # remembered
    finally:
        again.shutdown()


def test_app_list_is_checked(tmp_path):
    ctl = make(tmp_path)
    try:
        ctl.update_settings({"added_apps": ["jd", "nope", "jd", 3, None]})
        assert ctl.cfg["added_apps"] == ["jd"]
        ctl.update_settings({"added_apps": "qbt"})              # not a list
        assert ctl.cfg["added_apps"] == []
        r = Api(ctl).save_settings({"added_apps": ["idm"]})     # the window's way in
        assert r["error"] is None and r["settings"]["added_apps"] == ["idm"]
    finally:
        ctl.shutdown()


def test_removing_qbittorrent_while_on_gives_it_its_settings_back(tmp_path):
    q = FakeQbt()
    original = dict(q.prefs)
    ctl = make(tmp_path, qbt_url=q.url, qbt_auto=True, added_apps=["qbt"])
    try:
        assert ctl.power_on()["ok"] and ctl.qbt_state["state"] == "connected"
        assert q.prefs["proxy_port"] == ctl.proxy.port
        ctl.update_settings({"added_apps": [], "qbt_auto": False})        # removed in the window
        assert wait_for(lambda: q.prefs == original)
        assert ctl.cfg["qbt_saved"] is None and ctl.qbt_state["state"] == "disabled"
        assert ctl.running                                                 # DualLink itself stays on
        ctl.update_settings({"qbt_auto": True})                            # added back
        assert wait_for(lambda: q.prefs["proxy_port"] == ctl.proxy.port)
        assert wait_for(lambda: ctl.qbt_state["state"] == "connected")
        assert ctl.cfg["added_apps"] == ["qbt"]
        assert ctl.power_off()["ok"] and q.prefs == original
    finally:
        ctl.shutdown()
        q.close()


def test_other_settings_dont_touch_qbittorrent(tmp_path):
    q = FakeQbt()
    ctl = make(tmp_path, qbt_url=q.url, qbt_auto=True)
    try:
        assert ctl.power_on()["ok"]
        n = len(q.calls)
        ctl.update_settings({"quota_mb": 500, "qbt_auto": True})          # the window always sends everything
        time.sleep(0.3)
        assert len(q.calls) == n
    finally:
        ctl.power_off()
        ctl.shutdown()
        q.close()
