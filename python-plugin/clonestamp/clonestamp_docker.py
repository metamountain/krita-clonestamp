# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>

from krita import DockWidget, Krita
from PyQt5.QtCore import QEvent, QPointF, QTimer, Qt, QUrl
from PyQt5.QtGui import QColor, QCursor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QOpenGLWidget,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from . import clonestamp_core as core


def _find_canvas_widget():
    window = Krita.instance().activeWindow()
    if window is None:
        return None
    qwin = window.qwindow()
    if qwin is None:
        return None
    central = qwin.centralWidget()
    if central is None:
        return None
    return central.findChild(QOpenGLWidget)


class ClonestampDocker(DockWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Clonestamp Tool with Preview")

        self._canvas_widget = None
        self._stroke_active = False
        self._resize_active = False
        self._resize_start_global = None
        self._resize_start_size = None
        self._timer = QTimer(self)
        self._timer.setInterval(30)  # ~33Hz polling of QCursor.pos()
        self._timer.timeout.connect(self._onTimerTick)
        self._last_cursor_zoom = -1.0
        self._last_cursor_size = -1

        widget = QWidget()
        layout = QVBoxLayout()

        # --- Primary flow: Ctrl+click to sample, drag on canvas to paint ---
        self.enableCheck = QCheckBox("Enable Clone Brush")
        self.enableCheck.toggled.connect(self.onEnableToggled)
        layout.addWidget(self.enableCheck)

        hint = QLabel(
            "Ctrl+Click on canvas = sample source. Click+drag = paint. "
            "Shift+drag = resize brush. Clicks are consumed while enabled, "
            "so the active Krita tool won't also paint on the same drag.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.liveSourceLabel = QLabel("Source: none")
        self.liveSourceLabel.setWordWrap(True)
        layout.addWidget(self.liveSourceLabel)

        sizeRow = QHBoxLayout()
        sizeRow.addWidget(QLabel("Size:"))
        self.sizeSpin = QSpinBox()
        self.sizeSpin.setRange(1, 2000)
        self.sizeSpin.setValue(core.STATE.brush_size)
        self.sizeSpin.setSuffix(" px")
        self.sizeSpin.valueChanged.connect(self._onSizeChanged)
        sizeRow.addWidget(self.sizeSpin)
        layout.addLayout(sizeRow)

        hardnessRow = QHBoxLayout()
        hardnessRow.addWidget(QLabel("Hardness:"))
        self.hardnessSpin = QSpinBox()
        self.hardnessSpin.setRange(0, 100)
        self.hardnessSpin.setValue(int(core.STATE.brush_hardness * 100))
        self.hardnessSpin.setSuffix(" %")
        self.hardnessSpin.valueChanged.connect(self._onHardnessChanged)
        hardnessRow.addWidget(self.hardnessSpin)
        layout.addLayout(hardnessRow)

        brushOpacityRow = QHBoxLayout()
        brushOpacityRow.addWidget(QLabel("Opacity:"))
        self.brushOpacitySpin = QSpinBox()
        self.brushOpacitySpin.setRange(0, 100)
        self.brushOpacitySpin.setValue(core.STATE.brush_opacity)
        self.brushOpacitySpin.setSuffix(" %")
        self.brushOpacitySpin.valueChanged.connect(self._onBrushOpacityChanged)
        brushOpacityRow.addWidget(self.brushOpacitySpin)
        layout.addLayout(brushOpacityRow)

        self.alignedCheck = QCheckBox("Aligned")
        self.alignedCheck.setChecked(core.STATE.aligned)
        self.alignedCheck.toggled.connect(self._onAlignedToggled)
        layout.addWidget(self.alignedCheck)

        sampleRow = QHBoxLayout()
        sampleRow.addWidget(QLabel("Sample:"))
        self.sampleCombo = QComboBox()
        self.sampleCombo.addItem("Current Layer")
        self.sampleCombo.addItem("All Layers")
        self.sampleCombo.setCurrentIndex(1 if core.STATE.sample_scope == "all" else 0)
        self.sampleCombo.currentIndexChanged.connect(self._onSampleScopeChanged)
        sampleRow.addWidget(self.sampleCombo)
        layout.addLayout(sampleRow)

        self.brushStatusLabel = QLabel("")
        self.brushStatusLabel.setWordWrap(True)
        layout.addWidget(self.brushStatusLabel)

        aboutRow = QHBoxLayout()
        versionLabel = QLabel("v{0}".format(core.VERSION))
        aboutRow.addWidget(versionLabel)
        githubLabel = QLabel('<a href="{0}">GitHub</a>'.format(core.GITHUB_URL))
        githubLabel.setOpenExternalLinks(True)
        aboutRow.addWidget(githubLabel)
        aboutRow.addStretch()
        updateButton = QPushButton("Check for Updates")
        # Opens the releases page in the browser rather than downloading and
        # replacing files while Krita is running -- auto-updating a plugin
        # whose module is currently loaded/imported is a much riskier can of
        # worms (partial overwrite, needing an exact restart timing) for
        # limited benefit over "here's where to get the latest version."
        updateButton.clicked.connect(self._onCheckForUpdates)
        aboutRow.addWidget(updateButton)
        layout.addLayout(aboutRow)

        layout.addStretch()
        widget.setLayout(layout)
        self.setWidget(widget)

        if core.STATE.has_point_source:
            self._describeLiveSource()

    # --- lifecycle -------------------------------------------------------

    def canvasChanged(self, canvas):
        pass

    def closeEvent(self, event):
        self._disarm()
        super().closeEvent(event)

    # --- brush settings ----------------------------------------------------

    def _onSizeChanged(self, value):
        core.STATE.brush_size = value
        self._updateBrushCursor()

    def _onHardnessChanged(self, value):
        core.STATE.brush_hardness = value / 100.0

    def _onBrushOpacityChanged(self, value):
        core.STATE.brush_opacity = value

    def _onAlignedToggled(self, checked):
        core.STATE.aligned = checked

    def _onSampleScopeChanged(self, index):
        core.STATE.sample_scope = "all" if index == 1 else "current"

    def _onCheckForUpdates(self):
        QDesktopServices.openUrl(QUrl(core.GITHUB_URL + "/releases/latest"))

    def _describeLiveSource(self):
        p = core.STATE.source_point
        self.liveSourceLabel.setText(
            "Source: layer '{0}' @ ({1:.0f}, {2:.0f})".format(
                core.STATE.source_node.name(), p.x(), p.y()))

    # --- Enable/disable the global click/drag filter ----------------------

    def onEnableToggled(self, checked):
        if checked:
            self._arm()
        else:
            self._disarm()

    def _arm(self):
        widget = _find_canvas_widget()
        if widget is None:
            self.brushStatusLabel.setText(
                "Could not find the canvas widget; Clone Brush can't be enabled.")
            self.enableCheck.setChecked(False)
            return
        self._canvas_widget = widget
        # Must be QApplication-global, not widget-local: tried scoping this
        # to self._canvas_widget directly (cheaper in principle -- routes
        # far fewer events through Python) but it silently stopped receiving
        # the canvas's MouseButtonPress/Release entirely, confirming Krita
        # doesn't deliver them straight to this widget instance in a way a
        # local filter observes. Global scope is required for correctness;
        # if performance is still a problem, the fix has to be cheaper work
        # *inside* eventFilter, not a narrower install target.
        QApplication.instance().installEventFilter(self)
        self._updateBrushCursor()
        canvas = self._currentCanvas()
        if canvas:
            self._last_cursor_zoom = canvas.zoomLevel()
        self._last_cursor_size = core.STATE.brush_size
        if core.STATE.has_point_source:
            self._timer.start()
        self.brushStatusLabel.setText("Clone Brush enabled.")

    def _disarm(self):
        if self._canvas_widget is not None:
            QApplication.instance().removeEventFilter(self)
            self._canvas_widget.unsetCursor()
        self._timer.stop()
        self._stroke_active = False
        self._resize_active = False
        self._canvas_widget = None

    # --- event filter: only press/release are used; drag is timer-driven --

    def eventFilter(self, obj, event):
        if obj is not self._canvas_widget:
            return False

        et = event.type()
        if et == QEvent.MouseButtonPress:
            return self._onCanvasPress(event)
        elif et == QEvent.MouseButtonRelease:
            return self._onCanvasRelease(event)
        return False

    def _currentCanvas(self):
        window = Krita.instance().activeWindow()
        view = window.activeView() if window else None
        return view.canvas() if view else None

    def _mapEventPos(self, event):
        canvas = self._currentCanvas()
        if canvas is None or self._canvas_widget is None:
            return None
        if not core.coordinate_mapping_reliable(canvas):
            self.brushStatusLabel.setText(
                "Canvas is rotated or mirrored; Clone Brush coordinates would be wrong. "
                "Reset canvas rotation/mirror to use it.")
            return None
        return core.map_widget_to_document(canvas, self._canvas_widget, event.pos())

    def _onCanvasPress(self, event):
        if event.button() != Qt.LeftButton:
            return False  # only left-click is ours; right/middle pass through untouched

        self._updateBrushCursor()
        canvas = self._currentCanvas()
        if canvas:
            self._last_cursor_zoom = canvas.zoomLevel()
        self._last_cursor_size = core.STATE.brush_size

        mods = event.modifiers()

        if mods & Qt.ShiftModifier:
            self._resize_active = True
            self._resize_start_global = QCursor.pos()
            self._resize_start_size = core.STATE.brush_size
            self._resize_start_hardness_pct = int(round(core.STATE.brush_hardness * 100))
            self._timer.start()
            return True

        doc_point = self._mapEventPos(event)
        if doc_point is None:
            return True  # still consume it; we just can't act on it right now
        doc = Krita.instance().activeDocument()

        try:
            if mods & Qt.ControlModifier:
                core.sample_source_point(doc, core.STATE, doc_point)
                self._describeLiveSource()
                self.brushStatusLabel.setText("Source sampled.")
            else:
                core.begin_stroke(core.STATE, doc_point)
                self._stroke_active = True
                self._timer.start()
        except core.ClonestampError as e:
            self.brushStatusLabel.setText(str(e))
            self._warn(str(e))
        return True

    def _onCanvasRelease(self, event):
        if event.button() != Qt.LeftButton:
            return False
        if self._stroke_active:
            doc = Krita.instance().activeDocument()
            if doc is not None:
                core.finalize_stroke(doc, core.STATE)
            core.end_stroke(core.STATE)
            self._stroke_active = False
        if self._resize_active:
            self._resize_active = False
            cursor_brush_status = (
                "Size: {0} px  Hardness: {1}%  Opacity: {2}%"
                .format(core.STATE.brush_size,
                        int(round(core.STATE.brush_hardness * 100)),
                        core.STATE.brush_opacity))
            self.brushStatusLabel.setText(cursor_brush_status)
            self._updateBrushCursor()
        return True

    def _onTimerTick(self):
        # Only ever needed while actively resizing (Shift+drag) or actively
        # painting (drag) -- there is no floating preview to keep polling
        # for anymore (see git history: a hover-only preview window, shown
        # at ~25Hz via setGeometry()/show() on an always-on-top translucent
        # window, reliably froze Krita hard during live testing -- removed
        # rather than further patched, since even a movement-throttled
        # version still froze during actual use).
        if self._canvas_widget is None:
            self._timer.stop()
            return
        if self._resize_active:
            self._onResizeTick()
            return
        if not self._stroke_active:
            self._timer.stop()
            return

        local_pos = self._canvas_widget.mapFromGlobal(QCursor.pos())
        canvas = self._currentCanvas()
        if canvas is None or not core.coordinate_mapping_reliable(canvas):
            return
        doc_point = core.map_widget_to_document(canvas, self._canvas_widget, QPointF(local_pos))
        if doc_point is None:
            return

        doc = Krita.instance().activeDocument()
        try:
            core.continue_stroke(doc, core.STATE, doc_point)
        except core.ClonestampError as e:
            self.brushStatusLabel.setText(str(e))
            self._stroke_active = False
            return

        # Refresh cursor if zoom or brush size changed since last paint.
        zoom = canvas.zoomLevel() if canvas else 1.0
        if (abs(zoom - self._last_cursor_zoom) > 0.01 or
                core.STATE.brush_size != self._last_cursor_size):
            self._last_cursor_zoom = zoom
            self._last_cursor_size = core.STATE.brush_size
            self._updateBrushCursor()

    def _onResizeTick(self):
        current = QCursor.pos()
        dx = current.x() - self._resize_start_global.x()
        # Screen y grows downward, so negate: dragging up increases
        # hardness, down softens -- matches the native tool's convention
        # (and Photoshop's on-canvas brush resize gesture).
        dy = current.y() - self._resize_start_global.y()

        new_size = max(1, int(self._resize_start_size + dx))
        size_changed = new_size != core.STATE.brush_size
        if size_changed:
            core.STATE.brush_size = new_size
            self.sizeSpin.blockSignals(True)
            self.sizeSpin.setValue(new_size)
            self.sizeSpin.blockSignals(False)

        new_hardness_pct = max(0, min(100, self._resize_start_hardness_pct - dy))
        new_hardness = new_hardness_pct / 100.0
        hardness_changed = new_hardness != core.STATE.brush_hardness
        if hardness_changed:
            core.STATE.brush_hardness = new_hardness
            self.hardnessSpin.blockSignals(True)
            self.hardnessSpin.setValue(new_hardness_pct)
            self.hardnessSpin.blockSignals(False)

        if size_changed or hardness_changed:
            self._updateBrushCursor()

    def _updateBrushCursor(self):
        """Sets the canvas cursor to a ring matching the current brush size
        (in screen pixels, accounting for zoom) -- Krita's own brush-outline
        cursor doesn't apply while another tool is active underneath ours.
        Draws a semi-transparent fill showing the softness gradient and a
        solid inner ring marking the fully-opaque zone."""
        if self._canvas_widget is None:
            return
        canvas = self._currentCanvas()
        zoom = canvas.zoomLevel() if canvas else 1.0
        diameter = max(4, int(core.STATE.brush_size * zoom))
        pixmap = QPixmap(diameter + 2, diameter + 2)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(Qt.NoBrush)

        # Filled radial gradient to show softness falloff (very faint).
        if core.STATE.brush_hardness < 1.0:
            grad = QRadialGradient((diameter + 2) / 2.0, (diameter + 2) / 2.0,
                                    (diameter + 2) / 2.0)
            grad.setColorAt(0.0, QColor(255, 255, 255, 12))
            grad.setColorAt(float(core.STATE.brush_hardness),
                            QColor(255, 255, 255, 12))
            grad.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setBrush(grad)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(1, 1, diameter, diameter)

        # Outer ring (full brush extent).
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 200), 1))
        painter.drawEllipse(1, 1, diameter, diameter)
        painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
        painter.drawEllipse(0, 0, diameter, diameter)

        # Inner ring (fully-opaque zone boundary).
        hardness_diameter = max(3, int(diameter * core.STATE.brush_hardness))
        inset = (diameter - hardness_diameter) // 2
        painter.setPen(QPen(QColor(0, 0, 0, 160), 1, Qt.DashLine))
        painter.drawEllipse(inset + 1, inset + 1, hardness_diameter, hardness_diameter)
        painter.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.DashLine))
        painter.drawEllipse(inset, inset, hardness_diameter, hardness_diameter)

        painter.end()
        center = (diameter + 2) // 2
        self._canvas_widget.setCursor(QCursor(pixmap, center, center))

    def _warn(self, message):
        window = Krita.instance().activeWindow()
        if window is not None and window.activeView() is not None:
            window.activeView().showFloatingMessage(message, QIcon(), 3000, 1)
