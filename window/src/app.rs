//! The window's logic: asks the engine for the status once a second and shows it, and sends what the
//! user changes back to the engine. All of it runs on the window's thread; engine calls are made on
//! worker threads and their answers are brought back here.

use crate::engine::Engine;
use crate::fmt;
use crate::guides::{self, app as app_info, setting_key, APPS};
use crate::{AppItem, Bullet, CheckRow, MainWindow, MoreApp, Note, Series, SpeedRow, Step, Tile};
use duallink_common::shell;
use serde_json::{json, Map, Value};
use slint::{Color, ComponentHandle, Model, ModelRc, SharedString, Timer, TimerMode, VecModel};
use std::cell::RefCell;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::Arc;
use std::time::Duration;

pub struct Opts {
    /// Made-up data instead of the engine.
    pub demo: bool,
    /// Open every screen, save a picture of each into this folder, then close.
    pub shots: Option<PathBuf>,
    /// Some(true) = dark, Some(false) = light, None = follow Windows.
    pub dark: Option<bool>,
}

thread_local! {
    static APP: RefCell<Option<Rc<App>>> = const { RefCell::new(None) };
}

const LINK_COLORS: [u32; 4] = [0x4d8dff, 0xff9b3d, 0xa78bfa, 0x2dd4bf];

fn rgb(c: u32) -> Color {
    Color::from_rgb_u8((c >> 16) as u8, (c >> 8) as u8, c as u8)
}

fn link_color(i: usize) -> Color {
    rgb(LINK_COLORS[i.min(LINK_COLORS.len() - 1)])
}

// Forgiving access to the engine's JSON: missing or mistyped values give defaults.
fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

fn n(v: &Value, key: &str) -> f64 {
    v.get(key).and_then(Value::as_f64).unwrap_or(0.0)
}

fn b(v: &Value, key: &str) -> bool {
    v.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn list<'a>(v: &'a Value, key: &str) -> &'a [Value] {
    v.get(key).and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[])
}

fn strings(items: impl IntoIterator<Item = String>) -> ModelRc<SharedString> {
    ModelRc::new(VecModel::from(items.into_iter().map(SharedString::from).collect::<Vec<_>>()))
}

/// Make a list on screen match `new`, touching only the rows that changed (so nothing flickers).
fn sync<T: Clone + PartialEq + 'static>(model: &VecModel<T>, new: Vec<T>) {
    while model.row_count() > new.len() {
        model.remove(model.row_count() - 1);
    }
    for (i, item) in new.into_iter().enumerate() {
        if i >= model.row_count() {
            model.push(item);
        } else if model.row_data(i).as_ref() != Some(&item) {
            model.set_row_data(i, item);
        }
    }
}

#[derive(Default)]
struct State {
    settings: Map<String, Value>,
    /// Changes not sent to the engine yet.
    pending: Map<String, Value>,
    adapters: Vec<Value>,
    last: Option<Value>,
    running: bool,
    ready: bool,
    polling: bool,
    closing: bool,
    shown_updated: bool,
    /// Counts local setting changes, to spot statuses that were asked for before a change.
    changes: u64,
    /// Which dialog is open: "", "qbt", "guide", "more", "remove", "update", "speed" or "check".
    modal: String,
    /// The app the open guide or "remove" question is about.
    modal_app: String,
}

pub struct App {
    pub ui: MainWindow,
    pub engine: Arc<dyn Engine>,
    pub opts: Opts,
    st: RefCell<State>,
    tiles: Rc<VecModel<Tile>>,
    apps: Rc<VecModel<AppItem>>,
    chart: Rc<VecModel<Series>>,
    steps: Rc<VecModel<Step>>,
    speed_rows: Rc<VecModel<SpeedRow>>,
    poll_timer: Timer,
    save_timer: Timer,
    toast_timer: Timer,
}

/// Run `f` with the app, on the window's thread.
fn with_app(f: impl FnOnce(&Rc<App>)) {
    let app = APP.with(|a| a.borrow().clone());
    if let Some(app) = app {
        f(&app);
    }
}

impl App {
    pub fn start(ui: MainWindow, engine: Arc<dyn Engine>, opts: Opts) -> Rc<App> {
        let app = Rc::new(App {
            ui,
            engine,
            opts,
            st: RefCell::new(State::default()),
            tiles: Rc::new(VecModel::default()),
            apps: Rc::new(VecModel::default()),
            chart: Rc::new(VecModel::default()),
            steps: Rc::new(VecModel::default()),
            speed_rows: Rc::new(VecModel::default()),
            poll_timer: Timer::default(),
            save_timer: Timer::default(),
            toast_timer: Timer::default(),
        });
        APP.with(|a| *a.borrow_mut() = Some(app.clone()));
        if let Some(dark) = app.opts.dark {
            app.ui.invoke_set_dark(dark);
        }
        app.ui.set_tiles(app.tiles.clone().into());
        app.ui.set_apps(app.apps.clone().into());
        app.ui.set_chart(app.chart.clone().into());
        app.ui.set_steps(app.steps.clone().into());
        app.ui.set_speed_rows(app.speed_rows.clone().into());
        app.connect();
        app.init();
        app
    }

    /// Ask the engine for something; `then` runs back on the window's thread with the answer.
    pub fn call(&self, name: &'static str, args: Vec<Value>, then: impl FnOnce(&Rc<App>, Result<Value, String>) + Send + 'static) {
        let engine = self.engine.clone();
        std::thread::spawn(move || {
            let result = engine.call(name, args);
            let _ = slint::invoke_from_event_loop(move || with_app(|app| then(app, result)));
        });
    }

    fn setting_bool(&self, key: &str) -> bool {
        self.st.borrow().settings.get(key).and_then(Value::as_bool).unwrap_or(false)
    }

