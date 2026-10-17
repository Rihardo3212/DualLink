"""Writes build/icon.ico for the .exe (run before PyInstaller)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from duallink.icon import make_icon  # noqa: E402

os.makedirs("build", exist_ok=True)
img = make_icon(256, on=True)
img.save("build/icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save("build/icon.png")
print("wrote build/icon.ico")
