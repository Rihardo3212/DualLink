using System.Diagnostics;
using System.Text.Json.Nodes;
using Microsoft.UI.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Documents;
using Microsoft.UI.Xaml.Media;
using Windows.ApplicationModel.DataTransfer;
using Windows.UI;

namespace DualLinkWindow;

/// <summary>Setup guides, More apps, Update, Speed test and Traffic check.</summary>
public sealed partial class MainView
{
    ContentDialog? dialog;
    string? modalKind;

    async void ShowDialog(ContentDialog d, string kind)
    {
        if (dialog != null)
        {
            var old = dialog;                                // one dialog at a time
            dialog = null;
            modalKind = null;
            old.Hide();
            await Task.Delay(220);
        }
        dialog = d;
        modalKind = kind;
        d.XamlRoot = root.XamlRoot;
        d.RequestedTheme = p.Dark ? ElementTheme.Dark : ElementTheme.Light;
        d.Resources["ContentDialogMaxWidth"] = 680.0;
        d.Closed += (_, _) =>
        {
            if (dialog != d) return;
            dialog = null;
            modalKind = null;
        };
        try
        {
            await d.ShowAsync();
        }
        catch (Exception)
        {
            if (dialog == d)
            {
                dialog = null;
                modalKind = null;
            }
        }
    }

    void CloseDialog() => dialog?.Hide();

    // The dialog's own content area doesn't scroll, so each dialog gets a scroll area of its own:
    // otherwise anything taller than the window (like the opened "different port" section) is cut off.
    ContentDialog NewDialog(string title, UIElement content, string close = "Done") => new()
    {
        Title = title, Content = Scrollable(content), CloseButtonText = close, DefaultButton = ContentDialogButton.None,
    };

    static ScrollViewer Scrollable(UIElement content) => new()
    {
        Content = content, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, VerticalScrollMode = ScrollMode.Auto,
        HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, HorizontalScrollMode = ScrollMode.Disabled,
        Padding = new Thickness(0, 0, 14, 0),                  // room for the scroll bar
    };

    /// <summary>Once an expanding section has opened, scroll so all of it shows.</summary>
    void ShowWhenOpened(FrameworkElement element)
    {
        var t = queue.CreateTimer();
        t.Interval = TimeSpan.FromMilliseconds(380);           // after the opening animation
        t.IsRepeating = false;
        t.Tick += (_, _) => element.StartBringIntoView(new BringIntoViewOptions { AnimationDesired = true });
        t.Start();
    }

    // ---- pieces of the guides ------------------------------------------------------------------- //
    sealed record Socks(string Label);

    sealed class StepMark
    {
        public required Border Circle;
        public required TextBlock Text;
        public required string Number;
    }

    (Grid Row, StepMark Mark) Step(int number, params UIElement[] content)
    {
        var text = Ui.T(number.ToString(), 12.5, p.Text, 700);
        text.HorizontalAlignment = HorizontalAlignment.Center;
        var circle = new Border
        {
            Width = 26, Height = 26, CornerRadius = new CornerRadius(13), Background = Ui.B(p.Inner), BorderBrush = Ui.B(p.Line),
            BorderThickness = new Thickness(1), Child = text, VerticalAlignment = VerticalAlignment.Top,
        };
        var g = Ui.Columns(Ui.Px(26), Ui.Star());
        g.ColumnSpacing = 14;
        g.Margin = new Thickness(0, 4, 0, 14);
        g.Children.Add(circle.At(0));
        g.Children.Add(Ui.V(2, content).At(1));
        return (g, new StepMark { Circle = circle, Text = text, Number = number.ToString() });
    }

    void SetDone(StepMark m, bool done)
    {
        m.Text.Text = done ? "✓" : m.Number;
        m.Text.Foreground = Ui.B(done ? p.On : p.Text);
        m.Circle.BorderBrush = Ui.B(done ? Palette.Mix(p.Line, p.On, 0.4) : p.Line);
    }

    TextBlock StepTitle(string title, string? note = null)
    {
        var tb = new TextBlock { FontSize = 14, Foreground = Ui.B(p.Text), TextWrapping = TextWrapping.WrapWholeWords };
        tb.Inlines.Add(new Run { Text = title, FontWeight = Ui.W(600) });
        if (note != null) tb.Inlines.Add(new Run { Text = " " + note, Foreground = Ui.B(p.Faint) });
        return tb;
    }

    TextBlock Where(string marked)
    {
        var tb = Ui.Rich(marked, 12.5, p.Muted, p.Text);
        tb.Margin = new Thickness(0, 1, 0, 4);
        return tb;
    }