    fn setting_str(&self, key: &str) -> String {
        self.st.borrow().settings.get(key).and_then(Value::as_str).unwrap_or("").to_string()
    }

    fn setting_num(&self, key: &str, fallback: f64) -> f64 {
        self.st.borrow().settings.get(key).and_then(Value::as_f64).unwrap_or(fallback)
    }

    // ---- hooking up what the user can click ------------------------------------------------- //
    fn connect(self: &Rc<Self>) {
        let ui = &self.ui;
        ui.on_power_clicked(|| with_app(|a| a.power_clicked()));
        ui.on_chart_resized(|| with_app(|a| a.render_chart()));
        ui.on_open_speed_test(|| with_app(|a| a.open_speed_test()));
        ui.on_open_traffic_check(|| with_app(|a| a.open_traffic_check()));
        ui.on_open_update(|| with_app(|a| a.open_update()));
        ui.on_open_more_apps(|| with_app(|a| a.open_more_apps()));
        ui.on_app_setup(|key| with_app(|a| a.open_guide(&key)));
        ui.on_app_toggled(|key, on| with_app(|a| a.set_app(&key, on)));
        ui.on_app_remove(|key| with_app(|a| a.ask_remove_app(&key)));
        ui.on_setting_bool(|key, on| with_app(|a| a.save(json!({ key.as_str(): on }))));
        ui.on_setting_text(|key, text| with_app(|a| a.text_setting(&key, &text)));
        ui.on_adapter_chosen(|slot, index| with_app(|a| a.adapter_chosen(slot as usize, index)));
        ui.on_add_extra(|| with_app(|a| a.add_extra(true)));
        ui.on_remove_extra(|i| {
            with_app(|a| {
                let mut extra = a.extras();
                if (i as usize) < extra.len() {
                    extra.remove(i as usize);
                }
                a.save(json!({ "extra": extra }));
                a.fill_adapters();
            })
        });
        ui.on_refresh_adapters(|| with_app(|a| a.refresh_adapters(|a| a.toast("Connection list refreshed", false))));
        ui.on_check_updates(|| with_app(|a| a.check_updates()));
        ui.on_dialog_primary_clicked(|| with_app(|a| a.dialog_primary()));
        ui.on_dialog_secondary_clicked(|| {
            with_app(|a| {
                let url = a.st.borrow().last.as_ref().map(|l| s(&l["update"], "url").to_string()).unwrap_or_default();
                shell::open_github_url(&url);
            })
        });
        ui.on_dialog_closed(|| with_app(|a| a.close_dialog()));
        ui.on_copy(|text| {
            with_app(|a| {
                if shell::copy_text(&text) {
                    a.toast(&format!("Copied {text}"), false);
                } else {
                    a.toast(&format!("Couldn't copy - type {text}"), true);
                }
            })
        });
        ui.on_step_action(|_| with_app(|a| a.autostart_from_guide()));
        ui.on_test_qbt(|| with_app(|a| a.test_qbt()));
        ui.on_more_clicked(|key| with_app(|a| a.more_clicked(&key)));
    }

    // ---- talking to the engine -------------------------------------------------------------- //
    fn init(self: &Rc<Self>) {
        self.call("get_init", vec![], |app, res| match res {
            Ok(init) => {
                {
                    let mut st = app.st.borrow_mut();
                    st.settings = init["settings"].as_object().cloned().unwrap_or_default();
                    st.adapters = list(&init, "adapters").to_vec();
                    st.ready = true;
                }
                app.fill_adapters();
                app.fill_settings();
                app.render_apps(&json!({}), false);
                if app.setting_str("eth_name").is_empty() || app.setting_str("hot_name").is_empty() {
                    app.ui.set_settings_open(true);
                }
                app.poll();
                app.poll_timer.start(TimerMode::Repeated, Duration::from_secs(1), || with_app(|a| a.poll()));
                if app.opts.shots.is_some() {
                    crate::shots::run(app);
                }
            }
            Err(e) => {
                app.cant_reach(&e);
                Timer::single_shot(Duration::from_secs(2), || {
                    with_app(|a| {
                        if !a.st.borrow().closing {
                            a.init();
                        }
                    })
                });
            }
        });
    }

    fn cant_reach(&self, error: &str) {
        self.ui.set_state_bold("Can't reach DualLink".into());
        self.ui.set_state_rest(format!("· {error}").into());
    }

    pub fn poll(self: &Rc<Self>) {
        let asked = {
            let mut st = self.st.borrow_mut();
            if st.polling || st.closing || self.ui.window().is_minimized() {
                return; // nothing to draw while minimised
            }
            st.polling = true;
            st.changes
        };
        self.call("status", vec![], move |app, res| {
            app.st.borrow_mut().polling = false;
            match res {
                Ok(status) => {
                    let current = app.st.borrow().changes == asked;
                    app.render(status, current);
                }
                Err(e) => app.cant_reach(&e),
            }
        });
    }

    /// Change settings: shown at once, sent to the engine a moment later (so typing doesn't send every key).
    pub fn save(self: &Rc<Self>, patch: Value) {
        {
            let mut st = self.st.borrow_mut();
            st.changes += 1;
            for (k, v) in patch.as_object().into_iter().flatten() {
                st.settings.insert(k.clone(), v.clone());
                st.pending.insert(k.clone(), v.clone());
            }
        }
        self.fill_settings();
        self.render_apps_again();
        self.save_timer.start(TimerMode::SingleShot, Duration::from_millis(300), || with_app(|a| a.save_now(json!({}), |_| {})));
    }

