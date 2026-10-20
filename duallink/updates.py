"""Checks the GitHub Releases page this build came from for a newer version."""
import json
import re
import urllib.request

from . import __version__

try:                                  # written by the GitHub build (see .github/workflows/build.yml)
    from ._build_info import REPO
except Exception:                     # running from source: no repository known, no update checks
    REPO = ""


def parse_version(text):
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def is_newer(tag, current=__version__):
    return parse_version(tag) > parse_version(current)


def fetch_latest(repo, timeout=10):
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"User-Agent": "DualLink", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def check(repo=None, current=__version__, fetch=fetch_latest):
    """Returns {"version", "url"} when a newer release exists, otherwise None."""
    repo = repo if repo is not None else REPO
    if not repo:
        return None
    data = fetch(repo)
    tag = data.get("tag_name", "")
    if data.get("draft") or data.get("prerelease") or not is_newer(tag, current):
        return None
    installer = next((a.get("browser_download_url") for a in data.get("assets", [])
                      if a.get("name", "").lower().startswith("duallink-setup")), None)
    return {"version": tag.lstrip("vV"), "url": data.get("html_url", ""), "installer": installer}
