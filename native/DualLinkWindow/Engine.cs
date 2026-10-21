using System.Diagnostics;
using System.Net.Http;
using System.Text;
using System.Text.Json.Nodes;

namespace DualLinkWindow;

/// <summary>What the window asks of DualLink's engine (the same requests as the web window's js_api).</summary>
public interface IEngine
{
    Task<JsonNode?> CallAsync(string name, params JsonNode?[] args);
    int? Pid { get; }
}

public sealed class EngineError(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>
/// Talks to the engine's control API: POST http://127.0.0.1:port/api/name with the token from engine.json.
/// </summary>
public sealed class EngineClient : IEngine, IDisposable
{
    static readonly Dictionary<string, int> Timeouts = new()
    {
        ["traffic_check"] = 90, ["test_qbt"] = 40, ["check_updates"] = 40, ["refresh_adapters"] = 40,
        ["get_init"] = 40, ["power"] = 60, ["retry_qbt"] = 60, ["save_settings"] = 60, ["ping"] = 3,
    };

    readonly HttpClient http;
    readonly int port;
    readonly string token;
    public int? Pid { get; }

    EngineClient(int port, string token, int? pid)
    {
        this.port = port;
        this.token = token;
        Pid = pid;
        // never through a system proxy: the engine is on this PC
        http = new HttpClient(new SocketsHttpHandler { UseProxy = false, PooledConnectionIdleTimeout = TimeSpan.FromMinutes(5) })
        {
            Timeout = Timeout.InfiniteTimeSpan,
        };
    }

    public static string InfoPath()
    {
        var appData = Environment.GetEnvironmentVariable("APPDATA");
        if (string.IsNullOrEmpty(appData)) appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        return Path.Combine(appData, "DualLink", "engine.json");
    }

    static EngineClient? Load()
    {
        try
        {
            var info = JsonNode.Parse(File.ReadAllText(InfoPath()));
            if (info == null) return null;
            return new EngineClient(info["port"]!.GetValue<int>(), info["token"]!.GetValue<string>(), info["pid"]?.GetValue<int>());
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// <summary>The running engine, waiting a while if it's still starting. Null if DualLink isn't running.</summary>
    public static EngineClient? Find()
    {
        var client = TryConnect(TimeSpan.FromSeconds(3));
        if (client == null && Native.MutexHeld(Native.EngineMutex))
            client = TryConnect(TimeSpan.FromSeconds(20));
        return client;
    }

    static EngineClient? TryConnect(TimeSpan wait)
    {
        var end = DateTime.UtcNow + wait;
        while (true)
        {
            var client = Load();
            if (client != null)
            {
                try
                {
                    client.CallAsync("ping").GetAwaiter().GetResult();
                    return client;
                }
                catch (EngineError)
                {
                    client.Dispose();
                }
            }
            if (DateTime.UtcNow >= end) return null;
            Thread.Sleep(250);
        }
    }

    /// <summary>Starts DualLink.exe (one folder up from this window's folder).</summary>
    public static bool StartEngine()
    {
        var exe = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "DualLink.exe"));
        if (!File.Exists(exe)) return false;
        Process.Start(new ProcessStartInfo(exe) { UseShellExecute = false });
        return true;
    }

    public async Task<JsonNode?> CallAsync(string name, params JsonNode?[] args)
    {
        var body = new JsonObject { ["args"] = new JsonArray(args) };
        using var request = new HttpRequestMessage(HttpMethod.Post, $"http://127.0.0.1:{port}/api/{name}")
        {
            Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json"),
        };
        request.Headers.Add("X-DualLink-Token", token);
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(Timeouts.GetValueOrDefault(name, 15)));
        string text;
        try
        {
            using var response = await http.SendAsync(request, cts.Token).ConfigureAwait(false);
            text = await response.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
        }
        catch (Exception e)
        {
            throw new EngineError("DualLink's background engine isn't responding.", e);
        }
        JsonObject? reply;
        try
        {
            reply = JsonNode.Parse(text) as JsonObject;
        }
        catch (Exception e)
        {
            throw new EngineError("Unexpected reply from DualLink's engine.", e);
        }
        if (reply == null || !reply.Bool("ok"))
            throw new EngineError(reply?.Str("error") is { Length: > 0 } err ? err : "DualLink's engine refused the request.");
        var result = reply["result"];
        reply.Remove("result");                 // detach it so it can be used on its own
        return result;
    }

    public void Dispose() => http.Dispose();
}

/// <summary>Forgiving accessors for the engine's JSON (missing or mistyped values give defaults).</summary>
public static class Json
{
    public static JsonNode? Get(this JsonNode? node, string key) => node is JsonObject o && o.TryGetPropertyValue(key, out var v) ? v : null;

    public static bool Bool(this JsonNode? node, string key) => node.Get(key).AsBool();
    public static double Num(this JsonNode? node, string key, double fallback = 0) => node.Get(key).AsNum(fallback);
    public static string Str(this JsonNode? node, string key, string fallback = "") => node.Get(key).AsStr(fallback);
    public static JsonArray Arr(this JsonNode? node, string key) => node.Get(key) as JsonArray ?? new JsonArray();
    public static JsonObject Obj(this JsonNode? node, string key) => node.Get(key) as JsonObject ?? new JsonObject();
    public static bool Has(this JsonNode? node, string key) => node.Get(key) != null;

    public static bool AsBool(this JsonNode? v)
    {
        if (v is not JsonValue jv) return false;
        if (jv.TryGetValue<bool>(out var b)) return b;
        if (jv.TryGetValue<double>(out var d)) return d != 0;
        if (jv.TryGetValue<string>(out var s)) return s.Length > 0;
        return false;
    }

    public static double AsNum(this JsonNode? v, double fallback = 0)
    {
        if (v is not JsonValue jv) return fallback;
        if (jv.TryGetValue<double>(out var d)) return d;
        if (jv.TryGetValue<long>(out var l)) return l;
        if (jv.TryGetValue<int>(out var i)) return i;
        if (jv.TryGetValue<bool>(out var b)) return b ? 1 : 0;
        if (jv.TryGetValue<string>(out var s) && double.TryParse(s, System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out var p)) return p;
        return fallback;
    }

    public static string AsStr(this JsonNode? v, string fallback = "")
    {
        if (v is not JsonValue jv) return fallback;
        if (jv.TryGetValue<string>(out var s)) return s;
        return jv.ToJsonString();
    }

    public static JsonNode? Clone(this JsonNode? node) => node?.DeepClone();
}
