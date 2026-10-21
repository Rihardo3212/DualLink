using System.IO.Compression;
using System.Runtime.InteropServices;

namespace DualLinkWindow;

/// <summary>Windows API: single instance, "come to the front" signals and window screenshots.</summary>
static partial class Native
{
    public const string EngineMutex = "Local\\DualLinkSingleInstance";
    public const string WindowMutex = "Local\\DualLinkWindow";
    public const string WindowFront = "Local\\DualLinkWindowFront";

    static IntPtr mutex = IntPtr.Zero;

    [LibraryImport("kernel32.dll", EntryPoint = "CreateMutexW", SetLastError = true, StringMarshalling = StringMarshalling.Utf16)]
    private static partial IntPtr CreateMutex(IntPtr attributes, int initialOwner, string name);

    [LibraryImport("kernel32.dll", EntryPoint = "OpenMutexW", SetLastError = true, StringMarshalling = StringMarshalling.Utf16)]
    private static partial IntPtr OpenMutex(uint access, int inherit, string name);

    [LibraryImport("kernel32.dll", EntryPoint = "CreateEventW", SetLastError = true, StringMarshalling = StringMarshalling.Utf16)]
    private static partial IntPtr CreateEvent(IntPtr attributes, int manualReset, int initialState, string name);

    [LibraryImport("kernel32.dll", EntryPoint = "OpenEventW", SetLastError = true, StringMarshalling = StringMarshalling.Utf16)]
    private static partial IntPtr OpenEvent(uint access, int inherit, string name);

    [LibraryImport("kernel32.dll")]
    private static partial int SetEvent(IntPtr handle);

    [LibraryImport("kernel32.dll")]
    private static partial uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [LibraryImport("kernel32.dll")]
    private static partial int CloseHandle(IntPtr handle);

    [LibraryImport("user32.dll")]
    public static partial int SetForegroundWindow(IntPtr hwnd);

    [LibraryImport("user32.dll")]
    private static partial int AllowSetForegroundWindow(int processId);

    [LibraryImport("user32.dll")]
    public static partial uint GetDpiForWindow(IntPtr hwnd);

    public static bool AcquireMutex(string name)
    {
        var h = CreateMutex(IntPtr.Zero, 0, name);
        if (Marshal.GetLastPInvokeError() == 183)          // ERROR_ALREADY_EXISTS
        {
            CloseHandle(h);
            return false;
        }
        mutex = h;
        return true;
    }

    public static void ReleaseMutex()
    {
        if (mutex != IntPtr.Zero) CloseHandle(mutex);
        mutex = IntPtr.Zero;
    }

    public static bool MutexHeld(string name)
    {
        var h = OpenMutex(0x00100000, 0, name);            // SYNCHRONIZE
        if (h == IntPtr.Zero) return false;
        CloseHandle(h);
        return true;
    }

    public static void Signal(string name)
    {
        AllowSetForegroundWindow(-1);                      // ASFW_ANY
        var h = OpenEvent(0x0002, 0, name);                // EVENT_MODIFY_STATE
        if (h == IntPtr.Zero) return;
        SetEvent(h);
        CloseHandle(h);
    }

    /// <summary>Calls back (on a background thread) whenever another process signals the named event.</summary>
    public static void Listen(string name, Action callback)
    {
        var h = CreateEvent(IntPtr.Zero, 0, 0, name);       // auto-reset
        if (h == IntPtr.Zero) return;
        var t = new Thread(() =>
        {
            while (WaitForSingleObject(h, 0xFFFFFFFF) == 0) callback();
        }) { IsBackground = true, Name = "front-listener" };
        t.Start();
    }

    // ---- screenshots ------------------------------------------------------------------------- //
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT
    {
        public int Left, Top, Right, Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct BITMAPINFOHEADER
    {
        public uint biSize;
        public int biWidth, biHeight;
        public ushort biPlanes, biBitCount;
        public uint biCompression, biSizeImage;
        public int biXPelsPerMeter, biYPelsPerMeter;
        public uint biClrUsed, biClrImportant;
    }

    [LibraryImport("user32.dll")]
    private static partial int GetWindowRect(IntPtr hwnd, out RECT rect);

    [LibraryImport("dwmapi.dll")]
    private static partial int DwmGetWindowAttribute(IntPtr hwnd, uint attribute, out RECT value, uint size);

    [LibraryImport("user32.dll")]
    private static partial IntPtr GetDC(IntPtr hwnd);

    [LibraryImport("user32.dll")]
    private static partial int ReleaseDC(IntPtr hwnd, IntPtr dc);

    [LibraryImport("gdi32.dll")]
    private static partial IntPtr CreateCompatibleDC(IntPtr dc);

    [LibraryImport("gdi32.dll")]
    private static partial IntPtr CreateDIBSection(IntPtr dc, ref BITMAPINFOHEADER info, uint usage, out IntPtr bits, IntPtr section, uint offset);

    [LibraryImport("gdi32.dll")]
    private static partial IntPtr SelectObject(IntPtr dc, IntPtr obj);

    [LibraryImport("gdi32.dll")]
    private static partial int DeleteObject(IntPtr obj);

    [LibraryImport("gdi32.dll")]
    private static partial int DeleteDC(IntPtr dc);

    [LibraryImport("gdi32.dll")]
    private static partial int BitBlt(IntPtr dst, int x, int y, int w, int h, IntPtr src, int sx, int sy, uint rop);

    [LibraryImport("user32.dll")]
    private static partial int PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);

