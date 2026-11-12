//! The proxy core: a SOCKS5 proxy that spreads outgoing connections over several network links,
//! automatically favouring the faster one.

use duallink_common::LockExt;
use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex, MutexGuard, RwLock};
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::tcp::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::{TcpListener, TcpSocket, TcpStream, UdpSocket};
use tokio::sync::Notify;
use tokio::task::{JoinHandle, JoinSet};

/// Attempts being opened count as load, so a burst of new connections spreads over the links. But only
/// fresh ones: an attempt still unanswered after this long is almost always to a peer that isn't there
/// (torrents try many), and if those piled up on a link with slower failures (a phone's mobile network)
/// they would push the working connections onto the other link.
pub const FRESH_PENDING: Duration = Duration::from_millis(1500);
/// Seconds between speed measurements.
pub const BAL_PERIOD: f64 = 3.0;
/// A connection moving less than 16 KiB for this long counts as idle.
const IDLE_AFTER: Duration = Duration::from_secs(90);
/// Connections younger than this are never moved.
const MIN_AGE: Duration = Duration::from_secs(120);
/// At most one idle connection is moved per this long.
const MOVE_EVERY: Duration = Duration::from_secs(15);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// A link with no data-carrying connection for this long is "unknown", not "slow".
pub const UNKNOWN_AFTER: Duration = Duration::from_secs(60);

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

/// The parts of a link that change rarely (looked at when a connection is opened and every few seconds).
#[derive(Debug, Clone)]
pub struct LinkCtl {
    pub name: String,
    pub ip: Option<Ipv4Addr>,
    /// The adapter's global IPv6 address, if it has one.
    pub ip6: Option<Ipv6Addr>,
    pub weight: f64,
    pub enabled: bool,
    /// Smoothed download speed of this link, bytes/s.
    pub rate: f64,
    /// Smoothed download speed per connection, bytes/s.
    pub per_conn: f64,
    pub last_down: u64,
    /// Last time this link had a connection actually carrying data.
    pub measured_at: Option<Instant>,
    /// Connection attempts being opened right now: (attempt, when it started).
    pending: Vec<(u64, Instant)>,
}

/// One network connection (Ethernet, the hotspot, ...) that traffic can go out over.
#[derive(Debug)]
pub struct Link {
    pub id: u64,
    ctl: Mutex<LinkCtl>,
    /// 0 = unlimited (applies to down + up + UDP).
    pub quota: AtomicU64,
    pub down: AtomicU64,
    pub up: AtomicU64,
    /// DHT / UDP-tracker traffic (kept out of the speed measurements).
    pub udp: AtomicU64,
    pub active: AtomicI64,
    pub failures: AtomicU64,
}

impl Link {
    pub fn new(name: &str) -> Arc<Link> {
        Arc::new(Link {
            id: NEXT_ID.fetch_add(1, Relaxed),
            ctl: Mutex::new(LinkCtl {
                name: name.to_string(),
                ip: None,
                ip6: None,
                weight: 1.0,
                enabled: true,
                rate: 0.0,
                per_conn: 0.0,
                last_down: 0,
                measured_at: None,
                pending: Vec::new(),
            }),
            quota: AtomicU64::new(0),
            down: AtomicU64::new(0),
            up: AtomicU64::new(0),
            udp: AtomicU64::new(0),
            active: AtomicI64::new(0),
            failures: AtomicU64::new(0),
        })
    }

    pub fn ctl(&self) -> MutexGuard<'_, LinkCtl> {
        self.ctl.lock_ok()
    }

    pub fn used(&self) -> u64 {
        self.down.load(Relaxed) + self.up.load(Relaxed) + self.udp.load(Relaxed)
    }

    pub fn over_quota(&self) -> bool {
        let q = self.quota.load(Relaxed);
        q > 0 && self.used() >= q
    }

    pub fn usable(&self) -> bool {
        let c = self.ctl();
        c.ip.is_some() && c.enabled && !self.over_quota()
    }

    pub fn ip(&self) -> Option<Ipv4Addr> {
        self.ctl().ip
    }

    /// Back to a clean slate (a new session, or a different adapter in this slot).
    pub fn reset_counters(&self) {
        self.down.store(0, Relaxed);
        self.up.store(0, Relaxed);
        self.udp.store(0, Relaxed);
        self.failures.store(0, Relaxed);
        let mut c = self.ctl();
        c.rate = 0.0;
        c.per_conn = 0.0;
        c.last_down = 0;
        c.measured_at = None;
        c.weight = 1.0;
    }

    fn fresh_pending(&self, now: Instant) -> usize {
        self.ctl().pending.iter().filter(|(_, t)| now.duration_since(*t) < FRESH_PENDING).count()
    }

    #[cfg(test)]
    pub fn add_pending_for_test(&self, started: Instant) {
        self.ctl().pending.push((NEXT_ID.fetch_add(1, Relaxed), started));
    }
}

