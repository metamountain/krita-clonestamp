# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>

from krita import Extension, Krita
from PyQt5.QtGui import QIcon

from . import clonestamp_core as core


class ClonestampExtension(Extension):

    def __init__(self, parent):
        super().__init__(parent)

    def setup(self):
        pass

    def createActions(self, window):
        captureAction = window.createAction(
            "clonestamp_capture_source", "Clone Stamp: Capture Source", "")
        captureAction.triggered.connect(self.captureSource)

        stampAction = window.createAction(
            "clonestamp_stamp", "Clone Stamp: Stamp Here", "")
        stampAction.triggered.connect(self.stampHere)

    def captureSource(self):
        window = Krita.instance().activeWindow()
        doc = Krita.instance().activeDocument()
        try:
            rect = core.capture_source(doc, core.STATE)
            self._notify(window, "Clone Stamp source captured: {0}x{1}".format(
                rect.width(), rect.height()))
        except core.ClonestampError as e:
            self._notify(window, str(e))

    def stampHere(self):
        # No docker fields to read from a bare keyboard shortcut: always use
        # the current selection as the destination and the last feather/opacity
        # used (defaults: 0px feather, 100% opacity).
        window = Krita.instance().activeWindow()
        doc = Krita.instance().activeDocument()
        try:
            core.stamp(doc, core.STATE, True, 0, 0,
                       core.STATE.last_feather, core.STATE.last_opacity)
            self._notify(window, "Stamped.")
        except core.ClonestampError as e:
            self._notify(window, str(e))

    def _notify(self, window, message):
        if window is not None and window.activeView() is not None:
            window.activeView().showFloatingMessage(message, QIcon(), 2000, 2)
