"""
Traffic check: which programs use DualLink, which ones go around it, and how much traffic
Windows sees on each connection compared with what DualLink carries.

Uses Windows' own tools (ipconfig, netstat, tasklist, netsh). The parse_* functions are pure so they
can be tested on any OS with sample output.
"""
import csv
import io
import os
import subprocess

NO_WINDOW = 0x08000000


def _run(args, timeout=15):
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="ignore", timeout=timeout,
                              creationflags=NO_WINDOW if os.name == "nt" else 0).stdout
    except Exception:
        return ""


# ---- ipconfig: adapter -> IPv4 + IPv6 addresses ----------------------------------- #
def parse_ipconfig(text):
    """{adapter_name: {"v4": [...], "v6": [...]}} - adapter names without the 'Ethernet adapter ' prefix."""
    from .controller import pretty_adapter
    out, name = {}, None
    for line in text.splitlines():
        if line.strip() and not line[0].isspace() and line.rstrip().endswith(":"):
            name = pretty_adapter(line.rstrip()[:-1].strip())
            out.setdefault(name, {"v4": [], "v6": []})
            continue
        if name is None or " : " not in line:
            continue
        label, _, value = line.partition(" : ")
        value = value.split("(")[0].strip().split("%")[0]
        if "IPv4" in label and value.count(".") == 3:
            out[name]["v4"].append(value)
        elif "IPv6" in label and ":" in value and not value.lower().startswith("fe80"):
            if "Temporary" in label:       # prefer the stable address; temporary ones rotate
                out[name]["v6"].append(value.lower())
            else:
                out[name]["v6"].insert(0, value.lower())
    return out


def adapter_ipv6():
    """{adapter_name: global IPv6 address} (Windows only)."""
    if os.name != "nt":
        return {}
    return {n: a["v6"][0] for n, a in parse_ipconfig(_run(["ipconfig"])).items() if a["v6"]}


# ---- netstat -ano: TCP connections with owning process ------------------------------ #
def _split_endpoint(ep):
    if ep.startswith("["):                       # [2a00::1]:443
        host, _, port = ep[1:].rpartition("]:")
        return host.split("%")[0].lower(), port
    host, _, port = ep.rpartition(":")
    return host, port


def parse_netstat(text):
    """[(local_ip, local_port, remote_ip, remote_port, pid)] for TCP connections (listening sockets skipped)."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 5 or parts[0].upper() != "TCP" or not parts[4].isdigit():
            continue
        lip, lport = _split_endpoint(parts[1])
        rip, rport = _split_endpoint(parts[2])
        if rip in ("0.0.0.0", "*", "::") or rport in ("0", "*"):
            continue
        rows.append((lip, lport, rip, rport, int(parts[4])))
    return rows


# ---- tasklist: pid -> program name -------------------------------------------------- #
def parse_tasklist(text):
    names = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[1].isdigit():
            names[int(row[1])] = row[0]
    return names


# ---- netsh subinterfaces: adapter -> bytes received --------------------------------- #
def parse_subinterfaces(text):
    """{interface_name: bytes_in} from 'netsh interface ipv4|ipv6 show subinterfaces'."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and all(p.isdigit() for p in parts[:4]):
            out[" ".join(parts[4:])] = int(parts[2])
    return out


def adapter_rx_bytes():
    """Bytes received per adapter, IPv4 + IPv6 together. Empty dict when unavailable."""
    total = {}
    for fam in ("ipv4", "ipv6"):
        for name, b in parse_subinterfaces(_run(["netsh", "interface", fam, "show", "subinterfaces"])).items():
            total[name] = total.get(name, 0) + b
    return total


def connections_snapshot(proxy_port, own_pid):
    """Who is connected through DualLink, and who connects directly over each adapter."""
    adapters = parse_ipconfig(_run(["ipconfig"]))
    rows = parse_netstat(_run(["netstat", "-ano", "-p", "TCP"]) + "\n" + _run(["netstat", "-ano", "-p", "TCPv6"]))
    names = parse_tasklist(_run(["tasklist", "/fo", "csv", "/nh"]))
    return summarize(rows, names, adapters, proxy_port, own_pid)


def summarize(rows, names, adapters, proxy_port, own_pid):
    ip_to_adapter = {}
    for adapter, addrs in adapters.items():
        for ip in addrs["v4"] + addrs["v6"]:
            ip_to_adapter[ip] = adapter
    through, direct = {}, {}
    for lip, lport, rip, rport, pid in rows:
        prog = names.get(pid, f"PID {pid}")
        if rip in ("127.0.0.1", "::1") and rport == str(proxy_port) and pid != own_pid:
            through[prog] = through.get(prog, 0) + 1
        elif lip in ip_to_adapter and pid != own_pid and rip not in ("127.0.0.1", "::1"):
            per = direct.setdefault(ip_to_adapter[lip], {})
            per[prog] = per.get(prog, 0) + 1
    return {"through": through, "direct": direct}
