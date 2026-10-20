"""DualLink's window: a separate process that exists only while the window is open.

Everything it shows comes from the background engine over the control API (see api.py); closing the window
ends this process and gives its memory back, while the engine keeps downloads going from the tray.
"""
import os
import subprocess
import sys

from . import instance
from .api import EngineClient, EngineError, RemoteApi
from .paths import resource


def engine_command():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    return [sys.executable, main]


def find_engine():
    """The running engine, waiting for it if it's still starting. None if there's no engine."""
    client = EngineClient.connect(wait=3)
    if client is None and instance.held(instance.ENGINE_MUTEX):
        client = EngineClient.connect(wait=20)
    return client


def start_engine():
    """No engine running (the window was started on its own): start it; it opens a window itself."""
    from .updates import independent_env          # its own copy of the files, it outlives this process
    instance.release(instance.WINDOW_MUTEX)
    if not instance.held(instance.ENGINE_MUTEX):
        subprocess.Popen(engine_command(), env=independent_env(), close_fds=True)


def run():
    if not instance.acquire(name=instance.WINDOW_MUTEX):
        instance.signal_show(instance.WINDOW_SHOW)    # already open: bring it to the front
        return 0
    client = find_engine()
    if client is None:
        start_engine()
        return 0

    import webview

    state = {"visible": True, "engine_gone": False}
    api = RemoteApi(client, visible=lambda: state["visible"])
    window = webview.create_window(
        "DualLink", resource("ui", "index.html"), js_api=api,
        width=920, height=720, min_size=(720, 560), background_color="#0e1015",
    )

    def visible(v):
        return lambda *a: state.__setitem__("visible", v)

    for name, v in (("minimized", False), ("restored", True), ("shown", True), ("maximized", True)):
        event = getattr(window.events, name, None)    # not every pywebview version has all of them
        if event is not None:
            event += visible(v)

    def closed(*a):
        if state["engine_gone"]:
            return
        try:
            client.call("window_closed", timeout=5)  # the engine shows the "still running" hint once
        except EngineError:
            pass

    window.events.closed += closed

    def to_front():
        window.show()
        try:
            window.restore()
        except Exception:
            pass
        state["visible"] = True

    def engine_ended():
        state["engine_gone"] = True
        window.destroy()

    instance.listen_for_show(to_front, name=instance.WINDOW_SHOW)
    if client.pid:
        instance.on_exit(client.pid, engine_ended)   # DualLink was quit from the tray: close with it
    webview.start()
    client.close()
    return 0