    /// Send the changes now; `then` runs when the engine has them.
    pub fn save_now(self: &Rc<Self>, patch: Value, then: impl FnOnce(&Rc<App>) + Send + 'static) {
        self.save_timer.stop();
        let pending = {
            let mut st = self.st.borrow_mut();
            for (k, v) in patch.as_object().into_iter().flatten() {
                st.settings.insert(k.clone(), v.clone());
                st.pending.insert(k.clone(), v.clone());
            }
            st.changes += 1;
            std::mem::take(&mut st.pending)
        };
        if pending.is_empty() {
            then(self);
            return;
        }
        self.call("save_settings", vec![Value::Object(pending)], move |app, res| {
            let error = match res {
                Ok(saved) => {
                    let mut st = app.st.borrow_mut();
                    st.changes += 1; // statuses asked for before this are out of date
                    let mut settings = saved["settings"].as_object().cloned().unwrap_or_default();
                    settings.extend(st.pending.clone()); // typed since: still ours to send
                    st.settings = settings;
                    s(&saved, "error").to_string()
                }
                Err(e) => e,
            };
            if !error.is_empty() {
                app.toast(&error, true);
                app.fill_adapters();
                app.fill_settings();
                app.render_apps_again();
            }
            then(app);
        });
    }

    fn save_pending(&self) -> bool {
        !self.st.borrow().pending.is_empty()
    }

    pub fn power_clicked(self: &Rc<Self>) {
        self.ui.set_power_busy(true);
        self.save_now(json!({}), |app| {
            let turn_on = !app.st.borrow().running;
            app.call("power", vec![json!(turn_on)], |app, res| {
                match res {
                    Ok(r) => {
                        let msg = s(&r, "message");
                        if !b(&r, "ok") {
                            app.toast(msg, true);
                            if msg.contains("Settings") {
                                app.ui.set_settings_open(true);
                            }
                        } else {
                            app.toast(msg, false);
                        }
                    }
                    Err(e) => app.toast(&e, true),
                }
                let asked = app.st.borrow().changes;
                app.call("status", vec![], move |app, res| {
                    app.ui.set_power_busy(false);
                    if let Ok(status) = res {
                        let current = app.st.borrow().changes == asked;
                        app.render(status, current);
                    }
                });
            });
        });
    }

    // ---- showing the status ----------------------------------------------------------------- //
    /// `current` is false when the settings changed while the status was on its way: its app switches
    /// may show the old state, so they're left alone this time.
    pub fn render(self: &Rc<Self>, status: Value, current: bool) {
        let ui = &self.ui;
        let running = b(&status, "running");
        ui.set_power_on(running);
        let (num, unit) = fmt::speed(n(&status, "combined"));
        ui.set_speed_num(num.into());
        ui.set_speed_unit(unit.into());

        let links = list(&status, "links");
        let label = |l: &Value| match s(l, "kind") {
            "hot" => "hotspot".to_string(),
            "eth" => "Ethernet".to_string(),
            _ => s(l, "name").to_string(),
        };
        let up: Vec<String> = links.iter().filter(|l| b(l, "present")).map(label).collect();
        let down: Vec<String> = links.iter().filter(|l| !b(l, "present")).map(label).collect();
        let rest = if !running {
            "· press the button to combine your connections".to_string()
        } else if down.is_empty() && links.len() == 2 {
            "· using Ethernet and hotspot together".to_string()
        } else if down.is_empty() {
            format!("· using {} connections together", links.len())
        } else {
            let using = if up.is_empty() { "nothing".to_string() } else { up.join(" + ") };
            format!("· using {using} - waiting for {}", down.join(" and "))
        };
        ui.set_state_bold(if running { "On" } else { "Off" }.into());
        ui.set_state_rest(rest.into());

        let tiles: Vec<Tile> = links
            .iter()
            .enumerate()
            .map(|(i, l)| {
                let hot = s(l, "kind") == "hot";
                let waiting = running && !b(l, "present");
                let mut sub = if s(l, "adapter").is_empty() {
                    "Not chosen - pick it in Settings".to_string()
                } else if waiting {
                    "Not connected · joins automatically".to_string()
                } else if !running {
                    s(l, "adapter").to_string()
                } else {
                    fmt::plural(n(l, "connections"), "connection") + if b(l, "over_quota") { " · data limit reached" } else { "" }
                };
                let (used, quota) = (n(l, "down") + n(l, "up"), n(l, "quota"));
                let mut bar = -1.0;
                if hot && running && quota > 0.0 {
                    bar = (used / quota).min(1.0) as f32;
                    sub += &format!(" · {} of {}", fmt::size(used), fmt::size(quota));
                }
                Tile {
                    name: if hot { "Phone hotspot" } else { s(l, "name") }.into(),
                    sub: sub.into(),
                    speed: if running { fmt::speed_str(n(l, "speed")) } else { "-".into() }.into(),
                    color: link_color(i),
                    dim: waiting,
                    bar,
                }
            })
            .collect();
        sync(&self.tiles, tiles);

        ui.set_restart_note(running && n(&status, "port") != self.setting_num("port", 1080.0));
        ui.set_version_text(format!("DualLink {}", s(&status, "version")).into());
        let update = &status["update"];
        ui.set_update_label(if update.is_object() { format!("Update to {}", s(update, "version")) } else { String::new() }.into());

        let just_updated = b(&status, "just_updated") && !std::mem::replace(&mut self.st.borrow_mut().shown_updated, true);
        if just_updated {
            self.toast(&format!("DualLink is updated to {}", s(&status, "version")), false);
        }
        {
            let mut st = self.st.borrow_mut();
            st.running = running;
            st.last = Some(status.clone());
        }
        self.render_chart();
        self.render_apps(&status, current);
        let modal = self.st.borrow().modal.clone();
        match modal.as_str() {
            "qbt" if running && s(&status["qbt"], "state") == "connected" => self.mark_steps_done(3),
            "speed" => self.render_speed(&status["speedtest"]),
            "update" => self.render_update(&status),
            _ => {}
        }
    }

