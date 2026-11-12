//! DualLink's background engine: the proxy and its controller, the tray icon and the control API.
//!
//! It never loads any window libraries, so it stays small while DualLink sits in the tray. The window
//! (DualLinkWindow.exe) is a separate program that exists only while it's open; it asks the engine for
//! everything over the control API.

use crate::api::{ApiError, ControlServer};
use crate::controller::{Controller, Hooks};
use crate::tray::{Callbacks, Tray};
use crate::autostart;
use duallink_common::{fmt_rate, instance, LockExt, VERSION, WINDOW_EXE};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::time::{Duration, Instant};

pub struct Options {
    /// Start in the tray, without opening the window ("Start with Windows").
    pub minimized: bool,
    /// This start is the restart after an in-app update.
    pub updated: bool,
    /// Where to write engine.json.
    pub info_path: PathBuf,
    /// The window program; None when it can't be found.
    pub window_exe: Option<PathBuf>,
    /// Show the tray icon (tests run without one).
    pub tray: bool,
}

/// DualLinkWindow.exe next to DualLink.exe (DUALLINK_WINDOW can point at another copy).
pub fn find_window() -> Option<PathBuf> {
    let path = match std::env::var_os("DUALLINK_WINDOW") {
        Some(p) => PathBuf::from(p),
        None => std::env::current_exe().ok()?.parent()?.join(WINDOW_EXE),
    };
    path.is_file().then_some(path)
}

pub struct Engine {
    pub ctl: Arc<Controller>,
    opts: Options,
    server: OnceLock<Arc<ControlServer>>,
    tray: OnceLock<Tray>,
    /// The window program while it's open.
    window: Mutex<Option<Child>>,
    quitting: AtomicBool,
    stopped: (Mutex<bool>, Condvar),
}

impl Engine {
    pub fn new(ctl: Arc<Controller>, opts: Options) -> Arc<Engine> {
        ctl.just_updated.store(opts.updated, Relaxed);
        let engine = Arc::new(Engine {
            ctl,
            opts,
            server: OnceLock::new(),
            tray: OnceLock::new(),
            window: Mutex::new(None),
            quitting: AtomicBool::new(false),
            stopped: (Mutex::new(false), Condvar::new()),
        });
        engine.ctl.set_hooks(engine.clone());
        engine
    }

    // ---- the control API ------------------------------------------------------------------- //
    fn request(self: &Arc<Self>, name: &str, args: &[Value]) -> Result<Value, ApiError> {
        let ctl = &self.ctl;
        let init = || {
            let adapters = ctl.adapters(); // also fills in a first guess of the connections
            json!({"settings": ctl.public_settings(), "adapters": adapters})
        };
        Ok(match name {
            "ping" => json!({
                "version": VERSION, "pid": std::process::id(), "window": self.window_open(),
                "running": ctl.running(), "tray": self.tray.get().is_some(),
            }),
            "open_window" => self.open_window(),
            "window_closed" => self.window_closed(),
            "quit" => self.quit_soon(),
            "get_init" | "refresh_adapters" => init(),
            "status" => ctl.status(),
            "save_settings" => {
                let before = ctl.cfg().autostart;
                match ctl.update_settings(args.first().unwrap_or(&Value::Null)) {
                    Err(error) => json!({"settings": ctl.public_settings(), "error": error}),
                    Ok(settings) => {
                        let now = ctl.cfg().autostart;
                        let error = (now != before)
                            .then(|| autostart::set(now).err())
                            .flatten()
                            .map(|e| format!("Couldn't change 'Start with Windows': {e}"));
                        json!({"settings": settings, "error": error})
                    }
                }
            }
            "power" => {
                if args.first().and_then(Value::as_bool).unwrap_or(false) { ctl.power_on() } else { ctl.power_off() }
            }
            "retry_qbt" => ctl.retry_qbt(),
            "test_qbt" => ctl.test_qbt(),
            "start_speed_test" => ctl.start_speed_test(),
            "check_updates" => ctl.check_updates_now(),
            "start_update" => ctl.start_update(),
            "traffic_check" => ctl.traffic_check(),
            _ => return Err(ApiError::Unknown),
        })
    }

