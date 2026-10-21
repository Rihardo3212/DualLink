using System.Text.Json.Nodes;

namespace DualLinkWindow;

/// <summary>
/// Made-up data in the same shape the engine sends, for screenshots (--demo) and for trying the window out
/// without touching the real DualLink.
/// </summary>
public sealed class DemoEngine : IEngine
{
    public int? Pid => null;

    readonly JsonObject settings = new()
    {
        ["eth_name"] = "Ethernet", ["hot_name"] = "Ethernet 2", ["extra"] = new JsonArray(),
        ["eth_ip"] = "192.168.8.5", ["hot_ip"] = "172.20.10.3",
        ["auto"] = true, ["recycle"] = true, ["share_eth"] = 1.0, ["share_hot"] = 1.0, ["quota_mb"] = 0,
        ["port"] = 1080, ["qbt_auto"] = true, ["qbt_url"] = "http://127.0.0.1:8080", ["qbt_user"] = "", ["qbt_pass"] = "",
        ["reconnect"] = true, ["tray"] = true, ["autostart"] = true, ["tray_hint_shown"] = true,
        ["fdm"] = true, ["jd"] = false, ["idm"] = false, ["firefox"] = false, ["other"] = false,
        ["auto_on_hotspot"] = true, ["auto_on_start"] = false, ["check_updates"] = true, ["native_window"] = true,
    };

    bool running = true;
    readonly DateTime started = DateTime.UtcNow;
    JsonObject speedtest = new() { ["running"] = false, ["phase"] = "", ["result"] = null };
    DateTime? speedtestEnds;

    static JsonArray Adapters() => new(
        new JsonObject { ["name"] = "Ethernet", ["ip"] = "192.168.8.5", ["present"] = true },
        new JsonObject { ["name"] = "Ethernet 2", ["ip"] = "172.20.10.3", ["present"] = true },
        new JsonObject { ["name"] = "Wi-Fi", ["ip"] = "192.168.1.23", ["present"] = true });

    public async Task<JsonNode?> CallAsync(string name, params JsonNode?[] args)
    {
        await Task.Delay(name == "traffic_check" ? 1500 : 15);
        switch (name)
        {
            case "ping":
            case "window_closed":
                return new JsonObject();
            case "get_init":
            case "refresh_adapters":
                return new JsonObject { ["settings"] = settings.DeepClone(), ["adapters"] = Adapters() };
            case "save_settings":
                if (args.Length > 0 && args[0] is JsonObject patch)
                    foreach (var (k, v) in patch) settings[k] = v?.DeepClone();
                return new JsonObject { ["settings"] = settings.DeepClone(), ["error"] = null };
            case "power":
                running = args.Length > 0 && args[0].AsBool();
                return new JsonObject { ["ok"] = true };
            case "status":
                return Status();
            case "test_qbt":
                return new JsonObject { ["ok"] = true, ["message"] = "Connected to qBittorrent v5.0.2" };
            case "retry_qbt":
                return new JsonObject { ["state"] = "connected", ["message"] = "" };
            case "start_speed_test":
                speedtest = new JsonObject { ["running"] = true, ["phase"] = "Ethernet", ["result"] = speedtest["result"]?.DeepClone() };
                speedtestEnds = DateTime.UtcNow.AddSeconds(4);
                return new JsonObject { ["ok"] = true };
            case "check_updates":
                return new JsonObject { ["ok"] = true, ["update"] = Update() };
            case "start_update":
                return new JsonObject { ["ok"] = false, ["message"] = "This is a demo - nothing to install." };
            case "traffic_check":
                return TrafficCheck();
            case "copy":
            case "open_url":
                return true;
            default:
                throw new EngineError($"Unknown request '{name}'.");
        }
    }

    static JsonObject Update() => new()
    {
        ["version"] = "1.11.1",
        ["url"] = "https://github.com/Rihardo3212/DualLink/releases/tag/v1.11.1",
        ["notes"] = "## What's Changed\n* Faster start of the new window\n* Fix the tray tooltip with three connections\n\n**Full Changelog**: https://github.com/Rihardo3212/DualLink/compare/v1.11.0...v1.11.1",
    };

