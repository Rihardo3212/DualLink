//! The tray icon, drawn with the Windows API directly (no extra libraries).
//!
//! Left-click opens the window; right-click shows Open / Turn on-off / Quit.

#[cfg_attr(not(windows), allow(dead_code))]
pub struct Callbacks {
    pub open: Box<dyn Fn() + Send + Sync>,
    pub toggle: Box<dyn Fn() + Send + Sync>,
    pub quit: Box<dyn Fn() + Send + Sync>,
    pub is_on: Box<dyn Fn() -> bool + Send + Sync>,
}

pub use imp::Tray;

#[cfg(not(windows))]
mod imp {
    use super::Callbacks;

    /// There is no tray icon outside Windows.
    pub struct Tray;

    impl Tray {
        pub fn start(_cb: Callbacks) -> Option<Tray> {
            None
        }
        pub fn set_state(&self, _on: bool, _tip: &str) {}
        pub fn set_tip(&self, _tip: &str) {}
        pub fn notify(&self, _title: &str, _msg: &str) {}
        pub fn stop(&self) {}
    }
}

#[cfg(windows)]
mod imp {
    use super::Callbacks;
    use duallink_common::{wide, LockExt};
    use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering::Relaxed};
    use std::sync::{mpsc, Mutex, OnceLock};
    use std::time::Duration;
    use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, WPARAM};
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::Shell::{
        Shell_NotifyIconW, NIF_ICON, NIF_INFO, NIF_MESSAGE, NIF_TIP, NIIF_LARGE_ICON, NIIF_USER, NIM_ADD, NIM_DELETE, NIM_MODIFY,
        NOTIFYICONDATAW,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        AppendMenuW, CreateIconFromResourceEx, CreatePopupMenu, CreateWindowExW, DefWindowProcW, DestroyIcon, DestroyMenu,
        DestroyWindow, DispatchMessageW, GetCursorPos, GetMessageW, GetSystemMetrics, PostMessageW, PostQuitMessage,
        RegisterClassExW, RegisterWindowMessageW, SetForegroundWindow, SetMenuDefaultItem, TrackPopupMenu, TranslateMessage,
        MF_SEPARATOR, MF_STRING, MSG, SM_CXICON, SM_CXSMICON, TPM_NONOTIFY, TPM_RETURNCMD, TPM_RIGHTBUTTON, WM_APP, WM_CLOSE,
        WM_DESTROY, WM_ENDSESSION, WM_LBUTTONUP, WM_NULL, WM_QUERYENDSESSION, WM_RBUTTONUP, WM_USER, WNDCLASSEXW,
    };

    const WM_TRAY: u32 = WM_APP + 1; // our icon's mouse messages
    const WM_STOP: u32 = WM_APP + 2; // stop() from another thread
    const NIN_BALLOONUSERCLICK: u32 = WM_USER + 5;
    const CMD_OPEN: usize = 1;
    const CMD_TOGGLE: usize = 2;
    const CMD_QUIT: usize = 3;
    const ICON_ON: &[u8] = include_bytes!("../../assets/icon_on.ico");
    const ICON_OFF: &[u8] = include_bytes!("../../assets/icon_off.ico");

    struct Shared {
        cb: Callbacks,
        hwnd: AtomicIsize,
        on: AtomicBool,
        added: AtomicBool,
        /// Windows is shutting down: the tray's thread is busy answering that, don't wait for it.
        ending: AtomicBool,
        tip: Mutex<String>,
        icon_on: AtomicIsize,
        icon_off: AtomicIsize,
        icon_big: AtomicIsize,
        taskbar_created: AtomicIsize,
        ended: Mutex<Option<mpsc::Receiver<()>>>,
    }

    static SHARED: OnceLock<Shared> = OnceLock::new();

    pub struct Tray {
        shared: &'static Shared,
    }

    /// The picture in an .ico file that fits `size` pixels best, as an icon.
    fn load_icon(ico: &[u8], size: i32) -> isize {
        let count = u16::from_le_bytes([ico[4], ico[5]]) as usize;
        let mut best: Option<(i32, usize, usize)> = None; // (width, offset, length)
        for i in 0..count {
            let e = &ico[6 + 16 * i..22 + 16 * i];
            let w = if e[0] == 0 { 256 } else { e[0] as i32 };
            let len = u32::from_le_bytes([e[8], e[9], e[10], e[11]]) as usize;
            let off = u32::from_le_bytes([e[12], e[13], e[14], e[15]]) as usize;
            if off + len > ico.len() {
                continue;
            }
            let better = match best {
                None => true,
                Some((bw, _, _)) => (w >= size && (bw < size || w < bw)) || (w < size && bw < size && w > bw),
            };
            if better {
                best = Some((w, off, len));
            }
        }
        let Some((_, off, len)) = best else { return 0 };
        // SAFETY: the pointer and length describe one picture inside `ico`, which is static data.
        unsafe { CreateIconFromResourceEx(ico[off..].as_ptr(), len as u32, 1, 0x0003_0000, size, size, 0) as isize }
    }

    fn put(dst: &mut [u16], text: &str) {
        let max = dst.len() - 1;
        let mut n = 0;
        for unit in text.encode_utf16().take(max) {
            dst[n] = unit;
            n += 1;
        }
        dst[n] = 0;
    }

    impl Shared {
        fn hwnd(&self) -> HWND {
            self.hwnd.load(Relaxed) as HWND
        }

        fn nid(&self, flags: u32) -> NOTIFYICONDATAW {
            // SAFETY: NOTIFYICONDATAW is plain data; all zero is a valid value.
            let mut nid: NOTIFYICONDATAW = unsafe { std::mem::zeroed() };
            nid.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
            nid.hWnd = self.hwnd();
            nid.uID = 1;
            nid.uFlags = flags;
            nid.hIcon = if self.on.load(Relaxed) { self.icon_on.load(Relaxed) } else { self.icon_off.load(Relaxed) } as _;
            put(&mut nid.szTip, &self.tip.lock_ok());
            nid
        }

        fn shell(&self, message: u32, nid: &NOTIFYICONDATAW) -> bool {
            // SAFETY: nid is a fully initialised structure of the size it states.
            unsafe { Shell_NotifyIconW(message, nid) != 0 }
        }

        fn add(&self) {
            let mut nid = self.nid(NIF_MESSAGE | NIF_ICON | NIF_TIP);
            nid.uCallbackMessage = WM_TRAY;
            let ok = self.shell(NIM_ADD, &nid) || self.shell(NIM_MODIFY, &nid);
            self.added.store(ok, Relaxed);
        }

        fn modify(&self, flags: u32) {
            if self.hwnd.load(Relaxed) == 0 {
                return;
            }
            if !self.shell(NIM_MODIFY, &self.nid(flags)) && !self.added.load(Relaxed) {
                self.add(); // Explorer wasn't ready when we started
            }
        }
    }

    /// Run a callback off the tray's thread, so the icon stays responsive.
    fn later(f: &'static (dyn Fn() + Send + Sync)) {
        std::thread::spawn(f);
    }

    unsafe fn menu(s: &'static Shared, hwnd: HWND) {
        let menu = CreatePopupMenu();
        if menu.is_null() {
            return;
        }
        let toggle = wide(if (s.cb.is_on)() { "Turn off" } else { "Turn on" });
        AppendMenuW(menu, MF_STRING, CMD_OPEN, wide("Open DualLink").as_ptr());
        AppendMenuW(menu, MF_STRING, CMD_TOGGLE, toggle.as_ptr());
        AppendMenuW(menu, MF_SEPARATOR, 0, std::ptr::null());
        AppendMenuW(menu, MF_STRING, CMD_QUIT, wide("Quit").as_ptr());
        SetMenuDefaultItem(menu, CMD_OPEN as u32, 0);
        let mut pt = POINT { x: 0, y: 0 };
        GetCursorPos(&mut pt);
        SetForegroundWindow(hwnd); // so the menu closes when you click elsewhere
        let cmd = TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, hwnd, std::ptr::null());
        PostMessageW(hwnd, WM_NULL, 0, 0);
        DestroyMenu(menu);
        match cmd as usize {
            CMD_OPEN => later(&*s.cb.open),
            CMD_TOGGLE => later(&*s.cb.toggle),
            CMD_QUIT => later(&*s.cb.quit),
            _ => {}
        }
    }

    unsafe extern "system" fn wndproc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
        let Some(s) = SHARED.get() else { return DefWindowProcW(hwnd, msg, wparam, lparam) };
        match msg {
            WM_TRAY => {
                match (lparam & 0xffff) as u32 {
                    WM_LBUTTONUP | NIN_BALLOONUSERCLICK => later(&*s.cb.open),
                    WM_RBUTTONUP => menu(s, hwnd),
                    _ => {}
                }
                0
            }
            WM_STOP => {
                s.shell(NIM_DELETE, &s.nid(0));
                DestroyWindow(hwnd);
                0
            }
            WM_CLOSE => {
                later(&*s.cb.quit); // someone asked DualLink to close: quit cleanly
                0
            }
            WM_QUERYENDSESSION => 1,
            WM_ENDSESSION if wparam != 0 => {
                // Windows is signing out / shutting down: give qBittorrent its settings back, but don't hang
                s.ending.store(true, Relaxed);
                let (tx, rx) = mpsc::channel();
                std::thread::spawn(move || {
                    (s.cb.quit)();
                    let _ = tx.send(());
                });
                let _ = rx.recv_timeout(Duration::from_secs(4));
                0
            }
            WM_DESTROY => {
                s.hwnd.store(0, Relaxed);
                PostQuitMessage(0);
                0
            }
            _ if msg != 0 && msg as isize == s.taskbar_created.load(Relaxed) => {
                s.added.store(false, Relaxed); // Explorer restarted: put the icon back
                s.add();
                0
            }
            _ => DefWindowProcW(hwnd, msg, wparam, lparam),
        }
    }

    /// The tray's own thread: a hidden window that receives the icon's messages.
    fn run(s: &'static Shared, ready: mpsc::Sender<bool>, ended: mpsc::Sender<()>) {
        // SAFETY: standard window-class registration and message loop; every pointer passed is valid for
        // the call, and the window procedure only touches `SHARED`, which lives for the whole program.
        unsafe {
            let small = GetSystemMetrics(SM_CXSMICON).max(16);
            s.icon_on.store(load_icon(ICON_ON, small), Relaxed);
            s.icon_off.store(load_icon(ICON_OFF, small), Relaxed);
            s.icon_big.store(load_icon(ICON_ON, GetSystemMetrics(SM_CXICON).max(32)), Relaxed);
            s.taskbar_created.store(RegisterWindowMessageW(wide("TaskbarCreated").as_ptr()) as isize, Relaxed);
            let hinst = GetModuleHandleW(std::ptr::null());
            let class = wide("DualLinkTrayWindow");
            let mut wc: WNDCLASSEXW = std::mem::zeroed();
            wc.cbSize = std::mem::size_of::<WNDCLASSEXW>() as u32;
            wc.lpfnWndProc = Some(wndproc);
            wc.hInstance = hinst;
            wc.lpszClassName = class.as_ptr();
            RegisterClassExW(&wc);
            // a hidden top-level window (not message-only, so it hears "TaskbarCreated" when Explorer restarts)
            let hwnd = CreateWindowExW(
                0, class.as_ptr(), wide("DualLink").as_ptr(), 0, 0, 0, 0, 0, std::ptr::null_mut(), std::ptr::null_mut(), hinst,
                std::ptr::null(),
            );
            if hwnd.is_null() {
                let _ = ready.send(false);
                return;
            }
            s.hwnd.store(hwnd as isize, Relaxed);
            s.add();
            let _ = ready.send(true);
            let mut msg: MSG = std::mem::zeroed();
            while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
            for icon in [&s.icon_on, &s.icon_off, &s.icon_big] {
                DestroyIcon(icon.swap(0, Relaxed) as _);
            }
        }
        let _ = ended.send(());
    }

    impl Tray {
        /// Show the icon. None when it can't be created (DualLink then works without one).
        pub fn start(cb: Callbacks) -> Option<Tray> {
            let (ready_tx, ready_rx) = mpsc::channel();
            let (ended_tx, ended_rx) = mpsc::channel();
            let fresh = Shared {
                cb,
                hwnd: AtomicIsize::new(0),
                on: AtomicBool::new(false),
                added: AtomicBool::new(false),
                ending: AtomicBool::new(false),
                tip: Mutex::new("DualLink - off".into()),
                icon_on: AtomicIsize::new(0),
                icon_off: AtomicIsize::new(0),
                icon_big: AtomicIsize::new(0),
                taskbar_created: AtomicIsize::new(0),
                ended: Mutex::new(Some(ended_rx)),
            };
            if SHARED.set(fresh).is_err() {
                return None; // one tray icon per program
            }
            let shared = SHARED.get()?;
            std::thread::Builder::new().name("tray".into()).spawn(move || run(shared, ready_tx, ended_tx)).ok()?;
            match ready_rx.recv_timeout(Duration::from_secs(5)) {
                Ok(true) => Some(Tray { shared }),
                _ => None,
            }
        }

        pub fn set_state(&self, on: bool, tip: &str) {
            self.shared.on.store(on, Relaxed);
            *self.shared.tip.lock_ok() = tip.to_string();
            self.shared.modify(NIF_ICON | NIF_TIP);
        }

        pub fn set_tip(&self, tip: &str) {
            *self.shared.tip.lock_ok() = tip.to_string();
            self.shared.modify(NIF_TIP);
        }

        pub fn notify(&self, title: &str, msg: &str) {
            let s = self.shared;
            if s.hwnd.load(Relaxed) == 0 {
                return;
            }
            let mut nid = s.nid(NIF_INFO);
            put(&mut nid.szInfoTitle, title);
            put(&mut nid.szInfo, if msg.is_empty() { " " } else { msg });
            nid.dwInfoFlags = NIIF_USER | NIIF_LARGE_ICON;
            nid.hBalloonIcon = s.icon_big.load(Relaxed) as _;
            s.shell(NIM_MODIFY, &nid);
        }

        /// Remove the icon and end the tray's thread.
        pub fn stop(&self) {
            let hwnd = self.shared.hwnd();
            if hwnd.is_null() {
                return;
            }
            // SAFETY: posting a message to our own window.
            unsafe {
                PostMessageW(hwnd, WM_STOP, 0, 0);
            }
            if self.shared.ending.load(Relaxed) {
                return;
            }
            if let Some(ended) = self.shared.ended.lock_ok().take() {
                let _ = ended.recv_timeout(Duration::from_secs(3));
            }
        }
    }
}