    StackPanel Bullets(IEnumerable<object> items)
    {
        var list = Ui.V(3);
        foreach (var item in items)
        {
            UIElement content = item switch
            {
                Socks s => SocksLine(s.Label),
                UIElement e => e,
                _ => Ui.Rich(item.ToString() ?? "", 12.5, p.Muted, p.Text),
            };
            var row = Ui.Columns(Ui.Px(10), Ui.Star());
            row.ColumnSpacing = 6;
            var dot = Ui.T("•", 12.5, p.Muted);
            dot.VerticalAlignment = VerticalAlignment.Top;
            row.Children.Add(dot.At(0));
            row.Children.Add(((FrameworkElement)content).At(1));
            list.Children.Add(row);
        }
        return list;
    }

    Border Code(string text)
    {
        var t = Ui.T(text, 12, p.Text);
        t.FontFamily = new FontFamily("Cascadia Mono, Consolas");
        t.IsTextSelectionEnabled = true;
        var b = Ui.Box(t, p.Inner, p.Line, 6, new Thickness(6, 1, 6, 2));
        b.VerticalAlignment = VerticalAlignment.Center;
        return b;
    }

    Button CopyBtn(string text)
    {
        var b = Ui.Btn("Copy", () => _ = CopyText(text), small: true);
        b.FontSize = 11.5;
        b.Padding = new Thickness(8, 1, 8, 2);
        return b;
    }

    WrapPanel SocksLine(string label)
    {
        var port = settings.Num("port", 1080).ToString(System.Globalization.CultureInfo.InvariantCulture);
        var w = new WrapPanel { Spacing = 6 };
        w.Children.Add(Ui.T(label + ":", 12.5, p.Muted));
        w.Children.Add(Code("127.0.0.1"));
        w.Children.Add(CopyBtn("127.0.0.1"));
        w.Children.Add(Ui.T("port", 12.5, p.Muted));
        w.Children.Add(Code(port));
        w.Children.Add(CopyBtn(port));
        return w;
    }

    TextBlock Tip(string text)
    {
        var t = Ui.T(text, 12, p.Faint, wrap: true);
        t.Margin = new Thickness(0, 2, 0, 0);
        return t;
    }

    async Task CopyText(string text)
    {
        bool ok;
        try
        {
            var package = new DataPackage();
            package.SetText(text);
            Clipboard.SetContent(package);
            Clipboard.Flush();
            ok = true;
        }
        catch (Exception)
        {
            try
            {
                ok = (await engine.CallAsync("copy", text)).AsBool();
            }
            catch (EngineError)
            {
                ok = false;
            }
        }
        Toast(ok ? $"Copied {text}" : $"Couldn't copy - type {text}", !ok);
    }

    // ---- qBittorrent -------------------------------------------------------------------------- //
    StepMark[] qbtSteps = [];
    Expander? qbtAdvanced;
    Button? qbtTest;
    TextBlock? qbtResult;

    string QbtPort()
    {
        if (Uri.TryCreate(settings.Str("qbt_url"), UriKind.Absolute, out var u) && !u.IsDefaultPort) return u.Port.ToString();
        return "8080";
    }

