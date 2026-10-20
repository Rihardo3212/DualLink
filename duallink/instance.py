"""Only one DualLink at a time. Starting it again just brings the running one to the front."""
import os
import time

MUTEX = "Local\\DualLinkSingleInstance"
SHOW_EVENT = "Local\\DualLinkShowWindow"
_handles = []


def _k32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CreateEventW.restype = wintypes.HANDLE
    k.OpenEventW.restype = wintypes.HANDLE
    return k, ctypes


def acquire(wait=0.0):
    """True if this is the only DualLink. With wait, keep trying (used right after an update restart)."""
    if os.name != "nt":
        return True
    k, ctypes = _k32()
    end = time.time() + wait
    while True:
        h = k.CreateMutexW(None, False, MUTEX)
        if ctypes.get_last_error() != 183:          # ERROR_ALREADY_EXISTS
            _handles.append(h)
            return True
        k.CloseHandle(h)
        if time.time() >= end:
            return False
        time.sleep(0.5)


def signal_show():
    """Ask the running DualLink to show its window."""
    if os.name != "nt":
        return
    k, _ = _k32()
    h = k.OpenEventW(0x0002, False, SHOW_EVENT)      # EVENT_MODIFY_STATE
    if h:
        k.SetEvent(h)
        k.CloseHandle(h)


def listen_for_show(callback):
    """Call callback() whenever another start of DualLink asks us to show the window."""
    if os.name != "nt":
        return
    import threading
    k, _ = _k32()
    h = k.CreateEventW(None, False, False, SHOW_EVENT)  # auto-reset
    _handles.append(h)

    def loop():
        while True:
            if k.WaitForSingleObject(h, 0xFFFFFFFF) == 0:
                try:
                    callback()
                except Exception:
                    pass
    threading.Thread(target=loop, daemon=True).start()
