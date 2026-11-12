//! The apps DualLink knows and the set-up steps shown for each.

pub struct AppInfo {
    pub key: &'static str,
    pub name: &'static str,
    pub badge: &'static str,
    pub color: u32,
    pub desc: &'static str,
}

pub const APPS: [AppInfo; 6] = [
    AppInfo { key: "qbt", name: "qBittorrent", badge: "qB", color: 0x3c78d8, desc: "Torrents - set up automatically" },
    AppInfo { key: "fdm", name: "Free Download Manager", badge: "FDM", color: 0x2aa36b, desc: "Downloads from websites" },
    AppInfo { key: "jd", name: "JDownloader", badge: "JD", color: 0xd4972a, desc: "Downloads from file hosts" },
    AppInfo { key: "idm", name: "Internet Download Manager", badge: "IDM", color: 0x3a8fd6, desc: "Downloads from websites" },
    AppInfo { key: "firefox", name: "Firefox", badge: "FF", color: 0xe0601a, desc: "Browser downloads and browsing" },
    AppInfo { key: "other", name: "Other app", badge: "+", color: 0x6b7280, desc: "Any app with a SOCKS5 proxy setting" },
];

pub fn app(key: &str) -> &'static AppInfo {
    APPS.iter().find(|a| a.key == key).unwrap_or(&APPS[5])
}

/// The settings key that switches an app on.
pub fn setting_key(key: &str) -> &str {
    if key == "qbt" { "qbt_auto" } else { key }
}

/// One line of a step. `code` is something to type into the other app (shown in a box with a Copy button).
pub struct Line {
    pub text: String,
    pub code: Option<String>,
    pub tail: &'static str,
}

fn say(text: &str) -> Line {
    Line { text: text.to_string(), code: None, tail: "" }
}

fn code(label: &str, value: &str) -> Line {
    Line { text: label.to_string(), code: Some(value.to_string()), tail: "" }
}

#[derive(Default)]
pub struct StepText {
    pub title: String,
    pub note: &'static str,
    pub location: String,
    pub lines: Vec<Line>,
    /// A button under the step ("" = none).
    pub action: &'static str,
    /// The step with the "Test connection" button.
    pub test: bool,
}

pub struct Guide {
    pub steps: Vec<StepText>,
    pub tip: &'static str,
    pub foot: &'static str,
}

pub const AUTOSTART_ACTION: &str = "Start DualLink with Windows";

pub fn qbittorrent(web_ui_port: &str) -> Guide {
    Guide {
        steps: vec![
            StepText {
                title: "Turn on qBittorrent's Web UI".into(),
                note: "(one time)",
                location: "In qBittorrent: Tools › Options › Web UI".into(),
                lines: vec![
                    say("Tick \"Web User Interface (Remote control)\""),
                    Line { tail: "- just the address, no http://", ..code("IP address:", "127.0.0.1") },
                    code("Port:", web_ui_port),
                    say("Tick \"Bypass authentication for clients on localhost\""),
                    say("Click OK"),
                ],
                ..Default::default()
            },
            StepText { title: "Check that DualLink can reach it".into(), test: true, ..Default::default() },
            StepText {
                title: "That's it".into(),
                location: "When you turn DualLink on, it points qBittorrent at itself and reconnects running torrents. When you turn it off, your own settings are put back.".into(),
                ..Default::default()
            },
        ],
        tip: "",
        foot: "qBittorrent needs to be running.",
    }
}

/// Steps for an app that is set up by hand: point it at the proxy, allow several connections, keep DualLink running.
pub fn manual(key: &str, port: &str, autostart: bool) -> Guide {
    let name = app(key).name;
    let socks = |label: &str| [code(&format!("{label}:"), "127.0.0.1"), code("Port:", port)];
    let (location, lines, connections, tip): (&str, Vec<Line>, Option<(&str, &str)>, &str) = match key {
        "fdm" => (
            "In FDM: ☰ › Settings › Network, under Proxy",
            [say("Choose \"Configure manually\"")].into_iter().chain(socks("SOCKS5")).chain([say("Leave HTTP, HTTPS and FTP empty")]).collect(),
            Some(("In FDM: Settings › Traffic Limits", "Set \"Maximum number of connections per server\" to 8 or more for the mode you use (Low / Medium / High, shown at the bottom of FDM)")),
            "Some sites, like MediaFire, only accept one IP address per download link - those files can only use one connection.",
        ),
        "jd" => (
            "In JDownloader: Settings › Connection Manager",
            [say("Click Add and choose Socks5")].into_iter().chain(socks("Host")).chain([say("Untick \"No Proxy\" in the list, so every download goes through DualLink")]).collect(),
            Some(("In JDownloader: Settings › General", "Set \"Max. Chunks per Download\" to 8 or more")),
            "Menu names can differ slightly between JDownloader versions. Some file hosts only allow one connection per file.",
        ),
        "idm" => (
            "In IDM: Downloads › Options › Proxy / Socks",
            [say("Tick \"Use Socks\" and choose \"Socks 5\"")].into_iter().chain(socks("Address")).chain([say("Leave \"Use proxy\" (HTTP) unticked")]).collect(),
            Some(("In IDM: Downloads › Options › Connection", "Set \"Default max. conn. number\" to 8 or more")),
            "Menu names can differ slightly between IDM versions.",
        ),
        "firefox" => (
            "In Firefox: Settings › General › Network Settings › Settings…",
            [say("Choose \"Manual proxy configuration\"")]
                .into_iter()
                .chain(socks("SOCKS Host"))
                .chain([say("Choose \"SOCKS v5\" and tick \"Proxy DNS when using SOCKS v5\""), say("Leave HTTP Proxy empty")])
                .collect(),
            None,
            "Browsers usually download each file over one connection, so the speed-up comes from several downloads at once. All of Firefox's browsing goes through DualLink too.",
        ),
        _ => (
            "In your app's settings, look for the proxy options (often under Network or Connection)",
            [say("Choose SOCKS5 (sometimes called SOCKS v5)")]
                .into_iter()
                .chain(socks("Address"))
                .chain([say("If there's an option like \"remote DNS\" or \"Proxy DNS\", turn it on"), say("Leave HTTP and HTTPS proxy fields empty")])
                .collect(),
            Some(("If the app can download a file in parts", "Allow several connections per file (8 or more)")),
            "Apps without a SOCKS5 setting (most games and launchers) can't use DualLink.",
        ),
    };
    let shown = if key == "other" { "your app" } else { name };
    let mut steps = vec![StepText {
        title: format!("Point {shown} at DualLink"),
        note: "(one time)",
        location: location.into(),
        lines,
        ..Default::default()
    }];
    if let Some((place, what)) = connections {
        steps.push(StepText { title: "Allow several connections per file".into(), location: place.into(), lines: vec![say(what)], ..Default::default() });
    }
    steps.push(StepText {
        title: "Keep DualLink running".into(),
        location: "While DualLink is off, the app's downloads pass straight through. If DualLink is closed, the app can't download.".into(),
        action: if autostart { "" } else { AUTOSTART_ACTION },
        ..Default::default()
    });
    Guide { steps, tip, foot: "Use Traffic check during a download to confirm." }
}
