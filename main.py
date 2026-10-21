"""DualLink - use Ethernet and a phone hotspot together for faster downloads.

    DualLink.exe              the engine (proxy + tray icon); opens the window (DualLinkWindow.exe)
    DualLink.exe --minimized  the engine only (Start with Windows)
    DualLink.exe --cleanup    run by the uninstaller
"""
import sys

if __name__ == "__main__":
    if "--cleanup" in sys.argv:              # run by the uninstaller, no window
        from duallink.controller import cleanup
        cleanup()
        sys.exit(0)
    from duallink import instance
    updated = "--updated" in sys.argv
    if not instance.acquire(wait=15 if updated else 0):
        instance.signal_show()               # already running: open its window / bring it to the front
        sys.exit(0)
    if getattr(sys, "frozen", False):
        from duallink.updates import remove_leftovers
        remove_leftovers()                   # DualLink.old.exe from a standalone update (up to 1.11)
    from duallink.service import Engine
    Engine(minimized="--minimized" in sys.argv, updated=updated).run()
