//! Automatic qBittorrent configuration through its Web UI API.
//!
//! When DualLink turns on, we remember the user's current proxy/protocol settings, point qBittorrent at the
//! DualLink proxy, and reconnect running downloads so they go through it. When DualLink turns off, the
//! original settings are put back. Works with qBittorrent 4.4+ and 5.x.

use serde_json::{json, Map, Value};
use std::time::Duration;

/// Settings DualLink changes (and later restores). Only keys the running qBittorrent knows are touched.
const MANAGED_KEYS: [&str; 13] = [
    "proxy_type", "proxy_ip", "proxy_port", "proxy_auth_enabled", "proxy_peer_connections", "proxy_bittorrent",
    "proxy_torrents_only", "proxy_misc", "proxy_rss", "proxy_hostname_lookup", "bittorrent_protocol", "max_connec",
    "max_connec_per_torrent",
];

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Kind {
    Unreachable,
    Auth,
    Error,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Unreachable => "unreachable",
            Kind::Auth => "auth",
            Kind::Error => "error",
        }
    }
}

#[derive(Debug)]
pub struct QbtError {
    pub kind: Kind,
    pub message: String,
}

impl QbtError {
    fn new(kind: Kind, message: impl Into<String>) -> Self {
        QbtError { kind, message: message.into() }
    }
}

type Result<T> = std::result::Result<T, QbtError>;

pub struct QbtWebUi {
    url: String,
    username: String,
    password: String,
    agent: ureq::Agent,
    cookie: Option<String>,
    logged_in: bool,
}

impl QbtWebUi {
    pub fn new(url: &str, username: &str, password: &str) -> Self {
        QbtWebUi {
            url: url.trim().trim_end_matches('/').to_string(),
            username: username.to_string(),
            password: password.to_string(),
            agent: crate::http::agent(Duration::from_secs(5)),
            cookie: None,
            logged_in: false,
        }
    }

    // ---- low level ------------------------------------------------------------------------- //
    fn req(&mut self, path: &str, fields: Option<&[(&str, &str)]>) -> Result<String> {
        let full = format!("{}{}", self.url, path);
        let mut r = match fields {
            Some(_) => self.agent.post(&full),
            None => self.agent.get(&full),
        };
        r = r.set("Referer", &self.url).set("Origin", &self.url);
        if let Some(c) = &self.cookie {
            r = r.set("Cookie", c);
        }
        let sent = match fields {
            Some(f) => r.send_form(f),
            None => r.call(),
        };
        match sent {
            Ok(resp) => {
                let cookies: Vec<&str> = resp.all("set-cookie").into_iter().filter_map(|c| c.split(';').next()).collect();
                if !cookies.is_empty() {
                    self.cookie = Some(cookies.join("; "));
                }
                resp.into_string().map_err(|_| unreachable_error())
            }
            Err(ureq::Error::Status(401 | 403, _)) => Err(QbtError::new(
                Kind::Auth,
                "qBittorrent refused access - check the Web UI username/password, or tick 'Bypass authentication for clients on localhost'.",
            )),
            Err(ureq::Error::Status(code, _)) => Err(QbtError::new(Kind::Error, format!("qBittorrent returned HTTP {code} for {path}"))),
            Err(_) => Err(unreachable_error()),
        }
    }

    fn call(&mut self, path: &str, fields: Option<&[(&str, &str)]>) -> Result<String> {
        if !self.logged_in {
            self.login()?;
        }
        match self.req(path, fields) {
            Err(e) if e.kind == Kind::Auth && !self.username.is_empty() => {
                self.logged_in = false;
                self.login()?;
                self.req(path, fields)
            }
            other => other,
        }
    }

    // ---- API ------------------------------------------------------------------------------- //
    fn login(&mut self) -> Result<()> {
        if !self.username.is_empty() {
            let (u, p) = (self.username.clone(), self.password.clone());
            let res = self.req("/api/v2/auth/login", Some(&[("username", &u), ("password", &p)]))?;
            if res.trim() != "Ok." {
                return Err(QbtError::new(Kind::Auth, "qBittorrent rejected the Web UI username/password."));
            }
        }
        self.logged_in = true;
        Ok(())
    }

    pub fn version(&mut self) -> Result<String> {
        Ok(self.call("/api/v2/app/version", None)?.trim().to_string())
    }

