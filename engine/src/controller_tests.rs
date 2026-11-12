use crate::controller::*;
use crate::diag::{Counts, Snapshot};
use crate::net::Adapter;
use duallink_common::{now_ts, LockExt};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::net::Ipv4Addr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::Duration;

static N: AtomicU32 = AtomicU32::new(0);

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

fn adapter(name: &str, ip: [u8; 4]) -> Adapter {
    let ip = Ipv4Addr::from(ip);
    Adapter { name: name.into(), ip, ip6: None, hotspot: crate::net::looks_like_hotspot(name, ip) }
}

fn eth() -> Adapter {
    adapter("Ethernet", [127, 0, 0, 1])
}

fn phone() -> Adapter {
    adapter("Phone USB", [127, 0, 0, 2])
}

#[derive(Default)]
struct Seen {
    notes: Mutex<Vec<String>>,
    changes: AtomicU32,
}

impl Hooks for Seen {
    fn notify(&self, title: &str, _msg: &str) {
        self.notes.lock_ok().push(title.to_string());
    }
    fn changed(&self) {
        self.changes.fetch_add(1, Relaxed);
    }
}

struct Rig {
    ctl: Arc<Controller>,
    adapters: Arc<Mutex<Vec<Adapter>>>,
    seen: Arc<Seen>,
    path: PathBuf,
}

impl Drop for Rig {
    fn drop(&mut self) {
        self.ctl.shutdown();
        let _ = std::fs::remove_file(&self.path);
    }
}

fn rig_with(cfg: Value, adapters: Vec<Adapter>, tune: impl FnOnce(&mut Deps)) -> Rig {
    let path = std::env::temp_dir().join(format!("duallink-cfg-{}-{}.json", std::process::id(), N.fetch_add(1, Relaxed)));
    let mut cfg = cfg;
    if cfg.get("port").is_none() {
        cfg["port"] = json!(free_port());
    }
    std::fs::write(&path, cfg.to_string()).unwrap();
    let adapters = Arc::new(Mutex::new(adapters));
    let shared = adapters.clone();
    let mut deps = Deps {
        scan: Box::new(move || shared.lock_ok().clone()),
        clients: Box::new(|_, _| Counts::new()),
        rx_bytes: Box::new(HashMap::new),
        connections: Box::new(|_, _| Snapshot::default()),
        speed_url: String::new(),
        speed_seconds: 1.0,
        check_seconds: 0.05,
    };
    tune(&mut deps);
    let ctl = Controller::new(path.clone(), deps);
    let seen = Arc::new(Seen::default());
    ctl.set_hooks(seen.clone());
    Rig { ctl, adapters, seen, path }
}

fn rig(cfg: Value, adapters: Vec<Adapter>) -> Rig {
    rig_with(cfg, adapters, |_| {})
}

fn chosen() -> Value {
    json!({"eth_name": "Ethernet", "hot_name": "Phone USB"})
}

#[test]
fn a_new_install_has_no_apps_and_sane_defaults() {
    let r = rig(json!({}), vec![]);
    let s = r.ctl.public_settings();
    assert_eq!(s["added_apps"], json!([]));
    assert_eq!(s["qbt_auto"], false);
    assert_eq!(s["auto"], true);
    assert_eq!(s["auto_on_hotspot"], true);
    assert!(s.get("qbt_saved").is_none() && s.get("learned").is_none(), "internal keys stay inside");
    assert!(!r.ctl.proxy.is_up(), "nothing to pass through: no proxy while off");
}

