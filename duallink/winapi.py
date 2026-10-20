"""
Network information straight from Windows (iphlpapi / kernel32), instead of starting ipconfig, netstat and
tasklist every few seconds. Every caller falls back to those tools if anything here fails.
Tested against the command-line tools on a real Windows machine in CI (tests/test_winapi.py).
"""
import ctypes
import os
import socket
import struct
from ctypes import wintypes

AF_UNSPEC, AF_INET, AF_INET6 = 0, 2, 23
_IF_OPER_UP, _IF_TYPE_LOOPBACK = 1, 24
_DAD_PREFERRED, _SUFFIX_RANDOM = 4, 5          # NL_DAD_STATE, NL_SUFFIX_ORIGIN (temporary IPv6 address)
_TCP_TABLE_OWNER_PID_ALL, _TCP_STATE_LISTEN = 5, 2
_ERROR_BUFFER_OVERFLOW, _ERROR_INSUFFICIENT_BUFFER = 111, 122


class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class _UNICAST(ctypes.Structure):
    pass


_UNICAST._fields_ = [
    ("Length", wintypes.ULONG), ("Flags", wintypes.DWORD),
    ("Next", ctypes.POINTER(_UNICAST)),
    ("Address", _SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int), ("SuffixOrigin", ctypes.c_int), ("DadState", ctypes.c_int),
    ("ValidLifetime", wintypes.ULONG), ("PreferredLifetime", wintypes.ULONG), ("LeaseLifetime", wintypes.ULONG),
    ("OnLinkPrefixLength", ctypes.c_uint8),
]


class _ADAPTER(ctypes.Structure):
    pass


_ADAPTER._fields_ = [
    ("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(_ADAPTER)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_UNICAST)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG),
    ("Mtu", wintypes.ULONG),
    ("IfType", wintypes.DWORD),
    ("OperStatus", ctypes.c_int),
]


class _TCPROW(ctypes.Structure):
    _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]


class _TCP6ROW(ctypes.Structure):
    _fields_ = [("ucLocalAddr", ctypes.c_ubyte * 16), ("dwLocalScopeId", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD), ("ucRemoteAddr", ctypes.c_ubyte * 16),
                ("dwRemoteScopeId", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD),
                ("dwState", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]


def adapters():
    """Adapters that are up: [{"name", "description", "v4": [...], "v6": [stable first, then temporary]}]."""
    iphlp = ctypes.WinDLL("iphlpapi")
    size = wintypes.ULONG(32 * 1024)
    for _ in range(5):
        buf = ctypes.create_string_buffer(size.value)
        ret = iphlp.GetAdaptersAddresses(AF_UNSPEC, 0x2 | 0x4 | 0x8, None, buf, ctypes.byref(size))
        if ret == 0:
            break
        if ret != _ERROR_BUFFER_OVERFLOW:
            raise OSError(ret, "GetAdaptersAddresses failed")
    else:
        raise OSError("GetAdaptersAddresses: buffer keeps growing")
    out = []
    p = ctypes.cast(buf, ctypes.POINTER(_ADAPTER))
    while p:
        a = p.contents
        if a.OperStatus == _IF_OPER_UP and a.IfType != _IF_TYPE_LOOPBACK:
            v4, v6, v6_temp = [], [], []
            u = a.FirstUnicastAddress
            while u:
                ua = u.contents
                sa = ua.Address.lpSockaddr
                if sa:
                    family = ctypes.cast(sa, ctypes.POINTER(ctypes.c_ushort))[0]
                    if family == AF_INET:
                        v4.append(socket.inet_ntoa(ctypes.string_at(sa + 4, 4)))
                    elif family == AF_INET6 and ua.DadState == _DAD_PREFERRED:
                        ip = socket.inet_ntop(socket.AF_INET6, ctypes.string_at(sa + 8, 16))
                        if not ip.startswith("fe80"):
                            (v6_temp if ua.SuffixOrigin == _SUFFIX_RANDOM else v6).append(ip)
                u = ua.Next
            out.append({"name": a.FriendlyName or "", "description": a.Description or "", "v4": v4, "v6": v6 + v6_temp})
        p = a.Next
    return out


def _tcp_rows(af, row_type):
    iphlp = ctypes.WinDLL("iphlpapi")
    size = wintypes.DWORD(0)
    iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, af, _TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(5):
        buf = ctypes.create_string_buffer(size.value + 16 * 1024)    # room for connections opened meanwhile
        size = wintypes.DWORD(len(buf))
        ret = iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, af, _TCP_TABLE_OWNER_PID_ALL, 0)
        if ret == 0:
            break
        if ret != _ERROR_INSUFFICIENT_BUFFER:
            raise OSError(ret, "GetExtendedTcpTable failed")
    else:
        raise OSError("GetExtendedTcpTable: buffer keeps growing")
    n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
    return (row_type * n).from_buffer_copy(buf, 4) if n else []


def _port(dw):
    return str(socket.ntohs(dw & 0xFFFF))


def tcp_connections():
    """[(local_ip, local_port, remote_ip, remote_port, pid)] - same shape as diag.parse_netstat()."""
    rows = []
    for r in _tcp_rows(AF_INET, _TCPROW):
        if r.dwState == _TCP_STATE_LISTEN:
            continue
        rows.append((socket.inet_ntoa(struct.pack("<I", r.dwLocalAddr)), _port(r.dwLocalPort),
                     socket.inet_ntoa(struct.pack("<I", r.dwRemoteAddr)), _port(r.dwRemotePort), r.dwOwningPid))
    for r in _tcp_rows(AF_INET6, _TCP6ROW):
        if r.dwState == _TCP_STATE_LISTEN:
            continue
        rows.append((socket.inet_ntop(socket.AF_INET6, bytes(r.ucLocalAddr)), _port(r.dwLocalPort),
                     socket.inet_ntop(socket.AF_INET6, bytes(r.ucRemoteAddr)), _port(r.dwRemotePort), r.dwOwningPid))
    return rows


def process_names(pids):
    """{pid: "program.exe"} for the pids we're allowed to look at."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    out = {}
    for pid in set(pids):
        h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                out[pid] = os.path.basename(buf.value)
        finally:
            k.CloseHandle(h)
    return out