    // ---- the window ------------------------------------------------------------------------ //
    fn window_open(&self) -> bool {
        self.window.lock_ok().as_mut().is_some_and(|w| matches!(w.try_wait(), Ok(None)))
    }

    /// Open the window, or bring it to the front if it's already open.
    pub fn open_window(self: &Arc<Self>) -> Value {
        if self.quitting.load(Relaxed) {
            return json!({"ok": false});
        }
        let mut window = self.window.lock_ok();
        let open = window.as_mut().is_some_and(|w| matches!(w.try_wait(), Ok(None)));
        if open || instance::held(instance::WINDOW_MUTEX) {
            instance::signal(instance::WINDOW_SHOW);
            return json!({"ok": true, "opened": false});
        }
        let Some(exe) = &self.opts.window_exe else {
            self.notify("DualLink's window is missing", "Reinstall DualLink from its GitHub Releases page.");
            return json!({"ok": false, "message": "DualLink's window is missing."});
        };
        match std::process::Command::new(exe).spawn() {
            Ok(child) => {
                instance::allow_foreground(child.id());
                let pid = child.id();
                *window = Some(child);
                drop(window);
                let me = self.clone();
                std::thread::spawn(move || me.watch_window(pid));
                json!({"ok": true, "opened": true})
            }
            Err(e) => {
                self.notify("DualLink couldn't open its window", &e.to_string());
                json!({"ok": false, "message": e.to_string()})
            }
        }
    }

    /// Wait for the window to close; say so if it crashed while starting.
    fn watch_window(&self, pid: u32) {
        let started = Instant::now();
        loop {
            std::thread::sleep(Duration::from_millis(400));
            let mut window = self.window.lock_ok();
            let Some(child) = window.as_mut().filter(|c| c.id() == pid) else { return }; // closed by us
            match child.try_wait() {
                Ok(None) => continue,
                Ok(Some(status)) => {
                    *window = None;
                    drop(window);
                    if !status.success() && !self.quitting.load(Relaxed) && started.elapsed() < Duration::from_secs(10) {
                        // DualLink itself keeps working from the tray
                        self.notify(
                            "DualLink's window couldn't start",
                            "Downloads keep working. Try opening it again from the tray icon, or reinstall DualLink.",
                        );
                    }
                    return;
                }
                Err(_) => {
                    *window = None;
                    return;
                }
            }
        }
    }

    /// The window tells us the user closed it.
    fn window_closed(self: &Arc<Self>) -> Value {
        let (tray, hinted) = {
            let cfg = self.ctl.cfg();
            (cfg.tray, cfg.tray_hint_shown)
        };
        if !tray || self.tray.get().is_none() {
            return self.quit_soon(); // no tray icon to come back to: closing the window quits
        }
        if !hinted {
            self.ctl.cfg().tray_hint_shown = true;
            self.ctl.save();
            self.notify("DualLink is still running", "It's in the system tray. Right-click the icon to quit.");
        }
        json!({"quitting": false})
    }

