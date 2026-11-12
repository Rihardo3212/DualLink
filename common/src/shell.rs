//! Small desktop helpers: the clipboard and opening a web page.

/// Open one of DualLink's own GitHub pages in the browser. Anything else is refused.
pub fn open_github_url(url: &str) -> bool {
    if !url.starts_with("https://github.com/") {
        return false;
    }
    imp::open(url)
}

/// Put text on the clipboard.
pub fn copy_text(text: &str) -> bool {
    imp::copy(text)
}

/// Show a window of ours again if it was minimised and put it in front of the others.
pub fn bring_to_front(hwnd: isize) {
    imp::front(hwnd)
}

#[cfg(windows)]
mod imp {
    use crate::wide;
    use windows_sys::Win32::UI::WindowsAndMessaging::{IsIconic, SetForegroundWindow, ShowWindow, SW_RESTORE};

    pub fn front(hwnd: isize) {
        // SAFETY: a window handle of this process; the calls do nothing if it is no longer valid.
        unsafe {
            if IsIconic(hwnd as _) != 0 {
                ShowWindow(hwnd as _, SW_RESTORE);
            }
            SetForegroundWindow(hwnd as _);
        }
    }

    use windows_sys::Win32::Foundation::GlobalFree;
    use windows_sys::Win32::System::DataExchange::{CloseClipboard, EmptyClipboard, OpenClipboard, SetClipboardData};
    use windows_sys::Win32::System::Memory::{GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE};
    use windows_sys::Win32::UI::Shell::ShellExecuteW;

    const CF_UNICODETEXT: u32 = 13;
    const SW_SHOWNORMAL: i32 = 1;

    pub fn open(url: &str) -> bool {
        let (verb, target) = (wide("open"), wide(url));
        // SAFETY: both strings are valid and zero-terminated for the length of the call.
        let r = unsafe {
            ShellExecuteW(std::ptr::null_mut(), verb.as_ptr(), target.as_ptr(), std::ptr::null(), std::ptr::null(), SW_SHOWNORMAL)
        };
        r as isize > 32
    }

    pub fn copy(text: &str) -> bool {
        let data = wide(text);
        let bytes = data.len() * 2;
        // SAFETY: the usual clipboard sequence. The memory block is sized for `data`, filled while locked,
        // and handed to the clipboard (which then owns it) or freed if that fails.
        unsafe {
            if OpenClipboard(std::ptr::null_mut()) == 0 {
                return false;
            }
            let mut ok = false;
            if EmptyClipboard() != 0 {
                let mem = GlobalAlloc(GMEM_MOVEABLE, bytes);
                if !mem.is_null() {
                    let p = GlobalLock(mem) as *mut u16;
                    if !p.is_null() {
                        std::ptr::copy_nonoverlapping(data.as_ptr(), p, data.len());
                        GlobalUnlock(mem);
                        ok = !SetClipboardData(CF_UNICODETEXT, mem as _).is_null();
                    }
                    if !ok {
                        GlobalFree(mem);
                    }
                }
            }
            CloseClipboard();
            ok
        }
    }
}

#[cfg(not(windows))]
mod imp {
    pub fn front(_hwnd: isize) {}
    pub fn open(_url: &str) -> bool {
        false
    }
    pub fn copy(_text: &str) -> bool {
        false
    }
}
