# Linux plan: headless engine

Not built yet. This is the plan for when Rihards moves to Linux (CachyOS).

## Idea
Run the same Python engine with no tray icon and no window. It reads `~/.config/DualLink/config.json`, and
you control it from the terminal or as a systemd user service. Apps point at the SOCKS5 proxy just as they do
on Windows. The WinUI window can't run on Linux; a Linux window (GTK/Qt) could talk to the same control API later.

No root needed: since Linux 5.7, normal users can use `SO_BINDTODEVICE`, which ties a socket to one adapter.

## Changes (about 150 lines)

### 1. `duallink/netlinux.py` (new)
Finds the network adapters. It works like `winapi.adapters()`, but asks Linux's `ip` command instead:
```python
import json, subprocess

def adapters():
    out = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=5).stdout
    res = []
    for a in json.loads(out):
        if a["ifname"] == "lo" or "UP" not in a.get("flags", []):
            continue
        v4 = [x["local"] for x in a["addr_info"] if x["family"] == "inet"]
        v6 = [x["local"] for x in a["addr_info"] if x["family"] == "inet6" and x.get("scope") == "global"]
        res.append({"name": a["ifname"], "v4": v4, "v6": v6})
    return res
```
On Linux the adapter name is the device name (for example `enp3s0` or `usb0`), which is exactly what step 2
needs.

### 2. `engine.py` (about 10 lines)
- `list_adapters()`: add a `sys.platform == "linux"` branch that uses `netlinux`. The current fallback
  (`gethostname()`) returns `127.0.1.1` on Linux, so it finds nothing useful.
- In the connect loop (before the existing `s.bind(...)`), tie each socket to its adapter:
  ```python
  if sys.platform == "linux" and cand.ip:
      s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, cand.name.encode())
  ```
  Without this line, Linux sends everything out the main default route (Ethernet), whatever IP the socket is
  bound to.
- `looks_like_hotspot()`: add the Linux names `enx...` (USB tethering), `wlp...`/`wl...` (Wi-Fi) and `usb0`.

### 3. `duallink/headless.py` (new, `python -m duallink.headless`)
- creates a `Controller()` and turns it on; `paths.config_dir()` already falls back to `~/.config` when there's
  no `%APPDATA%`
- prints a status line every few seconds (each link's speed and connection count)
- stops cleanly on Ctrl+C or SIGTERM
- options: `--port`, `--off`, `--speedtest`

It skips `service.py`, the tray, `instance.py` and the updater. It can still start the control API, so a Linux
window could plug in later.

### 4. Autostart (optional)
A systemd user unit, `~/.config/systemd/user/duallink.service`:
```ini
[Unit]
Description=DualLink
After=network-online.target

[Service]
ExecStart=/usr/bin/python -m duallink.headless
Restart=on-failure

[Install]
WantedBy=default.target
```
Turn it on with `systemctl --user enable --now duallink`.

### 5. Tests
Add `tests/test_linux.py`. It fakes the output of `ip -j addr`, so it runs on any OS.

## Things to check on Linux
- **Each link needs its own default route.** NetworkManager normally adds one for each connection (Ethernet
  with metric 100, Wi-Fi/USB with 600), so this should just work. If the hotspot has none, its connections fail
  with "network unreachable". Check with `ip route`.
- **Reverse-path filtering:** if replies on the hotspot get dropped, set `net.ipv4.conf.all.rp_filter=2`.
- **What isn't ported:** qBittorrent auto-setup through its Web UI should work as is, because it only talks
  HTTP. Autostart via the registry (`controller.py`), in-app updates and the installer are Windows-only and are
  simply skipped.

## Later
- A Linux window (GTK or Qt) talking to the same control API
- Packaging (AUR or AppImage)
