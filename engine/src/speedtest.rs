//! Speed test: how fast can each connection download on its own?
//!
//! Downloads from a test server over one link (several connections bound to that link's address) for a few
//! seconds and measures the throughput after a short warm-up.

use duallink_common::LockExt;
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Cloudflare's public speed-test endpoint (the one speed.cloudflare.com uses).
pub const TEST_URL: &str = "https://speed.cloudflare.com/__down?bytes=50000000";
const STREAMS: usize = 4;
const WARMUP: f64 = 1.0;
const TIMEOUT: Duration = Duration::from_secs(8);

pub struct Measured {
    /// Bytes per second.
    pub bps: f64,
    pub error: Option<String>,
}

trait Stream: Read + Write {}
impl<T: Read + Write> Stream for T {}

struct Shared {
    total: AtomicU64,
    stop: AtomicBool,
    errors: Mutex<Vec<String>>,
}

/// Open a connection that leaves through the adapter with this address.
fn connect(source: Ipv4Addr, url: &url::Url) -> Result<Box<dyn Stream>, String> {
    let host = url.host_str().ok_or("The test address is not valid.")?;
    let port = url.port_or_known_default().unwrap_or(80);
    let dest = (host, port)
        .to_socket_addrs()
        .map_err(|e| e.to_string())?
        .find(SocketAddr::is_ipv4)
        .ok_or("The test server has no IPv4 address.")?;
    let socket = socket2::Socket::new(socket2::Domain::IPV4, socket2::Type::STREAM, None).map_err(|e| e.to_string())?;
    socket.bind(&SocketAddr::new(IpAddr::V4(source), 0).into()).map_err(|e| e.to_string())?;
    socket.connect_timeout(&dest.into(), TIMEOUT).map_err(|e| e.to_string())?;
    let tcp: TcpStream = socket.into();
    tcp.set_read_timeout(Some(TIMEOUT)).map_err(|e| e.to_string())?;
    tcp.set_write_timeout(Some(TIMEOUT)).map_err(|e| e.to_string())?;
    if url.scheme() == "https" {
        let tls = native_tls::TlsConnector::new().map_err(|e| e.to_string())?;
        Ok(Box::new(tls.connect(host, tcp).map_err(|e| e.to_string())?))
    } else {
        Ok(Box::new(tcp))
    }
}

/// One download: count everything after the response headers.
fn download(source: Ipv4Addr, url: &url::Url, shared: &Shared) -> Result<(), String> {
    let mut s = connect(source, url)?;
    let path = match url.query() {
        Some(q) => format!("{}?{q}", url.path()),
        None => url.path().to_string(),
    };
    let host = url.host_str().unwrap_or_default();
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: DualLink speed test\r\nCache-Control: no-cache\r\nAccept: */*\r\nConnection: close\r\n\r\n"
    );
    s.write_all(request.as_bytes()).map_err(|e| e.to_string())?;
    let mut buf = vec![0u8; 65536];
    let mut head: Vec<u8> = Vec::new();
    let mut in_body = false;
    while !shared.stop.load(Relaxed) {
        let n = match s.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) => return Err(e.to_string()),
        };
        if in_body {
            shared.total.fetch_add(n as u64, Relaxed);
            continue;
        }
        head.extend_from_slice(&buf[..n]);
        if let Some(end) = head.windows(4).position(|w| w == b"\r\n\r\n") {
            let status = String::from_utf8_lossy(&head[..end]).split_whitespace().nth(1).unwrap_or("?").to_string();
            if status != "200" {
                return Err(format!("The test server answered {status}."));
            }
            shared.total.fetch_add((head.len() - end - 4) as u64, Relaxed);
            in_body = true;
        } else if head.len() > 32 * 1024 {
            return Err("The test server sent an unexpected reply.".into());
        }
    }
    Ok(())
}

/// Download over the link with this local address for `seconds`.
pub fn measure(source: Ipv4Addr, seconds: f64, url: &str) -> Measured {
    let url = match url::Url::parse(url) {
        Ok(u) => u,
        Err(e) => return Measured { bps: 0.0, error: Some(e.to_string()) },
    };
    let shared = Arc::new(Shared { total: AtomicU64::new(0), stop: AtomicBool::new(false), errors: Mutex::new(Vec::new()) });
    for _ in 0..STREAMS {
        let (shared, url) = (shared.clone(), url.clone());
        std::thread::spawn(move || {
            while !shared.stop.load(Relaxed) {
                if let Err(e) = download(source, &url, &shared) {
                    shared.errors.lock_ok().push(e);
                    return;
                }
            }
        });
    }
    std::thread::sleep(Duration::from_secs_f64(WARMUP.min(seconds / 2.0))); // skip the slow start of each connection
    let (b0, t0) = (shared.total.load(Relaxed), Instant::now());
    std::thread::sleep(Duration::from_secs_f64((seconds - WARMUP).max(0.5)));
    let (b1, t1) = (shared.total.load(Relaxed), Instant::now());
    shared.stop.store(true, Relaxed); // the workers notice at their next read and end by themselves
    let bps = (b1 - b0) as f64 / (t1 - t0).as_secs_f64().max(0.001);
    let error = (b1 == 0).then(|| shared.errors.lock_ok().first().cloned().unwrap_or_else(|| "No data arrived.".into()));
    Measured { bps, error }
}

#[cfg(test)]
pub mod tests {
    use super::*;

    /// A server that sends zeros for as long as anyone listens.
    pub fn firehose() -> String {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/__down?bytes=1", l.local_addr().unwrap());
        std::thread::spawn(move || {
            for s in l.incoming() {
                let Ok(mut s) = s else { break };
                std::thread::spawn(move || {
                    let mut req = [0u8; 2048];
                    let _ = s.read(&mut req);
                    let _ = s.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n");
                    let chunk = [0u8; 16384];
                    for _ in 0..200_000 {
                        if s.write_all(&chunk).is_err() {
                            break;
                        }
                    }
                });
            }
        });
        url
    }

    #[test]
    fn measures_a_local_download() {
        let m = measure(Ipv4Addr::LOCALHOST, 1.0, &firehose());
        assert!(m.error.is_none(), "{:?}", m.error);
        assert!(m.bps > 100_000.0, "{}", m.bps);
    }

    #[test]
    fn reports_why_nothing_arrived() {
        let m = measure(Ipv4Addr::LOCALHOST, 1.0, "http://127.0.0.1:1/x");
        assert_eq!(m.bps, 0.0);
        assert!(m.error.is_some());
    }
}
