using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;

namespace DualLinkWindow;

/// <summary>Command-line options.</summary>
/// <remarks>
///   (none)                 the normal window, talking to the running DualLink engine
///   --demo                 made-up data instead of the engine (for screenshots and trying things out)
///   --screenshots [dir]    open every screen, save a PNG of each into dir, then close
///   --theme light|dark     force a theme (default: follow Windows)
/// </remarks>
public sealed class LaunchOptions
{
    public bool Demo;
    public string? ScreenshotsDir;
    public ElementTheme Theme = ElementTheme.Default;

    public static LaunchOptions Parse(string[] args)
    {
        var o = new LaunchOptions();
        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--demo":
                    o.Demo = true;
                    break;
                case "--screenshots":
                    o.ScreenshotsDir = i + 1 < args.Length && !args[i + 1].StartsWith("--") ? args[++i] : "shots";
                    break;
                case "--theme":
                    if (i + 1 < args.Length)
                        o.Theme = args[++i] == "light" ? ElementTheme.Light : args[i] == "dark" ? ElementTheme.Dark : ElementTheme.Default;
                    break;
            }
        }
        if (o.ScreenshotsDir != null) o.ScreenshotsDir = ResolveDir(o.ScreenshotsDir);
        return o;
    }

    static string ResolveDir(string p)
    {
        p = Environment.ExpandEnvironmentVariables(p);
        if (Path.IsPathRooted(p)) return p;
        // relative paths are relative to the DualLink repository when running from a build folder
        for (var d = new DirectoryInfo(AppContext.BaseDirectory); d != null; d = d.Parent)
            if (File.Exists(Path.Combine(d.FullName, "main.py"))) return Path.Combine(d.FullName, p);
        return Path.GetFullPath(p);
    }
}

public static class Program
{
    public static LaunchOptions Opts { get; private set; } = new();

    [STAThread]
    static int Main(string[] args)
    {
        Opts = LaunchOptions.Parse(args);
        bool normal = !Opts.Demo && Opts.ScreenshotsDir == null;
        if (normal && !Native.AcquireMutex(Native.WindowMutex))
        {
            Native.Signal(Native.WindowFront);          // already open: bring it to the front
            return 0;
        }
        IEngine? engine = Opts.Demo ? new DemoEngine() : EngineClient.Find();
        if (engine == null)
        {
            // DualLink isn't running: start it - it opens a window by itself
            Native.ReleaseMutex();
            return EngineClient.StartEngine() ? 0 : 1;
        }
        WinRT.ComWrappersSupport.InitializeComWrappers();
        Application.Start(callback =>
        {
            var context = new DispatcherQueueSynchronizationContext(DispatcherQueue.GetForCurrentThread());
            SynchronizationContext.SetSynchronizationContext(context);
            new DualLinkApp(engine);
        });
        return 0;
    }
}
