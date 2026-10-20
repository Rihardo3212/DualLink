"""The tray icon, drawn with the Windows API directly (no extra libraries, a few hundred KB of memory).

Left-click opens the window; right-click shows Open / Turn on-off / Quit. If the native icon can't be created,
pystray is used instead.
"""
import os
import threading

from .paths import resource

WM_NULL, WM_DESTROY, WM_CLOSE = 0x0000, 0x0002, 0x0010
WM_QUERYENDSESSION, WM_ENDSESSION = 0x0011, 0x0016
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_USER, WM_APP = 0x0400, 0x8000
WM_TRAY = WM_APP + 1                      # our icon's mouse messages
WM_STOP = WM_APP + 2                      # stop() from another thread
NIN_BALLOONUSERCLICK = WM_USER + 5
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_USER, NIIF_LARGE_ICON = 0x04, 0x20
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
SM_CXICON, SM_CXSMICON = 11, 49
CMD_OPEN, CMD_TOGGLE, CMD_QUIT = 1, 2, 3


def _api():
    import ctypes
    from ctypes import wintypes as w

    LRESULT = w.LPARAM
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE),
                    ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                    ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", w.BYTE * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                    ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                    ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                    ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                    ("guidItem", GUID), ("hBalloonIcon", w.HICON)]

    u = ctypes.WinDLL("user32", use_last_error=True)
    s = ctypes.WinDLL("shell32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    sig = {
        (u, "RegisterClassExW"): (w.ATOM, [ctypes.POINTER(WNDCLASSEXW)]),
        (u, "UnregisterClassW"): (w.BOOL, [w.LPCWSTR, w.HINSTANCE]),
        (u, "CreateWindowExW"): (w.HWND, [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]),
        (u, "DefWindowProcW"): (LRESULT, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
        (u, "DestroyWindow"): (w.BOOL, [w.HWND]),
        (u, "GetMessageW"): (w.BOOL, [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]),
        (u, "TranslateMessage"): (w.BOOL, [ctypes.POINTER(w.MSG)]),
        (u, "DispatchMessageW"): (LRESULT, [ctypes.POINTER(w.MSG)]),
        (u, "PostMessageW"): (w.BOOL, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
        (u, "PostQuitMessage"): (None, [ctypes.c_int]),
        (u, "RegisterWindowMessageW"): (w.UINT, [w.LPCWSTR]),
        (u, "CreatePopupMenu"): (w.HMENU, []),
        (u, "AppendMenuW"): (w.BOOL, [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]),
        (u, "SetMenuDefaultItem"): (w.BOOL, [w.HMENU, w.UINT, w.UINT]),
        (u, "TrackPopupMenu"): (ctypes.c_int, [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                               w.HWND, w.LPVOID]),
        (u, "DestroyMenu"): (w.BOOL, [w.HMENU]),
        (u, "GetCursorPos"): (w.BOOL, [ctypes.POINTER(w.POINT)]),
        (u, "SetForegroundWindow"): (w.BOOL, [w.HWND]),
        (u, "LoadImageW"): (w.HANDLE, [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]),
        (u, "DestroyIcon"): (w.BOOL, [w.HICON]),
        (u, "GetSystemMetrics"): (ctypes.c_int, [ctypes.c_int]),
        (s, "Shell_NotifyIconW"): (w.BOOL, [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]),
        (k, "GetModuleHandleW"): (w.HMODULE, [w.LPCWSTR]),
    }
    for (dll, name), (res, args) in sig.items():
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = res, args
    return ctypes, w, u, s, k, WNDPROC, WNDCLASSEXW, NOTIFYICONDATAW


class NativeTray:
    CLASS = "DualLinkTrayWindow"

    def __init__(self, on_open, on_toggle, on_quit, is_on):
        self.on_open, self.on_toggle, self.on_quit, self.is_on = on_open, on_toggle, on_quit, is_on
        self.hwnd = None
        self.added = False
        self._on = False
        self._tip = "DualLink - off"
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._error = None
        self._thread = None
        self._ending = False

    # ---- public ------------------------------------------------------------------------- #
    def start(self, timeout=5.0):
        self._thread = threading.Thread(target=self._run, daemon=True, name="tray")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("tray icon didn't start")
        if self._error:
            raise self._error
        return self

    def set_state(self, on, tip):
        with self._lock:
            self._on, self._tip = bool(on), tip
        self._modify(NIF_ICON | NIF_TIP)

    def set_tip(self, tip):
        with self._lock:
            self._tip = tip
        self._modify(NIF_TIP)

    def notify(self, title, msg):
        if not self.hwnd:
            return
        nid = self._nid(NIF_INFO)
        nid.szInfoTitle = str(title)[:63]
        nid.szInfo = str(msg)[:255] or " "
        nid.dwInfoFlags = NIIF_USER | NIIF_LARGE_ICON
        nid.hBalloonIcon = self._big
        self._shell(NIM_MODIFY, nid)

    def stop(self):
        if self.hwnd:
            self._u.PostMessageW(self.hwnd, WM_STOP, 0, 0)
        if self._thread and self._thread is not threading.current_thread() and not self._ending:
            self._thread.join(3)

    # ---- inside ---------------------------------------------------------------------------- #
    def _nid(self, flags):
        nid = self._NID()
        nid.cbSize = self._ctypes.sizeof(self._NID)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
        return nid

    def _shell(self, msg, nid):
        return bool(self._s.Shell_NotifyIconW(msg, self._ctypes.byref(nid)))

    def _modify(self, flags):
        if not self.hwnd:
            return
        with self._lock:
            nid = self._nid(flags)
            nid.hIcon = self._icons[self._on]
            nid.szTip = self._tip[:127]
        if not self._shell(NIM_MODIFY, nid) and not self.added:
            self._add()                         # Explorer wasn't ready when we started

    def _add(self):
        with self._lock:
            nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._icons[self._on]
            nid.szTip = self._tip[:127]
        self.added = self._shell(NIM_ADD, nid) or self._shell(NIM_MODIFY, nid)
        return self.added

    def _load(self, name, metric):
        size = self._u.GetSystemMetrics(metric) or 16
        h = self._u.LoadImageW(None, resource("assets", name), IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if not h:
            raise OSError(f"couldn't load {name}")
        return h

    def _run(self):
        try:
            (self._ctypes, w, self._u, self._s, k, WNDPROC, WNDCLASSEXW, self._NID) = _api()
            ctypes, u = self._ctypes, self._u
            self._icons = {False: self._load("icon_off.ico", SM_CXSMICON), True: self._load("icon_on.ico", SM_CXSMICON)}
            self._big = self._load("icon_on.ico", SM_CXICON)
            self._taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
            hinst = k.GetModuleHandleW(None)
            self._proc = WNDPROC(self._wndproc)        # keep a reference: Windows calls it
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = self.CLASS
            if not u.RegisterClassExW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:  # already there
                raise ctypes.WinError(ctypes.get_last_error())
            # a hidden top-level window (not message-only, so it hears "TaskbarCreated" when Explorer restarts)
            self.hwnd = u.CreateWindowExW(0, self.CLASS, "DualLink", 0, 0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._add()
        except Exception as e:  # noqa: BLE001
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        msg = w.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        for h in list(self._icons.values()) + [self._big]:
            u.DestroyIcon(h)

    def _later(self, fn):
        threading.Thread(target=fn, daemon=True).start()   # keep the tray responsive

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, NIN_BALLOONUSERCLICK):
                    self._later(self.on_open)
                elif event == WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == self._taskbar_created:           # Explorer restarted: put the icon back
                self.added = False
                self._add()
                return 0
            if msg == WM_STOP:
                self._shell(NIM_DELETE, self._nid(0))
                self._u.DestroyWindow(hwnd)
                return 0
            if msg == WM_CLOSE:                       # someone asked DualLink to close: quit cleanly
                self._later(self.on_quit)
                return 0
            if msg == WM_QUERYENDSESSION:
                return 1
            if msg == WM_ENDSESSION and wparam:        # Windows is signing out / shutting down
                self._ending = True
                t = threading.Thread(target=self.on_quit, daemon=True)
                t.start()
                t.join(4)
                return 0
            if msg == WM_DESTROY:
                self.hwnd = None
                self._u.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return self._u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self):
        u, ctypes = self._u, self._ctypes
        from ctypes import wintypes as w
        menu = u.CreatePopupMenu()
        u.AppendMenuW(menu, MF_STRING, CMD_OPEN, "Open DualLink")
        u.AppendMenuW(menu, MF_STRING, CMD_TOGGLE, "Turn off" if self.is_on() else "Turn on")
        u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        u.AppendMenuW(menu, MF_STRING, CMD_QUIT, "Quit")
        u.SetMenuDefaultItem(menu, CMD_OPEN, 0)
        pt = w.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(self.hwnd)             # so the menu closes when you click elsewhere
        cmd = u.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        u.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        u.DestroyMenu(menu)
        action = {CMD_OPEN: self.on_open, CMD_TOGGLE: self.on_toggle, CMD_QUIT: self.on_quit}.get(cmd)
        if action:
            self._later(action)


class PystrayTray:
    """Fallback: the same tray icon through pystray (needs Pillow, uses more memory)."""

    def __init__(self, on_open, on_toggle, on_quit, is_on):
        import pystray
        from .icon import make_icon
        self._make_icon = make_icon
        menu = pystray.Menu(
            pystray.MenuItem("Open DualLink", lambda: on_open(), default=True),
            pystray.MenuItem(lambda _: "Turn off" if is_on() else "Turn on", lambda: on_toggle()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: on_quit()),
        )
        self.icon = pystray.Icon("DualLink", make_icon(64, on=False), "DualLink - off", menu)

    def start(self):
        threading.Thread(target=self.icon.run, daemon=True, name="tray").start()
        return self

    def set_state(self, on, tip):
        self.icon.icon = self._make_icon(64, on=on)
        self.set_tip(tip)
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def set_tip(self, tip):
        self.icon.title = tip[:120]

    def notify(self, title, msg):
        self.icon.notify(msg, title)

    def stop(self):
        self.icon.stop()


def create(on_open, on_toggle, on_quit, is_on):
    """The tray icon, or None when there can't be one (not Windows, or both ways failed)."""
    if os.name != "nt":
        return None
    for kind in (NativeTray, PystrayTray):
        try:
            return kind(on_open, on_toggle, on_quit, is_on).start()
        except Exception:
            continue
    return None
