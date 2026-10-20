"""DualLink - use Ethernet and a phone hotspot together for faster downloads."""
import sys

if __name__ == "__main__":
    if "--cleanup" in sys.argv:              # run by the uninstaller, no window
        from duallink.controller import cleanup
        cleanup()
        sys.exit(0)
    from duallink.app import DualLinkApp
    DualLinkApp(start_minimized="--minimized" in sys.argv).run()
