//! App logic: settings, turning DualLink on/off, following the adapters, live statistics, qBittorrent
//! set-up, the speed test, the traffic check and updates. No window or tray code in here.

use crate::diag::{self, Counts, Snapshot};
use crate::net::{in_phone_range, Adapter};
use crate::proxy::{Link, Proxy, UNKNOWN_AFTER};
use crate::qbt::QbtWebUi;
use crate::updates::{self, Update};
use crate::{autostart, speedtest};
use duallink_common::{fmt_rate, now_ts, LockExt, VERSION};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, Mutex, MutexGuard, RwLock, Weak};
use std::time::{Duration, Instant};

/// Seconds of speed history kept for the graph.
const HISTORY: usize = 90;
pub const MAX_EXTRA: usize = 2;
pub const MANUAL_APPS: [&str; 5] = ["fdm", "jd", "idm", "firefox", "other"];
pub const APP_KEYS: [&str; 6] = ["qbt", "fdm", "jd", "idm", "firefox", "other"];
/// Kept in config.json but never shown to (or accepted from) the window.
const INTERNAL_KEYS: [&str; 5] = ["qbt_saved", "learned", "speedtest", "seen_apps", "on_after_update"];
/// Forget remembered speeds after 30 days.
const LEARNED_MAX_AGE: f64 = 30.0 * 24.0 * 3600.0;
const VIRTUAL_HINTS: [&str; 13] = [
    "vethernet", "virtual", "vmware", "virtualbox", "hyper-v", "loopback", "bluetooth", "tap-", "tailscale", "zerotier",
    "wireguard", "openvpn", "vpn",
];

/// Program names (lower case) of the apps DualLink knows.
fn app_of(program: &str) -> &'static str {
    match program.to_lowercase().as_str() {
        "qbittorrent.exe" => "qbt",
        "fdm.exe" => "fdm",
        "jdownloader2.exe" | "jdownloader.exe" | "javaw.exe" | "java.exe" => "jd",
        "idman.exe" => "idm",
        "firefox.exe" => "firefox",
        _ => "other",
    }
}

fn is_virtual(name: &str) -> bool {
    let low = name.to_lowercase();
    VIRTUAL_HINTS.iter().any(|h| low.contains(h))
}