/// One relayed connection.
struct Conn {
    link: Arc<Link>,
    started: Instant,
    /// Milliseconds since the proxy was created.
    last_busy: AtomicU64,
    bytes: AtomicU64,
    mark: AtomicU64,
    kill: Notify,
}

impl Conn {
    fn add(&self, n: u64, epoch: Instant) {
        let bytes = self.bytes.fetch_add(n, Relaxed) + n;
        if bytes - self.mark.load(Relaxed) >= 16384 {
            self.mark.store(bytes, Relaxed);
            self.last_busy.store(epoch.elapsed().as_millis() as u64, Relaxed);
        }
    }
}

struct Server {
    port: u16,
    accept: JoinHandle<()>,
    balance: JoinHandle<()>,
}

/// SOCKS5 proxy (CONNECT and UDP ASSOCIATE); each outgoing connection is bound to one link's local address.
pub struct Proxy {
    /// Ethernet, Hotspot, then any extra connections. Shared with the controller, which edits the list.
    pub links: RwLock<Vec<Arc<Link>>>,
    /// Pass-through: DualLink is "off" but apps like Free Download Manager stay pointed at the proxy,
    /// so connections go out normally (Windows picks the route) instead of failing.
    pub passthrough: AtomicBool,
    pub direct: Arc<Link>,
    /// Adapt weights to measured speed.
    pub auto: AtomicBool,
    /// Move idle connections to the faster link.
    pub recycle: AtomicBool,
    /// The slower link always keeps at least this share (keeps measuring it).
    pub min_share: f64,
    pub moved: AtomicU64,
    /// Open UDP associations (qBittorrent's DHT / UDP trackers).
    pub udp_assocs: AtomicI64,
    /// CONNECT requests to IPv6 addresses while no link had IPv6.
    pub refused_ipv6: AtomicU64,
    /// Connections that couldn't be made on any link.
    pub failed: AtomicU64,
    pub udp_packets: AtomicU64,
    conns: Mutex<HashMap<u64, Arc<Conn>>>,
    server: Mutex<Option<Server>>,
    rt: tokio::runtime::Runtime,
    epoch: Instant,
}

impl Proxy {
    pub fn new(links: Vec<Arc<Link>>) -> Arc<Proxy> {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .thread_name("proxy")
            .enable_all()
            .build()
            .expect("the proxy's runtime");
        Arc::new(Proxy {
            links: RwLock::new(links),
            passthrough: AtomicBool::new(false),
            direct: Link::new("Direct"),
            auto: AtomicBool::new(true),
            recycle: AtomicBool::new(true),
            min_share: 0.15,
            moved: AtomicU64::new(0),
            udp_assocs: AtomicI64::new(0),
            refused_ipv6: AtomicU64::new(0),
            failed: AtomicU64::new(0),
            udp_packets: AtomicU64::new(0),
            conns: Mutex::new(HashMap::new()),
            server: Mutex::new(None),
            rt,
            epoch: Instant::now(),
        })
    }

