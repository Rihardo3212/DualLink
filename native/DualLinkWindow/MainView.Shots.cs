using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;

namespace DualLinkWindow;

/// <summary>
/// --screenshots: opens every screen in turn and saves a PNG of each (used by GitHub Actions and for checking
/// changes without clicking through by hand). With the real engine it only reads; nothing is switched on or off.
/// </summary>
public sealed partial class MainView
{
    async Task RunScreenshots(string dir)
    {
        var log = new StringBuilder();
        var clock = Stopwatch.StartNew();

        async Task Shot(string name, int waitMs = 800)
        {
            await Task.Delay(waitMs);
            try
            {
                var note = Native.SaveWindowPng(hwnd, Path.Combine(dir, name + ".png"));
                log.AppendLine($"{clock.Elapsed.TotalSeconds,6:0.0}s  {name}: {note}");
            }
            catch (Exception e)
            {
                log.AppendLine($"{clock.Elapsed.TotalSeconds,6:0.0}s  {name}: FAILED {e.Message}");
            }
        }

        void Memory(string when)
        {
            using var me = Process.GetCurrentProcess();
            me.Refresh();
            log.AppendLine($"memory {when}: working set {me.WorkingSet64 / 1048576.0:0.0} MB, private {me.PrivateMemorySize64 / 1048576.0:0.0} MB");
        }

        try
        {
            Directory.CreateDirectory(dir);
            foreach (var old in Directory.GetFiles(dir, "*.png")) File.Delete(old);
            log.AppendLine($"DualLinkWindow {typeof(MainView).Assembly.GetName().Version} - {(opt.Demo ? "demo data" : "the running DualLink")}");
            BringToFront();
            await Shot("01-main", 2500);
            Memory("after start");
            OpenSettings(true);
            await Shot("02-settings");
            settingsScroll.ChangeView(null, 100000, null);
            await Shot("03-settings-end");
            OpenSettings(false);
            await Task.Delay(400);
            OpenQbtSetup();
            await Shot("04-qbittorrent-setup", 1000);
            if (qbtAdvanced != null) qbtAdvanced.IsExpanded = true;      // taller than the window: must scroll
            await Shot("04b-qbittorrent-different-port", 1400);
            OpenGuide("fdm");
            await Shot("05-fdm-setup", 1000);
            OpenMoreApps();
            await Shot("06-more-apps", 1000);
            OpenSpeedTest();
            await Shot("07-speed-test", 1000);
            if (opt.Demo)
            {
                // the demo can run a pretend test: show the result screen too
                await (engine.CallAsync("start_speed_test"));
                await Task.Delay(5500);
                if (await engine.CallAsync("status") is JsonObject s) Render(s);
                await Shot("07b-speed-test-result", 600);
            }
            OpenTrafficCheck();
            await Shot("08-traffic-check", opt.Demo ? 2600 : 5500);
            if (last?.Get("update") is JsonObject)
            {
                OpenUpdate();
                await Shot("09-update", 1000);
            }
            CloseDialog();
            await Task.Delay(500);
            Toast("Copied 127.0.0.1");
            await Shot("10-toast", 500);
            Memory("after every screen");
            theme = p.Dark ? ElementTheme.Light : ElementTheme.Dark;
            Rebuild();
            await Shot("11-main-other-theme", 1500);
            OpenSettings(true);
            await Shot("12-settings-other-theme");
            OpenSettings(false);
            log.AppendLine("done");
        }
        catch (Exception e)
        {
            log.AppendLine("error: " + e);
        }
        try
        {
            File.WriteAllText(Path.Combine(dir, "shots.log"), log.ToString());
        }
        catch (Exception)
        {
        }
        window.Close();
    }
}