    /// <summary>Saves a PNG of the window (without its invisible resize border). Returns a short note.</summary>
    public static string SaveWindowPng(IntPtr hwnd, string path)
    {
        GetWindowRect(hwnd, out var wr);
        if (DwmGetWindowAttribute(hwnd, 9, out var fr, 16) != 0) fr = wr;   // DWMWA_EXTENDED_FRAME_BOUNDS
        int w = wr.Right - wr.Left, h = wr.Bottom - wr.Top;
        if (w <= 0 || h <= 0) return "window has no size";
        var screen = GetDC(IntPtr.Zero);
        var mem = CreateCompatibleDC(screen);
        var info = new BITMAPINFOHEADER { biSize = 40, biWidth = w, biHeight = -h, biPlanes = 1, biBitCount = 32 };
        var bmp = CreateDIBSection(screen, ref info, 0, out var bits, IntPtr.Zero, 0);
        var old = SelectObject(mem, bmp);
        string how = "PrintWindow";
        var pixels = new byte[w * h * 4];
        try
        {
            PrintWindow(hwnd, mem, 2);                     // PW_RENDERFULLCONTENT: includes DirectComposition content
            Marshal.Copy(bits, pixels, 0, pixels.Length);
            if (IsBlank(pixels))
            {
                how = "screen copy";                       // fall back to what's on the screen there
                BitBlt(mem, 0, 0, w, h, screen, wr.Left, wr.Top, 0x40CC0020);   // SRCCOPY | CAPTUREBLT
                Marshal.Copy(bits, pixels, 0, pixels.Length);
            }
        }
        finally
        {
            SelectObject(mem, old);
            DeleteObject(bmp);
            DeleteDC(mem);
            ReleaseDC(IntPtr.Zero, screen);
        }
        int ox = Math.Max(0, fr.Left - wr.Left), oy = Math.Max(0, fr.Top - wr.Top);
        int cw = Math.Min(w - ox, fr.Right - fr.Left), ch = Math.Min(h - oy, fr.Bottom - fr.Top);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        Png.Save(path, pixels, w, ox, oy, cw, ch);
        return $"{cw}x{ch} via {how}";
    }

    static bool IsBlank(byte[] bgra)
    {
        for (int i = 0; i < bgra.Length; i += 4 * 97)
            if (bgra[i] != 0 || bgra[i + 1] != 0 || bgra[i + 2] != 0) return false;
        return true;
    }
}

/// <summary>A small PNG writer (no image libraries needed).</summary>
static class Png
{
    static readonly uint[] Crc = MakeCrc();

    static uint[] MakeCrc()
    {
        var t = new uint[256];
        for (uint n = 0; n < 256; n++)
        {
            uint c = n;
            for (int k = 0; k < 8; k++) c = (c & 1) != 0 ? 0xEDB88320u ^ (c >> 1) : c >> 1;
            t[n] = c;
        }
        return t;
    }

    /// <summary>Writes the (cx, cy, cw, ch) part of a top-down BGRA image of the given stride width.</summary>
    public static void Save(string path, byte[] bgra, int width, int cx, int cy, int cw, int ch)
    {
        var raw = new byte[ch * (cw * 4 + 1)];
        int o = 0;
        for (int y = 0; y < ch; y++)
        {
            raw[o++] = 0;                                  // no filter
            int i = ((cy + y) * width + cx) * 4;
            for (int x = 0; x < cw; x++, i += 4)
            {
                raw[o++] = bgra[i + 2];
                raw[o++] = bgra[i + 1];
                raw[o++] = bgra[i];
                raw[o++] = 255;
            }
        }
        using var compressed = new MemoryStream();
        using (var z = new ZLibStream(compressed, CompressionLevel.Optimal, leaveOpen: true)) z.Write(raw, 0, raw.Length);
        using var f = File.Create(path);
        f.Write([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]);
        var ihdr = new byte[13];
        BE(ihdr, 0, (uint)cw);
        BE(ihdr, 4, (uint)ch);
        ihdr[8] = 8;                                       // 8 bits per channel
        ihdr[9] = 6;                                       // RGBA
        Chunk(f, "IHDR", ihdr);
        Chunk(f, "IDAT", compressed.ToArray());
        Chunk(f, "IEND", []);
    }

    static void BE(byte[] b, int at, uint v)
    {
        b[at] = (byte)(v >> 24);
        b[at + 1] = (byte)(v >> 16);
        b[at + 2] = (byte)(v >> 8);
        b[at + 3] = (byte)v;
    }

    static void Chunk(Stream s, string type, byte[] data)
    {
        var head = new byte[8];
        BE(head, 0, (uint)data.Length);
        for (int i = 0; i < 4; i++) head[4 + i] = (byte)type[i];
        s.Write(head);
        s.Write(data);
        uint c = 0xFFFFFFFFu;
        for (int i = 4; i < 8; i++) c = Crc[(c ^ head[i]) & 0xFF] ^ (c >> 8);
        foreach (var x in data) c = Crc[(c ^ x) & 0xFF] ^ (c >> 8);
        var tail = new byte[4];
        BE(tail, 0, c ^ 0xFFFFFFFFu);
        s.Write(tail);
    }
}