    void OpenQbtSetup()
    {
        var ipLine = new WrapPanel { Spacing = 6 };
        ipLine.Children.Add(Ui.Rich("IP address:", 12.5, p.Muted, p.Text));
        ipLine.Children.Add(Code("127.0.0.1"));
        ipLine.Children.Add(CopyBtn("127.0.0.1"));
        ipLine.Children.Add(Ui.T("- just the address, no http://", 12.5, p.Faint));
        var portLine = new WrapPanel { Spacing = 6 };
        portLine.Children.Add(Ui.T("Port:", 12.5, p.Muted));
        portLine.Children.Add(Code(QbtPort()));
        var (s1, m1) = Step(1, StepTitle("Turn on qBittorrent's Web UI", "(one time)"), Where("In qBittorrent: <b>Tools › Options › Web UI</b>"),
            Bullets(["Tick <b>Web User Interface (Remote control)</b>", ipLine, portLine,
                     "Tick <b>Bypass authentication for clients on localhost</b>", "Click <b>OK</b>"]));
        qbtTest = Ui.Btn("Test connection", () => _ = TestQbt(), primary: true, small: true);
        qbtResult = Ui.T("", 12.5, p.Muted, wrap: true);
        var testRow = Ui.Columns(Ui.Auto, Ui.Star());          // the result wraps instead of being cut off
        testRow.ColumnSpacing = 10;
        testRow.Children.Add(qbtTest.At(0));
        testRow.Children.Add(qbtResult.At(1));
        testRow.Margin = new Thickness(0, 6, 0, 0);
        var (s2, m2) = Step(2, StepTitle("Check that DualLink can reach it"), testRow);
        var (s3, m3) = Step(3, StepTitle("That's it"),
            Where("When you turn DualLink on, it points qBittorrent at itself and reconnects running torrents. When you turn it off, your own settings are put back."));
        qbtSteps = [m1, m2, m3];

        var url = new TextBox { Text = settings.Str("qbt_url"), IsSpellCheckEnabled = false };
        url.TextChanged += (_, _) => Save(Patch("qbt_url", url.Text.Trim()));
        var user = new TextBox { Text = settings.Str("qbt_user"), IsSpellCheckEnabled = false };
        user.TextChanged += (_, _) => Save(Patch("qbt_user", user.Text.Trim()));
        var pass = new PasswordBox { Password = settings.Str("qbt_pass") };
        pass.PasswordChanged += (_, _) => Save(Patch("qbt_pass", pass.Password.Trim()));
        var reconnect = Ui.Switch("Reconnect running torrents when switching");
        reconnect.IsOn = settings.Bool("reconnect");
        reconnect.Toggled += (_, _) => Save(Patch("reconnect", reconnect.IsOn));
        var two = Ui.Columns(Ui.Star(), Ui.Star());
        two.Children.Add(Field("Username", user).At(0));
        two.Children.Add(Field("Password", pass).At(1));
        var advanced = new Expander
        {
            Header = "Using a different port, or a Web UI password?", HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Stretch, Margin = new Thickness(0, 4, 0, 0),
            Content = Ui.V(0, Field("Web UI address", url), two,
                Tip("Only needed if you don't tick \"Bypass authentication for clients on localhost\"."),
                ToggleRow("Reconnect running torrents when switching", "So existing peers move over immediately", reconnect)),
        };
        var foot = Ui.T("qBittorrent needs to be running.", 12, p.Faint);
        foot.Margin = new Thickness(0, 10, 0, 0);
        qbtAdvanced = advanced;
        advanced.Expanding += (_, _) => ShowWhenOpened(advanced);
        ShowDialog(NewDialog("Set up qBittorrent", Ui.V(0, s1, s2, s3, advanced, foot)), "qbt");
        RefreshQbtSteps();
    }

    async Task TestQbt()
    {
        if (qbtTest == null || qbtResult == null) return;
        qbtTest.IsEnabled = false;
        qbtResult.Text = "Checking…";
        qbtResult.Foreground = Ui.B(p.Muted);
        try
        {
            await SaveNow(null);
            var r = await engine.CallAsync("test_qbt");
            if (modalKind != "qbt") return;
            bool ok = r.Bool("ok");
            qbtResult.Text = ok ? "✓ " + r.Str("message") : r.Str("message");
            qbtResult.Foreground = Ui.B(ok ? p.On : p.Bad);
            if (qbtSteps.Length == 3)
            {
                SetDone(qbtSteps[0], ok);
                SetDone(qbtSteps[1], ok);
            }
            if (ok && running)
            {
                var q = await engine.CallAsync("retry_qbt");
                if (q.Str("state") == "connected") Toast("qBittorrent is now using DualLink");
            }
        }
        catch (EngineError e)
        {
            qbtResult.Text = e.Message;
            qbtResult.Foreground = Ui.B(p.Bad);
        }
        finally
        {
            qbtTest.IsEnabled = true;
        }
    }

    void RefreshQbtSteps()
    {
        bool ok = last != null && last.Bool("running") && last.Obj("qbt").Str("state") == "connected";
        if (ok)
            foreach (var m in qbtSteps) SetDone(m, true);
    }

    // ---- other apps ----------------------------------------------------------------------------- //
    sealed record Guide(string Where, object[] Items, string[]? Conn, string Tip);