    JsonObject Status()
    {
        double t = (DateTime.UtcNow - started).TotalSeconds;
        if (speedtestEnds is { } end && DateTime.UtcNow >= end)
        {
            speedtestEnds = null;
            speedtest = new JsonObject
            {
                ["running"] = false, ["phase"] = "",
                ["result"] = new JsonObject
                {
                    ["ts"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                    ["links"] = new JsonArray(
                        new JsonObject { ["name"] = "Ethernet", ["label"] = "Ethernet", ["bps"] = 2_350_000.0, ["error"] = null },
                        new JsonObject { ["name"] = "Ethernet 2", ["label"] = "Hotspot", ["bps"] = 4_100_000.0, ["error"] = null }),
                },
            };
        }
        var links = new JsonArray();
        string[] names = ["Ethernet", "Hotspot"], adapters = ["Ethernet", "Ethernet 2"], kinds = ["eth", "hot"];
        double[] base_ = [1.9 * 1048576, 3.3 * 1048576];
        double combined = 0;
        for (int i = 0; i < 2; i++)
        {
            var hist = new JsonArray();
            for (int k = 0; k < 60; k++)
            {
                double age = 59 - k;
                double ramp = Math.Clamp((t + 40 - age) / 6.0, 0, 1);
                double v = running ? base_[i] * ramp * (1 + 0.12 * Math.Sin((t - age) / (3 + i) + i)) : 0;
                hist.Add((JsonNode?)JsonValue.Create(Math.Round(v)));
            }
            double speed = hist[59].AsNum();
            combined += speed;
            links.Add((JsonNode)new JsonObject
            {
                ["name"] = names[i], ["ip"] = i == 0 ? "192.168.8.5" : "172.20.10.3", ["connections"] = running ? 8 : 0,
                ["adapter"] = adapters[i], ["kind"] = kinds[i], ["present"] = true, ["share"] = i == 0 ? 37 : 63,
                ["speed"] = speed, ["down"] = 0, ["up"] = 0, ["quota"] = 0, ["over_quota"] = false, ["history"] = hist,
            });
        }
        if (speedtest.Bool("running"))
            speedtest["phase"] = (speedtestEnds - DateTime.UtcNow)?.TotalSeconds > 2 ? "Ethernet" : "Hotspot";
        return new JsonObject
        {
            ["running"] = running, ["combined"] = combined,
            ["app_live"] = new JsonObject
            {
                ["qbt"] = new JsonObject { ["connections"] = running ? 9 : 0, ["programs"] = new JsonArray("qbittorrent.exe") },
                ["fdm"] = new JsonObject { ["connections"] = running ? 8 : 0, ["programs"] = new JsonArray("fdm.exe") },
            },
            ["seen_apps"] = new JsonObject { ["qbt"] = 1792500000, ["fdm"] = 1792500000 },
            ["links"] = links, ["moved"] = 3, ["dht"] = running, ["port"] = settings["port"]?.DeepClone(),
            ["qbt"] = new JsonObject { ["state"] = running ? "connected" : "off", ["message"] = "" },
            ["auto"] = settings.Bool("auto"), ["fdm"] = settings.Bool("fdm"),
            ["apps"] = new JsonObject
            {
                ["fdm"] = settings.Bool("fdm"), ["jd"] = settings.Bool("jd"), ["idm"] = settings.Bool("idm"),
                ["firefox"] = settings.Bool("firefox"), ["other"] = settings.Bool("other"),
            },
            ["speedtest"] = speedtest.DeepClone(), ["update"] = Update(),
            ["update_state"] = new JsonObject { ["phase"] = "idle", ["done"] = 0, ["total"] = 0, ["message"] = "" },
            ["just_updated"] = false, ["version"] = "1.11.0", ["qbt_auto"] = settings.Bool("qbt_auto"),
            ["standby"] = false, ["standby_error"] = null, ["native_available"] = true,
        };
    }

    JsonObject TrafficCheck() => new()
    {
        ["ok"] = true, ["seconds"] = 3.0,
        ["links"] = new JsonArray(
            new JsonObject { ["label"] = "Ethernet", ["adapter"] = "Ethernet", ["windows"] = 2_050_000.0, ["dualink"] = 1_990_000.0, ["other"] = 0.0, ["direct_apps"] = new JsonArray() },
            new JsonObject
            {
                ["label"] = "Hotspot", ["adapter"] = "Ethernet 2", ["windows"] = 3_900_000.0, ["dualink"] = 3_420_000.0, ["other"] = 310_000.0,
                ["direct_apps"] = new JsonArray(new JsonObject { ["name"] = "msedge.exe", ["connections"] = 4 }),
            }),
        ["through"] = new JsonArray(
            new JsonObject { ["name"] = "qbittorrent.exe", ["connections"] = 9 },
            new JsonObject { ["name"] = "fdm.exe", ["connections"] = 8 }),
        ["refused_ipv6"] = 0, ["failed"] = 0,
        ["notes"] = new JsonArray(
            new JsonObject { ["kind"] = "warn", ["text"] = "302.7 KiB/s on hotspot isn't going through DualLink. Programs connecting over it directly: msedge.exe." }),
    };
}
