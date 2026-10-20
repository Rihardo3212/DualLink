"""Window (pywebview) + system tray (pystray) around the Controller."""
import os
import sys
import threading

import webview

from .controller import Controller, SettingsError, set_autostart
from .icon import make_icon


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    if hasattr(sys, "_MEIPASS"):
        base = os.path.join(base, "duallink")
    return os.path.join(base, *parts)


def fmt_speed(b):
    for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
        if b < 1024 or unit == "GiB/s":
            return f"{b:.0f} {unit}" if unit == "B/s" else f"{b:.1f} {unit}"
        b /= 1024


class Api:
    """Methods callable from the page as window.pywebview.api.<name>()."""

    def __init__(self, ctl: Controller):
        self._ctl = ctl

    def get_init(self):
        adapters = self._ctl.adapters()
        return {"settings": self._ctl.public_settings(), "adapters": adapters}

    def status(self):
        return self._ctl.status(for_ui=True)

    def refresh_adapters(self):
        adapters = self._ctl.adapters()
        return {"settings": self._ctl.public_settings(), "adapters": adapters}

    def save_settings(self, new):
        before = bool(self._ctl.cfg.get("autostart"))
        try:
            settings = self._ctl.update_settings(new)
        except SettingsError as e:
            return {"settings": self._ctl.public_settings(), "error": str(e)}
        except (TypeError, ValueError):
            return {"settings": self._ctl.public_settings(), "error": "Some settings were not valid numbers."}
        error = None
        if bool(settings.get("autostart")) != before:
            try:
                set_autostart(bool(settings["autostart"]))
            except Exception as e:  # noqa: BLE001
                error = f"Couldn't change 'Start with Windows': {e}"
        return {"settings": settings, "error": error}

    def power(self, on):
        return self._ctl.power_on() if on else self._ctl.power_off()

    def retry_qbt(self):
        return self._ctl.retry_qbt()

    def test_qbt(self):
        return self._ctl.test_qbt()

    def start_speed_test(self):
        return self._ctl.start_speed_test()

    def check_updates(self):
        return self._ctl.check_updates_now()

    def start_update(self):
        return self._ctl.start_update()

    def open_url(self, url):
        if not str(url).startswith("https://github.com/"):
            return False                  # only ever open our own release pages
        import webbrowser
        webbrowser.open(url)
        return True

    def traffic_check(self):
        return self._ctl.traffic_check()

    def copy(self, text):
        """Clipboard fallback for the setup steps (navigator.clipboard may be blocked for local pages)."""
        try:
            import subprocess
            subprocess.run(["clip"], input=str(text).encode("ascii", "ignore"), check=True, creationflags=0x08000000)
            return True
        except Exception:
            return False


class DualLinkApp:
    def __init__(self, start_minimized=False, updated=False):
        self.ctl = Controller()
        self.api = Api(self.ctl)
        self.quitting = False
        self.tray = None
        self.window = webview.create_window(
            "DualLink", resource("ui", "index.html"), js_api=self.api,
            width=920, height=720, min_size=(720, 560), hidden=start_minimized,
            background_color="#0e1015",
        )
        self.window.events.closing += self._on_closing
        for name, visible in (("minimized", False), ("restored", True), ("shown", True), ("maximized", True)):
            event = getattr(self.window.events, name, None)        # not every pywebview version has all
            if event is not None:
                event += (lambda v: (lambda *a: setattr(self.ctl, "ui_visible", v)))(visible)
        self.ctl.notify = self._notify
        self.ctl.on_change = self._refresh_tray
        self.ctl.quit_app = self._quit_for_update
        self.ctl.just_updated = updated

    # ---- tray ---------------------------------------------------------------- #
    def _start_tray(self):
        try:
            import pystray
        except Exception:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open DualLink", lambda: self.show(), default=True),
            pystray.MenuItem(lambda _: "Turn off" if self.ctl.running else "Turn on", lambda: self._toggle()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.quit()),
        )
        self.tray = pystray.Icon("DualLink", make_icon(64, on=False), "DualLink - off", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()
        threading.Thread(target=self._tray_tooltip_loop, daemon=True).start()

    def _tray_tooltip_loop(self):
        import time
        while not self.quitting:
            time.sleep(2)
            if self.tray and self.ctl.running:
                s = self.ctl.status()
                self.tray.title = (f"DualLink - {fmt_speed(s['combined'])}  "
                                   f"(Ethernet {fmt_speed(s['links'][0]['speed'])}, "
                                   f"Hotspot {fmt_speed(s['links'][1]['speed'])})")[:120]

    def _refresh_tray(self):
        if not self.tray:
            return
        self.tray.icon = make_icon(64, on=self.ctl.running)
        self.tray.title = "DualLink - on" if self.ctl.running else "DualLink - off"
        try:
            self.tray.update_menu()
        except Exception:
            pass

    def _notify(self, title, msg):
        if self.tray:
            try:
                self.tray.notify(msg, title)
            except Exception:
                pass

    def _toggle(self):
        threading.Thread(target=lambda: self.ctl.power_off() if self.ctl.running else self._tray_power_on(),
                         daemon=True).start()

    def _tray_power_on(self):
        res = self.ctl.power_on()
        if not res.get("ok"):
            self._notify("DualLink couldn't start", res.get("message", ""))
            self.show()

    # ---- window -------------------------------------------------------------- #
    def show(self):
        self.ctl.ui_visible = True
        self.window.show()
        try:
            self.window.restore()
        except Exception:
            pass

    def _on_closing(self):
        if self.quitting or not (self.ctl.cfg.get("tray") and self.tray):
            self.quitting = True
            self.ctl.shutdown()
            if self.tray:
                self.tray.stop()
            return True
        self.window.hide()
        self.ctl.ui_visible = False
        if not self.ctl.cfg.get("tray_hint_shown"):
            self.ctl.cfg["tray_hint_shown"] = True
            self.ctl.save()
            self._notify("DualLink is still running",
                         "It's in the system tray. Right-click the icon to quit.")
        return False  # keep running in the tray

    def _quit_for_update(self):
        """Close completely so the installer can replace DualLink.exe (the new version starts by itself)."""
        import os
        import time
        self.quitting = True
        try:
            self.ctl.proxy.stop()
            if self.tray:
                self.tray.stop()
            self.window.destroy()
        except Exception:
            pass
        time.sleep(0.5)
        os._exit(0)

    def quit(self):
        self.quitting = True
        self.ctl.shutdown()
        if self.tray:
            self.tray.stop()
        self.window.destroy()

    def run(self):
        from .instance import listen_for_show
        listen_for_show(self.show)                # starting DualLink again shows this window
        self._start_tray()
        if self.ctl.just_updated:
            from . import __version__
            self._notify("DualLink updated", f"You're now on version {__version__}.")
        threading.Thread(target=self.ctl.startup, daemon=True).start()   # "turn on when DualLink starts"
        self.ctl.start_update_checks()
        webview.start()
