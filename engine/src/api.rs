//! The engine's local control API.
//!
//! The background engine (proxy, balancing, tray icon) and the window are separate programs. The window asks
//! the engine for everything over a small HTTP API on 127.0.0.1:
//!
//!     POST http://127.0.0.1:<port>/api/<name>      X-DualLink-Token: <token>      {"args": [...]}
//!     -> {"ok": true, "result": ...}   or   {"ok": false, "error": "..."}
//!
//! The port and token are in %APPDATA%\DualLink\engine.json, which only this Windows user can read. Requests
//! that come from a web page (they carry an Origin header) or name another host are refused, so websites
//! open in a browser can't reach it.

use duallink_common::{LockExt, TOKEN_HEADER, VERSION};
use serde_json::{json, Value};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, Mutex};

const MAX_BODY: usize = 1 << 20;

pub enum ApiError {
    /// No such request.
    Unknown,
}

pub type Handler = Arc<dyn Fn(&str, &[Value]) -> Result<Value, ApiError> + Send + Sync>;

pub struct ControlServer {
    server: Arc<tiny_http::Server>,
    pub port: u16,
    pub token: String,
    stopped: Arc<AtomicBool>,
    info_path: Mutex<Option<PathBuf>>,
}

/// A listener on 127.0.0.1 that no other program can share.
fn listen() -> std::io::Result<std::net::TcpListener> {
    let socket = socket2::Socket::new(socket2::Domain::IPV4, socket2::Type::STREAM, None)?;
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawSocket;
        use windows_sys::Win32::Networking::WinSock::{setsockopt, SOL_SOCKET, SO_EXCLUSIVEADDRUSE};
        let on: i32 = 1;
        // SAFETY: a valid socket and a 4-byte option value.
        unsafe {
            setsockopt(socket.as_raw_socket() as _, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, (&on as *const i32).cast(), 4);
        }
    }
    socket.bind(&std::net::SocketAddr::from(([127, 0, 0, 1], 0)).into())?;
    socket.listen(64)?;
    Ok(socket.into())
}

fn random_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).expect("the system's random numbers");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Compare without giving away how much matched.
fn same(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

impl ControlServer {
    pub fn start(handler: Handler) -> Result<Arc<ControlServer>, String> {
        let listener = listen().map_err(|e| e.to_string())?;
        let port = listener.local_addr().map_err(|e| e.to_string())?.port();
        let server = Arc::new(tiny_http::Server::from_listener(listener, None).map_err(|e| e.to_string())?);
        let this = Arc::new(ControlServer {
            server: server.clone(),
            port,
            token: random_token(),
            stopped: Arc::new(AtomicBool::new(false)),
            info_path: Mutex::new(None),
        });
        let me = this.clone();
        std::thread::Builder::new()
            .name("control-api".into())
            .spawn(move || {
                // one thread per request: some take a while (the traffic check, talking to qBittorrent)
                for rq in server.incoming_requests() {
                    let (me, handler) = (me.clone(), handler.clone());
                    std::thread::spawn(move || me.answer(rq, &handler));
                }
            })
            .map_err(|e| e.to_string())?;
        Ok(this)
    }

    fn answer(&self, mut rq: tiny_http::Request, handler: &Handler) {
        let (code, body) = self.dispatch(&mut rq, handler);
        let json = tiny_http::Header::from_bytes("Content-Type", "application/json").expect("a valid header");
        let no_store = tiny_http::Header::from_bytes("Cache-Control", "no-store").expect("a valid header");
        let resp = tiny_http::Response::from_string(body.to_string()).with_status_code(code).with_header(json).with_header(no_store);
        let _ = rq.respond(resp);
    }

    fn dispatch(&self, rq: &mut tiny_http::Request, handler: &Handler) -> (u16, Value) {
        let fail = |code: u16, error: &str| (code, json!({"ok": false, "error": error}));
        if *rq.method() != tiny_http::Method::Post {
            return fail(405, "Use POST.");
        }
        if rq.body_length().unwrap_or(0) > MAX_BODY {
            return fail(413, "Too large.");
        }
        let mut raw = Vec::new();
        if rq.as_reader().take(MAX_BODY as u64 + 1).read_to_end(&mut raw).is_err() || raw.len() > MAX_BODY {
            return fail(400, "Bad request.");
        }
        if self.stopped.load(Relaxed) {
            return fail(503, "DualLink is closing.");
        }
        let header = |name: &'static str| rq.headers().iter().find(|h| h.field.equiv(name)).map(|h| h.value.as_str().to_string());
        // browsers send Origin; DualLink's own window never does
        let host = header("Host").unwrap_or_default();
        let local = host == format!("127.0.0.1:{}", self.port) || host == format!("localhost:{}", self.port);
        let token = header(TOKEN_HEADER).unwrap_or_default();
        if header("Origin").is_some() || !local || !same(token.as_bytes(), self.token.as_bytes()) {
            return fail(403, "Forbidden.");
        }
        let name = rq.url().strip_prefix("/api/").unwrap_or("").to_string();
        let body: Value = if raw.is_empty() { json!({}) } else { serde_json::from_slice(&raw).unwrap_or(Value::Null) };
        let args = match (&body, body.get("args")) {
            (Value::Object(_), None) => Vec::new(),
            (Value::Object(_), Some(Value::Array(a))) => a.clone(),
            _ => return fail(400, "Bad request."),
        };
        let handler = handler.clone();
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| handler(&name, &args))) {
            Ok(Ok(result)) => (200, json!({"ok": true, "result": result})),
            Ok(Err(ApiError::Unknown)) => fail(404, &format!("Unknown request '{name}'.")),
            Err(_) => fail(500, "Something went wrong inside DualLink."),
        }
    }

    /// Write engine.json so a window can find this engine.
    pub fn publish(&self, path: &Path) {
        let info = json!({"port": self.port, "token": self.token, "pid": std::process::id(), "version": VERSION});
        let tmp = path.with_extension("json.tmp");
        if std::fs::write(&tmp, info.to_string()).is_ok() && std::fs::rename(&tmp, path).is_ok() {
            *self.info_path.lock_ok() = Some(path.to_path_buf());
        }
    }

    /// Remove engine.json again (if it is still ours).
    pub fn unpublish(&self) {
        let Some(path) = self.info_path.lock_ok().take() else { return };
        let ours = std::fs::read_to_string(&path)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .is_some_and(|v| v["pid"] == std::process::id());
        if ours {
            let _ = std::fs::remove_file(path);
        }
    }

    pub fn stop(&self) {
        self.stopped.store(true, Relaxed);
        self.server.unblock();
    }
}