    pub fn links(&self) -> Vec<Arc<Link>> {
        self.links.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    // ---- link choice ----------------------------------------------------------------------- //
    /// The links a new connection should try, best first.
    pub fn order(&self) -> Vec<Arc<Link>> {
        if self.passthrough.load(Relaxed) {
            return vec![self.direct.clone()];
        }
        let now = Instant::now();
        let mut usable: Vec<(f64, Arc<Link>)> = self
            .links()
            .into_iter()
            .filter(|l| l.usable())
            .map(|l| {
                let weight = l.ctl().weight.max(0.01);
                let load = (l.active.load(Relaxed).max(0) as f64 + l.fresh_pending(now) as f64 + 1.0) / weight;
                (load, l)
            })
            .collect();
        usable.sort_by(|a, b| a.0.total_cmp(&b.0));
        usable.into_iter().map(|(_, l)| l).collect()
    }

    /// UDP is many tiny packets: keep it on the first link (Ethernet) to save mobile data.
    pub fn udp_link(&self) -> Option<Arc<Link>> {
        if self.passthrough.load(Relaxed) {
            return Some(self.direct.clone());
        }
        self.links().into_iter().find(|l| l.usable())
    }

    // ---- lifecycle ------------------------------------------------------------------------- //
    /// True while the proxy is listening.
    pub fn is_up(&self) -> bool {
        self.server.lock_ok().is_some()
    }

    /// The port being listened on (0 while down).
    pub fn port(&self) -> u16 {
        self.server.lock_ok().as_ref().map(|s| s.port).unwrap_or(0)
    }

    /// Start listening on 127.0.0.1:`port` (0 = any free port).
    pub fn start(self: &Arc<Self>, port: u16) -> Result<(), String> {
        let mut server = self.server.lock_ok();
        if server.is_some() {
            return Ok(());
        }
        let mut last = None;
        let mut listener = None;
        for _ in 0..12 {
            // the old listener may still be closing: retry for ~6 s
            match std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, port)) {
                Ok(l) => {
                    listener = Some(l);
                    break;
                }
                Err(e) => {
                    last = Some(e);
                    std::thread::sleep(Duration::from_millis(500));
                }
            }
        }
        let Some(listener) = listener else {
            let e = last.map(|e| e.to_string()).unwrap_or_default();
            return Err(format!(
                "Could not listen on port {port}: {e}. Another program (or a copy of DualLink) is using it - try a different port."
            ));
        };
        listener.set_nonblocking(true).map_err(|e| e.to_string())?;
        let port = listener.local_addr().map_err(|e| e.to_string())?.port();
        let _guard = self.rt.enter();
        let listener = TcpListener::from_std(listener).map_err(|e| e.to_string())?;
        let accept = self.rt.spawn(self.clone().accept_loop(listener));
        let balance = self.rt.spawn(self.clone().balance_loop());
        *server = Some(Server { port, accept, balance });
        Ok(())
    }

    /// Stop listening and close every connection. Returns once the port is free again.
    pub fn stop(&self) {
        let Some(server) = self.server.lock_ok().take() else { return };
        server.balance.abort();
        server.accept.abort(); // dropping its JoinSet ends every connection it started
        let _ = self.rt.block_on(async { tokio::time::timeout(Duration::from_secs(6), server.accept).await });
        self.conns.lock_ok().clear();
    }

    /// Close every connection on a link (adapter gone or new address); the apps reconnect them.
    pub fn close_link(&self, link: &Arc<Link>) {
        for c in self.conns.lock_ok().values() {
            if Arc::ptr_eq(&c.link, link) {
                c.kill.notify_one();
            }
        }
    }

    async fn accept_loop(self: Arc<Self>, listener: TcpListener) {
        let mut tasks = JoinSet::new();
        loop {
            tokio::select! {
                r = listener.accept() => match r {
                    Ok((stream, _)) => {
                        tasks.spawn(self.clone().handle(stream));
                    }
                    Err(_) => tokio::time::sleep(Duration::from_millis(50)).await,
                },
                Some(_) = tasks.join_next(), if !tasks.is_empty() => {}
            }
        }
    }

    // ---- automatic balancing --------------------------------------------------------------- //
    /// Measure each link and, in automatic mode, set the weights from the speed per connection.
    pub fn update_weights(&self) {
        const A: f64 = 0.35;
        let now = Instant::now();
        let now_ms = self.epoch.elapsed().as_millis() as u64;
        let links = self.links();
        let mut busy: HashMap<u64, u32> = HashMap::new();
        for c in self.conns.lock_ok().values() {
            // only count connections that actually moved data in the last 10 s
            if c.mark.load(Relaxed) > 0 && now_ms.saturating_sub(c.last_busy.load(Relaxed)) < 10_000 {
                *busy.entry(c.link.id).or_default() += 1;
            }
        }
        for l in &links {
            let down = l.down.load(Relaxed);
            let mut c = l.ctl();
            let inst = down.saturating_sub(c.last_down) as f64 / BAL_PERIOD;
            c.last_down = down;
            c.rate = (1.0 - A) * c.rate + A * inst;
            if let Some(&n) = busy.get(&l.id) {
                c.per_conn = (1.0 - A) * c.per_conn + A * (inst / n as f64);
                c.measured_at = Some(now);
            }
        }
        if !self.auto.load(Relaxed) {
            return;
        }
        let usable: Vec<&Arc<Link>> = links.iter().filter(|l| l.usable()).collect();
        let total: f64 = usable.iter().map(|l| l.ctl().per_conn).sum();
        // keep an even split until every link has been measured
        let unknown = usable.iter().any(|l| l.ctl().measured_at.is_none_or(|t| now.duration_since(t) > UNKNOWN_AFTER));
        if usable.len() < 2 || unknown || total < 32.0 * 1024.0 {
            for l in &links {
                l.ctl().weight = 1.0;
            }
            return;
        }
        for l in usable {
            let mut c = l.ctl();
            c.weight = (c.per_conn / total).clamp(self.min_share, 1.0 - self.min_share);
        }
    }

    /// The link that is clearly better than the rest (60% share or more), if there is one.
    pub fn preferred(&self) -> Option<Arc<Link>> {
        let usable: Vec<(f64, Arc<Link>)> =
            self.links().into_iter().filter(|l| l.usable()).map(|l| { let w = l.ctl().weight; (w, l) }).collect();
        if usable.len() < 2 {
            return None;
        }
        let total: f64 = usable.iter().map(|(w, _)| w).sum();
        let (w, best) = usable.into_iter().max_by(|a, b| a.0.total_cmp(&b.0))?;
        (w / total >= 0.6).then_some(best)
    }

    async fn balance_loop(self: Arc<Self>) {
        let mut last_move: Option<Instant> = None;
        loop {
            tokio::time::sleep(Duration::from_secs_f64(BAL_PERIOD)).await;
            self.update_weights();
            let now = Instant::now();
            let best = self.preferred();
            let allowed = !self.passthrough.load(Relaxed) && self.auto.load(Relaxed) && self.recycle.load(Relaxed);
            let (true, Some(best)) = (allowed, best) else { continue };
            if last_move.is_some_and(|t| now.duration_since(t) < MOVE_EVERY) {
                continue;
            }
            let now_ms = self.epoch.elapsed().as_millis() as u64;
            let victim = self
                .conns
                .lock_ok()
                .values()
                .filter(|c| {
                    !Arc::ptr_eq(&c.link, &best)
                        && now.duration_since(c.started) > MIN_AGE
                        && now_ms.saturating_sub(c.last_busy.load(Relaxed)) > IDLE_AFTER.as_millis() as u64
                })
                .min_by_key(|c| c.last_busy.load(Relaxed))
                .cloned();
            if let Some(v) = victim {
                v.kill.notify_one(); // the app reconnects; the new connection goes to the faster link
                self.moved.fetch_add(1, Relaxed);
                last_move = Some(now);
            }
        }
    }

    // ---- SOCKS5 ---------------------------------------------------------------------------- //
    async fn handle(self: Arc<Self>, mut client: TcpStream) {
        let _ = self.serve(&mut client).await;
    }

    async fn serve(self: &Arc<Self>, client: &mut TcpStream) -> std::io::Result<()> {
        const OK: u8 = 0;
        async fn reply(client: &mut TcpStream, code: u8) -> std::io::Result<()> {
            client.write_all(&[5, code, 0, 1, 0, 0, 0, 0, 0, 0]).await
        }

        let mut head = [0u8; 2];
        client.read_exact(&mut head).await?;
        if head[0] != 5 {
            return Ok(());
        }
        let mut methods = vec![0u8; head[1] as usize];
        client.read_exact(&mut methods).await?;
        client.write_all(&[5, 0]).await?;
        let mut req = [0u8; 4];
        client.read_exact(&mut req).await?;
        let (cmd, atyp) = (req[1], req[3]);
        if cmd != 1 && cmd != 3 {
            return reply(client, 7).await; // command not supported
        }
        enum Target {
            V4(Ipv4Addr),
            V6(Ipv6Addr),
            Name(String),
        }
        let target = match atyp {
            1 => {
                let mut b = [0u8; 4];
                client.read_exact(&mut b).await?;
                Target::V4(Ipv4Addr::from(b))
            }
            3 => {
                let n = client.read_u8().await? as usize;
                let mut b = vec![0u8; n];
                client.read_exact(&mut b).await?;
                Target::Name(String::from_utf8_lossy(&b).into_owned())
            }
            4 => {
                let mut b = [0u8; 16];
                client.read_exact(&mut b).await?;
                Target::V6(Ipv6Addr::from(b))
            }
            _ => return reply(client, 8).await, // address type not supported
        };
        let port = client.read_u16().await?;
        if cmd == 3 {
            return self.udp_associate(client).await;
        }

        let passthrough = self.passthrough.load(Relaxed);
        let can_v6 = passthrough || self.links().iter().any(|l| l.usable() && l.ctl().ip6.is_some());
        let dest: SocketAddr = match target {
            Target::V4(ip) => SocketAddr::new(IpAddr::V4(ip), port),
            Target::V6(ip) => {
                if !can_v6 {
                    self.refused_ipv6.fetch_add(1, Relaxed);
                    return reply(client, 8).await; // no link has IPv6 right now
                }
                SocketAddr::new(IpAddr::V6(ip), port)
            }
            Target::Name(host) => {
                // IPv4 first; IPv6 only for names that have nothing else
                let found: Vec<SocketAddr> = match tokio::net::lookup_host((host.as_str(), port)).await {
                    Ok(it) => it.collect(),
                    Err(_) => Vec::new(),
                };
                let pick = found.iter().find(|a| a.is_ipv4()).or_else(|| found.iter().find(|_| can_v6));
                match pick {
                    Some(a) => *a,
                    None => return reply(client, 4).await, // host unreachable
                }
            }
        };
        let v6 = dest.is_ipv6();

        let mut tried: Vec<u64> = Vec::new();
        let mut made: Option<(TcpStream, Arc<Link>)> = None;
        loop {
            // pick again after every failure: the best link may have changed meanwhile
            let cand = self.order().into_iter().find(|l| {
                let c = l.ctl();
                !tried.contains(&l.id) && (!v6 || c.ip6.is_some() || c.ip.is_none())
            });
            let Some(cand) = cand else { break };
            tried.push(cand.id);
            let attempt = NEXT_ID.fetch_add(1, Relaxed);
            cand.ctl().pending.push((attempt, Instant::now()));
            let result = connect_via(&cand, dest).await;
            cand.ctl().pending.retain(|(a, _)| *a != attempt);
            match result {
                Ok(stream) => {
                    made = Some((stream, cand));
                    break;
                }
                Err(_) => {
                    cand.failures.fetch_add(1, Relaxed);
                }
            }
        }
        let Some((remote, link)) = made else {
            self.failed.fetch_add(1, Relaxed);
            return reply(client, 5).await; // connection refused
        };
        reply(client, OK).await?;

        let conn = Arc::new(Conn {
            link: link.clone(),
            started: Instant::now(),
            last_busy: AtomicU64::new(self.epoch.elapsed().as_millis() as u64),
            bytes: AtomicU64::new(0),
            mark: AtomicU64::new(0),
            kill: Notify::new(),
        });
        let id = NEXT_ID.fetch_add(1, Relaxed);
        self.conns.lock_ok().insert(id, conn.clone());
        link.active.fetch_add(1, Relaxed);
        let _registered = Registered { proxy: self, id, link: &link };

        let (mut client_r, mut client_w) = tokio::io::split(client);
        let (remote_r, remote_w) = remote.into_split();
        // when either direction ends, both sockets are closed
        tokio::select! {
            _ = pipe_up(&mut client_r, remote_w, &conn, self.epoch) => {}
            _ = pipe_down(remote_r, &mut client_w, &conn, self.epoch) => {}
            _ = conn.kill.notified() => {}
        }
        Ok(())
    }

    // ---- SOCKS5 UDP ASSOCIATE (DHT, UDP trackers) ------------------------------------------- //
    async fn udp_associate(self: &Arc<Self>, tcp: &mut TcpStream) -> std::io::Result<()> {
        let relay = Arc::new(UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).await?);
        let port = relay.local_addr()?.port();
        let mut ok = vec![5, 0, 0, 1, 127, 0, 0, 1];
        ok.extend_from_slice(&port.to_be_bytes());
        tcp.write_all(&ok).await?;

        self.udp_assocs.fetch_add(1, Relaxed);
        let _open = Counted(&self.udp_assocs);
        let mut client: Option<SocketAddr> = None; // where the app sends from
        let mut out: HashMap<(u64, Ipv4Addr), Arc<UdpSocket>> = HashMap::new();
        let mut dns: HashMap<String, Ipv4Addr> = HashMap::new();
        let mut readers = JoinSet::new(); // ended when this function returns
        let mut buf = vec![0u8; 65536];
        let mut tcp_buf = [0u8; 512];
        loop {
            tokio::select! {
                // the association lives as long as this TCP connection
                r = tcp.read(&mut tcp_buf) => match r {
                    Ok(0) | Err(_) => return Ok(()),
                    Ok(_) => {}
                },
                r = relay.recv_from(&mut buf) => {
                    let Ok((n, from)) = r else { continue }; // e.g. "port unreachable" on Windows: ignore
                    if *client.get_or_insert(from) != from {
                        continue; // wrong sender
                    }
                    let Some((host, port, payload)) = parse_udp_request(&buf[..n]) else { continue };
                    let ip = match host {
                        UdpHost::Ip(ip) => ip,
                        UdpHost::Name(name) => match dns.get(&name) {
                            Some(ip) => *ip,
                            None => {
                                let lookup = tokio::time::timeout(Duration::from_secs(5), tokio::net::lookup_host((name.as_str(), port)));
                                let found = match lookup.await {
                                    Ok(Ok(mut it)) => it.find_map(|a| match a.ip() { IpAddr::V4(v4) => Some(v4), _ => None }),
                                    _ => None,
                                };
                                let Some(ip) = found else { continue };
                                dns.insert(name, ip);
                                ip
                            }
                        },
                    };
                    let Some(link) = self.udp_link() else { continue };
                    let local = link.ip().unwrap_or(Ipv4Addr::UNSPECIFIED);
                    // a new address (a reconnected phone) needs a new socket
                    let socket = match out.get(&(link.id, local)) {
                        Some(s) => s.clone(),
                        None => {
                            let Ok(s) = UdpSocket::bind((local, 0)).await else { continue };
                            let s = Arc::new(s);
                            out.insert((link.id, local), s.clone());
                            readers.spawn(udp_from_remote(s.clone(), relay.clone(), from, link.clone()));
                            s
                        }
                    };
                    if socket.send_to(payload, (ip, port)).await.is_ok() {
                        link.udp.fetch_add(payload.len() as u64, Relaxed);
                        self.udp_packets.fetch_add(1, Relaxed);
                    }
                }
            }
        }
    }
}

