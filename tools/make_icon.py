"""Draws DualLink's icons with Pillow.

Writes build/icon.ico (for the .exe) and duallink/assets/icon_on.ico / icon_off.ico (the tray icon - committed,
so the running app never needs Pillow).
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from duallink.icon import make_icon  # noqa: E402

TRAY_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64)]

os.makedirs(os.path.join(ROOT, "build"), exist_ok=True)
img = make_icon(256, on=True)
img.save(os.path.join(ROOT, "build", "icon.ico"),
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(os.path.join(ROOT, "build", "icon.png"))
assets = os.path.join(ROOT, "duallink", "assets")
os.makedirs(assets, exist_ok=True)
for on in (True, False):
    frames = [make_icon(w, on=on) for w, _ in TRAY_SIZES]         # drawn at each size, so 16 px stays crisp
    frames[-1].save(os.path.join(assets, f"icon_{'on' if on else 'off'}.ico"), sizes=TRAY_SIZES,
                    append_images=frames[:-1])
print("wrote build/icon.ico and duallink/assets/icon_on.ico, icon_off.ico")
