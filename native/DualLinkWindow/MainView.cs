using System.Diagnostics;
using System.Text.Json.Nodes;
using Microsoft.UI;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Graphics;

namespace DualLinkWindow;

public sealed record AppInfo(string Key, string Name, string Badge, string Color, string Desc);

/// <summary>DualLink's window: speed, connections and apps, with Settings on the right and dialogs on top.</summary>
public sealed partial class MainView
{
    static readonly AppInfo[] Apps =
    [
        new("qbt", "qBittorrent", "qB", "#3c78d8", "Torrents - set up automatically"),
        new("fdm", "Free Download Manager", "FDM", "#2aa36b", "Downloads from websites"),
        new("jd", "JDownloader", "JD", "#d4972a", "Downloads from file hosts"),
        new("idm", "Internet Download Manager", "IDM", "#3a8fd6", "Downloads from websites"),
        new("firefox", "Firefox", "FF", "#e0601a", "Browser downloads and browsing"),
        new("other", "Other app", "+", "#6b7280", "Any app with a SOCKS5 proxy setting"),
    ];

    readonly IEngine engine;
    readonly LaunchOptions opt;
    readonly Window window;
    readonly DispatcherQueue queue;
    readonly IntPtr hwnd;
    readonly AppWindow appWindow;
    readonly bool mica;
    readonly DispatcherQueueTimer pollTimer, saveTimer, toastTimer;
    ElementTheme theme;
    Palette p = Palette.DarkMode;
    Grid root = new();

    // state from the engine
    JsonObject settings = new();
    JsonObject? last;
    JsonArray adapterList = new();
    bool running, ready, polling, savePending, shownUpdated, closing, engineGone, quiet;
    int changes;                                   // counts local setting changes, to spot out-of-date statuses

    // parts of the window that change
    Border powerHalo = null!;
    Button power = null!;
    TextBlock stateText = null!, speedNum = null!, speedUnit = null!;
    Chart chart = null!;
    Grid tiles = null!;
    string tileSig = "";
    int tileCols;
    readonly List<TileView> tileViews = [];
    StackPanel appRows = null!;
    string appSig = "";
    readonly List<AppRow> appViews = [];
    Button updateBtn = null!;
    SplitView split = null!;
    Border toast = null!;
    TextBlock toastText = null!;

    sealed class TileView
    {
        public required Border Root;
        public required TextBlock Name, Sub, Speed;
        public ProgressBar? Bar;
    }

    sealed class AppRow
    {
        public required string Key;
        public required TextBlock Status;
        public required Button Setup, Fix;
        public required ToggleSwitch Switch;
    }

    public MainView(IEngine engine, LaunchOptions opt)
    {
        this.engine = engine;
        this.opt = opt;
        theme = opt.Theme;
        window = new Window { Title = "DualLink" };
        queue = window.DispatcherQueue;
        hwnd = WinRT.Interop.WindowNative.GetWindowHandle(window);
        appWindow = window.AppWindow;
        window.ExtendsContentIntoTitleBar = true;
        mica = Microsoft.UI.Composition.SystemBackdrops.MicaController.IsSupported();
        if (mica) window.SystemBackdrop = new MicaBackdrop();
        try
        {
            appWindow.SetIcon(System.IO.Path.Combine(AppContext.BaseDirectory, "icon.ico"));
        }
        catch (Exception)
        {
            // no icon file: keep the default
        }
        SizeWindow(920, 720);

        pollTimer = queue.CreateTimer();
        pollTimer.Interval = TimeSpan.FromSeconds(1);
        pollTimer.IsRepeating = true;
        pollTimer.Tick += (_, _) => Poll();
        saveTimer = queue.CreateTimer();
        saveTimer.Interval = TimeSpan.FromMilliseconds(300);
        saveTimer.IsRepeating = false;
        saveTimer.Tick += (_, _) => _ = SaveNow(null, fromTimer: true);
        toastTimer = queue.CreateTimer();
        toastTimer.IsRepeating = false;
        toastTimer.Tick += (_, _) => toast.Opacity = 0;

        Build(null);
        window.Closed += OnClosed;
        if (!opt.Demo && opt.ScreenshotsDir == null)
            Native.Listen(Native.WindowFront, () => queue.TryEnqueue(BringToFront));
        if (engine.Pid is int pid) WatchEngine(pid);
        _ = InitAsync();
    }

