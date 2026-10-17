"""DualLink - combine Ethernet and a phone hotspot for qBittorrent downloads."""
import sys

from duallink.app import DualLinkApp

if __name__ == "__main__":
    DualLinkApp(start_minimized="--minimized" in sys.argv).run()