#[test]
fn settings_are_checked() {
    let r = rig(chosen(), vec![eth(), phone()]);
    let c = &r.ctl;
    assert!(c.update_settings(&json!({"hot_name": "Ethernet"})).unwrap_err().contains("two different"));
    assert!(c.update_settings(&json!({"extra": ["Ethernet"]})).unwrap_err().contains("only be used once"));
    assert!(c.update_settings(&json!({"quota_mb": "lots"})).unwrap_err().contains("valid numbers"));
    assert!(c.update_settings(&json!({"port": 70000})).unwrap_err().contains("port"));
    assert!(c.update_settings(&json!({"auto": "yes"})).unwrap_err().contains("valid"));
    assert_eq!(c.cfg().hot_name, "Phone USB", "a refused change changes nothing");

    let s = c.update_settings(&json!({"quota_mb": -5, "share_eth": 0, "share_hot": "2.5", "extra": ["Wi-Fi", "", "A", "B"],
                                      "qbt_saved": {"x": 1}, "nonsense": 1})).unwrap();
    assert_eq!(s["quota_mb"], 0.0);
    assert_eq!(s["share_eth"], 1.0);
    assert_eq!(s["share_hot"], 2.5);
    assert_eq!(s["extra"], json!(["Wi-Fi", "A"]));
    assert!(c.cfg().qbt_saved.is_none(), "the window can't set internal keys");
    assert_eq!(c.proxy.links().len(), 4);
    c.update_settings(&json!({"extra": []})).unwrap();
    assert_eq!(c.proxy.links().len(), 2);

    // saved to disk and read back
    assert_eq!(load_cfg(&r.path).share_hot, 2.5);
}

#[test]
fn switching_an_app_on_adds_it_to_the_list() {
    let r = rig(chosen(), vec![]);
    let s = r.ctl.update_settings(&json!({"jd": true, "added_apps": ["fdm", "fdm", "bogus"]})).unwrap();
    assert_eq!(s["added_apps"], json!(["fdm", "jd"]));
    // an app set up by hand keeps the proxy up (pass-through) while DualLink is off
    assert!(r.ctl.proxy.is_up());
    assert!(r.ctl.proxy.passthrough.load(Relaxed));
    assert_eq!(r.ctl.status()["standby"], true);
    r.ctl.update_settings(&json!({"jd": false})).unwrap();
    assert!(!r.ctl.proxy.is_up());
}

#[test]
fn first_run_guesses_the_connections() {
    let r = rig(json!({}), vec![adapter("vEthernet (WSL)", [10, 1, 1, 1]), eth(), adapter("Wi-Fi", [192, 168, 1, 9])]);
    let list = r.ctl.adapters();
    assert_eq!(list.as_array().unwrap().len(), 3);
    assert_eq!(r.ctl.cfg().eth_name, "Ethernet");
    assert_eq!(r.ctl.cfg().hot_name, "Wi-Fi");
    // a saved choice stays, and stays visible, when it's unplugged
    r.adapters.lock_ok().retain(|a| a.name != "Wi-Fi");
    let list = r.ctl.adapters();
    assert_eq!(r.ctl.cfg().hot_name, "Wi-Fi");
    let gone = list.as_array().unwrap().iter().find(|a| a["name"] == "Wi-Fi").unwrap();
    assert_eq!(gone["present"], false);
}

#[test]
fn power_needs_connections() {
    let r = rig(json!({}), vec![eth(), phone()]);
    assert!(r.ctl.power_on()["message"].as_str().unwrap().contains("Settings"));
    r.ctl.update_settings(&chosen()).unwrap();
    r.adapters.lock_ok().clear();
    assert!(r.ctl.power_on()["message"].as_str().unwrap().contains("Neither"));
    assert!(!r.ctl.running());
}

#[test]
fn on_and_off() {
    let r = rig(chosen(), vec![eth(), phone()]);
    let c = &r.ctl;
    assert_eq!(c.power_on(), json!({"ok": true}));
    assert!(c.running() && c.proxy.is_up() && !c.proxy.passthrough.load(Relaxed));
    assert_eq!(c.proxy.port(), c.cfg().port);
    let s = c.status();
    assert_eq!(s["running"], true);
    assert_eq!(s["links"][0]["kind"], "eth");
    assert_eq!(s["links"][1]["ip"], "127.0.0.2");
    assert_eq!(s["links"][0]["share"], 50.0);
    assert_eq!(s["links"][0]["history"].as_array().unwrap().len(), 90);
    assert_eq!(s["qbt"]["state"], "disabled");
    assert_eq!(c.power_off(), json!({"ok": true}));
    assert!(!c.running() && !c.proxy.is_up());
    assert_eq!(r.seen.changes.load(Relaxed), 2);
    assert_eq!(c.status()["links"][0]["share"], 0.0);
}