    public void Activate() => window.Activate();

    void SizeWindow(int width, int height)
    {
        double scale = Math.Max(1.0, Native.GetDpiForWindow(hwnd) / 96.0);
        var area = DisplayArea.GetFromWindowId(appWindow.Id, DisplayAreaFallback.Nearest).WorkArea;
        int w = Math.Min((int)(width * scale), area.Width), h = Math.Min((int)(height * scale), area.Height);
        if (appWindow.Presenter is OverlappedPresenter op)
        {
            op.PreferredMinimumWidth = Math.Min((int)(720 * scale), area.Width);
            op.PreferredMinimumHeight = Math.Min((int)(560 * scale), area.Height);
        }
        appWindow.Resize(new SizeInt32(w, h));
        appWindow.Move(new PointInt32(area.X + (area.Width - w) / 2, area.Y + (area.Height - h) / 2));
    }

    void BringToFront()
    {
        if (appWindow.Presenter is OverlappedPresenter op && op.State == OverlappedPresenterState.Minimized) op.Restore();
        appWindow.Show();
        window.Activate();
        Native.SetForegroundWindow(hwnd);
    }

    void WatchEngine(int pid)
    {
        try
        {
            var proc = Process.GetProcessById(pid);
            proc.EnableRaisingEvents = true;
            proc.Exited += (_, _) => queue.TryEnqueue(() =>
            {
                engineGone = true;                         // DualLink was quit from the tray: close with it
                window.Close();
            });
        }
        catch (Exception)
        {
            // already gone, or not ours to watch
        }
    }

    void OnClosed(object sender, WindowEventArgs e)
    {
        closing = true;
        pollTimer.Stop();
        if (!engineGone && !opt.Demo && opt.ScreenshotsDir == null)
        {
            try
            {
                // lets the engine show its "still running in the tray" hint (or quit, if that's the setting)
                Task.Run(() => engine.CallAsync("window_closed")).Wait(3000);
            }
            catch (Exception)
            {
            }
        }
        Application.Current.Exit();
    }

