"""1.12.1: after an in-app update DualLink is on again if it was on before."""
from duallink.controller import Controller
from test_adapters import Adapters, wait_for
from test_v18 import make


def restart(tmp_path, updated):
    """The new version starting from the same settings (the installer passes --updated)."""
    ad = Adapters(**{"Ethernet": "127.0.0.1", "Ethernet 2": "127.0.0.2"})
    ctl = Controller(cfg_path=tmp_path / "c.json", adapter_lister=ad, ipv6_lister=lambda: {})
    ctl.just_updated = updated
    return ctl


def test_on_again_after_an_update(tmp_path):
    ctl, calls = make(tmp_path, "installed")
    try:
        assert ctl.power_on()["ok"]
        assert ctl.start_update()["ok"]
        assert wait_for(lambda: ("quit",) in calls)
        assert calls[1] == ("installer", "DualLink-Setup-9.9.9.exe", False)   # still off while installing
    finally:
        ctl.shutdown()
    new = restart(tmp_path, updated=True)
    try:
        assert new.startup()["ok"] and new.running
        assert "on_after_update" not in new.public_settings()
    finally:
        new.shutdown()
    later = restart(tmp_path, updated=False)       # only the start right after the update
    try:
        later.startup()
        assert not later.running
    finally:
        later.shutdown()


def test_off_stays_off_after_an_update(tmp_path):
    ctl, calls = make(tmp_path, "installed")
    try:
        assert ctl.start_update()["ok"]
        assert wait_for(lambda: ("quit",) in calls)
    finally:
        ctl.shutdown()
    new = restart(tmp_path, updated=True)
    try:
        new.startup()
        assert not new.running
    finally:
        new.shutdown()


def test_failed_install_turns_back_on(tmp_path):
    ctl, calls = make(tmp_path, "installed")

    def blocked(path):
        raise OSError("The installer was blocked.")
    ctl.update_installer = blocked
    try:
        assert ctl.power_on()["ok"]
        assert ctl.start_update()["ok"]
        assert wait_for(lambda: ctl.update_state["phase"] == "error")
        assert wait_for(lambda: ctl.running)                     # back to how it was
        assert ctl.cfg["on_after_update"] is False               # and won't turn on by itself later
        assert ("quit",) not in calls
    finally:
        ctl.shutdown()
