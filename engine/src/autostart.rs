//! "Start with Windows": an entry under the current user's Run key.

#[cfg(windows)]
pub fn set(enabled: bool) -> Result<(), String> {
    use duallink_common::wide;
    use windows_sys::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_SUCCESS};
    use windows_sys::Win32::System::Registry::{
        RegCloseKey, RegDeleteValueW, RegOpenKeyExW, RegSetValueExW, HKEY, HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ,
    };

    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let command = wide(&format!("\"{}\" --minimized", exe.display()));
    let (key, name) = (wide("Software\\Microsoft\\Windows\\CurrentVersion\\Run"), wide("DualLink"));
    // SAFETY: every string is zero-terminated and outlives the calls; the key is closed again.
    unsafe {
        let mut h: HKEY = std::ptr::null_mut();
        let r = RegOpenKeyExW(HKEY_CURRENT_USER, key.as_ptr(), 0, KEY_SET_VALUE, &mut h);
        if r != ERROR_SUCCESS {
            return Err(format!("Windows error {r}"));
        }
        let r = if enabled {
            RegSetValueExW(h, name.as_ptr(), 0, REG_SZ, command.as_ptr().cast(), (command.len() * 2) as u32)
        } else {
            match RegDeleteValueW(h, name.as_ptr()) {
                ERROR_FILE_NOT_FOUND => ERROR_SUCCESS,
                r => r,
            }
        };
        RegCloseKey(h);
        if r == ERROR_SUCCESS { Ok(()) } else { Err(format!("Windows error {r}")) }
    }
}

#[cfg(not(windows))]
pub fn set(_enabled: bool) -> Result<(), String> {
    Ok(())
}