#[test]
fn on_with_one_connection_says_so_and_adds_the_other_later() {
    let r = rig(chosen(), vec![eth()]);
    let c = &r.ctl;
    assert!(c.power_on()["message"].as_str().unwrap().contains("isn't connected yet"));
    assert_eq!(c.status()["links"][1]["present"], false);
    assert_eq!(r.seen.notes.lock_ok().as_slice(), ["DualLink is on"]);

    r.adapters.lock_ok().push(phone());
    c.check_adapters();
    assert_eq!(c.proxy.links()[1].ip(), Some(Ipv4Addr::new(127, 0, 0, 2)));
    assert_eq!(r.seen.notes.lock_ok().last().unwrap(), "Hotspot connected");

    r.adapters.lock_ok()[1].ip = Ipv4Addr::new(127, 0, 0, 3);
    c.check_adapters();
    assert_eq!(r.seen.notes.lock_ok().last().unwrap(), "Hotspot got a new address");

    r.adapters.lock_ok().remove(0);
    c.check_adapters();
    assert_eq!(r.seen.notes.lock_ok().last().unwrap(), "Ethernet disconnected");
    assert!(!c.proxy.links()[0].usable());
}

#[test]
fn the_phone_is_found_again_under_another_name() {
    let r = rig(chosen(), vec![eth(), phone()]);
    let c = &r.ctl;
    c.power_on();
    *r.adapters.lock_ok() = vec![eth()];
    c.check_adapters();
    r.adapters.lock_ok().push(adapter("Ethernet 5", [172, 20, 10, 2]));
    c.check_adapters();
    assert_eq!(c.cfg().hot_name, "Ethernet 5");
    assert_eq!(c.proxy.links()[1].ip(), Some(Ipv4Addr::new(172, 20, 10, 2)));
}

#[test]
fn turns_on_when_the_hotspot_connects() {
    let r = rig(chosen(), vec![eth(), phone()]);
    let c = &r.ctl;
    c.check_auto_on();
    assert!(!c.running(), "it was already there: the user wants DualLink off");
    *r.adapters.lock_ok() = vec![eth()];
    c.check_auto_on();
    r.adapters.lock_ok().push(phone());
    c.check_auto_on();
    assert!(c.running());
    assert_eq!(r.seen.notes.lock_ok().last().unwrap(), "DualLink turned on");
}

#[test]
fn manual_shares_and_the_data_limit() {
    let r = rig(chosen(), vec![eth(), phone()]);
    let c = &r.ctl;
    c.update_settings(&json!({"auto": false, "share_eth": 3, "share_hot": 1, "quota_mb": 1})).unwrap();
    c.power_on();
    let links = c.proxy.links();
    assert_eq!(c.status()["links"][0]["share"], 75.0);
    links[1].down.store(2 * 1024 * 1024, Relaxed);
    c.sample();
    c.sample();
    assert_eq!(r.seen.notes.lock_ok().iter().filter(|n| *n == "Hotspot data limit reached").count(), 1);
    assert_eq!(c.status()["links"][1]["over_quota"], true);
    assert_eq!(c.proxy.order().len(), 1);
    assert!(c.status()["links"][1]["history"][89].as_f64().unwrap() == 0.0);
}