    fn preferences(&mut self) -> Result<Map<String, Value>> {
        let text = self.call("/api/v2/app/preferences", None)?;
        serde_json::from_str(&text).map_err(|_| QbtError::new(Kind::Error, "qBittorrent sent settings DualLink couldn't read."))
    }

    fn set_preferences(&mut self, prefs: &Map<String, Value>) -> Result<()> {
        let body = Value::Object(prefs.clone()).to_string();
        self.call("/api/v2/app/setPreferences", Some(&[("json", &body)])).map(|_| ())
    }

    pub fn downloading_hashes(&mut self) -> Result<Vec<String>> {
        let text = self.call("/api/v2/torrents/info?filter=downloading", None)?;
        let items: Vec<Value> = serde_json::from_str(&text).unwrap_or_default();
        Ok(items.iter().filter_map(|t| t.get("hash")?.as_str().map(String::from)).collect())
    }

    /// Stop + start torrents so their peer connections are re-made with the new settings.
    pub fn reconnect(&mut self, hashes: &[String]) -> Result<()> {
        if hashes.is_empty() {
            return Ok(());
        }
        let joined = hashes.join("|");
        let h = [("hashes", joined.as_str())];
        let v5 = self.call("/api/v2/torrents/stop", Some(&h)).and_then(|_| self.call("/api/v2/torrents/start", Some(&h)));
        match v5 {
            Err(e) if e.kind == Kind::Error => {
                self.call("/api/v2/torrents/pause", Some(&h))?; // qBittorrent 4.x
                self.call("/api/v2/torrents/resume", Some(&h)).map(|_| ())
            }
            other => other.map(|_| ()),
        }
    }

    // ---- DualLink-specific ----------------------------------------------------------------- //
    /// Point qBittorrent at the proxy. Returns the original values so they can be restored.
    pub fn apply_dual_link(&mut self, port: u16) -> Result<Map<String, Value>> {
        let cur = self.preferences()?;
        let (saved, new) = plan(&cur, port);
        self.set_preferences(&new)?;
        Ok(saved)
    }

    pub fn restore(&mut self, saved: &Map<String, Value>) -> Result<()> {
        if saved.is_empty() {
            return Ok(());
        }
        self.set_preferences(saved)
    }
}

fn unreachable_error() -> QbtError {
    QbtError::new(Kind::Unreachable, "Can't reach qBittorrent's Web UI. Is qBittorrent running with the Web UI turned on?")
}

