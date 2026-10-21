using System.Globalization;
using System.Text.RegularExpressions;
using Microsoft.UI;
using Microsoft.UI.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Documents;
using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace DualLinkWindow;

/// <summary>DualLink's colours for dark and light mode (the link colours match the tray icon and web window).</summary>
public sealed class Palette
{
    public required bool Dark;
    public required Color Bg, Card, CardStroke, Inner, InnerStroke, Line, Text, Muted, Faint, On, Warn, Bad, Toast;
    public Color Eth = Hex("#4d8dff"), Hot = Hex("#ff9b3d"), X1 = Hex("#a78bfa"), X2 = Hex("#2dd4bf");

    public static readonly Palette DarkMode = new()
    {
        Dark = true, Bg = Hex("#16181d"), Card = Hex("#12ffffff"), CardStroke = Hex("#1cffffff"), Inner = Hex("#0cffffff"),
        InnerStroke = Hex("#16ffffff"), Line = Hex("#1affffff"), Text = Hex("#e9ecf2"), Muted = Hex("#9aa2b4"),
        Faint = Hex("#6b7285"), On = Hex("#2fd67b"), Warn = Hex("#ffc84d"), Bad = Hex("#ff5d6c"), Toast = Hex("#2b2f38"),
    };

    public static readonly Palette LightMode = new()
    {
        Dark = false, Bg = Hex("#f3f5f9"), Card = Hex("#c8ffffff"), CardStroke = Hex("#14000000"), Inner = Hex("#99f7f8fa"),
        InnerStroke = Hex("#12000000"), Line = Hex("#14000000"), Text = Hex("#161a23"), Muted = Hex("#5e6678"),
        Faint = Hex("#8d94a3"), On = Hex("#16a35a"), Warn = Hex("#b8740a"), Bad = Hex("#d13438"), Toast = Hex("#ffffff"),
    };

    public Color[] LinkColors => [Eth, Hot, X1, X2];