    /// The graph: each connection's speed over the last 90 seconds, stacked.
    fn render_chart(&self) {
        let (w, h) = (self.ui.get_chart_w() as f64, self.ui.get_chart_h() as f64);
        let histories: Vec<Vec<f64>> = self
            .st
            .borrow()
            .last
            .as_ref()
            .map(|last| list(last, "links").iter().map(|l| list(l, "history").iter().map(|v| v.as_f64().unwrap_or(0.0)).collect()).collect())
            .unwrap_or_default();
        let count = histories.iter().map(Vec::len).max().unwrap_or(0);
        if w <= 0.0 || h <= 0.0 || count < 2 {
            sync(&self.chart, Vec::new());
            return;
        }
        let mut lower = vec![0.0; count];
        let mut stacks = Vec::new();
        for hist in &histories {
            let upper: Vec<f64> = (0..count).map(|i| lower[i] + hist.get(i).copied().unwrap_or(0.0)).collect();
            stacks.push((lower, upper.clone()));
            lower = upper;
        }
        let top = lower.iter().copied().fold(0.0, f64::max);
        let max = fmt::nice_max(top);
        self.ui.set_chart_labels(strings([fmt::axis(max), fmt::axis(max / 2.0), "0".to_string()]));
        if top <= 0.0 {
            sync(&self.chart, Vec::new());
            return;
        }
        let x = |i: usize| w * i as f64 / (count - 1) as f64;
        let y = |v: f64| (h - 1.0) * (1.0 - v / max);
        let series = stacks
            .iter()
            .enumerate()
            .map(|(si, (lower, upper))| {
                let mut line = String::with_capacity(count * 14);
                for (i, v) in upper.iter().enumerate() {
                    line += &format!("{}{:.1} {:.1} ", if i == 0 { "M " } else { "L " }, x(i), y(*v));
                }
                let mut area = line.clone();
                for (i, v) in lower.iter().enumerate().rev() {
                    area += &format!("L {:.1} {:.1} ", x(i), y(*v));
                }
                area += "Z";
                Series { line: line.into(), area: area.into(), color: link_color(si) }
            })
            .collect();
        sync(&self.chart, series);
    }

    // ---- apps ------------------------------------------------------------------------------- //
    /// Is the app in the Apps list? (A new install starts with none; the user adds and removes them.)
    fn added(&self, key: &str) -> bool {
        self.st.borrow().settings.get("added_apps").and_then(Value::as_array).is_some_and(|l| l.iter().any(|x| x == key))
    }

    fn app_list_with(&self, key: &str, add: bool) -> Value {
        let st = self.st.borrow();
        let mut apps: Vec<Value> =
            st.settings.get("added_apps").and_then(Value::as_array).into_iter().flatten().filter(|x| *x != key).cloned().collect();
        if add {
            apps.push(json!(key));
        }
        Value::Array(apps)
    }

    pub fn render_apps_again(self: &Rc<Self>) {
        let last = self.st.borrow().last.clone().unwrap_or_else(|| json!({}));
        self.render_apps(&last, false);
    }

    fn render_apps(self: &Rc<Self>, status: &Value, current: bool) {
        let running = b(status, "running");
        let qbt_state = s(&status["qbt"], "state");
        let pending = self.save_pending();
        let mut rows = Vec::new();
        for a in &APPS {
            let live = &status["app_live"][a.key];
            let conns = n(live, "connections");
            let added = self.added(a.key);
            // the apps the user added, plus any other app that is using DualLink right now (so it isn't missed);
            // qBittorrent only when added: DualLink sets it up and puts it back by itself
            if !added && (a.key == "qbt" || conns <= 0.0) {
                continue;
            }
            let key = setting_key(a.key);
            let engine_on = if a.key == "qbt" { status.get("qbt_auto") } else { status["apps"].get(a.key) }.and_then(Value::as_bool);
            if let (true, false, Some(on)) = (current, pending, engine_on) {
                self.st.borrow_mut().settings.insert(key.to_string(), json!(on)); // changed somewhere else
            }
            let on = self.setting_bool(key);
            let programs: Vec<&str> = list(live, "programs").iter().filter_map(Value::as_str).take(3).collect();
            let who = if a.key == "other" && !programs.is_empty() { format!("{} · ", programs.join(", ")) } else { String::new() };
            let seen = n(&status["seen_apps"], a.key);
            let (mut text, mut tone, mut fix) = ("Off".to_string(), 0, false);
            if a.key == "qbt" && on {
                if !running {
                    text = "Ready - set up automatically when DualLink is on".into();
                } else if qbt_state == "connected" {
                    text = if conns > 0.0 { format!("✓ Connected · {} now", fmt::plural(conns, "connection")) } else { "✓ Connected · no transfers right now".into() };
                    tone = 1;
                } else if qbt_state == "auth" {
                    (text, tone, fix) = ("Login needed".into(), 2, true);
                } else {
                    (text, tone, fix) = ("Can't reach qBittorrent - needs setup".into(), 2, true);
                }
            } else if on {
                if !s(status, "standby_error").is_empty() {
                    (text, tone) = ("Proxy port is in use - change it in Settings".into(), 3);
                } else if conns > 0.0 {
                    text = if running {
                        format!("✓ {who}{} now", fmt::plural(conns, "connection"))
                    } else {
                        format!("✓ {who}{}assing straight through while DualLink is off", if who.is_empty() { "Working - p" } else { "p" })
                    };
                    tone = 1;
                } else if seen > 0.0 {
                    (text, tone) = (format!("✓ Set up · idle (last used {})", fmt::ago(seen)), 4);
                } else {
                    let name = if a.key == "other" { "the app" } else { a.name };
                    text = format!("Not seen yet - start a download in {name} to check the setup");
                }
            } else if conns > 0.0 && a.key != "qbt" {
                text = if added {
                    format!("{who}Using DualLink - switch on so it keeps working while DualLink is off")
                } else {
                    format!("{who}Using DualLink, but not in your apps - switch on to add it")
                };
                tone = 2;
            }
            rows.push(AppItem {
                key: a.key.into(),
                name: a.name.into(),
                badge: a.badge.into(),
                color: rgb(a.color),
                status: text.into(),
                tone,
                on,
                fix,
                removable: added,
            });
        }
        sync(&self.apps, rows);
    }