    // ---- building the window ------------------------------------------------------------------- //
    void Build(bool? dark)
    {
        bool isDark = theme switch
        {
            ElementTheme.Dark => true,
            ElementTheme.Light => false,
            _ => dark ?? Application.Current.RequestedTheme == ApplicationTheme.Dark,
        };
        p = isDark ? Palette.DarkMode : Palette.LightMode;
        Ui.Dark = isDark;
        root = new Grid
        {
            RequestedTheme = theme,
            Background = mica ? new SolidColorBrush(Colors.Transparent) : Ui.B(p.Bg),
        };
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(34) });
        root.RowDefinitions.Add(new RowDefinition { Height = Ui.Star() });

        var titleBar = new Grid { Background = new SolidColorBrush(Colors.Transparent) };
        var titleText = Ui.H(8, new Image { Source = Logo(), Width = 16, Height = 16 }, Ui.T("DualLink", 12, p.Muted));
        titleText.Margin = new Thickness(14, 0, 0, 0);
        titleText.VerticalAlignment = VerticalAlignment.Center;
        titleBar.Children.Add(titleText);
        root.Children.Add(titleBar);

        var page = Ui.V(14, Header(), MainCard(), AppsCard());
        page.Padding = new Thickness(22, 4, 22, 22);
        page.MaxWidth = 1180;
        var scroll = new ScrollViewer
        {
            Content = page, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollMode = ScrollMode.Disabled,
        };
        split = new SplitView
        {
            Content = scroll, Pane = SettingsPane(), PanePlacement = SplitViewPanePlacement.Right,
            DisplayMode = SplitViewDisplayMode.Overlay, OpenPaneLength = 420, IsPaneOpen = false,
            PaneBackground = Ui.B(p.Dark ? Palette.Hex("#1f2228") : Palette.Hex("#fbfbfd")),
            LightDismissOverlayMode = LightDismissOverlayMode.On,
        };
        split.At(0, 1);
        root.Children.Add(split);

        toastText = Ui.T("", 13, p.Text, wrap: true);
        toast = Ui.Box(toastText, p.Toast, p.Line, 11, new Thickness(16, 10, 16, 10));
        toast.HorizontalAlignment = HorizontalAlignment.Center;
        toast.VerticalAlignment = VerticalAlignment.Bottom;
        toast.Margin = new Thickness(0, 0, 0, 22);
        toast.MaxWidth = 640;
        toast.Opacity = 0;
        toast.IsHitTestVisible = false;
        toast.OpacityTransition = new ScalarTransition();
        toast.At(0, 1);
        root.Children.Add(toast);

        root.ActualThemeChanged += OnThemeChanged;
        window.Content = root;
        window.SetTitleBar(titleBar);
        var bar = appWindow.TitleBar;
        bar.ButtonBackgroundColor = Colors.Transparent;
        bar.ButtonInactiveBackgroundColor = Colors.Transparent;
        bar.ButtonForegroundColor = p.Text;
        bar.ButtonInactiveForegroundColor = p.Faint;
        bar.ButtonHoverForegroundColor = p.Text;
        bar.ButtonHoverBackgroundColor = Palette.WithAlpha(p.Text, 0x18);
        bar.ButtonPressedBackgroundColor = Palette.WithAlpha(p.Text, 0x28);
        bar.ButtonPressedForegroundColor = p.Text;
        tileSig = "";
        appSig = "";
    }

    static BitmapImage Logo() => new(new Uri(System.IO.Path.Combine(AppContext.BaseDirectory, "logo.png")));

    void OnThemeChanged(FrameworkElement sender, object args)
    {
        if (theme != ElementTheme.Default) return;
        bool dark = sender.ActualTheme == ElementTheme.Dark;
        if (dark != p.Dark) Rebuild(dark);
    }

    void Rebuild(bool? dark = null)
    {
        bool settingsOpen = split.IsPaneOpen;
        Build(dark);
        if (ready)
        {
            FillAdapters();
            FillSettings();
            BuildApps();
        }
        if (last != null) Render(last, false);
        split.IsPaneOpen = settingsOpen;
    }

    UIElement Header()
    {
        var g = Ui.Columns(Ui.Auto, Ui.Star(), Ui.Auto);
        g.Children.Add(new Image { Source = Logo(), Width = 34, Height = 34, VerticalAlignment = VerticalAlignment.Center }.At(0));
        var title = Ui.V(0, Ui.T("DualLink", 17, p.Text, 650), Ui.T("Ethernet + phone hotspot for faster downloads", 12.5, p.Muted));
        title.VerticalAlignment = VerticalAlignment.Center;
        g.Children.Add(title.At(1));

        updateBtn = Ui.Btn("", OpenUpdate);
        updateBtn.Background = Ui.B(Palette.WithAlpha(p.On, 0x29));
        updateBtn.BorderBrush = Ui.B(Palette.WithAlpha(p.On, 0x73));
        updateBtn.Foreground = Ui.B(p.On);
        updateBtn.Visibility = Visibility.Collapsed;
        ToolTipService.SetToolTip(updateBtn, "A newer version of DualLink is available");
        var speedTest = Ui.Btn("Speed test", OpenSpeedTest);
        ToolTipService.SetToolTip(speedTest, "Measure how fast each connection is");
        var check = Ui.Btn("Traffic check", OpenTrafficCheck);
        ToolTipService.SetToolTip(check, "See which programs use DualLink and where your traffic goes");
        var gear = Ui.IconBtn("", "Settings", () => OpenSettings(true));
        g.Children.Add(Ui.H(8, updateBtn, speedTest, check, gear).At(2));
        return g;
    }

    UIElement MainCard()
    {
        power = new Button
        {
            Width = 84, Height = 84, CornerRadius = new CornerRadius(42), Padding = new Thickness(0), BorderThickness = new Thickness(2),
            Content = new FontIcon { Glyph = "", FontSize = 30 },
        };
        power.Resources["ButtonBackgroundPointerOver"] = Ui.B(Palette.WithAlpha(p.Text, 0x10));
        power.Resources["ButtonBackgroundPressed"] = Ui.B(Palette.WithAlpha(p.Text, 0x18));
        power.Resources["ButtonBorderBrushPointerOver"] = Ui.B(p.Faint);
        power.Resources["ButtonBorderBrushPressed"] = Ui.B(p.Faint);
        power.Resources["ButtonForegroundPointerOver"] = Ui.B(p.Muted);
        power.Resources["ButtonForegroundPressed"] = Ui.B(p.Muted);
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(power, "Turn DualLink on or off");
        power.Click += (_, _) => _ = PowerClicked();
        powerHalo = new Border { CornerRadius = new CornerRadius(48), Padding = new Thickness(6), Child = power, VerticalAlignment = VerticalAlignment.Center };
        SetPowerLook(running);

        stateText = Ui.Rich("<b>Off</b>", 13, p.Muted, p.Text);
        speedNum = Ui.T("0", 42, p.Text, 650);
        speedUnit = Ui.T("B/s", 17, p.Muted, 500);
        speedUnit.VerticalAlignment = VerticalAlignment.Bottom;
        speedUnit.Margin = new Thickness(0, 0, 0, 8);
        var info = Ui.V(0, stateText, Ui.H(6, speedNum, speedUnit));
        info.VerticalAlignment = VerticalAlignment.Center;
        var top = Ui.Columns(Ui.Auto, Ui.Star());
        top.ColumnSpacing = 14;
        top.Children.Add(powerHalo.At(0));
        top.Children.Add(info.At(1));
        top.Margin = new Thickness(-6, -6, 0, 0);

        chart = new Chart(p);
        tiles = new Grid { ColumnSpacing = 12, RowSpacing = 12, Margin = new Thickness(0, 12, 0, 0) };
        tiles.SizeChanged += (_, _) => LayoutTiles(false);
        return Ui.Box(Ui.V(0, top, chart.Root, tiles), p.Card, p.CardStroke, 16, new Thickness(22, 20, 22, 18));
    }

    void SetPowerLook(bool on)
    {
        power.Background = Ui.B(on ? Palette.WithAlpha(p.On, 0x1c) : p.Inner);
        power.BorderBrush = Ui.B(on ? p.On : p.Line);
        power.Foreground = Ui.B(on ? p.On : p.Faint);
        powerHalo.Background = Ui.B(on ? Palette.WithAlpha(p.On, 0x1a) : Colors.Transparent);
    }

    UIElement AppsCard()
    {
        appRows = new StackPanel();
        var title = Ui.SectionTitle("Apps", p.Muted);
        title.Margin = new Thickness(0, 12, 0, 4);
        var more = Ui.Link("+ More apps", OpenMoreApps);
        more.Margin = new Thickness(0, 8, 0, 10);
        return Ui.Box(Ui.V(0, title, appRows, Ui.Divider(p.Line), more), p.Card, p.CardStroke, 16, new Thickness(18, 0, 18, 0));
    }

    // ---- talking to the engine ---------------------------------------------------------------- //
    async Task InitAsync()
    {
        while (!closing)
        {
            try
            {
                var st = await engine.CallAsync("get_init");
                settings = st.Obj("settings");
                adapterList = st.Arr("adapters");
                ready = true;
                FillAdapters();
                FillSettings();
                BuildApps();
                if (settings.Str("eth_name") == "" || settings.Str("hot_name") == "") OpenSettings(true);
                await PollOnce();
                pollTimer.Start();
                if (opt.ScreenshotsDir != null) _ = RunScreenshots(opt.ScreenshotsDir);
                return;
            }
            catch (EngineError e)
            {
                Ui.SetRich(stateText, $"<b>Can't reach DualLink</b> · {e.Message}", p.Text);
                await Task.Delay(2000);
            }
        }
    }

    void Poll()
    {
        if (polling || closing) return;
        if (appWindow.Presenter is OverlappedPresenter op && op.State == OverlappedPresenterState.Minimized) return;
        _ = PollOnce();
    }

    async Task PollOnce()
    {
        polling = true;
        try
        {
            int asked = changes;
            if (await engine.CallAsync("status") is JsonObject s && !s.Bool("hidden")) Render(s, asked == changes);
        }
        catch (EngineError e)
        {
            Ui.SetRich(stateText, $"<b>Can't reach DualLink</b> · {e.Message}", p.Text);
        }
        finally
        {
            polling = false;
        }
    }

    static JsonObject Patch(string key, JsonNode? value) => new() { [key] = value };

    void Save(JsonObject patch)
    {
        changes++;
        foreach (var (k, v) in patch) settings[k] = v?.DeepClone();
        UpdateBalancingRows();
        savePending = true;
        saveTimer.Stop();
        saveTimer.Start();
    }

    async Task SaveNow(JsonObject? patch, bool fromTimer = false)
    {
        changes++;
        if (patch != null)
            foreach (var (k, v) in patch) settings[k] = v?.DeepClone();
        saveTimer.Stop();
        try
        {
            var res = await engine.CallAsync("save_settings", settings.DeepClone());
            savePending = false;
            settings = res.Obj("settings");
            changes++;                                         // statuses asked for before this are out of date
            var error = res.Str("error");
            if (error != "")
            {
                Toast(error, true);
                if (fromTimer) FillAdapters();
            }
        }
        catch (EngineError e)
        {
            savePending = false;
            Toast(e.Message, true);
        }
    }

    async Task PowerClicked()
    {
        power.IsEnabled = false;
        powerHalo.Opacity = 0.6;
        try
        {
            await SaveNow(null);
            var r = await engine.CallAsync("power", !running);
            var msg = r.Str("message");
            if (!r.Bool("ok"))
            {
                Toast(msg, true);
                if (msg.Contains("Settings")) OpenSettings(true);
            }
            else if (msg != "") Toast(msg);
            int asked = changes;
            if (await engine.CallAsync("status") is JsonObject s) Render(s, asked == changes);
        }
        catch (EngineError e)
        {
            Toast(e.Message, true);
        }
        finally
        {
            power.IsEnabled = true;
            powerHalo.Opacity = 1;
        }
    }

    // ---- showing the status ------------------------------------------------------------------ //
    /// <param name="current">False when the settings changed while the status was on its way: its app
    /// switches may show the old state, so they're left alone this time.</param>
    void Render(JsonObject s, bool current = true)
    {
        last = s;
        running = s.Bool("running");
        SetPowerLook(running);
        var (num, unit) = Fmt.Speed(s.Num("combined"));
        speedNum.Text = num;
        speedUnit.Text = unit;
        var links = s.Arr("links").OfType<JsonObject>().ToList();
        string Label(JsonObject l) => l.Str("kind") == "hot" ? "hotspot" : l.Str("kind") == "eth" ? "Ethernet" : l.Str("name");
        var up = links.Where(l => l.Bool("present")).ToList();
        var down = links.Where(l => !l.Bool("present")).ToList();
        string state;
        if (!running) state = "<b>Off</b> · press the button to combine your connections";
        else if (down.Count == 0)
            state = links.Count == 2 ? "<b>On</b> · using Ethernet and hotspot together" : $"<b>On</b> · using {links.Count} connections together";
        else
        {
            var using_ = up.Count > 0 ? string.Join(" + ", up.Select(Label)) : "nothing";
            state = $"<b>On</b> · using {using_} - waiting for {string.Join(" and ", down.Select(Label))}";
        }
        Ui.SetRich(stateText, state, p.Text);
        RenderTiles(links);
        chart.Update(links.Select(l => l.Arr("history").Select(x => x.AsNum()).ToArray()).ToList());
        RenderApps(s, current);
        restartNote.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
        switch (modalKind)
        {
            case "qbt": RefreshQbtSteps(); break;
            case "speed": RenderSpeed(s.Obj("speedtest")); break;
            case "update": RenderUpdate(s); break;
        }
        if (s.Bool("just_updated") && !shownUpdated)
        {
            shownUpdated = true;
            Toast($"DualLink is updated to {s.Str("version")}");
        }
        var update = s.Get("update") as JsonObject;
        updateBtn.Visibility = update != null ? Visibility.Visible : Visibility.Collapsed;
        if (update != null) updateBtn.Content = $"Update to {update.Str("version")}";
        verText.Text = $"DualLink {s.Str("version")}";
    }

    void RenderTiles(List<JsonObject> links)
    {
        var sig = string.Join("|", links.Select(l => l.Str("name")));
        if (sig != tileSig)
        {
            tileSig = sig;
            BuildTiles(links);
        }
        for (int i = 0; i < links.Count && i < tileViews.Count; i++)
        {
            var l = links[i];
            var tv = tileViews[i];
            bool waiting = running && !l.Bool("present");
            tv.Root.Opacity = waiting ? 0.6 : 1;
            tv.Speed.Text = running ? Fmt.SpeedStr(l.Num("speed")) : "-";
            string sub;
            if (l.Str("adapter") == "") sub = "Not chosen - pick it in Settings";
            else if (waiting) sub = "Not connected · joins automatically";
            else if (!running) sub = l.Str("adapter");
            else sub = Fmt.Plural(l.Num("connections"), "connection") + (l.Bool("over_quota") ? " · data limit reached" : "");
            if (tv.Bar != null)
            {
                double used = l.Num("down") + l.Num("up"), quota = l.Num("quota");
                bool show = running && quota > 0;
                tv.Bar.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
                if (show)
                {
                    tv.Bar.Value = Math.Min(100, 100 * used / quota);
                    sub += $" · {Fmt.Size(used)} of {Fmt.Size(quota)}";
                }
            }
            tv.Sub.Text = sub;
        }
    }

    void BuildTiles(List<JsonObject> links)
    {
        tileViews.Clear();
        tiles.Children.Clear();
        var colors = p.LinkColors;
        for (int i = 0; i < links.Count; i++)
        {
            var l = links[i];
            bool hot = l.Str("kind") == "hot";
            var name = Ui.T(hot ? "Phone hotspot" : l.Str("name"), 13.5, p.Text, 600);
            var sub = Ui.T("-", 12, p.Muted);
            var speed = Ui.T("-", 18, p.Text, 650);
            var main = Ui.V(1, name, sub);
            ProgressBar? bar = null;
            if (hot)
            {
                bar = new ProgressBar { Maximum = 100, Foreground = Ui.B(p.Warn), Margin = new Thickness(0, 5, 0, 0), Visibility = Visibility.Collapsed };
                main.Children.Add(bar);
            }
            var g = Ui.Columns(Ui.Auto, Ui.Star(), Ui.Auto);
            g.Children.Add(Ui.Dot(colors[Math.Min(i, colors.Length - 1)]).At(0));
            g.Children.Add(main.At(1));
            g.Children.Add(speed.At(2));
            var box = Ui.Box(g, p.Inner, p.InnerStroke, 12, new Thickness(14, 11, 14, 11));
            tileViews.Add(new TileView { Root = box, Name = name, Sub = sub, Speed = speed, Bar = bar });
            tiles.Children.Add(box);
        }
        LayoutTiles(true);
    }

    void LayoutTiles(bool force)
    {
        int n = tileViews.Count;
        if (n == 0) return;
        double width = tiles.ActualWidth;
        int cols = width <= 0 ? n : Math.Max(1, Math.Min(n, (int)((width + 12) / (210 + 12))));
        if (!force && cols == tileCols) return;
        tileCols = cols;
        tiles.ColumnDefinitions.Clear();
        tiles.RowDefinitions.Clear();
        for (int c = 0; c < cols; c++) tiles.ColumnDefinitions.Add(new ColumnDefinition { Width = Ui.Star() });
        int rows = (n + cols - 1) / cols;
        for (int r = 0; r < rows; r++) tiles.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        for (int i = 0; i < n; i++) tileViews[i].Root.At(i % cols, i / cols);
    }

    // ---- apps ------------------------------------------------------------------------------------ //
    static string SettingKey(string key) => key == "qbt" ? "qbt_auto" : key;
    bool AppOn(string key) => settings.Bool(SettingKey(key));
    JsonObject LiveOf(string key) => (last?.Obj("app_live") ?? new JsonObject()).Obj(key);

    List<AppInfo> ShownApps() =>
        Apps.Where(a => a.Key == "qbt" || a.Key == "fdm" || AppOn(a.Key) || LiveOf(a.Key).Num("connections") > 0).ToList();

    void BuildApps()
    {
        var shown = ShownApps();
        appSig = string.Join(",", shown.Select(a => a.Key));
        appRows.Children.Clear();
        appViews.Clear();
        for (int i = 0; i < shown.Count; i++)
        {
            var a = shown[i];
            if (i > 0) appRows.Children.Add(Ui.Divider(p.Line));
            var status = Ui.T("", 12, p.Muted);
            var setup = Ui.Btn("Set up", () => OpenGuide(a.Key), small: true);
            var fix = Ui.Btn("Fix", () => OpenGuide(a.Key), small: true);
            Ui.Paint(fix, p.Warn, p.Dark ? Palette.Hex("#1b1600") : Colors.White);
            fix.Visibility = Visibility.Collapsed;
            var sw = Ui.Switch($"Use DualLink with {a.Name}");
            sw.IsOn = AppOn(a.Key);
            sw.Toggled += (_, _) =>
            {
                if (quiet || sw.IsOn == AppOn(a.Key)) return;  // set by the window itself
                SetApp(a.Key, sw.IsOn);
            };
            var g = Ui.Columns(Ui.Auto, Ui.Star(), Ui.Auto, Ui.Auto);
            g.Padding = new Thickness(0, 11, 0, 11);
            g.Children.Add(Ui.Badge(a.Badge, Palette.Hex(a.Color)).At(0));
            g.Children.Add(Ui.V(1, Ui.T(a.Name, 13.5, p.Text, 600), status).At(1));
            g.Children.Add(setup.At(2));
            g.Children.Add(fix.At(2));
            g.Children.Add(sw.At(3));
            appRows.Children.Add(g);
            appViews.Add(new AppRow { Key = a.Key, Status = status, Setup = setup, Fix = fix, Switch = sw });
        }
        if (last != null) RenderApps(last, false);
    }

    void SetApp(string key, bool on)
    {
        Save(Patch(SettingKey(key), on));
        if (on) OpenGuide(key);
        if (!on && key != "qbt" && key != "fdm")
        {
            var t = queue.CreateTimer();                      // extra apps leave the list when switched off
            t.Interval = TimeSpan.FromMilliseconds(400);
            t.IsRepeating = false;
            t.Tick += (_, _) => BuildApps();
            t.Start();
        }
    }

    void RenderApps(JsonObject s, bool current)
    {
        if (string.Join(",", ShownApps().Select(a => a.Key)) != appSig)
        {
            BuildApps();
            return;
        }
        var qbtState = s.Obj("qbt").Str("state");
        foreach (var row in appViews)
        {
            var key = row.Key;
            bool on = key == "qbt" ? s.Bool("qbt_auto") : s.Obj("apps").Bool(key);
            if (current && !savePending && row.Switch.IsOn != on)
            {
                settings[SettingKey(key)] = on;                  // changed somewhere else
                quiet = true;
                row.Switch.IsOn = on;
                quiet = false;
            }
            else on = row.Switch.IsOn;
            var live = s.Obj("app_live").Obj(key);
            double n = live.Num("connections");
            var programs = live.Arr("programs").Select(x => x.AsStr()).ToList();
            double seen = s.Obj("seen_apps").Num(key);
            var app = Apps.First(a => a.Key == key);
            string who = key == "other" && programs.Count > 0 ? string.Join(", ", programs.Take(3)) + " · " : "";
            string t = "Off";
            Windows.UI.Color color = p.Muted;
            bool fix = false;
            if (key == "qbt" && on)
            {
                if (!running) t = "Ready - set up automatically when DualLink is on";
                else if (qbtState == "connected")
                {
                    t = n > 0 ? $"✓ Connected · {Fmt.Plural(n, "connection")} now" : "✓ Connected · no transfers right now";
                    color = p.On;
                }
                else if (qbtState == "auth") { t = "Login needed"; color = p.Warn; fix = true; }
                else { t = "Can't reach qBittorrent - needs setup"; color = p.Warn; fix = true; }
            }
            else if (on)
            {
                if (s.Str("standby_error") != "") { t = "Proxy port is in use - change it in Settings"; color = p.Bad; }
                else if (n > 0)
                {
                    t = running ? $"✓ {who}{Fmt.Plural(n, "connection")} now"
                                : $"✓ {who}{(who != "" ? "p" : "Working - p")}assing straight through while DualLink is off";
                    color = p.On;
                }
                else if (seen > 0) { t = $"✓ Set up · idle (last used {Fmt.Ago(seen)})"; color = Palette.Mix(p.On, p.Muted, 0.45); }
                else t = $"Not seen yet - start a download in {(app.Name == "Other app" ? "the app" : app.Name)} to check the setup";
            }
            else if (n > 0 && key != "qbt")
            {
                t = $"{who}Using DualLink - switch on so it keeps working while DualLink is off";
                color = p.Warn;
            }
            row.Status.Text = t;
            row.Status.Foreground = Ui.B(color);
            row.Setup.Visibility = fix ? Visibility.Collapsed : Visibility.Visible;
            row.Fix.Visibility = fix ? Visibility.Visible : Visibility.Collapsed;
        }
    }

    // ---- toast --------------------------------------------------------------------------------- //
    void Toast(string message, bool error = false)
    {
        if (string.IsNullOrEmpty(message)) return;
        toastText.Text = message;
        toast.BorderBrush = Ui.B(error ? p.Bad : p.Line);
        toast.Opacity = 1;
        toastTimer.Stop();
        toastTimer.Interval = TimeSpan.FromSeconds(error ? 6 : 3);
        toastTimer.Start();
    }
}