    fn close_window(&self) {
        if let Some(mut child) = self.window.lock_ok().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    // ---- the tray icon --------------------------------------------------------------------- //
    pub fn tooltip(&self) -> String {
        if !self.ctl.running() {
            return "DualLink - off".into();
        }
        let speeds = self.ctl.speeds();
        let parts: Vec<String> = speeds.iter().map(|(label, sp)| format!("{label} {}", fmt_rate(*sp))).collect();
        let total: f64 = speeds.iter().map(|(_, sp)| sp).sum();
        format!("DualLink - {}  ({})", fmt_rate(total), parts.join(", ")).chars().take(127).collect()
    }

    fn toggle(self: &Arc<Self>) {
        if self.ctl.running() {
            self.ctl.power_off();
            return;
        }
        let res = self.ctl.power_on();
        if res["ok"] != true {
            self.notify("DualLink couldn't start", res["message"].as_str().unwrap_or(""));
            self.open_window();
        }
    }

    // ---- start / stop ---------------------------------------------------------------------- //
    fn quit_soon(self: &Arc<Self>) -> Value {
        let me = self.clone();
        std::thread::spawn(move || me.quit()); // answer the request first
        json!({"quitting": true})
    }

    pub fn quit(&self) {
        if self.quitting.swap(true, Relaxed) {
            return;
        }
        self.close_window();
        self.ctl.shutdown(); // gives qBittorrent its own settings back
        if let Some(server) = self.server.get() {
            server.unpublish();
            server.stop();
        }
        if let Some(tray) = self.tray.get() {
            tray.stop();
        }
        *self.stopped.0.lock_ok() = true;
        self.stopped.1.notify_all();
    }

    /// Everything except waiting; returns once DualLink is ready.
    pub fn start(self: &Arc<Self>) -> Result<(), String> {
        let me = self.clone();
        let server = ControlServer::start(Arc::new(move |name: &str, args: &[Value]| me.request(name, args)))?;
        server.publish(&self.opts.info_path);
        let _ = self.server.set(server);
        let me = self.clone();
        instance::listen(instance::ENGINE_SHOW, move || {
            me.open_window(); // starting DualLink again opens the window
        });
        if !self.opts.minimized {
            self.open_window(); // the window starts while the rest gets ready
        }
        let (open, toggle, quit, is_on) = (self.clone(), self.clone(), self.clone(), self.clone());
        let tray = self.opts.tray.then(|| Tray::start(Callbacks {
            open: Box::new(move || {
                open.open_window();
            }),
            toggle: Box::new(move || toggle.toggle()),
            quit: Box::new(move || quit.quit()),
            is_on: Box::new(move || is_on.ctl.running()),
        }));
        if let Some(tray) = tray.flatten() {
            let _ = self.tray.set(tray);
        }
        self.changed();
        if self.opts.updated {
            self.notify("DualLink updated", &format!("You're now on version {VERSION}."));
        }
        self.ctl.start_loops();
        let ctl = self.ctl.clone();
        std::thread::spawn(move || ctl.startup()); // "turn on when DualLink starts"
        self.ctl.start_update_checks();
        let me = Arc::downgrade(self);
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_secs(2));
            let Some(me) = me.upgrade() else { return };
            if let (Some(tray), true) = (me.tray.get(), me.ctl.running()) {
                tray.set_tip(&me.tooltip());
            }
        });
        Ok(())
    }

    /// Wait until DualLink quits.
    pub fn wait(&self) {
        let mut stopped = self.stopped.0.lock_ok();
        while !*stopped {
            stopped = self.stopped.1.wait(stopped).unwrap_or_else(|e| e.into_inner());
        }
    }
}

impl Hooks for Engine {
    fn notify(&self, title: &str, msg: &str) {
        if let Some(tray) = self.tray.get() {
            tray.notify(title, msg);
        }
    }

    fn changed(&self) {
        if let Some(tray) = self.tray.get() {
            tray.set_state(self.ctl.running(), &self.tooltip());
        }
    }

