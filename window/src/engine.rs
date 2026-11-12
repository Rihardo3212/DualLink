//! Talking to DualLink's engine: POST http://127.0.0.1:port/api/name with the token from engine.json.

use duallink_common::{engine_file, instance, ENGINE_EXE, TOKEN_HEADER};
use serde_json::{json, Value};
use std::time::{Duration, Instant};

/// What the window asks of the engine. Calls block, so they are made off the window's thread.
pub trait Engine: Send + Sync {
    fn call(&self, name: &str, args: Vec<Value>) -> Result<Value, String>;
    /// The engine's process, so the window can close along with it.
    fn pid(&self) -> Option<u32> {
        None
    }
}

pub struct HttpEngine {
    agent: ureq::Agent,
    port: u16,
    token: String,
    pid: Option<u32>,
}

fn timeout(name: &str) -> Duration {
    Duration::from_secs(match name {
        "traffic_check" => 90,
        "test_qbt" | "check_updates" | "refresh_adapters" | "get_init" => 40,
        "power" | "retry_qbt" | "save_settings" => 60,
        "ping" | "window_closed" => 3,
        _ => 15,
    })
}

impl HttpEngine {
    fn load() -> Option<HttpEngine> {
        let info: Value = serde_json::from_str(&std::fs::read_to_string(engine_file()).ok()?).ok()?;
        Some(HttpEngine {
            // never through a system proxy: the engine is on this PC
            agent: ureq::AgentBuilder::new().try_proxy_from_env(false).build(),
            port: u16::try_from(info["port"].as_u64()?).ok()?,
            token: info["token"].as_str()?.to_string(),
            pid: info["pid"].as_u64().map(|p| p as u32),
        })
    }

    fn try_connect(wait: Duration) -> Option<HttpEngine> {
        let end = Instant::now() + wait;
        loop {
            if let Some(engine) = Self::load().filter(|e| e.call("ping", vec![]).is_ok()) {
                return Some(engine);
            }
            if Instant::now() >= end {
                return None;
            }
            std::thread::sleep(Duration::from_millis(250));
        }
    }

    /// The running engine, waiting a while if it's still starting. None if DualLink isn't running.
    pub fn find() -> Option<HttpEngine> {
        Self::try_connect(Duration::from_secs(3)).or_else(|| {
            instance::held(instance::ENGINE_MUTEX).then(|| Self::try_connect(Duration::from_secs(20))).flatten()
        })
    }
}

impl Engine for HttpEngine {
    fn call(&self, name: &str, args: Vec<Value>) -> Result<Value, String> {
        const GONE: &str = "DualLink's background engine isn't responding.";
        let url = format!("http://127.0.0.1:{}/api/{name}", self.port);
        let request = self.agent.post(&url).timeout(timeout(name)).set(TOKEN_HEADER, &self.token).set("Content-Type", "application/json");
        let text = match request.send_string(&json!({"args": args}).to_string()) {
            Ok(resp) => resp.into_string().map_err(|_| GONE.to_string())?,
            Err(ureq::Error::Status(_, resp)) => resp.into_string().map_err(|_| GONE.to_string())?,
            Err(_) => return Err(GONE.into()),
        };
        let mut reply: Value = serde_json::from_str(&text).map_err(|_| "Unexpected reply from DualLink's engine.".to_string())?;
        if reply["ok"] != true {
            return Err(reply["error"].as_str().filter(|e| !e.is_empty()).unwrap_or("DualLink's engine refused the request.").to_string());
        }
        Ok(reply["result"].take())
    }

    fn pid(&self) -> Option<u32> {
        self.pid
    }
}

/// Start DualLink.exe (next to this program); it opens a window by itself.
pub fn start_engine() -> bool {
    let Some(exe) = std::env::current_exe().ok().and_then(|p| Some(p.parent()?.join(ENGINE_EXE))) else { return false };
    exe.is_file() && std::process::Command::new(exe).spawn().is_ok()
}
