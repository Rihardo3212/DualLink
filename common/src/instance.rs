//! One engine and one window at a time.
//!
//! Starting DualLink again just asks the running engine to show its window; opening the window while it's
//! already open brings it to the front. On other systems (development only) these do nothing.

pub const ENGINE_MUTEX: &str = "Local\\DualLinkSingleInstance";
pub const ENGINE_SHOW: &str = "Local\\DualLinkShowWindow"; // "please open the window" -> engine
pub const WINDOW_MUTEX: &str = "Local\\DualLinkWindow";
pub const WINDOW_SHOW: &str = "Local\\DualLinkWindowFront"; // "come to the front" -> window

#[cfg(windows)]
mod imp {
    use crate::wide;
    use std::time::{Duration, Instant};
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, WAIT_OBJECT_0};
    use windows_sys::Win32::System::Threading::{
        CreateEventW, CreateMutexW, OpenEventW, OpenMutexW, OpenProcess, SetEvent, WaitForSingleObject, INFINITE,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::AllowSetForegroundWindow;

    const SYNCHRONIZE: u32 = 0x0010_0000;
    const EVENT_MODIFY_STATE: u32 = 0x0002;
    const ASFW_ANY: u32 = u32::MAX;

    pub fn acquire(name: &str, wait: Duration) -> bool {
        let w = wide(name);
        let end = Instant::now() + wait;
        loop {
            // SAFETY: plain Win32 calls with a valid, zero-terminated name. The handle is kept open on
            // purpose for the life of the process (that is what holds the mutex).
            unsafe {
                let h = CreateMutexW(std::ptr::null(), 0, w.as_ptr());
                if GetLastError() != ERROR_ALREADY_EXISTS {
                    return true;
                }
                if !h.is_null() {
                    CloseHandle(h);
                }
            }
            if Instant::now() >= end {
                return false;
            }
            std::thread::sleep(Duration::from_millis(500));
        }
    }

    pub fn held(name: &str) -> bool {
        let w = wide(name);
        // SAFETY: valid name; the handle is closed again right away.
        unsafe {
            let h = OpenMutexW(SYNCHRONIZE, 0, w.as_ptr());
            if h.is_null() {
                return false;
            }
            CloseHandle(h);
            true
        }
    }

    pub fn signal(name: &str) -> bool {
        let w = wide(name);
        // SAFETY: valid name; the handle is closed again right away.
        unsafe {
            AllowSetForegroundWindow(ASFW_ANY); // let the other process come to the front
            let h = OpenEventW(EVENT_MODIFY_STATE, 0, w.as_ptr());
            if h.is_null() {
                return false;
            }
            SetEvent(h);
            CloseHandle(h);
            true
        }
    }

    pub fn allow_foreground(pid: u32) {
        // SAFETY: no pointers involved.
        unsafe {
            AllowSetForegroundWindow(pid);
        }
    }

    pub fn listen(name: &str, callback: impl Fn() + Send + 'static) {
        let w = wide(name);
        // SAFETY: valid name. The event handle lives as long as the process.
        let h = unsafe { CreateEventW(std::ptr::null(), 0, 0, w.as_ptr()) } as usize; // auto-reset
        if h == 0 {
            return;
        }
        let _ = std::thread::Builder::new().name("show-listener".into()).spawn(move || loop {
            // SAFETY: h is a valid event handle that is never closed.
            if unsafe { WaitForSingleObject(h as _, INFINITE) } == WAIT_OBJECT_0 {
                callback();
            } else {
                std::thread::sleep(Duration::from_secs(1));
            }
        });
    }

    pub fn on_exit(pid: u32, callback: impl FnOnce() + Send + 'static) {
        // SAFETY: OpenProcess with SYNCHRONIZE only lets us wait for the process to end.
        let h = unsafe { OpenProcess(SYNCHRONIZE, 0, pid) } as usize;
        if h == 0 {
            return; // already gone, or not ours to watch
        }
        let _ = std::thread::Builder::new().name("engine-watch".into()).spawn(move || {
            // SAFETY: h is a valid process handle owned by this thread.
            unsafe {
                WaitForSingleObject(h as _, INFINITE);
                CloseHandle(h as _);
            }
            callback();
        });
    }
}

#[cfg(not(windows))]
mod imp {
    use std::time::Duration;

    pub fn acquire(_name: &str, _wait: Duration) -> bool {
        true
    }
    pub fn held(_name: &str) -> bool {
        false
    }
    pub fn signal(_name: &str) -> bool {
        false
    }
    pub fn allow_foreground(_pid: u32) {}
    pub fn listen(_name: &str, _callback: impl Fn() + Send + 'static) {}
    pub fn on_exit(_pid: u32, _callback: impl FnOnce() + Send + 'static) {}
}

/// True if nobody else holds `name`; this process then holds it until it ends. With `wait`, keep trying
/// (used right after an update restart, while the old engine is still closing).
pub fn acquire(name: &str, wait: std::time::Duration) -> bool {
    imp::acquire(name, wait)
}

/// True if some process holds the named mutex.
pub fn held(name: &str) -> bool {
    imp::held(name)
}

/// Ask the process listening on `name` to show its window.
pub fn signal(name: &str) -> bool {
    imp::signal(name)
}

/// Let process `pid` (the window we just started) come to the front.
pub fn allow_foreground(pid: u32) {
    imp::allow_foreground(pid)
}

/// Call `callback` whenever another process signals `name`.
pub fn listen(name: &str, callback: impl Fn() + Send + 'static) {
    imp::listen(name, callback)
}

/// Call `callback` once process `pid` has ended (the window uses this to close along with the engine).
pub fn on_exit(pid: u32, callback: impl FnOnce() + Send + 'static) {
    imp::on_exit(pid, callback)
}
