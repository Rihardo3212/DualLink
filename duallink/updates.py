"""
Updates from the GitHub Releases page this build came from: check, download, verify and install.

Installed copies update by running the new installer silently; a standalone DualLink.exe replaces itself
(a running .exe can be renamed on Windows, so the old one becomes DualLink.old.exe and is deleted next start).
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from . import __version__

try:                                  # written by the GitHub build (see .github/workflows/build.yml)
    from ._build_info import REPO
except Exception:                     # running from source: no repository known, no update checks
    REPO = ""

ALLOWED_HOSTS = ("github.com", "api.github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com", "github-releases.githubusercontent.com")


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


def _asset(data, test):
    for a in data.get("assets", []):
        name = a.get("name", "")
        if test(name.lower()):
            return {"name": name, "url": a.get("browser_download_url", ""), "size": int(a.get("size") or 0),
                    "digest": a.get("digest") or ""}
    return None


def check(repo=None, current=__version__, fetch=fetch_latest):
    """Returns details of a newer release, or None."""
    repo = repo if repo is not None else REPO
    if not repo:
        return None
    data = fetch(repo)
    tag = data.get("tag_name", "")
    if data.get("draft") or data.get("prerelease") or not is_newer(tag, current):
        return None
    installer = _asset(data, lambda n: n.startswith("duallink-setup") and n.endswith(".exe"))
    portable = _asset(data, lambda n: n == "duallink.exe")
    return {"version": tag.lstrip("vV"), "url": data.get("html_url", ""),
            "installer": installer["url"] if installer else None,
            "assets": {"installer": installer, "portable": portable},
            "notes": (data.get("body") or "").strip()[:4000]}


# ---- how is this copy running? ---------------------------------------------------------- #
def install_kind():
    """"installed" (via the installer), "portable" (a standalone DualLink.exe) or "source" (python main.py)."""
    if not getattr(sys, "frozen", False):
        return "source"
    exe_dir = pathlib.Path(sys.executable).resolve().parent
    return "installed" if any(exe_dir.glob("unins*.exe")) else "portable"


# ---- download ------------------------------------------------------------------------------ #
class UpdateError(Exception):
    pass


def download(asset, dest_dir=None, progress=lambda done, total: None, opener=urllib.request.urlopen,
             allow_any_host=False):
    """Download a release asset, check its size and SHA-256 (when GitHub provides it). Returns the file path."""
    url = asset["url"]
    host = urllib.parse.urlsplit(url).hostname or ""
    if not allow_any_host and (urllib.parse.urlsplit(url).scheme != "https" or host not in ALLOWED_HOSTS):
        raise UpdateError("The download address doesn't belong to GitHub - update cancelled.")
    dest_dir = pathlib.Path(dest_dir or tempfile.gettempdir()) / "DualLink-update"
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / pathlib.Path(urllib.parse.urlsplit(url).path).name
    part = final.with_suffix(final.suffix + ".part")
    sha = hashlib.sha256()
    total, done = int(asset.get("size") or 0), 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DualLink"})
        with opener(req, timeout=30) as r, open(part, "wb") as f:
            total = total or int(r.headers.get("Content-Length") or 0)
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                progress(done, total)
    except UpdateError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"The download failed ({e}).")
    if total and done != total:
        raise UpdateError("The download was incomplete - please try again.")
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:") and digest.split(":", 1)[1].lower() != sha.hexdigest():
        part.unlink(missing_ok=True)
        raise UpdateError("The downloaded file didn't match GitHub's checksum - update cancelled.")
    if final.exists():
        final.unlink()
    part.rename(final)
    return final


# ---- install ---------------------------------------------------------------------------------- #
DETACHED = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def independent_env():
    """Environment for programs that must outlive this one.

    A one-file PyInstaller app passes hidden variables to the programs it starts, pointing at its temporary
    unpack folder. A new DualLink started from here (directly or via the installer) would otherwise look for
    that folder after this process has deleted it: "Failed to load Python DLL ... _MEI...". This documented
    PyInstaller variable makes it start as a fresh, independent app instead.
    """
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def run_installer(path):
    """Silent update; the installer starts the new DualLink when it's done (see installer/DualLink.iss)."""
    subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/update=1"],
                     close_fds=True, env=independent_env(), creationflags=DETACHED if os.name == "nt" else 0)


def swap_portable(new_exe, current_exe=None, launch=True):
    """Replace a standalone DualLink.exe with the downloaded one and start it."""
    current = pathlib.Path(current_exe or sys.executable)
    old = current.with_name(current.stem + ".old" + current.suffix)
    if old.exists():
        old.unlink()
    current.rename(old)                       # allowed even while it's running
    try:
        pathlib.Path(new_exe).replace(current)
    except Exception:
        old.rename(current)                   # put the original back
        raise
    if launch:
        subprocess.Popen([str(current), "--updated"], close_fds=True, env=independent_env(),
                         creationflags=DETACHED if os.name == "nt" else 0)
    return current


def remove_leftovers(current_exe=None):
    """Delete DualLink.old.exe left behind by a standalone update (run at start-up)."""
    current = pathlib.Path(current_exe or sys.executable)
    old = current.with_name(current.stem + ".old" + current.suffix)
    try:
        if old.exists():
            old.unlink()
    except Exception:
        pass
