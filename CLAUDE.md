# DualLink - notes for Claude Code

DualLink is a Windows app that combines Ethernet and a phone hotspot for faster downloads. It runs a local
SOCKS5 proxy that spreads each app's connections over both links. Apps: qBittorrent (set up automatically through
its Web UI), FDM, JDownloader, IDM, Firefox, any SOCKS5 app. Repo: https://github.com/Rihardo3212/DualLink

## Rules from the owner (Rihards)
- Commits are authored as `Rihards <92665337+Rihardo3212@users.noreply.github.com>` (GitHub's private address; never a real email). Never add a `Claude-Session` line or session link.
  A `Co-Authored-By: Claude ...` line is fine.
- Keep token use low; don't do long live tests unless asked. Rihards tests on his PC himself.
- Plain, short explanations; the README is user-facing.

## How it's put together
All Rust (rewritten from Python + C#/WinUI 3 in 2.0.0, Nov 2026). A Cargo workspace with two programs:
- **Engine** (`engine/`, builds `DualLink.exe`): proxy, balancing, tray icon, qBittorrent setup, updates.
  Serves a local control API (HTTP on 127.0.0.1, random port, token header);
  port/token in `%APPDATA%\DualLink\engine.json`. Settings in `%APPDATA%\DualLink\config.json`.
  - `proxy.rs` - proxy core and balancing (`Proxy::order()`, `update_weights()`, idle recycling), on tokio
  - `controller.rs` - settings (`Cfg`), on/off, app status, speed test, updates, qBittorrent follow, traffic check
  - `service.rs` - engine + opening the window + `request()` = what the window may call; `api.rs` - the HTTP server
  - `tray.rs` native tray icon, `net.rs` Windows network calls, `updates.rs` in-app updates, `qbt.rs` qBittorrent
- **Window** (`window/`, builds `DualLinkWindow.exe`, Slint with the software renderer): runs only while open,
  talks to the engine over the API. `ui/main.slint` only draws; `src/app.rs` decides everything.
  `--demo --screenshots shots` saves PNGs of every screen with made-up data (`demo.rs`).
  Slint notes: a layout's `preferred-height` counts wrapped text as one line, so scroll areas read their height
  from a marker at the end (`page-end`, `settings-end`, `dialog-end`). Switches (`Toggle`) only show state;
  Rust changes it. The "Made with Slint" badge in Settings is required by Slint's free license - keep it.
- `common/` - shared: single-instance mutexes/events, clipboard, open URL, config folder.
- Windows API code is behind `cfg(windows)` (`windows-sys`), with empty stand-ins elsewhere, so everything
  builds and tests on Linux too.

## Build, test, release
- Tests: `cargo test --workspace` (next to the code; `engine/src/controller_tests.rs` is the big one).
- Check Windows code from Linux: `cargo check --workspace --target x86_64-pc-windows-msvc`.
- Look at the window on Linux: `xvfb-run -a target/debug/DualLinkWindow --demo --screenshots <dir>`.
- Releases come only from GitHub Actions (`.github/workflows/build.yml`): pushing a `v*` tag runs the tests,
  builds both programs, screenshots, a memory report, and the Inno Setup installer
  (`installer/DualLink.iss`), then publishes the release. The installer is the only download.
- Version: `Cargo.toml` (`[workspace.package] version`). CI fails a tag that doesn't match it.
- Release steps: `git add -A`, commit, `git push`, `git tag vX.Y.Z`, `git push origin vX.Y.Z`.

## Current state (Nov 2026)
- Released: 1.13.2 (the last Python/C# version).
- 2.0.0 (Rust rewrite) is written but NOT yet run on real Windows: it compiles for Windows, tests pass on Linux,
  and the window was checked from Linux screenshots. Rihards needs to test it on his PC before tagging:
  tray icon and its right-click menu, notifications, adapter detection, qBittorrent setup, speed test,
  traffic check, Start with Windows, updating from 1.13.2 with the in-app updater, uninstall.
- The control API is the same shape as 1.x except `copy`/`open_url` are gone (the window does them itself)
  and settings no longer have `eth_ip`/`hot_ip`. `config.json` from 1.x is read as it is.
- Open question (from 1.13): Rihards saw 103 Ethernet vs 7 hotspot connections in qBittorrent although the
  hotspot tested faster. The Traffic check now shows connections and failed connection attempts per link, to
  help find out why. Other possible causes: failed hotspot attempts fall back to Ethernet; IPv6 peers can
  only use a link with IPv6.
- Keep the repo root tidy.
