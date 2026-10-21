using Microsoft.UI.Xaml.Controls;
using Windows.Foundation;

namespace DualLinkWindow;

/// <summary>Lays children out left to right and wraps to a new line when there's no room (chips, "address: 127.0.0.1 Copy").</summary>
public partial class WrapPanel : Panel
{
    public double Spacing { get; set; } = 6;

    protected override Size MeasureOverride(Size availableSize)
    {
        double x = 0, y = 0, rowHeight = 0, width = 0;
        foreach (var child in Children)
        {
            child.Measure(new Size(availableSize.Width, double.PositiveInfinity));
            var d = child.DesiredSize;
            if (x > 0 && x + d.Width > availableSize.Width)
            {
                y += rowHeight + Spacing;
                x = 0;
                rowHeight = 0;
            }
            x += d.Width + Spacing;
            rowHeight = Math.Max(rowHeight, d.Height);
            width = Math.Max(width, x - Spacing);
        }
        return new Size(double.IsInfinity(availableSize.Width) ? width : Math.Min(width, availableSize.Width), y + rowHeight);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        double x = 0, y = 0, rowHeight = 0;
        var row = new List<(Microsoft.UI.Xaml.UIElement Child, double X)>();

        void Flush()
        {
            foreach (var (child, cx) in row)
            {
                var d = child.DesiredSize;
                child.Arrange(new Rect(cx, y + (rowHeight - d.Height) / 2, d.Width, d.Height));   // centred in the line
            }
            row.Clear();
        }

        foreach (var child in Children)
        {
            var d = child.DesiredSize;
            if (x > 0 && x + d.Width > finalSize.Width)
            {
                Flush();
                y += rowHeight + Spacing;
                x = 0;
                rowHeight = 0;
            }
            row.Add((child, x));
            x += d.Width + Spacing;
            rowHeight = Math.Max(rowHeight, d.Height);
        }
        Flush();
        return finalSize;
    }
}
