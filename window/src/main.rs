//! DualLink's window. It runs only while it is open and asks the engine (DualLink.exe) for everything.
//!
//!     (none)                 the normal window, talking to the running DualLink engine
//!     --demo                 made-up data instead of the engine (for screenshots and trying things out)
//!     --screenshots [dir]    open every screen, save a PNG of each into dir, then close
//!     --theme light|dark     force a theme (default: follow Windows)
#![cfg_attr(windows, windows_subsystem = "windows")]

slint::include_modules!();

mod app;
mod demo;
mod engine;
mod fmt;
mod guides;
mod shots;

use app::{App, Opts};
use duallink_common::{instance, shell};
use engine::Engine;
use slint::ComponentHandle;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::Arc;
use std::time::Duration;

static ENGINE_GONE: AtomicBool = AtomicBool::new(false);

fn parse_args() -> Opts {
    let mut opts = Opts { demo: false, shots: None, dark: None };
    let mut args = std::env::args().skip(1).peekable();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--demo" => opts.demo = true,
            "--screenshots" => {
                let dir = args.next_if(|a| !a.starts_with("--")).unwrap_or_else(|| "shots".into());
                opts.shots = Some(PathBuf::from(dir));
            }
            "--theme" => {
                opts.dark = match args.next().as_deref() {
                    Some("dark") => Some(true),
                    Some("light") => Some(false),
                    _ => None,
                }
            }
            _ => {}
        }
    }
    opts
}

/// Un-minimise the window and put it in front of the others.
fn bring_to_front(ui: &MainWindow) {
    use raw_window_handle::{HasWindowHandle, RawWindowHandle};
    ui.window().set_minimized(false);
    let _ = ui.show();
    if let Ok(RawWindowHandle::Win32(h)) = ui.window().window_handle().window_handle().map(|h| h.as_raw()) {
        shell::bring_to_front(h.hwnd.get());
    }
}

fn main() {
    let opts = parse_args();
    let normal = !opts.demo && opts.shots.is_none();
    let engine: Arc<dyn Engine> = if opts.demo {
        Arc::new(demo::DemoEngine::new())
    } else {
        match engine::HttpEngine::find() {
            Some(found) => Arc::new(found),
            // DualLink isn't running: start it - it opens a window by itself
            None => std::process::exit(if engine::start_engine() { 0 } else { 1 }),
        }
    };
    if normal && !instance::acquire(instance::WINDOW_MUTEX, Duration::ZERO) {
        instance::signal(instance::WINDOW_SHOW); // already open: bring it to the front
        return;
    }

    let ui = match MainWindow::new() {
        Ok(ui) => ui,
        Err(_) => std::process::exit(2),
    };
    let app = App::start(ui, engine.clone(), opts);
    if normal {
        let weak = app.ui.as_weak();
        instance::listen(instance::WINDOW_SHOW, move || {
            let _ = weak.upgrade_in_event_loop(|ui| bring_to_front(&ui));
        });
    }
    if let Some(pid) = engine.pid() {
        instance::on_exit(pid, || {
            ENGINE_GONE.store(true, Relaxed); // DualLink was quit from the tray: close with it
            let _ = slint::quit_event_loop();
        });
    }
    if app.ui.run().is_err() {
        std::process::exit(2);
    }

    app.closing();
    if normal && !ENGINE_GONE.load(Relaxed) {
        let pending = app.take_pending();
        if !pending.is_empty() {
            let _ = engine.call("save_settings", vec![serde_json::Value::Object(pending)]);
        }
        // lets the engine show its "still running in the tray" hint (or quit, if that's the setting)
        let _ = engine.call("window_closed", vec![]);
    }
}