    /// Close completely so the installer can replace the files (the new version starts by itself).
    fn quit_for_update(&self) {
        self.quitting.store(true, Relaxed);
        self.close_window();
        self.ctl.proxy.stop();
        if let Some(server) = self.server.get() {
            server.unpublish();
        }
        if let Some(tray) = self.tray.get() {
            tray.stop();
        }
        std::thread::sleep(Duration::from_millis(500));
        std::process::exit(0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::controller::Deps;
    use duallink_common::TOKEN_HEADER;

    struct Rig {
        engine: Arc<Engine>,
        dir: PathBuf,
        url: String,
        token: String,
    }

    impl Drop for Rig {
        fn drop(&mut self) {
            self.engine.quit();
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn rig(tag: &str) -> Rig {
        let dir = std::env::temp_dir().join(format!("duallink-engine-{}-{tag}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let deps = Deps { scan: Box::new(Vec::new), ..Deps::default() };
        let ctl = Controller::new(dir.join("config.json"), deps);
        let opts = Options { minimized: true, updated: false, info_path: dir.join("engine.json"), window_exe: None, tray: false };
        let engine = Engine::new(ctl, opts);
        engine.start().unwrap();
        let info: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("engine.json")).unwrap()).unwrap();
        assert_eq!(info["pid"], std::process::id());
        assert_eq!(info["version"], VERSION);
        let url = format!("http://127.0.0.1:{}/api/", info["port"]);
        Rig { engine, dir, url, token: info["token"].as_str().unwrap().to_string() }
    }

    fn call(r: &Rig, name: &str, args: Value) -> Value {
        let resp = ureq::post(&format!("{}{name}", r.url)).set(TOKEN_HEADER, &r.token).send_string(&json!({"args": args}).to_string());
        match resp {
            Ok(resp) => serde_json::from_str::<Value>(&resp.into_string().unwrap()).unwrap()["result"].clone(),
            Err(ureq::Error::Status(code, resp)) => json!({"http": code, "body": serde_json::from_str::<Value>(&resp.into_string().unwrap()).unwrap()}),
            Err(e) => panic!("{e}"),
        }
    }

    #[test]
    fn the_window_can_ask_for_everything() {
        let r = rig("api");
        let ping = call(&r, "ping", json!([]));
        assert_eq!(ping["version"], VERSION);
        assert_eq!(ping["window"], false);
        let init = call(&r, "get_init", json!([]));
        assert_eq!(init["settings"]["port"], 1080);
        assert_eq!(init["adapters"], json!([]));
        assert_eq!(call(&r, "status", json!([]))["running"], false);
        let saved = call(&r, "save_settings", json!([{"quota_mb": 250, "check_updates": false}]));
        assert_eq!(saved["settings"]["quota_mb"], 250.0);
        assert_eq!(saved["error"], Value::Null);
        let refused = call(&r, "save_settings", json!([{"eth_name": "A", "hot_name": "A"}]));
        assert!(refused["error"].as_str().unwrap().contains("two different"));
        assert!(call(&r, "power", json!([true]))["message"].as_str().unwrap().contains("Settings"));
        assert_eq!(call(&r, "power", json!([false])), json!({"ok": true}));
        assert_eq!(call(&r, "open_window", json!([]))["message"], "DualLink's window is missing.");
        assert_eq!(call(&r, "nonsense", json!([]))["http"], 404);
        assert_eq!(r.engine.tooltip(), "DualLink - off");
    }

    #[test]
    fn strangers_are_refused() {
        let r = rig("auth");
        let url = format!("{}status", r.url);
        let code = |resp: Result<ureq::Response, ureq::Error>| match resp {
            Err(ureq::Error::Status(code, _)) => code,
            Ok(_) => 200,
            Err(e) => panic!("{e}"),
        };
        assert_eq!(code(ureq::post(&url).send_string("{}")), 403, "no token");
        assert_eq!(code(ureq::post(&url).set(TOKEN_HEADER, "wrong").send_string("{}")), 403);
        let ok = |rq: ureq::Request| rq.set(TOKEN_HEADER, &r.token);
        assert_eq!(code(ok(ureq::post(&url)).set("Origin", "https://example.com").send_string("{}")), 403, "a web page");
        assert_eq!(code(ok(ureq::post(&url)).set("Host", "evil.example").send_string("{}")), 403, "DNS rebinding");
        assert_eq!(code(ok(ureq::get(&url)).call()), 405);
        assert_eq!(code(ok(ureq::post(&url)).send_string("[1]")), 400);
        assert_eq!(code(ok(ureq::post(&url)).send_string("{\"args\": 5}")), 400);
        assert_eq!(code(ok(ureq::post(&url)).send_string("{}")), 200);
    }

    #[test]
    fn closing_the_window_without_a_tray_icon_quits() {
        let r = rig("quit");
        assert_eq!(call(&r, "window_closed", json!([])), json!({"quitting": true}));
        r.engine.wait();
        assert!(!r.dir.join("engine.json").exists(), "engine.json is removed on the way out");
    }
}
