using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;
using Windows.Foundation;

namespace DualLinkWindow;

/// <summary>The speed graph: one stacked area per connection, newest on the right.</summary>
public sealed class Chart
{
    public readonly Grid Root;
    readonly Canvas canvas = new();
    readonly Palette p;
    List<double[]> series = [];

    public Chart(Palette palette)
    {
        p = palette;
        Root = new Grid { Height = 110, Margin = new Thickness(0, 14, 0, 0) };
        Root.Children.Add(canvas);
        Root.SizeChanged += (_, _) => Draw();
    }

    public void Update(List<double[]> s)
    {
        series = s;
        Draw();
    }

    void Draw()
    {
        canvas.Children.Clear();
        double w = Root.ActualWidth, h = Root.ActualHeight;
        if (w <= 0 || h <= 0 || series.Count == 0) return;
        int n = series.Max(s => s.Length);
        if (n < 2) return;
        double top = 6, bottom = h - 4, right = w - 58;
        var stacks = new List<(double[] Lower, double[] Upper)>();
        var acc = new double[n];
        foreach (var s in series)
        {
            var next = new double[n];
            for (int i = 0; i < n; i++) next[i] = acc[i] + (i < s.Length ? s[i] : 0);
            stacks.Add((acc, next));
            acc = next;
        }
        double max = Fmt.NiceMax(acc.Max());
        double X(int i) => right * i / (n - 1);
        double Y(double v) => bottom - (bottom - top) * (v / max);

        var lineBrush = Ui.B(p.Line);
        for (int k = 0; k <= 2; k++)
        {
            double v = max * k / 2, yy = Math.Round(Y(v)) + 0.5;
            canvas.Children.Add(new Line { X1 = 0, X2 = right, Y1 = yy, Y2 = yy, Stroke = lineBrush, StrokeThickness = 1 });
            var label = Ui.T(Fmt.Axis(v), 11, p.Faint);
            Canvas.SetLeft(label, right + 8);
            Canvas.SetTop(label, yy - 8);
            canvas.Children.Add(label);
        }
        if (acc.Max() <= 0) return;

        var colors = p.LinkColors;
        for (int si = 0; si < stacks.Count; si++)
        {
            var (lower, upper) = stacks[si];
            var color = colors[Math.Min(si, colors.Length - 1)];
            var fill = new LinearGradientBrush { StartPoint = new Point(0, 0), EndPoint = new Point(0, 1) };
            fill.GradientStops.Add(new GradientStop { Color = Palette.WithAlpha(color, 0x66), Offset = 0 });
            fill.GradientStops.Add(new GradientStop { Color = Palette.WithAlpha(color, 0x14), Offset = 1 });
            var figure = new PathFigure { StartPoint = new Point(X(0), Y(upper[0])), IsClosed = true, IsFilled = true };
            var outline = new PolyLineSegment();
            for (int i = 1; i < n; i++) outline.Points.Add(new Point(X(i), Y(upper[i])));
            for (int i = n - 1; i >= 0; i--) outline.Points.Add(new Point(X(i), Y(lower[i])));
            figure.Segments.Add(outline);
            var geometry = new PathGeometry();
            geometry.Figures.Add(figure);
            canvas.Children.Add(new Microsoft.UI.Xaml.Shapes.Path { Data = geometry, Fill = fill });

            var line = new Polyline { Stroke = Ui.B(color), StrokeThickness = 1.8, StrokeLineJoin = PenLineJoin.Round };
            for (int i = 0; i < n; i++) line.Points.Add(new Point(X(i), Y(upper[i])));
            canvas.Children.Add(line);
        }
    }
}
