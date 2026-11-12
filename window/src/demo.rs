//! A pretend engine with made-up data, for `--demo`: screenshots and trying the window out without the
//! real DualLink running.

use crate::engine::Engine;
use duallink_common::{now_ts, LockExt, VERSION};
use serde_json::{json, Map, Value};
use std::sync::Mutex;
use std::time::{Duration, Instant};

struct State {
    settings: Map<String, Value>,
    running: bool,
    checked_updates: bool,
    speed_result: Value,
    speed_ends: Option<Instant>,
}

pub struct DemoEngine {
    started: Instant,
    st: Mutex<State>,
}

impl DemoEngine {
    pub fn new() -> DemoEngine {
        let settings = json!({
            "eth_name": "Ethernet", "hot_name": "Ethernet 2", "extra": [],
            "auto": true, "recycle": true, "share_eth": 1.0, "share_hot": 1.0, "quota_mb": 0.0, "port": 1080,
            "qbt_auto": true, "qbt_url": "http://127.0.0.1:8080", "qbt_user": "", "qbt_pass": "", "reconnect": true,
            "tray": true, "autostart": true, "tray_hint_shown": true, "added_apps": ["qbt", "fdm"],
            "fdm": true, "jd": false, "idm": false, "firefox": false, "other": false,
            "auto_on_hotspot": true, "auto_on_start": false, "check_updates": true,
        });
        DemoEngine {
            started: Instant::now(),
            st: Mutex::new(State {
                settings: settings.as_object().cloned().unwrap_or_default(),
                running: true,
                checked_updates: false,
                speed_result: Value::Null,
                speed_ends: None,
            }),
        }
    }

    fn status(&self) -> Value {
        let mut st = self.st.lock_ok();
        let t = self.started.elapsed().as_secs_f64();
        if st.speed_ends.is_some_and(|end| Instant::now() >= end) {
            st.speed_ends = None;
            st.speed_result = json!({"ts": now_ts(), "links": [
                {"name": "Ethernet", "label": "Ethernet", "bps": 2_350_000.0, "error": null},
                {"name": "Ethernet 2", "label": "Hotspot", "bps": 4_100_000.0, "error": null}]});
        }
        let running = st.running;
        let mut links = Vec::new();
        let mut combined = 0.0;
        for (i, base) in [1.9 * 1048576.0, 3.3 * 1048576.0f64].into_iter().enumerate() {
            let hist: Vec<f64> = (0..90)
                .map(|k| {
                    let age = (89 - k) as f64;
                    let ramp = ((t + 60.0 - age) / 6.0).clamp(0.0, 1.0);
                    if running { (base * ramp * (1.0 + 0.12 * ((t - age) / (3.0 + i as f64) + i as f64).sin())).round() } else { 0.0 }
                })
                .collect();
            combined += hist[89];
            let (name, adapter, kind, ip, share) =
                [("Ethernet", "Ethernet", "eth", "192.168.8.5", 37), ("Hotspot", "Ethernet 2", "hot", "172.20.10.3", 63)][i];
            links.push(json!({
                "name": name, "adapter": adapter, "kind": kind,
                "ip": ip, "present": true, "connections": if running { 8 } else { 0 },
                "failures": 0, "share": share, "speed": hist[89], "down": 0, "up": 0, "quota": 0, "over_quota": false,
                "history": hist,
            }));
        }
        let phase = match st.speed_ends {
            Some(end) if end.saturating_duration_since(Instant::now()) > Duration::from_secs(2) => "Ethernet",
            Some(_) => "Hotspot",
            None => "",
        };
        let on = |k: &str| st.settings.get(k).and_then(Value::as_bool).unwrap_or(false);
        let n = |c: u32| if running { c } else { 0 };
        json!({
            "running": running, "combined": combined, "links": links,
            "app_live": {"qbt": {"connections": n(9), "programs": ["qbittorrent.exe"]}, "fdm": {"connections": n(8), "programs": ["fdm.exe"]}},
            "seen_apps": {"qbt": now_ts() - 400.0, "fdm": now_ts() - 400.0},
            "apps": {"fdm": on("fdm"), "jd": on("jd"), "idm": on("idm"), "firefox": on("firefox"), "other": on("other")},
            "qbt_auto": on("qbt_auto"), "qbt": {"state": if running { "connected" } else { "off" }, "message": ""},
            "moved": 3, "dht": running, "port": st.settings.get("port"),
            "speedtest": {"running": st.speed_ends.is_some(), "phase": phase, "result": st.speed_result},
            "update": if st.checked_updates { update() } else { Value::Null },
            "update_state": {"phase": "idle", "done": 0, "total": 0, "message": ""},
            "just_updated": false, "version": VERSION, "standby": false, "standby_error": null,
        })
    }
}

