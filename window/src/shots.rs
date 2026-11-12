//! `--screenshots <folder>`: open every screen, save a picture of each, then close. With `--demo` the
//! pictures show made-up data; without it they show the running DualLink (read-only).

use crate::app::App;
use serde_json::json;
use slint::ComponentHandle;
use std::cell::RefCell;
use std::collections::VecDeque;
use std::path::Path;
use std::rc::Rc;
use std::time::{Duration, Instant};

type Action = Box<dyn Fn(&Rc<App>)>;

/// Wait, take a picture (if it has a name), then do the next thing.
struct Shot {
    wait_ms: u64,
    name: &'static str,
    then: Action,
}

struct Run {
    app: Rc<App>,
    queue: VecDeque<Shot>,
    log: String,
    started: Instant,
}

thread_local! {
    static RUN: RefCell<Option<Run>> = const { RefCell::new(None) };
}

fn save_png(app: &App, path: &Path) -> Result<String, String> {
    let shot = app.ui.window().take_snapshot().map_err(|e| e.to_string())?;
    let file = std::fs::File::create(path).map_err(|e| e.to_string())?;
    let mut encoder = png::Encoder::new(std::io::BufWriter::new(file), shot.width(), shot.height());
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    encoder.write_header().and_then(|mut w| w.write_image_data(shot.as_bytes())).map_err(|e| e.to_string())?;
    Ok(format!("{}x{}", shot.width(), shot.height()))
}

fn next() {
    let wait = RUN.with(|r| r.borrow().as_ref().and_then(|run| run.queue.front().map(|s| s.wait_ms)));
    let Some(wait) = wait else {
        if let Some(mut run) = RUN.with(|r| r.borrow_mut().take()) {
            run.log += "done\n";
            if let Some(dir) = &run.app.opts.shots {
                let _ = std::fs::write(dir.join("shots.log"), &run.log);
            }
        }
        let _ = slint::quit_event_loop();
        return;
    };
    slint::Timer::single_shot(Duration::from_millis(wait), || {
        let step = RUN.with(|r| {
            let mut run = r.borrow_mut();
            let run = run.as_mut()?;
            let shot = run.queue.pop_front()?;
            if !shot.name.is_empty() {
                let dir = run.app.opts.shots.clone().unwrap_or_default();
                let note = save_png(&run.app, &dir.join(format!("{}.png", shot.name))).unwrap_or_else(|e| format!("FAILED {e}"));
                run.log += &format!("{:6.1}s  {}: {note}\n", run.started.elapsed().as_secs_f64(), shot.name);
            }
            Some((run.app.clone(), shot.then))
        });
        if let Some((app, then)) = step {
            then(&app);
        }
        next();
    });
}

pub fn run(app: &Rc<App>) {
    let Some(dir) = &app.opts.shots else { return };
    let _ = std::fs::create_dir_all(dir);
    for old in std::fs::read_dir(dir).into_iter().flatten().flatten() {
        if old.path().extension().is_some_and(|e| e == "png") {
            let _ = std::fs::remove_file(old.path());
        }
    }
    let demo = app.opts.demo;
    let mut queue: VecDeque<Shot> = VecDeque::new();
    let mut add = |wait_ms: u64, name: &'static str, then: Action| queue.push_back(Shot { wait_ms, name, then });

    add(2500, "01-main", Box::new(|a| a.ui.set_settings_open(true)));
    add(800, "02-settings", Box::new(|a| a.ui.invoke_scroll_settings_to_end()));
    add(600, "03-settings-end", Box::new(|a| {
        a.ui.set_settings_open(false);
        a.open_guide("qbt");
    }));
    add(800, "04-qbittorrent-setup", Box::new(|a| a.ui.set_qbt_more(true)));
    add(300, "", Box::new(|a| a.ui.invoke_scroll_dialog_to_end())); // taller than the window: must scroll
    add(600, "04b-qbittorrent-different-port", Box::new(|a| a.open_guide("fdm")));
    add(800, "05-fdm-setup", Box::new(|a| a.open_more_apps()));
    add(800, "06-more-apps", Box::new(|a| a.open_speed_test()));
    if demo {
        // the demo can run a pretend test: show the result screen too
        add(800, "07-speed-test", Box::new(|a| a.dialog_primary()));
        add(1500, "07a-speed-test-running", Box::new(|_| {}));
        add(4500, "07b-speed-test-result", Box::new(|a| a.open_traffic_check()));
        add(500, "08a-traffic-check-measuring", Box::new(|_| {}));
        add(2600, "08-traffic-check", Box::new(|a| a.check_updates())); // the demo always has an update to show
        add(1200, "09-update", Box::new(|a| {
            a.close_dialog();
            a.toast("Copied 127.0.0.1", false);
        }));
    } else {
        add(800, "07-speed-test", Box::new(|a| a.open_traffic_check()));
        add(5500, "08-traffic-check", Box::new(|a| {
            a.close_dialog();
            a.toast("Copied 127.0.0.1", false);
        }));
    }
    add(600, "10-toast", Box::new(|a| a.ui.invoke_set_dark(!a.ui.invoke_is_dark())));
    add(1200, "11-main-other-theme", Box::new(|a| a.ui.set_settings_open(true)));
    add(800, "12-settings-other-theme", Box::new(|a| a.ui.set_settings_open(false)));
    if demo {
        add(300, "", Box::new(|a| a.ask_remove_app("fdm")));
        add(800, "13-remove-app", Box::new(|a| {
            a.close_dialog();
            // what a new install looks like: no apps, nothing connected
            a.save_now(json!({"added_apps": [], "qbt_auto": false, "fdm": false}), |a| a.power_clicked());
        }));
        add(2500, "14-no-apps", Box::new(|_| {}));
    }
    let log = format!("DualLinkWindow {} - {}\n", duallink_common::VERSION, if demo { "demo data" } else { "the running DualLink" });
    RUN.with(|r| *r.borrow_mut() = Some(Run { app: app.clone(), queue, log, started: Instant::now() }));
    next();
}
