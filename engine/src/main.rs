//! DualLink - use Ethernet and a phone hotspot together for faster downloads.
//!
//!     DualLink.exe              the engine (proxy + tray icon); opens the window (DualLinkWindow.exe)
//!     DualLink.exe --minimized  the engine only (Start with Windows)
//!     DualLink.exe --updated    started by the installer after an in-app update
//!     DualLink.exe --cleanup    run by the uninstaller
#![cfg_attr(windows, windows_subsystem = "windows")]

mod api;
mod autostart;
mod controller;
#[cfg(test)]
mod controller_tests;
mod diag;
mod http;
mod net;
mod proxy;
mod qbt;
mod service;
mod speedtest;
mod tray;
mod updates;

use duallink_common::{config_dir, engine_file, instance};
use std::time::Duration;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let has = |flag: &str| args.iter().any(|a| a == flag);
    let cfg_path = config_dir().join("config.json");
    if has("--cleanup") {
        controller::cleanup(&cfg_path); // run by the uninstaller, no window
        return;
    }
    let updated = has("--updated");
    // right after an update the old engine may still be closing: give it a moment
    if !instance::acquire(instance::ENGINE_MUTEX, Duration::from_secs(if updated { 15 } else { 0 })) {
        instance::signal(instance::ENGINE_SHOW); // already running: open its window / bring it to the front
        return;
    }
    let ctl = controller::Controller::new(cfg_path, controller::Deps::default());
    let opts = service::Options { minimized: has("--minimized"), updated, info_path: engine_file(), window_exe: service::find_window(), tray: true };
    let engine = service::Engine::new(ctl, opts);
    if engine.start().is_err() {
        std::process::exit(1);
    }
    engine.wait();
}
