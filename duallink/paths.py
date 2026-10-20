"""Where DualLink's bundled files are (next to the code, or inside the PyInstaller bundle)."""
import os
import pathlib
import sys


def resource(*parts):
    if hasattr(sys, "_MEIPASS"):
        base = os.path.join(sys._MEIPASS, "duallink")
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def config_dir():
    """%APPDATA%\\DualLink: settings, and engine.json while DualLink runs."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    p = pathlib.Path(base) / "DualLink"
    p.mkdir(parents=True, exist_ok=True)
    return p