    static Guide GuideFor(string key) => key switch
    {
        "fdm" => new("In FDM: <b>☰ › Settings › Network</b>, under Proxy",
            ["Choose <b>Configure manually</b>", new Socks("SOCKS5"), "Leave HTTP, HTTPS and FTP empty"],
            ["In FDM: <b>Settings › Traffic Limits</b>",
             "Set <b>Maximum number of connections per server</b> to 8 or more for the mode you use (Low / Medium / High, shown at the bottom of FDM)"],
            "Some sites, like MediaFire, only accept one IP address per download link - those files can only use one connection."),
        "jd" => new("In JDownloader: <b>Settings › Connection Manager</b>",
            ["Click <b>Add</b> and choose <b>Socks5</b>", new Socks("Host"), "Untick <b>No Proxy</b> in the list, so every download goes through DualLink"],
            ["In JDownloader: <b>Settings › General</b>", "Set <b>Max. Chunks per Download</b> to 8 or more"],
            "Menu names can differ slightly between JDownloader versions. Some file hosts only allow one connection per file."),
        "idm" => new("In IDM: <b>Downloads › Options › Proxy / Socks</b>",
            ["Tick <b>Use Socks</b> and choose <b>Socks 5</b>", new Socks("Address"), "Leave <b>Use proxy</b> (HTTP) unticked"],
            ["In IDM: <b>Downloads › Options › Connection</b>", "Set <b>Default max. conn. number</b> to 8 or more"],
            "Menu names can differ slightly between IDM versions."),
        "firefox" => new("In Firefox: <b>Settings › General › Network Settings › Settings…</b>",
            ["Choose <b>Manual proxy configuration</b>", new Socks("SOCKS Host"),
             "Choose <b>SOCKS v5</b> and tick <b>Proxy DNS when using SOCKS v5</b>", "Leave HTTP Proxy empty"],
            null,
            "Browsers usually download each file over one connection, so the speed-up comes from several downloads at once. All of Firefox's browsing goes through DualLink too."),
        _ => new("In your app's settings, look for the proxy options (often under Network or Connection)",
            ["Choose <b>SOCKS5</b> (sometimes called SOCKS v5)", new Socks("Address"),
             "If there's an option like <b>remote DNS</b> or <b>Proxy DNS</b>, turn it on", "Leave HTTP and HTTPS proxy fields empty"],
            ["If the app can download a file in parts", "Allow several connections per file (8 or more)"],
            "Apps without a SOCKS5 setting (most games and launchers) can't use DualLink."),
    };

    void OpenGuide(string key)
    {
        if (key == "qbt")
        {
            OpenQbtSetup();
            return;
        }
        var app = Apps.First(a => a.Key == key);
        var g = GuideFor(key);
        var steps = Ui.V(0);
        int n = 1;
        steps.Children.Add(Step(n++, StepTitle($"Point {app.Name} at DualLink", "(one time)"), Where(g.Where), Bullets(g.Items)).Row);
        if (g.Conn != null)
            steps.Children.Add(Step(n++, StepTitle("Allow several connections per file"), Where(g.Conn[0]), Bullets([g.Conn[1]])).Row);
        var keep = new List<UIElement>
        {
            StepTitle("Keep DualLink running"),
            Where("While DualLink is off, the app's downloads pass straight through. If DualLink is closed, the app can't download."),
        };
        if (!settings.Bool("autostart"))
        {
            Button? auto = null;
            auto = Ui.Btn("Start DualLink with Windows", async () =>
            {
                await SaveNow(Patch("autostart", true));
                filling = true;
                sAutostart.IsOn = true;
                filling = false;
                if (auto != null) auto.Visibility = Visibility.Collapsed;
                Toast("DualLink will start with Windows");
            }, small: true);
            auto.HorizontalAlignment = HorizontalAlignment.Left;
            auto.Margin = new Thickness(0, 4, 0, 0);
            keep.Add(auto);
        }
        steps.Children.Add(Step(n, keep.ToArray()).Row);
        steps.Children.Add(Tip(g.Tip));
        var foot = Ui.Rich("Use <b>Traffic check</b> during a download to confirm.", 12, p.Faint, p.Muted);
        foot.Margin = new Thickness(0, 12, 0, 0);
        steps.Children.Add(foot);
        ShowDialog(NewDialog($"Set up {app.Name}", steps), key);
    }

