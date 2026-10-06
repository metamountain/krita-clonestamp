# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Clone Stamp tool loader.

Not a Python tool itself: it loads the native Clone Stamp tool
(lib/kritatoolclonestamp.dll) from this folder and registers it in Krita's
toolbox. That way the tool installs like any Python plugin -- Tools > Scripts >
Import Python Plugin from File -- without admin rights or copying files into
Krita's install folder. Same distribution model as Acly's krita-ai-tools.
"""
import ctypes
import os
import sys
from pathlib import Path

from krita import Extension, Krita

# The DLL is binary-locked to the Krita build it was compiled against.
SUPPORTED_VERSIONS = ("6.0.4",)  # 6.0.4 and 6.0.4.x
DLL_NAME = "kritatoolclonestamp.dll"


def _warn(text):
    # Shown once the main window exists; also printed to Krita's log.
    print(f"[clonestamp_tool] {text}")
    try:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QMessageBox

        QTimer.singleShot(3000, lambda: QMessageBox.warning(None, "Clone Stamp tool", text))
    except Exception:
        pass


def _shadow_copy(lib_file):
    """Load a copy, never the file in the plugin folder.

    Windows locks a loaded DLL. If Krita loaded lib/ directly, re-importing
    the plugin zip while Krita runs (an update) fails: Krita's importer
    deletes the old plugin folder first and hits the locked DLL. So the DLL
    is copied to a per-content cache file and loaded from there; stale
    copies from earlier versions are removed when they are no longer locked.
    """
    import hashlib
    import shutil

    data = lib_file.read_bytes()
    digest = hashlib.sha1(data).hexdigest()[:12]
    cache = Path(os.environ.get("LOCALAPPDATA", str(lib_file.parent))) / "clonestamp_tool"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"kritatoolclonestamp-{digest}.dll"
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        shutil.copyfile(lib_file, tmp)
        os.replace(tmp, target)
    for old in cache.glob("kritatoolclonestamp-*.dll"):
        if old != target:
            try:
                old.unlink()
            except OSError:
                pass  # still loaded by another Krita instance
    return target


class CloneStampLoader(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        version = Krita.instance().version()
        if sys.platform not in ("win32", "cygwin", "msys"):
            _warn("This build of the Clone Stamp tool is for Windows only.")
            return
        if not version.startswith(SUPPORTED_VERSIONS):
            _warn(f"This build of the Clone Stamp tool is for Krita {', '.join(SUPPORTED_VERSIONS)} "
                  f"only, but this is Krita {version}. The tool was not loaded.\n"
                  "Get the matching version: https://github.com/metamountain/krita-clonestamp/releases")
            return

        lib_dir = Path(__file__).parent / "lib"
        lib_file = lib_dir / DLL_NAME
        try:
            load_file = _shadow_copy(lib_file)
        except OSError as e:
            _warn(f"Failed to prepare {lib_file}: {e}")
            return
        # Dependencies (libkritaui.dll, Qt, ...) live next to krita.exe and are
        # already loaded; keep them findable while the DLL resolves its imports.
        prev_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join([str(lib_dir), str(Path(sys.executable).parent), prev_path])
        try:
            lib = ctypes.CDLL(str(load_file))
            lib.load_clonestamp_plugin()
        except (OSError, AttributeError) as e:
            _warn(f"Failed to load {lib_file}: {e}")
        finally:
            os.environ["PATH"] = prev_path

    def setup(self):
        pass

    def createActions(self, window):
        pass


Krita.instance().addExtension(CloneStampLoader(Krita.instance()))