fn update() -> Value {
    json!({
        "version": "9.9.0", "url": "https://github.com/Rihardo3212/DualLink/releases",
        "notes": "## What's Changed\n* Faster start of the window\n* Fix the tray tooltip with three connections\n\n**Full Changelog**: https://github.com/Rihardo3212/DualLink/compare/v9.8.0...v9.9.0",
        "installer": null,
    })
}

impl Engine for DemoEngine {
    fn call(&self, name: &str, args: Vec<Value>) -> Result<Value, String> {
        std::thread::sleep(Duration::from_millis(if name == "traffic_check" { 1500 } else { 15 }));
        let settings = || Value::Object(self.st.lock_ok().settings.clone());
        Ok(match name {
            "ping" | "window_closed" => json!({}),
            "get_init" | "refresh_adapters" => json!({"settings": settings(), "adapters": [
                {"name": "Ethernet", "ip": "192.168.8.5", "present": true},
                {"name": "Ethernet 2", "ip": "172.20.10.3", "present": true},
                {"name": "Wi-Fi", "ip": "192.168.1.23", "present": true}]}),
            "save_settings" => {
                if let Some(patch) = args.first().and_then(Value::as_object) {
                    self.st.lock_ok().settings.extend(patch.clone());
                }
                json!({"settings": settings(), "error": null})
            }
            "power" => {
                self.st.lock_ok().running = args.first().and_then(Value::as_bool).unwrap_or(false);
                json!({"ok": true})
            }
            "status" => self.status(),
            "test_qbt" => json!({"ok": true, "message": "Connected to qBittorrent v5.0.2"}),
            "retry_qbt" => json!({"state": "connected", "message": ""}),
            "start_speed_test" => {
                self.st.lock_ok().speed_ends = Some(Instant::now() + Duration::from_secs(4));
                json!({"ok": true})
            }
            "check_updates" => {
                self.st.lock_ok().checked_updates = true;
                json!({"ok": true, "update": update()})
            }
            "start_update" => json!({"ok": false, "message": "This is a demo - nothing to install."}),
            "traffic_check" => json!({
                "ok": true, "seconds": 3.0, "refused_ipv6": 0, "failed": 0,
                "links": [
                    {"label": "Ethernet", "adapter": "Ethernet", "windows": 2_050_000.0, "duallink": 1_990_000.0, "other": 0.0,
                     "connections": 8, "failures": 2, "direct_apps": []},
                    {"label": "Hotspot", "adapter": "Ethernet 2", "windows": 3_900_000.0, "duallink": 3_420_000.0, "other": 310_000.0,
                     "connections": 9, "failures": 31, "direct_apps": [{"name": "msedge.exe", "connections": 4}]}],
                "through": [{"name": "qbittorrent.exe", "connections": 9}, {"name": "fdm.exe", "connections": 8}],
                "notes": [{"kind": "warn", "text": "302.7 KiB/s on hotspot isn't going through DualLink. Programs connecting over it directly: msedge.exe."}],
            }),
            _ => return Err(format!("Unknown request '{name}'.")),
        })
    }
}
