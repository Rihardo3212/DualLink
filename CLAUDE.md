# DualLink - notes for Claude Code

DualLink is a Windows app that combines Ethernet and a phone hotspot for faster downloads. It runs a local
SOCKS5 proxy that spreads each app's connections over both links. Apps: qBittorrent (set up automatically through
its Web UI), FDM, JDownloader, IDM, Firefox, any SOCKS5 app. Repo: https://github.com/Rihardo3212/DualLink

## Rules from the owner (Rihards)
- Commits are authored as `Rihards <r1hardo@proton.me>`. Never add a `Claude-Session` line or session link.
  A `Co-Authored-By: Claude ...` line is fine.
- Keep token use low; don't do long live tests unless asked. Rihards tests on his PC himself.
- Plain, short explanations; the README is user-facing.

## How it's put together
Two processes:
- **Engine** (Python, `main.py` + `duallink/`): proxy, balancing, tray icon, qBittorrent setup, updates.
  Built with PyInstaller (onedir). Serves a local control API (HTTP on 127.0.0.1, random port, token header);
  port/token in `%APPDATA%\DualLink\engine.json`. Settings in `%APPDATA%\DualLink\config.json`.
  - `engine.py` - proxy core and balancing (`BalancingProxy.order()`, `_update_weights()`, idle recycling)
  - `controller.py` - settings (`DEFAULTS`), on/off, app status, speed test, updates, qBittorrent follow
  - `api.py` - control API (`Api.PAGE` = what the window may call), `service.py` - engine + opening the window
  - `tray.py` native tray icon, `instance.py` single instance, `updates.py` in-app updates, `qbt.py` qBittorrent
- **Window** (C# .NET 10, WinUI 3, code-only - no XAML, `native/DualLinkWindow/`): runs only while open, talks to
  the engine over the API. `--demo --screenshots shots/demo` saves PNGs of every screen with made-up data
  (`DemoEngine.cs`). In Native AOT, `AccentButtonStyle` lookups fail, so primary buttons are painted by hand
  (`Ui.Paint`). ContentDialog content doesn't scroll, so dialogs wrap content in a ScrollViewer.

## Build, test, release
- Tests: `python -m pytest tests` (some are Windows-only). Each version has its own `tests/test_vXYZ.py`.
- Releases come only from GitHub Actions (`.github/workflows/build.yml`): pushing a `v*` tag builds the engine,
  the window (AOT, falls back to non-AOT), screenshots, a memory report, and the Inno Setup installer
  (`installer/DualLink.iss`), then publishes the release. The installer is the only download.
- Version: `duallink/__init__.py` (`__version__`); CI passes it to the window and installer.
  `DualLinkWindow.csproj` `<Version>` only matters for local builds.
- Local AOT publish needs Visual Studio's C++ workload, which this PC doesn't have; Debug builds work in VS.
- Release steps: `git add -A`, commit, `git push`, `git tag vX.Y.Z`, `git push origin vX.Y.Z`.

## Current state (Oct 2026)
- Released: 1.13.0 (new install starts with no apps; apps can be removed with the ✕ button; switch = pause;
  DualLink turns back on after an in-app update if it was on).
- **1.13.1 pending**: `engine.py` change - only connection attempts younger than 1.5 s (`FRESH_PENDING`) count as a
  link's load, so unanswered torrent peer attempts piling up on the hotspot don't push working connections onto
  Ethernet. Check that the files are in place, then release.
- Open question: Rihards saw 103 Ethernet vs 7 hotspot connections in qBittorrent although the hotspot tested
  faster (98 vs 72 Mbps). If 1.13.1 doesn't fix it, next step is to count failed connection attempts per link
  (`Link.failures` exists but isn't reported) and show it, e.g. in the Traffic check. Other possible causes:
  failed hotspot attempts fall back to Ethernet; IPv6 peers can only use a link with IPv6.
- Untested: the tray icon's right-click menu.
- Leftover files from very old versions sit in the repo root (`__init__.py`, `controller.py`, `engine.py`, `ui/`);
  they're unused - ask before deleting.
