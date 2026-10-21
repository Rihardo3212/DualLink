using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace DualLinkWindow;

/// <summary>The Settings pane (opens from the right, like the web window's drawer).</summary>
public sealed partial class MainView
{
    ComboBox ethBox = null!, hotBox = null!;
    StackPanel extraList = null!;
    HyperlinkButton addExtra = null!;
    TextBlock extraHint = null!, restartNote = null!, verText = null!;
    ToggleSwitch sAuto = null!, sRecycle = null!, sOnHotspot = null!, sOnStart = null!, sTray = null!, sAutostart = null!,
        sUpdates = null!;
    FrameworkElement recycleRow = null!;
    Grid manualShares = null!;
    NumberBox nShareEth = null!, nShareHot = null!, nQuota = null!, nPort = null!;
    Button checkNow = null!;
    ScrollViewer settingsScroll = null!;
    bool filling;

    void OpenSettings(bool open) => split.IsPaneOpen = open;

    UIElement SettingsPane()
    {
        var head = Ui.Columns(Ui.Star(), Ui.Auto);
        head.Margin = new Thickness(20, 14, 16, 6);
        head.Children.Add(Ui.T("Settings", 17, p.Text, 600).At(0));
        head.Children.Add(Ui.IconBtn("", "Close settings", () => OpenSettings(false), 32).At(1));

        restartNote = Ui.T("A new proxy port applies the next time you turn DualLink on.", 12, p.Warn, wrap: true);
        restartNote.Margin = new Thickness(0, 2, 0, 6);
        restartNote.Visibility = Visibility.Collapsed;

        // connections
        ethBox = AdapterBox("eth_name");
        hotBox = AdapterBox("hot_name");
        var refresh = Ui.Link("refresh the list", () => _ = RefreshAdapters(true), 11.5);
        refresh.Margin = new Thickness(3, 0, 0, 0);
        var notListed = Ui.H(0, Ui.T("Not listed? Connect your phone, then", 11.5, p.Faint), refresh, Ui.T(".", 11.5, p.Faint));
        extraList = Ui.V(8);
        addExtra = Ui.Link("+ Add another connection", () => _ = AddExtra());
        extraHint = Ui.T("For example Wi-Fi or a second phone. Up to two extra connections.", 11.5, p.Faint, wrap: true);
        var connections = Section("Connections", true,
            Field("Ethernet (home internet)", ethBox),
            Field("Phone hotspot", hotBox, notListed),
            extraList, addExtra, extraHint);

        // balancing
        sAuto = BoundSwitch("auto", "Favour the faster connection automatically");
        sRecycle = BoundSwitch("recycle", "Move idle connections to the faster link");
        nShareEth = Number(0.1, 1000, 0.5);
        nShareHot = Number(0.1, 1000, 0.5);
        nShareEth.ValueChanged += (_, e) => { if (!filling) Save(Patch("share_eth", double.IsNaN(e.NewValue) ? 1 : e.NewValue)); };
        nShareHot.ValueChanged += (_, e) => { if (!filling) Save(Patch("share_hot", double.IsNaN(e.NewValue) ? 1 : e.NewValue)); };
        manualShares = Ui.Columns(Ui.Star(), Ui.Star());
        manualShares.Children.Add(Field("Ethernet share", nShareEth).At(0));
        manualShares.Children.Add(Field("Hotspot share", nShareHot).At(1));
        recycleRow = ToggleRow("Move idle connections to the faster link", "Reconnects peers that have been idle for 90 s", sRecycle);
        var balancing = Section("Balancing", false,
            ToggleRow("Favour the faster connection automatically", "Gives the faster link more of the new connections", sAuto),
            recycleRow, manualShares);

        // mobile data
        nQuota = Number(0, 10_000_000, 100);
        nQuota.ValueChanged += (_, e) => { if (!filling) Save(Patch("quota_mb", double.IsNaN(e.NewValue) ? 0 : e.NewValue)); };
        var mobile = Section("Mobile data", false,
            Field("Stop using the hotspot after (MB)", nQuota, Ui.T("0 = no limit. Counted from when you turn DualLink on.", 11.5, p.Faint, wrap: true)));

        // automation
        sOnHotspot = BoundSwitch("auto_on_hotspot", "Turn on when the hotspot connects");
        sOnStart = BoundSwitch("auto_on_start", "Turn on when DualLink starts");
        var automation = Section("Automation", false,
            ToggleRow("Turn on when the hotspot connects", "Plug in your phone and DualLink starts combining", sOnHotspot),
            ToggleRow("Turn on when DualLink starts", "Combine straight away - handy with Start with Windows", sOnStart));

        // app
        sTray = BoundSwitch("tray", "Keep running in the tray when closed");
        sAutostart = BoundSwitch("autostart", "Start with Windows");
        nPort = Number(1024, 65535, 1);
        nPort.ValueChanged += (_, e) => { if (!filling) Save(Patch("port", double.IsNaN(e.NewValue) ? 1080 : (int)e.NewValue)); };
        var app = Section("App", false,
            ToggleRow("Keep running in the tray when closed", null, sTray),
            ToggleRow("Start with Windows", null, sAutostart),
            Field("Proxy port", nPort));

        // updates
        sUpdates = BoundSwitch("check_updates", "Check for updates");
        verText = Ui.T("DualLink", 12.5, p.Faint);
        checkNow = Ui.Btn("Check now", () => _ = CheckNow(), small: true);
        var verRow = Ui.Columns(Ui.Star(), Ui.Auto);
        verRow.Margin = new Thickness(0, 4, 0, 8);
        verRow.Children.Add(verText.At(0));
        verRow.Children.Add(checkNow.At(1));
        var updates = Section("Updates", false,
            ToggleRow("Check for updates", "Looks at the GitHub Releases page twice a day", sUpdates), verRow);

        var body = Ui.V(0, restartNote, connections, balancing, mobile, automation, app, updates);
        body.Padding = new Thickness(20, 0, 20, 24);
        settingsScroll = new ScrollViewer { Content = body, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        var pane = new Grid();
        pane.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        pane.RowDefinitions.Add(new RowDefinition { Height = Ui.Star() });
        pane.Children.Add(head.At(0, 0));
        pane.Children.Add(settingsScroll.At(0, 1));
        return pane;
    }

    // ---- building blocks ---------------------------------------------------------------------- //
    StackPanel Section(string title, bool first, params UIElement[] children)
    {
        var s = new StackPanel();
        if (!first) s.Children.Add(Ui.Divider(p.Line));
        var t = Ui.SectionTitle(title, p.Muted);
        t.Margin = new Thickness(0, 14, 0, 10);
        s.Children.Add(t);
        foreach (var c in children) s.Children.Add(c);
        s.Margin = new Thickness(0, 0, 0, 6);
        return s;
    }

    StackPanel Field(string label, UIElement control, UIElement? hint = null)
    {
        var f = Ui.V(5, Ui.T(label, 12.5, p.Muted), control);
        if (hint != null) f.Children.Add(hint);
        f.Margin = new Thickness(0, 0, 0, 12);
        return f;
    }

    Grid ToggleRow(string title, string? description, ToggleSwitch toggle)
    {
        var g = Ui.Columns(Ui.Star(), Ui.Auto);
        g.Padding = new Thickness(0, 6, 0, 6);
        var text = Ui.V(1, Ui.T(title, 14, p.Text, wrap: true));
        if (description != null) text.Children.Add(Ui.T(description, 11.5, p.Faint, wrap: true));
        text.VerticalAlignment = VerticalAlignment.Center;
        g.Children.Add(text.At(0));
        g.Children.Add(toggle.At(1));
        return g;
    }

    ToggleSwitch BoundSwitch(string key, string name)
    {
        var t = Ui.Switch(name);
        t.Toggled += (_, _) =>
        {
            if (filling || t.IsOn == settings.Bool(key)) return;
            Save(Patch(key, t.IsOn));
        };
        return t;
    }

    static NumberBox Number(double min, double max, double step) => new()
    {
        Minimum = min, Maximum = max, SmallChange = step, LargeChange = step * 10,
        SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact, HorizontalAlignment = HorizontalAlignment.Stretch,
    };

    ComboBox AdapterBox(string key)
    {
        var box = new ComboBox { HorizontalAlignment = HorizontalAlignment.Stretch, PlaceholderText = "Choose a connection…" };
        box.SelectionChanged += (_, _) =>
        {
            if (filling || box.SelectedItem is not ComboBoxItem item) return;
            Save(Patch(key, (string)item.Tag));
        };
        return box;
    }

    static string AdapterLabel(JsonNode? a) =>
        a.Bool("present") ? $"{a.Str("name")} - {a.Str("ip")}" : $"{a.Str("name")} (not connected)";

    void FillBox(ComboBox box, string selected)
    {
        box.Items.Clear();
        box.PlaceholderText = adapterList.Count == 0 ? "No connections found" : "Choose a connection…";
        int index = -1;
        foreach (var a in adapterList)
        {
            if (a.Str("name") == selected) index = box.Items.Count;
            box.Items.Add(new ComboBoxItem { Content = AdapterLabel(a), Tag = a.Str("name") });
        }
        box.SelectedIndex = index;
    }

    // ---- filling in the values ----------------------------------------------------------------- //
    void FillAdapters()
    {
        filling = true;
        try
        {
            FillBox(ethBox, settings.Str("eth_name"));
            FillBox(hotBox, settings.Str("hot_name"));
        }
        finally
        {
            filling = false;
        }
        FillExtras();
    }

    List<string> Extras() => settings.Arr("extra").Select(x => x.AsStr()).Where(x => x != "").ToList();

    static JsonArray ToArray(IEnumerable<string> names) => new(names.Select(n => (JsonNode?)JsonValue.Create(n)).ToArray());

    void FillExtras()
    {
        var extra = Extras();
        filling = true;
        try
        {
            extraList.Children.Clear();
            for (int i = 0; i < extra.Count; i++)
            {
                int index = i;
                var box = new ComboBox { HorizontalAlignment = HorizontalAlignment.Stretch };
                FillBox(box, extra[i]);
                box.SelectionChanged += (_, _) =>
                {
                    if (filling || box.SelectedItem is not ComboBoxItem item) return;
                    var ex = Extras();
                    if (index < ex.Count) ex[index] = (string)item.Tag;
                    Save(Patch("extra", ToArray(ex)));
                };
                var remove = Ui.IconBtn("", "Remove this connection", () =>
                {
                    var ex = Extras();
                    if (index < ex.Count) ex.RemoveAt(index);
                    Save(Patch("extra", ToArray(ex)));
                    FillExtras();
                }, 32);
                var row = Ui.Columns(Ui.Star(), Ui.Auto);
                row.ColumnSpacing = 8;
                row.Children.Add(box.At(0));
                row.Children.Add(remove.At(1));
                extraList.Children.Add(row);
            }
        }
        finally
        {
            filling = false;
        }
        var used = new HashSet<string> { settings.Str("eth_name"), settings.Str("hot_name") };
        used.UnionWith(extra);
        var free = adapterList.Where(a => a.Bool("present") && !used.Contains(a.Str("name"))).Select(a => a.Str("name")).ToList();
        addExtra.Visibility = extra.Count >= 2 ? Visibility.Collapsed : Visibility.Visible;
        extraHint.Text = extra.Count >= 2 ? "That's the maximum of two extra connections."
            : free.Count > 0 ? $"Available now: {string.Join(", ", free)}."
            : extra.Count > 0 ? "You can add one more - connect it first, for example Wi-Fi or a second phone."
            : "For example Wi-Fi or a second phone - connect it first, then add it here.";
    }

    void FillSettings()
    {
        filling = true;
        try
        {
            sAuto.IsOn = settings.Bool("auto");
            sRecycle.IsOn = settings.Bool("recycle");
            nShareEth.Value = settings.Num("share_eth", 1);
            nShareHot.Value = settings.Num("share_hot", 1);
            nQuota.Value = settings.Num("quota_mb");
            nPort.Value = settings.Num("port", 1080);
            sTray.IsOn = settings.Bool("tray");
            sAutostart.IsOn = settings.Bool("autostart");
            sOnHotspot.IsOn = settings.Bool("auto_on_hotspot");
            sOnStart.IsOn = settings.Bool("auto_on_start");
            sUpdates.IsOn = settings.Bool("check_updates");
        }
        finally
        {
            filling = false;
        }
        UpdateBalancingRows();
    }

    void UpdateBalancingRows()
    {
        bool auto = settings.Bool("auto");
        manualShares.Visibility = auto ? Visibility.Collapsed : Visibility.Visible;
        recycleRow.Visibility = auto ? Visibility.Visible : Visibility.Collapsed;
    }

    // ---- actions ------------------------------------------------------------------------------- //
    async Task RefreshAdapters(bool announce)
    {
        try
        {
            var r = await engine.CallAsync("refresh_adapters");
            settings = r.Obj("settings");
            adapterList = r.Arr("adapters");
            FillAdapters();
            if (announce) Toast("Connection list refreshed");
        }
        catch (EngineError e)
        {
            Toast(e.Message, true);
        }
    }

    async Task AddExtra()
    {
        string? Pick()
        {
            var used = new HashSet<string> { settings.Str("eth_name"), settings.Str("hot_name") };
            used.UnionWith(Extras());
            return adapterList.Where(a => a.Bool("present") && !used.Contains(a.Str("name"))).Select(a => a.Str("name")).FirstOrDefault();
        }
        var pick = Pick();
        if (pick == null)
        {
            await RefreshAdapters(false);                  // maybe it was just plugged in: look again
            pick = Pick();
        }
        if (pick == null)
        {
            Toast("No other connection found. Connect Wi-Fi, a second phone or another network adapter, then try again.", true);
            return;
        }
        var ex = Extras();
        ex.Add(pick);
        Save(Patch("extra", ToArray(ex)));
        FillExtras();
    }

    async Task CheckNow()
    {
        checkNow.IsEnabled = false;
        try
        {
            var r = await engine.CallAsync("check_updates");
            if (!r.Bool("ok")) Toast(r.Str("message"), true);
            else if (r.Get("update") is JsonObject update)
            {
                OpenSettings(false);
                last ??= new JsonObject();
                last["update"] = update.DeepClone();
                OpenUpdate();
            }
            else Toast("You have the latest version");
        }
        catch (EngineError e)
        {
            Toast(e.Message, true);
        }
        finally
        {
            checkNow.IsEnabled = true;
        }
    }
}
