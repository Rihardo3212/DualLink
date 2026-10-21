"""DualLink's background engine: the proxy, balancing, qBittorrent set-up, the tray icon and the control API.

It never loads any window libraries, so it stays small while DualLink sits in the tray. The window
(DualLinkWindow\\DualLinkWindow.exe, a native Windows app) is a separate process that exists only while it's
open; it asks the engine for everything over the control API.
"""
import glob
import os
import subprocess
import sys
import threading
import time

from . import __version__, instance
from .api import Api, ControlServer
from .controller import Controller

NATIVE_EXE = "DualLinkWindow.exe"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fmt_speed(b):
    for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
        if b < 1024 or unit == "GiB/s":
            return f"{b:.0f} {unit}" if unit == "B/s" else f"{b:.1f} {unit}"
        b /= 1024


def window_path():
    """DualLink's window: DualLinkWindow\\DualLinkWindow.exe next to DualLink.exe.

    DUALLINK_NATIVE_WINDOW can point at another copy. Running from source, the newest build in the repository
    is used (dist\\native, or Visual Studio's bin folder)."""
    env = os.environ.get("DUALLINK_NATIVE_WINDOW")
    if env:
        return env if os.path.isfile(env) else None
    if getattr(sys, "frozen", False):
        path = os.path.join(os.path.dirname(sys.executable), "DualLinkWindow", NATIVE_EXE)
        return path if os.path.isfile(path) else None
    builds = [os.path.join(REPO, "dist", "native", NATIVE_EXE)]
    builds += glob.glob(os.path.join(REPO, "native", "DualLinkWindow", "bin", "**", NATIVE_EXE), recursive=True)
    builds = [b for b in builds if os.path.isfile(b)]
    return max(builds, key=os.path.getmtime) if builds else None


def spawn(exe):
    """Start the window as its own app (it doesn't share anything with this process)."""
    from .updates import independent_env
    return subprocess.Popen([exe], close_fds=True, env=independent_env())


