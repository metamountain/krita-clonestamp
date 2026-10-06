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
        # Dependencies (libkritaui.dll, Qt, ...) live next to krita.exe and are
        # already loaded; keep them findable while the DLL resolves its imports.
        prev_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join([str(lib_dir), str(Path(sys.executable).parent), prev_path])
        try:
            lib = ctypes.CDLL(str(lib_file.resolve()))
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