/// Takes a connection out of the books when its task ends, however it ends.
struct Registered<'a> {
    proxy: &'a Proxy,
    id: u64,
    link: &'a Link,
}

impl Drop for Registered<'_> {
    fn drop(&mut self) {
        self.link.active.fetch_sub(1, Relaxed);
        self.proxy.conns.lock_ok().remove(&self.id);
    }
}

struct Counted<'a>(&'a AtomicI64);

impl Drop for Counted<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Relaxed);
    }
}

/// Open a connection to `dest` that leaves through this link (the "Direct" pass-through link isn't bound).
async fn connect_via(link: &Link, dest: SocketAddr) -> std::io::Result<TcpStream> {
    let (ip, ip6) = {
        let c = link.ctl();
        (c.ip, c.ip6)
    };
    let socket = if dest.is_ipv6() { TcpSocket::new_v6()? } else { TcpSocket::new_v4()? };
    match (dest.is_ipv6(), ip, ip6) {
        (true, _, Some(ip6)) => socket.bind(SocketAddr::new(IpAddr::V6(ip6), 0))?,
        (false, Some(ip), _) => socket.bind(SocketAddr::new(IpAddr::V4(ip), 0))?,
        _ => {}
    }
    match tokio::time::timeout(CONNECT_TIMEOUT, socket.connect(dest)).await {
        Ok(r) => r,
        Err(_) => Err(std::io::ErrorKind::TimedOut.into()),
    }
}