/// From qBittorrent's current settings: (what to remember, what to set).
fn plan(cur: &Map<String, Value>, port: u16) -> (Map<String, Value>, Map<String, Value>) {
    let saved: Map<String, Value> = MANAGED_KEYS.iter().filter_map(|k| cur.get(*k).map(|v| (k.to_string(), v.clone()))).collect();
    let int = |k: &str| cur.get(k).and_then(Value::as_i64).unwrap_or(0);
    let mut new = Map::new();
    // qBittorrent 5 uses a name for the proxy type, 4.x uses a number (2 = SOCKS5)
    new.insert("proxy_type".into(), if cur.get("proxy_type").is_some_and(Value::is_string) { json!("SOCKS5") } else { json!(2) });
    let wanted = [
        ("proxy_ip", json!("127.0.0.1")),
        ("proxy_port", json!(port)),
        ("proxy_auth_enabled", json!(false)),
        ("proxy_peer_connections", json!(true)),
        ("proxy_bittorrent", json!(true)),
        ("proxy_torrents_only", json!(false)),
        ("proxy_misc", json!(false)),
        ("proxy_rss", json!(false)),
        ("proxy_hostname_lookup", json!(true)),
        ("bittorrent_protocol", json!(1)), // TCP only - uTP can't go through a SOCKS proxy
        ("max_connec", json!(int("max_connec").max(500))),
        ("max_connec_per_torrent", json!(int("max_connec_per_torrent").max(200))),
    ];
    for (k, v) in wanted {
        let unlimited = k.starts_with("max_connec") && int(k) < 0; // -1 = unlimited: leave it
        if cur.contains_key(k) && !unlimited {
            new.insert(k.into(), v);
        }
    }
    (saved, new)
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    #[test]
    fn plan_for_qbittorrent_5() {
        let cur = json!({"proxy_type": "None", "proxy_ip": "", "proxy_port": 8080, "bittorrent_protocol": 0,
                         "max_connec": 100, "max_connec_per_torrent": -1, "save_path": "C:/x"});
        let (saved, new) = plan(cur.as_object().unwrap(), 1080);
        assert_eq!(saved.len(), 6);
        assert!(!saved.contains_key("save_path"));
        assert_eq!(new["proxy_type"], "SOCKS5");
        assert_eq!(new["proxy_port"], 1080);
        assert_eq!(new["bittorrent_protocol"], 1);
        assert_eq!(new["max_connec"], 500);
        assert!(!new.contains_key("max_connec_per_torrent"), "unlimited stays unlimited");
        assert!(!new.contains_key("proxy_rss"), "keys this qBittorrent doesn't know are left out");
    }

    #[test]
    fn plan_for_qbittorrent_4() {
        let cur = json!({"proxy_type": 0, "max_connec": 900});
        let (_, new) = plan(cur.as_object().unwrap(), 1080);
        assert_eq!(new["proxy_type"], 2);
        assert_eq!(new["max_connec"], 900);
    }

    /// A pretend qBittorrent Web UI. Returns its address and the list of requests it received.
    pub fn fake_qbt(needs_login: bool) -> (String, Arc<Mutex<Vec<String>>>) {
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let url = format!("http://{}", server.server_addr());
        let log = Arc::new(Mutex::new(Vec::new()));
        let log2 = log.clone();
        std::thread::spawn(move || {
            let prefs = json!({"proxy_type": "None", "proxy_ip": "", "proxy_port": 8080, "bittorrent_protocol": 0, "max_connec": 100});
            for mut rq in server.incoming_requests() {
                let mut body = String::new();
                let _ = rq.as_reader().read_to_string(&mut body);
                let path = rq.url().to_string();
                let authed = rq.headers().iter().any(|h| h.field.equiv("Cookie") && h.value.as_str().contains("SID=abc"));
                log2.lock().unwrap().push(format!("{path} {body}"));
                let (code, text, cookie) = if path == "/api/v2/auth/login" {
                    if body.contains("password=secret") { (200, "Ok.".to_string(), true) } else { (200, "Fails.".to_string(), false) }
                } else if needs_login && !authed {
                    (403, "Forbidden".to_string(), false)
                } else {
                    match path.as_str() {
                        "/api/v2/app/version" => (200, "v5.0.2".to_string(), false),
                        "/api/v2/app/preferences" => (200, prefs.to_string(), false),
                        "/api/v2/torrents/info?filter=downloading" => (200, r#"[{"hash":"aa"},{"hash":"bb"}]"#.to_string(), false),
                        "/api/v2/torrents/stop" | "/api/v2/torrents/start" => (404, String::new(), false),
                        _ => (200, String::new(), false),
                    }
                };
                let mut resp = tiny_http::Response::from_string(text).with_status_code(code);
                if cookie {
                    resp.add_header(tiny_http::Header::from_bytes("Set-Cookie", "SID=abc; HttpOnly; path=/").unwrap());
                }
                let _ = rq.respond(resp);
            }
        });
        (url, log)
    }

    #[test]
    fn talks_to_the_web_ui() {
        let (url, log) = fake_qbt(false);
        let mut q = QbtWebUi::new(&url, "", "");
        assert_eq!(q.version().unwrap(), "v5.0.2");
        let saved = q.apply_dual_link(1080).unwrap();
        assert_eq!(saved["proxy_port"], 8080);
        let hashes = q.downloading_hashes().unwrap();
        assert_eq!(hashes, ["aa", "bb"]);
        q.reconnect(&hashes).unwrap(); // stop/start give 404 here, so it falls back to pause/resume
        q.restore(&saved).unwrap();
        let log = log.lock().unwrap();
        assert!(log.iter().any(|l| l.starts_with("/api/v2/torrents/resume hashes=aa%7Cbb")));
        assert_eq!(log.iter().filter(|l| l.starts_with("/api/v2/app/setPreferences json=")).count(), 2);
    }

    #[test]
    fn logs_in_when_needed() {
        let (url, _) = fake_qbt(true);
        assert_eq!(QbtWebUi::new(&url, "", "").version().unwrap_err().kind, Kind::Auth);
        assert_eq!(QbtWebUi::new(&url, "admin", "wrong").version().unwrap_err().kind, Kind::Auth);
        assert_eq!(QbtWebUi::new(&url, "admin", "secret").version().unwrap(), "v5.0.2");
        assert_eq!(QbtWebUi::new("http://127.0.0.1:1", "", "").version().unwrap_err().kind, Kind::Unreachable);
    }
}