    void OpenMoreApps()
    {
        var list = Ui.V(0);
        bool first = true;
        foreach (var a in Apps)
        {
            if (!first) list.Children.Add(Ui.Divider(p.Line));
            first = false;
            bool added = Added(a.Key);
            var key = a.Key;
            var btn = Ui.Btn(added ? "Set up" : "Add", async () =>
            {
                if (!Added(key))
                {
                    var patch = Patch(SettingKey(key), true);
                    patch["added_apps"] = AppListWith(key, true);
                    await SaveNow(patch);
                    BuildApps();
                }
                OpenGuide(key);
            }, primary: !added, small: true);
            var row = Ui.Columns(Ui.Auto, Ui.Star(), Ui.Auto);
            row.Padding = new Thickness(0, 10, 0, 10);
            row.Children.Add(Ui.Badge(a.Badge, Palette.Hex(a.Color)).At(0));
            row.Children.Add(Ui.V(0, Ui.T(a.Name, 13.5, p.Text, 600), Ui.T(a.Desc, 12, p.Faint)).At(1));
            row.Children.Add(btn.At(2));
            list.Children.Add(row);
        }
        var foot = Ui.T("Any app with a SOCKS5 proxy setting works.", 12, p.Faint);
        foot.Margin = new Thickness(0, 10, 0, 0);
        list.Children.Add(foot);
        ShowDialog(NewDialog("Apps that can use DualLink", list), "more");
    }

    void AskRemoveApp(AppInfo a)
    {
        bool other = a.Key == "other";
        string name = other ? "the other app" : a.Name;
        var body = Ui.V(10);
        if (a.Key == "qbt")
            body.Children.Add(Ui.T("DualLink will stop setting up qBittorrent. If DualLink is on, qBittorrent gets its own proxy settings back right away.", 13, p.Text, wrap: true));
        else
            body.Children.Add(Ui.Rich($"If you set up {name} to use DualLink, set its proxy back to <b>none</b> in {name} too - otherwise its downloads stop working while DualLink is off.", 13, p.Text, p.Text));
        body.Children.Add(Tip("You can add it again any time with + Add an app."));
        var d = NewDialog(other ? "Remove the other app?" : $"Remove {a.Name}?", body, "Cancel");
        d.PrimaryButtonText = "Remove";
        d.DefaultButton = ContentDialogButton.Close;       // removing isn't the default
        d.PrimaryButtonClick += (_, _) =>
        {
            RemoveApp(a.Key);
            Toast(other ? "Other app removed" : $"{a.Name} removed");
        };
        ShowDialog(d, "remove");
    }

    // ---- update ------------------------------------------------------------------------------- //
    ProgressBar? upBar;
    TextBlock? upText;
    StackPanel? upProgress;

    void OpenUpdate()
    {
        if (last?.Get("update") is not JsonObject u) return;
        var body = Ui.V(8);
        body.Children.Add(Ui.Rich($"Version <b>{u.Str("version")}</b> is available - you have {last.Str("version")}.", 14, p.Text, p.Text));
        var notes = Fmt.CleanNotes(u.Str("notes"));     // can be empty: GitHub's notes are often only a changelog link
        if (notes != "")
        {
            var text = Ui.T(notes, 12.5, p.Muted, wrap: true);
            text.IsTextSelectionEnabled = true;
            var box = Ui.Box(new ScrollViewer { Content = text, MaxHeight = 170 }, p.Inner, p.Line, 10, new Thickness(12, 10, 12, 10));
            body.Children.Add(box);
        }
        body.Children.Add(Tip("DualLink downloads the update, switches itself off (qBittorrent gets its own settings back), installs it and starts again. It takes about a minute."));
        upBar = new ProgressBar { Maximum = 100, Value = 0 };
        upText = Ui.T("", 12.5, p.Muted, wrap: true);
        upProgress = Ui.V(6, upBar, upText);
        upProgress.Visibility = Visibility.Collapsed;
        upProgress.Margin = new Thickness(0, 6, 0, 0);
        body.Children.Add(upProgress);
        var d = NewDialog("Update DualLink", body, "Later");
        d.PrimaryButtonText = "Update now";
        d.SecondaryButtonText = "Open download page";
        d.DefaultButton = ContentDialogButton.Primary;
        d.PrimaryButtonClick += async (_, e) =>
        {
            e.Cancel = true;                                 // stay open and show the progress
            d.IsPrimaryButtonEnabled = false;
            try
            {
                var r = await engine.CallAsync("start_update");
                if (!r.Bool("ok"))
                {
                    d.IsPrimaryButtonEnabled = true;
                    upProgress.Visibility = Visibility.Visible;
                    upText.Text = r.Str("message");
                    upText.Foreground = Ui.B(p.Bad);
                }
            }
            catch (EngineError ex)
            {
                d.IsPrimaryButtonEnabled = true;
                upText.Text = ex.Message;
                upText.Foreground = Ui.B(p.Bad);
                upProgress.Visibility = Visibility.Visible;
            }
        };
        d.SecondaryButtonClick += (_, e) =>
        {
            e.Cancel = true;
            OpenUrl(u.Str("url"));
        };
        ShowDialog(d, "update");
        RenderUpdate(last);
    }

