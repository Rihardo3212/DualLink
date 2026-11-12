//! Traffic check and live app status: which programs use DualLink, and which ones go around it.

use crate::net::{self, RawAdapter, TcpRow};
use duallink_common::LockExt;
use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::Mutex;

/// Program -> number of connections.
pub type Counts = HashMap<String, u32>;

#[derive(Debug, Default)]
pub struct Snapshot {
    /// Programs connected to DualLink's proxy.
    pub through: Counts,
    /// Adapter -> programs connecting straight over it.
    pub direct: HashMap<String, Counts>,
}

fn to_proxy(r: &TcpRow, proxy_port: u16) -> bool {
    r.remote_ip.is_loopback() && r.remote_port == proxy_port
}

/// "System Idle Process" / "System": closed connections waiting to expire.
fn system(pid: u32) -> bool {
    pid == 0 || pid == 4
}

pub fn summarize(rows: &[TcpRow], names: &HashMap<u32, String>, adapters: &[RawAdapter], proxy_port: u16, own_pid: u32) -> Snapshot {
    let mut by_ip: HashMap<IpAddr, &str> = HashMap::new();
    for a in adapters {
        for ip in &a.v4 {
            by_ip.insert(IpAddr::V4(*ip), &a.name);
        }
        for ip in &a.v6 {
            by_ip.insert(IpAddr::V6(*ip), &a.name);
        }
    }
    let mut snap = Snapshot::default();
    for r in rows {
        if system(r.pid) || r.pid == own_pid {
            continue;
        }
        let prog = names.get(&r.pid).cloned().unwrap_or_else(|| format!("PID {}", r.pid));
        if to_proxy(r, proxy_port) {
            *snap.through.entry(prog).or_default() += 1;
        } else if let (Some(adapter), false) = (by_ip.get(&r.local_ip), r.remote_ip.is_loopback()) {
            *snap.direct.entry(adapter.to_string()).or_default().entry(prog).or_default() += 1;
        }
    }
    snap
}

/// Who is connected through DualLink, and who connects directly over each adapter.
pub fn connections_snapshot(proxy_port: u16, own_pid: u32) -> Snapshot {
    let rows = net::tcp_connections();
    let pids: Vec<u32> = rows.iter().map(|r| r.pid).collect();
    summarize(&rows, &net::process_names(&pids), &net::adapters(), proxy_port, own_pid)
}

/// Bytes received per adapter. Empty when unavailable.
pub fn adapter_rx_bytes() -> HashMap<String, u64> {
    net::adapters().into_iter().filter_map(|a| a.rx_bytes.map(|b| (a.name, b))).collect()
}

static NAMES: Mutex<Option<HashMap<u32, String>>> = Mutex::new(None);

/// {program: connections} for programs connected to DualLink's port right now.
pub fn proxy_clients(proxy_port: u16, own_pid: u32) -> Counts {
    let mine: Vec<u32> = net::tcp_connections()
        .iter()
        .filter(|r| to_proxy(r, proxy_port) && !system(r.pid) && r.pid != own_pid)
        .map(|r| r.pid)
        .collect();
    let mut cache = NAMES.lock_ok();
    let names = cache.get_or_insert_with(HashMap::new);
    if mine.iter().any(|pid| !names.contains_key(pid)) {
        // only look up names when a new program shows up
        names.extend(net::process_names(&mine));
    }
    let mut counts = Counts::new();
    for pid in mine {
        *counts.entry(names.get(&pid).cloned().unwrap_or_else(|| format!("PID {pid}"))).or_default() += 1;
    }
    counts
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::Ipv4Addr;

    fn row(local: [u8; 4], remote: [u8; 4], port: u16, pid: u32) -> TcpRow {
        TcpRow { local_ip: IpAddr::V4(local.into()), remote_ip: IpAddr::V4(remote.into()), remote_port: port, pid }
    }

    #[test]
    fn sorts_connections_into_through_and_direct() {
        let adapters = vec![
            RawAdapter { name: "Ethernet".into(), v4: vec![Ipv4Addr::new(192, 168, 8, 5)], ..Default::default() },
            RawAdapter { name: "Phone".into(), v4: vec![Ipv4Addr::new(172, 20, 10, 3)], ..Default::default() },
        ];
        let names: HashMap<u32, String> = [(10, "qbittorrent.exe".to_string()), (20, "msedge.exe".to_string())].into();
        let rows = vec![
            row([127, 0, 0, 1], [127, 0, 0, 1], 1080, 10), // qBittorrent -> DualLink
            row([127, 0, 0, 1], [127, 0, 0, 1], 1080, 10),
            row([172, 20, 10, 3], [1, 2, 3, 4], 443, 20),  // Edge straight over the phone
            row([192, 168, 8, 5], [1, 2, 3, 4], 443, 99),  // DualLink's own outgoing connection
            row([192, 168, 8, 5], [5, 6, 7, 8], 443, 77),  // a program we can't name
            row([127, 0, 0, 1], [127, 0, 0, 1], 9999, 20), // local chatter
            row([192, 168, 8, 5], [1, 2, 3, 4], 443, 4),   // "System"
        ];
        let s = summarize(&rows, &names, &adapters, 1080, 99);
        assert_eq!(s.through, [("qbittorrent.exe".to_string(), 2)].into());
        assert_eq!(s.direct["Phone"], [("msedge.exe".to_string(), 1)].into());
        assert_eq!(s.direct["Ethernet"], [("PID 77".to_string(), 1)].into());
    }
}
