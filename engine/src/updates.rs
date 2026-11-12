//! Updates from the GitHub Releases page this build came from: check, download, verify and install.
//! DualLink updates by running the new installer silently; the installer starts the new version.

use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// "owner/name" of the GitHub repository, set by the GitHub build. Empty when built anywhere else:
/// no repository known, so no update checks.
pub const REPO: &str = match option_env!("DUALLINK_REPO") {
    Some(r) => r,
    None => "",
};

const ALLOWED_HOSTS: [&str; 5] = [
    "github.com", "api.github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
];

pub fn parse_version(text: &str) -> [u64; 3] {
    let mut out = [0u64; 3];
    let nums = text.split(|c: char| !c.is_ascii_digit()).filter(|s| !s.is_empty());
    for (slot, n) in out.iter_mut().zip(nums) {
        *slot = n.parse().unwrap_or(0);
    }
    out
}

pub fn is_newer(tag: &str, current: &str) -> bool {
    parse_version(tag) > parse_version(current)
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Asset {
    pub name: String,
    pub url: String,
    pub size: u64,
    /// "sha256:..." when GitHub provides it.
    pub digest: String,
}

/// A release that is newer than this copy.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Update {
    pub version: String,
    pub url: String,
    pub notes: String,
    pub installer: Option<Asset>,
}

pub fn fetch_latest(repo: &str) -> Result<Value, String> {
    let url = format!("https://api.github.com/repos/{repo}/releases/latest");
    let resp = crate::http::agent(Duration::from_secs(10))
        .get(&url)
        .set("Accept", "application/vnd.github+json")
        .call()
        .map_err(|e| e.to_string())?;
    serde_json::from_reader(resp.into_reader()).map_err(|e| e.to_string())
}

/// From GitHub's "latest release" answer: details of a newer release, or None.
pub fn newer_release(data: &Value, current: &str) -> Option<Update> {
    let s = |k: &str| data.get(k).and_then(Value::as_str).unwrap_or("");
    let flag = |k: &str| data.get(k).and_then(Value::as_bool).unwrap_or(false);
    let tag = s("tag_name");
    if flag("draft") || flag("prerelease") || !is_newer(tag, current) {
        return None;
    }
    let installer = data.get("assets").and_then(Value::as_array).into_iter().flatten().find_map(|a| {
        let name = a.get("name")?.as_str()?;
        let low = name.to_lowercase();
        (low.starts_with("duallink-setup") && low.ends_with(".exe")).then(|| Asset {
            name: name.to_string(),
            url: a.get("browser_download_url").and_then(Value::as_str).unwrap_or("").to_string(),
            size: a.get("size").and_then(Value::as_u64).unwrap_or(0),
            digest: a.get("digest").and_then(Value::as_str).unwrap_or("").to_string(),
        })
    });
    Some(Update {
        version: tag.trim_start_matches(['v', 'V']).to_string(),
        url: s("html_url").to_string(),
        notes: s("body").trim().chars().take(4000).collect(),
        installer,
    })
}

