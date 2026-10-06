# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tools > Scripts > "Clone Stamp: Check for Updates".

Asks GitHub for the latest release, picks the zip built for the running Krita
version and installs it with Krita's own plugin importer -- the same thing as
Tools > Scripts > Import Python Plugin from File. Takes effect after a restart
(the running copy of the tool is loaded from a cache file, so the plugin
folder can be replaced while Krita runs).
"""
import json
import os
import tempfile
import urllib.request
from pathlib import Path

REPO = "metamountain/krita-clonestamp"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"


def _version_tuple(text):
    parts = []
    for p in text.lstrip("v").split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def asset_name(krita_version):
    # "6.0.4 (git c5b23e3)" / "6.0.4.1" -> builds are per 6.0.4.x line
    base = ".".join(krita_version.split()[0].split(".")[:3])
    return f"clonestamp_tool-krita-{base}-windows-x64.zip"


def check(current_version, krita_version, timeout=10):
    """Returns (status, info): status is 'uptodate', 'update', 'nobuild' or 'error'."""
    try:
        req = urllib.request.Request(API_LATEST, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": "clonestamp-tool-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            release = json.load(resp)
    except Exception as e:
        return "error", f"Could not reach GitHub: {e}"
    tag = release.get("tag_name", "")
    if _version_tuple(tag) <= _version_tuple(current_version):
        return "uptodate", tag
    wanted = asset_name(krita_version)
    for asset in release.get("assets", []):
        if asset.get("name") == wanted:
            return "update", {"tag": tag, "url": asset["browser_download_url"], "name": wanted}
    return "nobuild", tag


def install(url, plugin_dir, timeout=60):
    """Downloads the zip and installs it with Krita's importer. Returns the imported plugin names."""
    from plugin_importer.plugin_importer import PluginImporter  # Krita's built-in importer

    resources_dir = Path(plugin_dir).resolve().parents[1]  # .../krita (contains pykrita/)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        zip_path = os.path.join(tmp, "update.zip")
        req = urllib.request.Request(url, headers={"User-Agent": "clonestamp-tool-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(zip_path, "wb") as f:
            f.write(resp.read())
        importer = PluginImporter(zip_path, str(resources_dir), lambda plugin: True)
        try:
            return [p["name"] for p in importer.import_all()]
        finally:
            importer.archive.close()  # the importer leaves the zip open


def run_interactive(current_version, krita_version, plugin_dir, parent=None):
    """Menu action: check, ask, install, tell the user to restart."""
    from PyQt6.QtWidgets import QMessageBox

    title = "Clone Stamp: Check for Updates"
    status, info = check(current_version, krita_version)
    if status == "error":
        QMessageBox.warning(parent, title, f"{info}\n\nReleases: {RELEASES_PAGE}")
        return
    if status == "uptodate":
        QMessageBox.information(parent, title, f"You have the latest version ({current_version}).")
        return
    if status == "nobuild":
        QMessageBox.information(parent, title,
                                f"Version {info} is out, but there is no build for Krita "
                                f"{krita_version.split()[0]} yet.\n\n{RELEASES_PAGE}")
        return
    answer = QMessageBox.question(parent, title,
                                  f"Version {info['tag']} is available (installed: {current_version}).\n"
                                  "Install it now? It takes effect after restarting Krita.")
    if answer != QMessageBox.StandardButton.Yes:
        return
    try:
        install(info["url"], plugin_dir)
    except Exception as e:
        QMessageBox.warning(parent, title, f"Update failed: {e}\n\nYou can install it by hand from:\n{RELEASES_PAGE}")
        return
    QMessageBox.information(parent, title, f"Installed {info['tag']}. Please restart Krita.")