    public static Color Hex(string s)
    {
        s = s.TrimStart('#');
        byte P(int i) => byte.Parse(s.Substring(i, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
        return s.Length == 8 ? ColorHelper.FromArgb(P(0), P(2), P(4), P(6)) : ColorHelper.FromArgb(255, P(0), P(2), P(4));
    }

    public static Color WithAlpha(Color c, byte a) => ColorHelper.FromArgb(a, c.R, c.G, c.B);

    public static Color Mix(Color a, Color b, double t) => ColorHelper.FromArgb(
        (byte)(a.A + (b.A - a.A) * t), (byte)(a.R + (b.R - a.R) * t), (byte)(a.G + (b.G - a.G) * t), (byte)(a.B + (b.B - a.B) * t));
}

/// <summary>Number formatting, the same as the web window.</summary>
public static class Fmt
{
    static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    static readonly string[] SpeedUnits = ["B/s", "KiB/s", "MiB/s", "GiB/s"];

    public static (string Num, string Unit) Speed(double b)
    {
        int i = 0;
        while (b >= 1024 && i < SpeedUnits.Length - 1) { b /= 1024; i++; }
        return (i == 0 ? Math.Round(b).ToString(Inv) : b.ToString(b < 10 ? "0.00" : "0.0", Inv), SpeedUnits[i]);
    }

    public static string SpeedStr(double b)
    {
        var (n, u) = Speed(b);
        return $"{n} {u}";
    }

    public static string Size(double b)
    {
        string[] u = ["B", "KiB", "MiB", "GiB", "TiB"];
        int i = 0;
        while (b >= 1024 && i < u.Length - 1) { b /= 1024; i++; }
        return i == 0 ? $"{Math.Round(b).ToString(Inv)} B" : $"{b.ToString("0.0", Inv)} {u[i]}";
    }

    public static string Axis(double b)
    {
        if (b <= 0) return "0";
        int i = 0;
        while (b >= 1000 && i < SpeedUnits.Length - 1) { b /= 1024; i++; }
        return $"{Math.Round(b, b < 10 ? 1 : 0).ToString(Inv)} {SpeedUnits[i]}";
    }

    public static string Plural(double n, string word) => $"{n.ToString(Inv)} {word}{(n == 1 ? "" : "s")}";

    public static string Ago(double ts)
    {
        var m = Math.Max(0, Math.Round((DateTimeOffset.UtcNow.ToUnixTimeSeconds() - ts) / 60));
        return m < 1 ? "just now" : m < 60 ? $"{m} min ago" : m < 48 * 60 ? $"{Math.Round(m / 60)} h ago" : $"{Math.Round(m / 1440)} days ago";
    }

    public static string Mbps(double bps) => (bps * 8 / 1e6).ToString("0", Inv);

    public static double NiceMax(double v)
    {
        if (v <= 0) return 1024 * 1024;
        double p = Math.Pow(1024, Math.Floor(Math.Log(v) / Math.Log(1024)));
        foreach (var s in new double[] { 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000 })
            if (s * p >= v * 1.1) return s >= 1000 ? 1024 * p : s * p;
        return v * 1.2;
    }

    public static string CleanNotes(string t)
    {
        t = Regex.Replace(t, @"^#+\s*", "", RegexOptions.Multiline);
        t = t.Replace("**", "");
        t = Regex.Replace(t, @"^\s*[*-] ", "• ", RegexOptions.Multiline);
        t = Regex.Replace(t, @" by @\S+ in https://\S+", "");
        t = Regex.Replace(t, @"^Full Changelog:.*$", "", RegexOptions.Multiline);
        return t.Trim();
    }
}

/// <summary>Small builders for the interface (it's built in code, without XAML files).</summary>
public static class Ui
{
    public static SolidColorBrush B(Color c) => new(c);

    public static Windows.UI.Text.FontWeight W(ushort weight) => new() { Weight = weight };

    public static TextBlock T(string text, double size, Color fg, ushort weight = 400, bool wrap = false)
    {
        return new TextBlock
        {
            Text = text, FontSize = size, Foreground = B(fg), FontWeight = W(weight),
            TextWrapping = wrap ? TextWrapping.WrapWholeWords : TextWrapping.NoWrap,
            TextTrimming = wrap ? TextTrimming.None : TextTrimming.CharacterEllipsis,
            VerticalAlignment = VerticalAlignment.Center,
        };
    }

    /// <summary>Text with <b>bold</b> parts (the guides are written that way).</summary>
    public static TextBlock Rich(string marked, double size, Color fg, Color boldFg)
    {
        var tb = new TextBlock { FontSize = size, Foreground = B(fg), TextWrapping = TextWrapping.WrapWholeWords };
        SetRich(tb, marked, boldFg);
        return tb;
    }

    public static void SetRich(TextBlock tb, string marked, Color boldFg)
    {
        tb.Inlines.Clear();
        var parts = marked.Split(new[] { "<b>", "</b>" }, StringSplitOptions.None);
        for (int i = 0; i < parts.Length; i++)
        {
            if (parts[i].Length == 0) continue;
            var run = new Run { Text = parts[i] };
            if (i % 2 == 1)
            {
                var bold = new Span { FontWeight = W(600), Foreground = B(boldFg) };
                bold.Inlines.Add(run);
                tb.Inlines.Add(bold);
            }
            else tb.Inlines.Add(run);
        }
    }

    public static StackPanel H(double spacing, params UIElement[] children)
    {
        var p = new StackPanel { Orientation = Orientation.Horizontal, Spacing = spacing };
        foreach (var c in children) p.Children.Add(c);
        return p;
    }

    public static StackPanel V(double spacing, params UIElement[] children)
    {
        var p = new StackPanel { Orientation = Orientation.Vertical, Spacing = spacing };
        foreach (var c in children) p.Children.Add(c);
        return p;
    }

    public static Grid Columns(params GridLength[] widths)
    {
        var g = new Grid { ColumnSpacing = 12 };
        foreach (var w in widths) g.ColumnDefinitions.Add(new ColumnDefinition { Width = w });
        return g;
    }

    public static GridLength Star(double v = 1) => new(v, GridUnitType.Star);
    public static GridLength Auto => GridLength.Auto;
    public static GridLength Px(double v) => new(v);

    public static TEl At<TEl>(this TEl el, int column, int row = 0) where TEl : FrameworkElement
    {
        Grid.SetColumn(el, column);
        Grid.SetRow(el, row);
        return el;
    }

    public static Border Box(UIElement child, Color bg, Color stroke, double radius, Thickness padding)
    {
        return new Border
        {
            Child = child, Background = B(bg), BorderBrush = B(stroke), BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(radius), Padding = padding,
        };
    }

    public static Button Btn(string text, Action onClick, bool primary = false, bool small = false)
    {
        var b = new Button { Content = text, VerticalAlignment = VerticalAlignment.Center };
        if (small)
        {
            b.FontSize = 12.5;
            b.Padding = new Thickness(11, 3, 11, 4);
        }
        if (primary) b.Style = AccentStyle();
        b.Click += (_, _) => onClick();
        return b;
    }

    static Style? accent;

    public static Style? AccentStyle()
    {
        if (accent != null) return accent;
        try
        {
            accent = Application.Current.Resources["AccentButtonStyle"] as Style;
        }
        catch (Exception)
        {
            accent = null;
        }
        return accent;
    }

    public static Button IconBtn(string glyph, string tooltip, Action onClick, double size = 36)
    {
        var b = new Button
        {
            Content = new FontIcon { Glyph = glyph, FontSize = 15 }, Width = size, Height = size,
            Padding = new Thickness(0), VerticalAlignment = VerticalAlignment.Center,
        };
        ToolTipService.SetToolTip(b, tooltip);
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(b, tooltip);
        b.Click += (_, _) => onClick();
        return b;
    }

    public static HyperlinkButton Link(string text, Action onClick, double size = 13)
    {
        var b = new HyperlinkButton { Content = text, FontSize = size, Padding = new Thickness(0) };
        b.Click += (_, _) => onClick();
        return b;
    }

    public static ToggleSwitch Switch(string name)
    {
        var t = new ToggleSwitch { OnContent = "", OffContent = "", MinWidth = 0, VerticalAlignment = VerticalAlignment.Center };
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(t, name);
        return t;
    }

    public static Border Divider(Color c) => new() { Height = 1, Background = B(c), HorizontalAlignment = HorizontalAlignment.Stretch };

    public static Border Dot(Color c, double size = 10) => new()
    {
        Width = size, Height = size, CornerRadius = new CornerRadius(size / 2), Background = B(c), VerticalAlignment = VerticalAlignment.Center,
    };

    public static Border Badge(string text, Color c) => new()
    {
        Width = 34, Height = 34, CornerRadius = new CornerRadius(9), Background = B(c), VerticalAlignment = VerticalAlignment.Center,
        Child = new TextBlock
        {
            Text = text, FontSize = 11, FontWeight = W(700), Foreground = B(Colors.White),
            HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center,
        },
    };

    public static TextBlock SectionTitle(string text, Color c) => new()
    {
        Text = text.ToUpperInvariant(), FontSize = 12, FontWeight = W(600), Foreground = B(c), CharacterSpacing = 80,
    };
}