/// True when this copy was put there by the installer (its uninstaller sits next to DualLink.exe).
pub fn installed() -> bool {
    let Some(dir) = std::env::current_exe().ok().and_then(|p| p.parent().map(Path::to_path_buf)) else { return false };
    std::fs::read_dir(dir).is_ok_and(|entries| {
        entries.flatten().any(|e| {
            let name = e.file_name().to_string_lossy().to_lowercase();
            name.starts_with("unins") && name.ends_with(".exe")
        })
    })
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Download a release asset, check its size and SHA-256 (when GitHub provides it). Returns the file's path.
pub fn download(asset: &Asset, dest_dir: &Path, progress: &dyn Fn(u64, u64), allow_any_host: bool) -> Result<PathBuf, String> {
    let url = url::Url::parse(&asset.url).map_err(|_| "The download address isn't valid - update cancelled.".to_string())?;
    if !allow_any_host && (url.scheme() != "https" || !ALLOWED_HOSTS.contains(&url.host_str().unwrap_or(""))) {
        return Err("The download address doesn't belong to GitHub - update cancelled.".into());
    }
    let dir = dest_dir.join("DualLink-update");
    std::fs::create_dir_all(&dir).map_err(|e| format!("The download failed ({e})."))?;
    let name = url.path_segments().and_then(|mut s| s.next_back()).filter(|n| !n.is_empty()).unwrap_or("DualLink-Setup.exe");
    let fin = dir.join(name);
    let part = dir.join(format!("{name}.part"));
    let failed = |e: &dyn std::fmt::Display| format!("The download failed ({e}).");

    let resp = crate::http::agent(Duration::from_secs(600)).get(url.as_str()).call().map_err(|e| failed(&e))?;
    let mut total = asset.size;
    if total == 0 {
        total = resp.header("Content-Length").and_then(|v| v.parse().ok()).unwrap_or(0);
    }
    let mut reader = resp.into_reader();
    let mut file = std::fs::File::create(&part).map_err(|e| failed(&e))?;
    let mut sha = Sha256::new();
    let mut done = 0u64;
    let mut buf = vec![0u8; 256 * 1024];
    loop {
        let n = reader.read(&mut buf).map_err(|e| failed(&e))?;
        if n == 0 {
            break;
        }
        file.write_all(&buf[..n]).map_err(|e| failed(&e))?;
        sha.update(&buf[..n]);
        done += n as u64;
        progress(done, total);
    }
    drop(file);
    if total != 0 && done != total {
        return Err("The download was incomplete - please try again.".into());
    }
    if let Some(expected) = asset.digest.strip_prefix("sha256:") {
        if expected.to_lowercase() != hex(&sha.finalize()) {
            let _ = std::fs::remove_file(&part);
            return Err("The downloaded file didn't match GitHub's checksum - update cancelled.".into());
        }
    }
    let _ = std::fs::remove_file(&fin);
    std::fs::rename(&part, &fin).map_err(|e| failed(&e))?;
    Ok(fin)
}

/// Silent update; the installer starts the new DualLink when it's done (see installer/DualLink.iss).
pub fn run_installer(path: &Path) -> Result<(), String> {
    let mut cmd = std::process::Command::new(path);
    cmd.args(["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/update=1"]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0000_0008 | 0x0000_0200); // DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    }
    cmd.spawn().map(|_| ()).map_err(|e| format!("Couldn't start the installer ({e})."))
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use serde_json::json;

    pub fn release(tag: &str) -> Value {
        json!({"tag_name": tag, "html_url": format!("https://github.com/o/r/releases/tag/{tag}"), "body": " notes ",
               "assets": [{"name": "Source.zip", "browser_download_url": "https://github.com/o/r/s.zip", "size": 1},
                          {"name": format!("DualLink-Setup-{}.exe", tag.trim_start_matches('v')),
                           "browser_download_url": "https://github.com/o/r/releases/download/x/DualLink-Setup.exe",
                           "size": 5, "digest": "sha256:abc"}]})
    }

    #[test]
    fn versions() {
        assert_eq!(parse_version("v1.13.2"), [1, 13, 2]);
        assert_eq!(parse_version("2"), [2, 0, 0]);
        assert_eq!(parse_version(""), [0, 0, 0]);
        assert!(is_newer("v2.0.0", "1.13.2"));
        assert!(is_newer("v1.10.0", "1.9.9"));
        assert!(!is_newer("v2.0.0", "2.0.0"));
    }

    #[test]
    fn finds_a_newer_release_and_its_installer() {
        let u = newer_release(&release("v9.1.0"), "2.0.0").unwrap();
        assert_eq!(u.version, "9.1.0");
        assert_eq!(u.notes, "notes");
        assert_eq!(u.installer.unwrap().name, "DualLink-Setup-9.1.0.exe");
        assert!(newer_release(&release("v2.0.0"), "2.0.0").is_none());
        let mut pre = release("v9.0.0");
        pre["prerelease"] = json!(true);
        assert!(newer_release(&pre, "2.0.0").is_none());
    }

    fn serve(body: &'static [u8]) -> String {
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let url = format!("http://{}/files/DualLink-Setup-9.exe", server.server_addr());
        std::thread::spawn(move || {
            for rq in server.incoming_requests() {
                let _ = rq.respond(tiny_http::Response::from_data(body));
            }
        });
        url
    }

    #[test]
    fn downloads_and_verifies() {
        let dir = std::env::temp_dir().join(format!("duallink-test-{}", std::process::id()));
        let url = serve(b"hello");
        let sha = "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";
        let good = Asset { name: "x".into(), url: url.clone(), size: 5, digest: sha.into() };
        let seen = std::cell::Cell::new((0, 0));
        let path = download(&good, &dir, &|d, t| seen.set((d, t)), true).unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"hello");
        assert_eq!(path.file_name().unwrap(), "DualLink-Setup-9.exe");
        assert_eq!(seen.get(), (5, 5));

        let bad_sum = Asset { digest: "sha256:00".into(), ..good.clone() };
        assert!(download(&bad_sum, &dir, &|_, _| {}, true).unwrap_err().contains("checksum"));
        let short = Asset { size: 9, digest: String::new(), ..good.clone() };
        assert!(download(&short, &dir, &|_, _| {}, true).unwrap_err().contains("incomplete"));
        // only GitHub's own addresses are accepted
        assert!(download(&good, &dir, &|_, _| {}, false).unwrap_err().contains("GitHub"));
        let evil = Asset { url: "https://github.com.evil.example/x.exe".into(), ..good };
        assert!(download(&evil, &dir, &|_, _| {}, false).unwrap_err().contains("GitHub"));
        let _ = std::fs::remove_dir_all(dir);
    }
}