class Engine:
    def __init__(self, ctl=None, minimized=False, updated=False, spawner=spawn, tray_factory=None,
                 info_path=None, tip_every=2.0, window=window_path):
        self.ctl = ctl or Controller()
        self.api = Api(self.ctl)
        self.minimized, self.updated = minimized, updated
        self._spawn = spawner
        self._window_path = window
        self._closed_by_us = set()
        self._tray_factory = tray_factory
        self._info_path = info_path
        self.tip_every = tip_every
        self.tray = None
        self.window = None                    # the window process while it's open
        self._wlock = threading.Lock()
        self.quitting = False
        self.stopped = threading.Event()
        handlers = {name: getattr(self.api, name) for name in Api.PAGE}
        handlers.update(ping=self.ping, open_window=self.open_window, window_closed=self.window_closed,
                        quit=self.quit_soon)
        self.server = ControlServer(handlers)
        self.ctl.ui_visible = True            # the window knows when it's minimised
        self.ctl.notify = self.notify
        self.ctl.on_change = self._refresh_tray
        self.ctl.quit_app = self._quit_for_update
        self.ctl.just_updated = updated

    # ---- control API extras ---------------------------------------------------------------- #
    def ping(self):
        return {"version": __version__, "pid": os.getpid(), "window": self.window_open(),
                "running": self.ctl.running, "tray": self.tray is not None}

    def window_open(self):
        w = self.window
        return w is not None and w.poll() is None

    def open_window(self):
        """Open the window, or bring it to the front if it's already open."""
        if self.quitting:
            return {"ok": False}
        with self._wlock:
            if self.window_open() or instance.held(instance.WINDOW_MUTEX):
                instance.signal_show(instance.WINDOW_SHOW)
                return {"ok": True, "opened": False}
            exe = self._window_path()
            if not exe:
                self.notify("DualLink's window is missing", "Reinstall DualLink from its GitHub Releases page.")
                return {"ok": False, "message": "DualLink's window is missing."}
            try:
                self.window = proc = self._spawn(exe)
            except OSError as e:
                self.notify("DualLink couldn't open its window", str(e))
                return {"ok": False, "message": str(e)}
            instance.allow_foreground(getattr(proc, "pid", None))
        threading.Thread(target=self._wait_window, args=(proc, time.monotonic()), daemon=True).start()
        return {"ok": True, "opened": True}

    def _wait_window(self, proc, started=0.0):
        code = None
        try:
            code = proc.wait()
        except Exception:
            pass
        with self._wlock:
            if self.window is proc:
                self.window = None
            closed_by_us = id(proc) in self._closed_by_us
            self._closed_by_us.discard(id(proc))
        if code not in (0, None) and not closed_by_us and not self.quitting and time.monotonic() - started < 10:
            # it crashed while starting: say so (DualLink itself keeps working from the tray)
            self.notify("DualLink's window couldn't start",
                        "Downloads keep working. Try opening it again from the tray icon, or reinstall DualLink.")

    def window_closed(self):
        """The window process tells us the user closed the window."""
        if not self.ctl.cfg.get("tray") or self.tray is None:
            self.quit_soon()                  # no tray icon to come back to: closing the window quits
            return {"quitting": True}
        if not self.ctl.cfg.get("tray_hint_shown"):
            self.ctl.cfg["tray_hint_shown"] = True
            self.ctl.save()
            self.notify("DualLink is still running", "It's in the system tray. Right-click the icon to quit.")
        return {"quitting": False}

    def quit_soon(self):
        threading.Thread(target=self.quit, daemon=True).start()   # answer the request first
        return {"quitting": True}

    # ---- tray ------------------------------------------------------------------------------- #
    def _make_tray(self):
        factory = self._tray_factory
        if factory is None:
            from . import tray
            factory = tray.create
        try:
            return factory(on_open=self.open_window, on_toggle=self._toggle, on_quit=self.quit,
                           is_on=lambda: self.ctl.running)
        except Exception:
            return None

    def notify(self, title, msg):
        if self.tray:
            try:
                self.tray.notify(title, msg)
            except Exception:
                pass

    def _refresh_tray(self):
        if self.tray:
            try:
                self.tray.set_state(self.ctl.running, "DualLink - on" if self.ctl.running else "DualLink - off")
            except Exception:
                pass

    def tooltip(self):
        if not self.ctl.running:
            return "DualLink - off"
        speeds = self.ctl.speeds()
        parts = ", ".join(f"{label} {fmt_speed(sp)}" for label, sp in speeds)
        return f"DualLink - {fmt_speed(sum(sp for _, sp in speeds))}  ({parts})"[:127]

    def _tooltip_loop(self):
        while not self.stopped.wait(self.tip_every):
            if self.tray and self.ctl.running:
                try:
                    self.tray.set_tip(self.tooltip())
                except Exception:
                    pass

    def _toggle(self):
        if self.ctl.running:
            self.ctl.power_off()
            return
        res = self.ctl.power_on()
        if not res.get("ok"):
            self.notify("DualLink couldn't start", res.get("message", ""))
            self.open_window()

    # ---- start / stop ---------------------------------------------------------------------- #
    def _close_window(self):
        with self._wlock:
            proc, self.window = self.window, None
            if proc is not None:
                self._closed_by_us.add(id(proc))
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(3)
            except Exception:
                pass

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self._close_window()
        try:
            self.ctl.shutdown()               # gives qBittorrent its own settings back
        finally:
            self.server.unpublish()
            self.server.stop()
            if self.tray:
                try:
                    self.tray.stop()
                except Exception:
                    pass
            self.stopped.set()

    def _quit_for_update(self):
        """Close completely so the installer can replace DualLink.exe (the new version starts by itself)."""
        self.quitting = True
        try:
            self._close_window()
            self.ctl.proxy.stop()
            self.server.unpublish()
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        time.sleep(0.5)
        os._exit(0)

    def start(self):
        """Everything except waiting; returns once DualLink is ready."""
        self.server.start()
        self.server.publish(self._info_path)
        instance.listen_for_show(self.open_window)        # starting DualLink again opens the window
        if not self.minimized:
            self.open_window()                            # the window starts while the rest gets ready
        self.tray = self._make_tray()
        self._refresh_tray()
        if self.updated:
            self.notify("DualLink updated", f"You're now on version {__version__}.")
        threading.Thread(target=self.ctl.startup, daemon=True).start()   # "turn on when DualLink starts"
        self.ctl.start_update_checks()
        threading.Thread(target=self._tooltip_loop, daemon=True).start()
        return self

    def run(self):
        self.start()
        try:
            while not self.stopped.wait(1.0):
                pass
        except KeyboardInterrupt:
            self.quit()