    fn set_app(self: &Rc<Self>, key: &str, on: bool) {
        let mut patch = json!({ setting_key(key): on });
        if on && !self.added(key) {
            patch["added_apps"] = self.app_list_with(key, true); // switching on adds it to the list
        }
        self.save(patch);
        if on {
            self.open_guide(key);
        }
    }

    // ---- settings --------------------------------------------------------------------------- //
    fn extras(&self) -> Vec<String> {
        let st = self.st.borrow();
        st.settings.get("extra").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_str).filter(|x| !x.is_empty()).map(String::from).collect()
    }

    fn adapter_index(&self, name: &str) -> i32 {
        self.st.borrow().adapters.iter().position(|a| s(a, "name") == name).map(|i| i as i32).unwrap_or(-1)
    }

    /// Present adapters that aren't used as a connection yet.
    fn free_adapters(&self) -> Vec<String> {
        let mut used = self.extras();
        used.push(self.setting_str("eth_name"));
        used.push(self.setting_str("hot_name"));
        let st = self.st.borrow();
        st.adapters.iter().filter(|a| b(a, "present")).map(|a| s(a, "name").to_string()).filter(|name| !used.contains(name)).collect()
    }

    fn fill_adapters(&self) {
        let ui = &self.ui;
        let labels: Vec<String> = self
            .st
            .borrow()
            .adapters
            .iter()
            .map(|a| if b(a, "present") { format!("{} - {}", s(a, "name"), s(a, "ip")) } else { format!("{} (not connected)", s(a, "name")) })
            .collect();
        ui.set_adapter_labels(strings(labels));
        let extra = self.extras();
        // (the lists forget their place when their items change: set it again afterwards)
        ui.set_extra_count(extra.len() as i32);
        ui.set_eth_index(self.adapter_index(&self.setting_str("eth_name")));
        ui.set_hot_index(self.adapter_index(&self.setting_str("hot_name")));
        ui.set_extra0_index(extra.first().map_or(-1, |name| self.adapter_index(name)));
        ui.set_extra1_index(extra.get(1).map_or(-1, |name| self.adapter_index(name)));
        let free = self.free_adapters();
        let hint = if extra.len() >= 2 {
            "That's the maximum of two extra connections.".to_string()
        } else if !free.is_empty() {
            format!("Available now: {}.", free.join(", "))
        } else if !extra.is_empty() {
            "You can add one more - connect it first, for example Wi-Fi or a second phone.".to_string()
        } else {
            "For example Wi-Fi or a second phone - connect it first, then add it here.".to_string()
        };
        ui.set_extra_hint(hint.into());
    }

    fn fill_settings(&self) {
        let ui = &self.ui;
        ui.set_s_auto(self.setting_bool("auto"));
        ui.set_s_recycle(self.setting_bool("recycle"));
        ui.set_s_on_hotspot(self.setting_bool("auto_on_hotspot"));
        ui.set_s_on_start(self.setting_bool("auto_on_start"));
        ui.set_s_tray(self.setting_bool("tray"));
        ui.set_s_autostart(self.setting_bool("autostart"));
        ui.set_s_updates(self.setting_bool("check_updates"));
        ui.set_qbt_reconnect(self.setting_bool("reconnect"));
        ui.set_share_eth(self.setting_num("share_eth", 1.0).to_string().into());
        ui.set_share_hot(self.setting_num("share_hot", 1.0).to_string().into());
        ui.set_quota(self.setting_num("quota_mb", 0.0).to_string().into());
        ui.set_port(self.setting_num("port", 1080.0).to_string().into());
    }

    /// A number or text field was changed.
    fn text_setting(self: &Rc<Self>, key: &str, text: &str) {
        let text = text.trim();
        let number = text.replace(',', ".").parse::<f64>().ok().filter(|v| v.is_finite());
        let value = match key {
            "share_eth" | "share_hot" => json!(number.unwrap_or(1.0).clamp(0.1, 1000.0)),
            "quota_mb" => json!(number.unwrap_or(0.0).clamp(0.0, 10_000_000.0)),
            "port" => json!(number.unwrap_or(1080.0).clamp(1024.0, 65535.0) as u16),
            _ => json!(text),
        };
        if self.st.borrow().settings.get(key) == Some(&value) {
            self.fill_settings(); // e.g. "abc" typed over the same number: show the real value again
            return;
        }
        self.save(json!({ key: value }));
    }

    fn adapter_chosen(self: &Rc<Self>, slot: usize, index: i32) {
        let Some(name) = self.st.borrow().adapters.get(index.max(0) as usize).map(|a| s(a, "name").to_string()) else { return };
        match slot {
            0 => self.save(json!({ "eth_name": name })),
            1 => self.save(json!({ "hot_name": name })),
            _ => {
                let mut extra = self.extras();
                if let Some(slot) = extra.get_mut(slot - 2) {
                    *slot = name;
                }
                self.save(json!({ "extra": extra }));
            }
        }
        self.fill_adapters();
    }

    fn refresh_adapters(self: &Rc<Self>, then: impl FnOnce(&Rc<App>) + Send + 'static) {
        self.call("refresh_adapters", vec![], move |app, res| match res {
            Ok(r) => {
                {
                    let mut st = app.st.borrow_mut();
                    let mut settings = r["settings"].as_object().cloned().unwrap_or_default();
                    settings.extend(st.pending.clone());
                    st.settings = settings;
                    st.adapters = list(&r, "adapters").to_vec();
                }
                app.fill_adapters();
                then(app);
            }
            Err(e) => app.toast(&e, true),
        });
    }

    fn add_extra(self: &Rc<Self>, look_again: bool) {
        match self.free_adapters().into_iter().next() {
            Some(pick) => {
                let mut extra = self.extras();
                extra.push(pick);
                self.save(json!({ "extra": extra }));
                self.fill_adapters();
            }
            // maybe it was just plugged in: look once more
            None if look_again => self.refresh_adapters(|app| app.add_extra(false)),
            None => self.toast("No other connection found. Connect Wi-Fi, a second phone or another network adapter, then try again.", true),
        }
    }

    pub fn check_updates(self: &Rc<Self>) {
        self.ui.set_checking_updates(true);
        self.call("check_updates", vec![], |app, res| {
            app.ui.set_checking_updates(false);
            match res {
                Err(e) => app.toast(&e, true),
                Ok(r) if !b(&r, "ok") => app.toast(s(&r, "message"), true),
                Ok(r) if r["update"].is_object() => {
                    app.ui.set_settings_open(false);
                    app.st.borrow_mut().last.get_or_insert_with(|| json!({}))["update"] = r["update"].clone();
                    app.open_update();
                }
                Ok(_) => app.toast("You have the latest version", false),
            }
        });
    }

    // ---- dialogs ---------------------------------------------------------------------------- //
    fn open_dialog(&self, kind: &str, layout: &str, title: &str, primary: &str, secondary: &str, close: &str) {
        self.st.borrow_mut().modal = kind.to_string();
        let ui = &self.ui;
        ui.set_dialog_title(title.into());
        ui.set_dialog_primary(primary.into());
        ui.set_dialog_primary_enabled(true);
        ui.set_dialog_secondary(secondary.into());
        ui.set_dialog_close(close.into());
        ui.set_dialog(layout.into());
    }

    pub fn close_dialog(&self) {
        self.st.borrow_mut().modal.clear();
        self.ui.set_dialog("".into());
    }

    pub fn dialog_primary(self: &Rc<Self>) {
        let (modal, key) = {
            let st = self.st.borrow();
            (st.modal.clone(), st.modal_app.clone())
        };
        match modal.as_str() {
            "remove" => {
                self.save(json!({ setting_key(&key): false, "added_apps": self.app_list_with(&key, false) }));
                self.close_dialog();
                self.toast(&if key == "other" { "Other app removed".to_string() } else { format!("{} removed", app_info(&key).name) }, false);
            }
            "update" => {
                self.ui.set_dialog_primary_enabled(false);
                self.call("start_update", vec![], |app, res| {
                    let error = match res {
                        Ok(r) if b(&r, "ok") => return,
                        Ok(r) => s(&r, "message").to_string(),
                        Err(e) => e,
                    };
                    app.ui.set_dialog_primary_enabled(true);
                    app.show_progress(0.0, &error, true);
                });
            }
            "speed" => self.call("start_speed_test", vec![], |app, res| match res {
                Ok(r) if b(&r, "ok") => app.render_speed(&json!({"running": true, "phase": ""})),
                Ok(r) => app.toast(s(&r, "message"), true),
                Err(e) => app.toast(&e, true),
            }),
            "check" => self.run_check(),
            _ => {}
        }
    }

    fn set_steps(&self, guide: &guides::Guide) {
        let steps: Vec<Step> = guide
            .steps
            .iter()
            .map(|st| Step {
                title: st.title.as_str().into(),
                note: st.note.into(),
                r#where: st.location.as_str().into(),
                bullets: ModelRc::new(VecModel::from(
                    st.lines
                        .iter()
                        .map(|l| Bullet { text: l.text.as_str().into(), codes: strings(l.code.clone()), tail: l.tail.into() })
                        .collect::<Vec<_>>(),
                )),
                done: false,
                action: st.action.into(),
                test: st.test,
            })
            .collect();
        self.steps.set_vec(steps);
        self.ui.set_guide_tip(guide.tip.into());
        self.ui.set_guide_foot(guide.foot.into());
    }

    fn mark_steps_done(&self, count: usize) {
        for i in 0..self.steps.row_count() {
            if let Some(mut step) = self.steps.row_data(i).filter(|st| st.done != (i < count)) {
                step.done = i < count;
                self.steps.set_row_data(i, step);
            }
        }
    }

    pub fn open_guide(self: &Rc<Self>, key: &str) {
        let ui = &self.ui;
        ui.set_guide_qbt(key == "qbt");
        self.st.borrow_mut().modal_app = key.to_string();
        if key == "qbt" {
            let url = self.setting_str("qbt_url");
            let web_port = url.rsplit(':').next().filter(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit())).unwrap_or("8080");
            self.set_steps(&guides::qbittorrent(web_port));
            ui.set_qbt_url(url.as_str().into());
            ui.set_qbt_user(self.setting_str("qbt_user").into());
            ui.set_qbt_pass(self.setting_str("qbt_pass").into());
            ui.set_qbt_more(false);
            ui.set_test_busy(false);
            ui.set_test_result("".into());
            self.open_dialog("qbt", "guide", "Set up qBittorrent", "", "", "Done");
            let connected = self.st.borrow().last.as_ref().is_some_and(|l| b(l, "running") && s(&l["qbt"], "state") == "connected");
            if connected {
                self.mark_steps_done(3);
            }
        } else {
            let port = self.setting_num("port", 1080.0).to_string();
            self.set_steps(&guides::manual(key, &port, self.setting_bool("autostart")));
            self.open_dialog("guide", "guide", &format!("Set up {}", app_info(key).name), "", "", "Done");
        }
    }

    /// "Start DualLink with Windows", pressed in a guide.
    fn autostart_from_guide(self: &Rc<Self>) {
        self.save_now(json!({ "autostart": true }), |app| {
            app.fill_settings();
            for i in 0..app.steps.row_count() {
                if let Some(mut step) = app.steps.row_data(i).filter(|st| !st.action.is_empty()) {
                    step.action = "".into();
                    app.steps.set_row_data(i, step);
                }
            }
            app.toast("DualLink will start with Windows", false);
        });
    }

    fn test_qbt(self: &Rc<Self>) {
        let ui = &self.ui;
        ui.set_test_busy(true);
        ui.set_test_result("Checking…".into());
        ui.set_test_tone(0);
        self.save_now(json!({}), |app| {
            app.call("test_qbt", vec![], |app, res| {
                let ui = &app.ui;
                ui.set_test_busy(false);
                if app.st.borrow().modal != "qbt" {
                    return;
                }
                let (ok, message) = match res {
                    Ok(r) => (b(&r, "ok"), s(&r, "message").to_string()),
                    Err(e) => (false, e),
                };
                ui.set_test_result(if ok { format!("✓ {message}") } else { message }.into());
                ui.set_test_tone(if ok { 1 } else { 3 });
                app.mark_steps_done(if ok { 2 } else { 0 });
                if ok && app.st.borrow().running {
                    app.call("retry_qbt", vec![], |app, res| {
                        if res.is_ok_and(|q| s(&q, "state") == "connected") {
                            app.toast("qBittorrent is now using DualLink", false);
                        }
                    });
                }
            });
        });
    }

    pub fn open_more_apps(&self) {
        let apps: Vec<MoreApp> = APPS
            .iter()
            .map(|a| MoreApp { key: a.key.into(), name: a.name.into(), badge: a.badge.into(), color: rgb(a.color), desc: a.desc.into(), added: self.added(a.key) })
            .collect();
        self.ui.set_more_apps(ModelRc::new(VecModel::from(apps)));
        self.open_dialog("more", "more", "Apps that can use DualLink", "", "", "Done");
    }

    fn more_clicked(self: &Rc<Self>, key: &str) {
        if self.added(key) {
            self.open_guide(key);
            return;
        }
        let key = key.to_string();
        let patch = json!({ setting_key(&key): true, "added_apps": self.app_list_with(&key, true) });
        self.save_now(patch, move |app| {
            app.render_apps_again();
            app.open_guide(&key);
        });
    }

    pub fn ask_remove_app(&self, key: &str) {
        let other = key == "other";
        let name = if other { "the other app" } else { app_info(key).name };
        let text = if key == "qbt" {
            "DualLink will stop setting up qBittorrent. If DualLink is on, qBittorrent gets its own proxy settings back right away.".to_string()
        } else {
            format!("If you set up {name} to use DualLink, set its proxy back to none in {name} too - otherwise its downloads stop working while DualLink is off.")
        };
        let ui = &self.ui;
        ui.set_body_text(text.into());
        ui.set_notes_text("".into());
        ui.set_body_tip("You can add it again any time with + Add an app.".into());
        ui.set_progress_shown(false);
        self.st.borrow_mut().modal_app = key.to_string();
        let title = if other { "Remove the other app?".to_string() } else { format!("Remove {}?", app_info(key).name) };
        self.open_dialog("remove", "remove", &title, "Remove", "", "Cancel");
    }

    // ---- update ----------------------------------------------------------------------------- //
    pub fn open_update(self: &Rc<Self>) {
        let Some(last) = self.st.borrow().last.clone().filter(|l| l["update"].is_object()) else { return };
        let ui = &self.ui;
        ui.set_body_text(format!("Version {} is available - you have {}.", s(&last["update"], "version"), s(&last, "version")).into());
        // can be empty: GitHub's notes are often only a changelog link
        ui.set_notes_text(fmt::clean_notes(s(&last["update"], "notes")).into());
        ui.set_body_tip("DualLink downloads the update, switches itself off (qBittorrent gets its own settings back), installs it and starts again. It takes about a minute.".into());
        ui.set_progress_shown(false);
        self.open_dialog("update", "update", "Update DualLink", "Update now", "Open download page", "Later");
        self.render_update(&last);
    }

    fn show_progress(&self, fraction: f64, text: &str, bad: bool) {
        let ui = &self.ui;
        ui.set_progress_shown(true);
        ui.set_progress(fraction as f32);
        ui.set_progress_text(text.into());
        ui.set_progress_bad(bad);
    }

    fn render_update(&self, status: &Value) {
        let st = &status["update_state"];
        let (done, total) = (n(st, "done"), n(st, "total"));
        match s(st, "phase") {
            "downloading" => {
                self.ui.set_dialog_primary_enabled(false);
                let text = if total > 0.0 { format!("Downloading… {} of {}", fmt::size(done), fmt::size(total)) } else { format!("Downloading… {}", fmt::size(done)) };
                self.show_progress(if total > 0.0 { done / total } else { 0.0 }, &text, false);
            }
            "installing" => {
                self.ui.set_dialog_primary_enabled(false);
                self.show_progress(1.0, "Installing - DualLink will close and start again in a moment.", false);
            }
            "error" => {
                self.ui.set_dialog_primary_enabled(true);
                self.ui.set_dialog_primary("Try again".into());
                self.show_progress(0.0, s(st, "message"), true);
            }
            _ => {}
        }
    }

    // ---- speed test ------------------------------------------------------------------------- //
    pub fn open_speed_test(self: &Rc<Self>) {
        self.open_dialog("speed", "speed", "Speed test", "Start test", "", "Close");
        let state = self.st.borrow().last.as_ref().map(|l| l["speedtest"].clone()).unwrap_or(Value::Null);
        self.render_speed(&state);
    }

    fn render_speed(&self, state: &Value) {
        let ui = &self.ui;
        let running = b(state, "running");
        ui.set_speed_running(running);
        ui.set_dialog_primary_enabled(!running);
        if running {
            ui.set_speed_status(format!("Testing {}… about 5 seconds per connection.", s(state, "phase")).into());
            return;
        }
        let result = &state["result"];
        let links = list(result, "links");
        let max = links.iter().map(|r| n(r, "bps")).fold(1.0, f64::max);
        let rows = links
            .iter()
            .enumerate()
            .map(|(i, r)| {
                let bps = n(r, "bps");
                SpeedRow {
                    label: s(r, "label").into(),
                    fraction: (bps / max) as f32,
                    color: link_color(i),
                    big: format!("{} Mbps", fmt::mbps(bps)).into(),
                    small: fmt::speed_str(bps).into(),
                    error: s(r, "error").into(),
                }
            })
            .collect();
        sync(&self.speed_rows, rows);
        if links.is_empty() {
            ui.set_speed_together("".into());
            ui.set_speed_foot("".into());
            return;
        }
        let total: f64 = links.iter().map(|r| n(r, "bps")).sum();
        ui.set_speed_together(format!("Together: up to {} Mbps ({})", fmt::mbps(total), fmt::speed_str(total)).into());
        ui.set_speed_foot(format!("Tested {}", fmt::ago(n(result, "ts"))).into());
        ui.set_dialog_primary("Test again".into());
    }

    // ---- traffic check ---------------------------------------------------------------------- //
    pub fn open_traffic_check(self: &Rc<Self>) {
        self.open_dialog("check", "check", "Traffic check", "Check again", "", "Close");
        self.run_check();
    }

    fn run_check(self: &Rc<Self>) {
        let ui = &self.ui;
        ui.set_check_busy(true);
        ui.set_dialog_primary_enabled(false);
        self.call("traffic_check", vec![], |app, res| {
            if app.st.borrow().modal != "check" {
                return;
            }
            let ui = &app.ui;
            ui.set_check_busy(false);
            ui.set_dialog_primary_enabled(true);
            let r = res.unwrap_or_else(|e| json!({"ok": false, "message": e}));
            if !b(&r, "ok") {
                ui.set_check_notes(ModelRc::new(VecModel::from(vec![Note { kind: "info".into(), text: s(&r, "message").into() }])));
                ui.set_check_done(false);
                return;
            }
            let notes: Vec<Note> = list(&r, "notes").iter().map(|x| Note { kind: s(x, "kind").into(), text: s(x, "text").into() }).collect();
            ui.set_check_notes(ModelRc::new(VecModel::from(notes)));
            ui.set_check_through(strings(list(&r, "through").iter().map(|a| format!("{} · {}", s(a, "name"), fmt::plural(n(a, "connections"), "connection")))));
            let rows: Vec<CheckRow> = list(&r, "links")
                .iter()
                .map(|l| {
                    let windows = l["windows"].as_f64();
                    let other = l["other"].as_f64();
                    let bad = other.is_some_and(|o| o > (256.0 * 1024.0f64).max(0.15 * windows.unwrap_or(0.0)));
                    let mut detail = format!("{}, {}", fmt::plural(n(l, "connections"), "connection"), fmt::plural(n(l, "failures"), "failed attempt"));
                    let apps = list(l, "direct_apps");
                    if bad && !apps.is_empty() {
                        let names: Vec<String> = apps.iter().take(6).map(|a| format!("{} ({})", s(a, "name"), n(a, "connections"))).collect();
                        detail += &format!(". Connecting directly: {}", names.join(", "));
                    }
                    let show = |v: Option<f64>| v.map(fmt::speed_str).unwrap_or_else(|| "-".into());
                    CheckRow {
                        label: s(l, "label").into(),
                        detail: detail.into(),
                        windows: show(windows).into(),
                        through: fmt::speed_str(n(l, "duallink")).into(),
                        around: show(other).into(),
                        bad,
                    }
                })
                .collect();
            ui.set_check_rows(ModelRc::new(VecModel::from(rows)));
            ui.set_check_foot(format!("Measured over {} s with Windows' own counters.", n(&r, "seconds")).into());
            ui.set_check_done(true);
        });
    }

    // ---- a short message at the bottom ------------------------------------------------------ //
    pub fn toast(&self, message: &str, error: bool) {
        if message.is_empty() {
            return;
        }
        let ui = &self.ui;
        ui.set_toast_text(message.into());
        ui.set_toast_error(error);
        ui.set_toast_shown(true);
        self.toast_timer.start(TimerMode::SingleShot, Duration::from_secs(if error { 6 } else { 3 }), || with_app(|a| a.ui.set_toast_shown(false)));
    }

    /// The window is closing: stop asking the engine for things.
    pub fn closing(&self) {
        self.st.borrow_mut().closing = true;
        self.poll_timer.stop();
        self.save_timer.stop();
    }

    /// Changes that were typed but not sent yet.
    pub fn take_pending(&self) -> Map<String, Value> {
        std::mem::take(&mut self.st.borrow_mut().pending)
    }
}
