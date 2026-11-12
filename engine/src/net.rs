//! Network information straight from Windows (iphlpapi / kernel32): adapters and their addresses, TCP
//! connections with the owning program, and bytes received per adapter. On other systems (development
//! only) everything is empty.

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

/// A network adapter that is up.
#[derive(Debug, Clone, Default)]
pub struct RawAdapter {
    pub name: String,
    pub v4: Vec<Ipv4Addr>,
    /// Global IPv6 addresses, stable ones first, then temporary ones (those rotate).
    pub v6: Vec<Ipv6Addr>,
    /// Bytes received since the adapter came up, IPv4 + IPv6.
    pub rx_bytes: Option<u64>,
}

/// A TCP connection (listening sockets are left out).
#[derive(Debug, Clone)]
pub struct TcpRow {
    pub local_ip: IpAddr,
    pub remote_ip: IpAddr,
    pub remote_port: u16,
    pub pid: u32,
}

/// An adapter as the rest of DualLink sees it: one IPv4 address, maybe an IPv6 one.
#[derive(Debug, Clone, PartialEq)]
pub struct Adapter {
    pub name: String,
    pub ip: Ipv4Addr,
    pub ip6: Option<Ipv6Addr>,
    pub hotspot: bool,
}

pub fn looks_like_hotspot(name: &str, ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    let low = name.to_lowercase();
    matches!((o[0], o[1], o[2]), (192, 168, 43) | (172, 20, 10) | (192, 168, 42) | (192, 168, 137))
        || ["rndis", "wi-fi", "wifi", "wireless", "wlan", "mobile", "tether", "usb"].iter().any(|k| low.contains(k))
}

/// Phone tethering address ranges (iPhone, Android USB/Wi-Fi). Used to find the hotspot again if it comes
/// back as a differently-named adapter (e.g. a different USB port).
pub fn in_phone_range(ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    matches!((o[0], o[1], o[2]), (172, 20, 10) | (192, 168, 42) | (192, 168, 43))
}

/// Usable adapters: the first real IPv4 address of each (no loopback, no self-assigned 169.254 address).
pub fn simplify(raw: Vec<RawAdapter>) -> Vec<Adapter> {
    let mut out: Vec<Adapter> = Vec::new();
    for a in raw {
        let ip = a.v4.iter().copied().find(|ip| !ip.is_loopback() && !ip.is_link_local());
        let Some(ip) = ip else { continue };
        if out.iter().any(|o| o.name == a.name || o.ip == ip) {
            continue;
        }
        out.push(Adapter { hotspot: looks_like_hotspot(&a.name, ip), name: a.name, ip, ip6: a.v6.first().copied() });
    }
    out
}

/// What the system has right now.
pub fn scan() -> Vec<Adapter> {
    simplify(adapters())
}

pub use imp::{adapters, process_names, tcp_connections};

#[cfg(not(windows))]
mod imp {
    use super::*;

    pub fn adapters() -> Vec<RawAdapter> {
        Vec::new()
    }
    pub fn tcp_connections() -> Vec<TcpRow> {
        Vec::new()
    }
    pub fn process_names(_pids: &[u32]) -> HashMap<u32, String> {
        HashMap::new()
    }
}

#[cfg(windows)]
mod imp {
    use super::*;
    use windows_sys::Win32::Foundation::{CloseHandle, ERROR_BUFFER_OVERFLOW, ERROR_INSUFFICIENT_BUFFER};
    use windows_sys::Win32::NetworkManagement::IpHelper::{
        GetAdaptersAddresses, GetExtendedTcpTable, GetIfEntry2, IP_ADAPTER_ADDRESSES_LH, MIB_IF_ROW2,
        MIB_TCP6ROW_OWNER_PID, MIB_TCPROW_OWNER_PID, TCP_TABLE_OWNER_PID_ALL,
    };
    use windows_sys::Win32::Networking::WinSock::{AF_INET, AF_INET6, AF_UNSPEC, SOCKADDR, SOCKADDR_IN, SOCKADDR_IN6};
    use windows_sys::Win32::System::Threading::{OpenProcess, QueryFullProcessImageNameW, PROCESS_QUERY_LIMITED_INFORMATION};

    const IF_OPER_UP: i32 = 1;
    const IF_TYPE_LOOPBACK: u32 = 24;
    const DAD_PREFERRED: i32 = 4; // NL_DAD_STATE
    const SUFFIX_RANDOM: i32 = 5; // NL_SUFFIX_ORIGIN: a temporary IPv6 address
    const SKIP_ANYCAST_MULTICAST_DNS: u32 = 0x2 | 0x4 | 0x8;
    const TCP_STATE_LISTEN: u32 = 2;