/// App -> internet.
async fn pipe_up(from: &mut tokio::io::ReadHalf<&mut TcpStream>, mut to: OwnedWriteHalf, conn: &Conn, epoch: Instant) {
    let mut buf = vec![0u8; 8 * 1024];
    loop {
        let n = match from.read(&mut buf).await {
            Ok(0) | Err(_) => return,
            Ok(n) => n,
        };
        conn.link.up.fetch_add(n as u64, Relaxed);
        conn.add(n as u64, epoch);
        if to.write_all(&buf[..n]).await.is_err() || conn.link.over_quota() {
            return;
        }
    }
}

/// Internet -> app.
async fn pipe_down(mut from: OwnedReadHalf, to: &mut tokio::io::WriteHalf<&mut TcpStream>, conn: &Conn, epoch: Instant) {
    let mut buf = vec![0u8; 16 * 1024];
    loop {
        let n = match from.read(&mut buf).await {
            Ok(0) | Err(_) => return,
            Ok(n) => n,
        };
        conn.link.down.fetch_add(n as u64, Relaxed);
        conn.add(n as u64, epoch);
        if to.write_all(&buf[..n]).await.is_err() || conn.link.over_quota() {
            return;
        }
    }
}

enum UdpHost {
    Ip(Ipv4Addr),
    Name(String),
}

