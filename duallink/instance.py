"""One engine and one window at a time.

Starting DualLink again just asks the running engine to show its window; opening the window while it's already
open brings it to the front.
"""
import os
import threading
import time

ENGINE_MUTEX = "Local\\DualLinkSingleInstance"      # same name as before 1.10, so old and new see each other
ENGINE_SHOW = "Local\\DualLinkShowWindow"            # "please open the window" -> engine
WINDOW_MUTEX = "Local\\DualLinkWindow"
WINDOW_SHOW = "Local\\DualLinkWindowFront"           # "come to the front" -> window process
_handles = {}


def _k32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    for fn in ("CreateMutexW", "OpenMutexW", "CreateEventW", "OpenEventW", "OpenProcess"):
        getattr(k, fn).restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.SetEvent.argtypes = (wintypes.HANDLE,)
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k, ctypes


def acquire(wait=0.0, name=ENGINE_MUTEX):
    """True if nobody else holds `name`. With wait, keep trying (used right after an update restart)."""
    if os.name != "nt":
        return True
    k, ctypes = _k32()
    end = time.time() + wait
    while True:
        h = k.CreateMutexW(None, False, name)
        if ctypes.get_last_error() != 183:          # ERROR_ALREADY_EXISTS
            _handles[name] = h
            return True
        k.CloseHandle(h)
        if time.time() >= end:
            return False
        time.sleep(0.5)


def release(name):
    """Let go of a mutex before this process exits (so a process we start can take it)."""
    h = _handles.pop(name, None)
    if h and os.name == "nt":
        k, _ = _k32()
        k.CloseHandle(h)


def held(name):
    """True if some process holds the named mutex."""
    if os.name != "nt":
        return False
    k, _ = _k32()
    h = k.OpenMutexW(0x00100000, False, name)         # SYNCHRONIZE
    if h:
        k.CloseHandle(h)
        return True
    return False


def signal_show(name=ENGINE_SHOW):
    """Ask the process listening on `name` to show its window."""
    if os.name != "nt":
        return False
    import ctypes
    try:
        ctypes.WinDLL("user32").AllowSetForegroundWindow(-1)   # ASFW_ANY: let it come to the front
    except Exception:
        pass
    k, _ = _k32()
    h = k.OpenEventW(0x0002, False, name)             # EVENT_MODIFY_STATE
    if not h:
        return False
    k.SetEvent(h)
    k.CloseHandle(h)
    return True


def allow_foreground(pid):
    """Let process `pid` (the window we just started) come to the front."""
    if os.name != "nt" or not pid:
        return
    try:
        import ctypes
        ctypes.WinDLL("user32").AllowSetForegroundWindow(int(pid))
    except Exception:
        pass


def listen_for_show(callback, name=ENGINE_SHOW):
    """Call callback() whenever another process signals `name`."""
    if os.name != "nt":
        return
    k, _ = _k32()
    h = k.CreateEventW(None, False, False, name)       # auto-reset
    _handles["event:" + name] = h

    def loop():
        while True:
            if k.WaitForSingleObject(h, 0xFFFFFFFF) == 0:
                try:
                    callback()
                except Exception:
                    pass
    threading.Thread(target=loop, daemon=True, name="show-listener").start()


def on_exit(pid, callback, poll=2.0):
    """Call callback() once process `pid` has ended (the window uses this to close along with the engine)."""
    def wait():
        if os.name == "nt":
            k, _ = _k32()
            h = k.OpenProcess(0x00100000, False, int(pid))      # SYNCHRONIZE
            if h:
                k.WaitForSingleObject(h, 0xFFFFFFFF)
                k.CloseHandle(h)
        else:
            while True:
                try:
                    os.kill(int(pid), 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    pass
                time.sleep(poll)
        try:
            callback()
        except Exception:
            pass
    threading.Thread(target=wait, daemon=True, name="engine-watch").start()