/// {program: connections} -> {app: {"connections": n, "programs": [...]}} (unknown programs -> "other").
pub fn classify_clients(counts: &Counts) -> Value {
    let mut live: Map<String, Value> = APP_KEYS.iter().map(|k| (k.to_string(), json!({"connections": 0, "programs": []}))).collect();
    let mut sorted: Vec<_> = counts.iter().collect();
    sorted.sort();
    for (name, c) in sorted {
        let entry = &mut live[app_of(name)];
        entry["connections"] = json!(entry["connections"].as_u64().unwrap_or(0) + *c as u64);
        if let Some(list) = entry["programs"].as_array_mut() {
            list.push(json!(name));
        }
    }
    Value::Object(live)
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Learned {
    /// Bytes/s per connection.
    pub per_conn: f64,
    pub ts: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SpeedLink {
    pub name: String,
    pub label: String,
    pub bps: f64,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SpeedResult {
    pub ts: f64,
    pub links: Vec<SpeedLink>,
}

/// Everything in config.json.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Cfg {
    /// Adapters are remembered by name - their address may change.
    pub eth_name: String,
    pub hot_name: String,
    /// Up to MAX_EXTRA more adapters (Wi-Fi, a second phone, ...).
    pub extra: Vec<String>,
    pub auto: bool,
    pub recycle: bool,
    pub share_eth: f64,
    pub share_hot: f64,
    pub quota_mb: f64,
    pub port: u16,
    pub qbt_auto: bool,
    pub qbt_url: String,
    pub qbt_user: String,
    pub qbt_pass: String,
    pub reconnect: bool,
    pub tray: bool,
    pub autostart: bool,
    pub tray_hint_shown: bool,
    /// The apps in the window's Apps list (a new install starts with none).
    pub added_apps: Vec<String>,
    // Apps set up by hand stay pointed at the proxy -> it stays up in pass-through mode while DualLink is off
    pub fdm: bool,
    pub jd: bool,
    pub idm: bool,
    pub firefox: bool,
    pub other: bool,
    /// Turn on when the hotspot connects.
    pub auto_on_hotspot: bool,
    /// Turn on when DualLink starts.
    pub auto_on_start: bool,
    pub check_updates: bool,
    /// qBittorrent's own settings while DualLink is on (crash recovery).
    pub qbt_saved: Option<Map<String, Value>>,
    /// Adapter name -> measured speed.
    pub learned: HashMap<String, Learned>,
    pub speedtest: Option<SpeedResult>,
    /// App -> last time it was seen connected to DualLink.
    pub seen_apps: HashMap<String, f64>,
    /// DualLink was on when an in-app update started: turn on again after it.
    pub on_after_update: bool,
}

impl Default for Cfg {
    fn default() -> Self {
        Cfg {
            eth_name: String::new(),
            hot_name: String::new(),
            extra: Vec::new(),
            auto: true,
            recycle: true,
            share_eth: 1.0,
            share_hot: 1.0,
            quota_mb: 0.0,
            port: 1080,
            qbt_auto: false,
            qbt_url: "http://127.0.0.1:8080".into(),
            qbt_user: String::new(),
            qbt_pass: String::new(),
            reconnect: true,
            tray: true,
            autostart: false,
            tray_hint_shown: false,
            added_apps: Vec::new(),
            fdm: false,
            jd: false,
            idm: false,
            firefox: false,
            other: false,
            auto_on_hotspot: true,
            auto_on_start: false,
            check_updates: true,
            qbt_saved: None,
            learned: HashMap::new(),
            speedtest: None,
            seen_apps: HashMap::new(),
            on_after_update: false,
        }
    }
}

impl Cfg {
    fn app_on(&self, key: &str) -> bool {
        match key {
            "qbt" => self.qbt_auto,
            "fdm" => self.fdm,
            "jd" => self.jd,
            "idm" => self.idm,
            "firefox" => self.firefox,
            "other" => self.other,
            _ => false,
        }
    }

    pub fn manual_apps_on(&self) -> bool {
        MANUAL_APPS.iter().any(|k| self.app_on(k))
    }

    pub fn extra_names(&self) -> Vec<String> {
        self.extra.iter().filter(|n| !n.is_empty()).take(MAX_EXTRA).cloned().collect()
    }

    /// Adapter names: Ethernet, hotspot, then the extras.
    pub fn link_names(&self) -> Vec<String> {
        let mut v = vec![self.eth_name.clone(), self.hot_name.clone()];
        v.extend(self.extra_names());
        v
    }

    pub fn link_labels(&self) -> Vec<String> {
        let mut v = vec!["Ethernet".to_string(), "Hotspot".to_string()];
        v.extend(self.extra_names());
        v
    }

    fn qbt(&self) -> QbtWebUi {
        QbtWebUi::new(&self.qbt_url, &self.qbt_user, &self.qbt_pass)
    }
}

pub fn load_cfg(path: &Path) -> Cfg {
    std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default()
}

/// What the rest of the app wants to hear from the controller.
pub trait Hooks: Send + Sync {
    /// Show a notification (the tray icon does).
    fn notify(&self, _title: &str, _msg: &str) {}
    /// DualLink turned on or off, or a connection changed.
    fn changed(&self) {}
    /// Close completely so the installer can replace the files.
    fn quit_for_update(&self) {}
}

struct NoHooks;
impl Hooks for NoHooks {}

/// The outside world, replaceable in tests.
pub struct Deps {
    pub scan: Box<dyn Fn() -> Vec<Adapter> + Send + Sync>,
    pub clients: Box<dyn Fn(u16, u32) -> Counts + Send + Sync>,
    pub rx_bytes: Box<dyn Fn() -> HashMap<String, u64> + Send + Sync>,
    pub connections: Box<dyn Fn(u16, u32) -> Snapshot + Send + Sync>,
    pub speed_url: String,
    pub speed_seconds: f64,
    pub check_seconds: f64,
}

impl Default for Deps {
    fn default() -> Self {
        Deps {
            scan: Box::new(crate::net::scan),
            clients: Box::new(diag::proxy_clients),
            rx_bytes: Box::new(diag::adapter_rx_bytes),
            connections: Box::new(diag::connections_snapshot),
            speed_url: speedtest::TEST_URL.into(),
            speed_seconds: 5.0,
            check_seconds: 3.0,
        }
    }
}

#[derive(Clone, Serialize)]
struct UpdateState {
    phase: &'static str,
    done: u64,
    total: u64,
    message: String,
}

impl UpdateState {
    fn idle() -> Self {
        UpdateState { phase: "idle", done: 0, total: 0, message: String::new() }
    }
}

/// Things that change while DualLink runs.
struct State {
    qbt_state: &'static str,
    qbt_message: String,
    hist: Vec<VecDeque<f64>>,
    speed: Vec<f64>,
    last: Vec<(u64, Instant)>,
    quota_warned: bool,
    app_live: Value,
    seen_saved: Option<Instant>,
    /// For "turn on when the hotspot connects".
    hot_present: Option<bool>,
    last_learn: Instant,
    update: Option<Update>,
    update_state: UpdateState,
    update_notified: Option<String>,
    speedtest_running: bool,
    speedtest_phase: String,
    standby_error: Option<String>,
}

fn ok() -> Value {
    json!({"ok": true})
}

fn refused(message: impl Into<String>) -> Value {
    json!({"ok": false, "message": message.into()})
}

fn plural(n: u64, word: &str) -> String {
    format!("{n} {word}{}", if n == 1 { "" } else { "s" })
}

fn ip_of(items: &[Adapter], name: &str) -> Option<std::net::Ipv4Addr> {
    if name.is_empty() {
        return None;
    }
    items.iter().find(|a| a.name == name).map(|a| a.ip)
}

fn ip6_of(items: &[Adapter], name: &str) -> Option<std::net::Ipv6Addr> {
    items.iter().find(|a| !name.is_empty() && a.name == name).and_then(|a| a.ip6)
}

pub struct Controller {
    cfg_path: PathBuf,
    cfg: Mutex<Cfg>,
    pub proxy: Arc<Proxy>,
    running: AtomicBool,
    /// One power change (on, off, standby) at a time.
    power: Mutex<()>,
    /// One qBittorrent set-up or restore at a time.
    qbt_lock: Mutex<()>,
    st: Mutex<State>,
    hooks: RwLock<Arc<dyn Hooks>>,
    deps: Deps,
    /// This start is the restart after an in-app update.
    pub just_updated: AtomicBool,
}

impl Controller {
    pub fn new(cfg_path: PathBuf, deps: Deps) -> Arc<Controller> {
        let cfg = load_cfg(&cfg_path);
        let leftover = cfg.qbt_saved.is_some();
        let now = Instant::now();
        let ctl = Arc::new(Controller {
            cfg_path,
            cfg: Mutex::new(cfg),
            proxy: Proxy::new(vec![Link::new("Ethernet"), Link::new("Hotspot")]),
            running: AtomicBool::new(false),
            power: Mutex::new(()),
            qbt_lock: Mutex::new(()),
            st: Mutex::new(State {
                qbt_state: "off",
                qbt_message: String::new(),
                hist: Vec::new(),
                speed: Vec::new(),
                last: Vec::new(),
                quota_warned: false,
                app_live: json!({}),
                seen_saved: None,
                hot_present: None,
                last_learn: now,
                update: None,
                update_state: UpdateState::idle(),
                update_notified: None,
                speedtest_running: false,
                speedtest_phase: String::new(),
                standby_error: None,
            }),
            hooks: RwLock::new(Arc::new(NoHooks)),
            deps,
            just_updated: AtomicBool::new(false),
        });
        ctl.sync_links();
        ctl.ensure_standby();
        if leftover {
            // DualLink was not shut down cleanly last time
            let c = ctl.clone();
            std::thread::spawn(move || c.restore_leftover());
        }
        ctl
    }

    /// The background loops: speed samples, adapter changes, which apps are connected.
    pub fn start_loops(self: &Arc<Self>) {
        fn every(ctl: &Arc<Controller>, period: Duration, f: fn(&Arc<Controller>)) {
            let weak: Weak<Controller> = Arc::downgrade(ctl);
            std::thread::spawn(move || loop {
                std::thread::sleep(period);
                match weak.upgrade() {
                    Some(c) => f(&c),
                    None => return,
                }
            });
        }
        every(self, Duration::from_secs(1), |c| c.sample());
        every(self, Duration::from_secs(3), |c| {
            if c.running() {
                c.check_adapters();
            } else if c.cfg().auto_on_hotspot {
                c.check_auto_on();
            }
        });
        every(self, Duration::from_secs(5), |c| c.check_apps());
    }

    pub fn set_hooks(&self, hooks: Arc<dyn Hooks>) {
        *self.hooks.write().unwrap_or_else(|e| e.into_inner()) = hooks;
    }

    fn hooks(&self) -> Arc<dyn Hooks> {
        self.hooks.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    fn notify(&self, title: &str, msg: &str) {
        self.hooks().notify(title, msg);
    }

    pub fn cfg(&self) -> MutexGuard<'_, Cfg> {
        self.cfg.lock_ok()
    }

    fn st(&self) -> MutexGuard<'_, State> {
        self.st.lock_ok()
    }

    pub fn running(&self) -> bool {
        self.running.load(Relaxed)
    }

    // ---- settings -------------------------------------------------------------------------- //
    pub fn save(&self) {
        let text = serde_json::to_string_pretty(&*self.cfg()).unwrap_or_default();
        let _ = std::fs::write(&self.cfg_path, text);
    }

    /// The settings the window shows and changes.
    pub fn public_settings(&self) -> Value {
        let mut v = serde_json::to_value(&*self.cfg()).unwrap_or_else(|_| json!({}));
        if let Some(map) = v.as_object_mut() {
            for k in INTERNAL_KEYS {
                map.remove(k);
            }
        }
        v
    }

    /// Change some settings. Errors are messages meant for the user.
    pub fn update_settings(self: &Arc<Self>, new: &Value) -> Result<Value, String> {
        const BAD: &str = "Some settings were not valid numbers.";
        let mut new = new.as_object().cloned().unwrap_or_default();
        new.retain(|k, _| !INTERNAL_KEYS.contains(&k.as_str()));
        let number = |v: &Value| -> Result<Option<f64>, String> {
            match v {
                Value::Null => Ok(None),
                Value::Number(n) => Ok(n.as_f64()),
                Value::String(s) if s.trim().is_empty() => Ok(None),
                Value::String(s) => s.trim().parse().map(Some).map_err(|_| BAD.to_string()),
                _ => Err(BAD.to_string()),
            }
        };
        if let Some(v) = new.get("extra") {
            let names: Vec<Value> = v.as_array().into_iter().flatten().filter(|n| n.as_str().is_some_and(|s| !s.is_empty())).take(MAX_EXTRA).cloned().collect();
            new.insert("extra".into(), Value::Array(names));
        }
        if let Some(v) = new.get("quota_mb") {
            let n = number(v)?.unwrap_or(0.0).max(0.0);
            new.insert("quota_mb".into(), json!(n));
        }
        if let Some(v) = new.get("port") {
            let n = number(v)?.unwrap_or(1080.0);
            if !(1.0..=65535.0).contains(&n) {
                return Err("The proxy port must be between 1 and 65535.".into());
            }
            new.insert("port".into(), json!(n as u16));
        }
        for k in ["share_eth", "share_hot"] {
            if let Some(v) = new.get(k) {
                let n = number(v)?.filter(|n| *n > 0.0).unwrap_or(1.0).max(0.1);
                new.insert(k.into(), json!(n));
            }
        }

        let qbt_before;
        {
            let mut cfg = self.cfg();
            qbt_before = cfg.qbt_auto;
            let mut merged = serde_json::to_value(&*cfg).map_err(|_| BAD.to_string())?;
            for (k, v) in &new {
                merged[k.as_str()] = v.clone();
            }
            let mut next: Cfg = serde_json::from_value(merged).map_err(|_| BAD.to_string())?;
            if !next.eth_name.is_empty() && next.eth_name == next.hot_name {
                return Err("Ethernet and hotspot must be two different connections.".into());
            }
            let mut names: Vec<String> = next.link_names().into_iter().filter(|n| !n.is_empty()).collect();
            let count = names.len();
            names.sort();
            names.dedup();
            if names.len() != count {
                return Err("Each connection can only be used once.".into());
            }
            // known apps only, each once; an app that is switched on is always in the list
            let mut added: Vec<String> = Vec::new();
            for k in next.added_apps.iter().map(String::as_str).chain(APP_KEYS.into_iter().filter(|k| next.app_on(k))) {
                if APP_KEYS.contains(&k) && !added.iter().any(|a| a == k) {
                    added.push(k.to_string());
                }
            }
            next.added_apps = added;
            *cfg = next;
        }
        self.save();
        self.apply_live_settings();
        let has = |k: &str| new.contains_key(k);
        if has("port") || MANUAL_APPS.iter().any(|k| has(k)) {
            self.ensure_standby();
        }
        if has("extra") {
            self.sync_links();
        }
        if self.running() && (has("eth_name") || has("hot_name") || has("extra")) {
            self.check_adapters(); // switch connections right away
        }
        if self.running() && self.cfg().qbt_auto != qbt_before {
            let c = self.clone();
            std::thread::spawn(move || c.qbt_follow()); // talks to qBittorrent: don't wait
        }
        Ok(self.public_settings())
    }

    fn apply_live_settings(&self) {
        let cfg = self.cfg().clone();
        let links = self.proxy.links();
        self.proxy.auto.store(cfg.auto, Relaxed);
        self.proxy.recycle.store(cfg.recycle, Relaxed);
        if !cfg.auto {
            links[0].ctl().weight = cfg.share_eth;
            links[1].ctl().weight = cfg.share_hot;
        }
        links[1].quota.store((cfg.quota_mb * 1024.0 * 1024.0) as u64, Relaxed);
        if !links[1].over_quota() {
            self.st().quota_warned = false;
        }
    }

    /// Make the proxy's links match the configured connections: Ethernet, Hotspot, then the extras.
    fn sync_links(&self) {
        let extras = self.cfg().extra_names();
        let mut gone = Vec::new();
        {
            let mut links = self.proxy.links.write().unwrap_or_else(|e| e.into_inner());
            while links.len() > 2 + extras.len() {
                gone.extend(links.pop());
            }
            while links.len() < 2 + extras.len() {
                links.push(Link::new(""));
            }
            for (l, name) in links[2..].iter().zip(&extras) {
                if l.ctl().name != *name {
                    // a different adapter now uses this slot
                    gone.push(l.clone());
                    l.reset_counters();
                    let mut c = l.ctl();
                    c.name = name.clone();
                    c.ip = None;
                    c.ip6 = None;
                }
            }
            let n = links.len();
            let mut st = self.st();
            st.hist.resize_with(n, || VecDeque::from(vec![0.0; HISTORY]));
            st.speed.resize(n, 0.0);
            st.last.resize(n, (0, Instant::now()));
        }
        for l in gone {
            self.proxy.close_link(&l);
        }
    }

    // ---- adapters -------------------------------------------------------------------------- //
    fn scan(&self) -> Vec<Adapter> {
        (self.deps.scan)()
    }

    /// Adapters for the Settings list. Fills in sensible defaults on first run.
    pub fn adapters(&self) -> Value {
        let items = self.scan();
        let mut real: Vec<&Adapter> = items.iter().filter(|a| !is_virtual(&a.name)).collect();
        if real.is_empty() {
            real = items.iter().collect();
        }
        let chosen = {
            let mut cfg = self.cfg();
            let (mut eth, mut hot) = (cfg.eth_name.clone(), cfg.hot_name.clone());
            let pick = |test: &dyn Fn(&Adapter) -> bool| real.iter().find(|a| test(a)).map(|a| a.name.clone()).unwrap_or_default();
            // first run: guess. A saved choice is never replaced just because it's unplugged right now.
            if hot.is_empty() {
                hot = pick(&|a| a.hotspot && a.name != eth);
            }
            if eth.is_empty() {
                eth = pick(&|a| a.name != hot && !a.hotspot);
            }
            if eth.is_empty() {
                eth = pick(&|a| a.name != hot);
            }
            if hot.is_empty() {
                hot = pick(&|a| a.name != eth);
            }
            cfg.eth_name = eth;
            cfg.hot_name = hot;
            cfg.link_names()
        };
        let mut out: Vec<Value> = items
            .iter()
            .map(|a| json!({"name": a.name, "ip": a.ip.to_string(), "hotspot": a.hotspot, "present": true}))
            .collect();
        for name in chosen {
            // keep an unplugged choice visible in the list
            if !name.is_empty() && !items.iter().any(|a| a.name == name) {
                out.push(json!({"name": name, "ip": "", "hotspot": false, "present": false}));
            }
        }
        Value::Array(out)
    }

    // ---- qBittorrent ----------------------------------------------------------------------- //
    pub fn test_qbt(&self) -> Value {
        let mut q = self.cfg().qbt();
        match q.version() {
            Ok(v) => json!({"ok": true, "message": format!("Connected to qBittorrent {v}")}),
            Err(e) => json!({"ok": false, "kind": e.kind.as_str(), "message": e.message}),
        }
    }

    fn restore_leftover(&self) {
        for _ in 0..30 {
            // qBittorrent may start after us: keep trying for a minute
            let cfg = self.cfg().clone();
            let Some(saved) = cfg.qbt_saved.as_ref().filter(|_| !self.running()) else { return };
            if cfg.qbt().restore(saved).is_ok() {
                self.cfg().qbt_saved = None;
                self.save();
                return;
            }
            std::thread::sleep(Duration::from_secs(2));
        }
    }

    fn set_qbt_state(&self, state: &'static str, message: &str) {
        let mut st = self.st();
        st.qbt_state = state;
        st.qbt_message = message.to_string();
    }

    fn qbt_apply(&self) {
        let cfg = self.cfg().clone();
        let mut q = cfg.qbt();
        let result = q.apply_dual_link(self.proxy.port()).and_then(|saved| {
            if cfg.qbt_saved.is_none() {
                // keep the true originals if we crashed while on
                self.cfg().qbt_saved = Some(saved);
                self.save();
            }
            if cfg.reconnect {
                let hashes = q.downloading_hashes()?;
                q.reconnect(&hashes)?;
            }
            Ok(())
        });
        match result {
            Ok(()) => self.set_qbt_state("connected", "qBittorrent is set up and using DualLink."),
            Err(e) => self.set_qbt_state(e.kind.as_str(), &e.message),
        }
    }

    /// Give qBittorrent its own settings back (saved when DualLink set it up).
    fn qbt_restore(&self) {
        let cfg = self.cfg().clone();
        let Some(saved) = &cfg.qbt_saved else { return };
        let mut q = cfg.qbt();
        if q.restore(saved).is_err() {
            return; // will be retried at next start
        }
        self.cfg().qbt_saved = None;
        self.save();
        if cfg.reconnect {
            if let Ok(hashes) = q.downloading_hashes() {
                let _ = q.reconnect(&hashes);
            }
        }
    }

    /// qBittorrent was added (switched on) or removed (switched off) while DualLink is on.
    fn qbt_follow(&self) {
        {
            let _one = self.qbt_lock.lock_ok();
            if !self.running() {
                return; // turned off meanwhile: power_off has dealt with it
            }
            if self.cfg().qbt_auto {
                self.qbt_apply();
            } else {
                self.qbt_restore();
                self.set_qbt_state("disabled", "");
            }
        }
        self.hooks().changed();
    }

    pub fn retry_qbt(&self) -> Value {
        if self.running() && self.cfg().qbt_auto {
            let _one = self.qbt_lock.lock_ok();
            self.qbt_apply();
        }
        let st = self.st();
        json!({"state": st.qbt_state, "message": st.qbt_message})
    }

    // ---- power ----------------------------------------------------------------------------- //
    pub fn power_on(&self) -> Value {
        let note;
        {
            let _one = self.power.lock_ok();
            if self.running() {
                return ok();
            }
            let cfg = self.cfg().clone();
            if cfg.eth_name.is_empty() || cfg.hot_name.is_empty() {
                return refused("Pick both an Ethernet and a hotspot connection in Settings.");
            }
            if cfg.eth_name == cfg.hot_name {
                return refused("Ethernet and hotspot must be two different connections.");
            }
            let items = self.scan();
            let names = cfg.link_names();
            let ips: Vec<_> = names.iter().map(|n| ip_of(&items, n)).collect();
            if ips.iter().all(Option::is_none) {
                return refused("Neither connection is available right now. Check that Ethernet or your phone is connected.");
            }
            note = if ips[0].is_none() {
                Some(format!("{} isn't connected - DualLink is using the hotspot and will add Ethernet when it's back.", cfg.eth_name))
            } else if ips[1].is_none() {
                Some(format!("{} isn't connected yet - DualLink will add the hotspot automatically when it appears.", cfg.hot_name))
            } else {
                None
            };
            let links = self.proxy.links();
            for ((l, ip), name) in links.iter().zip(&ips).zip(&names) {
                l.reset_counters();
                let mut c = l.ctl();
                c.ip = *ip;
                c.ip6 = ip6_of(&items, name);
                c.enabled = true;
            }
            self.seed_speeds();
            self.apply_live_settings();
            self.proxy.moved.store(0, Relaxed);
            self.proxy.refused_ipv6.store(0, Relaxed);
            self.proxy.failed.store(0, Relaxed);
            {
                let mut st = self.st();
                st.quota_warned = false;
                let now = Instant::now();
                st.last.iter_mut().for_each(|l| *l = (0, now));
            }
            if self.proxy.is_up() && self.proxy.port() != cfg.port {
                self.proxy.stop(); // standby proxy on an old port
            }
            if self.proxy.is_up() {
                self.proxy.close_link(&self.proxy.direct); // apps reconnect their pieces over both links
            } else if let Err(e) = self.proxy.start(cfg.port) {
                return refused(e);
            }
            self.proxy.passthrough.store(false, Relaxed);
            self.running.store(true, Relaxed);
        }
        if let Some(note) = &note {
            self.notify("DualLink is on", note);
        }
        {
            let _one = self.qbt_lock.lock_ok();
            if self.cfg().qbt_auto {
                self.qbt_apply();
            } else {
                self.set_qbt_state("disabled", "");
            }
        }
        self.hooks().changed();
        match note {
            Some(note) => json!({"ok": true, "message": note}),
            None => ok(),
        }
    }

    pub fn power_off(&self) -> Value {
        {
            let _one = self.power.lock_ok();
            if !self.running() {
                return ok();
            }
            self.remember_speeds();
            self.running.store(false, Relaxed);
        }
        {
            let _one = self.qbt_lock.lock_ok();
            self.qbt_restore();
        }
        if self.cfg().manual_apps_on() {
            self.proxy.passthrough.store(true, Relaxed); // apps set up by hand keep working over the normal route
        } else {
            self.proxy.stop();
        }
        self.set_qbt_state("off", "");
        self.hooks().changed();
        ok()
    }

    /// While off: keep the proxy up in pass-through mode if an app is set up by hand, otherwise keep it down.
    pub fn ensure_standby(&self) {
        let _one = self.power.lock_ok();
        if self.running() {
            return;
        }
        let (want, port) = {
            let cfg = self.cfg();
            (cfg.manual_apps_on(), cfg.port)
        };
        if self.proxy.is_up() && (!want || self.proxy.port() != port) {
            self.proxy.stop();
        }
        if want && !self.proxy.is_up() {
            self.proxy.passthrough.store(true, Relaxed);
            let result = self.proxy.start(port);
            self.st().standby_error = result.err();
        }
    }

    /// Called once when the app starts: "turn on when DualLink starts", or back on after an update.
    pub fn startup(&self) -> Value {
        let (resume, wanted) = {
            let mut cfg = self.cfg();
            let resume = std::mem::take(&mut cfg.on_after_update); // only for the start right after the update
            let wanted = cfg.auto_on_start || (resume && self.just_updated.load(Relaxed));
            (resume, wanted && !cfg.eth_name.is_empty() && !cfg.hot_name.is_empty())
        };
        if resume {
            self.save();
        }
        if wanted && !self.running() {
            return self.power_on();
        }
        ok()
    }

    pub fn shutdown(&self) {
        self.power_off();
        self.proxy.stop(); // quitting the app: nothing left to pass through
    }

    // ---- following the adapters ------------------------------------------------------------- //
    /// While off: turn on when the hotspot appears (not if it was already there - the user turned us off).
    pub fn check_auto_on(&self) {
        let cfg = self.cfg().clone();
        if cfg.hot_name.is_empty() || cfg.eth_name.is_empty() {
            return;
        }
        let items = self.scan();
        let mut present = ip_of(&items, &cfg.hot_name).is_some();
        let before = self.st().hot_present;
        if !present && before == Some(false) {
            // the phone may come back as a differently-named adapter
            if let Some(cand) = items.iter().find(|a| in_phone_range(a.ip) && a.name != cfg.eth_name) {
                self.cfg().hot_name = cand.name.clone();
                self.save();
                present = true;
            }
        }
        self.st().hot_present = Some(present);
        if before == Some(false) && present && !self.running() && cfg.auto_on_hotspot && self.power_on()["ok"] == true {
            self.notify("DualLink turned on", "Your hotspot connected, so DualLink is combining it with Ethernet.");
        }
    }

    /// While on: follow address changes, unplugging and re-plugging of every connection.
    pub fn check_adapters(&self) {
        let items = self.scan();
        let cfg = self.cfg().clone();
        let (names, labels) = (cfg.link_names(), cfg.link_labels());
        let mut changed = false;
        for (i, link) in self.proxy.links().iter().enumerate().take(names.len()) {
            let mut name = names[i].clone();
            let mut ip = ip_of(&items, &name);
            let mut ip6 = ip6_of(&items, &name);
            if ip.is_none() && i == 1 {
                // the phone may come back as a differently-named adapter (another USB port, Wi-Fi instead of USB)
                let taken = |n: &str| names.iter().enumerate().any(|(j, other)| j != 1 && other == n);
                if let Some(cand) = items.iter().find(|a| in_phone_range(a.ip) && !taken(&a.name)) {
                    self.cfg().hot_name = cand.name.clone();
                    name = cand.name.clone();
                    ip = Some(cand.ip);
                    ip6 = cand.ip6;
                    changed = true;
                }
            }
            let old = {
                let mut c = link.ctl();
                c.ip6 = if ip.is_some() { ip6 } else { None };
                if c.ip == ip || !self.running() {
                    continue;
                }
                if ip.is_some() {
                    c.measured_at = None; // treat as unknown again: a fair share until measured
                }
                std::mem::replace(&mut c.ip, ip)
            };
            if old.is_some() {
                self.proxy.close_link(link); // its connections are dead or on the old address
            }
            let shown = if i < 2 { labels[i].clone() } else { format!("{} connection", labels[i]) };
            match (old, ip) {
                (_, None) => self.notify(
                    &format!("{shown} disconnected"),
                    "Downloads continue over the other connections. DualLink will add it back automatically when it reconnects.",
                ),
                (None, Some(ip)) => self.notify(&format!("{shown} connected"), &format!("DualLink is using {name} ({ip}).")),
                (Some(_), Some(ip)) => self.notify(&format!("{shown} got a new address"), &format!("DualLink switched to {ip} automatically.")),
            }
            changed = true;
        }
        let hot_present = self.proxy.links()[1].ip().is_some();
        self.st().hot_present = Some(hot_present);
        if changed {
            self.save();
            self.hooks().changed();
        }
    }

    // ---- live app status ------------------------------------------------------------------- //
    /// Which apps are connected to DualLink right now; remembers when each was last seen working.
    pub fn check_apps(&self) {
        if !self.proxy.is_up() {
            self.st().app_live = json!({});
            return;
        }
        let counts = (self.deps.clients)(self.proxy.port(), std::process::id());
        let live = classify_clients(&counts);
        let active: Vec<&str> = APP_KEYS.into_iter().filter(|k| live[*k]["connections"].as_u64().unwrap_or(0) > 0).collect();
        let newly = {
            let mut cfg = self.cfg();
            let newly = active.iter().any(|k| !cfg.seen_apps.contains_key(*k));
            for k in &active {
                cfg.seen_apps.insert(k.to_string(), now_ts());
            }
            newly
        };
        let due = {
            let mut st = self.st();
            st.app_live = live;
            let due = newly || st.seen_saved.is_none_or(|t| t.elapsed() > Duration::from_secs(300));
            if due {
                st.seen_saved = Some(Instant::now());
            }
            due
        };
        if due && !active.is_empty() {
            self.save();
        }
    }

    // ---- remembered speeds ----------------------------------------------------------------- //
    /// Store each link's measured speed per connection, so the next session starts with it.
    pub fn remember_speeds(&self) {
        let names = self.cfg().link_names();
        let mut fresh = Vec::new();
        for (l, name) in self.proxy.links().iter().zip(names) {
            let c = l.ctl();
            if !name.is_empty() && c.per_conn > 0.0 && c.measured_at.is_some_and(|t| t.elapsed() < UNKNOWN_AFTER) {
                fresh.push((name, Learned { per_conn: c.per_conn, ts: now_ts() }));
            }
        }
        if !fresh.is_empty() {
            self.cfg().learned.extend(fresh);
            self.save();
        }
    }

    /// Speeds to start balancing with (remembered, or from the last speed test), one per link.
    fn seed_values(&self) -> Option<Vec<f64>> {
        let cfg = self.cfg();
        let (now, names) = (now_ts(), cfg.link_names());
        let learned: Vec<f64> = names
            .iter()
            .filter_map(|n| cfg.learned.get(n).filter(|v| now - v.ts < LEARNED_MAX_AGE && v.per_conn > 0.0).map(|v| v.per_conn))
            .collect();
        if learned.len() == names.len() {
            return Some(learned);
        }
        let st = cfg.speedtest.as_ref().filter(|st| now - st.ts < LEARNED_MAX_AGE)?;
        let tested: Vec<f64> =
            names.iter().filter_map(|n| st.links.iter().find(|r| r.name == *n).map(|r| r.bps).filter(|b| *b > 0.0)).collect();
        (tested.len() == names.len()).then_some(tested)
    }

    fn seed_speeds(&self) {
        let Some(seeds) = self.seed_values() else { return };
        let total: f64 = seeds.iter().sum();
        let min = self.proxy.min_share;
        for (l, v) in self.proxy.links().iter().zip(seeds) {
            let mut c = l.ctl();
            c.per_conn = v;
            c.measured_at = Some(Instant::now()); // counts as measured: no even-split warm-up needed
            c.weight = (v / total).clamp(min, 1.0 - min);
        }
    }

    // ---- speed test ------------------------------------------------------------------------ //
    pub fn start_speed_test(self: &Arc<Self>) -> Value {
        let cfg = self.cfg().clone();
        let items = self.scan();
        let names = cfg.link_names();
        let ips: Vec<_> = names.iter().map(|n| ip_of(&items, n)).collect();
        {
            let mut st = self.st();
            if st.speedtest_running {
                return ok();
            }
            if ips.iter().all(Option::is_none) {
                return refused("None of your connections is available right now.");
            }
            st.speedtest_running = true;
            st.speedtest_phase.clear();
        }
        let ctl = self.clone();
        let labels = cfg.link_labels();
        std::thread::spawn(move || {
            let mut result = SpeedResult { ts: now_ts(), links: Vec::new() };
            for ((name, label), ip) in names.into_iter().zip(labels).zip(ips) {
                ctl.st().speedtest_phase = label.clone();
                let m = match ip {
                    Some(ip) => speedtest::measure(ip, ctl.deps.speed_seconds, &ctl.deps.speed_url),
                    None => speedtest::Measured { bps: 0.0, error: Some(format!("{label} isn't connected.")) },
                };
                result.links.push(SpeedLink { name, label, bps: m.bps, error: m.error });
            }
            ctl.cfg().speedtest = Some(result);
            ctl.save();
            let unmeasured = ctl.proxy.links().iter().all(|l| l.ctl().measured_at.is_none_or(|t| t.elapsed() > UNKNOWN_AFTER));
            if ctl.running() && unmeasured {
                ctl.seed_speeds(); // nothing measured yet this session: use the test right away
            }
            let mut st = ctl.st();
            st.speedtest_running = false;
            st.speedtest_phase.clear();
        });
        ok()
    }

    // ---- updates --------------------------------------------------------------------------- //
    pub fn check_updates_now(&self) -> Value {
        if updates::REPO.is_empty() {
            return refused("Update checks work in the version built by GitHub.");
        }
        self.check_updates_with(|| updates::fetch_latest(updates::REPO))
    }

    pub fn check_updates_with(&self, fetch: impl FnOnce() -> Result<Value, String>) -> Value {
        let Ok(data) = fetch() else { return refused("Couldn't reach GitHub to check for updates.") };
        let update = updates::newer_release(&data, VERSION);
        let announce = {
            let mut st = self.st();
            st.update = update.clone();
            match &update {
                Some(u) if st.update_notified.as_deref() != Some(&u.version) => {
                    st.update_notified = Some(u.version.clone());
                    true
                }
                _ => false,
            }
        };
        if let (true, Some(u)) = (announce, &update) {
            self.notify(&format!("DualLink {} is available", u.version), "Open DualLink to download it.");
        }
        json!({"ok": true, "update": update})
    }

    /// Check a little after starting, then twice a day.
    pub fn start_update_checks(self: &Arc<Self>) {
        let weak = Arc::downgrade(self);
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(10));
            while let Some(c) = weak.upgrade() {
                if c.cfg().check_updates {
                    c.check_updates_now();
                }
                drop(c);
                std::thread::sleep(Duration::from_secs(12 * 3600));
            }
        });
    }

    /// Download the new version, check it, switch off safely, install and restart.
    pub fn start_update(self: &Arc<Self>) -> Value {
        let asset = {
            let mut st = self.st();
            if matches!(st.update_state.phase, "downloading" | "installing") {
                return ok();
            }
            let Some(update) = &st.update else { return refused("There's no update to install.") };
            if !updates::installed() {
                return refused("This copy of DualLink wasn't set up with the installer - use the download page instead.");
            }
            let Some(asset) = update.installer.clone().filter(|a| !a.url.is_empty()) else {
                return refused("This release has no installer - use the download page.");
            };
            st.update_state = UpdateState { phase: "downloading", done: 0, total: asset.size, message: String::new() };
            asset
        };
        let ctl = self.clone();
        std::thread::spawn(move || {
            let progress = |done, total| {
                let mut st = ctl.st();
                st.update_state.done = done;
                st.update_state.total = total;
            };
            let result = updates::download(&asset, &std::env::temp_dir(), &progress, false).and_then(|path| {
                ctl.st().update_state.phase = "installing";
                if ctl.running() {
                    ctl.cfg().on_after_update = true; // the new version turns on again (see startup)
                    ctl.save();
                }
                ctl.power_off(); // gives qBittorrent its own settings back first
                updates::run_installer(&path) // the installer starts the new version when done
            });
            match result {
                Ok(()) => ctl.hooks().quit_for_update(),
                Err(message) => {
                    let was_on = std::mem::take(&mut ctl.cfg().on_after_update);
                    ctl.st().update_state = UpdateState { phase: "error", done: 0, total: 0, message };
                    if was_on {
                        ctl.save();
                        if !ctl.running() {
                            ctl.power_on(); // the update didn't happen: back to how it was
                        }
                    }
                }
            }
        });
        ok()
    }

    // ---- statistics ------------------------------------------------------------------------ //
    /// Once a second: the speed of each link, for the graph and the tray.
    pub fn sample(&self) {
        let running = self.running();
        let links = self.proxy.links();
        let now = Instant::now();
        let (learn, warn);
        {
            let mut st = self.st();
            for (i, l) in links.iter().enumerate().take(st.last.len()) {
                let (prev, t0) = st.last[i];
                let down = l.down.load(Relaxed);
                let secs = now.duration_since(t0).as_secs_f64().max(0.001);
                let sp = if running { down.saturating_sub(prev) as f64 / secs } else { 0.0 };
                st.last[i] = (down, now);
                st.speed[i] = sp;
                st.hist[i].pop_front();
                st.hist[i].push_back(sp);
            }
            learn = running && st.last_learn.elapsed() > Duration::from_secs(60);
            if learn {
                st.last_learn = now;
            }
            warn = running && links[1].over_quota() && !st.quota_warned;
            if warn {
                st.quota_warned = true;
            }
        }
        if learn {
            self.remember_speeds();
        }
        if warn {
            self.notify("Hotspot data limit reached", "DualLink stopped using the hotspot. Downloads continue over Ethernet.");
        }
    }

    /// (label, bytes/s) per connection, for the tray icon's tooltip.
    pub fn speeds(&self) -> Vec<(String, f64)> {
        let labels = self.cfg().link_labels();
        labels.into_iter().zip(self.st().speed.iter().copied()).collect()
    }

    /// Everything the window shows, once a second.
    pub fn status(&self) -> Value {
        let cfg = self.cfg().clone();
        let running = self.running();
        let (names, labels) = (cfg.link_names(), cfg.link_labels());
        let links = self.proxy.links();
        let wsum: f64 = links.iter().filter(|l| l.usable()).map(|l| l.ctl().weight).sum();
        let st = self.st();
        let mut out = Vec::new();
        for (i, l) in links.iter().enumerate().take(names.len().min(st.speed.len())) {
            let (ip, weight) = {
                let c = l.ctl();
                (c.ip, c.weight)
            };
            let kind = match i {
                0 => "eth",
                1 => "hot",
                _ => "extra",
            };
            let share = if running && l.usable() && wsum > 0.0 { (100.0 * weight / wsum).round() } else { 0.0 };
            out.push(json!({
                "name": labels[i], "adapter": names[i], "kind": kind,
                "ip": ip.map(|ip| ip.to_string()).unwrap_or_default(),
                "present": ip.is_some() || !running,
                "connections": l.active.load(Relaxed).max(0), "failures": l.failures.load(Relaxed),
                "share": share, "speed": st.speed[i],
                "down": l.down.load(Relaxed), "up": l.up.load(Relaxed),
                "quota": l.quota.load(Relaxed), "over_quota": l.over_quota(),
                "history": st.hist[i],
            }));
        }
        let apps: Map<String, Value> = MANUAL_APPS.iter().map(|k| (k.to_string(), json!(cfg.app_on(k)))).collect();
        json!({
            "running": running,
            "combined": st.speed.iter().sum::<f64>(),
            "links": out,
            "app_live": st.app_live,
            "seen_apps": cfg.seen_apps,
            "apps": apps,
            "qbt_auto": cfg.qbt_auto,
            "qbt": {"state": st.qbt_state, "message": st.qbt_message},
            "moved": self.proxy.moved.load(Relaxed),
            "dht": running && self.proxy.udp_assocs.load(Relaxed) > 0,
            "port": if running { self.proxy.port() } else { cfg.port },
            "speedtest": {"running": st.speedtest_running, "phase": st.speedtest_phase, "result": cfg.speedtest},
            "update": st.update,
            "update_state": st.update_state,
            "just_updated": self.just_updated.load(Relaxed),
            "version": VERSION,
            "standby": !running && self.proxy.is_up(),
            "standby_error": st.standby_error,
        })
    }

    // ---- traffic check --------------------------------------------------------------------- //
    /// Compare what Windows sees on each connection with what DualLink carries, and list the programs.
    pub fn traffic_check(&self) -> Value {
        let downs = || -> Vec<u64> { self.proxy.links().iter().map(|l| l.down.load(Relaxed)).collect() };
        let (rx0, d0, t0) = ((self.deps.rx_bytes)(), downs(), Instant::now());
        std::thread::sleep(Duration::from_secs_f64(self.deps.check_seconds));
        let (rx1, d1) = ((self.deps.rx_bytes)(), downs());
        let dt = t0.elapsed().as_secs_f64().max(0.001);
        let cfg = self.cfg().clone();
        let running = self.running();
        let port = if self.proxy.is_up() { self.proxy.port() } else { cfg.port };
        let conns = (self.deps.connections)(port, std::process::id());
        if rx0.is_empty() && conns.through.is_empty() && conns.direct.is_empty() {
            return refused("The traffic check needs Windows.");
        }
        let sorted = |c: &Counts| -> Vec<(String, u32)> {
            let mut v: Vec<_> = c.iter().map(|(n, c)| (n.clone(), *c)).collect();
            v.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
            v
        };
        let listed = |v: &[(String, u32)]| -> Vec<Value> { v.iter().map(|(n, c)| json!({"name": n, "connections": c})).collect() };

        let mut notes: Vec<(&str, String)> = Vec::new();
        let mut report = Vec::new();
        for (i, (adapter, label)) in cfg.link_names().iter().zip(cfg.link_labels()).enumerate() {
            let windows = match (rx0.get(adapter), rx1.get(adapter)) {
                (Some(a), Some(b)) if b >= a => Some((b - a) as f64 / dt),
                _ => None,
            };
            let mine = match (d0.get(i), d1.get(i)) {
                (Some(a), Some(b)) if running => b.saturating_sub(*a) as f64 / dt,
                _ => 0.0,
            };
            let other = windows.map(|w| (w - mine * 1.05).max(0.0)); // ~5% protocol overhead
            let apps = conns.direct.get(adapter).map(sorted).unwrap_or_default();
            if let Some(o) = other.filter(|o| *o > (256.0 * 1024.0f64).max(0.15 * windows.unwrap_or(0.0))) {
                let who: Vec<&str> = apps.iter().take(4).map(|(n, _)| n.as_str()).collect();
                let shown = if i >= 2 { label.clone() } else { label.to_lowercase() };
                let tail = if who.is_empty() { ".".to_string() } else { format!(". Programs connecting over it directly: {}.", who.join(", ")) };
                notes.push(("warn", format!("{} on {shown} isn't going through DualLink{tail}", fmt_rate(o))));
            }
            let link = self.proxy.links().get(i).cloned();
            report.push(json!({
                "label": label, "adapter": adapter, "windows": windows, "duallink": mine, "other": other,
                "connections": link.as_ref().map(|l| l.active.load(Relaxed).max(0)).unwrap_or(0),
                "failures": link.as_ref().map(|l| l.failures.load(Relaxed)).unwrap_or(0),
                "direct_apps": listed(&apps),
            }));
        }
        let through = sorted(&conns.through);
        let mut direct_any: Counts = Counts::new();
        for per in conns.direct.values() {
            for (n, c) in per {
                *direct_any.entry(n.clone()).or_default() += c;
            }
        }
        for (name, c) in &through {
            if let Some(d) = direct_any.get(name) {
                notes.push(("warn", format!(
                    "{name} sends {} through DualLink but also connects directly {}, so part of its download isn't split.",
                    plural(*c as u64, "connection"), plural(*d as u64, "time")
                )));
            }
        }
        let refused_v6 = self.proxy.refused_ipv6.load(Relaxed);
        if refused_v6 > 0 {
            notes.push(("warn", format!(
                "DualLink couldn't make {} because neither of your connections has an IPv6 address right now. Apps often connect directly instead, so that traffic isn't split.",
                plural(refused_v6, "IPv6 connection")
            )));
        }
        if !running {
            notes.insert(0, ("info", "DualLink is off, so nothing is being split. Turn it on and run the check while a download is going.".into()));
        } else if through.is_empty() {
            notes.insert(0, ("warn", format!(
                "No program is connected through DualLink right now. Check your download app's proxy setting (SOCKS5, 127.0.0.1, port {port})."
            )));
        }
        if running && !notes.iter().any(|(k, _)| *k == "warn") {
            notes.push(("good", "Everything matches: your downloads are going through DualLink.".into()));
        }
        json!({
            "ok": true, "seconds": (dt * 10.0).round() / 10.0, "links": report, "through": listed(&through),
            "refused_ipv6": refused_v6, "failed": self.proxy.failed.load(Relaxed),
            "notes": notes.iter().map(|(k, t)| json!({"kind": k, "text": t})).collect::<Vec<_>>(),
        })
    }
}

/// Run by the uninstaller: give qBittorrent its own settings back and remove "Start with Windows".
pub fn cleanup(cfg_path: &Path) {
    let mut cfg = load_cfg(cfg_path);
    if let Some(saved) = cfg.qbt_saved.take() {
        if cfg.qbt().restore(&saved).is_ok() {
            let _ = std::fs::write(cfg_path, serde_json::to_string_pretty(&cfg).unwrap_or_default());
        }
    }
    let _ = autostart::set(false);
}
