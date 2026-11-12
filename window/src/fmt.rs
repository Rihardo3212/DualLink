//! How numbers are shown.

use duallink_common::now_ts;

const SPEED_UNITS: [&str; 4] = ["B/s", "KiB/s", "MiB/s", "GiB/s"];

/// ("3.42", "MiB/s")
pub fn speed(mut b: f64) -> (String, &'static str) {
    let mut i = 0;
    while b >= 1024.0 && i < SPEED_UNITS.len() - 1 {
        b /= 1024.0;
        i += 1;
    }
    let num = if i == 0 {
        format!("{b:.0}")
    } else if b < 10.0 {
        format!("{b:.2}")
    } else {
        format!("{b:.1}")
    };
    (num, SPEED_UNITS[i])
}

pub fn speed_str(b: f64) -> String {
    let (n, u) = speed(b);
    format!("{n} {u}")
}

pub fn size(mut b: f64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut i = 0;
    while b >= 1024.0 && i < UNITS.len() - 1 {
        b /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{b:.0} B")
    } else {
        format!("{b:.1} {}", UNITS[i])
    }
}

/// Labels beside the graph: short, no trailing zeros.
pub fn axis(mut b: f64) -> String {
    if b <= 0.0 {
        return "0".into();
    }
    let mut i = 0;
    while b >= 1000.0 && i < SPEED_UNITS.len() - 1 {
        b /= 1024.0;
        i += 1;
    }
    let n = if b < 10.0 { (b * 10.0).round() / 10.0 } else { b.round() };
    format!("{n} {}", SPEED_UNITS[i])
}

pub fn plural(n: f64, word: &str) -> String {
    format!("{n} {word}{}", if n == 1.0 { "" } else { "s" })
}

/// "5 min ago" for a time in seconds since 1970.
pub fn ago(ts: f64) -> String {
    let m = ((now_ts() - ts) / 60.0).round().max(0.0);
    if m < 1.0 {
        "just now".into()
    } else if m < 60.0 {
        format!("{m} min ago")
    } else if m < 48.0 * 60.0 {
        format!("{} h ago", (m / 60.0).round())
    } else {
        format!("{} days ago", (m / 1440.0).round())
    }
}

pub fn mbps(bps: f64) -> String {
    format!("{:.0}", bps * 8.0 / 1e6)
}

/// A round number a bit above `v`, for the top of the graph.
pub fn nice_max(v: f64) -> f64 {
    if v <= 0.0 {
        return 1024.0 * 1024.0;
    }
    let p = 1024f64.powf((v.ln() / 1024f64.ln()).floor());
    for s in [1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1000.0] {
        if s * p >= v * 1.1 {
            return if s >= 1000.0 { 1024.0 * p } else { s * p };
        }
    }
    v * 1.2
}

/// GitHub's release notes as plain text: no markdown marks, no "by @someone in https://..." tails.
pub fn clean_notes(text: &str) -> String {
    let mut out = Vec::new();
    for line in text.lines() {
        let mut l = line.trim_end().to_string();
        let stripped = l.trim_start_matches('#');
        if stripped.len() != l.len() {
            l = stripped.trim_start().to_string();
        }
        l = l.replace("**", "");
        let body = l.trim_start();
        if let Some(rest) = body.strip_prefix("* ").or_else(|| body.strip_prefix("- ")) {
            l = format!("• {rest}");
        }
        if let Some(i) = l.find(" by @") {
            if let Some(j) = l[i..].find(" in https://") {
                let tail = &l[i + j + 4..];
                let end = tail.find(char::is_whitespace).map(|e| i + j + 4 + e).unwrap_or(l.len());
                l.replace_range(i..end, "");
            }
        }
        if l.starts_with("Full Changelog:") {
            continue;
        }
        out.push(l);
    }
    out.join("\n").trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn speeds_and_sizes() {
        assert_eq!(speed(0.0), ("0".into(), "B/s"));
        assert_eq!(speed_str(1536.0), "1.50 KiB/s");
        assert_eq!(speed_str(52.0 * 1024.0 * 1024.0), "52.0 MiB/s");
        assert_eq!(size(500.0), "500 B");
        assert_eq!(size(3.5 * 1024.0 * 1024.0 * 1024.0), "3.5 GiB");
        assert_eq!(axis(0.0), "0");
        assert_eq!(axis(2.5 * 1024.0 * 1024.0), "2.5 MiB/s");
        assert_eq!(axis(20.0 * 1024.0), "20 KiB/s");
        assert_eq!(mbps(12_500_000.0), "100");
        assert_eq!(plural(1.0, "connection"), "1 connection");
        assert_eq!(plural(8.0, "connection"), "8 connections");
    }

    #[test]
    fn graph_tops_are_round() {
        assert_eq!(nice_max(0.0), 1048576.0);
        assert_eq!(nice_max(1.7 * 1048576.0), 2.0 * 1048576.0);
        assert_eq!(nice_max(4.9 * 1048576.0), 10.0 * 1048576.0);
        assert_eq!(nice_max(900.0 * 1024.0), 1048576.0);
    }

    #[test]
    fn times() {
        assert_eq!(ago(now_ts()), "just now");
        assert_eq!(ago(now_ts() - 300.0), "5 min ago");
        assert_eq!(ago(now_ts() - 7200.0), "2 h ago");
        assert_eq!(ago(now_ts() - 5.0 * 86400.0), "5 days ago");
    }

    #[test]
    fn release_notes_become_plain_text() {
        let notes = "## What's Changed\n* Faster start by @someone in https://github.com/o/r/pull/3\n- **Fix** the tray\n\n**Full Changelog**: https://github.com/o/r/compare/a...b";
        assert_eq!(clean_notes(notes), "What's Changed\n• Faster start\n• Fix the tray");
        assert_eq!(clean_notes("**Full Changelog**: https://x"), "");
    }
}