    /// A zeroed buffer of at least `bytes` bytes, aligned for the structures Windows writes into it.
    fn buffer(bytes: usize) -> Vec<u64> {
        vec![0u64; bytes.div_ceil(8)]
    }

    unsafe fn wide_to_string(p: *const u16) -> String {
        if p.is_null() {
            return String::new();
        }
        let mut n = 0;
        while *p.add(n) != 0 {
            n += 1;
        }
        String::from_utf16_lossy(std::slice::from_raw_parts(p, n))
    }

    pub fn adapters() -> Vec<RawAdapter> {
        let mut size: u32 = 32 * 1024;
        let mut buf = buffer(size as usize);
        let mut ok = false;
        for _ in 0..5 {
            buf = buffer(size as usize);
            // SAFETY: buf is at least `size` bytes and aligned; Windows fills it with a linked list
            // that points into buf itself.
            let ret = unsafe {
                GetAdaptersAddresses(AF_UNSPEC as u32, SKIP_ANYCAST_MULTICAST_DNS, std::ptr::null(), buf.as_mut_ptr().cast(), &mut size)
            };
            if ret == 0 {
                ok = true;
                break;
            }
            if ret != ERROR_BUFFER_OVERFLOW {
                return Vec::new();
            }
        }
        if !ok {
            return Vec::new();
        }
        let mut out = Vec::new();
        let mut p = buf.as_ptr() as *const IP_ADAPTER_ADDRESSES_LH;
        // SAFETY: the list was written by GetAdaptersAddresses into buf, which outlives this loop; every
        // pointer in it is either null or points inside buf.
        unsafe {
            while !p.is_null() {
                let a = &*p;
                if a.OperStatus == IF_OPER_UP && a.IfType != IF_TYPE_LOOPBACK {
                    let (mut v4, mut v6, mut v6_temp) = (Vec::new(), Vec::new(), Vec::new());
                    let mut u = a.FirstUnicastAddress;
                    while !u.is_null() {
                        let ua = &*u;
                        let sa = ua.Address.lpSockaddr as *const SOCKADDR;
                        if !sa.is_null() {
                            let family = (*sa).sa_family;
                            if family == AF_INET {
                                let sin = &*(sa as *const SOCKADDR_IN);
                                v4.push(Ipv4Addr::from(sin.sin_addr.S_un.S_addr.to_ne_bytes()));
                            } else if family == AF_INET6 && ua.DadState == DAD_PREFERRED {
                                let sin6 = &*(sa as *const SOCKADDR_IN6);
                                let ip = Ipv6Addr::from(sin6.sin6_addr.u.Byte);
                                if (ip.segments()[0] & 0xffc0) != 0xfe80 {
                                    if ua.SuffixOrigin == SUFFIX_RANDOM { v6_temp.push(ip) } else { v6.push(ip) }
                                }
                            }
                        }
                        u = ua.Next;
                    }
                    v6.extend(v6_temp);
                    let mut row: MIB_IF_ROW2 = std::mem::zeroed();
                    row.InterfaceLuid = a.Luid;
                    let rx_bytes = (GetIfEntry2(&mut row) == 0).then_some(row.InOctets);
                    out.push(RawAdapter { name: wide_to_string(a.FriendlyName), v4, v6, rx_bytes });
                }
                p = a.Next;
            }
        }
        out
    }

    /// The raw table for one address family: (bytes, number of rows).
    fn tcp_table(af: u16) -> Option<(Vec<u64>, usize)> {
        let mut size: u32 = 0;
        // SAFETY: a null table with size 0 just asks for the size needed.
        unsafe { GetExtendedTcpTable(std::ptr::null_mut(), &mut size, 0, af as u32, TCP_TABLE_OWNER_PID_ALL, 0) };
        for _ in 0..5 {
            size += 16 * 1024; // room for connections opened meanwhile
            let mut buf = buffer(size as usize);
            // SAFETY: buf is at least `size` bytes and aligned.
            let ret = unsafe { GetExtendedTcpTable(buf.as_mut_ptr().cast(), &mut size, 0, af as u32, TCP_TABLE_OWNER_PID_ALL, 0) };
            if ret == 0 {
                let n = (buf[0] & 0xffff_ffff) as usize; // dwNumEntries, the first 4 bytes
                return Some((buf, n));
            }
            if ret != ERROR_INSUFFICIENT_BUFFER {
                return None;
            }
        }
        None
    }

