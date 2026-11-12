//! Things DualLink's engine (DualLink.exe) and window (DualLinkWindow.exe) share.
pub mod instance;
pub mod shell;

use std::path::PathBuf;
use std::sync::{Mutex, MutexGuard};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const TOKEN_HEADER: &str = "X-DualLink-Token";
pub const WINDOW_EXE: &str = if cfg!(windows) { "DualLinkWindow.exe" } else { "DualLinkWindow" };
pub const ENGINE_EXE: &str = if cfg!(windows) { "DualLink.exe" } else { "DualLink" };

/// `%APPDATA%\DualLink`: settings, and engine.json while DualLink runs.
pub fn config_dir() -> PathBuf {
    let base = std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))
        .unwrap_or_else(std::env::temp_dir);
    let dir = base.join("DualLink");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

/// Where the running engine writes its control API port and token.
pub fn engine_file() -> PathBuf {
    config_dir().join("engine.json")
}

/// Lock a mutex even if another thread panicked while holding it (the data is still usable for us).
pub trait LockExt<T> {
    fn lock_ok(&self) -> MutexGuard<'_, T>;
}

impl<T> LockExt<T> for Mutex<T> {
    fn lock_ok(&self) -> MutexGuard<'_, T> {
        self.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// UTF-16, zero-terminated, for Windows API calls.
pub fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// "1.5 MiB/s" - used by the tray tooltip, the traffic check and the window.
pub fn fmt_rate(mut b: f64) -> String {
    const UNITS: [&str; 4] = ["B/s", "KiB/s", "MiB/s", "GiB/s"];
    let mut i = 0;
    while b >= 1024.0 && i < UNITS.len() - 1 {
        b /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{b:.0} {}", UNITS[i])
    } else {
        format!("{b:.1} {}", UNITS[i])
    }
}

/// Seconds since 1970.
pub fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rates() {
        assert_eq!(fmt_rate(0.0), "0 B/s");
        assert_eq!(fmt_rate(1536.0), "1.5 KiB/s");
        assert_eq!(fmt_rate(3.0 * 1024.0 * 1024.0), "3.0 MiB/s");
        assert_eq!(fmt_rate(5e12), "4656.6 GiB/s");
    }
}