#[test]
fn starts_with_remembered_speeds_or_the_speed_test() {
    let learned = json!({"Ethernet": {"per_conn": 100000.0, "ts": now_ts()}, "Phone USB": {"per_conn": 300000.0, "ts": now_ts()}});
    let mut cfg = chosen();
    cfg["learned"] = learned;
    let r = rig(cfg, vec![eth(), phone()]);
    r.ctl.power_on();
    assert_eq!(r.ctl.status()["links"][1]["share"], 75.0);
    drop(r);

    let mut cfg = chosen();
    cfg["learned"] = json!({"Ethernet": {"per_conn": 100000.0, "ts": 1000.0}}); // too old, and only one link
    cfg["speedtest"] = json!({"ts": now_ts(), "links": [
        {"name": "Ethernet", "label": "Ethernet", "bps": 9e6, "error": null},
        {"name": "Phone USB", "label": "Hotspot", "bps": 1e6, "error": null}]});
    let r = rig(cfg, vec![eth(), phone()]);
    r.ctl.power_on();
    assert_eq!(r.ctl.status()["links"][0]["share"], 85.0, "the slower link keeps its minimum share");
}

#[test]
fn measured_speeds_are_remembered() {
    let r = rig(chosen(), vec![eth(), phone()]);
    let c = &r.ctl;
    c.power_on();
    for (l, v) in c.proxy.links().iter().zip([5e5, 2e5]) {
        let mut ctl = l.ctl();
        ctl.per_conn = v;
        ctl.measured_at = Some(std::time::Instant::now());
    }
    c.power_off();
    assert_eq!(load_cfg(&r.path).learned["Phone USB"].per_conn, 2e5);
}