    fn port(dw: u32) -> u16 {
        u16::from_be((dw & 0xffff) as u16)
    }

    pub fn tcp_connections() -> Vec<TcpRow> {
        let mut rows = Vec::new();
        if let Some((buf, n)) = tcp_table(AF_INET) {
            // SAFETY: the table is a u32 count followed by n rows (4-byte aligned), all inside buf.
            let table = unsafe { std::slice::from_raw_parts((buf.as_ptr() as *const u8).add(4) as *const MIB_TCPROW_OWNER_PID, n) };
            for r in table {
                if r.dwState == TCP_STATE_LISTEN {
                    continue;
                }
                rows.push(TcpRow {
                    local_ip: IpAddr::V4(Ipv4Addr::from(r.dwLocalAddr.to_ne_bytes())),
                    remote_ip: IpAddr::V4(Ipv4Addr::from(r.dwRemoteAddr.to_ne_bytes())),
                    remote_port: port(r.dwRemotePort),
                    pid: r.dwOwningPid,
                });
            }
        }
        if let Some((buf, n)) = tcp_table(AF_INET6) {
            // SAFETY: as above, with the IPv6 row layout.
            let table = unsafe { std::slice::from_raw_parts((buf.as_ptr() as *const u8).add(4) as *const MIB_TCP6ROW_OWNER_PID, n) };
            for r in table {
                if r.dwState == TCP_STATE_LISTEN {
                    continue;
                }
                rows.push(TcpRow {
                    local_ip: IpAddr::V6(Ipv6Addr::from(r.ucLocalAddr)),
                    remote_ip: IpAddr::V6(Ipv6Addr::from(r.ucRemoteAddr)),
                    remote_port: port(r.dwRemotePort),
                    pid: r.dwOwningPid,
                });
            }
        }
        rows
    }

    /// {pid: "program.exe"} for the programs we're allowed to look at.
    pub fn process_names(pids: &[u32]) -> HashMap<u32, String> {
        let mut out = HashMap::new();
        for &pid in pids {
            if out.contains_key(&pid) {
                continue;
            }
            // SAFETY: the handle is checked, used for one query into a buffer of the stated size, and closed.
            unsafe {
                let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
                if h.is_null() {
                    continue;
                }
                let mut buf = [0u16; 1024];
                let mut len = buf.len() as u32;
                if QueryFullProcessImageNameW(h, 0, buf.as_mut_ptr(), &mut len) != 0 {
                    let path = String::from_utf16_lossy(&buf[..len as usize]);
                    let name = path.rsplit(['\\', '/']).next().unwrap_or(&path).to_string();
                    out.insert(pid, name);
                }
                CloseHandle(h);
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raw(name: &str, v4: &[[u8; 4]]) -> RawAdapter {
        RawAdapter { name: name.into(), v4: v4.iter().map(|o| Ipv4Addr::from(*o)).collect(), ..Default::default() }
    }

    #[test]
    fn hotspot_detection() {
        assert!(looks_like_hotspot("Ethernet 2", Ipv4Addr::new(172, 20, 10, 3)));
        assert!(looks_like_hotspot("Wi-Fi", Ipv4Addr::new(10, 0, 0, 2)));
        assert!(!looks_like_hotspot("Ethernet", Ipv4Addr::new(192, 168, 8, 5)));
        assert!(in_phone_range(Ipv4Addr::new(192, 168, 42, 9)));
        assert!(!in_phone_range(Ipv4Addr::new(192, 168, 137, 1)));
    }

    #[test]
    fn simplify_keeps_one_real_address_per_adapter() {
        let got = simplify(vec![
            raw("Ethernet", &[[169, 254, 3, 4], [192, 168, 8, 5], [192, 168, 8, 6]]),
            raw("Loop", &[[127, 0, 0, 1]]),
            raw("Phone", &[[172, 20, 10, 3]]),
            raw("Empty", &[]),
        ]);
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].ip, Ipv4Addr::new(192, 168, 8, 5));
        assert!(!got[0].hotspot);
        assert!(got[1].hotspot);
    }

    #[test]
    fn the_real_system_can_be_asked() {
        // On Windows (CI) this talks to the real API; elsewhere it is empty. Either way it must not crash.
        let adapters = adapters();
        let rows = tcp_connections();
        let pids: Vec<u32> = rows.iter().map(|r| r.pid).collect();
        let names = process_names(&pids);
        if cfg!(windows) {
            assert!(!adapters.is_empty(), "a Windows machine has at least one adapter that is up");
            assert!(names.len() <= pids.len());
        }
    }
}