    static void OpenUrl(string url)
    {
        if (!url.StartsWith("https://github.com/")) return;   // only ever our own release pages
        try
        {
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (Exception)
        {
        }
    }

    void RenderUpdate(JsonObject s)
    {
        if (dialog == null || modalKind != "update" || upBar == null || upText == null || upProgress == null) return;
        var st = s.Obj("update_state");
        var phase = st.Str("phase", "idle");
        if (phase == "idle") return;
        upProgress.Visibility = Visibility.Visible;
        bool busy = phase == "downloading" || phase == "installing";
        dialog.IsPrimaryButtonEnabled = !busy;
        dialog.IsSecondaryButtonEnabled = !busy;
        double done = st.Num("done"), total = st.Num("total");
        if (phase == "downloading")
        {
            upBar.Value = total > 0 ? Math.Min(100, 100 * done / total) : 0;
            upText.Text = total > 0 ? $"Downloading… {Fmt.Size(done)} of {Fmt.Size(total)}" : $"Downloading… {Fmt.Size(done)}";
            upText.Foreground = Ui.B(p.Muted);
        }
        else if (phase == "installing")
        {
            upBar.Value = 100;
            upText.Text = "Installing - DualLink will close and start again in a moment.";
            upText.Foreground = Ui.B(p.Muted);
        }
        else if (phase == "error")
        {
            upText.Text = st.Str("message");
            upText.Foreground = Ui.B(p.Bad);
            dialog.PrimaryButtonText = "Try again";
        }
    }

    // ---- speed test ----------------------------------------------------------------------------- //
    StackPanel? speedBody;
    TextBlock? speedFoot;
    string speedSig = "";

    void OpenSpeedTest()
    {
        speedSig = "";
        speedBody = Ui.V(0);
        speedFoot = Ui.T("", 12, p.Faint);
        speedFoot.Margin = new Thickness(0, 10, 0, 0);
        var d = NewDialog("Speed test", Ui.V(0, speedBody, speedFoot), "Close");
        d.PrimaryButtonText = "Start test";
        d.DefaultButton = ContentDialogButton.Primary;
        d.PrimaryButtonClick += async (_, e) =>
        {
            e.Cancel = true;
            try
            {
                var r = await engine.CallAsync("start_speed_test");
                if (!r.Bool("ok")) Toast(r.Str("message"), true);
                else RenderSpeed(new JsonObject { ["running"] = true, ["phase"] = "" });
            }
            catch (EngineError ex)
            {
                Toast(ex.Message, true);
            }
        };
        ShowDialog(d, "speed");
        RenderSpeed(last?.Obj("speedtest") ?? new JsonObject());
    }

    void RenderSpeed(JsonObject st)
    {
        if (speedBody == null || speedFoot == null) return;
        var sig = st.ToJsonString();
        if (sig == speedSig) return;
        speedSig = sig;
        speedBody.Children.Clear();
        var d = dialog;                                        // (its button stays enabled: pressing it during a
                                                               // test does nothing, and re-enabling loses the colour)
        if (st.Bool("running"))
        {
            var ring = new ProgressRing { IsActive = true, Width = 18, Height = 18 };
            speedBody.Children.Add(Ui.H(10, ring, Ui.T($"Testing {st.Str("phase")}… about 5 seconds per connection.", 13, p.Muted)));
            speedFoot.Text = "";
            return;
        }
        var intro = Ui.T("Measures how fast each connection can download on its own, one after the other. For accurate results, " +
                         "pause big downloads first. The hotspot part uses a few dozen MB of mobile data.", 12.5, p.Muted, wrap: true);
        intro.Margin = new Thickness(0, 2, 0, 10);
        speedBody.Children.Add(intro);
        if (st.Get("result") is not JsonObject res) return;
        var list = res.Arr("links").OfType<JsonObject>().ToList();
        if (list.Count == 0)
        {
            if (res.Get("eth") is JsonObject e1) { e1["label"] = "Ethernet"; list.Add(e1); }
            if (res.Get("hot") is JsonObject h1) { h1["label"] = "Hotspot"; list.Add(h1); }
        }
        double max = Math.Max(1, list.Select(r => r.Num("bps")).DefaultIfEmpty(0).Max());
        var colors = p.LinkColors;
        for (int i = 0; i < list.Count; i++)
        {
            var r = list[i];
            double bps = r.Num("bps");
            var err = r.Str("error");
            if (i > 0) speedBody.Children.Add(Ui.Divider(p.Line));
            var row = Ui.Columns(Ui.Px(110), Ui.Star(), Ui.Auto);
            row.Padding = new Thickness(0, 10, 0, 10);
            row.Children.Add(Ui.T(r.Str("label"), 14, p.Text, 600).At(0));
            row.Children.Add(Bar(bps / max, colors[Math.Min(i, colors.Length - 1)]).At(1));
            UIElement value;
            if (err != "")
            {
                var et = Ui.T(err, 12, p.Bad, wrap: true);
                et.MaxWidth = 220;
                value = et;
            }
            else
            {
                var big = Ui.T($"{Fmt.Mbps(bps)} Mbps", 14, p.Text, 650);
                big.HorizontalAlignment = HorizontalAlignment.Right;
                var small = Ui.T(Fmt.SpeedStr(bps), 11.5, p.Muted);
                small.HorizontalAlignment = HorizontalAlignment.Right;
                value = Ui.V(0, big, small);
            }
            row.Children.Add(((FrameworkElement)value).At(2));
            speedBody.Children.Add(row);
        }
        double total = list.Sum(r => r.Num("bps"));
        var together = Ui.V(2,
            Ui.Rich($"Together: up to <b>{Fmt.Mbps(total)} Mbps</b> ({Fmt.SpeedStr(total)})", 13, p.Muted, p.Text),
            Ui.T("DualLink uses these results to favour the faster connection from the start.", 12.5, p.Muted, wrap: true));
        var box = Ui.Box(together, p.Inner, p.Line, 12, new Thickness(14, 12, 14, 12));
        box.Margin = new Thickness(0, 10, 0, 0);
        speedBody.Children.Add(box);
        var when = DateTimeOffset.FromUnixTimeSeconds((long)res.Num("ts")).ToLocalTime();
        speedFoot.Text = $"Tested {when:yyyy-MM-dd HH:mm}";
        if (d != null && modalKind == "speed") d.PrimaryButtonText = "Test again";
    }

    Grid Bar(double fraction, Color color)
    {
        fraction = Math.Clamp(fraction, 0, 1);
        var track = new Grid
        {
            Height = 10, CornerRadius = new CornerRadius(5), Background = Ui.B(p.Inner), BorderBrush = Ui.B(p.Line),
            BorderThickness = new Thickness(1), VerticalAlignment = VerticalAlignment.Center,
        };
        track.ColumnDefinitions.Add(new ColumnDefinition { Width = Ui.Star(Math.Max(fraction, 0.0001)) });
        track.ColumnDefinitions.Add(new ColumnDefinition { Width = Ui.Star(Math.Max(1 - fraction, 0.0001)) });
        track.Children.Add(new Border { Background = Ui.B(color), CornerRadius = new CornerRadius(5) }.At(0));
        return track;
    }

    // ---- traffic check ---------------------------------------------------------------------------- //
    StackPanel? checkBody;
    TextBlock? checkFoot;

    void OpenTrafficCheck()
    {
        checkBody = Ui.V(0);
        checkFoot = Ui.T("", 12, p.Faint);
        checkFoot.Margin = new Thickness(0, 10, 0, 0);
        var d = NewDialog("Traffic check", Ui.V(0, checkBody, checkFoot), "Close");
        d.PrimaryButtonText = "Check again";
        d.PrimaryButtonClick += (sender, e) =>
        {
            e.Cancel = true;
            _ = RunCheck(d);
        };
        ShowDialog(d, "check");
        _ = RunCheck(d);
    }

    async Task RunCheck(ContentDialog d)
    {
        if (checkBody == null || checkFoot == null) return;
        var body = checkBody;
        d.IsPrimaryButtonEnabled = false;
        checkFoot.Text = "";
        body.Children.Clear();
        body.Children.Add(Ui.H(10, new ProgressRing { IsActive = true, Width = 18, Height = 18 },
            Ui.T("Measuring for 3 seconds - keep your download running…", 13, p.Muted)));
        JsonNode? r;
        try
        {
            r = await engine.CallAsync("traffic_check");
        }
        catch (EngineError e)
        {
            r = new JsonObject { ["ok"] = false, ["message"] = e.Message };
        }
        finally
        {
            d.IsPrimaryButtonEnabled = true;
        }
        if (modalKind != "check" || body != checkBody) return;
        body.Children.Clear();
        if (!r.Bool("ok"))
        {
            body.Children.Add(Note("info", r.Str("message")));
            return;
        }
        foreach (var n in r.Arr("notes")) body.Children.Add(Note(n.Str("kind"), n.Str("text")));

        body.Children.Add(CheckHeading("Programs using DualLink"));
        var through = r.Arr("through");
        if (through.Count == 0) body.Children.Add(Ui.T("None right now.", 13, p.Faint));
        else
        {
            var chips = new WrapPanel { Spacing = 6 };
            foreach (var a in through)
            {
                var chip = Ui.Box(Ui.T($"{a.Str("name")} · {Fmt.Plural(a.Num("connections"), "connection")}", 12.5, p.On),
                    p.Inner, Palette.Mix(p.Line, p.On, 0.35), 13, new Thickness(10, 3, 10, 4));
                chips.Children.Add(chip);
            }
            body.Children.Add(chips);
        }

        body.Children.Add(CheckHeading("Download speed"));
        var table = new Grid { ColumnSpacing = 8 };
        foreach (var w in new[] { 1.5, 1.0, 1.0, 1.0 }) table.ColumnDefinitions.Add(new ColumnDefinition { Width = Ui.Star(w) });
        string[] heads = ["Connection", "Windows sees", "Through DualLink", "Around DualLink"];
        table.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        for (int c = 0; c < 4; c++)
        {
            var h = Ui.T(heads[c], 12.5, p.Faint, 500);
            h.HorizontalAlignment = c == 0 ? HorizontalAlignment.Left : HorizontalAlignment.Right;
            h.Margin = new Thickness(0, 0, 0, 4);
            table.Children.Add(h.At(c, 0));
        }
        int row = 1;
        foreach (var l in r.Arr("links"))
        {
            table.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var line = Ui.Divider(p.Line);
            Grid.SetColumnSpan(line, 4);
            line.VerticalAlignment = VerticalAlignment.Top;
            table.Children.Add(line.At(0, row));
            double? windows = l.Get("windows") is JsonValue ? l.Num("windows") : null;
            double? other = l.Get("other") is JsonValue ? l.Num("other") : null;
            bool bad = other != null && other > Math.Max(256 * 1024, 0.15 * (windows ?? 0));
            var name = Ui.V(2, Ui.T(l.Str("label"), 12.5, p.Text, 600));
            var apps = l.Arr("direct_apps");
            if (bad && apps.Count > 0)
                name.Children.Add(Ui.T("Connecting directly: " + string.Join(", ", apps.Take(6).Select(a => $"{a.Str("name")} ({a.Num("connections")})")),
                    11.5, p.Faint, wrap: true));
            name.Margin = new Thickness(0, 7, 0, 7);
            table.Children.Add(name.At(0, row));
            string[] values = [windows == null ? "-" : Fmt.SpeedStr(windows.Value), Fmt.SpeedStr(l.Num("dualink")), other == null ? "-" : Fmt.SpeedStr(other.Value)];
            for (int c = 0; c < 3; c++)
            {
                var v = Ui.T(values[c], 12.5, c == 2 && bad ? p.Warn : p.Text, (ushort)(c == 2 && bad ? 600 : 400));
                v.HorizontalAlignment = HorizontalAlignment.Right;
                v.VerticalAlignment = VerticalAlignment.Top;
                v.Margin = new Thickness(0, 7, 0, 7);
                table.Children.Add(v.At(c + 1, row));
            }
            row++;
        }
        body.Children.Add(table);
        checkFoot.Text = $"Measured over {r.Num("seconds")} s with Windows' own counters.";
    }

    TextBlock CheckHeading(string text)
    {
        var t = Ui.SectionTitle(text, p.Muted);
        t.Margin = new Thickness(0, 14, 0, 6);
        return t;
    }

    Border Note(string kind, string text)
    {
        var (icon, color) = kind switch
        {
            "warn" => ("!", p.Warn),
            "good" => ("✓", p.On),
            _ => ("i", p.Eth),
        };
        var g = Ui.Columns(Ui.Auto, Ui.Star());
        g.ColumnSpacing = 10;
        var i = Ui.T(icon, 13, color, 700);
        i.VerticalAlignment = VerticalAlignment.Top;
        g.Children.Add(i.At(0));
        g.Children.Add(Ui.T(text, 13, p.Text, wrap: true).At(1));
        var stroke = kind is "warn" or "good" ? Palette.Mix(p.Line, color, 0.35) : p.Line;
        var b = Ui.Box(g, p.Inner, stroke, 10, new Thickness(12, 10, 12, 10));
        b.Margin = new Thickness(0, 4, 0, 4);
        return b;
    }
}