#[test]
fn qbittorrent_is_set_up_and_put_back() {
    let (url, log) = crate::qbt::tests::fake_qbt(false);
    let mut cfg = chosen();
    cfg["qbt_auto"] = json!(true);
    cfg["qbt_url"] = json!(url);
    let r = rig(cfg, vec![eth(), phone()]);
    let c = &r.ctl;
    assert_eq!(c.test_qbt()["message"], "Connected to qBittorrent v5.0.2");
    c.power_on();
    assert_eq!(c.status()["qbt"]["state"], "connected");
    assert_eq!(c.cfg().qbt_saved.as_ref().unwrap()["proxy_port"], 8080);
    assert_eq!(load_cfg(&r.path).qbt_saved.unwrap()["proxy_type"], "None", "saved on disk for crash recovery");
    c.power_off();
    assert!(c.cfg().qbt_saved.is_none());
    let sets: Vec<String> = log.lock_ok().iter().filter(|l| l.contains("setPreferences")).cloned().collect();
    assert_eq!(sets.len(), 2);
    assert!(sets[0].contains("SOCKS5") && sets[1].contains("None"));

    // removing qBittorrent while on gives its settings back right away
    c.power_on();
    c.update_settings(&json!({"qbt_auto": false})).unwrap();
    for _ in 0..100 {
        if c.cfg().qbt_saved.is_none() {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(c.cfg().qbt_saved.is_none());
    assert_eq!(c.status()["qbt"]["state"], "disabled");
}

#[test]
fn qbittorrent_problems_are_reported() {
    let mut cfg = chosen();
    cfg["qbt_auto"] = json!(true);
    cfg["qbt_url"] = json!("http://127.0.0.1:1");
    let r = rig(cfg, vec![eth(), phone()]);
    assert_eq!(r.ctl.test_qbt()["kind"], "unreachable");
    r.ctl.power_on();
    assert_eq!(r.ctl.status()["qbt"]["state"], "unreachable");
    assert_eq!(r.ctl.retry_qbt()["state"], "unreachable");
}

#[test]
fn leftover_qbittorrent_settings_are_restored_at_start() {
    let (url, log) = crate::qbt::tests::fake_qbt(false);
    let r = rig(json!({"qbt_url": url, "qbt_saved": {"proxy_port": 4321}}), vec![]);
    for _ in 0..200 {
        if r.ctl.cfg().qbt_saved.is_none() {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(r.ctl.cfg().qbt_saved.is_none());
    assert!(log.lock_ok().iter().any(|l| l.contains("4321")));
    // and the uninstaller's clean-up does the same from the file
    std::fs::write(&r.path, json!({"qbt_url": url, "qbt_saved": {"proxy_port": 999}}).to_string()).unwrap();
    cleanup(&r.path);
    assert!(load_cfg(&r.path).qbt_saved.is_none());
    assert!(log.lock_ok().iter().any(|l| l.contains("999")));
}

#[test]
fn apps_by_hand_pass_straight_through_while_off() {
    let mut cfg = chosen();
    cfg["fdm"] = json!(true);
    let r = rig(cfg, vec![eth(), phone()]);
    let c = &r.ctl;
    assert!(c.proxy.is_up() && c.proxy.passthrough.load(Relaxed));
    c.power_on();
    assert!(!c.proxy.passthrough.load(Relaxed));
    c.power_off();
    assert!(c.proxy.is_up() && c.proxy.passthrough.load(Relaxed), "the app keeps downloading");
    c.shutdown();
    assert!(!c.proxy.is_up());
}

#[test]
fn a_busy_port_is_reported() {
    let taken = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let mut cfg = chosen();
    cfg["port"] = json!(taken.local_addr().unwrap().port());
    let r = rig(cfg, vec![eth(), phone()]);
    let res = r.ctl.power_on();
    assert_eq!(res["ok"], false);
    assert!(res["message"].as_str().unwrap().contains("Could not listen on port"));
}

#[test]
fn apps_are_recognised_and_remembered() {
    let live = classify_clients(&[("qBittorrent.exe".to_string(), 9), ("javaw.exe".to_string(), 2), ("curl.exe".to_string(), 1), ("wget.exe".to_string(), 1)].into());
    assert_eq!(live["qbt"]["connections"], 9);
    assert_eq!(live["jd"]["programs"], json!(["javaw.exe"]));
    assert_eq!(live["other"], json!({"connections": 2, "programs": ["curl.exe", "wget.exe"]}));
    assert_eq!(live["fdm"]["connections"], 0);

    let mut cfg = chosen();
    cfg["fdm"] = json!(true);
    let r = rig_with(cfg, vec![], |d| d.clients = Box::new(|_, _| [("fdm.exe".to_string(), 8)].into()));
    r.ctl.check_apps();
    let s = r.ctl.status();
    assert_eq!(s["app_live"]["fdm"]["connections"], 8);
    assert!(s["seen_apps"]["fdm"].as_f64().unwrap() > 0.0);
    assert!(load_cfg(&r.path).seen_apps.contains_key("fdm"));
}

#[test]
fn speed_test_runs_over_each_connection() {
    let url = crate::speedtest::tests::firehose();
    let r = rig_with(chosen(), vec![eth()], |d| d.speed_url = url);
    let c = &r.ctl;
    assert_eq!(c.start_speed_test(), json!({"ok": true}));
    assert_eq!(c.status()["speedtest"]["running"], true);
    for _ in 0..200 {
        if c.status()["speedtest"]["running"] == false {
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let res = c.status()["speedtest"]["result"].clone();
    assert!(res["links"][0]["bps"].as_f64().unwrap() > 0.0);
    assert_eq!(res["links"][1]["error"], "Hotspot isn't connected.");
    r.adapters.lock_ok().clear();
    assert_eq!(c.start_speed_test()["ok"], false);
}

#[test]
fn traffic_check_points_at_programs_going_around() {
    let rx = Arc::new(AtomicU32::new(0));
    let rx2 = rx.clone();
    let r = rig_with(chosen(), vec![eth(), phone()], move |d| {
        d.rx_bytes = Box::new(move || {
            let n = rx2.fetch_add(1, Relaxed) as u64;
            [("Ethernet".to_string(), 1000), ("Phone USB".to_string(), n * 5_000_000)].into()
        });
        d.connections = Box::new(|_, _| Snapshot {
            through: [("qbittorrent.exe".to_string(), 9)].into(),
            direct: [("Phone USB".to_string(), [("msedge.exe".to_string(), 4), ("qbittorrent.exe".to_string(), 1)].into())].into(),
        });
    });
    let c = &r.ctl;
    let off = c.traffic_check();
    assert_eq!(off["notes"][0]["kind"], "info");
    c.power_on();
    let res = c.traffic_check();
    assert_eq!(res["ok"], true);
    assert_eq!(res["through"][0], json!({"name": "qbittorrent.exe", "connections": 9}));
    assert_eq!(res["links"][0]["other"], 0.0);
    assert!(res["links"][1]["other"].as_f64().unwrap() > 1e6);
    assert_eq!(res["links"][1]["direct_apps"][0]["name"], "msedge.exe");
    let notes: Vec<&str> = res["notes"].as_array().unwrap().iter().map(|n| n["text"].as_str().unwrap()).collect();
    assert!(notes[0].contains("on hotspot isn't going through DualLink. Programs connecting over it directly: msedge.exe, qbittorrent.exe."));
    assert!(notes[1].contains("qbittorrent.exe sends 9 connections through DualLink but also connects directly 1 time,"));
}

#[test]
fn traffic_check_says_when_all_is_well_or_unavailable() {
    let r = rig(chosen(), vec![eth(), phone()]);
    assert_eq!(r.ctl.traffic_check()["message"], "The traffic check needs Windows.");
    drop(r);
    let r = rig_with(chosen(), vec![eth(), phone()], |d| {
        d.rx_bytes = Box::new(|| [("Ethernet".to_string(), 5)].into());
        d.connections = Box::new(|_, _| Snapshot { through: [("fdm.exe".to_string(), 1)].into(), direct: HashMap::new() });
    });
    r.ctl.power_on();
    let res = r.ctl.traffic_check();
    assert_eq!(res["notes"], json!([{"kind": "good", "text": "Everything matches: your downloads are going through DualLink."}]));
    assert_eq!(res["links"][1]["windows"], Value::Null);
}

#[test]
fn update_checks_announce_a_new_version_once() {
    let r = rig(json!({}), vec![]);
    let c = &r.ctl;
    assert_eq!(c.check_updates_with(|| Err("offline".into()))["ok"], false);
    let res = c.check_updates_with(|| Ok(crate::updates::tests::release("v99.0.0")));
    assert_eq!(res["update"]["version"], "99.0.0");
    c.check_updates_with(|| Ok(crate::updates::tests::release("v99.0.0")));
    assert_eq!(r.seen.notes.lock_ok().as_slice(), ["DualLink 99.0.0 is available"]);
    assert_eq!(c.status()["update"]["installer"]["name"], "DualLink-Setup-99.0.0.exe");
    assert_eq!(c.check_updates_with(|| Ok(crate::updates::tests::release("v0.1.0")))["update"], Value::Null);
    assert_eq!(c.start_update()["ok"], false);
}

#[test]
fn turns_on_at_start_when_asked_or_after_an_update() {
    let mut cfg = chosen();
    cfg["auto_on_start"] = json!(true);
    let r = rig(cfg, vec![eth(), phone()]);
    r.ctl.startup();
    assert!(r.ctl.running());
    drop(r);

    let mut cfg = chosen();
    cfg["on_after_update"] = json!(true);
    let r = rig(cfg.clone(), vec![eth(), phone()]);
    r.ctl.startup();
    assert!(!r.ctl.running(), "a normal start doesn't resume");
    assert!(!load_cfg(&r.path).on_after_update, "and the flag is used up");
    drop(r);
    let r = rig(cfg, vec![eth(), phone()]);
    r.ctl.just_updated.store(true, Relaxed);
    r.ctl.startup();
    assert!(r.ctl.running());
}
