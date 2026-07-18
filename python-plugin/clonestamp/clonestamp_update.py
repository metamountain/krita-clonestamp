# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
"""Self-update: check the main branch on GitHub for a newer VERSION and, if
found, download the current plugin files straight into this install -- no
git required, so this works the same way whether or not the machine running
Krita has a local clone (most end users won't). Kept out of
clonestamp_core.py, whose own header reserves that file for pure logic with
no I/O of this kind.
"""

import os
import shutil
import urllib.request

GITHUB_RAW_BASE = "https://raw.githubusercontent.com/metamountain/krita-clonestamp/main/python-plugin/clonestamp/"

# Only files actually needed to run the plugin -- not the .desktop (that's
# registration metadata Krita already has from install time; overwriting it
# risks fighting whatever Krita wrote there) and not this file itself (a
# mid-update failure while overwriting the code doing the overwriting would
# be exactly the kind of thing that leaves the plugin unloadable).
UPDATE_FILES = ("clonestamp_core.py", "clonestamp_docker.py", "__init__.py")


class UpdateError(Exception):
    """Raised for any expected/user-facing update failure; callers show it and stop."""


def _parse_version(v):
    parts = []
    for p in v.strip().split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def remote_version_is_newer(remote, local):
    return _parse_version(remote) > _parse_version(local)


def fetch_remote_version(timeout=5):
    """Reads the VERSION string straight out of the main branch's
    clonestamp_core.py -- no GitHub API, no auth, just one small text file."""
    url = GITHUB_RAW_BASE + "clonestamp_core.py"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise UpdateError("Could not reach GitHub: {0}".format(e))
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("VERSION"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise UpdateError("Could not find VERSION in the downloaded file.")


def download_update(dest_dir, timeout=10):
    """Downloads each file in UPDATE_FILES into memory first and only writes
    them once all of them succeeded, so a mid-download failure (dropped
    connection, GitHub hiccup) can't leave a half-updated, unloadable plugin
    on disk. Also clears __pycache__ so a stale compiled .pyc can't shadow
    the newly written source on next load."""
    fetched = {}
    for name in UPDATE_FILES:
        url = GITHUB_RAW_BASE + name
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                fetched[name] = resp.read()
        except Exception as e:
            raise UpdateError("Failed downloading {0}: {1}".format(name, e))

    for name, data in fetched.items():
        with open(os.path.join(dest_dir, name), "wb") as f:
            f.write(data)

    pycache = os.path.join(dest_dir, "__pycache__")
    if os.path.isdir(pycache):
        shutil.rmtree(pycache, ignore_errors=True)