/// A SOCKS5 UDP request: RSV(2) FRAG(1) ATYP(1) address port data. Fragments and IPv6 aren't supported.
fn parse_udp_request(data: &[u8]) -> Option<(UdpHost, u16, &[u8])> {
    if data.len() < 10 || data[2] != 0 {
        return None;
    }
    let (host, i) = match data[3] {
        1 => (UdpHost::Ip(Ipv4Addr::new(data[4], data[5], data[6], data[7])), 8),
        3 => {
            let n = data[4] as usize;
            (UdpHost::Name(String::from_utf8_lossy(data.get(5..5 + n)?).into_owned()), 5 + n)
        }
        _ => return None,
    };
    let port = u16::from_be_bytes([*data.get(i)?, *data.get(i + 1)?]);
    Some((host, port, &data[i + 2..]))
}

/// Replies from the internet on one link's socket go back to the app, with the SOCKS5 UDP header in front.
async fn udp_from_remote(socket: Arc<UdpSocket>, relay: Arc<UdpSocket>, client: SocketAddr, link: Arc<Link>) {
    let mut buf = vec![0u8; 65536 + 10];
    loop {
        let (n, from) = match socket.recv_from(&mut buf[10..]).await {
            Ok(r) => r,
            Err(_) => {
                // "port unreachable" from a dead DHT node: normal, carry on
                tokio::time::sleep(Duration::from_millis(1)).await;
                continue;
            }
        };
        let SocketAddr::V4(from) = from else { continue };
        link.udp.fetch_add(n as u64, Relaxed);
        buf[..4].copy_from_slice(&[0, 0, 0, 1]);
        buf[4..8].copy_from_slice(&from.ip().octets());
        buf[8..10].copy_from_slice(&from.port().to_be_bytes());
        let _ = relay.send_to(&buf[..10 + n], client).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    fn two_links() -> (Arc<Proxy>, Arc<Link>, Arc<Link>) {
        let (a, b) = (Link::new("Ethernet"), Link::new("Hotspot"));
        a.ctl().ip = Some(Ipv4Addr::LOCALHOST);
        b.ctl().ip = Some(Ipv4Addr::LOCALHOST);
        (Proxy::new(vec![a.clone(), b.clone()]), a, b)
    }

    #[test]
    fn order_prefers_the_less_loaded_link() {
        let (p, a, b) = two_links();
        a.active.store(5, Relaxed);
        assert_eq!(p.order()[0].id, b.id);
        b.ctl().weight = 0.1; // b is much slower: 1/0.1 = 10 > 6/1
        assert_eq!(p.order()[0].id, a.id);
    }

    #[test]
    fn only_fresh_attempts_count_as_load() {
        let (p, a, b) = two_links();
        let now = Instant::now();
        for _ in 0..50 {
            b.add_pending_for_test(now - Duration::from_secs(5)); // unanswered peers piling up
        }
        a.active.store(3, Relaxed);
        assert_eq!(p.order()[0].id, b.id, "stale attempts must not push connections off the link");
        for _ in 0..5 {
            b.add_pending_for_test(now);
        }
        assert_eq!(p.order()[0].id, a.id, "fresh attempts do count");
    }

    #[test]
    fn unusable_links_are_left_out() {
        let (p, a, b) = two_links();
        b.ctl().ip = None;
        assert_eq!(p.order().len(), 1);
        b.ctl().ip = Some(Ipv4Addr::LOCALHOST);
        a.quota.store(100, Relaxed);
        a.down.store(100, Relaxed);
        assert_eq!(p.order()[0].id, b.id);
        assert_eq!(p.order().len(), 1);
        p.passthrough.store(true, Relaxed);
        assert_eq!(p.order()[0].id, p.direct.id);
    }

    #[test]
    fn weights_follow_speed_per_connection() {
        let (p, a, b) = two_links();
        let now = Instant::now();
        for (l, per) in [(&a, 100_000.0), (&b, 300_000.0)] {
            let mut c = l.ctl();
            c.per_conn = per;
            c.measured_at = Some(now);
        }
        p.update_weights();
        assert!((a.ctl().weight - 0.25).abs() < 1e-9);
        assert!((b.ctl().weight - 0.75).abs() < 1e-9);
        assert_eq!(p.preferred().unwrap().id, b.id);
        // a link that hasn't been measured is unknown, not slow
        a.ctl().measured_at = None;
        p.update_weights();
        assert_eq!(a.ctl().weight, 1.0);
        assert_eq!(b.ctl().weight, 1.0);
        assert!(p.preferred().is_none());
    }

    #[test]
    fn the_slower_link_keeps_a_minimum_share() {
        let (p, a, b) = two_links();
        let now = Instant::now();
        for (l, per) in [(&a, 1_000.0), (&b, 900_000.0)] {
            let mut c = l.ctl();
            c.per_conn = per;
            c.measured_at = Some(now);
        }
        p.update_weights();
        assert!((a.ctl().weight - 0.15).abs() < 1e-9);
        assert!((b.ctl().weight - 0.85).abs() < 1e-9);
    }

    #[test]
    fn manual_mode_leaves_weights_alone() {
        let (p, a, b) = two_links();
        p.auto.store(false, Relaxed);
        a.ctl().weight = 3.0;
        b.ctl().weight = 1.0;
        p.update_weights();
        assert_eq!(a.ctl().weight, 3.0);
    }

    /// A tiny server that answers every connection with "hello" + whatever it was sent.
    fn echo_server() -> u16 {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for s in l.incoming() {
                let Ok(mut s) = s else { break };
                std::thread::spawn(move || {
                    let _ = s.write_all(b"hello");
                    let mut buf = [0u8; 1024];
                    while let Ok(n) = s.read(&mut buf) {
                        if n == 0 || s.write_all(&buf[..n]).is_err() {
                            break;
                        }
                    }
                });
            }
        });
        port
    }

    fn socks_connect(proxy_port: u16, request: &[u8]) -> (std::net::TcpStream, u8) {
        let mut s = std::net::TcpStream::connect(("127.0.0.1", proxy_port)).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(15))).unwrap();
        s.write_all(&[5, 1, 0]).unwrap();
        let mut b = [0u8; 2];
        s.read_exact(&mut b).unwrap();
        assert_eq!(b, [5, 0]);
        s.write_all(request).unwrap();
        let mut r = [0u8; 10];
        s.read_exact(&mut r).unwrap();
        (s, r[1])
    }

    fn v4_request(port: u16) -> Vec<u8> {
        let mut req = vec![5, 1, 0, 1, 127, 0, 0, 1];
        req.extend_from_slice(&port.to_be_bytes());
        req
    }

    #[test]
    fn relays_data_and_spreads_connections() {
        let (p, a, b) = two_links();
        p.start(0).unwrap();
        let target = echo_server();
        let mut open = Vec::new();
        for _ in 0..6 {
            let (mut s, code) = socks_connect(p.port(), &v4_request(target));
            assert_eq!(code, 0);
            let mut hello = [0u8; 5];
            s.read_exact(&mut hello).unwrap();
            assert_eq!(&hello, b"hello");
            s.write_all(b"ping").unwrap();
            let mut back = [0u8; 4];
            s.read_exact(&mut back).unwrap();
            assert_eq!(&back, b"ping");
            open.push(s);
        }
        assert_eq!(a.active.load(Relaxed), 3);
        assert_eq!(b.active.load(Relaxed), 3);
        assert!(a.down.load(Relaxed) >= 27 && a.up.load(Relaxed) >= 12);

        // closing a link ends its connections; the others stay
        p.close_link(&a);
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(a.active.load(Relaxed), 0);
        assert_eq!(b.active.load(Relaxed), 3);
        drop(open);
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(b.active.load(Relaxed), 0);

        // stopping frees the port straight away
        let port = p.port();
        p.stop();
        assert!(!p.is_up());
        std::net::TcpListener::bind(("127.0.0.1", port)).expect("the port is free after stop()");
    }

    #[test]
    fn names_are_resolved_and_failures_reported() {
        let (p, a, b) = two_links();
        p.start(0).unwrap();
        let target = echo_server();
        let mut req = vec![5, 1, 0, 3, 9];
        req.extend_from_slice(b"localhost");
        req.extend_from_slice(&target.to_be_bytes());
        let (_s, code) = socks_connect(p.port(), &req);
        assert_eq!(code, 0);

        // nothing listens on this port: both links are tried, then the app is told
        let dead = {
            let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            l.local_addr().unwrap().port()
        };
        let (_s, code) = socks_connect(p.port(), &v4_request(dead));
        assert_eq!(code, 5);
        assert_eq!(p.failed.load(Relaxed), 1);
        assert_eq!(a.failures.load(Relaxed) + b.failures.load(Relaxed), 2);

        // IPv6 with no IPv6 link is refused and counted
        let mut req = vec![5, 1, 0, 4];
        req.extend_from_slice(&Ipv6Addr::LOCALHOST.octets());
        req.extend_from_slice(&target.to_be_bytes());
        let (_s, code) = socks_connect(p.port(), &req);
        assert_eq!(code, 8);
        assert_eq!(p.refused_ipv6.load(Relaxed), 1);

        // BIND isn't supported
        let (_s, code) = socks_connect(p.port(), &[5, 2, 0, 1, 127, 0, 0, 1, 0, 80]);
        assert_eq!(code, 7);
        p.stop();
    }

    #[test]
    fn passthrough_connects_without_any_link() {
        let (p, a, b) = two_links();
        a.ctl().ip = None;
        b.ctl().ip = None;
        p.passthrough.store(true, Relaxed);
        p.start(0).unwrap();
        let target = echo_server();
        let (_s, code) = socks_connect(p.port(), &v4_request(target));
        assert_eq!(code, 0);
        std::thread::sleep(Duration::from_millis(100));
        assert_eq!(p.direct.active.load(Relaxed), 1);
        p.stop();
    }

    #[test]
    fn a_link_over_its_data_limit_is_cut_off() {
        let (p, a, b) = two_links();
        b.ctl().ip = None;
        a.quota.store(1000, Relaxed);
        p.start(0).unwrap();
        let target = echo_server();
        let (mut s, code) = socks_connect(p.port(), &v4_request(target));
        assert_eq!(code, 0);
        let _ = s.write_all(&[7u8; 2000]);
        let mut sink = Vec::new();
        let _ = s.read_to_end(&mut sink); // the proxy closes the connection
        assert!(a.over_quota());
        let (_s, code) = socks_connect(p.port(), &v4_request(target));
        assert_eq!(code, 5, "no usable link is left");
        p.stop();
    }

    #[test]
    fn udp_associate_relays_datagrams() {
        let (p, a, _b) = two_links();
        p.start(0).unwrap();
        let server = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        let server_port = server.local_addr().unwrap().port();
        std::thread::spawn(move || {
            let mut buf = [0u8; 2048];
            while let Ok((n, from)) = server.recv_from(&mut buf) {
                let _ = server.send_to(&buf[..n], from);
            }
        });
        let mut tcp = std::net::TcpStream::connect(("127.0.0.1", p.port())).unwrap();
        tcp.write_all(&[5, 1, 0]).unwrap();
        let mut b = [0u8; 2];
        tcp.read_exact(&mut b).unwrap();
        tcp.write_all(&[5, 3, 0, 1, 0, 0, 0, 0, 0, 0]).unwrap();
        let mut r = [0u8; 10];
        tcp.read_exact(&mut r).unwrap();
        assert_eq!(r[1], 0);
        let relay_port = u16::from_be_bytes([r[8], r[9]]);
        assert_eq!(p.udp_assocs.load(Relaxed), 1);

        let app = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        app.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        let mut packet = vec![0, 0, 0, 1, 127, 0, 0, 1];
        packet.extend_from_slice(&server_port.to_be_bytes());
        packet.extend_from_slice(b"dht ping");
        app.send_to(&packet, ("127.0.0.1", relay_port)).unwrap();
        let mut buf = [0u8; 2048];
        let (n, _) = app.recv_from(&mut buf).unwrap();
        assert_eq!(&buf[..4], &[0, 0, 0, 1]);
        assert_eq!(&buf[10..n], b"dht ping");
        assert_eq!(a.udp.load(Relaxed), 16, "UDP stays on the first link, counted both ways");
        assert_eq!(a.down.load(Relaxed), 0, "and out of the speed measurement");

        drop(tcp); // closing the TCP connection ends the association
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(p.udp_assocs.load(Relaxed), 0);
        p.stop();
    }
}
